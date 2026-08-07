import ast
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from matterhorn.contracts import Record, SourceRef
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


def test_staging_upserts_latest_record_and_reads_deterministic_context(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "staging.db")

    def record(
        native_id: str,
        sent_at: datetime,
        content: str,
        *,
        thread_id: str = "room:thread",
        revoked_at: datetime | None = None,
    ) -> Record:
        return Record(
            record_id=f"room:{native_id}",
            native_id=native_id,
            container_id="room",
            thread_id=thread_id,
            sent_at=sent_at,
            author={"id": "ada", "kind": "human"},
            content=content,
            revoked_at=revoked_at,
            kind="revocation" if revoked_at is not None else "message",
        )

    start = datetime(2026, 8, 4, 9, tzinfo=UTC)
    original = record("same", start, "Original content")
    edited = record("same", start, "Edited content")
    older = record("older", start - timedelta(minutes=1), "Older content")
    revoked = record(
        "revoked",
        start + timedelta(minutes=1),
        "Revoked content",
        revoked_at=start + timedelta(minutes=2),
    )
    other_thread = record(
        "other",
        start + timedelta(minutes=2),
        "Other thread",
        thread_id="room:other-thread",
    )
    with store.transaction():
        store.stage_records(
            "scope",
            [original, older, revoked, other_thread],
            staged_at=start,
        )
        store.stage_records(
            "scope",
            [edited],
            staged_at=start + timedelta(minutes=3),
        )

    context = store.staged_records(
        "scope",
        "room",
        sent_at_from=start - timedelta(days=7),
        sent_at_before=start + timedelta(hours=1),
        thread_id="room:thread",
        exclude_record_ids=[],
    )

    assert [(item.record_id, item.content) for item in context] == [
        ("room:older", "Older content"),
        ("room:same", "Edited content"),
    ]


def test_recent_staged_orders_across_scopes_and_excludes_revoked(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "recent-staging.db")
    sent_at = datetime(2026, 8, 4, 9, tzinfo=UTC)

    def record(
        record_id: str,
        *,
        container_id: str,
        sent_offset: int = 0,
        revoked: bool = False,
    ) -> Record:
        observed = sent_at + timedelta(minutes=sent_offset)
        return Record(
            record_id=f"{container_id}:{record_id}",
            native_id=record_id,
            container_id=container_id,
            sent_at=observed,
            author={"id": "dana", "display_name": "Dana Reyes", "kind": "human"},
            content=f"Fictional stream item {record_id}.",
            revoked_at=observed + timedelta(minutes=1) if revoked else None,
            kind="revocation" if revoked else "message",
        )

    with store.transaction():
        store.stage_records(
            "scope-a",
            [
                record("a", container_id="room-a"),
                record("z", container_id="room-a"),
                record("revoked", container_id="room-a", revoked=True),
            ],
            staged_at=sent_at + timedelta(minutes=2),
        )
        store.stage_records(
            "scope-b",
            [record("new-stage", container_id="room-b", sent_offset=-5)],
            staged_at=sent_at + timedelta(minutes=3),
        )

    assert [row.record.record_id for row in store.recent_staged(None, limit=10)] == [
        "room-b:new-stage",
        "room-a:z",
        "room-a:a",
    ]
    assert [
        row.record.record_id for row in store.recent_staged("scope-a", limit=1)
    ] == ["room-a:z"]


def test_staging_purge_deletes_only_raw_rows(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "staging-purge.db")
    sent_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
    record = Record(
        record_id="room:old",
        native_id="old",
        container_id="room",
        sent_at=sent_at,
        author={"id": "ada", "kind": "human"},
        content="Fictional retained evidence.",
    )
    source_ref = SourceRef(
        source_id=record.record_id,
        sent_at=sent_at,
        sender="Ada",
        excerpt=record.content,
    )
    with store.transaction():
        store.stage_records("scope", [record], staged_at=sent_at)
        store.observe_source("scope", source_ref)

    with store.transaction():
        deleted = store.purge_staged_records(
            "scope",
            before=sent_at + timedelta(days=1),
        )

    assert deleted == 1
    assert store.source_metadata("scope")[0].source_id == "room:old"
    assert store.staged_records(
        "scope",
        "room",
        sent_at_from=sent_at - timedelta(days=1),
        sent_at_before=sent_at + timedelta(days=2),
        thread_id=None,
        exclude_record_ids=[],
    ) == []


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
    assert {
        "attempts",
        "last_error",
        "unchanged_dropped",
        "handle_conflicts",
        "route_handle",
        "route_thread",
        "route_evidence",
        "route_model",
        "route_new",
        "route_review",
        "route_disagreements",
    } <= columns
    assert store.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='review_queue'"
    ).fetchone()
    assert store.connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='correction_captures'"
    ).fetchone()
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
    assert result.result.unchanged_dropped == 0
    assert result.result.gate.unchanged_dropped == 0
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


def test_postgres_never_calls_executemany_on_connection() -> None:
    path = Path(__file__).resolve().parents[1] / "src/matterhorn/store/postgres.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "executemany":
            continue
        owner = node.func.value
        if (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "self"
            and owner.attr == "connection"
        ):
            violations.append(node.lineno)

    assert violations == []


def test_postgres_correction_capture_sql_uses_c_collation_and_cursors() -> None:
    path = Path(__file__).resolve().parents[1] / "src/matterhorn/store/postgres.py"
    source = path.read_text(encoding="utf-8")
    table_sql = source.split(
        "CREATE TABLE IF NOT EXISTS correction_captures (", 1
    )[1].split(");", 1)[0]
    for column in ("capture_id", "scope_id", "kind", "status"):
        assert re.search(rf"{column} TEXT COLLATE \"C\"", table_sql)

    tree = ast.parse(source)
    store_class = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == "PostgresStore"
    )
    capture_methods = {
        "capture_window",
        "add_correction_capture",
        "correction_capture",
        "correction_captures",
        "resolve_correction_capture",
    }
    checked = set()
    for method in store_class.body:
        if isinstance(method, ast.FunctionDef) and method.name in capture_methods:
            method_source = ast.get_source_segment(source, method)
            assert method_source is not None
            assert "self.connection.cursor()" in method_source
            checked.add(method.name)
    assert checked == capture_methods


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
