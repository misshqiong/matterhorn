from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from typer.testing import CliRunner

from matterhorn.api import create_app
from matterhorn.canonical import canonical_json, derive_assertion_id, object_key
from matterhorn.cli.app import app
from matterhorn.contracts import (
    Assertion,
    EpisodeCard,
    Operation,
    Origin,
    Record,
    SourceRef,
)
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


def _seed_legacy_cycle(engine: Engine) -> None:
    """Store a mutually-parented pair the way pre-INV-22 data holds one.

    Both directions were legally admitted under the retract-override
    election; weighted election reopens them as a projected two-cycle.
    The gate blocks this pair through every current door, so the seed
    writes the store directly.
    """

    engine._ingest_cards_sync([_card("loop-a", minute=0), _card("loop-b", minute=1)])
    with engine.store.transaction():
        for source, target, minute in (("loop-a", "loop-b", 10), ("loop-b", "loop-a", 11)):
            refs = [SourceRef.model_validate(_source(f"legacy:{source}", minute=minute))]
            assert engine.store.add_assertion(
                Assertion(
                    assertion_id=derive_assertion_id(
                        SCOPE,
                        source,
                        "part_of",
                        Operation.ASSERT,
                        object_key(target),
                        NOW + timedelta(minutes=minute),
                        refs,
                    ),
                    scope_id=SCOPE,
                    subject_key=source,
                    subject_type="MATTER",
                    predicate="part_of",
                    operation=Operation.ASSERT,
                    object_value=target,
                    object_key=object_key(target),
                    valid_from=NOW + timedelta(minutes=minute),
                    recorded_at=NOW + timedelta(minutes=minute),
                    source_refs=refs,
                    origin=Origin.model,
                )
            )
        engine._rebuild(SCOPE)


def test_preexisting_cycle_does_not_veto_unrelated_edges(tmp_path) -> None:
    engine = Engine(tmp_path / "poisoned.db", clock=lambda: NOW + timedelta(hours=1))
    _seed_legacy_cycle(engine)
    engine._ingest_cards_sync([_card("root", minute=2), _card("child", minute=3)])

    # The legacy loop elsewhere in the scope never freezes unrelated edges.
    engine.correct(_correction("child", "part_of", "root", minute=20))
    # An edge pointing into the loop region creates no cycle through root.
    engine.correct(_correction("root", "part_of", "loop-a", minute=21))
    # An edge that itself closes a cycle is still rejected.
    with pytest.raises(ValueError, match="STRUCTURE_CYCLE"):
        engine.correct(_correction("loop-a", "part_of", "child", minute=22))


