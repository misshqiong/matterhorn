from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from matterhorn import Engine
from matterhorn.contracts import Message, TaskResult, TaskStatus
from matterhorn.store import SQLiteStore


class FacadeGateway:
    def __init__(self):
        self.calls = 0

    def complete(self, **kwargs) -> str:
        self.calls += 1
        if kwargs["response_schema"].get("$id") == (
            "matterhorn-identity-adjudication/v1"
        ):
            return json.dumps(
                {
                    "decision": "new",
                    "subject_key": None,
                    "confidence": 1.0,
                    "evidence_source_ids": [],
                }
            )
        payload = json.loads(kwargs["user"])
        if "records" not in payload:
            return json.dumps({"candidates": []})
        cards = []
        for item in payload["records"]:
            record = item["record"]
            conversation = record["container_id"].split(":")[-1]
            cards.append(
                {
                    "date": "2026-07-28",
                    "title": f"Matter {conversation}",
                    "status": "open",
                    "participants": [
                        {"id": record["author"]["id"], "role": "owner"}
                    ],
                    "source_ids": [item["source_alias"]],
                }
            )
        return json.dumps({"cards": cards})


class ExplodingGateway:
    def complete(self, **_kwargs):
        raise AssertionError("add() called the gateway")


@pytest.fixture(params=["sqlite", "postgres"])
def retry_store(request, tmp_path):
    scope_id = f"fictional-retry-{uuid4().hex}"
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "retry.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "MATTERHORN_TEST_POSTGRES_DSN is unset; PostgreSQL retry tests skipped"
            )
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    try:
        yield store, scope_id
    finally:
        store.clear_scope(scope_id)
        store.close()


def _message(**updates):
    payload = {
        "id": "m1",
        "sender": {"id": "u1", "name": "Ada"},
        "text": "I own the launch.",
        "sent_at": "2026-07-28T14:00:00+08:00",
        "conversation_id": "launch",
    }
    payload.update(updates)
    return payload


def test_message_contract_is_closed_and_minimal() -> None:
    assert Message.model_validate(_message()).sender.name == "Ada"
    assert Message.model_validate(
        _message(sender={"id": "u1"})
    ).sender.name is None
    with pytest.raises(ValidationError):
        Message.model_validate(_message(platform="slack"))
    with pytest.raises(ValidationError):
        Message.model_validate({key: value for key, value in _message().items() if key != "text"})


def test_add_is_llm_free_and_task_survives_restart(tmp_path) -> None:
    path = tmp_path / "restart.db"
    clock = lambda: datetime(2026, 7, 29, 8, tzinfo=UTC)
    first = Engine(path, llm=ExplodingGateway(), clock=clock)
    receipt = first.add("team", [_message()])
    assert receipt.accepted == 1
    assert first.task(receipt.task_id).status == TaskStatus.pending
    first.store.close()

    gateway = FacadeGateway()
    second = Engine(path, llm=gateway, clock=clock)
    assert second.task(receipt.task_id).status == TaskStatus.pending
    second.flush("team")
    assert second.task(receipt.task_id) == TaskResult.model_validate(
        {
            "task_id": receipt.task_id,
            "status": "completed",
            "cards_produced": 1,
            "new_assertions": 3,
            "gate": {
                "accepted": 1,
                "rejected": {},
                "route_new": 1,
            },
        }
    )
    second.store.close()

    third = Engine(path, clock=clock)
    assert third.task(receipt.task_id).status == TaskStatus.completed


def test_add_stages_validated_records_when_task_is_enqueued(tmp_path) -> None:
    engine = Engine(
        tmp_path / "enqueue-staging.db",
        llm=ExplodingGateway(),
        clock=lambda: datetime(2026, 8, 4, 9, 1, tzinfo=UTC),
    )

    receipt = engine.add(
        "fictional-team",
        [
            _message(
                sent_at="2026-08-04T09:00:00Z",
                text="Fictional raw content staged before extraction.",
            )
        ],
    )
    staged = engine.store.staged_records(
        "fictional-team",
        "fictional-team:launch",
        sent_at_from=datetime(2026, 8, 3, tzinfo=UTC),
        sent_at_before=datetime(2026, 8, 5, tzinfo=UTC),
        thread_id=None,
        exclude_record_ids=[],
    )

    assert engine.task(receipt.task_id).status == TaskStatus.pending
    assert [(record.record_id, record.content) for record in staged] == [
        (
            "fictional-team:launch:m1",
            "Fictional raw content staged before extraction.",
        )
    ]


