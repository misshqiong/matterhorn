from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import RootModel

from matterhorn.contracts import (
    AddRecordsReport,
    Correction,
    EpisodeCard,
    EvidenceRef,
    Record,
)
from matterhorn.contracts.models import StrictModel


class AddEpisodeCardsRequest(StrictModel):
    cards: list[EpisodeCard]
    scope_id: str | None = None


class MutationResponse(StrictModel):
    cards: int
    assertions_emitted: int
    assertion_ids: list[str]


class AddRecordsRequest(StrictModel):
    scope_id: str
    records: list[Record]
    cursors: dict[str, str] | None = None
    backfill: bool = False


class AddRecordsResponse(AddRecordsReport):
    pass


class PredicateRequest(StrictModel):
    scope_id: str
    subject_key: str
    predicate: str


class AtRequest(PredicateRequest):
    instant: datetime


class ByPersonRequest(StrictModel):
    scope_id: str
    person_id: str


class ListMattersRequest(StrictModel):
    scope_id: str


class CorrectRequest(StrictModel):
    correction: Correction


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


class HealthResponse(StrictModel):
    status: str


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
