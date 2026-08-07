from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from matterhorn.canonical import canonical_json
from matterhorn.contracts import EpisodeCard, ReviewItem
from matterhorn.defaults import Engine
from matterhorn.distill import ToolLoopResult
from matterhorn.engine.aggregate import (
    AggregateContext,
    AggregateOperator,
    AliasNamespace,
    PreparedAggregation,
    RejectionCounter,
)
from matterhorn.store import SQLiteStore

NOW = datetime(2026, 8, 7, 10, tzinfo=UTC)


class _InjectedStrategy:
    def __init__(self, *, decision: str) -> None:
        self.decision = decision
        self.calls: list[str] = []

    def recall(self, unit: Any) -> tuple[tuple[str, ...], ...]:
        self.calls.append("recall")
        return (tuple(sorted(unit)),)

    def candidate_keys(
        self,
        _unit: Any,
        recall: tuple[str, ...],
    ) -> tuple[str, ...]:
        self.calls.append("candidate_keys")
        return recall

    def aliases(
        self,
        _unit: Any,
        recall: tuple[str, ...],
    ) -> AliasNamespace:
        self.calls.append("aliases")
        return AliasNamespace.sequential(recall, prefix="k")

    def adjudicate(
        self,
        context: AggregateContext,
        _rejections: RejectionCounter,
    ) -> str:
        self.calls.append("adjudicate")
        assert context.aliases.resolve("k1") == "matter-a"
        return self.decision

    def gate(
        self,
        context: AggregateContext,
        decision: Any,
        rejections: RejectionCounter,
    ) -> str:
        self.calls.append("gate")
        if not context.world.contains(decision):
            rejections.increment("CLOSED_WORLD_VIOLATION")
            return "review"
        return "attach"

    def commit(
        self,
        prepared: PreparedAggregation,
        _operator: AggregateOperator,
    ) -> dict[str, Any]:
        self.calls.append("commit")
        return {
            "keys": prepared.context.world.keys,
            "outcome": prepared.outcome,
            "rejections": prepared.rejection_counts,
        }


def test_operator_injects_strategy_and_shares_closed_candidate_world(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "operator.db")
    strategy = _InjectedStrategy(decision="invented-parent")
    try:
        result = AggregateOperator(store).run(
            ["matter-b", "matter-a"],
            strategy,
        )
    finally:
        store.close()

    assert result == (
        {
            "keys": ("matter-a", "matter-b"),
            "outcome": "review",
            "rejections": {"CLOSED_WORLD_VIOLATION": 1},
        },
    )
    assert strategy.calls == [
        "recall",
        "candidate_keys",
        "aliases",
        "adjudicate",
        "gate",
        "commit",
    ]


def test_operator_shared_review_path_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "review.db")
    operator = AggregateOperator(store)
    card = EpisodeCard(
        card_id="fictional-review-card",
        scope_id="aggregate-review",
        date=NOW.date(),
        title="Fictional aggregation review",
        source_refs=[
            {
                "source_id": "aggregate-review-source",
                "sent_at": NOW,
                "sender": "Dana Reyes",
                "excerpt": "Fictional review evidence.",
            }
        ],
    )
    item = ReviewItem(
        scope_id=card.scope_id,
        review_id="review_aggregate_shared",
        card_json=card.model_dump(mode="json"),
        reasons=["CLOSED_WORLD_VIOLATION"],
        created_at=NOW,
    )
    try:
        with store.transaction():
            first = operator.enqueue_reviews((item,))
            second = operator.enqueue_reviews((item,))
        stored = store.review_items(card.scope_id)
    finally:
        store.close()

    assert (first, second) == (1, 0)
    assert [review.review_id for review in stored] == [item.review_id]


@dataclass
class _GatewayCounts:
    extraction: int = 0
    adjudication: int = 0
    semantic: int = 0
    tool_loop: int = 0


