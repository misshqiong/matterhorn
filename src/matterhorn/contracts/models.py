from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Participant(StrictModel):
    id: str
    display_name: str | None = None
    role: str | None = None


class SourceRef(StrictModel):
    source_id: str
    sent_at: datetime
    sender: str
    excerpt: str | None = None


class Outcome(StrictModel):
    type: str
    content: str


class EpisodeCard(StrictModel):
    card_id: str
    scope_id: str
    date: date
    title: str
    status: str | None = None
    participants: list[Participant] = Field(default_factory=list)
    progress: str | None = None
    blocker: str | None = None
    next_step: str | None = None
    due: datetime | None = None
    outcome: Outcome | None = None
    occurred_at: datetime | None = None
    last_active_at: datetime | None = None
    source_refs: list[SourceRef]
    cleared_fields: list[str] = Field(default_factory=list)
    subject_key: str | None = None

    @field_validator("source_refs")
    @classmethod
    def source_refs_must_not_be_empty(cls, value: list[SourceRef]) -> list[SourceRef]:
        if not value:
            raise ValueError("source_refs MUST be non-empty")
        return value

    @field_validator("cleared_fields")
    @classmethod
    def cleared_fields_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("cleared_fields MUST NOT contain duplicates")
        return value


class Cardinality(str, Enum):
    SINGLE = "SINGLE"
    SET = "SET"
    APPEND = "APPEND"


class ExtractionMode(str, Enum):
    deterministic = "deterministic"
    semantic = "semantic"


class RetractGuard(str, Enum):
    explicit = "explicit"
    never = "never"
    implicit = "implicit"


class Operation(str, Enum):
    ASSERT = "ASSERT"
    RETRACT = "RETRACT"


class Origin(str, Enum):
    model = "model"
    human = "human"


class SubjectDefinition(StrictModel):
    type: str
    parent: str | None = None
    primary: bool = False


class PredicateDefinition(StrictModel):
    name: str
    subject: str
    cardinality: Cardinality
    extraction: ExtractionMode
    retract_guard: RetractGuard = RetractGuard.explicit
    object: str = "string"
    source_field: str | None = None
    extraction_rule: str = "scalar"
    role_filter: list[str] = Field(default_factory=list)
    semantic_filter: str | None = None
    value_domain: list[Any] | None = None

    @model_validator(mode="after")
    def deterministic_has_source(self) -> PredicateDefinition:
        if self.extraction == ExtractionMode.deterministic and not self.source_field:
            raise ValueError("deterministic predicates require source_field")
        if self.semantic_filter not in (None, "conservative"):
            raise ValueError("semantic_filter MUST be null or conservative")
        return self


class MergeEvidence(StrictModel):
    min_shared_sources: int = 2
    or_share_ratio: float = 0.5

    @field_validator("min_shared_sources")
    @classmethod
    def positive_minimum(cls, value: int) -> int:
        if value < 1:
            raise ValueError("min_shared_sources MUST be >= 1")
        return value

    @field_validator("or_share_ratio")
    @classmethod
    def ratio_range(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("or_share_ratio MUST be in (0, 1]")
        return value


class IdentityDefinition(StrictModel):
    merge_evidence: MergeEvidence = Field(default_factory=MergeEvidence)


class CompletionDefinition(StrictModel):
    predicate: str
    completed_values: list[str]


class SemanticDefinition(StrictModel):
    conservative_confidence_threshold: float = 0.8

    @field_validator("conservative_confidence_threshold")
    @classmethod
    def confidence_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("conservative_confidence_threshold MUST be in [0, 1]")
        return value


class SchemaProfile(StrictModel):
    schema_id: str = Field(alias="schema", serialization_alias="schema")
    subjects: list[SubjectDefinition]
    predicates: list[PredicateDefinition]
    identity: IdentityDefinition = Field(default_factory=IdentityDefinition)
    completion: CompletionDefinition | None = None
    semantic: SemanticDefinition = Field(default_factory=SemanticDefinition)

    @model_validator(mode="after")
    def profile_is_closed_and_consistent(self) -> SchemaProfile:
        subject_names = [item.type for item in self.subjects]
        predicate_names = [item.name for item in self.predicates]
        if not subject_names or len(subject_names) != len(set(subject_names)):
            raise ValueError("subject types MUST be non-empty and unique")
        if not predicate_names or len(predicate_names) != len(set(predicate_names)):
            raise ValueError("predicate names MUST be non-empty and unique")
        for subject in self.subjects:
            if subject.parent and subject.parent not in subject_names:
                raise ValueError(f"unknown parent subject: {subject.parent}")
        for predicate in self.predicates:
            if predicate.subject not in subject_names:
                raise ValueError(f"unknown predicate subject: {predicate.subject}")
        if sum(1 for item in self.subjects if item.primary) > 1:
            raise ValueError("at most one subject may be primary")
        if self.completion and self.completion.predicate not in predicate_names:
            raise ValueError("completion predicate MUST be registered")
        return self

    @property
    def primary_subject(self) -> SubjectDefinition:
        return next((item for item in self.subjects if item.primary), self.subjects[0])

    def predicate(self, name: str) -> PredicateDefinition:
        for predicate in self.predicates:
            if predicate.name == name:
                return predicate
        raise ValueError(f"unregistered predicate: {name}")


class Assertion(StrictModel):
    assertion_id: str
    scope_id: str
    subject_key: str
    subject_type: str
    predicate: str
    operation: Operation
    object_value: Any = None
    object_key: str
    valid_from: datetime
    recorded_at: datetime
    source_refs: list[SourceRef]
    origin: Origin = Origin.model

    @field_validator("source_refs")
    @classmethod
    def assertion_sources_must_not_be_empty(cls, value: list[SourceRef]) -> list[SourceRef]:
        if not value:
            raise ValueError("assertions MUST have source_refs")
        return value


class Interval(StrictModel):
    interval_id: str
    scope_id: str
    subject_key: str
    subject_type: str
    predicate: str
    object_value: Any
    object_key: str
    valid_from: datetime
    valid_to: datetime | None
    assertion_id: str
    supporting_assertion_ids: list[str]
    source_refs: list[SourceRef]
    origin: Origin


class MemoryCard(StrictModel):
    scope_id: str
    subject_key: str
    subject_type: str
    title: str
    current: dict[str, Any]
    updated_at: datetime | None = None
    source_ids: list[str] = Field(default_factory=list)


class Correction(StrictModel):
    scope_id: str
    subject_key: str
    subject_type: str
    predicate: str
    operation: Operation = Operation.ASSERT
    object_value: Any = None
    object_key: str | None = None
    valid_from: datetime
    source_refs: list[SourceRef]

    @field_validator("source_refs")
    @classmethod
    def correction_sources_must_not_be_empty(cls, value: list[SourceRef]) -> list[SourceRef]:
        if not value:
            raise ValueError("corrections MUST have source_refs")
        return value


class ProjectionStats(StrictModel):
    scope_id: str
    predicate: str
    conflicts_resolved: int = 0


class GateStatistics(StrictModel):
    scope_id: str
    accepted: int = 0
    rejections: dict[str, int] = Field(default_factory=dict)


class DreamReport(StrictModel):
    scope_id: str
    queued: int
    processed: int
    failed: int
    accepted_candidates: int
    rejected_candidates: int
    new_assertions: int
    new_subjects: int
    remaining: int
