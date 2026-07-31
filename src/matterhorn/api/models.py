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


class QuickMessageRequest(StrictModel):
    sender: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=8_000)
    sent_at: datetime | None = None


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


class SubjectMergeInput(StrictModel):
    source_subject_key: str
    target_subject_key: str
    source_refs: list[SourceRef] = Field(min_length=1)


class SubjectUnmergeInput(StrictModel):
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
    aliases: list[str] = Field(default_factory=list)


class MatterListResponse(RootModel[list[MatterResponse]]):
    pass


class UnifiedMatterResponse(MatterResponse):
    scope_id: str


class UnifiedMatterListResponse(RootModel[list[UnifiedMatterResponse]]):
    pass


class ScopeListResponse(RootModel[list[str]]):
    pass


class MatterDetailResponse(StrictModel):
    subject_key: str
    subject_type: str
    title: str
    aliases: list[str] = Field(default_factory=list)
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


class MailConfigRequest(StrictModel):
    account_id: str | None = Field(default=None, min_length=1, max_length=512)
    name: str | None = Field(default=None, min_length=1, max_length=512)
    provider: str
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    ssl: bool | None = None
    user: str = Field(alias="account", min_length=1, max_length=320)
    folder: str = Field(default="INBOX", min_length=1, max_length=512)
    interval: str = "off"
    initial_window: int = Field(default=50, ge=1)
    scope: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = None


class MailConfigResponse(StrictModel):
    account_id: str
    name: str | None = None
    provider: str
    host: str
    port: int
    ssl: bool
    account: str
    folder: str
    interval: str
    initial_window: int
    scope: str | None = None
    help_url: str | None = None
    auth_note: str | None = None


class MailSyncRequest(StrictModel):
    scope_id: str | None = Field(default=None, min_length=1, max_length=120)
    backfill: bool = False


class MailResetRequest(StrictModel):
    scope_id: str | None = Field(default=None, min_length=1, max_length=120)
    confirm: bool = False


class MailResetResponse(StrictModel):
    scope_id: str
    container_id: str
    position_deleted: bool
    next_sync: Literal["initial_window"]


class MailSyncResponse(StrictModel):
    scope_id: str
    account: str
    folder: str
    container_id: str
    pulled: int
    filtered: int
    filtered_by_reason: dict[str, int] = Field(default_factory=dict)
    parse_errors: int
    effective_window: int | None = None
    cards_produced: int
    new_assertions: int
    new_matters: int
    new_watermark: int
    uidvalidity: str
    previous_uidvalidity: str | None = None
    reset_detected: bool
    backfill: bool


class MailStatusResponse(StrictModel):
    configured: bool
    config: MailConfigResponse | None = None
    scope_id: str | None = None
    password_state: str
    last_sync_at: str | None = None
    last_run_at: str | None = None
    next_run_at: str | None = None
    syncing: bool
    uid_watermark: int | None = None
    uidvalidity: str | None = None
    last_report: MailSyncResponse | None = None
    error: str | None = None


class MailAccountsResponse(RootModel[list[MailStatusResponse]]):
    pass


class MailDeleteResponse(StrictModel):
    account_id: str
    removed: bool
    watermark_retained: bool
    message: str


class AIConfigRequest(StrictModel):
    provider: Literal["openai-compatible", "anthropic"]
    base_url: str = Field(min_length=1, max_length=2_000)
    model: str = Field(min_length=1, max_length=512)
    timeout: float = Field(default=60.0, gt=0)
    api_key: str | None = None


class AIConfigResponse(StrictModel):
    provider: Literal["openai-compatible", "anthropic"]
    base_url: str
    model: str
    timeout: float


class AIStatusResponse(StrictModel):
    configured: bool
    config: AIConfigResponse | None = None
    source: str | None = None
    api_key_state: str
    chat_enabled: bool


class AITestResponse(StrictModel):
    reachable: bool
    message: str


class ActivityEventResponse(StrictModel):
    event_id: str
    event_type: str
    scope_id: str
    subject_key: str
    matter_title: str
    predicate: str
    old_value: Any
    new_value: Any
    valid_from: datetime
    recorded_at: datetime
    origin: str
    source_ids: list[str]


class ScopeConnectionResponse(StrictModel):
    scope_id: str
    last_ingestion_at: datetime | None = None
    message_count: int = Field(ge=0)


class ConnectionsResponse(StrictModel):
    mail: MailStatusResponse
    mail_accounts: list[MailStatusResponse] = Field(default_factory=list)
    ai: AIStatusResponse | None = None
    scopes: list[ScopeConnectionResponse]
    distill_queue_length: int = Field(ge=0)


class HealthResponse(StrictModel):
    status: str


class ErrorDetail(StrictModel):
    code: str
    message: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
