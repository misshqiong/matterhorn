from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

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
    uri: str | None = None


class MessageSender(StrictModel):
    id: str = Field(min_length=1)
    name: str | None = None


class Message(StrictModel):
    """The minimal public message contract."""

    id: str = Field(min_length=1)
    sender: MessageSender
    text: str
    sent_at: datetime
    conversation_id: str | None = None
    conversation_label: str | None = None
    reply_to: str | None = None


class AuthorKind(str, Enum):
    human = "human"
    bot = "bot"
    app = "app"


class RecordAuthor(StrictModel):
    id: str
    display_name: str | None = None
    kind: AuthorKind


class RecordReaction(StrictModel):
    name: str
    count: int = Field(ge=0)
    author_ids: list[str] = Field(default_factory=list)

    @field_validator("author_ids")
    @classmethod
    def author_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("reaction author_ids MUST NOT contain duplicates")
        return value


class RecordAttachment(StrictModel):
    attachment_id: str
    kind: str
    title: str | None = None
    mime_type: str | None = None
    uri: str | None = None
    size: int | None = Field(default=None, ge=0)


class Record(StrictModel):
    """A provider-neutral, traceable communication observation."""

    record_id: str
    container_id: str
    thread_id: str | None = None
    sent_at: datetime
    author: RecordAuthor
    content: str
    uri: str | None = None
    reactions: list[RecordReaction] = Field(default_factory=list)
    attachments: list[RecordAttachment] = Field(default_factory=list)
    edited_at: datetime | None = None
    revoked_at: datetime | None = None
    kind: str = "message"
    subtype: str | None = None
    native_id: str | None = None
    workspace_id: str | None = None
    client_id: str | None = None
    parent_author_id: str | None = None
    broadcast: bool = False

    @model_validator(mode="after")
    def identity_is_namespaced_and_times_are_ordered(self) -> Record:
        prefix = f"{self.container_id}:"
        if not self.container_id or not self.record_id.startswith(prefix):
            raise ValueError(
                "record_id MUST be namespaced as '<container_id>:<native_id>'"
            )
        native_part = self.record_id[len(prefix) :]
        if not native_part:
            raise ValueError("record_id native component MUST be non-empty")
        if self.native_id is not None and native_part != self.native_id:
            raise ValueError("record_id native component MUST equal native_id")
        if self.thread_id is not None and not self.thread_id.startswith(prefix):
            raise ValueError("thread_id MUST be namespaced by container_id")
        if self.edited_at is not None and self.edited_at < self.sent_at:
            raise ValueError("edited_at MUST NOT precede sent_at")
        if self.revoked_at is not None and self.revoked_at < self.sent_at:
            raise ValueError("revoked_at MUST NOT precede sent_at")
        return self

    @property
    def matter_boundary(self) -> str:
        return self.thread_id or self.record_id

    def to_source_ref(self) -> SourceRef:
        return SourceRef(
            source_id=self.record_id,
            sent_at=self.sent_at,
            sender=self.author.display_name or self.author.id,
            excerpt=self.content or None,
            uri=self.uri,
        )


@dataclass(frozen=True)
class SubjectRecord:
    scope_id: str
    subject_key: str
    subject_type: str
    title: str
    normalized_title: str
    source_ids: frozenset[str]
    parent_subject_key: str | None = None
    thread_ids: frozenset[str] = frozenset()


class EvidenceStatus(str, Enum):
    active = "active"
    revoked = "revoked"


class EvidenceSummary(str, Enum):
    active = "active"
    partially_revoked = "partially_revoked"
    revoked = "revoked"


class EvidenceRef(StrictModel):
    source_id: str
    uri: str | None = None
    status: EvidenceStatus = EvidenceStatus.active
    revoked_at: datetime | None = None


class SyncPosition(StrictModel):
    scope_id: str
    container_id: str
    watermark: datetime | None = None
    cursor: str | None = None
    uid_watermark: int | None = Field(default=None, ge=0)


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
    thread_id: str | None = None

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


class SubjectAnchor(StrictModel):
    subject_key: str
    title: str
    status: str | None = None
    last_active_at: datetime | None = None


@runtime_checkable
class RecordExtractionReport(Protocol):
    @property
    def cards(self) -> list[EpisodeCard]: ...

    @property
    def rejection_counts(self) -> dict[str, int]: ...


@runtime_checkable
class RecordExtractor(Protocol):
    def extract(
        self,
        *,
        scope_id: str,
        records: list[Record],
        context: list[Record],
        batch_size: int,
        anchors: list[SubjectAnchor],
    ) -> RecordExtractionReport: ...


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


FIELD_WIDE_RETRACT = "*"


