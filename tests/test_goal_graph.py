from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from matterhorn.api import create_app
from matterhorn.canonical import canonical_json
from matterhorn.cli.app import app
from matterhorn.contracts import EpisodeCard, Record, SourceRef
from matterhorn.defaults import Engine

NOW = datetime(2026, 8, 5, 9, tzinfo=UTC)
SCOPE = "octo-goals"


def _source(source_id: str, *, minute: int = 0) -> dict[str, object]:
    return {
        "source_id": source_id,
        "sent_at": NOW + timedelta(minutes=minute),
        "sender": "Dana Reyes",
        "excerpt": f"Fictional evidence for {source_id}.",
    }


def _card(
    subject_key: str,
    *,
    minute: int,
    status: str = "open",
    blocker: str | None = None,
) -> dict[str, object]:
    return {
        "card_id": f"card-{subject_key}",
        "scope_id": SCOPE,
        "subject_key": subject_key,
        "date": "2026-08-05",
        "occurred_at": NOW + timedelta(minutes=minute),
        "title": f"Fictional {subject_key}",
        "status": status,
        "blocker": blocker,
        "source_refs": [_source(f"octo-room:{subject_key}", minute=minute)],
    }


def _correction(
    subject_key: str,
    predicate: str,
    value: object,
    *,
    minute: int,
    scope_id: str = SCOPE,
) -> dict[str, object]:
    return {
        "scope_id": scope_id,
        "subject_key": subject_key,
        "subject_type": "MATTER",
        "predicate": predicate,
        "object_value": value,
        "valid_from": NOW + timedelta(minutes=minute),
        "source_refs": [
            _source(f"review:{subject_key}:{predicate}:{minute}", minute=minute)
        ],
    }


def _tree_engine(tmp_path) -> Engine:
    engine = Engine(tmp_path / "goal-tree.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [
            _card("root", minute=0),
            _card("child", minute=1, status="completed"),
            _card("grandchild", minute=2, status="blocked", blocker="permit"),
            _card("sibling", minute=3),
        ]
    )
    engine.correct(_correction("child", "part_of", "root", minute=10))
    engine.correct(_correction("grandchild", "part_of", "child", minute=11))
    engine.correct(_correction("sibling", "part_of", "root", minute=12))
    engine.correct(
        _correction("grandchild", "spawned_from", "child", minute=8)
    )
    engine.correct(
        _correction("root", "decision", "Adopt the fictional plan.", minute=13)
    )
    return engine


def test_structure_gates_count_unknown_cross_scope_self_and_cycle(tmp_path) -> None:
    engine = Engine(tmp_path / "goal-gates.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [_card("root", minute=0), _card("child", minute=1)]
    )
    engine._ingest_cards_sync(
        [
            {
                **_card("outside", minute=2),
                "scope_id": "octo-other",
                "source_refs": [_source("octo-other:outside", minute=2)],
            }
        ],
        scope_id="octo-other",
    )

    with pytest.raises(ValueError, match="STRUCTURE_UNKNOWN_TARGET"):
        engine.correct(_correction("child", "part_of", "missing", minute=3))
    with pytest.raises(ValueError, match="STRUCTURE_CROSS_SCOPE"):
        engine.correct(_correction("child", "part_of", "outside", minute=4))
    with pytest.raises(ValueError, match="STRUCTURE_SELF_REFERENCE"):
        engine.correct(_correction("child", "part_of", "child", minute=5))
    engine.correct(_correction("child", "part_of", "root", minute=6))
    with pytest.raises(ValueError, match="STRUCTURE_CYCLE"):
        engine.correct(_correction("root", "spawned_from", "child", minute=7))

    assert engine.gate_statistics(SCOPE).rejections == {
        "STRUCTURE_CROSS_SCOPE": 1,
        "STRUCTURE_CYCLE": 1,
        "STRUCTURE_SELF_REFERENCE": 1,
        "STRUCTURE_UNKNOWN_TARGET": 1,
    }


def test_cycle_gate_canonicalizes_a_merge_chain(tmp_path) -> None:
    engine = Engine(tmp_path / "merge-cycle.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [
            _card("root", minute=0),
            _card("child", minute=1),
            _card("alias", minute=2),
        ]
    )
    engine.correct(_correction("root", "part_of", "child", minute=3))
    engine.merge_subjects(
        SCOPE,
        "alias",
        "root",
        source_refs=[SourceRef.model_validate(_source("review:merge", minute=4))],
        valid_from=NOW + timedelta(minutes=4),
    )

    with pytest.raises(ValueError, match="STRUCTURE_CYCLE"):
        engine.correct(_correction("child", "part_of", "alias", minute=5))


