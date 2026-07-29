from __future__ import annotations

import json
from pathlib import Path

import pytest

from matterhorn.adapters.github import map_devlog, map_git_log
from matterhorn.gateway_config import configured_gateway
from scripts.ledger_fill import (
    GatewaySelection,
    fill_records,
    select_gateway,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _records(tmp_path: Path):
    git_fixture = json.loads(
        (FIXTURES / "git-log.json").read_text(encoding="utf-8")
    )
    commits = map_git_log(
        git_fixture["output"],
        owner="misshqiong",
        repo="matterhorn",
    )
    directory = tmp_path / "devlog"
    directory.mkdir()
    path = directory / "0001-project-strategy.md"
    path.write_text("# Strategy\n\nEvidence-backed.\n", encoding="utf-8")
    devlogs = map_devlog(
        [(path, "2026-07-29T12:00:00Z")],
        owner="misshqiong",
        repo="matterhorn",
    )
    return [*commits, *devlogs]


def _gateway() -> GatewaySelection:
    path = FIXTURES / "ledger-gateway.json"
    return GatewaySelection(
        configured_gateway(provider="fixture", fixture_path=path),
        f"fixture ({path.resolve()})",
    )


def test_no_credential_fails_before_database_creation(tmp_path: Path) -> None:
    db = tmp_path / "ledger" / "dev.db"
    with pytest.raises(ValueError, match="no usable LLM credential"):
        select_gateway(environ={})
    assert not db.exists()


def test_fixture_fill_is_incremental_without_network(tmp_path: Path) -> None:
    records = _records(tmp_path)
    db = tmp_path / "ledger" / "dev.db"

    first = fill_records(
        db_path=db,
        records=records,
        gateway_selection=_gateway(),
        source_counts={"total": len(records)},
    )
    second = fill_records(
        db_path=db,
        records=records,
        gateway_selection=_gateway(),
        source_counts={"total": len(records)},
    )

    assert first.add_records["records_processed"] == len(records)
    assert first.add_records["records_skipped"] == 0
    assert first.add_records["cards_accepted"] == 1
    assert first.queued_before_flush == 1
    assert first.flush["remaining"] == 0
    assert first.matters[0]["title"] == "Matterhorn project strategy"
    source_refs = first.evidence[0]["timeline"][0]["source_refs"]
    assert source_refs == [
        {
            "source_id": "devlog:0001",
            "uri": (
                "https://github.com/misshqiong/matterhorn/"
                "blob/main/devlog/0001-project-strategy.md"
            ),
            "status": "active",
            "revoked_at": None,
        }
    ]

    assert second.add_records["records_processed"] == 0
    assert second.add_records["records_skipped"] == len(records)
    assert second.add_records["cards_accepted"] == 0
    assert second.add_records["assertions_emitted"] == 0
    assert second.queued_before_flush == 0
    assert second.flush == {
        "scope_id": "dev",
        "tasks_processed": 0,
        "task_ids": [],
        "remaining": 0,
    }
    assert second.matters == first.matters
    assert second.evidence == first.evidence
