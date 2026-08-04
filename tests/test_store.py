import ast
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from matterhorn.store import SQLiteStore


def test_transaction_rolls_back_every_write(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(RuntimeError), store.transaction():
        store.mark_card("s", "c", "hash")
        raise RuntimeError("abort")
    assert store.card_payload_hash("s", "c") is None


def test_nested_transaction_uses_savepoint(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with store.transaction():
        store.mark_card("s", "outer", "hash")
        with pytest.raises(RuntimeError), store.transaction():
            store.mark_card("s", "inner", "hash")
            raise RuntimeError("abort inner")
    assert store.card_payload_hash("s", "outer") == "hash"
    assert store.card_payload_hash("s", "inner") is None


def test_shared_sqlite_store_serializes_reads_and_write_transactions(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "threaded.db")
    writer_entered = threading.Event()
    release_writer = threading.Event()
    reader_started = threading.Event()
    reader_finished = threading.Event()
    errors: list[BaseException] = []

    def hold_write_transaction() -> None:
        try:
            with store.transaction():
                store.update_sync_position(
                    "fictional-team",
                    "fictional-room",
                    watermark=datetime(2026, 8, 4, tzinfo=UTC),
                    cursor="cursor-0",
                )
                writer_entered.set()
                if not release_writer.wait(timeout=2):
                    raise TimeoutError("test did not release writer")
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    def blocked_read() -> None:
        try:
            reader_started.set()
            store.sync_positions("fictional-team")
            reader_finished.set()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    writer = threading.Thread(target=hold_write_transaction)
    reader = threading.Thread(target=blocked_read)
    writer.start()
    assert writer_entered.wait(timeout=2)
    reader.start()
    assert reader_started.wait(timeout=2)
    assert not reader_finished.wait(timeout=0.05)
    release_writer.set()
    writer.join(timeout=2)
    reader.join(timeout=2)
    assert not writer.is_alive()
    assert not reader.is_alive()
    assert errors == []

    barrier = threading.Barrier(3)
    start = datetime(2026, 8, 4, tzinfo=UTC)

    def hammer_writer() -> None:
        try:
            barrier.wait(timeout=2)
            for index in range(250):
                with store.transaction():
                    store.update_sync_position(
                        "fictional-team",
                        "fictional-room",
                        watermark=start + timedelta(seconds=index),
                        cursor=f"cursor-{index}",
                    )
                    store.scope_exists("fictional-team")
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    def hammer_reader() -> None:
        try:
            barrier.wait(timeout=2)
            for _ in range(500):
                store.sync_positions("fictional-team")
                store.list_scopes()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [
        threading.Thread(target=hammer_writer),
        threading.Thread(target=hammer_reader),
        threading.Thread(target=hammer_reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert store.sync_positions("fictional-team")[0].cursor == "cursor-249"


def test_sqlite_migrates_legacy_task_retry_columns(tmp_path) -> None:
    path = tmp_path / "legacy-tasks.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            scope_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            accepted INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            newest_message_at TEXT,
            status TEXT NOT NULL,
            cards_produced INTEGER NOT NULL DEFAULT 0,
            new_assertions INTEGER NOT NULL DEFAULT 0,
            gate_accepted INTEGER NOT NULL DEFAULT 0,
            gate_rejected_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteStore(path)
    columns = {
        row["name"] for row in store.connection.execute("PRAGMA table_info(tasks)")
    }
    assert {"attempts", "last_error", "handle_conflicts"} <= columns
    assert store.create_task(
        task_id="task-legacy",
        scope_id="fictional-team",
        kind="messages",
        payload={"records": []},
        accepted=0,
        created_at=datetime(2026, 8, 4, tzinfo=UTC),
        newest_message_at=None,
    )
    result = store.task("task-legacy")
    assert result is not None
    assert result.result.attempts == 0
    assert result.result.last_error is None


def test_postgres_store_declares_every_sqlite_spi_method() -> None:
    root = Path(__file__).resolve().parents[1] / "src/matterhorn/store"

    def methods(path: Path, class_name: str) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        class_node = next(
            item
            for item in tree.body
            if isinstance(item, ast.ClassDef) and item.name == class_name
        )
        return {
            item.name
            for item in class_node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not item.name.startswith("_")
        }

    sqlite_methods = methods(root / "sqlite.py", "SQLiteStore")
    postgres_methods = methods(root / "postgres.py", "PostgresStore")
    assert postgres_methods == sqlite_methods


def test_backend_sql_and_connections_are_confined_to_store_package() -> None:
    package = Path(__file__).resolve().parents[1] / "src/matterhorn"
    violations: list[str] = []
    sql_keyword = re.compile(
        r"\b(?:SELECT|INSERT|UPDATE|DELETE|JOIN|WHERE|ORDER\s+BY)\b",
        re.IGNORECASE,
    )
    connection_access = re.compile(r"\b_?store\.connection\b")

    for path in sorted(package.rglob("*.py")):
        if path.parent == package / "store":
            continue
        text = path.read_text(encoding="utf-8")
        if connection_access.search(text):
            violations.append(f"{path.relative_to(package)}: store.connection")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not sql_keyword.search(node.value):
                continue
            if "?" in node.value or "%s" in node.value:
                violations.append(
                    f"{path.relative_to(package)}:{node.lineno}: SQL placeholder"
                )

    assert violations == []
