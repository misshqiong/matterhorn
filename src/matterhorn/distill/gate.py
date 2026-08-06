from __future__ import annotations

import json
from datetime import UTC, datetime, time
from enum import Enum
from typing import Any

from pydantic import Field, ValidationError, computed_field, field_validator

from matterhorn.canonical import (
    as_utc,
    derive_child_subject_key,
    normalize_title,
)
from matterhorn.contracts import (
    EpisodeCard,
    ExtractionMode,
    Operation,
    SchemaProfile,
    SourceRef,
    SubjectRecord,
)
from matterhorn.contracts.models import StrictModel
from matterhorn.distill.traceability import resolve_traceable_sources


class GateReason(str, Enum):
    UNPARSEABLE = "UNPARSEABLE"
    UNREGISTERED_PREDICATE = "UNREGISTERED_PREDICATE"
    NOT_SEMANTIC = "NOT_SEMANTIC"
    SUBJECT_TYPE_MISMATCH = "SUBJECT_TYPE_MISMATCH"
    UNKNOWN_SUBJECT = "UNKNOWN_SUBJECT"
    UNKNOWN_PARENT_SUBJECT = "UNKNOWN_PARENT_SUBJECT"
    INVALID_SUBJECT_PARENT = "INVALID_SUBJECT_PARENT"
    MISSING_SUBJECT_TITLE = "MISSING_SUBJECT_TITLE"
    NO_SOURCES = "NO_SOURCES"
    SOURCE_NOT_TRACEABLE = "SOURCE_NOT_TRACEABLE"
    VALUE_OUT_OF_DOMAIN = "VALUE_OUT_OF_DOMAIN"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    VALID_FROM_OUT_OF_WINDOW = "VALID_FROM_OUT_OF_WINDOW"


class SemanticCandidate(StrictModel):
    subject_key: str | None = None
    subject_type: str
    parent_subject_key: str | None = None
    subject_title: str | None = None
    predicate: str
    operation: Operation
    object_value: Any
    valid_from: datetime
    source_ids: list[str] = Field(default_factory=list)
    confidence: float

    @field_validator("confidence")
    @classmethod
    def confidence_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("confidence MUST be in [0, 1]")
        return value

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("source_ids MUST be unique")
        return value


class CandidateResponse(StrictModel):
    candidates: list[SemanticCandidate]


class AcceptedCandidate(StrictModel):
    candidate: SemanticCandidate
    source_refs: list[SourceRef]
    create_subject: bool = False


class GateRejection(StrictModel):
    reason: GateReason
    candidate: dict[str, Any] | None = None
    detail: str | None = None


class GateReport(StrictModel):
    accepted: list[AcceptedCandidate] = Field(default_factory=list)
    rejections: list[GateRejection] = Field(default_factory=list)

    @computed_field
    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @computed_field
    @property
    def rejected_count(self) -> int:
        return len(self.rejections)

    @computed_field
    @property
    def rejection_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for rejection in self.rejections:
            result[rejection.reason.value] = result.get(rejection.reason.value, 0) + 1
        return result


