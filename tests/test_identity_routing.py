from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from matterhorn import Engine
from matterhorn.api import create_app
from matterhorn.contracts import EpisodeCard
from matterhorn.engine.routing import (
    ADJUDICATION_SCHEMA_ID,
    PINNED_ADJUDICATION_EXAMPLE,
    AdjudicationCandidate,
    build_adjudication_prompt,
    candidate_score,
    gate_adjudication,
)

NOW = datetime(2026, 8, 4, 16, tzinfo=UTC)


def _source(source_id: str, excerpt: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "sent_at": "2026-08-04T15:55:00Z",
        "sender": "Dana Reyes",
        "excerpt": excerpt,
    }


def _card() -> EpisodeCard:
    return EpisodeCard.model_validate(
        {
            "card_id": "routing-card",
            "scope_id": "routing-scope",
            "date": "2026-08-04",
            "title": "Atlas launch verification",
            "progress": "The checklist passed.",
            "source_refs": [
                _source(
                    "routing-scope:updates:r1",
                    "Atlas launch checklist passed verification.",
                )
            ],
        }
    )


def _candidate() -> AdjudicationCandidate:
    return AdjudicationCandidate(
        subject_key="atlas-matter",
        title="Fictional Atlas launch",
        aliases=["Atlas alpha"],
        handles=["issue:OCT-9501"],
        status="open",
        next_step="Verify the launch checklist.",
        participants=["dana"],
        recent_evidence="Atlas launch awaits checklist verification.",
    )


def test_adjudication_prompt_is_rich_closed_and_pins_literal_example() -> None:
    prompt = build_adjudication_prompt(_card(), [_candidate()])
    payload = json.loads(prompt.user)

    assert PINNED_ADJUDICATION_EXAMPLE in prompt.system
    assert prompt.response_schema["$id"] == ADJUDICATION_SCHEMA_ID
    assert prompt.response_schema["additionalProperties"] is False
    assert payload["card"]["sources"] == [
        {
            "source_id": "routing-scope:updates:r1",
            "excerpt": "Atlas launch checklist passed verification.",
        }
    ]
    assert payload["candidates"] == [_candidate().model_dump(mode="json")]


def test_candidate_score_uses_handle_values_without_payload_type_labels() -> None:
    card = _card().model_copy(update={"title": "Issue report", "progress": None})
    candidate = AdjudicationCandidate(
        subject_key="beacon-matter",
        title="Beacon migration",
        handles=["issue:OCT-9501"],
    )

    assert candidate_score(card, candidate) == 0


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ("not-json", "MALFORMED_RESPONSE"),
        (
            {
                "decision": "attach",
                "subject_key": "atlas-matter",
                "confidence": "0.99",
                "evidence_source_ids": ["routing-scope:updates:r1"],
            },
            "MALFORMED_RESPONSE",
        ),
        (
            {
                "decision": "attach",
                "subject_key": "atlas-matter",
                "confidence": 0.59,
                "evidence_source_ids": ["routing-scope:updates:r1"],
            },
            "LOW_CONFIDENCE",
        ),
        (
            {
                "decision": "attach",
                "subject_key": "unoffered-matter",
                "confidence": 0.99,
                "evidence_source_ids": ["routing-scope:updates:r1"],
            },
            "SUBJECT_NOT_OFFERED",
        ),
        (
            {
                "decision": "attach",
                "subject_key": "atlas-matter",
                "confidence": 0.99,
                "evidence_source_ids": ["invented-source"],
            },
            "SOURCE_NOT_TRACEABLE",
        ),
        (
            {
                "decision": "new",
                "subject_key": None,
                "confidence": 0.1,
                "evidence_source_ids": ["invented-source"],
            },
            "SOURCE_NOT_TRACEABLE",
        ),
    ],
)
def test_adjudication_gate_converts_invalid_attach_to_review(
    response: str | dict[str, object],
    reason: str,
) -> None:
    raw = response if isinstance(response, str) else json.dumps(response)

    gated = gate_adjudication(
        raw,
        card=_card(),
        candidates=[_candidate()],
        confidence_threshold=0.6,
    )

    assert gated.outcome == "review"
    assert gated.subject_key is None
    assert gated.reasons == (reason,)