class Origin(str, Enum):
    model = "model"
    human = "human"


class HandleOrigin(str, Enum):
    human = "human"
    model = "model"
    system = "system"


class EventType(str, Enum):
    matter_created = "matter_created"
    status_changed = "status_changed"
    matter_completed = "matter_completed"
    blocked = "blocked"
    unblocked = "unblocked"
    decision_adopted = "decision_adopted"
    value_corrected = "value_corrected"
    subject_merged = "subject_merged"
    subject_unmerged = "subject_unmerged"


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


class HandleNormalization(StrictModel):
    strip_prefixes: list[str] = Field(default_factory=list)
    strip_leading_zeros: bool = False

    @field_validator("strip_prefixes")
    @classmethod
    def prefixes_are_non_empty_and_unique(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("handle normalization prefixes MUST be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("handle normalization prefixes MUST be unique")
        return value


class HandlePattern(StrictModel):
    handle_type: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    normalization: HandleNormalization = Field(default_factory=HandleNormalization)

    @field_validator("pattern")
    @classmethod
    def pattern_must_compile(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as error:
            raise ValueError(f"invalid handle pattern: {error}") from error
        return value


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
    adjudication_confidence_threshold: float = 0.6

    @field_validator("adjudication_confidence_threshold")
    @classmethod
    def adjudication_confidence_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError(
                "adjudication_confidence_threshold MUST be in [0, 1]"
            )
        return value


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
    handle_patterns: list[HandlePattern] = Field(default_factory=list)
    completion: CompletionDefinition | None = None
    semantic: SemanticDefinition = Field(default_factory=SemanticDefinition)

    @model_validator(mode="after")
    def profile_is_closed_and_consistent(self) -> SchemaProfile:
        subject_names = [item.type for item in self.subjects]
        predicate_names = [item.name for item in self.predicates]
        handle_types = [item.handle_type for item in self.handle_patterns]
        if not subject_names or len(subject_names) != len(set(subject_names)):
            raise ValueError("subject types MUST be non-empty and unique")
        if not predicate_names or len(predicate_names) != len(set(predicate_names)):
            raise ValueError("predicate names MUST be non-empty and unique")
        if len(handle_types) != len(set(handle_types)):
            raise ValueError("handle types MUST be unique")
        for subject in self.subjects:
            if subject.parent and subject.parent not in subject_names:
                raise ValueError(f"unknown parent subject: {subject.parent}")
        for predicate in self.predicates:
            if predicate.subject not in subject_names:
                raise ValueError(f"unknown predicate subject: {predicate.subject}")
            if (
                predicate.object == "subject"
                and predicate.cardinality != Cardinality.SINGLE
            ):
                raise ValueError("subject-reference predicates MUST be SINGLE")
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
    observation_id: str | None = None

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


class SubjectMerge(StrictModel):
    scope_id: str
    source_subject_key: str
    target_subject_key: str
    source_refs: list[SourceRef]
    valid_from: datetime

    @field_validator("source_refs")
    @classmethod
    def merge_sources_must_not_be_empty(cls, value: list[SourceRef]) -> list[SourceRef]:
        if not value:
            raise ValueError("subject merges MUST have source_refs")
        return value


class SubjectHandle(StrictModel):
    binding_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    subject_key: str = Field(min_length=1)
    handle_type: str = Field(min_length=1)
    handle_value: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    origin: HandleOrigin
    source_refs: list[SourceRef]
    bound_at: datetime
    revoked_at: datetime | None = None
    revocation_origin: HandleOrigin | None = None
    revocation_source_refs: list[SourceRef] = Field(default_factory=list)

    @field_validator("source_refs")
    @classmethod
    def binding_sources_must_not_be_empty(cls, value: list[SourceRef]) -> list[SourceRef]:
        if not value:
            raise ValueError("subject handle bindings MUST have source_refs")
        return value

    @model_validator(mode="after")
    def revocation_is_complete(self) -> SubjectHandle:
        fields = (
            self.revoked_at is not None,
            self.revocation_origin is not None,
            bool(self.revocation_source_refs),
        )
        if any(fields) and not all(fields):
            raise ValueError("subject handle revocation provenance MUST be complete")
        return self


class SignalStatus(str, Enum):
    open = "open"
    acked = "acked"


class Signal(StrictModel):
    scope_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    kind: Literal["mention_of_self", "machine_alert"]
    detected_at: datetime
    matched_text: str = Field(min_length=1)
    subject_key: str | None = None
    status: SignalStatus = SignalStatus.open
    acked_at: datetime | None = None

    @model_validator(mode="after")
    def acknowledgement_is_complete(self) -> Signal:
        if (self.status == SignalStatus.acked) != (self.acked_at is not None):
            raise ValueError("acked signals MUST carry acked_at and open signals MUST NOT")
        return self


class ReviewItem(StrictModel):
    scope_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    card_json: dict[str, Any]
    reasons: list[str]
    candidates_json: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    resolved_at: datetime | None = None
    resolution_json: dict[str, Any] | None = None

    @field_validator("reasons")
    @classmethod
    def reasons_must_not_be_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("review reasons MUST be non-empty")
        return value

    @model_validator(mode="after")
    def resolution_is_complete(self) -> ReviewItem:
        if (self.resolved_at is None) != (self.resolution_json is None):
            raise ValueError("review resolution state MUST be complete")
        return self


class ProjectionStats(StrictModel):
    scope_id: str
    predicate: str
    conflicts_resolved: int = 0


class GateStatistics(StrictModel):
    scope_id: str
    accepted: int = 0
    rejections: dict[str, int] = Field(default_factory=dict)
    unchanged_dropped: int = 0
    handle_conflicts: int = 0
    route_handle: int = 0
    route_thread: int = 0
    route_evidence: int = 0
    route_model: int = 0
    route_new: int = 0
    route_review: int = 0
    route_disagreements: int = 0


class TaskStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class TaskReceipt(StrictModel):
    accepted: int
    task_id: str


class TaskGate(StrictModel):
    accepted: int = 0
    rejected: dict[str, int] = Field(default_factory=dict)
    unchanged_dropped: int = 0
    handle_conflicts: int = 0
    route_handle: int = 0
    route_thread: int = 0
    route_evidence: int = 0
    route_model: int = 0
    route_new: int = 0
    route_review: int = 0
    route_disagreements: int = 0


class TaskResult(StrictModel):
    task_id: str
    status: TaskStatus
    cards_produced: int = 0
    new_assertions: int = 0
    unchanged_dropped: int = 0
    attempts: int = 0
    last_error: str | None = None
    gate: TaskGate = Field(default_factory=TaskGate)


class FlushReport(StrictModel):
    scope_id: str
    tasks_processed: int
    task_ids: list[str] = Field(default_factory=list)
    remaining: int


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


class AddRecordsReport(StrictModel):
    scope_id: str
    records_received: int
    records_processed: int
    records_skipped: int
    records_revoked: int
    cards_accepted: int
    cards_dropped: int
    drop_reasons: dict[str, int] = Field(default_factory=dict)
    card_ids: list[str] = Field(default_factory=list)
    assertions_emitted: int
    assertion_ids: list[str] = Field(default_factory=list)
    unchanged_dropped: int = 0
    handles_bound: int = 0
    handles_already_bound: int = 0
    handle_conflicts: int = 0
    route_handle: int = 0
    route_thread: int = 0
    route_evidence: int = 0
    route_model: int = 0
    route_new: int = 0
    route_review: int = 0
    route_disagreements: int = 0
    sync_positions: list[SyncPosition] = Field(default_factory=list)


class HandleBackfillReport(StrictModel):
    scope_id: str
    bound: int = 0
    skipped_conflict: int = 0
    already_bound: int = 0


class ChangeEvent(StrictModel):
    event_id: str
    event_type: EventType
    scope_id: str
    subject_key: str
    predicate: str
    old_value: Any = None
    new_value: Any = None
    valid_from: datetime
    recorded_at: datetime
    origin: Origin
    source_ids: list[str] = Field(default_factory=list)


class ExportSchemaProfile(StrictModel):
    id: str
    version: str


class ExportSubject(StrictModel):
    scope_id: str
    subject_key: str
    subject_type: str
    title: str
    normalized_title: str
    source_ids: list[str] = Field(default_factory=list)
    parent_subject_key: str | None = None
    thread_ids: list[str] = Field(default_factory=list)


class ExportSourceState(StrictModel):
    source_id: str
    uri: str | None = None
    revoked_at: datetime | None = None


class ExportEnvelope(StrictModel):
    format: Literal["matterhorn-scope-export"] = "matterhorn-scope-export"
    version: Literal[1] = 1
    scope_id: str
    schema_profile: ExportSchemaProfile
    subjects: list[ExportSubject]
    assertions: list[Assertion]
    source_states: list[ExportSourceState]
    events: list[ChangeEvent]
    merges: list[SubjectMerge] = Field(default_factory=list)


class ImportReport(StrictModel):
    scope_id: str
    subjects: int
    assertions: int
    events: int
    intervals: int
    memory_cards: int


class ReplayReport(StrictModel):
    scope_id: str
    intervals: int
    memory_cards: int
    events_emitted: int