def test_structure_cycles_reports_the_projected_loop(tmp_path) -> None:
    engine = Engine(tmp_path / "audit.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync([_card("root", minute=0), _card("child", minute=1)])
    engine.correct(_correction("child", "part_of", "root", minute=5))
    assert engine.structure_cycles(SCOPE) == []

    _seed_legacy_cycle(engine)
    assert engine.structure_cycles(SCOPE) == [["loop-a", "loop-b"]]


def test_merging_a_mutually_parented_pair_dissolves_the_cycle(tmp_path) -> None:
    engine = Engine(tmp_path / "merge-repair.db", clock=lambda: NOW + timedelta(hours=1))
    _seed_legacy_cycle(engine)
    assert engine.structure_cycles(SCOPE) == [["loop-a", "loop-b"]]

    engine.merge_subjects(
        SCOPE,
        "loop-a",
        "loop-b",
        source_refs=[SourceRef.model_validate(_source("review:repair", minute=30))],
        valid_from=NOW + timedelta(minutes=30),
    )

    # The collapsed self-edge is void: no residual loop, and the survivor
    # returns to the wall as an ordinary root.
    assert engine.structure_cycles(SCOPE) == []
    assert "loop-b" in {item.subject_key for item in engine.matters(SCOPE)}


def test_retract_that_flips_election_onto_cycle_is_rejected(tmp_path) -> None:
    engine = Engine(tmp_path / "retract-mint.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [_card("alpha", minute=0), _card("beta", minute=1), _card("gamma", minute=2)]
    )
    engine.correct(_correction("alpha", "part_of", "gamma", minute=10))
    # Equal human weight; recency elects beta and closes alpha -> gamma.
    engine.correct(_correction("alpha", "part_of", "beta", minute=11))
    engine.correct(_correction("gamma", "part_of", "alpha", minute=12))

    # Withdrawing the beta contribution would re-elect gamma and mint
    # alpha -> gamma -> alpha; the retract runs the same minting gate.
    with pytest.raises(ValueError, match="STRUCTURE_CYCLE"):
        engine.correct(
            {**_correction("alpha", "part_of", "beta", minute=13), "operation": "RETRACT"}
        )

    # The field-wide retract can only empty the slot: escape stays open.
    engine.correct(
        {**_correction("alpha", "part_of", None, minute=14), "operation": "RETRACT"}
    )
    assert engine.query.current(SCOPE, "alpha", "part_of") == []
    assert engine.structure_cycles(SCOPE) == []


def test_losing_vote_passes_while_subject_rides_a_legacy_cycle(tmp_path) -> None:
    from matterhorn.engine.goal_graph import structure_rejection

    engine = Engine(tmp_path / "losing-vote.db", clock=lambda: NOW + timedelta(hours=1))
    _seed_legacy_cycle(engine)
    engine._ingest_cards_sync([_card("haven", minute=3)])

    # One fresh model vote loses to nothing it can flip: loop-a's elected
    # edge stays loop-b, so the losing vote is admissible evidence.
    refs = [SourceRef.model_validate(_source("model:losing", minute=20))]
    losing = Assertion(
        assertion_id=derive_assertion_id(
            SCOPE,
            "loop-a",
            "part_of",
            Operation.ASSERT,
            object_key("haven"),
            NOW + timedelta(minutes=9),
            refs,
        ),
        scope_id=SCOPE,
        subject_key="loop-a",
        subject_type="MATTER",
        predicate="part_of",
        operation=Operation.ASSERT,
        object_value="haven",
        object_key=object_key("haven"),
        valid_from=NOW + timedelta(minutes=9),
        recorded_at=NOW + timedelta(minutes=20),
        source_refs=refs,
        origin=Origin.model,
    )
    assert (
        structure_rejection(
            losing,
            profile=engine.profile,
            subjects=engine.store.subjects(SCOPE),
            assertions=engine.store.assertions(SCOPE),
            merges=engine.store.subject_merges(SCOPE),
            human_edge_weight=engine.human_edge_weight,
        )
        is None
    )

    # A human vote flips loop-a out of the loop entirely — escape admitted.
    engine.correct(_correction("loop-a", "part_of", "haven", minute=21))
    assert engine.structure_cycles(SCOPE) == []


def test_merge_that_would_activate_cycle_is_rejected(tmp_path) -> None:
    engine = Engine(tmp_path / "merge-mint.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [_card("left", minute=0), _card("bridge", minute=1), _card("right", minute=2)]
    )
    engine.correct(_correction("left", "part_of", "bridge", minute=5))
    engine.correct(_correction("bridge", "part_of", "right", minute=6))

    with pytest.raises(ValueError, match="STRUCTURE_CYCLE"):
        engine.merge_subjects(
            SCOPE,
            "right",
            "left",
            source_refs=[SourceRef.model_validate(_source("review:bad-merge", minute=7))],
            valid_from=NOW + timedelta(minutes=7),
        )

    # Collapsing the edge endpoints instead stays a tree.
    engine.merge_subjects(
        SCOPE,
        "right",
        "bridge",
        source_refs=[SourceRef.model_validate(_source("review:good-merge", minute=8))],
        valid_from=NOW + timedelta(minutes=8),
    )
    assert engine.structure_cycles(SCOPE) == []


def test_merge_suggestion_review_resolves_through_the_merge_door(tmp_path) -> None:
    from matterhorn.contracts import ReviewItem

    engine = Engine(tmp_path / "merge-review.db", clock=lambda: NOW + timedelta(hours=1))
    _seed_legacy_cycle(engine)
    card = EpisodeCard(
        card_id="merge-suggestion",
        scope_id=SCOPE,
        date="2026-08-05",
        title="Fictional loop-a ↔ Fictional loop-b",
        source_refs=[SourceRef.model_validate(_source("review:mutual", minute=30))],
        subject_key="loop-a",
    )
    with engine.store.transaction():
        engine.store.add_review_item(
            ReviewItem(
                scope_id=SCOPE,
                review_id="review_merge_pair",
                card_json=card.model_dump(mode="json"),
                reasons=["MERGE_SUGGESTION"],
                candidates_json=[
                    {
                        "action": "merge",
                        "subject_key": "loop-a",
                        "parent_subject_key": "loop-b",
                        "title": "Fictional loop-b",
                    }
                ],
                created_at=NOW + timedelta(minutes=30),
            )
        )

    resolved = engine.resolve_review(
        SCOPE,
        "review_merge_pair",
        action="merge",
        subject_key="loop-a",
        parent_subject_key="loop-b",
        source_refs=[_source("review:merge-resolution", minute=31)],
    )

    assert resolved.resolved_at is not None
    assert engine.canonical_subject_key(SCOPE, "loop-a") == "loop-b"
    assert engine.structure_cycles(SCOPE) == []


def _model_part_of(subject_key: str, target: str, *, minute: int) -> Assertion:
    refs = [SourceRef.model_validate(_source(f"model:{subject_key}:{minute}", minute=minute))]
    return Assertion(
        assertion_id=derive_assertion_id(
            SCOPE,
            subject_key,
            "part_of",
            Operation.ASSERT,
            object_key(target),
            NOW + timedelta(minutes=minute),
            refs,
        ),
        scope_id=SCOPE,
        subject_key=subject_key,
        subject_type="MATTER",
        predicate="part_of",
        operation=Operation.ASSERT,
        object_value=target,
        object_key=object_key(target),
        valid_from=NOW + timedelta(minutes=minute),
        recorded_at=NOW + timedelta(minutes=minute),
        source_refs=refs,
        origin=Origin.model,
    )


def test_one_human_retract_withdraws_every_model_contribution(tmp_path) -> None:
    engine = Engine(tmp_path / "retract-authority.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync([_card("child", minute=0), _card("wrong", minute=1)])
    with engine.store.transaction():
        for minute in (10, 11, 12):
            assert engine._add_assertion(_model_part_of("child", "wrong", minute=minute))
        engine._rebuild(SCOPE)
    assert [item.value for item in engine.query.current(SCOPE, "child", "part_of")] == ["wrong"]

    engine.correct({**_correction("child", "part_of", "wrong", minute=20), "operation": "RETRACT"})

    assert engine.query.current(SCOPE, "child", "part_of") == []


def test_automatic_doors_hold_a_human_placement_but_the_correction_door_does_not(
    tmp_path,
) -> None:
    from matterhorn.engine.goal_graph import automatic_reparent_rejection

    engine = Engine(tmp_path / "placement-held.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [_card("child", minute=0), _card("wrong", minute=1), _card("right", minute=2)]
    )
    engine.correct(_correction("child", "part_of", "right", minute=10))

    def held(target: str, *, minute: int) -> bool:
        return (
            automatic_reparent_rejection(
                _model_part_of("child", target, minute=minute),
                assertions=engine.store.assertions(SCOPE),
                merges=engine.store.subject_merges(SCOPE),
            )
            is not None
        )

    # A standing human placement closes the automatic door to other targets,
    # while re-affirming the human's own target stays admissible.
    assert held("wrong", minute=11)
    assert not held("right", minute=12)

    # The correction door keeps INV-22's arithmetic: a human may be outvoted
    # there deliberately, which is what case 113 pins.
    assert engine._structure_rejection(_model_part_of("child", "wrong", minute=13)) is None

    # After the human empties the slot, the retracted pair stays closed to
    # automatic re-assertion -- otherwise the next window silently undoes it.
    engine.correct({**_correction("child", "part_of", "right", minute=20), "operation": "RETRACT"})
    assert engine.query.current(SCOPE, "child", "part_of") == []
    assert held("right", minute=21)


def _held(engine: Engine, target: str, *, minute: int, child: str = "child") -> bool:
    from matterhorn.engine.goal_graph import automatic_reparent_rejection

    return (
        automatic_reparent_rejection(
            _model_part_of(child, target, minute=minute),
            assertions=engine.store.assertions(SCOPE),
            merges=engine.store.subject_merges(SCOPE),
        )
        is not None
    )


def test_gate_reads_the_same_rows_the_election_reads(tmp_path) -> None:
    """A merge must not leave the gate holding a withdrawn placement."""

    engine = Engine(tmp_path / "gate-canonical.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [
            _card("child", minute=0),
            _card("alpha", minute=1),
            _card("beta", minute=2),
            _card("gamma", minute=3),
        ]
    )
    engine.correct(_correction("child", "part_of", "alpha", minute=10))
    engine.merge_subjects(
        SCOPE,
        "alpha",
        "beta",
        source_refs=[SourceRef.model_validate(_source("review:merge", minute=11))],
        valid_from=NOW + timedelta(minutes=11),
    )
    # The console only exposes the survivor, so this is the retract a human
    # can actually issue; the election honours it.
    engine.correct(
        {**_correction("child", "part_of", "beta", minute=12), "operation": "RETRACT"}
    )
    assert engine.query.current(SCOPE, "child", "part_of") == []

    # The gate must agree with the projection, not with the raw rows.
    assert not _held(engine, "gamma", minute=13)


def test_merge_collapsed_edge_does_not_hold_the_survivor(tmp_path) -> None:
    """The cycle repair must not make its own survivor unparentable."""

    engine = Engine(tmp_path / "gate-collapse.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [_card("child", minute=0), _card("alpha", minute=1), _card("gamma", minute=2)]
    )
    engine.correct(_correction("child", "part_of", "alpha", minute=10))
    engine.merge_subjects(
        SCOPE,
        "alpha",
        "child",
        source_refs=[SourceRef.model_validate(_source("review:repair", minute=11))],
        valid_from=NOW + timedelta(minutes=11),
    )
    assert engine.query.current(SCOPE, "child", "part_of") == []

    assert not _held(engine, "gamma", minute=12)


def test_human_reparent_holds_the_abandoned_target(tmp_path) -> None:
    """part_of is SINGLE: a later human ASSERT supersedes the earlier one."""

    engine = Engine(tmp_path / "gate-single.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [_card("child", minute=0), _card("wrong", minute=1), _card("right", minute=2)]
    )
    engine.correct(_correction("child", "part_of", "wrong", minute=10))
    engine.correct(_correction("child", "part_of", "right", minute=11))
    assert [item.value for item in engine.query.current(SCOPE, "child", "part_of")] == [
        "right"
    ]

    # The abandoned parent stays closed; the human's own live target passes.
    assert _held(engine, "wrong", minute=12)
    assert not _held(engine, "right", minute=13)


def test_naming_the_live_human_target_is_never_a_reparent(tmp_path) -> None:
    engine = Engine(tmp_path / "gate-agree.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync([_card("child", minute=0), _card("right", minute=1)])
    # A pair with retract history that the human later re-asserted.
    engine.correct(_correction("child", "part_of", "right", minute=10))
    engine.correct(
        {**_correction("child", "part_of", "right", minute=11), "operation": "RETRACT"}
    )
    engine.correct(_correction("child", "part_of", "right", minute=12))

    # Agreeing with the human's live placement is not a re-parent.
    assert not _held(engine, "right", minute=13)


def test_cycles_command_reports_loops_and_exits_nonzero(tmp_path) -> None:
    db = tmp_path / "cycles-cli.db"
    engine = Engine(db, clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync([_card("root", minute=0)])
    runner = CliRunner()

    clean = runner.invoke(app, ["cycles", SCOPE, "--db", str(db)])
    assert clean.exit_code == 0
    assert json.loads(clean.stdout) == []

    _seed_legacy_cycle(engine)
    poisoned = runner.invoke(app, ["cycles", SCOPE, "--db", str(db)])
    assert poisoned.exit_code == 1
    assert [item["subject_key"] for item in json.loads(poisoned.stdout)[0]] == [
        "loop-a",
        "loop-b",
    ]


def test_spawned_from_remains_single_most_recent_not_weight_elected(tmp_path) -> None:
    engine = Engine(tmp_path / "spawned-from.db", clock=lambda: NOW + timedelta(hours=1))
    engine._ingest_cards_sync(
        [
            _card("root-a", minute=0),
            _card("root-b", minute=1),
            _card("child", minute=2),
        ]
    )
    engine.correct(_correction("child", "spawned_from", "root-a", minute=3))
    engine.correct(_correction("child", "spawned_from", "root-b", minute=4))

    current = engine.query.current(SCOPE, "child", "spawned_from")
    assert [item.value for item in current] == ["root-b"]
    assert engine.matter_graph(SCOPE, "child").node.birth_instant == (
        NOW + timedelta(minutes=4)
    )


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
