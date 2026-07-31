from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from matterhorn.api import create_app
from matterhorn.canonical import canonical_json
from matterhorn.contracts import SourceRef
from matterhorn.engine import Engine

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _source(source_id: str) -> SourceRef:
    return SourceRef(
        source_id=source_id,
        sent_at=NOW,
        sender="operator",
        excerpt=f"Reason for {source_id}",
    )


def _card(
    card_id: str,
    subject_key: str,
    title: str,
    status: str,
    *,
    occurred_at: str,
):
    return {
        "card_id": card_id,
        "scope_id": "team",
        "date": "2026-08-01",
        "title": title,
        "status": status,
        "progress": f"{title} progress",
        "occurred_at": occurred_at,
        "source_refs": [
            {
                "source_id": f"evidence-{card_id}",
                "sent_at": occurred_at,
                "sender": "ada",
            }
        ],
        "subject_key": subject_key,
    }


def _engine(tmp_path) -> Engine:
    engine = Engine(tmp_path / "merge.db", clock=lambda: NOW)
    engine._ingest_cards_sync(
        [
            _card(
                "target-card",
                "target",
                "Canonical release",
                "open",
                occurred_at="2026-08-01T09:00:00Z",
            ),
            _card(
                "source-card",
                "source",
                "Release verification",
                "blocked",
                occurred_at="2026-08-01T10:00:00Z",
            ),
        ]
    )
    return engine


def test_merge_projects_onto_target_with_aliases_and_unioned_evidence(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    event = engine.merge_subjects(
        "team",
        "source",
        "target",
        source_refs=[_source("merge-reason")],
        valid_from=NOW,
    )

    assert event.event_type.value == "subject_merged"
    assert event.source_ids == ["merge-reason"]
    matters = engine.matters("team")
    assert [matter.subject_key for matter in matters] == ["target"]
    assert matters[0].title == "Canonical release"
    assert matters[0].aliases == ["Release verification"]
    assert matters[0].status == "blocked"
    timeline = engine.query.timeline("team", "target", "status")
    assert [item.value for item in timeline] == ["open", "blocked"]
    assert {
        source_id
        for item in timeline
        for source_id in item.source_ids
    } == {"evidence-target-card", "evidence-source-card"}
    assert {item.subject_key for item in engine.store.assertions("team")} == {
        "source",
        "target",
    }
    assert engine.replay("team").events_emitted == 0


def test_card_and_correction_to_merged_source_redirect_to_target(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.merge_subjects(
        "team",
        "source",
        "target",
        source_refs=[_source("merge-reason")],
        valid_from=NOW,
    )
    engine._ingest_cards_sync(
        [
            _card(
                "follow-up",
                "source",
                "Release verification follow-up",
                "in_progress",
                occurred_at="2026-08-01T11:00:00Z",
            )
        ]
    )
    correction = engine.correct(
        {
            "scope_id": "team",
            "subject_key": "source",
            "subject_type": "MATTER",
            "predicate": "status",
            "operation": "ASSERT",
            "object_value": "done",
            "valid_from": "2026-08-01T12:00:00Z",
            "source_refs": [_source("human-correction").model_dump(mode="json")],
        }
    )

    assert correction.subject_key == "target"
    assert engine.query.current("team", "target", "status")[0].value == "done"


def test_unmerge_restores_both_subjects_and_cycle_is_rejected(tmp_path) -> None:
    engine = _engine(tmp_path)
    before = canonical_json(
        [
            item.model_dump(mode="json")
            for item in engine.store.intervals("team")
        ]
    )
    engine.merge_subjects(
        "team",
        "source",
        "target",
        source_refs=[_source("merge-reason")],
        valid_from=NOW,
    )
    with pytest.raises(ValueError, match="already merged"):
        engine.merge_subjects(
            "team",
            "source",
            "target",
            source_refs=[_source("duplicate")],
            valid_from=NOW,
        )
    event = engine.unmerge_subjects(
        "team",
        "source",
        source_refs=[_source("unmerge-reason")],
        valid_from=NOW,
    )

    assert event.event_type.value == "subject_unmerged"
    assert engine.store.subject_merges("team") == []
    assert {matter.subject_key for matter in engine.matters("team")} == {
        "source",
        "target",
    }
    assert canonical_json(
        [
            item.model_dump(mode="json")
            for item in engine.store.intervals("team")
        ]
    ) == before

    engine.merge_subjects(
        "team",
        "source",
        "target",
        source_refs=[_source("source-target")],
        valid_from=NOW,
    )
    with pytest.raises(ValueError, match="cycle"):
        engine.merge_subjects(
            "team",
            "target",
            "source",
            source_refs=[_source("target-source")],
            valid_from=NOW,
        )


def test_export_import_and_replay_preserve_active_merges(tmp_path) -> None:
    source = _engine(tmp_path)
    source.merge_subjects(
        "team",
        "source",
        "target",
        source_refs=[_source("merge-reason")],
        valid_from=NOW,
    )
    before = canonical_json(source.export("team").model_dump(mode="json"))
    source.replay("team")
    assert canonical_json(source.export("team").model_dump(mode="json")) == before

    target = Engine(tmp_path / "imported.db", clock=lambda: NOW)
    target.import_snapshot(source.export("team"))
    assert target.store.subject_merges("team") == source.store.subject_merges(
        "team"
    )
    assert [matter.to_dict() for matter in target.matters("team")] == [
        matter.to_dict() for matter in source.matters("team")
    ]


def test_rest_merge_and_unmerge_are_resource_style_and_provenanced(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=engine))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            payload = {
                "source_subject_key": "source",
                "target_subject_key": "target",
                "source_refs": [_source("rest-merge").model_dump(mode="json")],
            }
            merged = await client.post("/v1/scopes/team/merges", json=payload)
            assert merged.status_code == 200
            assert merged.json()["event_type"] == "subject_merged"

            matters = (
                await client.get("/v1/scopes/team/matters")
            ).json()
            assert matters[0]["aliases"] == ["Release verification"]
            detail = (
                await client.get("/v1/scopes/team/matters/source")
            ).json()
            assert detail["subject_key"] == "target"
            assert detail["aliases"] == ["Release verification"]

            conflict = await client.post("/v1/scopes/team/merges", json=payload)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "SUBJECT_MERGE_CONFLICT"
            invalid = await client.post(
                "/v1/scopes/team/merges",
                json={**payload, "source_refs": []},
            )
            assert invalid.status_code == 422

            unmerged = await client.post(
                "/v1/scopes/team/merges/source/unmerge",
                json={
                    "source_refs": [
                        _source("rest-unmerge").model_dump(mode="json")
                    ]
                },
            )
            assert unmerged.status_code == 200
            assert unmerged.json()["event_type"] == "subject_unmerged"
            events = await client.get("/v1/scopes/team/events")
            assert {"subject_merged", "subject_unmerged"} <= {
                event["event_type"] for event in events.json()
            }

    asyncio.run(scenario())