class _CountingGateway:
    def __init__(self) -> None:
        self.counts = _GatewayCounts()

    def complete(self, *, response_schema: dict[str, Any], **_kwargs: Any) -> str:
        properties = response_schema.get("properties", {})
        if "cards" in properties:
            self.counts.extraction += 1
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-08-07",
                            "title": "Atlas launch verification",
                            "progress": "The fictional checklist passed.",
                            "source_ids": ["aggregate-routing:updates:r1"],
                        }
                    ]
                }
            )
        if response_schema.get("$id") == "matterhorn-identity-adjudication/v1":
            self.counts.adjudication += 1
            return json.dumps(
                {
                    "decision": "attach",
                    "subject_key": "atlas-matter",
                    "confidence": 0.86,
                    "evidence_source_ids": ["aggregate-routing:updates:r1"],
                }
            )
        self.counts.semantic += 1
        return json.dumps({"candidates": []})

    def tool_loop(
        self,
        *,
        handler,
        **_kwargs: Any,
    ) -> ToolLoopResult:
        self.counts.tool_loop += 1
        handler(
            "emit",
            {
                "title": "Octo release theme",
                "member_subject_keys": ["a", "b", "c"],
                "existing_root_subsumes": None,
            },
        )
        return ToolLoopResult("done", tool_calls=1, emissions=1)


def _source(source_id: str, excerpt: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "sent_at": "2026-08-07T09:00:00Z",
        "sender": "Dana Reyes",
        "excerpt": excerpt,
    }


def _exercise_both_layers(root, gateway: _CountingGateway) -> str:
    root.mkdir(parents=True)
    routing = Engine(root / "routing.db", gateway=gateway, clock=lambda: NOW)
    routing._ingest_cards_sync(
        [
            {
                "card_id": "atlas-seed",
                "scope_id": "aggregate-routing",
                "subject_key": "atlas-matter",
                "date": "2026-08-07",
                "title": "Fictional Atlas launch",
                "status": "open",
                "source_refs": [
                    _source(
                        "atlas-source",
                        "Atlas launch awaits checklist verification.",
                    )
                ],
            }
        ]
    )
    route_report = routing.add_records(
        [
            {
                "record_id": "aggregate-routing:updates:r1",
                "container_id": "aggregate-routing:updates",
                "sent_at": "2026-08-07T09:30:00Z",
                "author": {
                    "id": "dana",
                    "display_name": "Dana Reyes",
                    "kind": "human",
                },
                "content": "Atlas launch checklist passed verification.",
            }
        ],
        scope_id="aggregate-routing",
    )

    themes = Engine(
        root / "themes.db",
        gateway=gateway,
        clock=lambda: NOW,
        theme_converge="review",
    )
    themes._ingest_cards_sync(
        [
            {
                "card_id": f"theme-{key}",
                "scope_id": "aggregate-themes",
                "subject_key": key,
                "date": "2026-08-07",
                "title": f"Octo release {key}",
                "status": "open",
                "source_refs": [
                    _source(
                        f"theme-source-{key}",
                        f"Fictional Octo evidence {key}.",
                    )
                ],
            }
            for key in ("a", "b", "c")
        ]
    )
    theme_report = themes.themes("aggregate-themes")
    payload = canonical_json(
        {
            "routing": route_report.model_dump(mode="json"),
            "routing_export": routing.export("aggregate-routing").model_dump(
                mode="json"
            ),
            "themes": theme_report.to_dict(),
            "theme_export": themes.export("aggregate-themes").model_dump(
                mode="json"
            ),
        }
    )
    routing.store.close()
    themes.store.close()
    return payload


def test_gateway_invocation_count_matches_pre_operator_contract(tmp_path) -> None:
    gateway = _CountingGateway()

    _exercise_both_layers(tmp_path / "parity", gateway)

    legacy_counts = _GatewayCounts(
        extraction=1,
        adjudication=1,
        semantic=0,
        tool_loop=1,
    )
    assert gateway.counts == legacy_counts


def test_aggregate_outputs_are_byte_identical_across_fresh_runs(tmp_path) -> None:
    first_gateway = _CountingGateway()
    second_gateway = _CountingGateway()

    first = _exercise_both_layers(tmp_path / "first", first_gateway)
    second = _exercise_both_layers(tmp_path / "second", second_gateway)

    assert first == second
    assert first_gateway.counts == second_gateway.counts
