from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from matterhorn import Engine
from matterhorn.engine.canonical import canonical_json
from matterhorn.errors import ImportRefusedError
from matterhorn.scheduler import ServiceScheduler, parse_daily_flush_at
from matterhorn.webhooks import WebhookDispatcher


class EmptySemanticGateway:
    def complete(self, **kwargs) -> str:
        payload = json.loads(kwargs["user"])
        if "records" in payload:
            source_id = payload["records"][0]["record_id"]
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-07-29",
                            "title": "Daily message",
                            "status": "open",
                            "source_ids": [source_id],
                        }
                    ]
                }
            )
        return json.dumps({"candidates": []})


def _card(
    *,
    card_id: str = "c1",
    status: str = "blocked",
    source_id: str = "m1",
) -> dict:
    return {
        "card_id": card_id,
        "scope_id": "team",
        "subject_key": "release",
        "date": "2026-07-29",
        "title": "Release",
        "status": status,
        "participants": [{"id": "u1", "role": "owner"}],
        "source_refs": [
            {
                "source_id": source_id,
                "sent_at": "2026-07-29T08:00:00Z",
                "sender": "u1",
                "uri": f"https://evidence.test/{source_id}",
            }
        ],
    }


def _projection_snapshot(engine: Engine, scope_id: str) -> str:
    return canonical_json(
        {
            "intervals": [
                item.model_dump(mode="json")
                for item in engine.store.intervals(scope_id)
            ],
            "memory_cards": [
                item.model_dump(mode="json")
                for item in engine.store.memory_cards(scope_id)
            ],
            "projection_stats": [
                item.model_dump(mode="json")
                for item in engine.store.projection_stats(scope_id)
            ],
            "matters": [item.to_dict() for item in engine.matters(scope_id)],
            "current": [
                item.to_dict()
                for item in engine.query.current(scope_id, "release", "status")
            ],
            "timeline": [
                item.to_dict()
                for item in engine.query.timeline(scope_id, "release", "status")
            ],
            "at": [
                item.to_dict()
                for item in engine.query.at(
                    scope_id,
                    "release",
                    "status",
                    datetime(2026, 7, 29, tzinfo=UTC),
                )
            ],
            "by_person": [
                item.to_dict()
                for item in engine.query.by_person(scope_id, "u1")
            ],
            "completion": engine.query.completion(scope_id),
        }
    )


def test_projection_diff_events_are_traceable_and_replay_safe(tmp_path) -> None:
    engine = Engine(
        tmp_path / "events.db",
        clock=iter(
            [
                datetime(2026, 7, 29, 8, tzinfo=UTC),
                datetime(2026, 7, 29, 9, tzinfo=UTC),
            ]
        ),
    )
    engine._ingest_cards_sync([_card()])
    first = engine.events("team")
    assert {item.event_type.value for item in first} == {
        "matter_created",
        "status_changed",
    }

    engine.correct(
        {
            "scope_id": "team",
            "subject_key": "release",
            "subject_type": "MATTER",
            "predicate": "status",
            "object_value": "open",
            "valid_from": "2026-07-29T00:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-note",
                    "sent_at": "2026-07-29T08:55:00Z",
                    "sender": "human",
                }
            ],
        }
    )
    events = engine.events("team")
    correction = next(
        item for item in events if item.event_type.value == "value_corrected"
    )
    assert correction.old_value == "blocked"
    assert correction.new_value == "open"
    assert correction.origin.value == "human"
    assert correction.source_ids == ["human-note"]

    before_ids = [item.event_id for item in events]
    engine._ingest_cards_sync([_card()])
    assert [item.event_id for item in engine.events("team")] == before_ids
    replay = engine.replay("team")
    assert replay.events_emitted == 0
    assert [item.event_id for item in engine.events("team")] == before_ids


def test_blocking_and_semantic_projection_events_are_emitted(tmp_path) -> None:
    class SemanticGateway:
        def complete(self, **_kwargs) -> str:
            return json.dumps(
                {
                    "candidates": [
                        {
                            "subject_type": "DECISION_SLOT",
                            "parent_subject_key": "release",
                            "subject_title": "Ship decision",
                            "predicate": "decision_adopted",
                            "operation": "ASSERT",
                            "object_value": True,
                            "valid_from": "2026-07-29T00:00:00Z",
                            "source_ids": ["m1"],
                            "confidence": 0.95,
                        }
                    ]
                }
            )

    engine = Engine(
        tmp_path / "event-types.db",
        gateway=SemanticGateway(),
        clock=iter(
            [
                datetime(2026, 7, 29, 8, tzinfo=UTC),
                datetime(2026, 7, 29, 9, tzinfo=UTC),
                datetime(2026, 7, 29, 10, tzinfo=UTC),
            ]
        ),
    )
    blocked_card = _card()
    blocked_card["blocker"] = "vendor"
    engine._ingest_cards_sync([blocked_card])
    assert "blocked" in {
        item.event_type.value for item in engine.events("team")
    }
    engine.correct(
        {
            "scope_id": "team",
            "subject_key": "release",
            "subject_type": "MATTER",
            "predicate": "blocked_by",
            "operation": "RETRACT",
            "valid_from": "2026-07-29T01:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-unblock",
                    "sent_at": "2026-07-29T09:55:00Z",
                    "sender": "human",
                }
            ],
        }
    )
    assert "unblocked" in {
        item.event_type.value for item in engine.events("team")
    }
    report = engine.dream("team")
    assert report.accepted_candidates == 1
    assert "decision_adopted" in {
        item.event_type.value for item in engine.events("team")
    }