class InspectingGateway:
    def __init__(self) -> None:
        self.engine: Engine | None = None
        self.adjudication_payload: dict[str, object] | None = None

    def complete(self, *, system: str, user: str, response_schema: dict) -> str:
        if "cards" in response_schema.get("properties", {}):
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-08-04",
                            "title": "Atlas launch verification",
                            "progress": "The checklist passed.",
                            "source_ids": ["routing-scope:updates:r1"],
                        }
                    ]
                }
            )
        assert response_schema["$id"] == ADJUDICATION_SCHEMA_ID
        assert self.engine is not None
        assert self.engine.store._transaction_depth == 0
        assert PINNED_ADJUDICATION_EXAMPLE in system
        self.adjudication_payload = json.loads(user)
        return json.dumps(
            {
                "decision": "attach",
                "subject_key": "atlas-matter",
                "confidence": 0.86,
                "evidence_source_ids": ["routing-scope:updates:r1"],
            }
        )


def test_adjudication_call_runs_without_store_transaction_and_gets_rich_recall(
    tmp_path,
) -> None:
    gateway = InspectingGateway()
    engine = Engine(tmp_path / "adjudication.db", gateway=gateway, clock=lambda: NOW)
    gateway.engine = engine
    engine._ingest_cards_sync(
        [
            {
                "card_id": "atlas-seed",
                "scope_id": "routing-scope",
                "subject_key": "atlas-matter",
                "date": "2026-08-04",
                "title": "Fictional Atlas launch",
                "status": "open",
                "next_step": "Verify the launch checklist.",
                "participants": [
                    {"id": "dana", "display_name": "Dana Reyes", "role": "owner"}
                ],
                "source_refs": [
                    _source(
                        "atlas-seed-source",
                        "Atlas launch awaits checklist verification.",
                    )
                ],
            }
        ]
    )
    engine.bind_handle(
        "routing-scope",
        "atlas-matter",
        "issue",
        "OCT-9501",
        source_refs=[_source("atlas-handle-source", "OCT-9501 tracks Atlas.")],
    )

    report = engine.add_records(
        [
            {
                "record_id": "routing-scope:updates:r1",
                "container_id": "routing-scope:updates",
                "sent_at": "2026-08-04T15:58:00Z",
                "author": {
                    "id": "dana",
                    "display_name": "Dana Reyes",
                    "kind": "human",
                },
                "content": "Atlas launch checklist passed verification.",
            }
        ],
        scope_id="routing-scope",
    )

    assert report.route_model == 1
    assert report.assertions_emitted == 1
    assert gateway.adjudication_payload is not None
    candidate = gateway.adjudication_payload["candidates"][0]
    assert candidate["subject_key"] == "atlas-matter"
    assert candidate["handles"] == ["issue:OCT-9501"]
    assert candidate["status"] == "open"
    assert candidate["next_step"] == "Verify the launch checklist."
    assert candidate["participants"] == ["dana"]
    assert candidate["recent_evidence"]


class AbstainingGateway:
    def complete(self, *, response_schema: dict, **_kwargs) -> str:
        if "cards" in response_schema.get("properties", {}):
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-08-04",
                            "title": "Atlas follow-up",
                            "progress": "The held review progress.",
                            "source_ids": ["review-scope:updates:r1"],
                        }
                    ]
                }
            )
        if response_schema.get("$id") != ADJUDICATION_SCHEMA_ID:
            return json.dumps({"candidates": []})
        return json.dumps(
            {
                "decision": "abstain",
                "subject_key": None,
                "confidence": 0.5,
                "evidence_source_ids": ["review-scope:updates:r1"],
            }
        )


