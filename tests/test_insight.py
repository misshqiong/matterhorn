from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from matterhorn.api import create_app
from matterhorn.canonical import normalize_title, object_key
from matterhorn.contracts import Record, SourceRef, SubjectRecord
from matterhorn.defaults import Engine
from matterhorn.engine.unified_loop import UnifiedLoopSession

VIEWPOINT = {
    "who": "dana-reyes",
    "stance": "supports",
    "content": "Keep the fictional rubric small.",
    "where": "octo-room",
}


def _record(record_id: str = "octo-room:r1") -> Record:
    return Record.model_validate(
        {
            "record_id": record_id,
            "container_id": record_id.rsplit(":", 1)[0],
            "sent_at": "2026-08-06T09:00:00Z",
            "author": {
                "id": "dana-reyes",
                "display_name": "Dana Reyes",
                "kind": "human",
            },
            "content": "A fictional viewpoint about the octo-org rubric.",
            "kind": "im",
        }
    )


def _source(source_id: str, minute: int = 0) -> SourceRef:
    return SourceRef(
        source_id=source_id,
        sent_at=datetime(2026, 8, 6, 9, minute, tzinfo=UTC),
        sender="Dana Reyes",
    )


def _topic(engine: Engine, key: str, title: str | None = None) -> None:
    selected_title = title or key.replace("-", " ").title()
    engine.store.upsert_subject(
        SubjectRecord(
            scope_id="scope",
            subject_key=key,
            subject_type="TOPIC",
            title=selected_title,
            normalized_title=normalize_title(selected_title),
            source_ids=frozenset(),
        )
    )


def _correction(
    subject_key: str,
    predicate: str,
    object_value: Any,
    *,
    subject_type: str,
    operation: str = "ASSERT",
    minute: int = 10,
) -> dict[str, Any]:
    return {
        "scope_id": "scope",
        "subject_key": subject_key,
        "subject_type": subject_type,
        "predicate": predicate,
        "operation": operation,
        "object_value": object_value,
        "valid_from": datetime(2026, 8, 6, 9, minute, tzinfo=UTC),
        "source_refs": [
            _source(f"correction:{subject_key}:{predicate}:{minute}", minute)
        ],
    }


def test_schema_registers_topic_without_lifecycle_predicates() -> None:
    engine = Engine(":memory:")
    topic_predicates = {
        predicate.name
        for predicate in engine.profile.predicates
        if predicate.subject == "TOPIC"
    }

    assert topic_predicates == {"viewpoint", "stated_by"}
    assert {"status", "blocked_by", "next_step", "due_at"}.isdisjoint(
        topic_predicates
    )
    viewpoint = engine.profile.predicate("viewpoint")
    assert viewpoint.retractable is True
    assert viewpoint.field_domains == {
        "stance": ["supports", "opposes", "neutral", "informs"]
    }
    assert engine.profile.predicate("outcome").retractable is False
    assert engine.profile.predicate("decision").retractable is False


def test_unified_loop_creates_topic_and_existing_lifecycle_gate_rejects_peer(
    tmp_path,
) -> None:
    engine = Engine(
        tmp_path / "topic-loop.db",
        clock=lambda: datetime(2026, 8, 6, 10, tzinfo=UTC),
    )
    session = UnifiedLoopSession(
        engine=engine,
        scope_id="scope",
        records=[_record()],
        context=[],
    )

    result = session.handle_tool(
        "emit",
        {
            "assertions": [
                {
                    "subject": {
                        "new_subject": {
                            "ref": "rubric",
                            "subject_type": "TOPIC",
                            "title": "Fictional review rubric",
                        }
                    },
                    "predicate": "viewpoint",
                    "operation": "ASSERT",
                    "object_value": VIEWPOINT,
                    "evidence_aliases": ["m1"],
                },
                {
                    "subject": {
                        "new_subject": {
                            "ref": "rubric",
                            "subject_type": "TOPIC",
                            "title": "Fictional review rubric",
                        }
                    },
                    "predicate": "status",
                    "operation": "ASSERT",
                    "object_value": "open",
                    "evidence_aliases": ["m1"],
                },
            ]
        },
    )

    assert len(result["accepted"]) == 1
    assert result["rejected"] == [
        {"index": 1, "reason": "SUBJECT_TYPE_MISMATCH"}
    ]
    subjects = engine.store.subjects("scope")
    assert len(subjects) == 1 and subjects[0].subject_type == "TOPIC"
    assert [item.predicate for item in engine.store.assertions("scope")] == [
        "viewpoint"
    ]