def test_graph_birth_order_reparent_rollup_and_replay_are_deterministic(tmp_path) -> None:
    engine = _tree_engine(tmp_path)
    graph = engine.matter_graph(SCOPE, "grandchild")

    assert [item.subject_key for item in graph.parent_chain] == ["child", "root"]
    assert graph.node.birth_instant == NOW + timedelta(minutes=8)
    root_graph = engine.matter_graph(SCOPE, "root")
    assert [item.subject_key for item in root_graph.children] == ["child", "sibling"]
    assert root_graph.children[0].birth_instant == NOW + timedelta(minutes=1)
    assert root_graph.rollup.descendants_total == 3
    assert root_graph.rollup.descendants_completed == 1
    assert root_graph.rollup.descendants_blocked == 1
    assert root_graph.rollup.bubbled_blockers == [
        {"subject_key": "grandchild", "blocker": ["permit"]}
    ]
    assert root_graph.node.decisions == ["Adopt the fictional plan."]

    engine.correct(_correction("sibling", "part_of", "child", minute=14))
    assert engine.matter_graph(SCOPE, "sibling").parent_chain[0].subject_key == "child"
    assert [item.value for item in engine.query.timeline(SCOPE, "sibling", "part_of")] == [
        "root",
        "child",
    ]
    before = canonical_json(engine.matter_graph(SCOPE, "root").to_dict())
    engine.replay(SCOPE)
    assert canonical_json(engine.matter_graph(SCOPE, "root").to_dict()) == before


def test_wall_and_brief_filter_children_but_keep_all_matter_counts(tmp_path) -> None:
    engine = _tree_engine(tmp_path)
    matters = engine.matters(SCOPE)
    assert [item.subject_key for item in matters] == ["root"]
    assert matters[0].descendants_total == 3

    brief = engine.brief(
        NOW,
        NOW + timedelta(hours=2),
        console_groups={"fictional": [SCOPE]},
        scope_ids=[SCOPE],
    )
    group = brief["groups"][0]
    assert group["counts"] == {"touched": 4, "completed": 1, "blocked": 1}
    assert [item["subject_key"] for item in group["matters"]] == ["root"]
    assert group["matters"][0]["descendants_completed"] == 1
    assert group["matters"][0]["descendants_total"] == 3

    engine.set_seen(SCOPE, "root", last_seen_at=NOW + timedelta(hours=2))
    engine.set_seen(SCOPE, "child", last_seen_at=NOW + timedelta(hours=2))
    engine.set_seen(SCOPE, "sibling", last_seen_at=NOW + timedelta(hours=2))
    assert engine.matter_unseen(SCOPE, "root") is True
    engine.set_seen(SCOPE, "grandchild", last_seen_at=NOW + timedelta(hours=2))
    assert engine.matter_unseen(SCOPE, "root") is False


class _CardExtractor:
    def extract(self, *, records: list[Record], **_kwargs):
        record = records[0]
        title = "Amber launch" if record.native_id == "root" else "Zephyr follow-up"
        card = EpisodeCard.model_validate(
            {
                "card_id": f"card-{record.native_id}",
                "scope_id": SCOPE,
                "date": "2026-08-05",
                "occurred_at": record.sent_at,
                "title": title,
                "status": "open",
                "source_refs": [record.to_source_ref()],
            }
        )
        return type("Report", (), {"cards": [card], "rejection_counts": {}})()


def _record(native_id: str, *, minute: int) -> Record:
    return Record.model_validate(
        {
            "record_id": f"octo-room:{native_id}",
            "container_id": "octo-room",
            "native_id": native_id,
            "sent_at": NOW + timedelta(minutes=minute),
            "author": {
                "id": "dana",
                "display_name": "Dana Reyes",
                "kind": "human",
            },
            "content": (
                "Crimson beacon."
                if native_id == "root"
                else "Turquoise orchard."
            ),
        }
    )