def test_review_route_is_persisted_in_enclosing_task_gate(tmp_path) -> None:
    engine = Engine(
        tmp_path / "review-task.db",
        gateway=AbstainingGateway(),
        clock=lambda: NOW,
    )
    engine._ingest_cards_sync(
        [
            {
                "card_id": "review-task-seed",
                "scope_id": "review-scope",
                "subject_key": "atlas-matter",
                "date": "2026-08-04",
                "title": "Fictional Atlas matter",
                "status": "open",
                "source_refs": [
                    _source("review-task-seed-source", "Atlas remains open.")
                ],
            }
        ]
    )
    receipt = engine.add(
        "review-scope",
        [
            {
                "id": "r1",
                "sender": {"id": "dana", "name": "Dana Reyes"},
                "text": "The Atlas follow-up needs identity review.",
                "sent_at": "2026-08-04T15:58:00Z",
                "conversation_id": "updates",
            }
        ],
    )

    engine.flush("review-scope")
    task = engine.task(receipt.task_id)

    assert task.cards_produced == 1
    assert task.gate.route_review == 1
    assert task.gate.route_new == 0
    assert engine.gate_statistics("review-scope").route_review == 1


def test_review_survives_restart_and_rest_resolution_is_human_and_once(tmp_path) -> None:
    path = tmp_path / "reviews.db"
    first = Engine(path, gateway=AbstainingGateway(), clock=lambda: NOW)
    first._ingest_cards_sync(
        [
            {
                "card_id": "review-seed",
                "scope_id": "review-scope",
                "subject_key": "atlas-matter",
                "date": "2026-08-04",
                "title": "Fictional Atlas matter",
                "status": "open",
                "source_refs": [
                    _source("review-seed-source", "Atlas remains open.")
                ],
            }
        ]
    )
    report = first.add_records(
        [
            {
                "record_id": "review-scope:updates:r1",
                "container_id": "review-scope:updates",
                "sent_at": "2026-08-04T15:58:00Z",
                "author": {
                    "id": "dana",
                    "display_name": "Dana Reyes",
                    "kind": "human",
                },
                "content": "OCT-9601 is the Atlas follow-up.",
            }
        ],
        scope_id="review-scope",
    )
    assert report.route_review == 1
    review_id = first.review_items("review-scope")[0].review_id
    first.store.close()

    second = Engine(path, clock=lambda: NOW)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=second))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            pending = await client.get("/v1/scopes/review-scope/reviews")
            assert pending.status_code == 200
            assert [item["review_id"] for item in pending.json()] == [review_id]
            payload = {
                "action": "attach",
                "subject_key": "atlas-matter",
                "source_refs": [
                    _source(
                        "review:operator",
                        "A human attached the held follow-up to Atlas.",
                    )
                ],
            }
            resolved = await client.post(
                f"/v1/scopes/review-scope/reviews/{review_id}/resolve",
                json=payload,
            )
            assert resolved.status_code == 200
            assert resolved.json()["resolution_json"]["action"] == "attach"
            assert (await client.get("/v1/scopes/review-scope/reviews")).json() == []
            repeated = await client.post(
                f"/v1/scopes/review-scope/reviews/{review_id}/resolve",
                json=payload,
            )
            assert repeated.status_code == 409
            assert repeated.json()["error"]["code"] == "REVIEW_CONFLICT"

    asyncio.run(scenario())

    progress = [
        assertion
        for assertion in second.store.assertions("review-scope")
        if assertion.predicate == "progress"
    ]
    assert len(progress) == 1
    assert progress[0].origin.value == "human"
    assert [ref.source_id for ref in progress[0].source_refs] == [
        "review-scope:updates:r1",
        "review:operator",
    ]
    assert [
        item.normalized_value
        for item in second.subject_handles("review-scope", "atlas-matter")
    ] == ["oct-9601"]
    resolved_rows = second.store.review_items("review-scope", pending_only=False)
    assert len(resolved_rows) == 1
    assert resolved_rows[0].resolved_at == NOW
