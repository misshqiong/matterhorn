from __future__ import annotations

import json
from datetime import UTC, datetime

from matterhorn.contracts import SchemaProfile
from matterhorn.engine import Engine


class FakeGateway:
    def __init__(self, response: dict, expected_predicate: str = "semantic_value"):
        self.response = response
        self.expected_predicate = expected_predicate
        self.calls = 0

    def complete(self, *, system: str, user: str, response_schema: dict) -> str:
        self.calls += 1
        assert self.expected_predicate in system
        assert "phase" not in system
        assert response_schema["additionalProperties"] is False
        return json.dumps(self.response)


class ExplodingGateway:
    def complete(self, **_kwargs) -> str:
        raise RuntimeError("offline model")


def _profile() -> SchemaProfile:
    return SchemaProfile.model_validate(
        {
            "schema": "dream/v1",
            "subjects": [{"type": "THING", "primary": True}],
            "predicates": [
                {
                    "name": "phase",
                    "subject": "THING",
                    "cardinality": "SINGLE",
                    "extraction": "deterministic",
                    "source_field": "status",
                },
                {
                    "name": "semantic_value",
                    "subject": "THING",
                    "cardinality": "SINGLE",
                    "extraction": "semantic",
                    "semantic_filter": "conservative",
                    "value_domain": ["model", "human"],
                },
            ],
        }
    )


def _card():
    return {
        "card_id": "c1",
        "scope_id": "s",
        "subject_key": "thing-1",
        "date": "2026-01-01",
        "title": "Thing",
        "status": "open",
        "source_refs": [
            {
                "source_id": "m1",
                "sent_at": "2026-01-01T10:00:00Z",
                "sender": "u",
            }
        ],
    }


def _response(value="model"):
    return {
        "candidates": [
            {
                "subject_key": "thing-1",
                "subject_type": "THING",
                "predicate": "semantic_value",
                "operation": "ASSERT",
                "object_value": value,
                "valid_from": "2026-01-01T10:00:00Z",
                "source_ids": ["m1"],
                "confidence": 0.95,
            }
        ]
    }


def test_dream_happy_path_and_second_run_is_noop(tmp_path) -> None:
    gateway = FakeGateway(_response())
    engine = Engine(
        tmp_path / "dream.db",
        _profile(),
        gateway=gateway,
        clock=[
            datetime(2026, 1, 1, 11, tzinfo=UTC),
            datetime(2026, 1, 1, 12, tzinfo=UTC),
        ],
    )
    engine.ingest([_card()])
    first = engine.dream("s")
    second = engine.dream("s")
    assert first.new_assertions == 1
    assert first.new_subjects == 0
    assert first.remaining == 0
    assert second.new_assertions == 0
    assert second.new_subjects == 0
    assert second.processed == 0
    assert gateway.calls == 1
    assert engine.query.current("s", "thing-1", "semantic_value")[0].value == "model"
    assert engine.gate_statistics("s").accepted == 1


def test_dream_creates_child_subject_only_inside_successful_transaction(tmp_path) -> None:
    profile = SchemaProfile.model_validate(
        {
            "schema": "child/v1",
            "subjects": [
                {"type": "PARENT", "primary": True},
                {"type": "CHILD", "parent": "PARENT"},
            ],
            "predicates": [
                {
                    "name": "phase",
                    "subject": "PARENT",
                    "cardinality": "SINGLE",
                    "extraction": "deterministic",
                    "source_field": "status",
                },
                {
                    "name": "related_person",
                    "subject": "CHILD",
                    "cardinality": "SET",
                    "extraction": "semantic",
                    "object": "person",
                },
            ],
        }
    )
    response = {
        "candidates": [
            {
                "subject_type": "CHILD",
                "parent_subject_key": "parent-1",
                "subject_title": "Review slot",
                "predicate": "related_person",
                "operation": "ASSERT",
                "object_value": "p1",
                "valid_from": "2026-01-01T10:00:00Z",
                "source_ids": ["m1"],
                "confidence": 0.95,
            }
        ]
    }
    engine = Engine(
        tmp_path / "child.db",
        profile,
        gateway=FakeGateway(response, expected_predicate="related_person"),
        clock=[
            datetime(2026, 1, 1, 11, tzinfo=UTC),
            datetime(2026, 1, 1, 12, tzinfo=UTC),
        ],
    )
    card = _card() | {"subject_key": "parent-1"}
    engine.ingest([card])
    assert [item.subject_type for item in engine.store.subjects("s")] == ["PARENT"]

    report = engine.dream("s")
    subjects = engine.store.subjects("s")
    child = next(item for item in subjects if item.subject_type == "CHILD")
    assert report.new_subjects == 1
    assert child.parent_subject_key == "parent-1"
    assert engine.query.current("s", child.subject_key, "related_person")[0].value == "p1"
    assert [item.subject_type for item in engine.query.by_person("s", "p1")] == [
        "CHILD"
    ]
    assert [item.subject_type for item in engine.query.list_matters("s")] == [
        "PARENT"
    ]

    before = engine.store.subjects("s")
    second = engine.dream("s")
    assert (second.new_assertions, second.new_subjects) == (0, 0)
    engine.replay("s")
    assert engine.store.subjects("s") == before


def test_gateway_error_keeps_queue_and_deterministic_state(tmp_path) -> None:
    engine = Engine(
        tmp_path / "retry.db",
        _profile(),
        gateway=ExplodingGateway(),
        clock=[datetime(2026, 1, 1, 11, tzinfo=UTC)],
    )
    deterministic = engine.ingest([_card()])
    before = engine.store.assertions("s")
    report = engine.dream("s")
    queued = engine.store.distill_queue("s")
    assert report.failed == 1
    assert report.remaining == 1
    assert queued[0].attempt_count == 1
    assert "offline model" in queued[0].last_error
    assert engine.store.assertions("s") == before
    assert len(deterministic) == 1


def test_human_correction_at_same_instant_outranks_model(tmp_path) -> None:
    gateway = FakeGateway(_response())
    engine = Engine(
        tmp_path / "human.db",
        _profile(),
        gateway=gateway,
        clock=[
            datetime(2026, 1, 1, 11, tzinfo=UTC),
            datetime(2026, 1, 1, 12, tzinfo=UTC),
            datetime(2026, 1, 1, 13, tzinfo=UTC),
        ],
    )
    engine.ingest([_card()])
    engine.dream("s")
    engine.correct(
        {
            "scope_id": "s",
            "subject_key": "thing-1",
            "subject_type": "THING",
            "predicate": "semantic_value",
            "object_value": "human",
            "valid_from": "2026-01-01T10:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-note",
                    "sent_at": "2026-01-01T13:00:00Z",
                    "sender": "human",
                }
            ],
        }
    )
    current = engine.query.current("s", "thing-1", "semantic_value")
    assert [(item.value, item.origin) for item in current] == [("human", "human")]
