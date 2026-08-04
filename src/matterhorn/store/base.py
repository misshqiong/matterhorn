from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Literal, Protocol

from matterhorn.contracts import (
    Assertion,
    ChangeEvent,
    EpisodeCard,
    EvidenceRef,
    GateStatistics,
    Interval,
    MemoryCard,
    ProjectionStats,
    Record,
    SourceRef,
    SubjectHandle,
    SubjectMerge,
    SubjectRecord,
    SyncPosition,
    TaskResult,
    TaskStatus,
)

MAX_TASK_ATTEMPTS = 5


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


def thread_safe_store(cls: type[Any]) -> type[Any]:
    """Serialize each concrete store's public API on its instance lock."""

    for name, method in vars(cls).items():
        if name.startswith("_") or name == "transaction" or not callable(method):
            continue
        setattr(cls, name, _locked(method))
    return cls


@dataclass(frozen=True)
class DistillQueueItem:
    scope_id: str
    card_id: str
    card: EpisodeCard
    subject_key: str
    subject_type: str
    attempt_count: int
    last_error: str | None


@dataclass(frozen=True)
class QueryValueRow:
    subject_key: str
    predicate: str
    value: Any
    valid_from: str
    valid_to: str | None
    recorded_at: str
    assertion_id: str
    supporting_assertion_ids: list[str]
    source_refs: list[SourceRef]
    origin: str

    @property
    def source_ids(self) -> list[str]:
        return [item.source_id for item in self.source_refs]


@dataclass(frozen=True)
class RecordObservationRow:
    scope_id: str
    record_id: str
    observation_hash: str
    container_id: str
    observed_at: str


@dataclass(frozen=True)
class QuerySubjectRow:
    subject_key: str
    subject_type: str
    title: str
    current: dict[str, Any]


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    scope_id: str
    kind: str
    payload: dict[str, Any]
    accepted: int
    created_at: str
    newest_message_at: str | None
    result: TaskResult


