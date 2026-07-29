from datetime import date, datetime, timezone

from matterhorn.contracts import EpisodeCard, PredicateDefinition, SourceRef
from matterhorn.engine.extractor import observe_field


def _card(**values):
    payload = {
        "card_id": "c1",
        "scope_id": "s",
        "date": date(2026, 1, 1),
        "title": "T",
        "source_refs": [
            SourceRef(
                source_id="m1",
                sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                sender="u",
            )
        ],
    }
    payload.update(values)
    return EpisodeCard(**payload)


def _predicate(guard: str):
    return PredicateDefinition(
        name="phase",
        subject="THING",
        cardinality="SINGLE",
        extraction="deterministic",
        source_field="status",
        retract_guard=guard,
    )


def test_explicit_missing_is_no_observation() -> None:
    observation = observe_field(_card(), _predicate("explicit"))
    assert not observation.updates_projection
    assert not observation.retract


def test_explicit_clear_is_the_same_update_and_retract_gate() -> None:
    observation = observe_field(
        _card(cleared_fields=["status"]), _predicate("explicit")
    )
    assert observation.updates_projection
    assert observation.retract


def test_never_guard_refuses_explicit_clear() -> None:
    observation = observe_field(_card(cleared_fields=["status"]), _predicate("never"))
    assert not observation.updates_projection


def test_implicit_retracts_absent_and_explicit_null() -> None:
    assert observe_field(_card(), _predicate("implicit")).retract
    assert observe_field(_card(status=None), _predicate("implicit")).retract
