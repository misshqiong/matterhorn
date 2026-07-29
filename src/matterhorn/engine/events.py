from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from matterhorn.canonical import derive_event_id
from matterhorn.contracts import (
    Assertion,
    Cardinality,
    ChangeEvent,
    EventType,
    Interval,
    Operation,
    Origin,
    SchemaProfile,
)


def derive_change_events(
    previous: Iterable[Interval],
    current: Iterable[Interval],
    assertions: Iterable[Assertion],
    profile: SchemaProfile,
) -> list[ChangeEvent]:
    """Derive event candidates exclusively from a projection interval diff."""

    old_intervals = list(previous)
    new_intervals = list(current)
    assertion_by_id = {item.assertion_id: item for item in assertions}
    candidates: list[ChangeEvent] = []

    old_subjects = {item.subject_key for item in old_intervals}
    new_by_subject: dict[str, list[Interval]] = {}
    for interval in new_intervals:
        new_by_subject.setdefault(interval.subject_key, []).append(interval)
    for subject_key in sorted(set(new_by_subject) - old_subjects):
        trigger = min(new_by_subject[subject_key], key=_interval_rank)
        candidates.append(
            _event(
                EventType.matter_created,
                trigger,
                assertion_by_id,
                old_value=None,
                new_value=trigger.object_value,
            )
        )

    status_predicates = {
        item.name
        for item in profile.predicates
        if item.cardinality == Cardinality.SINGLE
        and item.source_field == "status"
    }
    blocker_predicates = {
        item.name
        for item in profile.predicates
        if item.source_field == "blocker"
    }
    semantic_predicates = {
        item.name for item in profile.predicates if item.extraction.value == "semantic"
    }
    old_current = _current_values(old_intervals, profile)
    new_current = _current_values(new_intervals, profile)

    for key in sorted(set(old_current) | set(new_current)):
        subject_key, predicate = key
        old_state = old_current.get(key)
        new_state = new_current.get(key)
        old_value = old_state[0] if old_state is not None else None
        new_value = new_state[0] if new_state is not None else None
        if old_value == new_value:
            continue
        trigger = _change_trigger(
            subject_key,
            predicate,
            old_state,
            new_state,
            old_intervals,
            new_intervals,
            assertion_by_id,
        )
        if trigger is None:
            continue
        if predicate in status_predicates:
            candidates.append(
                _event(
                    EventType.status_changed,
                    trigger,
                    assertion_by_id,
                    old_value=old_value,
                    new_value=new_value,
                )
            )
            completion = profile.completion
            if (
                completion is not None
                and predicate == completion.predicate
                and new_value in completion.completed_values
                and old_value not in completion.completed_values
            ):
                candidates.append(
                    _event(
                        EventType.matter_completed,
                        trigger,
                        assertion_by_id,
                        old_value=old_value,
                        new_value=new_value,
                    )
                )
        if predicate in blocker_predicates:
            was_blocked = bool(old_value)
            is_blocked = bool(new_value)
            if was_blocked != is_blocked:
                candidates.append(
                    _event(
                        EventType.blocked if is_blocked else EventType.unblocked,
                        trigger,
                        assertion_by_id,
                        old_value=old_value,
                        new_value=new_value,
                    )
                )
        assertion = _trigger_assertion(trigger, assertion_by_id)
        if (
            assertion is not None
            and assertion.origin == Origin.human
            and old_value is not None
        ):
            candidates.append(
                _event(
                    EventType.value_corrected,
                    trigger,
                    assertion_by_id,
                    old_value=old_value,
                    new_value=new_value,
                )
            )

    old_ids = {item.interval_id for item in old_intervals}
    for interval in new_intervals:
        if (
            interval.interval_id not in old_ids
            and interval.predicate in semantic_predicates
        ):
            candidates.append(
                _event(
                    EventType.decision_adopted,
                    interval,
                    assertion_by_id,
                    old_value=(
                        old_current.get((interval.subject_key, interval.predicate), (None,))[
                            0
                        ]
                    ),
                    new_value=interval.object_value,
                )
            )

    unique = {item.event_id: item for item in candidates}
    return sorted(unique.values(), key=lambda item: (item.recorded_at, item.event_id))


