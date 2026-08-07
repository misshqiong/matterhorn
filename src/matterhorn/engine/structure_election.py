"""Pure weighted election for committed structure assertions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from matterhorn.canonical import object_key as canonical_object_key
from matterhorn.contracts import (
    FIELD_WIDE_RETRACT,
    Assertion,
    Operation,
    Origin,
)

PART_OF = "part_of"
GATHERS = "gathers"
WEIGHTED_STRUCTURE_PREDICATES = frozenset({PART_OF, GATHERS})
DEFAULT_HUMAN_EDGE_WEIGHT = 10


@dataclass(frozen=True)
class ElectionCandidate:
    """One target's live committed contributions at an election instant."""

    target: str
    object_key: str
    human_weight: int
    model_weight: int
    total_weight: int
    latest_assertion: Assertion
    contributing_assertions: tuple[Assertion, ...]

    def weights(self) -> dict[str, int]:
        return {
            "human": self.human_weight,
            "model": self.model_weight,
            "total": self.total_weight,
        }


@dataclass(frozen=True)
class StructureElection:
    """The elected target and complete candidate tally for one subject."""

    scope_id: str
    subject_key: str
    predicate: str
    elected_target: str
    elected_object_key: str
    candidates: tuple[ElectionCandidate, ...]

    def candidate(self, target: str) -> ElectionCandidate | None:
        return next((item for item in self.candidates if item.target == target), None)

    @property
    def winner(self) -> ElectionCandidate:
        return next(
            item
            for item in self.candidates
            if item.object_key == self.elected_object_key
        )


@dataclass(frozen=True)
class ElectionStep:
    """Election state after all committed assertions at one valid instant."""

    instant: datetime
    election: StructureElection | None


