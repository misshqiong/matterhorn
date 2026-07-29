from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from matterhorn.contracts import (
    Assertion,
    EpisodeCard,
    EvidenceRef,
    GateStatistics,
    Interval,
    MemoryCard,
    ProjectionStats,
    SourceRef,
    SyncPosition,
    TaskResult,
    TaskStatus,
)
from matterhorn.engine.identity import SubjectRecord


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

    def update_sync_position(
        self,
        scope_id: str,
        container_id: str,
        *,
        watermark: datetime,
        cursor: str | None,
    ) -> None: ...

    def sync_positions(self, scope_id: str) -> list[SyncPosition]: ...

    def assertions(self, scope_id: str) -> list[Assertion]: ...

    def subjects(self, scope_id: str) -> list[SubjectRecord]: ...

    def upsert_subject(self, subject: SubjectRecord) -> None: ...

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
        self, scope_id: str, *, accepted: int, rejections: dict[str, int]
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

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        cards_produced: int = 0,
        new_assertions: int = 0,
        gate_accepted: int = 0,
        gate_rejected: dict[str, int] | None = None,
    ) -> None: ...

    def quiet_scopes(self, cutoff: datetime) -> list[str]: ...

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