class Store(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...

    def close(self) -> None: ...

    def clear_scope(self, scope_id: str) -> None: ...

    def scope_exists(self, scope_id: str) -> bool: ...

    def list_scopes(self) -> list[str]: ...

    def card_payload_hash(self, scope_id: str, card_id: str) -> str | None: ...

    def mark_card(self, scope_id: str, card_id: str, payload_hash: str) -> None: ...

    def has_record_observation(
        self, scope_id: str, record_id: str, observation_hash: str
    ) -> bool: ...

    def mark_record_observation(
        self,
        scope_id: str,
        record_id: str,
        observation_hash: str,
        container_id: str,
        observed_at: datetime,
    ) -> None: ...

    def record_observations(self, scope_id: str) -> list[RecordObservationRow]: ...

    def stage_records(
        self,
        scope_id: str,
        records: list[Record],
        *,
        staged_at: datetime,
    ) -> None: ...

    def staged_records(
        self,
        scope_id: str,
        container_id: str,
        *,
        sent_at_from: datetime,
        sent_at_before: datetime,
        thread_id: str | None,
        exclude_record_ids: list[str],
    ) -> list[Record]: ...

    def purge_staged_records(self, scope_id: str, *, before: datetime) -> int: ...

    def observe_source(
        self,
        scope_id: str,
        source_ref: SourceRef,
        *,
        revoked_at: datetime | None = None,
    ) -> None: ...

    def source_states(
        self, scope_id: str, source_refs: list[SourceRef]
    ) -> list[EvidenceRef]: ...

    def source_metadata(self, scope_id: str) -> list[EvidenceRef]: ...

    def put_source_state(self, scope_id: str, source: EvidenceRef) -> None: ...

    def update_sync_position(
        self,
        scope_id: str,
        container_id: str,
        *,
        watermark: datetime,
        cursor: str | None,
    ) -> None: ...

    def update_mail_sync_position(
        self,
        scope_id: str,
        container_id: str,
        *,
        uid_watermark: int,
        uidvalidity: str,
        fallback_watermark: datetime,
    ) -> None: ...

    def delete_sync_position(self, scope_id: str, container_id: str) -> bool: ...

    def sync_positions(self, scope_id: str) -> list[SyncPosition]: ...

    def assertions(self, scope_id: str) -> list[Assertion]: ...

    def subjects(self, scope_id: str) -> list[SubjectRecord]: ...

    def upsert_subject(self, subject: SubjectRecord) -> None: ...

    def subject_merges(self, scope_id: str) -> list[SubjectMerge]: ...

    def subject_handle_bindings(self, scope_id: str) -> list[SubjectHandle]: ...

    def active_subject_handles(
        self,
        scope_id: str,
        *,
        handle_type: str | None = None,
        normalized_value: str | None = None,
    ) -> list[SubjectHandle]: ...

    def add_subject_handle(
        self, handle: SubjectHandle
    ) -> Literal["bound", "already_bound", "conflict"]: ...

    def revoke_subject_handle(
        self,
        scope_id: str,
        handle_type: str,
        normalized_value: str,
        *,
        revoked_at: datetime,
        revocation_origin: str,
        source_refs: list[SourceRef],
    ) -> SubjectHandle | None: ...

    def add_subject_merge(self, merge: SubjectMerge) -> None: ...

    def remove_subject_merge(
        self, scope_id: str, source_subject_key: str
    ) -> bool: ...

    def add_assertion(self, assertion: Assertion) -> bool: ...

    def intervals(self, scope_id: str) -> list[Interval]: ...

    def memory_cards(self, scope_id: str) -> list[MemoryCard]: ...

    def projection_stats(self, scope_id: str) -> list[ProjectionStats]: ...

    def enqueue_distill(
        self, card: EpisodeCard, *, subject_key: str, subject_type: str
    ) -> bool: ...

    def distill_queue(
        self, scope_id: str, limit: int | None = None
    ) -> list[DistillQueueItem]: ...

    def remove_distill_item(self, scope_id: str, card_id: str) -> None: ...

    def fail_distill_item(self, scope_id: str, card_id: str, error: str) -> None: ...

    def distill_queue_count(self, scope_id: str) -> int: ...

    def record_gate_report(
        self,
        scope_id: str,
        *,
        accepted: int,
        rejections: dict[str, int],
        handle_conflicts: int = 0,
    ) -> None: ...

    def gate_statistics(self, scope_id: str) -> GateStatistics: ...

    def create_task(
        self,
        *,
        task_id: str,
        scope_id: str,
        kind: str,
        payload: dict[str, Any],
        accepted: int,
        created_at: datetime,
        newest_message_at: datetime | None,
    ) -> bool: ...

    def task(self, task_id: str) -> TaskRow | None: ...

    def tasks(
        self, scope_id: str, *, status: TaskStatus | None = None
    ) -> list[TaskRow]: ...

    def flushable_tasks(
        self, scope_id: str, *, max_attempts: int = MAX_TASK_ATTEMPTS
    ) -> list[TaskRow]: ...

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        cards_produced: int = 0,
        new_assertions: int = 0,
        gate_accepted: int = 0,
        gate_rejected: dict[str, int] | None = None,
        handle_conflicts: int = 0,
        last_error: str | None = None,
    ) -> None: ...

    def quiet_scopes(
        self,
        cutoff: datetime,
        *,
        max_attempts: int = MAX_TASK_ATTEMPTS,
    ) -> list[str]: ...

    def pending_scopes(
        self, *, max_attempts: int = MAX_TASK_ATTEMPTS
    ) -> list[str]: ...

    def add_event(self, event: ChangeEvent) -> bool: ...

    def events(
        self, scope_id: str, *, since: datetime | None = None
    ) -> list[ChangeEvent]: ...

    def pending_webhook_events(
        self, webhook_url: str, *, limit: int = 100
    ) -> list[ChangeEvent]: ...

    def mark_webhook_delivered(
        self,
        webhook_url: str,
        event_ids: list[str],
        *,
        delivered_at: datetime,
    ) -> None: ...

    def query_current_values(
        self,
        scope_id: str,
        subject_key: str,
        predicate: str,
        *,
        append: bool,
    ) -> list[QueryValueRow]: ...

    def query_timeline_values(
        self, scope_id: str, subject_key: str, predicate: str
    ) -> list[QueryValueRow]: ...

    def query_values_at(
        self,
        scope_id: str,
        subject_key: str,
        predicate: str,
        instant: datetime,
        *,
        append: bool,
    ) -> list[QueryValueRow]: ...

    def query_subjects_by_object(
        self,
        scope_id: str,
        predicates: list[str],
        object_key: str,
    ) -> list[QuerySubjectRow]: ...

    def query_subjects_by_type(
        self, scope_id: str, subject_type: str
    ) -> list[QuerySubjectRow]: ...

    def query_completion_counts(
        self,
        scope_id: str,
        predicate: str | None,
        completed_object_keys: list[str],
    ) -> tuple[int, int]: ...

    def replace_projection(
        self,
        scope_id: str,
        intervals: list[Interval],
        cards: list[MemoryCard],
        stats: list[ProjectionStats],
    ) -> None: ...