def validate_human_edge_weight(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        raise ValueError("human_edge_weight MUST be an integer >= 2")
    return value


def election_history(
    assertions: Iterable[Assertion],
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> tuple[ElectionStep, ...]:
    """Return deterministic election states for one structure-edge stream.

    ASSERT rows are immutable contributions. A RETRACT withdraws live
    contributions from the same origin, either for its object key or for the
    whole predicate when it carries ``*``. Store-level assertion-id
    idempotency makes every contribution a distinct gated admission.
    """

    weight = validate_human_edge_weight(human_edge_weight)
    items = list(assertions)
    if not items:
        return ()
    groups = {
        _election_stream_key(item)
        for item in items
        if not (
            item.predicate == GATHERS
            and item.object_key == FIELD_WIDE_RETRACT
        )
    }
    if not groups:
        return ()
    if len(groups) != 1:
        raise ValueError("election_history requires one assertion stream")
    scope_id, subject_key, predicate, _ = next(iter(groups))
    if predicate not in WEIGHTED_STRUCTURE_PREDICATES:
        raise ValueError("weighted election requires a structure predicate")

    by_time: dict[datetime, list[Assertion]] = defaultdict(list)
    for item in items:
        by_time[item.valid_from].append(item)

    live: dict[str, Assertion] = {}
    result: list[ElectionStep] = []
    for instant in sorted(by_time):
        for item in sorted(by_time[instant], key=_assertion_recency):
            if item.operation == Operation.ASSERT:
                live.setdefault(item.assertion_id, item)
                continue
            live = {
                assertion_id: contribution
                for assertion_id, contribution in live.items()
                if not _retracts(item, contribution)
            }
        result.append(
            ElectionStep(
                instant=instant,
                election=_elect(
                    scope_id,
                    subject_key,
                    predicate,
                    live.values(),
                    human_edge_weight=weight,
                ),
            )
        )
    return tuple(result)


def elected_part_of(
    assertions: Iterable[Assertion],
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> dict[tuple[str, str], StructureElection]:
    """Elect the current ``part_of`` target for every scope and subject."""

    grouped: dict[tuple[str, str], list[Assertion]] = defaultdict(list)
    for assertion in assertions:
        if assertion.predicate == PART_OF:
            grouped[(assertion.scope_id, assertion.subject_key)].append(assertion)
    result: dict[tuple[str, str], StructureElection] = {}
    for key in sorted(grouped, key=lambda item: (item[0].encode(), item[1].encode())):
        history = election_history(
            grouped[key], human_edge_weight=human_edge_weight
        )
        if history and history[-1].election is not None:
            result[key] = history[-1].election
    return result


def elected_gathers(
    assertions: Iterable[Assertion],
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> dict[tuple[str, str, str], StructureElection]:
    """Elect every independent ``gathers`` SET membership slot.

    A normal correction derives ``object_key`` from the canonical member
    reference, so each member occupies its own slot and the predicate remains
    a SET. Supplying the same explicit ``object_key`` for competing member
    references expresses a disputed slot; INV-22 then elects that slot exactly
    like the single ``part_of`` slot.
    """

    grouped: dict[tuple[str, str, str], list[Assertion]] = defaultdict(list)
    field_wide: dict[tuple[str, str], list[Assertion]] = defaultdict(list)
    for assertion in assertions:
        if assertion.predicate == GATHERS:
            if assertion.object_key == FIELD_WIDE_RETRACT:
                field_wide[(assertion.scope_id, assertion.subject_key)].append(
                    assertion
                )
                continue
            grouped[
                (assertion.scope_id, assertion.subject_key, assertion.object_key)
            ].append(assertion)
    result: dict[tuple[str, str, str], StructureElection] = {}
    for key in sorted(
        grouped,
        key=lambda item: (
            item[0].encode(),
            item[1].encode(),
            item[2].encode(),
        ),
    ):
        history = election_history(
            [*grouped[key], *field_wide.get((key[0], key[1]), [])],
            human_edge_weight=human_edge_weight,
        )
        if history and history[-1].election is not None:
            result[key] = history[-1].election
    return result


def _elect(
    scope_id: str,
    subject_key: str,
    predicate: str,
    assertions: Iterable[Assertion],
    *,
    human_edge_weight: int,
) -> StructureElection | None:
    by_object: dict[str, list[Assertion]] = defaultdict(list)
    for assertion in assertions:
        if isinstance(assertion.object_value, str) and assertion.object_value:
            candidate_key = (
                canonical_object_key(assertion.object_value)
                if predicate == GATHERS
                else assertion.object_key
            )
            by_object[candidate_key].append(assertion)
    if not by_object:
        return None

    candidates: list[ElectionCandidate] = []
    for candidate_object_key in sorted(
        by_object, key=lambda value: value.encode()
    ):
        contributions = tuple(
            sorted(by_object[candidate_object_key], key=_assertion_recency)
        )
        latest = contributions[-1]
        human_count = sum(item.origin == Origin.human for item in contributions)
        model_count = sum(item.origin == Origin.model for item in contributions)
        candidates.append(
            ElectionCandidate(
                target=str(latest.object_value),
                object_key=candidate_object_key,
                human_weight=human_count * human_edge_weight,
                model_weight=model_count,
                total_weight=human_count * human_edge_weight + model_count,
                latest_assertion=latest,
                contributing_assertions=contributions,
            )
        )
    winner = max(
        candidates,
        key=lambda item: (
            item.total_weight,
            _assertion_recency(item.latest_assertion),
        ),
    )
    return StructureElection(
        scope_id=scope_id,
        subject_key=subject_key,
        predicate=predicate,
        elected_target=winner.target,
        elected_object_key=winner.object_key,
        candidates=tuple(candidates),
    )


def _retracts(retract: Assertion, contribution: Assertion) -> bool:
    if retract.origin != contribution.origin:
        return False
    return (
        retract.object_key == FIELD_WIDE_RETRACT
        or retract.object_key == contribution.object_key
    )


def _election_stream_key(assertion: Assertion) -> tuple[str, str, str, str]:
    return (
        assertion.scope_id,
        assertion.subject_key,
        assertion.predicate,
        assertion.object_key if assertion.predicate == GATHERS else "",
    )


def _assertion_recency(assertion: Assertion) -> tuple[datetime, datetime, bytes]:
    return (
        assertion.valid_from,
        assertion.recorded_at,
        assertion.assertion_id.encode("utf-8"),
    )


__all__ = [
    "DEFAULT_HUMAN_EDGE_WEIGHT",
    "GATHERS",
    "PART_OF",
    "WEIGHTED_STRUCTURE_PREDICATES",
    "ElectionCandidate",
    "ElectionStep",
    "StructureElection",
    "elected_gathers",
    "elected_part_of",
    "election_history",
    "validate_human_edge_weight",
]