def test_export_import_round_trip_preserves_projection_queries_and_origin(
    tmp_path,
) -> None:
    source = Engine(
        tmp_path / "source.db",
        clock=iter(
            [
                datetime(2026, 7, 29, 8, tzinfo=UTC),
                datetime(2026, 7, 29, 9, tzinfo=UTC),
            ]
        ),
    )
    source._ingest_cards_sync([_card()])
    source.correct(
        {
            "scope_id": "team",
            "subject_key": "release",
            "subject_type": "MATTER",
            "predicate": "status",
            "object_value": "done",
            "valid_from": "2026-07-29T00:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-note",
                    "sent_at": "2026-07-29T08:55:00Z",
                    "sender": "human",
                }
            ],
        }
    )
    expected = _projection_snapshot(source, "team")
    envelope = source.export("team")

    target = Engine(tmp_path / "target.db")
    report = target.import_snapshot(
        json.loads(envelope.model_dump_json())
    )
    assert report.assertions == len(envelope.assertions)
    assert _projection_snapshot(target, "team") == expected
    assert any(
        item.origin.value == "human"
        for item in target.store.assertions("team")
    )
    assert target.replay("team").events_emitted == 0
    assert _projection_snapshot(target, "team") == expected


def test_import_refuses_unavailable_profile_and_nonempty_scope(tmp_path) -> None:
    source = Engine(tmp_path / "source.db")
    source._ingest_cards_sync([_card()])
    envelope = source.export("team")

    wrong_profile = Engine(
        tmp_path / "wrong.db", schema="personal-decisions/v1"
    )
    with pytest.raises(ImportRefusedError, match="unavailable local schema"):
        wrong_profile.import_snapshot(envelope)

    nonempty = Engine(tmp_path / "nonempty.db")
    nonempty._ingest_cards_sync([_card(card_id="other")])
    with pytest.raises(ImportRefusedError, match="MUST be empty"):
        nonempty.import_snapshot(envelope)


def test_daily_flush_scheduler_uses_injected_utc_clock_without_sleep(
    tmp_path,
) -> None:
    engine = Engine(
        tmp_path / "daily.db",
        gateway=EmptySemanticGateway(),
        clock=lambda: datetime(2026, 7, 29, 1, tzinfo=UTC),
    )
    receipt = engine.add(
        "team",
        [
            {
                "id": "daily-message",
                "sender": {"id": "u1"},
                "text": "Daily message is open.",
                "sent_at": "2026-07-29T01:30:00Z",
            }
        ],
    )
    instants = iter(
        [
            datetime(2026, 7, 29, 1, 59, tzinfo=UTC),
            datetime(2026, 7, 29, 2, 0, tzinfo=UTC),
            datetime(2026, 7, 29, 3, 0, tzinfo=UTC),
        ]
    )
    scheduler = ServiceScheduler(
        engine,
        daily_flush_at="02:00",
        clock=lambda: next(instants),
    )
    assert scheduler.tick() == []
    assert engine.task(receipt.task_id).status.value == "pending"
    assert [item.scope_id for item in scheduler.tick()] == ["team"]
    assert engine.task(receipt.task_id).status.value == "completed"
    assert scheduler.tick() == []
    assert parse_daily_flush_at("23:59").hour == 23
    with pytest.raises(ValueError, match="UTC HH:MM"):
        parse_daily_flush_at("2:00")


def test_webhook_retries_to_in_process_asgi_receiver_and_dedupes(
    tmp_path,
) -> None:
    async def scenario() -> None:
        engine = Engine(tmp_path / "webhook.db")
        engine._ingest_cards_sync([_card(status="done")])
        receiver = FastAPI()
        calls = 0
        received: list[dict] = []

        @receiver.post("/events")
        async def receive(payload: dict):
            nonlocal calls
            calls += 1
            if calls == 1:
                return JSONResponse(status_code=503, content={"retry": True})
            received.append(payload)
            return {"ok": True}

        async def no_sleep(_seconds: float) -> None:
            return None

        dispatcher = WebhookDispatcher(
            engine.store,
            "http://receiver.test/events",
            transport=httpx.ASGITransport(app=receiver),
            max_attempts=3,
            backoff_seconds=0,
            sleep=no_sleep,
        )
        delivered = await dispatcher.deliver_pending()
        assert delivered == 3
        assert calls == 2
        assert {item["event_type"] for item in received[0]["events"]} == {
            "matter_created",
            "status_changed",
            "matter_completed",
        }
        assert await dispatcher.deliver_pending() == 0
        assert calls == 2

    import asyncio

    asyncio.run(scenario())