def test_flush_opportunistically_purges_expired_staging(tmp_path) -> None:
    class EmptyExtractor:
        def extract(self, **_kwargs):
            return SimpleNamespace(cards=[], rejection_counts={})

    now = datetime(2026, 8, 9, 9, tzinfo=UTC)
    engine = Engine(
        tmp_path / "flush-purge.db",
        extractor=EmptyExtractor(),
        clock=lambda: now,
    )
    engine.add(
        "fictional-team",
        [_message(sent_at="2026-08-01T09:00:00Z")],
    )

    engine.flush("fictional-team")

    assert engine.store.staged_records(
        "fictional-team",
        "fictional-team:launch",
        sent_at_from=datetime(2026, 8, 1, tzinfo=UTC),
        sent_at_before=datetime(2026, 8, 10, tzinfo=UTC),
        thread_id=None,
        exclude_record_ids=[],
    ) == []


@pytest.mark.parametrize("retention", [0, -1, float("inf"), float("nan"), True])
def test_staging_retention_must_be_positive_and_finite(
    tmp_path, retention
) -> None:
    with pytest.raises((TypeError, ValueError), match="positive finite"):
        Engine(tmp_path / "invalid-retention.db", staging_retention_days=retention)


def test_wait_returns_completed_result_inline(tmp_path) -> None:
    result = Engine(
        tmp_path / "wait.db",
        llm=FacadeGateway(),
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    ).add("team", [_message()], wait=True)
    assert isinstance(result, TaskResult)
    assert result.status == TaskStatus.completed
    assert result.cards_produced == 1
    assert result.task_id.startswith("task_")


def test_wait_cards_returns_completed_result_with_task_id(tmp_path) -> None:
    result = Engine(
        tmp_path / "wait-cards.db",
        llm=FacadeGateway(),
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    ).add_cards(
        [
            {
                "card_id": "c1",
                "scope_id": "team",
                "date": "2026-07-28",
                "title": "Launch",
                "status": "open",
                "source_refs": [
                    {
                        "source_id": "team:m1",
                        "sent_at": "2026-07-28T14:00:00Z",
                        "sender": "u1",
                    }
                ],
            }
        ],
        wait=True,
    )
    assert isinstance(result, TaskResult)
    assert result.status == TaskStatus.completed
    assert result.task_id.startswith("task_")


