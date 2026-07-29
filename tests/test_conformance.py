from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import CONFORMANCE

from matterhorn.conformance import run_case
from matterhorn.store import SQLiteStore

CASE_PATHS = sorted(CONFORMANCE.glob("*.yaml"))
BACKENDS = ["sqlite", "postgres"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("case_path", CASE_PATHS, ids=lambda path: path.stem)
def test_conformance(
    case_path: Path,
    backend: str,
    tmp_path: Path,
) -> None:
    if backend == "sqlite":
        store = SQLiteStore(tmp_path / f"{case_path.stem}.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "MATTERHORN_TEST_POSTGRES_DSN is unset; PostgreSQL conformance skipped"
            )
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    try:
        case_scope = _case_scope(case_path)
        store.clear_scope(case_scope)
        result = run_case(case_path, store)
        assert result.passed, result.detail
    finally:
        store.close()


def _case_scope(path: Path) -> str:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["scope_id"]


def test_at_least_forty_language_neutral_cases_are_collected() -> None:
    assert len(CASE_PATHS) >= 40
    for path in CASE_PATHS:
        text = path.read_text(encoding="utf-8")
        assert "import " not in text
        assert "lambda" not in text
