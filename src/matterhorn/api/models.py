from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, RootModel

from matterhorn.contracts import (
    EvidenceRef,
    Message,
    Operation,
    Outcome,
    Participant,
    SourceRef,
    TaskGate,
    TaskStatus,
)
from matterhorn.contracts.models import StrictModel


class AddMessagesRequest(StrictModel):
    messages: list[Message]
    wait: bool = False


class RawIngestRequest(StrictModel):
    text: str = Field(min_length=1, max_length=200_000)
    wait: bool = False


class IngestResponse(StrictModel):
    input_format: Literal["chat", "messages", "email"]
    synthesized_timestamps: bool
    accepted: int
    task_id: str
    status: TaskStatus | None = None
    cards_produced: int = 0
    new_assertions: int = 0
    gate: TaskGate | None = None


class CardInput(StrictModel):
    card_id: str
    scope_id: str | None = None
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
    source_refs: list[SourceRef] = Field(min_length=1)
    cleared_fields: list[str] = Field(default_factory=list)
    subject_key: str | None = None
    thread_id: str | None = None


class AddCardsRequest(StrictModel):
    cards: list[CardInput]
    wait: bool = False


class CorrectionInput(StrictModel):
    subject_key: str
    subject_type: str
    predicate: str
    operation: Operation = Operation.ASSERT
    object_value: Any = None
    object_key: str | None = None
    valid_from: datetime
    source_refs: list[SourceRef] = Field(min_length=1)


class ValueResponse(StrictModel):
    subject_key: str
    predicate: str
    value: Any
    valid_from: str
    valid_to: str | None
    recorded_at: str
    assertion_id: str
    supporting_assertion_ids: list[str]
    source_ids: list[str]
    source_refs: list[EvidenceRef]
    evidence_status: str
    origin: str


class ValueListResponse(RootModel[list[ValueResponse]]):
    pass


class SubjectResponse(StrictModel):
    subject_key: str
    subject_type: str
    title: str
    current: dict[str, Any]


class SubjectListResponse(RootModel[list[SubjectResponse]]):
    pass


class MatterResponse(StrictModel):
    title: str
    status: Any = None
    owners: list[Any]
    participants: list[Any]
    blocked_by: list[Any]
    next_step: Any = None
    due: Any = None
    subject_key: str


class MatterListResponse(RootModel[list[MatterResponse]]):
    pass


class ScopeListResponse(RootModel[list[str]]):
    pass


class MatterDetailResponse(StrictModel):
    subject_key: str
    subject_type: str
    title: str
    current: list[ValueResponse]
    timeline: dict[str, list[ValueResponse]]


class ChatHistoryMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8_000)


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=8_000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=20)


class ChatEvidence(StrictModel):
    name: str
    args: dict[str, Any]
    source_ids: list[str]
    subject_keys: list[str]
    error: str | None = None


class ChatResponse(StrictModel):
    answer: str
    evidence: list[ChatEvidence]
    tool_calls: int


class ConsoleConfigResponse(StrictModel):
    chat_enabled: bool
    chat_provider: str | None = None


class HealthResponse(StrictModel):
    status: str


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
