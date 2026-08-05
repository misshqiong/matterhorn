from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from matterhorn import Engine
from matterhorn.contracts import EpisodeCard, Origin, TaskResult
from matterhorn.store import SQLiteStore


class EmptySemanticGateway:
    def complete(self, **_kwargs) -> str:
        return json.dumps({"candidates": []})


@pytest.fixture(params=["sqlite", "postgres"])
def engine_factory(request, tmp_path):
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "change-only.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "MATTERHORN_TEST_POSTGRES_DSN is unset; PostgreSQL admission skipped"
            )
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)

    scopes: list[str] = []

    def make(scope_id: str) -> Engine:
        store.clear_scope(scope_id)
        scopes.append(scope_id)
        return Engine(
            store,
            clock=lambda: datetime(2026, 8, 5, 9, tzinfo=UTC),
            gateway=EmptySemanticGateway(),
        )

    yield make

    for scope_id in scopes:
        store.clear_scope(scope_id)
    store.close()


def _card(
    scope_id: str,
    card_id: str,
    day: int,
    source_id: str,
    **updates,
) -> EpisodeCard:
    return EpisodeCard.model_validate(
        {
            "card_id": card_id,
            "scope_id": scope_id,
            "subject_key": "matter",
            "date": f"2026-08-{day:02d}",
            "title": "Change-only matter",
            "source_refs": [
                {
                    "source_id": source_id,
                    "sent_at": f"2026-08-{day:02d}T08:00:00Z",
                    "sender": "Tester",
                }
            ],
            **updates,
        }
    )


def test_single_same_value_drops_without_attaching_evidence(engine_factory) -> None:
    scope_id = "unit-change-only-single"
    engine = engine_factory(scope_id)
    first = _card(scope_id, "first", 1, "source-1", status="open")
    second = _card(scope_id, "second", 2, "source-2", status="open")

    engine._ingest_cards_sync([first])
    event_ids = [event.event_id for event in engine.events(scope_id)]
    emitted = engine._ingest_cards_sync([second])

    assert emitted == []
    assert len(engine.store.assertions(scope_id)) == 1
    assert engine.gate_statistics(scope_id).unchanged_dropped == 1
    assert [event.event_id for event in engine.events(scope_id)] == event_ids
    assert engine.store.subjects(scope_id)[0].source_ids == frozenset({"source-1"})
    assert engine.store.memory_cards(scope_id)[0].source_ids == ["source-1"]


def test_single_real_change_records_after_same_value_drop(engine_factory) -> None:
    scope_id = "unit-change-only-single-change"
    engine = engine_factory(scope_id)

    engine._ingest_cards_sync(
        [
            _card(scope_id, "first", 1, "source-1", status="open"),
            _card(scope_id, "same", 2, "source-2", status="open"),
            _card(scope_id, "changed", 3, "source-3", status="completed"),
        ]
    )

    assert [
        assertion.object_value for assertion in engine.store.assertions(scope_id)
    ] == ["open", "completed"]
    assert [value.value for value in engine.query.current(scope_id, "matter", "status")] == [
        "completed"
    ]
    assert engine.gate_statistics(scope_id).unchanged_dropped == 1


def test_set_live_member_drops_while_new_member_records(engine_factory) -> None:
    scope_id = "unit-change-only-set"
    engine = engine_factory(scope_id)

    engine._ingest_cards_sync(
        [
            _card(
                scope_id,
                "first",
                1,
                "source-1",
                participants=[{"id": "ada"}],
            ),
            _card(
                scope_id,
                "second",
                2,
                "source-2",
                participants=[{"id": "ada"}, {"id": "bob"}],
            ),
        ]
    )

    assertions = engine.store.assertions(scope_id)
    assert [(item.predicate, item.object_value) for item in assertions] == [
        ("participated_by", "ada"),
        ("participated_by", "bob"),
    ]
    assert engine.gate_statistics(scope_id).unchanged_dropped == 1


def test_append_identical_object_key_drops_across_effective_times(
    engine_factory,
) -> None:
    scope_id = "unit-change-only-append"
    engine = engine_factory(scope_id)

    engine._ingest_cards_sync(
        [
            _card(scope_id, "first", 1, "source-1", progress="Built fixture"),
            _card(scope_id, "second", 2, "source-2", progress="Built fixture"),
        ]
    )

    assertions = engine.store.assertions(scope_id)
    assert [(item.predicate, item.object_value) for item in assertions] == [
        ("progress", "Built fixture")
    ]
    assert engine.gate_statistics(scope_id).unchanged_dropped == 1


def test_retract_from_cleared_fields_is_never_dropped(engine_factory) -> None:
    scope_id = "unit-change-only-retract"
    engine = engine_factory(scope_id)

    engine._ingest_cards_sync(
        [
            _card(scope_id, "first", 1, "source-1", next_step="Ship"),
            _card(
                scope_id,
                "clear",
                2,
                "source-2",
                cleared_fields=["next_step"],
            ),
        ]
    )

    assertions = engine.store.assertions(scope_id)
    assert [item.operation.value for item in assertions] == ["ASSERT", "RETRACT"]
    assert engine.query.current(scope_id, "matter", "next_step") == []
    assert engine.gate_statistics(scope_id).unchanged_dropped == 0


def test_human_correction_same_value_is_stored_as_affirmation(engine_factory) -> None:
    scope_id = "unit-change-only-human"
    engine = engine_factory(scope_id)
    engine._ingest_cards_sync(
        [_card(scope_id, "first", 1, "source-1", status="open")]
    )

    engine.correct(
        {
            "scope_id": scope_id,
            "subject_key": "matter",
            "subject_type": "MATTER",
            "predicate": "status",
            "operation": "ASSERT",
            "object_value": "open",
            "valid_from": "2026-08-02T00:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-source",
                    "sent_at": "2026-08-02T08:00:00Z",
                    "sender": "Reviewer",
                }
            ],
        }
    )

    assertions = engine.store.assertions(scope_id)
    assert [item.origin for item in assertions] == [Origin.model, Origin.human]
    assert len(engine.store.intervals(scope_id)[0].supporting_assertion_ids) == 2
    assert engine.gate_statistics(scope_id).unchanged_dropped == 0


def test_task_result_surfaces_unchanged_drop_counter(engine_factory) -> None:
    scope_id = "unit-change-only-task"
    engine = engine_factory(scope_id)

    first = engine.add_cards(
        [_card(scope_id, "first", 1, "source-1", status="open")],
        wait=True,
    )
    second = engine.add_cards(
        [_card(scope_id, "second", 2, "source-2", status="open")],
        wait=True,
    )

    assert isinstance(first, TaskResult)
    assert isinstance(second, TaskResult)
    assert first.unchanged_dropped == first.gate.unchanged_dropped == 0
    assert second.new_assertions == 0
    assert second.unchanged_dropped == second.gate.unchanged_dropped == 1
    assert engine.gate_statistics(scope_id).unchanged_dropped == 1