def _current_values(
    intervals: list[Interval], profile: SchemaProfile
) -> dict[tuple[str, str], tuple[Any, list[Interval]]]:
    grouped: dict[tuple[str, str], list[Interval]] = {}
    for interval in intervals:
        if interval.valid_to is None:
            grouped.setdefault((interval.subject_key, interval.predicate), []).append(
                interval
            )
    result: dict[tuple[str, str], tuple[Any, list[Interval]]] = {}
    for key, items in grouped.items():
        ordered = sorted(items, key=lambda item: item.object_key)
        definition = profile.predicate(key[1])
        value = (
            ordered[-1].object_value
            if definition.cardinality == Cardinality.SINGLE
            else [item.object_value for item in ordered]
        )
        result[key] = (value, ordered)
    return result


def _change_trigger(
    subject_key: str,
    predicate: str,
    old_state: tuple[Any, list[Interval]] | None,
    new_state: tuple[Any, list[Interval]] | None,
    old_intervals: list[Interval],
    new_intervals: list[Interval],
    assertion_by_id: dict[str, Assertion],
) -> Interval | Assertion | None:
    if new_state is not None:
        old_ids = {
            item.interval_id for item in old_state[1]
        } if old_state is not None else set()
        changed = [item for item in new_state[1] if item.interval_id not in old_ids]
        if changed:
            return max(changed, key=_interval_rank)
        return max(new_state[1], key=_interval_rank)

    ended = [
        new
        for new in new_intervals
        if new.subject_key == subject_key
        and new.predicate == predicate
        and new.valid_to is not None
        and any(
            old.interval_id == new.interval_id and old.valid_to is None
            for old in old_intervals
        )
    ]
    if not ended:
        return None
    instant = max(item.valid_to for item in ended if item.valid_to is not None)
    retractions = [
        item
        for item in assertion_by_id.values()
        if item.subject_key == subject_key
        and item.predicate == predicate
        and item.operation == Operation.RETRACT
        and item.valid_from == instant
    ]
    return max(retractions, key=_assertion_rank) if retractions else max(
        ended, key=_interval_rank
    )


def _event(
    event_type: EventType,
    trigger: Interval | Assertion,
    assertion_by_id: dict[str, Assertion],
    *,
    old_value: Any,
    new_value: Any,
) -> ChangeEvent:
    assertion = _trigger_assertion(trigger, assertion_by_id)
    if assertion is None:
        if not isinstance(trigger, Interval):
            raise ValueError("event trigger assertion is unavailable")
        source_ids = list(dict.fromkeys(ref.source_id for ref in trigger.source_refs))
        recorded_at = trigger.valid_from
        origin = trigger.origin
    else:
        source_ids = list(dict.fromkeys(ref.source_id for ref in assertion.source_refs))
        recorded_at = assertion.recorded_at
        origin = assertion.origin
    return ChangeEvent(
        event_id=derive_event_id(
            event_type,
            trigger.scope_id,
            trigger.subject_key,
            trigger.predicate,
            old_value,
            new_value,
            trigger.valid_from,
            recorded_at,
            origin,
            source_ids,
        ),
        event_type=event_type,
        scope_id=trigger.scope_id,
        subject_key=trigger.subject_key,
        predicate=trigger.predicate,
        old_value=old_value,
        new_value=new_value,
        valid_from=trigger.valid_from,
        recorded_at=recorded_at,
        origin=origin,
        source_ids=source_ids,
    )


def _trigger_assertion(
    trigger: Interval | Assertion,
    assertion_by_id: dict[str, Assertion],
) -> Assertion | None:
    if isinstance(trigger, Assertion):
        return trigger
    return assertion_by_id.get(trigger.assertion_id)


def _interval_rank(interval: Interval) -> tuple:
    return (
        interval.valid_from,
        interval.predicate.encode("utf-8"),
        interval.object_key.encode("utf-8"),
        interval.assertion_id.encode("utf-8"),
    )


def _assertion_rank(assertion: Assertion) -> tuple:
    return (
        assertion.valid_from,
        1 if assertion.origin == Origin.human else 0,
        assertion.recorded_at,
        assertion.assertion_id.encode("utf-8"),
    )
