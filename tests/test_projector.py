from datetime import UTC, datetime

from matterhorn.canonical import derive_assertion_id, object_key
from matterhorn.contracts import Assertion, SchemaProfile, SourceRef
from matterhorn.projection import project_assertions


def _profile():
    return SchemaProfile.model_validate(
        {
            "schema": "test/v1",
            "subjects": [{"type": "THING"}],
            "predicates": [
                {
                    "name": "phase",
                    "subject": "THING",
                    "cardinality": "SINGLE",
                    "extraction": "deterministic",
                    "source_field": "status",
                }
            ],
        }
    )


def _assertion(value, recorded_at, origin="model"):
    valid = datetime(2026, 1, 1, tzinfo=UTC)
    source = [
        SourceRef(
            source_id=f"m-{value}",
            sent_at=valid,
            sender="u",
        )
    ]
    key = object_key(value)
    return Assertion(
        assertion_id=derive_assertion_id(
            "s", "x", "phase", "ASSERT", key, valid, source
        ),
        scope_id="s",
        subject_key="x",
        subject_type="THING",
        predicate="phase",
        operation="ASSERT",
        object_value=value,
        object_key=key,
        valid_from=valid,
        recorded_at=recorded_at,
        source_refs=source,
        origin=origin,
    )


def test_human_rank_beats_later_recorded_model_and_conflict_is_counted() -> None:
    human = _assertion(
        "human", datetime(2026, 1, 1, 9, tzinfo=UTC), "human"
    )
    model = _assertion(
        "model", datetime(2026, 1, 1, 12, tzinfo=UTC), "model"
    )
    intervals, stats = project_assertions([human, model], _profile())
    assert [item.object_value for item in intervals] == ["human"]
    assert stats[0].conflicts_resolved == 1


def test_same_value_at_same_or_later_instant_does_not_split() -> None:
    first = _assertion("same", datetime(2026, 1, 1, 9, tzinfo=UTC))
    second = first.model_copy(
        update={
            "assertion_id": "f" * 64,
            "valid_from": datetime(2026, 1, 2, tzinfo=UTC),
            "recorded_at": datetime(2026, 1, 2, 9, tzinfo=UTC),
        }
    )
    intervals, _ = project_assertions([first, second], _profile())
    assert len(intervals) == 1
    assert intervals[0].valid_to is None
