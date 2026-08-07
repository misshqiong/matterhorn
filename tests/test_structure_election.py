from __future__ import annotations

from datetime import UTC, datetime, timedelta

from matterhorn.canonical import canonical_json, object_key
from matterhorn.contracts import Assertion, Operation, Origin, SourceRef
from matterhorn.contracts.schema import resolve_schema
from matterhorn.engine.structure_election import (
    elected_part_of,
    election_history,
)
from matterhorn.projection import project_assertions

NOW = datetime(2026, 8, 7, 9, tzinfo=UTC)
SCOPE = "octo-election-unit"


def _assertion(
    target: str | None,
    *,
    minute: int,
    origin: Origin,
    operation: Operation = Operation.ASSERT,
    assertion_id: str | None = None,
) -> Assertion:
    selected_key = object_key(target) if target is not None else "*"
    return Assertion(
        assertion_id=assertion_id or f"a-{origin.value}-{minute}-{target}",
        scope_id=SCOPE,
        subject_key="child",
        subject_type="MATTER",
        predicate="part_of",
        operation=operation,
        object_value=target,
        object_key=selected_key,
        valid_from=NOW + timedelta(minutes=minute),
        recorded_at=NOW + timedelta(minutes=minute, seconds=30),
        source_refs=[
            SourceRef(
                source_id=f"octo-org:election:{origin.value}:{minute}:{target}",
                sent_at=NOW + timedelta(minutes=minute),
                sender="Dana Reyes",
            )
        ],
        origin=origin,
    )


def test_human_weight_beats_nine_models_and_ten_models_win_by_recency() -> None:
    human = _assertion("human-parent", minute=1, origin=Origin.human)
    model = [
        _assertion("model-parent", minute=minute, origin=Origin.model)
        for minute in range(2, 12)
    ]

    before = elected_part_of([human, *model[:9]])[(SCOPE, "child")]
    after = elected_part_of([human, *model])[(SCOPE, "child")]

    assert before.elected_target == "human-parent"
    assert before.candidate("human-parent").weights() == {
        "human": 10,
        "model": 0,
        "total": 10,
    }
    assert before.candidate("model-parent").weights() == {
        "human": 0,
        "model": 9,
        "total": 9,
    }
    assert after.elected_target == "model-parent"
    assert after.candidate("model-parent").model_weight == 10


def test_human_retract_withdraws_only_the_matching_human_contribution() -> None:
    model = _assertion("model-parent", minute=1, origin=Origin.model)
    human = _assertion("human-parent", minute=2, origin=Origin.human)
    retract = _assertion(
        "human-parent",
        minute=3,
        origin=Origin.human,
        operation=Operation.RETRACT,
    )

    history = election_history([model, human, retract])

    assert [step.election.elected_target for step in history] == [
        "model-parent",
        "human-parent",
        "model-parent",
    ]
    final = history[-1].election
    assert final.candidate("human-parent") is None
    assert final.candidate("model-parent").model_weight == 1


def test_equal_weight_tie_uses_most_recent_contribution() -> None:
    assertions = [
        _assertion("parent-a", minute=1, origin=Origin.model),
        _assertion("parent-b", minute=2, origin=Origin.model),
        _assertion("parent-a", minute=3, origin=Origin.model),
        _assertion("parent-b", minute=4, origin=Origin.model),
    ]

    election = elected_part_of(assertions)[(SCOPE, "child")]

    assert election.candidate("parent-a").total_weight == 2
    assert election.candidate("parent-b").total_weight == 2
    assert election.elected_target == "parent-b"


def test_duplicate_assertion_id_counts_once_and_projection_is_order_independent() -> None:
    human = _assertion("human-parent", minute=1, origin=Origin.human)
    duplicate = _assertion(
        "model-parent",
        minute=2,
        origin=Origin.model,
        assertion_id="model-admission",
    )
    assertions = [human, duplicate, duplicate]
    election = elected_part_of(assertions)[(SCOPE, "child")]
    assert election.candidate("model-parent").model_weight == 1

    profile = resolve_schema("org-matters/v1")
    forward, _ = project_assertions(assertions, profile)
    reverse, _ = project_assertions(list(reversed(assertions)), profile)
    assert canonical_json([item.model_dump(mode="json") for item in forward]) == (
        canonical_json([item.model_dump(mode="json") for item in reverse])
    )