def test_failed_extractor_task_retries_and_clears_last_error(retry_store) -> None:
    class FailOnceExtractor:
        def __init__(self):
            self.calls = 0

        def extract(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError(
                    "Authorization: Bearer fictional-secret-value " + "x" * 600
                )
            return SimpleNamespace(cards=[], rejection_counts={})

    store, scope_id = retry_store
    extractor = FailOnceExtractor()
    engine = Engine(
        store,
        extractor=extractor,
        clock=lambda: datetime(2026, 8, 4, 9, tzinfo=UTC),
    )
    receipt = engine.add(scope_id, [_message()])

    first = engine.flush(scope_id)
    failed = engine.task(receipt.task_id)

    assert first.tasks_processed == 1
    assert first.remaining == 1
    assert failed.status == TaskStatus.failed
    assert failed.attempts == 1
    assert failed.last_error is not None
    assert len(failed.last_error) <= 500
    assert "fictional-secret-value" not in failed.last_error

    second = engine.flush(scope_id)
    completed = engine.task(receipt.task_id)

    assert second.tasks_processed == 1
    assert second.remaining == 0
    assert extractor.calls == 2
    assert completed.status == TaskStatus.completed
    assert completed.attempts == 1
    assert completed.last_error is None


def test_failed_task_stops_retrying_at_attempt_cap(retry_store) -> None:
    class AlwaysFailExtractor:
        def __init__(self):
            self.calls = 0

        def extract(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("fictional provider unavailable")

    store, scope_id = retry_store
    extractor = AlwaysFailExtractor()
    engine = Engine(
        store,
        extractor=extractor,
        clock=lambda: datetime(2026, 8, 4, 9, tzinfo=UTC),
    )
    receipt = engine.add(scope_id, [_message()])

    for expected_attempts in range(1, 6):
        report = engine.flush(scope_id)
        assert report.tasks_processed == 1
        assert engine.task(receipt.task_id).attempts == expected_attempts

    exhausted = engine.task(receipt.task_id)
    stopped = engine.flush(scope_id)

    assert exhausted.status == TaskStatus.failed
    assert exhausted.attempts == 5
    assert exhausted.last_error == "RuntimeError: fictional provider unavailable"
    assert stopped.tasks_processed == 0
    assert stopped.remaining == 0
    assert extractor.calls == 5


def test_dream_failure_retries_the_enclosing_task(retry_store) -> None:
    class FailOnceGateway:
        def __init__(self):
            self.calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("fictional semantic provider unavailable")
            return json.dumps({"candidates": []})

    store, scope_id = retry_store
    gateway = FailOnceGateway()
    engine = Engine(
        store,
        gateway=gateway,
        clock=lambda: datetime(2026, 8, 4, 9, tzinfo=UTC),
    )
    receipt = engine.add_cards(
        [
            {
                "card_id": "fictional-dream-card",
                "scope_id": scope_id,
                "date": "2026-08-04",
                "title": "Fictional retry exercise",
                "status": "open",
                "source_refs": [
                    {
                        "source_id": "fictional-source-1",
                        "sent_at": "2026-08-04T08:00:00Z",
                        "sender": "Ada Example",
                    }
                ],
            }
        ]
    )

    engine.flush(scope_id)
    failed = engine.task(receipt.task_id)
    assert failed.status == TaskStatus.failed
    assert failed.attempts == 1
    assert failed.last_error is not None

    engine.flush(scope_id)
    completed = engine.task(receipt.task_id)
    assert gateway.calls == 2
    assert completed.status == TaskStatus.completed
    assert completed.attempts == 1
    assert completed.last_error is None


def test_ingest_is_a_deprecated_alias_for_add_cards(tmp_path) -> None:
    engine = Engine(
        tmp_path / "alias.db",
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    )
    with pytest.warns(DeprecationWarning):
        receipt = engine.ingest(
            [
                {
                    "card_id": "c1",
                    "scope_id": "team",
                    "date": "2026-07-28",
                    "title": "Launch",
                    "source_refs": [
                        {
                            "source_id": "team:m1",
                            "sent_at": "2026-07-28T14:00:00Z",
                            "sender": "u1",
                        }
                    ],
                }
            ]
        )
    assert receipt.accepted == 1
    assert engine.task(receipt.task_id).status == TaskStatus.pending


def test_colliding_ids_across_conversations_never_share_evidence(tmp_path) -> None:
    gateway = FacadeGateway()
    engine = Engine(
        tmp_path / "collision.db",
        llm=gateway,
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    )
    receipt = engine.add(
        "team",
        [
            _message(conversation_id="alpha"),
            _message(
                conversation_id="beta",
                sender={"id": "u2", "name": "Bob"},
            ),
        ],
    )
    assert gateway.calls == 0
    engine.flush("team")

    assert {matter.title for matter in engine.matters("team")} == {
        "Matter alpha",
        "Matter beta",
    }
    evidence = [subject.source_ids for subject in engine.store.subjects("team")]
    assert evidence == [
        frozenset({"team:beta:m1"}),
        frozenset({"team:alpha:m1"}),
    ] or evidence == [
        frozenset({"team:alpha:m1"}),
        frozenset({"team:beta:m1"}),
    ]
    assert evidence[0].isdisjoint(evidence[1])
    assert engine.task(receipt.task_id).new_assertions == 6


def test_repeated_batch_has_a_new_receipt_and_zero_new_assertions(tmp_path) -> None:
    gateway = FacadeGateway()
    engine = Engine(
        tmp_path / "repeat.db",
        llm=gateway,
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    )
    first = engine.add("team", [_message()])
    engine.flush("team")
    second = engine.add("team", [_message()])
    engine.flush("team")
    assert first.task_id != second.task_id
    assert engine.task(first.task_id).new_assertions == 3
    assert engine.task(second.task_id).new_assertions == 0
    assert engine.task(second.task_id).cards_produced == 0


def test_quiet_period_flushes_only_old_pending_message_scopes(tmp_path) -> None:
    engine = Engine(
        tmp_path / "quiet.db",
        llm=FacadeGateway(),
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    )
    receipt = engine.add("team", [_message()])
    reports = engine.flush_quiet(10)
    assert [report.scope_id for report in reports] == ["team"]
    assert engine.task(receipt.task_id).status == TaskStatus.completed


def test_future_stamped_message_cannot_freeze_scope_quiet_period(tmp_path) -> None:
    now = datetime(2026, 7, 29, 8, tzinfo=UTC)
    engine = Engine(
        tmp_path / "skew.db",
        llm=FacadeGateway(),
        clock=lambda: now,
    )
    skewed = dict(_message())
    skewed["sent_at"] = "2026-07-29T09:30:00Z"  # source clock 90 min ahead
    receipt = engine.add("team", [skewed])
    # Quiet computation must treat the message as arrived at enqueue time,
    # so the scope flushes after the quiet period instead of at 09:30.
    reports = engine.flush_quiet_at(10, now + timedelta(minutes=11))
    assert [report.scope_id for report in reports] == ["team"]
    assert engine.task(receipt.task_id).status == TaskStatus.completed
