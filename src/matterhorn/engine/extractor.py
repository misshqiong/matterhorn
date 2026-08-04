from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

from matterhorn.canonical import (
    as_utc,
    derive_assertion_id,
    object_key,
)
from matterhorn.contracts import (
    FIELD_WIDE_RETRACT,
    Assertion,
    EpisodeCard,
    ExtractionMode,
    Operation,
    Origin,
    PredicateDefinition,
    RetractGuard,
    SchemaProfile,
)


@dataclass(frozen=True)
class FieldObservation:
    values: tuple[Any, ...]
    retract: bool

    @property
    def updates_projection(self) -> bool:
        return bool(self.values) or self.retract


def observe_field(card: EpisodeCard, predicate: PredicateDefinition) -> FieldObservation:
    """The single guard used for both RETRACT emission and projection updates."""
    field = predicate.source_field
    if field is None:
        return FieldObservation((), False)
    explicitly_cleared = field in card.cleared_fields
    raw = None if explicitly_cleared else getattr(card, field)
    values = tuple(_extract_values(raw, predicate))
    if values:
        return FieldObservation(values, False)
    if predicate.retract_guard == RetractGuard.never:
        return FieldObservation((), False)
    if predicate.retract_guard == RetractGuard.explicit:
        return FieldObservation((), explicitly_cleared)
    if predicate.retract_guard == RetractGuard.implicit:
        return FieldObservation((), True)
    return FieldObservation((), False)


def _extract_values(raw: Any, predicate: PredicateDefinition) -> list[Any]:
    if raw is None:
        return []
    if predicate.extraction_rule == "scalar":
        return [raw]
    if predicate.extraction_rule == "participant_ids":
        allowed = set(predicate.role_filter)
        return [
            item.id
            for item in raw
            if not allowed or (item.role is not None and item.role in allowed)
        ]
    if predicate.extraction_rule == "list":
        return list(raw)
    raise ValueError(f"unsupported extraction_rule: {predicate.extraction_rule}")


def card_valid_from(card: EpisodeCard) -> datetime:
    if card.occurred_at is not None:
        return as_utc(card.occurred_at)
    return datetime.combine(card.date, time.min, tzinfo=UTC)


def extract_card(
    card: EpisodeCard,
    subject_key: str,
    subject_type: str,
    profile: SchemaProfile,
    recorded_at: datetime,
    *,
    origin: Origin = Origin.model,
) -> list[Assertion]:
    assertions: list[Assertion] = []
    valid_from = card_valid_from(card)
    recorded_at = as_utc(recorded_at)
    observation_id = card.card_id if card.thread_id is not None else None
    for predicate in profile.predicates:
        if (
            predicate.subject != subject_type
            or predicate.extraction == ExtractionMode.semantic
        ):
            continue
        observation = observe_field(card, predicate)
        if not observation.updates_projection:
            continue
        for value in observation.values:
            value_key = object_key(value)
            assertions.append(
                Assertion(
                    assertion_id=derive_assertion_id(
                        card.scope_id,
                        subject_key,
                        predicate.name,
                        Operation.ASSERT,
                        value_key,
                        valid_from,
                        card.source_refs,
                        observation_id,
                    ),
                    scope_id=card.scope_id,
                    subject_key=subject_key,
                    subject_type=subject_type,
                    predicate=predicate.name,
                    operation=Operation.ASSERT,
                    object_value=value,
                    object_key=value_key,
                    valid_from=valid_from,
                    recorded_at=recorded_at,
                    source_refs=card.source_refs,
                    origin=origin,
                    observation_id=observation_id,
                )
            )
        if observation.retract:
            assertions.append(
                Assertion(
                    assertion_id=derive_assertion_id(
                        card.scope_id,
                        subject_key,
                        predicate.name,
                        Operation.RETRACT,
                        FIELD_WIDE_RETRACT,
                        valid_from,
                        card.source_refs,
                        observation_id,
                    ),
                    scope_id=card.scope_id,
                    subject_key=subject_key,
                    subject_type=subject_type,
                    predicate=predicate.name,
                    operation=Operation.RETRACT,
                    object_value=None,
                    object_key=FIELD_WIDE_RETRACT,
                    valid_from=valid_from,
                    recorded_at=recorded_at,
                    source_refs=card.source_refs,
                    origin=origin,
                    observation_id=observation_id,
                )
            )
    return assertions