def test_field_domains_reject_bad_viewpoint_stance(tmp_path) -> None:
    engine = Engine(tmp_path / "bad-stance.db")
    _topic(engine, "topic-existing")
    bad = {**VIEWPOINT, "stance": "maybe"}
    with pytest.raises(
        ValueError, match="^correction value is outside the predicate domain$"
    ):
        engine.correct(
            _correction(
                "topic-existing",
                "viewpoint",
                bad,
                subject_type="TOPIC",
            )
        )
    session = UnifiedLoopSession(
        engine=engine,
        scope_id="scope",
        records=[_record()],
        context=[],
    )

    result = session.handle_tool(
        "emit",
        {
            "assertions": [
                {
                    "subject": {
                        "new_subject": {
                            "ref": "rubric",
                            "subject_type": "TOPIC",
                            "title": "Fictional review rubric",
                        }
                    },
                    "predicate": "viewpoint",
                    "operation": "ASSERT",
                    "object_value": bad,
                    "evidence_aliases": ["m1"],
                }
            ]
        },
    )

    assert result["accepted"] == []
    assert result["rejected"] == [
        {"index": 0, "reason": "VALUE_OUT_OF_DOMAIN"}
    ]
    assert [item.subject_key for item in engine.store.subjects("scope")] == [
        "topic-existing"
    ]
    assert engine.store.assertions("scope") == []


def test_viewpoint_retracts_but_outcome_and_decision_still_never_retract(
    tmp_path,
) -> None:
    clock_values = iter(
        [
            datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
            datetime(2026, 8, 6, 10, 1, tzinfo=UTC),
            datetime(2026, 8, 6, 10, 2, tzinfo=UTC),
            datetime(2026, 8, 6, 10, 3, tzinfo=UTC),
            datetime(2026, 8, 6, 10, 4, tzinfo=UTC),
        ]
    )
    engine = Engine(tmp_path / "retract.db", clock=lambda: next(clock_values))
    _topic(engine, "topic-rubric", "Fictional review rubric")
    engine.correct(
        _correction("topic-rubric", "viewpoint", VIEWPOINT, subject_type="TOPIC")
    )
    retained = {**VIEWPOINT, "who": "ellis-stone", "stance": "informs"}
    engine.correct(
        _correction(
            "topic-rubric", "viewpoint", retained, subject_type="TOPIC", minute=11
        )
    )
    retraction = _correction(
            "topic-rubric",
            "viewpoint",
            VIEWPOINT,
            subject_type="TOPIC",
            operation="RETRACT",
            minute=12,
    )
    retraction["object_key"] = object_key(VIEWPOINT)
    engine.correct(retraction)

    assert [
        item.value
        for item in engine.query.timeline("scope", "topic-rubric", "viewpoint")
    ] == [retained]
    assert [item.operation.value for item in engine.store.assertions("scope")] == [
        "ASSERT",
        "ASSERT",
        "RETRACT",
    ]

    engine._ingest_cards_sync(
        [
            {
                "card_id": "matter-seed",
                "scope_id": "scope",
                "subject_key": "matter-root",
                "date": "2026-08-06",
                "title": "Fictional rollout",
                "status": "open",
                "outcome": {"type": "result", "content": "Passed."},
                "source_refs": [
                    _source("matter:seed").model_dump(mode="json")
                ],
            }
        ]
    )
    engine.correct(
        _correction(
            "matter-root",
            "decision",
            "Proceed with the fictional rollout.",
            subject_type="MATTER",
            minute=12,
        )
    )
    for predicate, value in (
        ("outcome", {"type": "result", "content": "Passed."}),
        ("decision", "Proceed with the fictional rollout."),
    ):
        with pytest.raises(
            ValueError, match="^APPEND predicates cannot be retracted$"
        ):
            engine.correct(
                _correction(
                    "matter-root",
                    predicate,
                    value,
                    subject_type="MATTER",
                    operation="RETRACT",
                    minute=13,
                )
            )


