from __future__ import annotations

import json
from pathlib import Path

import pytest

from matterhorn import Engine
from matterhorn.adapters.github import map_devlog, map_git_log
from matterhorn.engine.canonical import canonical_json
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
        owner="octo-org",
        repo="sample-repo",
    )
    directory = tmp_path / "devlog"
    directory.mkdir()
    path = directory / "0001-project-strategy.md"
    path.write_text("# Strategy\n\nEvidence-backed.\n", encoding="utf-8")
    devlogs = map_devlog(
        [(path, "2026-07-29T12:00:00Z")],
        owner="octo-org",
        repo="sample-repo",
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
                "https://github.com/octo-org/sample-repo/"
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


def test_export_rebuild_uses_source_checkpoint_without_an_llm_call(
    tmp_path: Path,
) -> None:
    records = _records(tmp_path)
    source_db = tmp_path / "source.db"
    rebuilt_db = tmp_path / "rebuilt.db"
    fill_records(
        db_path=source_db,
        records=records,
        gateway_selection=_gateway(),
    )
    source = Engine(source_db)
    envelope = source.export("dev")
    source.store.close()

    rebuilt = Engine(rebuilt_db)
    rebuilt.import_snapshot(envelope)
    rebuilt.store.close()

    class NoCallGateway:
        def complete(self, **_kwargs):
            raise AssertionError("durable source checkpoint called the LLM")

    result = fill_records(
        db_path=rebuilt_db,
        records=records,
        gateway_selection=GatewaySelection(NoCallGateway(), "no-call"),
    )
    after = Engine(rebuilt_db)
    rebuilt_envelope = after.export("dev")
    after.store.close()

    assert result.add_records["records_processed"] == 0
    assert result.add_records["records_skipped"] == len(records)
    assert canonical_json(rebuilt_envelope.model_dump(mode="json")) == canonical_json(
        envelope.model_dump(mode="json")
    )


def test_ledger_bookkeeping_commits_are_excluded() -> None:
    import scripts.ledger_fill as lf
    from matterhorn.contracts import Record

    def rec(author_name: str, author_id: str, content: str) -> Record:
        return Record.model_validate(
            {
                "record_id": "github:o/r:commit:" + "a" * 40,
                "container_id": "github:o/r",
                "sent_at": "2026-07-29T00:00:00Z",
                "author": {"id": author_id, "display_name": author_name, "kind": "human"},
                "content": content,
                "kind": "commit",
                "native_id": "commit:" + "a" * 40,
            }
        )

    assert lf._is_ledger_bookkeeping(
        rec("github-actions[bot]", "bot-1", "anything")
    )
    assert lf._is_ledger_bookkeeping(
        rec("aurelvana", "u1", "ledger: nightly update\n\nauto")
    )
    assert not lf._is_ledger_bookkeeping(
        rec("aurelvana", "u1", "M7: real feature work")
    )