def validate_response(
    raw: str,
    *,
    card: EpisodeCard,
    profile: SchemaProfile,
    subjects: list[SubjectRecord],
) -> GateReport:
    try:
        decoded = json.loads(raw)
        response = CandidateResponse.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        return GateReport(
            rejections=[
                GateRejection(
                    reason=GateReason.UNPARSEABLE,
                    detail=str(error),
                )
            ]
        )

    accepted: list[AcceptedCandidate] = []
    rejected: list[GateRejection] = []
    known_subjects = {
        (subject.subject_key, subject.subject_type): subject for subject in subjects
    }
    subjects_by_key = {subject.subject_key: subject for subject in subjects}
    subject_definitions = {subject.type: subject for subject in profile.subjects}

    for candidate in response.candidates:
        dumped = candidate.model_dump(mode="json")
        try:
            definition = profile.predicate(candidate.predicate)
        except ValueError:
            rejected.append(_reject(GateReason.UNREGISTERED_PREDICATE, dumped))
            continue
        if definition.extraction != ExtractionMode.semantic:
            rejected.append(_reject(GateReason.NOT_SEMANTIC, dumped))
            continue
        if candidate.predicate in ("part_of", "spawned_from"):
            # Dream sees one card, never the tree: structure edges are owned
            # by theme convergence (§28) and the unified loop (§26).
            rejected.append(_reject(GateReason.NOT_SEMANTIC, dumped))
            continue
        if definition.subject != candidate.subject_type:
            rejected.append(_reject(GateReason.SUBJECT_TYPE_MISMATCH, dumped))
            continue
        create_subject = False
        creation_requested = (
            candidate.parent_subject_key is not None
            or candidate.subject_title is not None
        )
        if creation_requested:
            parent = (
                subjects_by_key.get(candidate.parent_subject_key)
                if candidate.parent_subject_key is not None
                else None
            )
            if parent is None:
                rejected.append(_reject(GateReason.UNKNOWN_PARENT_SUBJECT, dumped))
                continue
            requested_definition = subject_definitions[candidate.subject_type]
            if (
                requested_definition.type == profile.primary_subject.type
                or requested_definition.parent != parent.subject_type
            ):
                rejected.append(_reject(GateReason.INVALID_SUBJECT_PARENT, dumped))
                continue
            if not candidate.subject_title or not normalize_title(candidate.subject_title):
                rejected.append(_reject(GateReason.MISSING_SUBJECT_TITLE, dumped))
                continue
            derived_key = derive_child_subject_key(
                card.scope_id,
                parent.subject_key,
                candidate.subject_type,
                candidate.subject_title,
            )
            existing_child = subjects_by_key.get(derived_key)
            if existing_child is not None and (
                existing_child.subject_type != candidate.subject_type
                or existing_child.parent_subject_key != parent.subject_key
            ):
                rejected.append(_reject(GateReason.INVALID_SUBJECT_PARENT, dumped))
                continue
            candidate = candidate.model_copy(update={"subject_key": derived_key})
            create_subject = existing_child is None
        elif (
            candidate.subject_key is None
            or (candidate.subject_key, candidate.subject_type) not in known_subjects
        ):
            rejected.append(_reject(GateReason.UNKNOWN_SUBJECT, dumped))
            continue
        evidence = resolve_traceable_sources(candidate.source_ids, card.source_refs)
        if evidence.failure is not None:
            rejected.append(_reject(GateReason(evidence.failure.value), dumped))
            continue
        if (
            candidate.operation == Operation.ASSERT
            and not definition.value_is_in_domain(candidate.object_value)
        ):
            rejected.append(_reject(GateReason.VALUE_OUT_OF_DOMAIN, dumped))
            continue
        if (
            definition.semantic_filter == "conservative"
            and candidate.confidence
            < profile.semantic.conservative_confidence_threshold
        ):
            rejected.append(_reject(GateReason.LOW_CONFIDENCE, dumped))
            continue
        if not _inside_card_window(candidate.valid_from, card):
            rejected.append(_reject(GateReason.VALID_FROM_OUT_OF_WINDOW, dumped))
            continue
        accepted.append(
            AcceptedCandidate(
                candidate=candidate,
                source_refs=evidence.source_refs,
                create_subject=create_subject,
            )
        )
    return GateReport(accepted=accepted, rejections=rejected)


def _reject(reason: GateReason, candidate: dict[str, Any]) -> GateRejection:
    return GateRejection(reason=reason, candidate=candidate)


def _inside_card_window(value: datetime, card: EpisodeCard) -> bool:
    start = (
        as_utc(card.occurred_at)
        if card.occurred_at is not None
        else datetime.combine(card.date, time.min, tzinfo=UTC)
    )
    day_end = datetime.combine(card.date, time.max, tzinfo=UTC)
    end = as_utc(card.last_active_at) if card.last_active_at else max(start, day_end)
    return start <= as_utc(value) <= end