def test_worth_knowing_window_and_subject_key_tie_break_are_deterministic(
    tmp_path,
) -> None:
    clock_values = iter(
        [
            datetime(2026, 8, 6, 8, 30, tzinfo=UTC),
            datetime(2026, 8, 6, 9, 10, tzinfo=UTC),
            datetime(2026, 8, 6, 9, 20, tzinfo=UTC),
        ]
    )
    engine = Engine(tmp_path / "worth.db", clock=lambda: next(clock_values))
    for key in ("topic-old", "topic-b", "topic-a"):
        _topic(engine, key)
    for index, key in enumerate(("topic-old", "topic-b", "topic-a")):
        engine.correct(
            _correction(
                key,
                "viewpoint",
                {**VIEWPOINT, "who": f"speaker-{index}"},
                subject_type="TOPIC",
                minute=15,
            )
        )

    brief = engine.brief(
        datetime(2026, 8, 6, 9, tzinfo=UTC),
        datetime(2026, 8, 6, 10, tzinfo=UTC),
        scope_ids=["scope"],
    )

    assert [item["subject_key"] for item in brief["worth_knowing"]] == [
        "topic-a",
        "topic-b",
    ]
    assert brief["worth_knowing"][0]["viewpoint_count"] == 1
    assert brief["worth_knowing"][0]["distinct_speakers"] == ["speaker-2"]
    assert brief["worth_knowing"][0]["newest_viewpoint_at"] == datetime(
        2026, 8, 6, 9, 15, tzinfo=UTC
    )


def test_rest_brief_topic_detail_and_console_worth_knowing(tmp_path) -> None:
    async def scenario() -> None:
        engine = Engine(
            tmp_path / "rest-insight.db",
            clock=lambda: datetime(2026, 8, 6, 9, 30, tzinfo=UTC),
        )
        _topic(engine, "topic-rest", "Fictional rollout safety")
        engine.correct(
            _correction(
                "topic-rest",
                "viewpoint",
                VIEWPOINT,
                subject_type="TOPIC",
                minute=15,
            )
        )
        app = create_app(engine=engine, console_enabled=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            brief = await client.get(
                "/v1/console/brief",
                params={
                    "window_start": "2026-08-06T09:00:00Z",
                    "window_end": "2026-08-06T10:00:00Z",
                },
            )
            assert brief.status_code == 200
            assert brief.json()["worth_knowing"] == [
                {
                    "scope_id": "scope",
                    "subject_key": "topic-rest",
                    "title": "Fictional rollout safety",
                    "viewpoint_count": 1,
                    "distinct_speakers": ["dana-reyes"],
                    "newest_viewpoint_at": "2026-08-06T09:15:00Z",
                }
            ]
            detail = await client.get(
                "/v1/scopes/scope/matters/topic-rest"
            )
            assert detail.status_code == 200
            assert detail.json()["subject_type"] == "TOPIC"
            assert list(detail.json()["timeline"]) == ["viewpoint"]
            page = await client.get("/console")
            # 2026-08-06 user direction: briefing surface (incl. worth-knowing)
            # left the first screen; topic detail rendering stays.
            assert "值得知道" not in page.text
            assert 'id="brief-worth-knowing"' not in page.text
            assert 'detail.subject_type === "TOPIC"' in page.text
            assert '["viewpoint"]' in page.text

    asyncio.run(scenario())
