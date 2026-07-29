from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import Field, RootModel

from matterhorn.contracts import (
    EvidenceRef,
    Message,
    Operation,
    Outcome,
    Participant,
    SourceRef,
)
from matterhorn.contracts.models import StrictModel


class AddMessagesRequest(StrictModel):
    messages: list[Message]
    wait: bool = False


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


class HealthResponse(StrictModel):
    status: str


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
