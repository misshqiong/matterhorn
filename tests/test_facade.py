from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from matterhorn import Engine
from matterhorn.contracts import Message, TaskResult, TaskStatus


class FacadeGateway:
    def __init__(self):
        self.calls = 0

    def complete(self, **kwargs) -> str:
        self.calls += 1
        payload = json.loads(kwargs["user"])
        if "records" not in payload:
            return json.dumps({"candidates": []})
        cards = []
        for record in payload["records"]:
            conversation = record["container_id"].split(":")[-1]
            cards.append(
                {
                    "date": "2026-07-28",
                    "title": f"Matter {conversation}",
                    "status": "open",
                    "participants": [
                        {"id": record["author"]["id"], "role": "owner"}
                    ],
                    "source_ids": [record["record_id"]],
                }
            )
        return json.dumps({"cards": cards})


class ExplodingGateway:
    def complete(self, **_kwargs):
        raise AssertionError("add() called the gateway")


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
            "status": "completed",
            "cards_produced": 1,
            "new_assertions": 3,
            "gate": {"accepted": 1, "rejected": {}},
        }
    )
    second.store.close()

    third = Engine(path, clock=clock)
    assert third.task(receipt.task_id).status == TaskStatus.completed


def test_wait_returns_completed_result_inline(tmp_path) -> None:
    result = Engine(
        tmp_path / "wait.db",
        llm=FacadeGateway(),
        clock=lambda: datetime(2026, 7, 29, 8, tzinfo=UTC),
    ).add("team", [_message()], wait=True)
    assert isinstance(result, TaskResult)
    assert result.status == TaskStatus.completed
    assert result.cards_produced == 1


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