def test_new_subject_parent_suggestion_resolves_through_standard_gate(tmp_path) -> None:
    clock_values = iter(
        [
            NOW + timedelta(minutes=10),
            NOW + timedelta(minutes=20),
            NOW + timedelta(minutes=21),
        ]
    )
    engine = Engine(
        tmp_path / "suggestion.db",
        extractor=_CardExtractor(),
        clock=lambda: next(clock_values),
    )
    engine.add_records([_record("root", minute=0)], scope_id=SCOPE)
    engine.add_records([_record("child", minute=5)], scope_id=SCOPE)

    reviews = engine.review_items(SCOPE)
    assert len(reviews) == 1
    proposal = reviews[0].candidates_json[0]
    assert proposal["action"] == "attach_subgoal"
    resolved = engine.resolve_review(
        SCOPE,
        reviews[0].review_id,
        action="attach_subgoal",
        parent_subject_key=proposal["parent_subject_key"],
        source_refs=[_source("review:attach-subgoal", minute=21)],
    )
    assert resolved.resolution_json["action"] == "attach_subgoal"
    child_key = proposal["subject_key"]
    assert engine.matter_graph(SCOPE, child_key).parent_chain[0].subject_key == proposal[
        "parent_subject_key"
    ]
    assertion = engine.query.current(SCOPE, child_key, "part_of")[0]
    assert assertion.origin == "human"
    assert assertion.source_ids == ["review:attach-subgoal"]


def test_rest_graph_rollups_corrections_and_attach_subgoal_shape(tmp_path) -> None:
    engine = _tree_engine(tmp_path)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=engine))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            graph = await client.get(f"/v1/scopes/{SCOPE}/matters/root/graph")
            assert graph.status_code == 200
            assert graph.json()["rollup"]["descendants_total"] == 3
            matters = await client.get(f"/v1/scopes/{SCOPE}/matters")
            assert matters.json()[0]["descendants_completed"] == 1
            detail = await client.get(f"/v1/scopes/{SCOPE}/matters/root")
            assert detail.json()["graph"]["node"]["decisions"] == [
                "Adopt the fictional plan."
            ]
            correction_payload = _correction(
                "root",
                "decision",
                "Record another fictional decision.",
                minute=15,
            )
            correction_payload.pop("scope_id")
            correction_payload["valid_from"] = correction_payload[
                "valid_from"
            ].isoformat()
            correction_payload["source_refs"][0]["sent_at"] = (
                correction_payload["source_refs"][0]["sent_at"].isoformat()
            )
            correction = await client.post(
                f"/v1/scopes/{SCOPE}/corrections",
                json=correction_payload,
            )
            assert correction.status_code == 200

    asyncio.run(scenario())


class _SemanticGateway:
    def __init__(self) -> None:
        self.index = 0

    def complete(self, **_kwargs) -> str:
        responses = [
            {
                "candidates": [
                    {
                        "subject_key": "child",
                        "subject_type": "MATTER",
                        "parent_subject_key": None,
                        "subject_title": None,
                        "predicate": "part_of",
                        "operation": "ASSERT",
                        "object_value": "root",
                        "valid_from": "2026-08-05T09:01:00Z",
                        "source_ids": ["octo-room:child"],
                        "confidence": 0.99,
                    }
                ]
            },
            {"candidates": []},
        ]
        response = responses[self.index]
        self.index += 1
        return json.dumps(response)


def test_dream_offered_structure_edges_are_rejected(tmp_path) -> None:
    # Governance (spec 25.1): the single-card dream pass never sees the
    # tree, so structure edges it offers are rejected — they belong to
    # theme convergence and the unified loop.
    engine = Engine(
        tmp_path / "model-edge.db",
        gateway=_SemanticGateway(),
        clock=lambda: NOW + timedelta(hours=1),
    )
    engine._ingest_cards_sync(
        [_card("root", minute=0), _card("child", minute=1)]
    )
    report = engine.dream(SCOPE)
    assert report.accepted_candidates == 0
    assert list(engine.matter_graph(SCOPE, "child").parent_chain) == []


def test_cli_graph_prints_the_tree_and_rollup(tmp_path) -> None:
    _tree_engine(tmp_path)
    completed = CliRunner().invoke(
        app,
        [
            "graph",
            SCOPE,
            "root",
            "--db",
            str(tmp_path / "goal-tree.db"),
        ],
    )

    assert completed.exit_code == 0
    assert "completed=1/3 blocked=1" in completed.stdout
    assert "Fictional grandchild [blocked]" in completed.stdout
