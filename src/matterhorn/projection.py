"""Deterministic assertion projection shared by engine and renderers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from matterhorn.canonical import instant_text, stable_hash
from matterhorn.contracts import (
    FIELD_WIDE_RETRACT,
    Assertion,
    Cardinality,
    Interval,
    Operation,
    Origin,
    ProjectionStats,
    SchemaProfile,
)
from matterhorn.engine.structure_election import (
    DEFAULT_HUMAN_EDGE_WEIGHT,
    GATHERS,
    PART_OF,
    ElectionCandidate,
    election_history,
)


def _rank(assertion: Assertion) -> tuple:
    return (
        assertion.valid_from,
        1 if assertion.origin == Origin.human else 0,
        assertion.recorded_at,
        assertion.assertion_id,
    )


def _interval(assertion: Assertion, valid_to=None) -> Interval:
    interval_id = stable_hash(
        [
            assertion.scope_id,
            assertion.subject_key,
            assertion.predicate,
            assertion.object_key,
            instant_text(assertion.valid_from),
            assertion.assertion_id,
        ]
    )
    return Interval(
        interval_id=interval_id,
        scope_id=assertion.scope_id,
        subject_key=assertion.subject_key,
        subject_type=assertion.subject_type,
        predicate=assertion.predicate,
        object_value=assertion.object_value,
        object_key=assertion.object_key,
        valid_from=assertion.valid_from,
        valid_to=valid_to,
        assertion_id=assertion.assertion_id,
        supporting_assertion_ids=[assertion.assertion_id],
        source_refs=assertion.source_refs,
        origin=assertion.origin,
    )


def _add_support(interval: Interval, assertion: Assertion) -> Interval:
    if assertion.assertion_id in interval.supporting_assertion_ids:
        return interval
    seen_source_ids = {ref.source_id for ref in interval.source_refs}
    source_refs = list(interval.source_refs)
    for ref in assertion.source_refs:
        if ref.source_id not in seen_source_ids:
            source_refs.append(ref)
            seen_source_ids.add(ref.source_id)
    return interval.model_copy(
        update={
            "supporting_assertion_ids": [
                *interval.supporting_assertion_ids,
                assertion.assertion_id,
            ],
            "source_refs": source_refs,
        }
    )


def project_assertions(
    assertions: Iterable[Assertion],
    profile: SchemaProfile,
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> tuple[list[Interval], list[ProjectionStats]]:
    grouped: dict[tuple[str, str, str], list[Assertion]] = defaultdict(list)
    scopes: set[str] = set()
    for assertion in assertions:
        definition = profile.predicate(assertion.predicate)
        if definition.subject != assertion.subject_type:
            raise ValueError("predicate is not registered for assertion subject type")
        grouped[
            (assertion.scope_id, assertion.subject_key, assertion.predicate)
        ].append(assertion)
        scopes.add(assertion.scope_id)

    intervals: list[Interval] = []
    conflict_totals: dict[tuple[str, str], int] = defaultdict(int)
    for (scope_id, _, predicate_name), items in sorted(grouped.items()):
        definition = profile.predicate(predicate_name)
        if definition.cardinality == Cardinality.SINGLE:
            if predicate_name == PART_OF:
                projected, conflicts = _project_elected_single(
                    items,
                    human_edge_weight=human_edge_weight,
                )
            else:
                projected, conflicts = _project_single(items)
            conflict_totals[(scope_id, predicate_name)] += conflicts
        elif definition.cardinality == Cardinality.SET:
            projected = (
                _project_elected_set(
                    items,
                    human_edge_weight=human_edge_weight,
                )
                if predicate_name == GATHERS
                else _project_set(items)
            )
        else:
            projected = _project_append(
                items,
                retractable=definition.retractable,
            )
        intervals.extend(projected)

    stats = [
        ProjectionStats(
            scope_id=scope,
            predicate=predicate.name,
            conflicts_resolved=conflict_totals[(scope, predicate.name)],
        )
        for scope in sorted(scopes)
        for predicate in profile.predicates
    ]
    return sorted(intervals, key=_interval_sort), stats


def _project_single(items: list[Assertion]) -> tuple[list[Interval], int]:
    by_time: dict[object, list[Assertion]] = defaultdict(list)
    for item in items:
        by_time[item.valid_from].append(item)
    result: list[Interval] = []
    active: Interval | None = None
    conflicts = 0
    for instant in sorted(by_time):
        events = by_time[instant]
        asserted_keys = {
            item.object_key for item in events if item.operation == Operation.ASSERT
        }
        conflicts += max(0, len(asserted_keys) - 1)
        winner = max(events, key=_rank)
        if winner.operation == Operation.RETRACT:
            if active is not None:
                result.append(active.model_copy(update={"valid_to": instant}))
                active = None
            continue
        supporting = sorted(
            (
                event
                for event in events
                if event.operation == Operation.ASSERT
                and event.object_key == winner.object_key
            ),
            key=_rank,
        )
        if active is not None and active.object_key == winner.object_key:
            for assertion in supporting:
                active = _add_support(active, assertion)
            continue
        if active is not None:
            result.append(active.model_copy(update={"valid_to": instant}))
        active = _interval(winner)
        for assertion in supporting:
            active = _add_support(active, assertion)
    if active is not None:
        result.append(active)
    return result, conflicts


def _project_elected_single(
    items: list[Assertion],
    *,
    human_edge_weight: int,
) -> tuple[list[Interval], int]:
    """Project weighted ``part_of`` history without consulting mutable state."""

    asserted_by_time: dict[object, set[str]] = defaultdict(set)
    for item in items:
        if item.operation == Operation.ASSERT:
            asserted_by_time[item.valid_from].add(item.object_key)
    conflicts = sum(max(0, len(keys) - 1) for keys in asserted_by_time.values())

    result: list[Interval] = []
    active: Interval | None = None
    for step in election_history(items, human_edge_weight=human_edge_weight):
        election = step.election
        if election is None:
            if active is not None:
                result.append(active.model_copy(update={"valid_to": step.instant}))
                active = None
            continue
        winner = election.winner
        if active is not None and active.object_key == winner.object_key:
            active = _election_interval(winner, valid_from=active.valid_from)
            continue
        if active is not None:
            result.append(active.model_copy(update={"valid_to": step.instant}))
        active = _election_interval(winner, valid_from=step.instant)
    if active is not None:
        result.append(active)
    return result, conflicts


def _project_elected_set(
    items: list[Assertion],
    *,
    human_edge_weight: int,
) -> list[Interval]:
    """Project independent weighted membership slots for ``gathers``."""

    by_slot: dict[str, list[Assertion]] = defaultdict(list)
    field_wide: list[Assertion] = []
    for item in items:
        if item.object_key == FIELD_WIDE_RETRACT:
            field_wide.append(item)
            continue
        by_slot[item.object_key].append(item)
    result: list[Interval] = []
    for slot in sorted(by_slot, key=lambda value: value.encode("utf-8")):
        active: Interval | None = None
        for step in election_history(
            [*by_slot[slot], *field_wide],
            human_edge_weight=human_edge_weight,
        ):
            election = step.election
            if election is None:
                if active is not None:
                    result.append(
                        active.model_copy(update={"valid_to": step.instant})
                    )
                    active = None
                continue
            winner = election.winner
            if active is not None and active.object_key == winner.object_key:
                active = _election_interval(
                    winner,
                    valid_from=active.valid_from,
                )
                continue
            if active is not None:
                result.append(active.model_copy(update={"valid_to": step.instant}))
            active = _election_interval(winner, valid_from=step.instant)
        if active is not None:
            result.append(active)
    return sorted(result, key=_interval_sort)


def _election_interval(
    winner: ElectionCandidate,
    *,
    valid_from: datetime,
) -> Interval:
    base = winner.latest_assertion
    source_refs = []
    seen_source_ids: set[str] = set()
    for assertion in winner.contributing_assertions:
        for source_ref in assertion.source_refs:
            if source_ref.source_id in seen_source_ids:
                continue
            source_refs.append(source_ref)
            seen_source_ids.add(source_ref.source_id)
    interval_id = stable_hash(
        [
            base.scope_id,
            base.subject_key,
            base.predicate,
            base.object_key,
            instant_text(valid_from),
            base.assertion_id,
        ]
    )
    return Interval(
        interval_id=interval_id,
        scope_id=base.scope_id,
        subject_key=base.subject_key,
        subject_type=base.subject_type,
        predicate=base.predicate,
        object_value=base.object_value,
        object_key=winner.object_key,
        valid_from=valid_from,
        valid_to=None,
        assertion_id=base.assertion_id,
        supporting_assertion_ids=[
            item.assertion_id for item in winner.contributing_assertions
        ],
        source_refs=source_refs,
        origin=base.origin,
    )


def _project_set(items: list[Assertion]) -> list[Interval]:
    by_time: dict[object, list[Assertion]] = defaultdict(list)
    for item in items:
        by_time[item.valid_from].append(item)
    active: dict[str, Interval] = {}
    result: list[Interval] = []
    for instant in sorted(by_time):
        events = sorted(by_time[instant], key=_rank)
        for event in events:
            if event.operation == Operation.RETRACT:
                keys = (
                    list(active)
                    if event.object_key == FIELD_WIDE_RETRACT
                    else [event.object_key]
                )
                for key in keys:
                    if key in active:
                        result.append(
                            active.pop(key).model_copy(update={"valid_to": instant})
                        )
            elif event.object_key not in active:
                active[event.object_key] = _interval(event)
            else:
                active[event.object_key] = _add_support(
                    active[event.object_key], event
                )
    result.extend(active.values())
    return sorted(result, key=_interval_sort)


def _project_append(
    items: list[Assertion], *, retractable: bool
) -> list[Interval]:
    if not retractable:
        return [
            _interval(item, item.valid_from)
            for item in sorted(items, key=_rank)
            if item.operation == Operation.ASSERT
        ]
    active: list[Interval] = []
    for item in sorted(items, key=_rank):
        if item.operation == Operation.ASSERT:
            active.append(_interval(item, item.valid_from))
            continue
        if item.object_key == FIELD_WIDE_RETRACT:
            active = []
        else:
            active = [
                interval
                for interval in active
                if interval.object_key != item.object_key
            ]
    return active


def _interval_sort(item: Interval) -> tuple:
    return (
        item.scope_id,
        item.subject_key,
        item.predicate,
        item.valid_from,
        item.object_key,
        item.interval_id,
    )
