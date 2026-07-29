import ast
import re
from pathlib import Path

import pytest

from matterhorn.store import SQLiteStore


def test_transaction_rolls_back_every_write(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.mark_card("s", "c", "hash")
            raise RuntimeError("abort")
    assert store.card_payload_hash("s", "c") is None


def test_nested_transaction_uses_savepoint(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    with store.transaction():
        store.mark_card("s", "outer", "hash")
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.mark_card("s", "inner", "hash")
                raise RuntimeError("abort inner")
    assert store.card_payload_hash("s", "outer") == "hash"
    assert store.card_payload_hash("s", "inner") is None


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
