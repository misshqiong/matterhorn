from __future__ import annotations

import json
import math
import re
import warnings
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from matterhorn.canonical import (
    as_utc,
    derive_assertion_id,
    instant_text,
    normalize_title,
    object_key,
    stable_hash,
)
from matterhorn.contracts import (
    FIELD_WIDE_RETRACT,
    AddRecordsReport,
    Assertion,
    Cardinality,
    ChangeEvent,
    Correction,
    DreamReport,
    EpisodeCard,
    EventType,
    EvidenceRef,
    EvidenceStatus,
    ExportEnvelope,
    ExportSchemaProfile,
    ExportSourceState,
    ExportSubject,
    FlushReport,
    HandleBackfillReport,
    HandleOrigin,
    ImportReport,
    Message,
    Operation,
    Origin,
    Record,
    RecordExtractor,
    ReplayReport,
    ReviewItem,
    SchemaProfile,
    Signal,
    SourceRef,
    SubjectAnchor,
    SubjectHandle,
    SubjectMerge,
    SubjectRecord,
    TaskReceipt,
    TaskResult,
    TaskStatus,
)
from matterhorn.contracts.schema import resolve_schema
from matterhorn.distill import LlmGateway, NullGateway, build_prompt, validate_response
from matterhorn.distill.traceability import restore_source_aliases
from matterhorn.engine.events import derive_change_events
from matterhorn.engine.extractor import extract_card
from matterhorn.engine.goal_graph import (
    DECISION,
    PART_OF,
    MatterGraph,
    StructureRejection,
    canonicalize_graph_assertions,
    project_goal_graph,
    structure_rejection,
)
from matterhorn.engine.goal_graph import (
    matter_graph as project_matter_graph,
)
from matterhorn.engine.handles import (
    matches_handle_pattern,
    normalize_handle,
    scan_handles,
)
from matterhorn.engine.identity import (
    attach_subject,
    evidence_match,
    new_subject,
    resolve_subject,
    thread_match,
)
from matterhorn.engine.materializer import materialize
from matterhorn.engine.routing import (
    AdjudicationCandidate,
    build_adjudication_prompt,
    candidate_score,
    gate_adjudication,
)
from matterhorn.engine.signals import (
    DEFAULT_ALERT_KEYWORDS,
    DEFAULT_HOT_MIN_AUTHORS,
    DEFAULT_HOT_MIN_MESSAGES,
    DEFAULT_IDENTITY_HANDLES,
    DEFAULT_MACHINE_SENDERS,
    SignalConfig,
    best_token_match,
    configured_signal_config,
    first_pattern_match,
)
from matterhorn.errors import (
    ImportRefusedError,
    ResourceNotFoundError,
    ReviewConflictError,
    SubjectHandleConflictError,
    SubjectMergeConflictError,
)
from matterhorn.projection import project_assertions
from matterhorn.query import QueryService
from matterhorn.store import SQLiteStore, Store
from matterhorn.store.base import MAX_TASK_ATTEMPTS, ROUTE_COUNTER_NAMES

Clock = Callable[[], datetime]
DEFAULT_MAX_ANCHORS = 40
DEFAULT_MIN_BATCH_MESSAGES = 1
DEFAULT_STAGING_RETENTION_DAYS = 7
DEFAULT_MAX_BATCH_DELAY_MINUTES = 5
DEFAULT_CONTEXT_MAX_RECORDS = 20
DEFAULT_CONTEXT_MAX_CHARS = 4000
SUBJECT_MERGE_PREDICATE = "subject_merge"
_TASK_ERROR_SECRET_MARKER = re.compile(
    r"(?i)\b(?:authorization|proxy-authorization|x-api-key|api[ _-]?key|"
    r"access[ _-]?token|refresh[ _-]?token|password|secret|bearer)\b"
)
_TASK_ERROR_LONG_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_.-]{24,}(?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class Matter:
    title: str
    status: Any
    owners: list[Any]
    participants: list[Any]
    blocked_by: list[Any]
    next_step: Any
    due: Any
    subject_key: str
    aliases: list[str]
    updated_at: datetime | None
    owners_display: list[Any] | None = None
    participants_display: list[Any] | None = None
    sources_display: list[str] | None = None
    progress: str | None = None
    descendants_total: int = 0
    descendants_completed: int = 0
    descendants_blocked: int = 0
    bubbled_blockers: list[dict[str, Any]] | None = None
    latest_activity: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "status": self.status,
            "progress": self.progress,
            "owners": self.owners,
            "participants": self.participants,
            "blocked_by": self.blocked_by,
            "next_step": self.next_step,
            "due": self.due,
            "subject_key": self.subject_key,
            "owners_display": self.owners_display or self.owners,
            "participants_display": self.participants_display or self.participants,
            "sources_display": self.sources_display or [],
            "aliases": self.aliases,
            "updated_at": self.updated_at,
            "descendants_total": self.descendants_total,
            "descendants_completed": self.descendants_completed,
            "descendants_blocked": self.descendants_blocked,
            "bubbled_blockers": self.bubbled_blockers or [],
            "latest_activity": self.latest_activity,
        }


@dataclass(frozen=True)
class RelatedMatter:
    scope_id: str
    subject_key: str
    title: str
    via: str

    def to_dict(self) -> dict[str, str]:
        return {
            "scope_id": self.scope_id,
            "subject_key": self.subject_key,
            "title": self.title,
            "via": self.via,
        }


@dataclass(frozen=True)
class _ScopeReadBundle:
    """One scope's read-side state, loaded once per wall/brief request."""

    subjects: list[Any]
    canonical_assertions: list[Assertion]
    graph: Any | None


@dataclass
class _HandleCounts:
    bound: int = 0
    already_bound: int = 0
    conflicts: int = 0

    def add(self, other: _HandleCounts) -> None:
        self.bound += other.bound
        self.already_bound += other.already_bound
        self.conflicts += other.conflicts


@dataclass
class _AdmissionCounts:
    unchanged_dropped: int = 0

    def add(self, other: _AdmissionCounts) -> None:
        self.unchanged_dropped += other.unchanged_dropped


@dataclass
class _RouteCounts:
    route_handle: int = 0
    route_thread: int = 0
    route_evidence: int = 0
    route_model: int = 0
    route_new: int = 0
    route_review: int = 0
    route_disagreements: int = 0

    def add(self, other: _RouteCounts) -> None:
        for name in ROUTE_COUNTER_NAMES:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in ROUTE_COUNTER_NAMES}


@dataclass(frozen=True)
class _RoutePlan:
    route: str
    subject_key: str | None = None
    candidates: tuple[AdjudicationCandidate, ...] = ()
    reasons: tuple[str, ...] = ()
    handle_conflicts: int = 0
    disagreement: bool = False
    duplicate: bool = False


class Engine:
    DEFAULT_STAGING_RETENTION_DAYS = DEFAULT_STAGING_RETENTION_DAYS
    DEFAULT_MAX_BATCH_DELAY_MINUTES = DEFAULT_MAX_BATCH_DELAY_MINUTES
    DEFAULT_MIN_BATCH_MESSAGES = DEFAULT_MIN_BATCH_MESSAGES
    DEFAULT_CONTEXT_MAX_RECORDS = DEFAULT_CONTEXT_MAX_RECORDS
    DEFAULT_CONTEXT_MAX_CHARS = DEFAULT_CONTEXT_MAX_CHARS
    DEFAULT_IDENTITY_HANDLES = DEFAULT_IDENTITY_HANDLES
    DEFAULT_MACHINE_SENDERS = DEFAULT_MACHINE_SENDERS
    DEFAULT_ALERT_KEYWORDS = DEFAULT_ALERT_KEYWORDS
    DEFAULT_HOT_MIN_AUTHORS = DEFAULT_HOT_MIN_AUTHORS
    DEFAULT_HOT_MIN_MESSAGES = DEFAULT_HOT_MIN_MESSAGES

    def __init__(
        self,
        store: str | Path | Store,
        schema: str | Path | SchemaProfile = "org-matters/v1",
        *,
        clock: Clock | Iterable[datetime] | None = None,
        llm: LlmGateway | None = None,
        gateway: LlmGateway | None = None,
        extractor: RecordExtractor | None = None,
        staging_retention_days: float = DEFAULT_STAGING_RETENTION_DAYS,
        max_batch_delay_minutes: float = DEFAULT_MAX_BATCH_DELAY_MINUTES,
        min_batch_messages: int = DEFAULT_MIN_BATCH_MESSAGES,
        identity_handles: list[str] | tuple[str, ...] | None = None,
        machine_senders: list[str] | tuple[str, ...] | None = None,
        alert_keywords: list[str] | tuple[str, ...] | None = None,
        hot_min_authors: int = DEFAULT_HOT_MIN_AUTHORS,
        hot_min_messages: int = DEFAULT_HOT_MIN_MESSAGES,
        unified_loop: bool = False,
    ):
        self.store = _resolve_store(store)
        self.profile = resolve_schema(schema)
        self._clock = _clock_callable(clock)
        if llm is not None and gateway is not None:
            raise ValueError("pass either llm or gateway, not both")
        self._write_gateway: LlmGateway = gateway or llm or NullGateway()
        self._extractor = extractor
        self.staging_retention_days = validate_staging_retention_days(
            staging_retention_days
        )
        if min_batch_messages < 1:
            raise ValueError("min_batch_messages MUST be positive")
        self.min_batch_messages = min_batch_messages
        self.max_batch_delay_minutes = validate_max_batch_delay_minutes(
            max_batch_delay_minutes
        )
        if not isinstance(unified_loop, bool):
            raise TypeError("unified_loop MUST be a boolean")
        self.unified_loop = unified_loop
        self.signal_config: SignalConfig = configured_signal_config(
            identity_handles=identity_handles,
            machine_senders=machine_senders,
            alert_keywords=alert_keywords,
            hot_min_authors=hot_min_authors,
            hot_min_messages=hot_min_messages,
        )
        self.query = QueryService(
            self.store,
            self.profile,
            subject_resolver=self.canonical_subject_key,
        )

    def add(
        self,
        scope_id: str,
        messages: list[Message | dict[str, Any]],
        *,
        wait: bool = False,
    ) -> TaskReceipt | TaskResult:
        """Queue minimal public messages without touching the LLM."""

        if not scope_id:
            raise ValueError("scope_id is required")
        validated = [
            message
            if isinstance(message, Message)
            else Message.model_validate(message)
            for message in messages
        ]
        records = [_message_to_record(scope_id, message) for message in validated]
        conversation_labels = {
            message.conversation_id: message.conversation_label
            for message in validated
            if message.conversation_id and message.conversation_label
        }
        if conversation_labels:
            newest = max(message.sent_at for message in validated)
            self.store.upsert_conversation_names(
                scope_id, conversation_labels, seen_at=as_utc(newest)
            )
        receipt = self._enqueue_task(
            scope_id=scope_id,
            kind="messages",
            payload={
                "records": [
                    record.model_dump(mode="json") for record in records
                ]
            },
            accepted=len(validated),
            newest_message_at=max(
                (message.sent_at for message in validated),
                default=None,
            ),
            staged_records=records,
        )
        if wait:
            self.flush(scope_id)
            return self.task(receipt.task_id)
        return receipt

    def add_cards(
        self,
        cards: list[EpisodeCard | dict[str, Any]],
        scope_id: str | None = None,
        *,
        wait: bool = False,
    ) -> TaskReceipt | TaskResult:
        """Queue advanced EpisodeCard input and return a persistent receipt."""

        prepared = []
        for card in cards:
            if isinstance(card, EpisodeCard):
                prepared.append(card)
                continue
            payload = dict(card)
            if scope_id is not None:
                payload.setdefault("scope_id", scope_id)
            prepared.append(EpisodeCard.model_validate(payload))
        if scope_id is None:
            scopes = {card.scope_id for card in prepared}
            if len(scopes) != 1:
                raise ValueError("add_cards requires exactly one scope")
            scope_id = next(iter(scopes))
        if not scope_id:
            raise ValueError("scope_id is required")
        if any(card.scope_id != scope_id for card in prepared):
            raise ValueError("all card scope_id values MUST match scope_id")
        receipt = self._enqueue_task(
            scope_id=scope_id,
            kind="cards",
            payload={
                "cards": [card.model_dump(mode="json") for card in prepared]
            },
            accepted=len(prepared),
            newest_message_at=None,
        )
        if wait:
            self.flush(scope_id)
            return self.task(receipt.task_id)
        return receipt

    def ingest(
        self,
        cards: list[EpisodeCard | dict[str, Any]],
        scope_id: str | None = None,
        *,
        wait: bool = False,
    ) -> TaskReceipt | TaskResult:
        """Deprecated alias for :meth:`add_cards`."""

        warnings.warn(
            "Engine.ingest() is deprecated; use Engine.add_cards() and flush()",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.add_cards(cards, scope_id=scope_id, wait=wait)

    def _ingest_cards_sync(
        self,
        cards: list[EpisodeCard | dict[str, Any]],
        scope_id: str | None = None,
        *,
        record_by_id: dict[str, Record] | None = None,
        handle_counts: _HandleCounts | None = None,
        admission_counts: _AdmissionCounts | None = None,
    ) -> list[Assertion]:
        validated = [
            card if isinstance(card, EpisodeCard) else EpisodeCard.model_validate(card)
            for card in cards
        ]
        if scope_id is not None and any(card.scope_id != scope_id for card in validated):
            raise ValueError("all card scope_id values MUST match the ingest scope_id")
        scopes = sorted({card.scope_id for card in validated})
        emitted: list[Assertion] = []
        with self.store.transaction():
            for card in validated:
                payload_hash = stable_hash(card.model_dump(mode="json"))
                prior_hash = self.store.card_payload_hash(card.scope_id, card.card_id)
                if prior_hash is not None:
                    if prior_hash != payload_hash:
                        raise ValueError(
                            f"card_id {card.card_id!r} was already used with another payload"
                        )
                    continue
                existing = self.store.subjects(card.scope_id)
                subject, _ = resolve_subject(card, self.profile, existing)
                subject = _canonicalized_subject(
                    subject,
                    existing,
                    self.store.subject_merges(card.scope_id),
                )
                subject_exists = any(
                    item.subject_key == subject.subject_key for item in existing
                )
                if not subject_exists:
                    self.store.upsert_subject(subject)
                recorded_at = self._clock()
                assertions = extract_card(
                    card,
                    subject.subject_key,
                    subject.subject_type,
                    self.profile,
                    recorded_at,
                )
                card_emitted, card_admission = self._admit_card_assertions(
                    assertions
                )
                emitted.extend(card_emitted)
                if subject_exists and (card_emitted or not assertions):
                    self.store.upsert_subject(subject)
                if card_admission.unchanged_dropped:
                    self.store.record_gate_report(
                        card.scope_id,
                        accepted=0,
                        rejections={},
                        unchanged_dropped=card_admission.unchanged_dropped,
                    )
                if admission_counts is not None:
                    admission_counts.add(card_admission)
                self.store.enqueue_distill(
                    card,
                    subject_key=subject.subject_key,
                    subject_type=subject.subject_type,
                )
                if record_by_id is not None:
                    counts = self._bind_card_handles(
                        card,
                        subject.subject_key,
                        record_by_id,
                        bound_at=recorded_at,
                    )
                    if handle_counts is not None:
                        handle_counts.add(counts)
                self.store.mark_card(card.scope_id, card.card_id, payload_hash)
            for scope in scopes:
                self._rebuild(scope)
        return emitted

    def _admit_card_assertions(
        self,
        assertions: list[Assertion],
    ) -> tuple[list[Assertion], _AdmissionCounts]:
        emitted: list[Assertion] = []
        counts = _AdmissionCounts()
        for assertion in assertions:
            stored = self.store.assertions(assertion.scope_id)
            if any(
                existing.assertion_id == assertion.assertion_id
                for existing in stored
            ):
                if self.store.add_assertion(assertion):
                    emitted.append(assertion)
                continue
            if self._model_assertion_is_unchanged(assertion, stored):
                counts.unchanged_dropped += 1
                continue
            if self.store.add_assertion(assertion):
                emitted.append(assertion)
        return emitted, counts

    def _model_assertion_is_unchanged(
        self,
        assertion: Assertion,
        stored: list[Assertion],
    ) -> bool:
        if (
            assertion.origin != Origin.model
            or assertion.operation != Operation.ASSERT
        ):
            return False
        intervals, _ = project_assertions(
            _canonicalized_assertions(
                stored,
                self.store.subject_merges(assertion.scope_id),
            ),
            self.profile,
        )
        matching = [
            interval
            for interval in intervals
            if interval.subject_key == assertion.subject_key
            and interval.predicate == assertion.predicate
            and interval.object_key == assertion.object_key
        ]
        cardinality = self.profile.predicate(assertion.predicate).cardinality
        if cardinality in {Cardinality.SINGLE, Cardinality.SET}:
            return any(interval.valid_to is None for interval in matching)
        if cardinality == Cardinality.APPEND:
            return bool(matching)
        raise ValueError(f"unsupported cardinality: {cardinality}")

    def add_records(
        self,
        records: list[Record | dict[str, Any]],
        *,
        scope_id: str,
        cursors: dict[str, str] | None = None,
        backfill: bool = False,
        batch_size: int = 8,
        _stage: bool = True,
    ) -> AddRecordsReport:
        """Extract and ingest previously unseen communication observations."""

        validated = [
            record if isinstance(record, Record) else Record.model_validate(record)
            for record in records
        ]
        if not scope_id:
            raise ValueError("scope_id is required")
        if len({record.record_id for record in validated}) != len(validated):
            raise ValueError(
                "one add_records batch MUST contain at most one observation "
                "for each record_id"
            )
        if _stage:
            staged_at = datetime.now(UTC)
            with self.store.transaction():
                self._stage_and_detect(scope_id, validated, staged_at=staged_at)
        pending: list[tuple[Record, str]] = []
        seen_observations: set[tuple[str, str]] = set()
        skipped = 0
        for record in validated:
            observation_hash = stable_hash(record.model_dump(mode="json"))
            identity = (record.record_id, observation_hash)
            if identity in seen_observations or self.store.has_record_observation(
                scope_id,
                record.record_id,
                observation_hash,
            ):
                skipped += 1
                continue
            seen_observations.add(identity)
            pending.append((record, observation_hash))

        active = [record for record, _ in pending if record.revoked_at is None]
        revoked = [item for item in pending if item[0].revoked_at is not None]
        if active and self._extractor is None and not self.unified_loop:
            raise RuntimeError("record extraction requires a RecordExtractor")
        chunks = _conversation_extraction_chunks(active, batch_size) if active else []
        pending_by_record_id = {
            record.record_id: (record, observation_hash)
            for record, observation_hash in pending
        }
        if self.unified_loop:
            return self._add_records_unified(
                validated=validated,
                pending=pending,
                skipped=skipped,
                revoked=revoked,
                chunks=chunks,
                pending_by_record_id=pending_by_record_id,
                scope_id=scope_id,
                cursors=cursors,
                backfill=backfill,
            )
        cards: list[EpisodeCard] = []
        rejection_counts: dict[str, int] = {}
        emitted: list[Assertion] = []
        admission_counts = _AdmissionCounts()
        handle_counts = _HandleCounts()
        route_counts = _RouteCounts()
        if revoked:
            self._commit_record_chunk(
                scope_id=scope_id,
                observations=revoked,
                cards=[],
                context=[],
                cursors=cursors,
                backfill=backfill,
            )
        for chunk in chunks:
            assert self._extractor is not None
            with self.store.transaction():
                anchors = self._subject_anchors(scope_id)
                context = self._staging_context(scope_id, chunk)
            extraction = self._extractor.extract(
                scope_id=scope_id,
                records=chunk,
                context=context,
                batch_size=batch_size,
                anchors=anchors,
            )
            chunk_cards = list(extraction.cards)
            chunk_observations = [
                pending_by_record_id[record.record_id] for record in chunk
            ]
            record_by_id = {
                record.record_id: record
                for record in [
                    *context,
                    *(record for record, _ in chunk_observations),
                ]
            }
            route_plans = [
                self._route_record_card(card, record_by_id)
                for card in chunk_cards
            ]
            (
                chunk_emitted,
                chunk_admission_counts,
                chunk_handle_counts,
                chunk_route_counts,
            ) = self._commit_record_chunk(
                scope_id=scope_id,
                observations=chunk_observations,
                cards=chunk_cards,
                route_plans=route_plans,
                context=context,
                cursors=cursors,
                backfill=backfill,
            )
            cards.extend(chunk_cards)
            rejection_counts = _merge_counts(
                rejection_counts,
                extraction.rejection_counts,
            )
            emitted.extend(chunk_emitted)
            admission_counts.add(chunk_admission_counts)
            handle_counts.add(chunk_handle_counts)
            route_counts.add(chunk_route_counts)

        return AddRecordsReport(
            scope_id=scope_id,
            records_received=len(validated),
            records_processed=len(pending),
            records_skipped=skipped,
            records_revoked=sum(
                record.revoked_at is not None for record, _ in pending
            ),
            cards_accepted=len(cards),
            cards_dropped=sum(rejection_counts.values()),
            drop_reasons=rejection_counts,
            card_ids=[card.card_id for card in cards],
            assertions_emitted=len(emitted),
            assertion_ids=[assertion.assertion_id for assertion in emitted],
            unchanged_dropped=admission_counts.unchanged_dropped,
            handles_bound=handle_counts.bound,
            handles_already_bound=handle_counts.already_bound,
            handle_conflicts=handle_counts.conflicts,
            **route_counts.to_dict(),
            sync_positions=self.store.sync_positions(scope_id),
        )

    def _add_records_unified(
        self,
        *,
        validated: list[Record],
        pending: list[tuple[Record, str]],
        skipped: int,
        revoked: list[tuple[Record, str]],
        chunks: list[list[Record]],
        pending_by_record_id: dict[str, tuple[Record, str]],
        scope_id: str,
        cursors: dict[str, str] | None,
        backfill: bool,
    ) -> AddRecordsReport:
        from matterhorn.engine.unified_loop import UnifiedLoopSession

        assertion_ids: list[str] = []
        rejection_counts: dict[str, int] = {}
        unchanged_dropped = 0
        route_counts = _RouteCounts()
        if revoked:
            self._commit_record_chunk(
                scope_id=scope_id,
                observations=revoked,
                cards=[],
                context=[],
                cursors=cursors,
                backfill=backfill,
            )
        for chunk in chunks:
            with self.store.transaction():
                context = self._staging_context(scope_id, chunk)
            session = UnifiedLoopSession(
                engine=self,
                scope_id=scope_id,
                records=chunk,
                context=context,
            )
            report = session.run(self._write_gateway)
            assertion_ids.extend(report.assertion_ids)
            rejection_counts = _merge_counts(
                rejection_counts,
                report.rejection_counts,
            )
            unchanged_dropped += report.unchanged_dropped
            observations = [
                pending_by_record_id[record.record_id] for record in chunk
            ]
            self._finalize_unified_chunk(
                scope_id=scope_id,
                observations=observations,
                cursors=cursors,
                backfill=backfill,
                exhausted=report.exhausted,
            )
            if report.exhausted:
                rejection_counts["LOOP_BOUND_EXHAUSTED"] = (
                    rejection_counts.get("LOOP_BOUND_EXHAUSTED", 0) + 1
                )
                route_counts.route_review += 1
        return AddRecordsReport(
            scope_id=scope_id,
            records_received=len(validated),
            records_processed=len(pending),
            records_skipped=skipped,
            records_revoked=sum(record.revoked_at is not None for record, _ in pending),
            cards_accepted=0,
            cards_dropped=sum(rejection_counts.values()),
            drop_reasons=rejection_counts,
            card_ids=[],
            assertions_emitted=len(assertion_ids),
            assertion_ids=assertion_ids,
            unchanged_dropped=unchanged_dropped,
            **route_counts.to_dict(),
            sync_positions=self.store.sync_positions(scope_id),
        )

    def _finalize_unified_chunk(
        self,
        *,
        scope_id: str,
        observations: list[tuple[Record, str]],
        cursors: dict[str, str] | None,
        backfill: bool,
        exhausted: bool,
    ) -> None:
        records = [record for record, _ in observations]
        with self.store.transaction():
            for record, observation_hash in observations:
                self.store.observe_source(
                    scope_id,
                    record.to_source_ref(),
                    revoked_at=record.revoked_at,
                )
                self.store.mark_record_observation(
                    scope_id,
                    record.record_id,
                    observation_hash,
                    record.container_id,
                    _record_observed_at(record),
                )
            if exhausted and records:
                refs = [record.to_source_ref() for record in records]
                card_id = "loop_review_" + stable_hash(
                    [scope_id, [record.record_id for record in records]]
                )
                card = EpisodeCard(
                    card_id=card_id,
                    scope_id=scope_id,
                    date=min(as_utc(record.sent_at) for record in records).date(),
                    title=(records[0].content.strip()[:120] or "Unresolved window"),
                    source_refs=refs,
                    thread_id=(
                        records[0].thread_id
                        if len({record.thread_id for record in records}) == 1
                        else None
                    ),
                )
                self.store.add_review_item(
                    ReviewItem(
                        scope_id=scope_id,
                        review_id="review_" + stable_hash([scope_id, card_id]),
                        card_json=card.model_dump(mode="json"),
                        reasons=["LOOP_BOUND_EXHAUSTED"],
                        created_at=self._clock(),
                    )
                )
                self.store.record_gate_report(
                    scope_id,
                    accepted=0,
                    rejections={"LOOP_BOUND_EXHAUSTED": 1},
                    route_counts={"route_review": 1},
                )
            if observations and not backfill:
                by_container: dict[str, list[Record]] = {}
                for record in records:
                    by_container.setdefault(record.container_id, []).append(record)
                for container_id, items in by_container.items():
                    self.store.update_sync_position(
                        scope_id,
                        container_id,
                        watermark=max(_record_observed_at(item) for item in items),
                        cursor=(cursors or {}).get(container_id),
                    )

    def _unified_pre_route_keys(
        self,
        scope_id: str,
        records: list[Record],
        context: list[Record],
    ) -> list[str]:
        if not records:
            return []
        refs = [record.to_source_ref() for record in [*context, *records]]
        thread_ids = {record.thread_id for record in records}
        card = EpisodeCard(
            card_id="loop_route_" + stable_hash(
                [scope_id, [record.record_id for record in records]]
            ),
            scope_id=scope_id,
            date=min(as_utc(record.sent_at) for record in records).date(),
            title=" ".join(record.content for record in records),
            source_refs=refs,
            thread_id=(
                next(iter(thread_ids))
                if len(thread_ids) == 1 and None not in thread_ids
                else None
            ),
        )
        with self.store.transaction():
            subjects = self.store.subjects(scope_id)
            merges = self.store.subject_merges(scope_id)
            canonical = _canonicalized_subjects(subjects, merges)
            edges = _merge_edges(merges)
            handles = self._handle_route_targets(
                card,
                {record.record_id: record for record in [*context, *records]},
                edges,
            )
            if len(handles) == 1:
                return list(handles)
            threaded = thread_match(card, self.profile, canonical)
            if threaded is not None:
                return [threaded.subject_key]
            evidenced = evidence_match(card, self.profile, canonical)
            if evidenced is not None:
                return [evidenced.subject_key]
        return []

    @staticmethod
    def _canonical_subjects_for_loop(
        subjects: list[SubjectRecord],
        merges: list[SubjectMerge],
    ) -> list[SubjectRecord]:
        return _canonicalized_subjects(subjects, merges)

    @staticmethod
    def _merge_edges_for_loop(merges: list[SubjectMerge]) -> dict[str, str]:
        return _merge_edges(merges)

    def _commit_record_chunk(
        self,
        *,
        scope_id: str,
        observations: list[tuple[Record, str]],
        cards: list[EpisodeCard],
        route_plans: list[_RoutePlan] | None = None,
        context: list[Record],
        cursors: dict[str, str] | None,
        backfill: bool,
    ) -> tuple[
        list[Assertion],
        _AdmissionCounts,
        _HandleCounts,
        _RouteCounts,
    ]:
        emitted: list[Assertion] = []
        admission_counts = _AdmissionCounts()
        handle_counts = _HandleCounts()
        route_counts = _RouteCounts()
        with self.store.transaction():
            record_by_id = {
                record.record_id: record
                for record in [
                    *context,
                    *(record for record, _ in observations),
                ]
            }
            # Person directory: ids stay the identity in assertions; names are
            # display data, registered whenever a record author carries one.
            names = {
                record.author.id: record.author.display_name
                for record, _ in observations
                if record.author.display_name
                and record.author.display_name != record.author.id
            }
            for card in cards:
                for participant in card.participants:
                    display = getattr(participant, "display_name", None)
                    if display and display != participant.id:
                        names[participant.id] = display
            if names:
                # Deterministic stamp without consuming the engine clock
                # (conformance clocks are finite iterators).
                seen_at = max(
                    _record_observed_at(record) for record, _ in observations
                )
                self.store.upsert_person_names(scope_id, names, seen_at=seen_at)
            for record, observation_hash in observations:
                self.store.observe_source(
                    scope_id,
                    record.to_source_ref(),
                    revoked_at=record.revoked_at,
                )
                self.store.mark_record_observation(
                    scope_id,
                    record.record_id,
                    observation_hash,
                    record.container_id,
                    _record_observed_at(record),
                )
            for card in cards:
                for source_ref in card.source_refs:
                    record = record_by_id.get(source_ref.source_id)
                    self.store.observe_source(
                        scope_id,
                        source_ref,
                        revoked_at=(
                            record.revoked_at if record is not None else None
                        ),
                    )
            for card, plan in zip(
                cards,
                route_plans or [],
                strict=True,
            ):
                (
                    card_emitted,
                    card_admission_counts,
                    card_handles,
                    card_routes,
                ) = self._apply_route_plan(
                    card,
                    plan,
                    record_by_id=record_by_id,
                )
                emitted.extend(card_emitted)
                admission_counts.add(card_admission_counts)
                handle_counts.add(card_handles)
                route_counts.add(card_routes)
            if observations and not backfill:
                by_container: dict[str, list[Record]] = {}
                for record, _ in observations:
                    by_container.setdefault(record.container_id, []).append(record)
                for container_id, items in by_container.items():
                    self.store.update_sync_position(
                        scope_id,
                        container_id,
                        watermark=max(_record_observed_at(item) for item in items),
                        cursor=(cursors or {}).get(container_id),
                    )
        return emitted, admission_counts, handle_counts, route_counts

    def _route_record_card(
        self,
        card: EpisodeCard,
        record_by_id: dict[str, Record],
    ) -> _RoutePlan:
        payload_hash = stable_hash(card.model_dump(mode="json"))
        review_id = "review_" + stable_hash([card.scope_id, card.card_id])
        with self.store.transaction():
            prior_hash = self.store.card_payload_hash(card.scope_id, card.card_id)
            if prior_hash is not None:
                if prior_hash != payload_hash:
                    raise ValueError(
                        f"card_id {card.card_id!r} was already used with another payload"
                    )
                return _RoutePlan("duplicate", duplicate=True)
            if self.store.review_item(card.scope_id, review_id) is not None:
                return _RoutePlan("duplicate", duplicate=True)

            subjects = self.store.subjects(card.scope_id)
            merges = self.store.subject_merges(card.scope_id)
            canonical_subjects = _canonicalized_subjects(subjects, merges)
            edges = _merge_edges(merges)
            by_key = {item.subject_key: item for item in canonical_subjects}

            if card.subject_key is not None and card.subject_key.startswith("mail:"):
                return _RoutePlan("trusted", card.subject_key)

            handle_targets = self._handle_route_targets(
                card,
                record_by_id,
                edges,
            )
            handle_conflicts = int(len(handle_targets) > 1)
            handle_subject = (
                next(iter(handle_targets)) if len(handle_targets) == 1 else None
            )
            thread_subject = thread_match(card, self.profile, canonical_subjects)
            evidence_subject = evidence_match(
                card,
                self.profile,
                canonical_subjects,
            )
            suggestion = None
            if card.subject_key is not None:
                suggested_key = _canonical_subject_key(card.subject_key, edges)
                if suggested_key in by_key and self._subject_open_from_cards(
                    suggested_key,
                    self.store.memory_cards(card.scope_id),
                ):
                    suggestion = suggested_key

            if handle_subject is not None:
                lower = [
                    thread_subject.subject_key if thread_subject else None,
                    evidence_subject.subject_key if evidence_subject else None,
                    suggestion,
                ]
                return _RoutePlan(
                    "handle",
                    handle_subject,
                    handle_conflicts=handle_conflicts,
                    disagreement=_disagrees(handle_subject, lower),
                )
            if thread_subject is not None:
                lower = [
                    evidence_subject.subject_key if evidence_subject else None,
                    suggestion,
                ]
                return _RoutePlan(
                    "thread",
                    thread_subject.subject_key,
                    handle_conflicts=handle_conflicts,
                    disagreement=_disagrees(thread_subject.subject_key, lower),
                )
            if evidence_subject is not None:
                return _RoutePlan(
                    "evidence",
                    evidence_subject.subject_key,
                    handle_conflicts=handle_conflicts,
                    disagreement=_disagrees(
                        evidence_subject.subject_key,
                        [suggestion],
                    ),
                )
            if suggestion is not None:
                return _RoutePlan(
                    "model",
                    suggestion,
                    handle_conflicts=handle_conflicts,
                )
            candidates = self._routing_candidates(
                card,
                canonical_subjects,
                subjects,
                merges,
            )

        if not candidates:
            return _RoutePlan("new", handle_conflicts=handle_conflicts)
        prompt = build_adjudication_prompt(card, candidates)
        raw = self._write_gateway.complete(
            system=prompt.system,
            user=prompt.user,
            response_schema=prompt.response_schema,
        )
        gated = gate_adjudication(
            raw,
            card=card,
            candidates=candidates,
            confidence_threshold=(
                self.profile.identity.adjudication_confidence_threshold
            ),
        )
        if gated.outcome == "attach":
            return _RoutePlan(
                "model",
                gated.subject_key,
                tuple(candidates),
                handle_conflicts=handle_conflicts,
            )
        if gated.outcome == "new":
            return _RoutePlan(
                "new",
                candidates=tuple(candidates),
                handle_conflicts=handle_conflicts,
            )
        return _RoutePlan(
            "review",
            candidates=tuple(candidates),
            reasons=gated.reasons,
            handle_conflicts=handle_conflicts,
        )

    def _handle_route_targets(
        self,
        card: EpisodeCard,
        record_by_id: dict[str, Record],
        edges: dict[str, str],
    ) -> set[str]:
        matches = scan_handles(self.profile, card.title, card.source_refs)
        refs_by_id = {ref.source_id: ref for ref in card.source_refs}
        for source_ref in card.source_refs:
            record = record_by_id.get(source_ref.source_id)
            content = (
                record.content
                if record is not None
                else source_ref.excerpt
            )
            if not content:
                continue
            matches.extend(
                scan_handles(
                    self.profile,
                    content,
                    [refs_by_id[source_ref.source_id]],
                )
            )
        active = {
            (item.handle_type, item.normalized_value): _canonical_subject_key(
                item.subject_key,
                edges,
            )
            for item in self.store.active_subject_handles(card.scope_id)
        }
        return {
            active[(match.handle_type, match.normalized_value)]
            for match in matches
            if (match.handle_type, match.normalized_value) in active
        }

    def _routing_candidates(
        self,
        card: EpisodeCard,
        canonical_subjects: list[SubjectRecord],
        original_subjects: list[SubjectRecord],
        merges: list[SubjectMerge],
    ) -> list[AdjudicationCandidate]:
        edges = _merge_edges(merges)
        memory_cards = {
            item.subject_key: item
            for item in self.store.memory_cards(card.scope_id)
        }
        aliases: dict[str, list[str]] = {}
        original_by_key = {item.subject_key: item for item in original_subjects}
        for subject in original_subjects:
            target = _canonical_subject_key(subject.subject_key, edges)
            if target != subject.subject_key:
                title = subject.title
                if title != original_by_key[target].title:
                    aliases.setdefault(target, []).append(title)
        for values in aliases.values():
            values.sort(key=lambda value: value.encode("utf-8"))

        handles: dict[str, list[str]] = {}
        for handle in self.store.active_subject_handles(card.scope_id):
            target = _canonical_subject_key(handle.subject_key, edges)
            handles.setdefault(target, []).append(
                f"{handle.handle_type}:{handle.handle_value}"
            )
        for values in handles.values():
            values.sort(key=lambda value: value.encode("utf-8"))

        evidence: dict[str, list[tuple[datetime, bytes, str]]] = {}
        for assertion in self.store.assertions(card.scope_id):
            target = _canonical_subject_key(assertion.subject_key, edges)
            for source_ref in assertion.source_refs:
                if source_ref.excerpt:
                    evidence.setdefault(target, []).append(
                        (
                            as_utc(assertion.valid_from),
                            assertion.assertion_id.encode("utf-8"),
                            source_ref.excerpt,
                        )
                    )

        recalled: list[tuple[int, AdjudicationCandidate]] = []
        for subject in canonical_subjects:
            if subject.subject_type != self.profile.primary_subject.type:
                continue
            if not self._subject_open_from_cards(
                subject.subject_key,
                list(memory_cards.values()),
            ):
                continue
            projected = memory_cards.get(subject.subject_key)
            current = projected.current if projected is not None else {}
            recent = _bounded_recent_evidence(
                evidence.get(subject.subject_key, [])
            )
            candidate = AdjudicationCandidate(
                subject_key=subject.subject_key,
                title=subject.title,
                aliases=aliases.get(subject.subject_key, []),
                handles=handles.get(subject.subject_key, []),
                status=current.get("status"),
                next_step=current.get("next_step"),
                participants=_as_list(current.get("participated_by")),
                recent_evidence=recent,
            )
            recalled.append((candidate_score(card, candidate), candidate))

        # Only lexically related candidates are offered. Zero-score filler
        # candidates created false dilemmas: a genuinely NEW topic faced a
        # lineup of unrelated matters, the adjudicator abstained, the card
        # queued for review, and the topic could never form a matter — every
        # later batch repeated the loop. No positive candidate → clean new.
        # A single shared token (often a common word from evidence text) is
        # noise, and noise candidates make the adjudicator abstain — every
        # abstain costs a human review. Require a real lexical relationship.
        positive = sorted(
            (item for item in recalled if item[0] >= 2),
            key=lambda item: (-item[0], item[1].subject_key.encode("utf-8")),
        )
        return [candidate for _, candidate in positive[:5]]

    def _subject_open_from_cards(
        self,
        subject_key: str,
        cards: list[Any],
    ) -> bool:
        completion = self.profile.completion
        if completion is None:
            return True
        card = next(
            (item for item in cards if item.subject_key == subject_key),
            None,
        )
        if card is None:
            return True
        return card.current.get(completion.predicate) not in set(
            completion.completed_values
        )

    def _apply_route_plan(
        self,
        card: EpisodeCard,
        plan: _RoutePlan,
        *,
        record_by_id: dict[str, Record],
    ) -> tuple[
        list[Assertion],
        _AdmissionCounts,
        _HandleCounts,
        _RouteCounts,
    ]:
        handles = _HandleCounts()
        routes = _RouteCounts(
            route_disagreements=int(plan.disagreement),
        )
        if plan.duplicate:
            return [], _AdmissionCounts(), _HandleCounts(), _RouteCounts()
        if plan.route == "review":
            handles.conflicts = plan.handle_conflicts
            routes.route_review = 1
            item = ReviewItem(
                scope_id=card.scope_id,
                review_id="review_" + stable_hash([card.scope_id, card.card_id]),
                card_json=card.model_dump(mode="json"),
                reasons=list(plan.reasons),
                candidates_json=[
                    candidate.model_dump(mode="json")
                    for candidate in plan.candidates
                ],
                created_at=self._clock(),
            )
            self.store.add_review_item(item)
            self.store.record_gate_report(
                card.scope_id,
                accepted=0,
                rejections={},
                handle_conflicts=handles.conflicts,
                route_counts=routes.to_dict(),
            )
            return [], _AdmissionCounts(), handles, routes

        existing = self.store.subjects(card.scope_id)
        merges = self.store.subject_merges(card.scope_id)
        if plan.route == "trusted":
            routed_card = card
            subject, _ = resolve_subject(routed_card, self.profile, existing)
        elif plan.subject_key is not None:
            match = next(
                (item for item in existing if item.subject_key == plan.subject_key),
                None,
            )
            if match is None:
                raise ValueError(
                    f"routed subject_key {plan.subject_key!r} no longer exists"
                )
            subject = attach_subject(match, card)
        else:
            subject = new_subject(card, self.profile, existing)
        subject = _canonicalized_subject(subject, existing, merges)
        subject_exists = any(
            item.subject_key == subject.subject_key for item in existing
        )
        if not subject_exists:
            self.store.upsert_subject(subject)
        recorded_at = self._clock()
        assertions = extract_card(
            card,
            subject.subject_key,
            subject.subject_type,
            self.profile,
            recorded_at,
        )
        emitted, admission_counts = self._admit_card_assertions(assertions)
        if subject_exists and (emitted or not assertions):
            self.store.upsert_subject(subject)
        self.store.enqueue_distill(
            card,
            subject_key=subject.subject_key,
            subject_type=subject.subject_type,
        )
        bound = self._bind_card_handles(
            card,
            subject.subject_key,
            record_by_id,
            bound_at=recorded_at,
        )
        handles.add(bound)
        handles.conflicts = max(handles.conflicts, plan.handle_conflicts)
        self.store.mark_card(
            card.scope_id,
            card.card_id,
            stable_hash(card.model_dump(mode="json")),
        )
        if plan.route != "trusted":
            setattr(routes, f"route_{plan.route}", 1)
        self.store.record_gate_report(
            card.scope_id,
            accepted=0,
            rejections={},
            unchanged_dropped=admission_counts.unchanged_dropped,
            handle_conflicts=handles.conflicts,
            route_counts=routes.to_dict(),
        )
        if not subject_exists:
            self._enqueue_parent_suggestion(
                card,
                child_subject_key=subject.subject_key,
                record_by_id=record_by_id,
                created_at=recorded_at,
            )
        self._rebuild(card.scope_id)
        return emitted, admission_counts, handles, routes

    def _enqueue_parent_suggestion(
        self,
        card: EpisodeCard,
        *,
        child_subject_key: str,
        record_by_id: dict[str, Record],
        created_at: datetime,
    ) -> None:
        if not self._goal_graph_enabled():
            return
        containers = {
            record_by_id[ref.source_id].container_id
            for ref in card.source_refs
            if ref.source_id in record_by_id
        }
        if len(containers) != 1:
            return
        container_id = next(iter(containers))
        conversation_sources = {
            item.record_id
            for item in self.store.record_observations(card.scope_id)
            if item.container_id == container_id
        }
        cutoff = created_at - timedelta(days=self.staging_retention_days)
        graph = self._goal_projection(card.scope_id)
        completed_values = graph.completed_values
        candidates: set[str] = set()
        for assertion in _canonicalized_assertions(
            self.store.assertions(card.scope_id),
            self.store.subject_merges(card.scope_id),
        ):
            if assertion.subject_key == child_subject_key:
                continue
            if not cutoff <= assertion.recorded_at <= created_at:
                continue
            if assertion.subject_key in graph.parents:
                continue
            if not any(
                ref.source_id in conversation_sources
                for ref in assertion.source_refs
            ):
                continue
            node = graph.nodes.get(assertion.subject_key)
            if node is None or str(node.status).casefold() in completed_values:
                continue
            candidates.add(assertion.subject_key)
        if len(candidates) != 1:
            return
        parent_subject_key = next(iter(candidates))
        parent = graph.nodes[parent_subject_key]
        review_id = "review_goal_" + stable_hash(
            [
                card.scope_id,
                card.card_id,
                child_subject_key,
                parent_subject_key,
            ]
        )
        review_card = card.model_copy(
            update={"subject_key": child_subject_key}
        )
        self.store.add_review_item(
            ReviewItem(
                scope_id=card.scope_id,
                review_id=review_id,
                card_json=review_card.model_dump(mode="json"),
                reasons=["PARENT_SUGGESTION"],
                candidates_json=[
                    {
                        "action": "attach_subgoal",
                        "subject_key": child_subject_key,
                        "parent_subject_key": parent_subject_key,
                        "title": parent.title,
                    }
                ],
                created_at=created_at,
            )
        )

    def _bind_card_handles(
        self,
        card: EpisodeCard,
        subject_key: str,
        record_by_id: dict[str, Record],
        *,
        bound_at: datetime,
    ) -> _HandleCounts:
        matches = scan_handles(self.profile, card.title, card.source_refs)
        refs_by_id = {ref.source_id: ref for ref in card.source_refs}
        for source_ref in card.source_refs:
            record = record_by_id.get(source_ref.source_id)
            content = record.content if record is not None else source_ref.excerpt
            if not content:
                continue
            matches.extend(
                scan_handles(
                    self.profile,
                    content,
                    [refs_by_id[source_ref.source_id]],
                )
            )

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for match in matches:
            key = (match.handle_type, match.normalized_value)
            entry = grouped.setdefault(
                key,
                {
                    "handle_value": match.handle_value,
                    "source_refs": [],
                    "source_ids": set(),
                },
            )
            for source_ref in match.source_refs:
                if source_ref.source_id in entry["source_ids"]:
                    continue
                entry["source_ids"].add(source_ref.source_id)
                entry["source_refs"].append(source_ref)

        counts = _HandleCounts()
        for handle_type, normalized_value in sorted(
            grouped,
            key=lambda item: (item[0].encode("utf-8"), item[1].encode("utf-8")),
        ):
            entry = grouped[(handle_type, normalized_value)]
            result, _ = self._bind_handle(
                scope_id=card.scope_id,
                subject_key=subject_key,
                handle_type=handle_type,
                handle_value=entry["handle_value"],
                normalized_value=normalized_value,
                source_refs=entry["source_refs"],
                origin=HandleOrigin.system,
                bound_at=bound_at,
            )
            if result == "bound":
                counts.bound += 1
            elif result == "already_bound":
                counts.already_bound += 1
            else:
                counts.conflicts += 1
        return counts

    def bind_handle(
        self,
        scope_id: str,
        subject_key: str,
        handle_type: str,
        handle_value: str,
        *,
        source_refs: list[SourceRef | dict[str, Any]],
    ) -> SubjectHandle:
        refs = _source_refs(source_refs, operation="subject handle bindings")
        if not self.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
        if not matches_handle_pattern(self.profile, handle_type, handle_value):
            raise ValueError(
                "handle_value MUST match the configured structured-identifier pattern"
            )
        normalized_value = normalize_handle(
            self.profile,
            handle_type,
            handle_value,
        )
        with self.store.transaction():
            for source_ref in refs:
                self.store.observe_source(scope_id, source_ref)
            result, handle = self._bind_handle(
                scope_id=scope_id,
                subject_key=self.canonical_subject_key(scope_id, subject_key),
                handle_type=handle_type,
                handle_value=handle_value,
                normalized_value=normalized_value,
                source_refs=refs,
                origin=HandleOrigin.human,
                bound_at=self._clock(),
            )
            if result == "conflict":
                raise SubjectHandleConflictError(
                    "handle is already bound to a different subject"
                )
            assert handle is not None
            return handle

    def unbind_handle(
        self,
        scope_id: str,
        subject_key: str,
        handle_type: str,
        normalized_value: str,
        *,
        source_refs: list[SourceRef | dict[str, Any]],
    ) -> SubjectHandle:
        refs = _source_refs(source_refs, operation="subject handle unbindings")
        expected_subject = self.canonical_subject_key(scope_id, subject_key)
        normalized_value = normalize_handle(
            self.profile,
            handle_type,
            normalized_value,
        )
        with self.store.transaction():
            for source_ref in refs:
                self.store.observe_source(scope_id, source_ref)
            active = self.store.active_subject_handles(
                scope_id,
                handle_type=handle_type,
                normalized_value=normalized_value,
            )
            if not active:
                raise ResourceNotFoundError("active subject handle binding not found")
            actual_subject = self.canonical_subject_key(
                scope_id,
                active[0].subject_key,
            )
            if actual_subject != expected_subject:
                raise SubjectHandleConflictError(
                    "handle is bound to a different subject"
                )
            revoked = self.store.revoke_subject_handle(
                scope_id,
                handle_type,
                normalized_value,
                revoked_at=self._clock(),
                revocation_origin=HandleOrigin.human.value,
                source_refs=refs,
            )
            assert revoked is not None
            return revoked.model_copy(update={"subject_key": actual_subject})

    def subject_handles(
        self,
        scope_id: str,
        subject_key: str,
    ) -> list[SubjectHandle]:
        if not self.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
        canonical_key = self.canonical_subject_key(scope_id, subject_key)
        result = []
        for handle in self.store.active_subject_handles(scope_id):
            if self.canonical_subject_key(scope_id, handle.subject_key) != canonical_key:
                continue
            result.append(handle.model_copy(update={"subject_key": canonical_key}))
        return sorted(result, key=_handle_sort_key)

    def handle_lookup(
        self,
        scope_id: str,
        value: str,
        handle_type: str | None = None,
    ) -> list[SubjectHandle]:
        if handle_type is not None:
            candidates = [(handle_type, normalize_handle(self.profile, handle_type, value))]
        else:
            candidates = [
                (
                    pattern.handle_type,
                    normalize_handle(self.profile, pattern.handle_type, value),
                )
                for pattern in self.profile.handle_patterns
            ]
        result: dict[str, SubjectHandle] = {}
        for selected_type, normalized_value in candidates:
            for handle in self.store.active_subject_handles(
                scope_id,
                handle_type=selected_type,
                normalized_value=normalized_value,
            ):
                canonical_key = self.canonical_subject_key(
                    scope_id,
                    handle.subject_key,
                )
                result[handle.binding_id] = handle.model_copy(
                    update={"subject_key": canonical_key}
                )
        return sorted(result.values(), key=_handle_sort_key)

    def review_items(self, scope_id: str) -> list[ReviewItem]:
        return self.store.review_items(scope_id)

    def resolve_review(
        self,
        scope_id: str,
        review_id: str,
        *,
        action: str,
        subject_key: str | None = None,
        parent_subject_key: str | None = None,
        source_refs: list[SourceRef | dict[str, Any]],
    ) -> ReviewItem:
        if action not in {"attach", "new", "drop", "attach_subgoal"}:
            raise ValueError(
                "review action MUST be 'attach', 'new', 'drop', or "
                "'attach_subgoal'"
            )
        if action == "attach" and not subject_key:
            raise ValueError("attach review action requires subject_key")
        if action in {"new", "drop", "attach_subgoal"} and subject_key is not None:
            raise ValueError(f"{action} review action MUST NOT include subject_key")
        if action == "attach_subgoal" and not parent_subject_key:
            raise ValueError(
                "attach_subgoal review action requires parent_subject_key"
            )
        if action != "attach_subgoal" and parent_subject_key is not None:
            raise ValueError(
                f"{action} review action MUST NOT include parent_subject_key"
            )
        refs = _source_refs(source_refs, operation="review resolutions")
        if action == "attach_subgoal":
            return self._resolve_subgoal_review(
                scope_id,
                review_id,
                parent_subject_key=parent_subject_key or "",
                source_refs=refs,
            )
        with self.store.transaction():
            item = self.store.review_item(scope_id, review_id)
            if item is None:
                raise ResourceNotFoundError(f"unknown review_id: {review_id}")
            if item.resolved_at is not None:
                raise ReviewConflictError(
                    f"review_id {review_id!r} is already resolved"
                )
            original_card = EpisodeCard.model_validate(item.card_json)
            if action == "drop":
                # Auditor verdict: this card is noise (process log, junk) —
                # discard it with provenance. The review row keeps the card
                # and the resolution for the audit trail; nothing is ingested.
                resolved_at = self._clock()
                for source_ref in refs:
                    self.store.observe_source(scope_id, source_ref)
                return self.store.resolve_review_item(
                    scope_id,
                    review_id,
                    resolved_at=resolved_at,
                    resolution={
                        "action": "drop",
                        "source_refs": [
                            ref.model_dump(mode="json") for ref in refs
                        ],
                    },
                )
            combined_refs = _stable_source_refs(
                [*original_card.source_refs, *refs]
            )
            card = original_card.model_copy(
                update={"source_refs": combined_refs}
            )
            existing = self.store.subjects(scope_id)
            merges = self.store.subject_merges(scope_id)
            if action == "attach":
                canonical_key = self.canonical_subject_key(
                    scope_id,
                    subject_key or "",
                )
                match = next(
                    (
                        subject
                        for subject in existing
                        if subject.subject_key == canonical_key
                    ),
                    None,
                )
                if match is None:
                    raise ResourceNotFoundError(
                        f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
                    )
                subject = attach_subject(match, card)
            else:
                subject = new_subject(card, self.profile, existing)
            subject = _canonicalized_subject(subject, existing, merges)
            self.store.upsert_subject(subject)
            resolved_at = self._clock()
            assertions = extract_card(
                card,
                subject.subject_key,
                subject.subject_type,
                self.profile,
                resolved_at,
                origin=Origin.human,
            )
            for assertion in assertions:
                self.store.add_assertion(assertion)
            for source_ref in refs:
                self.store.observe_source(scope_id, source_ref)
            self.store.enqueue_distill(
                card,
                subject_key=subject.subject_key,
                subject_type=subject.subject_type,
            )
            self._bind_card_handles(
                card,
                subject.subject_key,
                {},
                bound_at=resolved_at,
            )
            self.store.mark_card(
                scope_id,
                original_card.card_id,
                stable_hash(original_card.model_dump(mode="json")),
            )
            resolution = {
                "action": action,
                "subject_key": subject.subject_key,
                "source_refs": [
                    ref.model_dump(mode="json") for ref in refs
                ],
            }
            resolved = self.store.resolve_review_item(
                scope_id,
                review_id,
                resolved_at=resolved_at,
                resolution=resolution,
            )
            self._rebuild(scope_id)
            return resolved

    def _resolve_subgoal_review(
        self,
        scope_id: str,
        review_id: str,
        *,
        parent_subject_key: str,
        source_refs: list[SourceRef],
    ) -> ReviewItem:
        item = self.store.review_item(scope_id, review_id)
        if item is None:
            raise ResourceNotFoundError(f"unknown review_id: {review_id}")
        if item.resolved_at is not None:
            raise ReviewConflictError(
                f"review_id {review_id!r} is already resolved"
            )
        proposal = next(
            (
                candidate
                for candidate in item.candidates_json
                if candidate.get("action") == "attach_subgoal"
            ),
            None,
        )
        if proposal is None:
            raise ValueError("review item does not contain a subgoal proposal")
        child_subject_key = proposal.get("subject_key")
        if not isinstance(child_subject_key, str) or not child_subject_key:
            raise ValueError("subgoal proposal is missing subject_key")
        subjects = {
            subject.subject_key: subject
            for subject in self.store.subjects(scope_id)
        }
        child_canonical = self.canonical_subject_key(
            scope_id, child_subject_key
        )
        child = subjects.get(child_canonical)
        if child is None:
            raise ResourceNotFoundError(
                f"unknown subject_key {child_subject_key!r} in scope {scope_id!r}"
            )
        resolved_at = self._clock()
        assertion = Assertion(
            assertion_id=derive_assertion_id(
                scope_id,
                child_canonical,
                PART_OF,
                Operation.ASSERT,
                object_key(parent_subject_key),
                resolved_at,
                source_refs,
            ),
            scope_id=scope_id,
            subject_key=child_canonical,
            subject_type=child.subject_type,
            predicate=PART_OF,
            operation=Operation.ASSERT,
            object_value=parent_subject_key,
            object_key=object_key(parent_subject_key),
            valid_from=resolved_at,
            recorded_at=resolved_at,
            source_refs=source_refs,
            origin=Origin.human,
        )
        rejection = self._structure_rejection(assertion)
        if rejection is not None:
            self._record_structure_rejection(scope_id, rejection)
            raise ValueError(
                f"{rejection.value}: rejected {PART_OF} edge"
            )
        with self.store.transaction():
            for source_ref in source_refs:
                self.store.observe_source(scope_id, source_ref)
            self.store.add_assertion(assertion)
            resolved = self.store.resolve_review_item(
                scope_id,
                review_id,
                resolved_at=resolved_at,
                resolution={
                    "action": "attach_subgoal",
                    "subject_key": child_canonical,
                    "parent_subject_key": self.canonical_subject_key(
                        scope_id, parent_subject_key
                    ),
                    "source_refs": [
                        ref.model_dump(mode="json") for ref in source_refs
                    ],
                },
            )
            self._rebuild(scope_id)
            return resolved

    def subject_is_active(self, scope_id: str, subject_key: str) -> bool:
        if not self.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
        completion = self.profile.completion
        if completion is None:
            return True
        values = self.query.current(
            scope_id,
            self.canonical_subject_key(scope_id, subject_key),
            completion.predicate,
        )
        return not any(
            value.value in completion.completed_values for value in values
        )

    def backfill_handles(self, scope_id: str) -> HandleBackfillReport:
        if not self.scope_exists(scope_id):
            raise ResourceNotFoundError(f"unknown scope_id: {scope_id}")
        assertions_by_subject: dict[str, list[Assertion]] = {}
        for assertion in self.store.assertions(scope_id):
            assertions_by_subject.setdefault(assertion.subject_key, []).append(assertion)
        cards = {
            card.subject_key: card for card in self.store.memory_cards(scope_id)
        }
        counts = _HandleCounts()
        with self.store.transaction():
            for subject in self.store.subjects(scope_id):
                assertions = assertions_by_subject.get(subject.subject_key, [])
                refs = _stable_source_refs(
                    ref for assertion in assertions for ref in assertion.source_refs
                )
                matches = scan_handles(self.profile, subject.title, refs) if refs else []
                for assertion in assertions:
                    for source_ref in assertion.source_refs:
                        if source_ref.excerpt:
                            matches.extend(
                                scan_handles(
                                    self.profile,
                                    source_ref.excerpt,
                                    [source_ref],
                                )
                            )
                canonical_key = self.canonical_subject_key(
                    scope_id,
                    subject.subject_key,
                )
                card = cards.get(canonical_key)
                if (
                    card is not None
                    and refs
                    and subject.subject_key == canonical_key
                ):
                    matches.extend(scan_handles(self.profile, card.title, refs))
                    matches.extend(
                        scan_handles(
                            self.profile,
                            json.dumps(
                                card.current,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            refs,
                        )
                    )
                grouped = _group_handle_matches(matches)
                for (selected_type, normalized_value), entry in grouped.items():
                    result, _ = self._bind_handle(
                        scope_id=scope_id,
                        subject_key=subject.subject_key,
                        handle_type=selected_type,
                        handle_value=entry["handle_value"],
                        normalized_value=normalized_value,
                        source_refs=entry["source_refs"],
                        origin=HandleOrigin.system,
                        bound_at=self._clock(),
                    )
                    if result == "bound":
                        counts.bound += 1
                    elif result == "already_bound":
                        counts.already_bound += 1
                    else:
                        counts.conflicts += 1
            if counts.conflicts:
                self.store.record_gate_report(
                    scope_id,
                    accepted=0,
                    rejections={},
                    handle_conflicts=counts.conflicts,
                )
        return HandleBackfillReport(
            scope_id=scope_id,
            bound=counts.bound,
            skipped_conflict=counts.conflicts,
            already_bound=counts.already_bound,
        )

    def _bind_handle(
        self,
        *,
        scope_id: str,
        subject_key: str,
        handle_type: str,
        handle_value: str,
        normalized_value: str,
        source_refs: list[SourceRef],
        origin: HandleOrigin,
        bound_at: datetime,
    ) -> tuple[str, SubjectHandle | None]:
        active = self.store.active_subject_handles(
            scope_id,
            handle_type=handle_type,
            normalized_value=normalized_value,
        )
        if active:
            existing = active[0]
            if (
                self.canonical_subject_key(scope_id, existing.subject_key)
                == self.canonical_subject_key(scope_id, subject_key)
            ):
                return (
                    "already_bound",
                    existing.model_copy(
                        update={
                            "subject_key": self.canonical_subject_key(
                                scope_id,
                                subject_key,
                            )
                        }
                    ),
                )
            return "conflict", None
        generation = sum(
            handle.handle_type == handle_type
            and handle.normalized_value == normalized_value
            for handle in self.store.subject_handle_bindings(scope_id)
        )
        handle = SubjectHandle(
            binding_id="hdl_"
            + stable_hash(
                [
                    scope_id,
                    subject_key,
                    handle_type,
                    normalized_value,
                    instant_text(bound_at),
                    origin.value,
                    [ref.model_dump(mode="json") for ref in source_refs],
                    generation,
                ]
            ),
            scope_id=scope_id,
            subject_key=subject_key,
            handle_type=handle_type,
            handle_value=handle_value,
            normalized_value=normalized_value,
            origin=origin,
            source_refs=source_refs,
            bound_at=bound_at,
        )
        result = self.store.add_subject_handle(handle)
        return result, handle if result != "conflict" else None

    def _subject_anchors(self, scope_id: str) -> list[SubjectAnchor]:
        activity: dict[str, datetime] = {}
        merges = self.store.subject_merges(scope_id)
        for assertion in _canonicalized_assertions(
            self.store.assertions(scope_id),
            merges,
        ):
            previous = activity.get(assertion.subject_key)
            if previous is None or assertion.valid_from > previous:
                activity[assertion.subject_key] = assertion.valid_from
        cards = sorted(
            self.store.memory_cards(scope_id),
            key=lambda item: (
                (
                    -as_utc(activity[item.subject_key]).timestamp()
                    if item.subject_key in activity
                    else float("inf")
                ),
                item.subject_key.encode("utf-8"),
            ),
        )
        anchors = []
        for card in cards:
            status = card.current.get("status")
            completion = self.profile.completion
            if (
                completion is not None
                and card.current.get(completion.predicate)
                in completion.completed_values
            ):
                continue
            last_active_at = activity.get(card.subject_key, card.updated_at)
            anchors.append(
                SubjectAnchor(
                    subject_key=card.subject_key,
                    title=card.title,
                    status=status if isinstance(status, str) else None,
                    last_active_at=last_active_at,
                )
            )
            if len(anchors) == DEFAULT_MAX_ANCHORS:
                break
        return anchors

    def _staging_context(
        self,
        scope_id: str,
        chunk: list[Record],
    ) -> list[Record]:
        earliest = min(as_utc(record.sent_at) for record in chunk)
        thread_ids = {record.thread_id for record in chunk}
        thread_id = (
            next(iter(thread_ids))
            if len(thread_ids) == 1 and None not in thread_ids
            else None
        )
        candidates = self.store.staged_records(
            scope_id,
            chunk[0].container_id,
            sent_at_from=earliest - timedelta(days=self.staging_retention_days),
            sent_at_before=earliest,
            thread_id=thread_id,
            exclude_record_ids=[record.record_id for record in chunk],
        )
        retained = candidates[-DEFAULT_CONTEXT_MAX_RECORDS:]
        total_chars = sum(len(record.content or "") for record in retained)
        while retained and total_chars > DEFAULT_CONTEXT_MAX_CHARS:
            total_chars -= len(retained[0].content or "")
            retained.pop(0)
        return retained

    def conversation_display_names(self, scope_id: str) -> dict[str, str]:
        """Display names for conversation keys, disambiguated on collisions."""

        return _disambiguated_conversation_names(
            self.store.conversation_names(scope_id)
        )

    def _scope_read_bundle(self, scope_id: str) -> _ScopeReadBundle:
        """Load a scope's read-side state exactly once for one wall request.

        The full assertion scan dominates wall latency; every consumer of a
        single request (matters, rollups, unseen flags) MUST share this
        bundle instead of re-reading the store.
        """

        subjects = self.store.subjects(scope_id)
        raw_assertions = self.store.assertions(scope_id)
        merges = self.store.subject_merges(scope_id)
        return _ScopeReadBundle(
            subjects=subjects,
            canonical_assertions=_canonicalized_assertions(
                raw_assertions, merges
            ),
            graph=(
                project_goal_graph(
                    self.profile, subjects, raw_assertions, merges
                )
                if self._goal_graph_enabled()
                else None
            ),
        )

    def _all_matters(
        self,
        scope_id: str,
        *,
        bundle: _ScopeReadBundle | None = None,
    ) -> list[Matter]:
        """Return every canonical projected matter without touching the LLM."""

        bundle = bundle or self._scope_read_bundle(scope_id)
        result = []
        aliases = self._subject_aliases(scope_id)
        names = self.store.person_names(scope_id)
        evidence_by_subject = {
            subject.subject_key: subject.source_ids
            for subject in bundle.subjects
        }
        conversation_names = self.conversation_display_names(scope_id)
        progress_subjects = {
            predicate.subject
            for predicate in self.profile.predicates
            if predicate.name == "progress"
        }
        updated_at: dict[str, datetime] = {}
        for assertion in bundle.canonical_assertions:
            previous = updated_at.get(assertion.subject_key)
            if previous is None or assertion.recorded_at > previous:
                updated_at[assertion.subject_key] = assertion.recorded_at
        for subject in self.query.list_matters(scope_id):
            current = subject.current
            progress_values = (
                self.query.current(scope_id, subject.subject_key, "progress")
                if subject.subject_type in progress_subjects
                else []
            )
            progress = progress_values[0].value if progress_values else None
            result.append(
                Matter(
                    title=subject.title,
                    status=current.get("status"),
                    progress=progress if isinstance(progress, str) else None,
                    owners=_as_list(current.get("owned_by")),
                    participants=_as_list(current.get("participated_by")),
                    blocked_by=_as_list(current.get("blocked_by")),
                    next_step=current.get("next_step"),
                    due=current.get("due_at"),
                    subject_key=subject.subject_key,
                    aliases=aliases.get(subject.subject_key, []),
                    owners_display=[
                        names.get(str(item), item)
                        for item in _as_list(current.get("owned_by"))
                    ],
                    participants_display=[
                        names.get(str(item), item)
                        for item in _as_list(current.get("participated_by"))
                    ],
                    sources_display=_conversation_labels(
                        scope_id,
                        evidence_by_subject.get(subject.subject_key, frozenset()),
                        names=conversation_names,
                    ),
                    updated_at=updated_at.get(subject.subject_key),
                )
            )
        return result

    def matters(
        self,
        scope_id: str,
        *,
        bundle: _ScopeReadBundle | None = None,
    ) -> list[Matter]:
        """Return root wall matters with deterministic descendant rollups."""

        bundle = bundle or self._scope_read_bundle(scope_id)
        matters = self._all_matters(scope_id, bundle=bundle)
        if not self._goal_graph_enabled() or bundle.graph is None:
            return matters
        graph = bundle.graph
        result = []
        for matter in matters:
            if matter.subject_key in graph.parents:
                continue
            rollup = graph.rollup(matter.subject_key)
            result.append(
                Matter(
                    **{
                        **matter.__dict__,
                        "updated_at": rollup.latest_activity or matter.updated_at,
                        "descendants_total": rollup.descendants_total,
                        "descendants_completed": rollup.descendants_completed,
                        "descendants_blocked": rollup.descendants_blocked,
                        "bubbled_blockers": rollup.bubbled_blockers,
                        "latest_activity": rollup.latest_activity,
                    }
                )
            )
        return result

    def matter_graph(self, scope_id: str, subject_key: str) -> MatterGraph:
        """Project one canonical goal-tree neighborhood without a model call."""

        if not self.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
        canonical = self.canonical_subject_key(scope_id, subject_key)
        try:
            return project_matter_graph(
                scope_id=scope_id,
                subject_key=canonical,
                profile=self.profile,
                subjects=self.store.subjects(scope_id),
                assertions=self.store.assertions(scope_id),
                merges=self.store.subject_merges(scope_id),
            )
        except KeyError as error:
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            ) from error

    def matter_unseen(self, scope_id: str, subject_key: str) -> bool:
        """Return whether a root or any descendant has activity past its watermark."""

        canonical = self.canonical_subject_key(scope_id, subject_key)
        return self.matters_unseen(scope_id).get(canonical, False)

    def matters_unseen(
        self,
        scope_id: str,
        *,
        bundle: _ScopeReadBundle | None = None,
    ) -> dict[str, bool]:
        """Watermark-relative unseen flags for every subject in one scope pass.

        One assertions load, one goal projection, and one watermark read for
        the whole scope; per-matter callers must not re-run these scans.
        """

        bundle = bundle or self._scope_read_bundle(scope_id)
        graph = bundle.graph or self._goal_projection(scope_id)
        watermarks = self.store.read_watermarks(scope_id)
        node_unseen: dict[str, bool] = {}
        for assertion in bundle.canonical_assertions:
            key = assertion.subject_key
            if node_unseen.get(key):
                continue
            watermark = watermarks.get(key)
            node_unseen[key] = (
                watermark is None or assertion.recorded_at > watermark
            )
        result: dict[str, bool] = {}
        for key in set(node_unseen) | set(graph.nodes):
            result[key] = node_unseen.get(key, False) or any(
                node_unseen.get(descendant, False)
                for descendant in graph.descendants(key)
            )
        return result

    def _goal_graph_enabled(self) -> bool:
        names = {item.name for item in self.profile.predicates}
        return {PART_OF, "spawned_from", DECISION}.issubset(names)

    def _goal_projection(self, scope_id: str):
        return project_goal_graph(
            self.profile,
            self.store.subjects(scope_id),
            self.store.assertions(scope_id),
            self.store.subject_merges(scope_id),
        )

    def signals(
        self,
        scope_id: str | None = None,
        *,
        status: str | None = None,
    ) -> list[Signal]:
        if status not in (None, "open", "acked"):
            raise ValueError("signal status MUST be open or acked")
        return self.store.signals(scope_id, status=status)

    def acknowledge_signal(
        self,
        scope_id: str,
        record_id: str,
        kind: str,
        *,
        acked_at: datetime | None = None,
    ) -> Signal:
        with self.store.transaction():
            signal = self.store.acknowledge_signal(
                scope_id,
                record_id,
                kind,
                acked_at=as_utc(acked_at) if acked_at is not None else self._clock(),
            )
        if signal is None:
            raise ResourceNotFoundError(
                f"unknown signal: {scope_id}/{record_id}/{kind}"
            )
        return signal

    def set_seen(
        self,
        scope_id: str,
        subject_key: str,
        *,
        last_seen_at: datetime | None = None,
    ) -> datetime:
        if not self.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
        canonical = self.canonical_subject_key(scope_id, subject_key)
        with self.store.transaction():
            return self.store.set_read_watermark(
                scope_id,
                canonical,
                last_seen_at=(
                    as_utc(last_seen_at)
                    if last_seen_at is not None
                    else self._clock()
                ),
            )

    def hotness(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        scope_ids: list[str] | None = None,
    ) -> list[Any]:
        start = as_utc(window_start)
        end = as_utc(window_end)
        if end <= start:
            raise ValueError("brief window_end MUST be after window_start")
        scopes = self.store.list_scopes() if scope_ids is None else scope_ids
        return self.store.conversation_hotness(
            scopes,
            window_start=start,
            window_end=end,
            min_authors=self.signal_config.hot_min_authors,
            min_messages=self.signal_config.hot_min_messages,
        )

    def brief(
        self,
        window_start: datetime,
        window_end: datetime,
        *,
        console_groups: dict[str, list[str]] | None = None,
        scope_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Project the deterministic zero-model briefing from committed state."""

        start = as_utc(window_start)
        end = as_utc(window_end)
        if end <= start:
            raise ValueError("brief window_end MUST be after window_start")
        scopes = scope_ids if scope_ids is not None else self.store.list_scopes()
        group_for_scope = _brief_group_assignments(console_groups or {}, scopes)
        group_names = sorted(
            {*group_for_scope.values(), *(console_groups or {})},
            key=lambda item: item.encode(),
        )

        matters_by_key: dict[tuple[str, str], Matter] = {}
        root_matters_by_key: dict[tuple[str, str], Matter] = {}
        graph_by_scope = {}
        titles: dict[tuple[str, str], str] = {}
        all_assertions: dict[str, list[Assertion]] = {}
        for scope_id in scopes:
            bundle = self._scope_read_bundle(scope_id)
            scope_matters = self._all_matters(scope_id, bundle=bundle)
            scope_roots = self.matters(scope_id, bundle=bundle)
            graph_by_scope[scope_id] = (
                bundle.graph
                if bundle.graph is not None
                else self._goal_projection(scope_id)
            )
            for matter in scope_matters:
                key = (scope_id, matter.subject_key)
                matters_by_key[key] = matter
                titles[key] = matter.title
            for matter in scope_roots:
                root_matters_by_key[(scope_id, matter.subject_key)] = matter
            all_assertions[scope_id] = bundle.canonical_assertions

        activity = []
        for scope_id in scopes:
            activity.extend(
                _canonicalized_assertions(
                    self.store.brief_assertions(
                        [scope_id],
                        window_start=start,
                        window_end=end,
                    ),
                    self.store.subject_merges(scope_id),
                )
            )
        activity_by_matter: dict[tuple[str, str], list[Assertion]] = {}
        for assertion in activity:
            key = (assertion.scope_id, assertion.subject_key)
            if key in matters_by_key:
                activity_by_matter.setdefault(key, []).append(assertion)
        activity_by_root: dict[tuple[str, str], list[Assertion]] = {}
        for (scope_id, subject_key), assertions in activity_by_matter.items():
            root = graph_by_scope[scope_id].root_for(subject_key)
            activity_by_root.setdefault((scope_id, root), []).extend(assertions)

        completed_values = {
            str(value).casefold()
            for value in (
                self.profile.completion.completed_values
                if self.profile.completion
                else []
            )
        }
        transitions = self.store.brief_events(
            scopes,
            window_start=start,
            window_end=end,
        )
        transition_counts: dict[str, dict[str, int]] = {}
        for event in transitions:
            group_name = group_for_scope[event.scope_id]
            counts = transition_counts.setdefault(
                group_name,
                {"completed": 0, "blocked": 0},
            )
            counts["completed"] += int(
                event.event_type == EventType.matter_completed
            )
            counts["blocked"] += int(
                event.event_type == EventType.status_changed
                and str(event.new_value).casefold() == "blocked"
            )
        groups = []
        for group_name in group_names:
            entries = []
            for (scope_id, subject_key), assertions in activity_by_root.items():
                if group_for_scope.get(scope_id) != group_name:
                    continue
                matter = root_matters_by_key.get((scope_id, subject_key))
                if matter is None:
                    continue
                latest_activity = max(item.recorded_at for item in assertions)
                progress_assertions = [
                    item
                    for item in all_assertions[scope_id]
                    if item.subject_key == subject_key
                    and item.predicate == "progress"
                    and item.operation == Operation.ASSERT
                ]
                latest_progress = (
                    max(
                        progress_assertions,
                        key=lambda item: (
                            item.recorded_at,
                            item.assertion_id.encode("utf-8"),
                        ),
                    ).object_value
                    if progress_assertions
                    else matter.progress
                )
                subtree = [
                    subject_key,
                    *graph_by_scope[scope_id].descendants(subject_key),
                ]
                unseen = 0
                for descendant in subtree:
                    watermark = self.store.read_watermark(
                        scope_id, descendant
                    )
                    unseen += sum(
                        1
                        for item in all_assertions[scope_id]
                        if item.subject_key == descendant
                        and item.recorded_at < end
                        and (
                            item.recorded_at > watermark
                            if watermark is not None
                            else item.recorded_at >= start
                        )
                    )
                entries.append(
                    {
                        "scope_id": scope_id,
                        "subject_key": subject_key,
                        "title": matter.title,
                        "status": matter.status,
                        "progress": (
                            latest_progress
                            if isinstance(latest_progress, str)
                            else None
                        ),
                        "blocker": matter.blocked_by,
                        "unseen": unseen,
                        "latest_activity": latest_activity,
                        "descendants_total": matter.descendants_total,
                        "descendants_completed": matter.descendants_completed,
                        "descendants_blocked": matter.descendants_blocked,
                        "bubbled_blockers": matter.bubbled_blockers or [],
                    }
                )
            entries.sort(
                key=lambda item: (
                    -item["unseen"],
                    -item["latest_activity"].timestamp(),
                    item["subject_key"].encode("utf-8"),
                )
            )
            groups.append(
                {
                    "name": group_name,
                    "counts": {
                        "touched": sum(
                            group_for_scope.get(scope_id) == group_name
                            for scope_id, _ in activity_by_matter
                        ),
                        "completed": transition_counts.get(group_name, {}).get(
                            "completed", 0
                        ),
                        "blocked": transition_counts.get(group_name, {}).get(
                            "blocked", 0
                        ),
                    },
                    "matters": entries,
                }
            )

        needs_me = []
        scope_set = set(scopes)
        open_signals = [
            signal
            for signal in self.store.signals(status="open")
            if signal.scope_id in scope_set
        ]
        open_signals.sort(
            key=lambda item: (
                item.subject_key is None,
                -item.detected_at.timestamp(),
                item.scope_id.encode("utf-8"),
                item.record_id.encode("utf-8"),
                item.kind.encode("utf-8"),
            )
        )
        for signal in open_signals:
            subject_key = (
                self.canonical_subject_key(signal.scope_id, signal.subject_key)
                if signal.subject_key is not None
                else None
            )
            needs_me.append(
                {
                    "type": "signal",
                    "scope_id": signal.scope_id,
                    "subject_key": subject_key,
                    "title": titles.get((signal.scope_id, subject_key)),
                    "signal_kind": signal.kind,
                    "record_id": signal.record_id,
                    "matched_text": signal.matched_text,
                    "detected_at": signal.detected_at,
                    "reason": signal.kind,
                }
            )
        needs_matter_keys: set[tuple[str, str]] = set()
        for key in sorted(
            matters_by_key,
            key=lambda item: (item[0].encode(), item[1].encode()),
        ):
            matter = matters_by_key[key]
            if str(matter.status).casefold() in completed_values:
                continue
            if not _contains_identity_handle(
                [matter.blocked_by, matter.next_step, matter.owners],
                self.signal_config.identity_handles,
            ):
                continue
            root_key = (
                key[0],
                graph_by_scope[key[0]].root_for(key[1]),
            )
            if root_key in needs_matter_keys:
                continue
            root_matter = root_matters_by_key[root_key]
            needs_matter_keys.add(root_key)
            needs_me.append(
                {
                    "type": "matter",
                    "scope_id": root_key[0],
                    "subject_key": root_key[1],
                    "title": root_matter.title,
                    "signal_kind": None,
                    "record_id": None,
                    "matched_text": None,
                    "detected_at": None,
                    "reason": "identity_handle",
                }
            )

        activity_source_ids = {
            ref.source_id for assertion in activity for ref in assertion.source_refs
        }
        quiet = []
        for row in self.hotness(start, end, scope_ids=scopes):
            if any(
                source_id.startswith(f"{row.container_id}:")
                for source_id in activity_source_ids
            ):
                continue
            quiet.append(
                {
                    "group": group_for_scope[row.scope_id],
                    "scope_id": row.scope_id,
                    "container_id": row.container_id,
                    "message_count": row.message_count,
                    "distinct_authors": row.distinct_authors,
                    "reaction_total": row.reaction_total,
                    "hot": row.hot,
                }
            )
        quiet.sort(
            key=lambda item: (
                item["group"].encode(),
                item["scope_id"].encode(),
                item["container_id"].encode(),
            )
        )
        return {"needs_me": needs_me, "groups": groups, "quiet": quiet}

    def related_matters(
        self,
        scope_id: str,
        subject_key: str,
        *,
        limit: int = 5,
    ) -> list[RelatedMatter]:
        """Return deterministic cross-scope links without touching the LLM."""

        if limit < 1:
            raise ValueError("related matter limit MUST be positive")
        if not self.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
        scopes = self.store.list_scopes()
        if len(scopes) > 20:
            return []

        merge_edges = {
            selected_scope: _merge_edges(
                self.store.subject_merges(selected_scope)
            )
            for selected_scope in scopes
        }
        canonical_key = _canonical_subject_key(
            subject_key,
            merge_edges.get(scope_id, {}),
        )
        matters_by_scope = {
            selected_scope: self.query.list_matters(selected_scope)
            for selected_scope in scopes
        }
        selected = next(
            item
            for item in matters_by_scope.get(scope_id, [])
            if item.subject_key == canonical_key
        )
        aliases_by_scope = {
            selected_scope: self._subject_aliases(selected_scope)
            for selected_scope in scopes
        }
        selected_tokens = _normalized_title_tokens(
            selected.title,
            *aliases_by_scope.get(scope_id, {}).get(canonical_key, []),
        )

        handles_by_subject: dict[
            tuple[str, str], set[tuple[str, str]]
        ] = {}
        for handle in self.store.active_subject_handles_across_scopes(scopes):
            key = (
                handle.scope_id,
                _canonical_subject_key(
                    handle.subject_key,
                    merge_edges.get(handle.scope_id, {}),
                ),
            )
            handles_by_subject.setdefault(key, set()).add(
                (handle.handle_type, handle.normalized_value)
            )
        selected_handles = handles_by_subject.get(
            (scope_id, canonical_key), set()
        )

        ranked: list[tuple[bool, float, bytes, bytes, RelatedMatter]] = []
        for candidate_scope in scopes:
            for candidate in matters_by_scope[candidate_scope]:
                if (
                    candidate_scope == scope_id
                    and candidate.subject_key == canonical_key
                ):
                    continue
                candidate_tokens = _normalized_title_tokens(
                    candidate.title,
                    *aliases_by_scope.get(candidate_scope, {}).get(
                        candidate.subject_key, []
                    ),
                )
                score = _jaccard(selected_tokens, candidate_tokens)
                shared_handles = sorted(
                    selected_handles
                    & handles_by_subject.get(
                        (candidate_scope, candidate.subject_key), set()
                    ),
                    key=lambda item: (
                        item[0].encode("utf-8"),
                        item[1].encode("utf-8"),
                    ),
                )
                if not shared_handles and score < 0.5:
                    continue
                via = "title"
                if shared_handles:
                    handle_type, value = shared_handles[0]
                    via = f"handle:{handle_type}:{value}"
                ranked.append(
                    (
                        not bool(shared_handles),
                        -score,
                        candidate_scope.encode("utf-8"),
                        candidate.subject_key.encode("utf-8"),
                        RelatedMatter(
                            scope_id=candidate_scope,
                            subject_key=candidate.subject_key,
                            title=candidate.title,
                            via=via,
                        ),
                    )
                )
        ranked.sort(key=lambda item: item[:4])
        return [item[4] for item in ranked[:limit]]

    def task(self, task_id: str) -> TaskResult:
        row = self.store.task(task_id)
        if row is None:
            raise ResourceNotFoundError(f"unknown task_id: {task_id}")
        return row.result

    def flush(self, scope_id: str) -> FlushReport:
        """Synchronously run all pending extraction and distillation for a scope."""

        self.purge_staging(scope_id)
        pending = self.store.flushable_tasks(
            scope_id,
            max_attempts=MAX_TASK_ATTEMPTS,
        )
        processed: list[str] = []
        for row in pending:
            with self.store.transaction():
                self.store.update_task(row.task_id, status=TaskStatus.running)
            before_assertions = {
                item.assertion_id for item in self.store.assertions(scope_id)
            }
            cards_produced = gate_accepted = handle_conflicts = 0
            unchanged_dropped = 0
            task_routes = _RouteCounts()
            gate_rejected: dict[str, int] = {}
            failed = False
            last_error: str | None = None
            gate_before = self.gate_statistics(scope_id)
            try:
                if row.kind == "messages":
                    record_report = self.add_records(
                        row.payload["records"],
                        scope_id=scope_id,
                        _stage=False,
                    )
                    cards_produced = record_report.cards_accepted
                    gate_accepted += record_report.cards_accepted
                    gate_rejected = _merge_counts(
                        gate_rejected, record_report.drop_reasons
                    )
                    handle_conflicts += record_report.handle_conflicts
                    unchanged_dropped += record_report.unchanged_dropped
                    task_routes.add(
                        _RouteCounts(
                            **{
                                name: getattr(record_report, name)
                                for name in ROUTE_COUNTER_NAMES
                            }
                        )
                    )
                elif row.kind == "cards":
                    cards = [
                        EpisodeCard.model_validate(item)
                        for item in row.payload["cards"]
                    ]
                    cards_produced = sum(
                        self.store.card_payload_hash(scope_id, card.card_id) is None
                        for card in cards
                    )
                    admission_counts = _AdmissionCounts()
                    self._ingest_cards_sync(
                        cards,
                        scope_id=scope_id,
                        admission_counts=admission_counts,
                    )
                    unchanged_dropped += admission_counts.unchanged_dropped
                    gate_accepted += cards_produced
                else:
                    raise ValueError(f"unknown task kind: {row.kind}")

                dream = (
                    self.dream(scope_id)
                    if not self.unified_loop
                    else DreamReport(
                        scope_id=scope_id,
                        queued=0,
                        processed=0,
                        failed=0,
                        accepted_candidates=0,
                        rejected_candidates=0,
                        new_assertions=0,
                        new_subjects=0,
                        remaining=0,
                    )
                )
                gate_accepted += (
                    record_report.assertions_emitted
                    if row.kind == "messages" and self.unified_loop
                    else dream.accepted_candidates
                )
                gate_after = self.gate_statistics(scope_id)
                gate_rejected = _merge_counts(
                    gate_rejected,
                    {
                        reason: count - gate_before.rejections.get(reason, 0)
                        for reason, count in gate_after.rejections.items()
                        if count - gate_before.rejections.get(reason, 0)
                    },
                )
                failed = dream.failed > 0
                if failed:
                    last_error = (
                        "DreamFailure: semantic distillation failed for "
                        f"{dream.failed} queue item(s)"
                    )
            except Exception as error:  # noqa: BLE001
                failed = True
                last_error = _task_error_summary(error)

            after_assertions = {
                item.assertion_id for item in self.store.assertions(scope_id)
            }
            with self.store.transaction():
                self.store.update_task(
                    row.task_id,
                    status=TaskStatus.failed if failed else TaskStatus.completed,
                    cards_produced=cards_produced,
                    new_assertions=len(after_assertions - before_assertions),
                    unchanged_dropped=unchanged_dropped,
                    gate_accepted=gate_accepted,
                    gate_rejected=gate_rejected,
                    handle_conflicts=handle_conflicts,
                    route_counts=task_routes.to_dict(),
                    last_error=last_error,
                )
            processed.append(row.task_id)
        if (
            not self.unified_loop
            and not pending
            and self.store.distill_queue_count(scope_id)
        ):
            self.dream(scope_id)
        return FlushReport(
            scope_id=scope_id,
            tasks_processed=len(processed),
            task_ids=processed,
            remaining=len(
                self.store.flushable_tasks(
                    scope_id,
                    max_attempts=MAX_TASK_ATTEMPTS,
                )
            ),
        )

    def _resolve_min_batch(self, override: int | None) -> int:
        value = (
            override
            if override is not None
            else getattr(self, "min_batch_messages", DEFAULT_MIN_BATCH_MESSAGES)
        )
        if value < 1:
            raise ValueError("min_batch_messages MUST be positive")
        return value

    def flush_quiet(
        self,
        quiet_period_minutes: float = 10,
        *,
        max_batch_delay_minutes: float | None = None,
        min_batch_messages: int | None = None,
    ) -> list[FlushReport]:
        if quiet_period_minutes < 0:
            raise ValueError("quiet_period_minutes MUST be non-negative")
        maximum_delay = validate_max_batch_delay_minutes(
            self.max_batch_delay_minutes
            if max_batch_delay_minutes is None
            else max_batch_delay_minutes
        )
        reference = self._clock()
        quiet_cutoff = reference - timedelta(minutes=quiet_period_minutes)
        delay_cutoff = reference - timedelta(minutes=maximum_delay)
        return [
            self.flush(scope_id)
            for scope_id in self.store.quiet_scopes(
                quiet_cutoff,
                delay_cutoff=delay_cutoff,
                min_accepted=self._resolve_min_batch(min_batch_messages),
                max_attempts=MAX_TASK_ATTEMPTS,
            )
        ]

    def flush_quiet_at(
        self,
        quiet_period_minutes: float,
        instant: datetime,
        *,
        max_batch_delay_minutes: float | None = None,
        min_batch_messages: int | None = None,
    ) -> list[FlushReport]:
        if quiet_period_minutes < 0:
            raise ValueError("quiet_period_minutes MUST be non-negative")
        maximum_delay = validate_max_batch_delay_minutes(
            self.max_batch_delay_minutes
            if max_batch_delay_minutes is None
            else max_batch_delay_minutes
        )
        reference = as_utc(instant)
        quiet_cutoff = reference - timedelta(minutes=quiet_period_minutes)
        delay_cutoff = reference - timedelta(minutes=maximum_delay)
        return [
            self.flush(scope_id)
            for scope_id in self.store.quiet_scopes(
                quiet_cutoff,
                delay_cutoff=delay_cutoff,
                min_accepted=self._resolve_min_batch(min_batch_messages),
                max_attempts=MAX_TASK_ATTEMPTS,
            )
        ]

    def flush_pending(self) -> list[FlushReport]:
        return [
            self.flush(scope_id)
            for scope_id in self.store.pending_scopes(
                max_attempts=MAX_TASK_ATTEMPTS,
            )
        ]

    def now(self) -> datetime:
        return self._clock()

    def purge_staging(
        self,
        scope_id: str,
        *,
        as_of: datetime | None = None,
    ) -> int:
        if not scope_id:
            raise ValueError("scope_id is required")
        reference = as_utc(as_of) if as_of is not None else self._clock()
        cutoff = reference - timedelta(days=self.staging_retention_days)
        with self.store.transaction():
            return self.store.purge_staged_records(scope_id, before=cutoff)

    def _enqueue_task(
        self,
        *,
        scope_id: str,
        kind: str,
        payload: dict[str, Any],
        accepted: int,
        newest_message_at: datetime | None,
        staged_records: list[Record] | None = None,
    ) -> TaskReceipt:
        created_at = self._clock()
        if newest_message_at is not None:
            # Source clocks skew. A future-stamped message must count as
            # "arrived now" for quiet-period purposes, or one bad timestamp
            # freezes the whole scope's distillation until that instant.
            newest_message_at = min(as_utc(newest_message_at), as_utc(created_at))
        nonce = 0
        while True:
            task_id = "task_" + stable_hash(
                [
                    scope_id,
                    kind,
                    payload,
                    instant_text(created_at),
                    nonce,
                ]
            )
            with self.store.transaction():
                if staged_records:
                    self._stage_and_detect(
                        scope_id,
                        staged_records,
                        staged_at=created_at,
                    )
                inserted = self.store.create_task(
                    task_id=task_id,
                    scope_id=scope_id,
                    kind=kind,
                    payload=payload,
                    accepted=accepted,
                    created_at=created_at,
                    newest_message_at=newest_message_at,
                )
            if inserted:
                return TaskReceipt(accepted=accepted, task_id=task_id)
            nonce += 1

    def _stage_and_detect(
        self,
        scope_id: str,
        records: list[Record],
        *,
        staged_at: datetime,
    ) -> None:
        self.store.stage_records(scope_id, records, staged_at=staged_at)
        for record in records:
            detector_matches: list[tuple[str, str]] = []
            mention = best_token_match(
                record.content,
                self.signal_config.identity_handles,
                digit_prefixes="@:",
            )
            if mention is not None:
                detector_matches.append(("mention_of_self", mention[1]))
            sender_match = first_pattern_match(
                record.author.id,
                self.signal_config.machine_senders,
            )
            alert_match = first_pattern_match(
                record.content,
                self.signal_config.alert_keywords,
            )
            if sender_match is not None and alert_match is not None:
                detector_matches.append(("machine_alert", alert_match))
            for kind, matched_text in detector_matches:
                self.store.add_signal(
                    Signal(
                        scope_id=scope_id,
                        record_id=record.record_id,
                        kind=kind,
                        detected_at=staged_at,
                        matched_text=matched_text,
                        subject_key=self._critical_signal_subject(
                            scope_id,
                            record.content,
                        ),
                    )
                )

    def _critical_signal_subject(
        self,
        scope_id: str,
        content: str,
    ) -> str | None:
        matches: list[tuple[int, bytes, str]] = []
        for handle in self.store.active_subject_handles(scope_id):
            matched = best_token_match(
                content,
                [handle.normalized_value],
                digit_prefixes="@:#",
            )
            if matched is None:
                continue
            subject_key = self.canonical_subject_key(
                scope_id,
                handle.subject_key,
            )
            matches.append(
                (
                    -len(handle.normalized_value),
                    subject_key.encode("utf-8"),
                    subject_key,
                )
            )
        return min(matches)[2] if matches else None

    def sync_positions(self, scope_id: str):
        return self.store.sync_positions(scope_id)

    def _structure_rejection(
        self,
        assertion: Assertion,
    ) -> StructureRejection | None:
        target = assertion.object_value
        outside = False
        if isinstance(target, str):
            outside = any(
                scope_id != assertion.scope_id
                and any(
                    subject.subject_key == target
                    for subject in self.store.subjects(scope_id)
                )
                for scope_id in self.store.list_scopes()
            )
        return structure_rejection(
            assertion,
            profile=self.profile,
            subjects=self.store.subjects(assertion.scope_id),
            assertions=self.store.assertions(assertion.scope_id),
            merges=self.store.subject_merges(assertion.scope_id),
            target_exists_outside_scope=outside,
        )

    def _record_structure_rejection(
        self,
        scope_id: str,
        reason: StructureRejection,
    ) -> None:
        with self.store.transaction():
            self.store.record_gate_report(
                scope_id,
                accepted=0,
                rejections={reason.value: 1},
            )

    def correct(self, correction: Correction | dict[str, Any]) -> Assertion:
        item = (
            correction
            if isinstance(correction, Correction)
            else Correction.model_validate(correction)
        )
        definition = self.profile.predicate(item.predicate)
        if definition.subject != item.subject_type:
            raise ValueError("predicate is not registered for correction subject type")
        if definition.cardinality.value == "APPEND" and item.operation == Operation.RETRACT:
            raise ValueError("APPEND predicates cannot be retracted")
        subjects = {
            subject.subject_key: subject
            for subject in self.store.subjects(item.scope_id)
        }
        if item.subject_key not in subjects:
            raise ValueError("correction subject does not exist")
        canonical_subject_key = self.canonical_subject_key(
            item.scope_id,
            item.subject_key,
        )
        value_key = item.object_key
        if value_key is None:
            value_key = (
                object_key(item.object_value)
                if item.operation == Operation.ASSERT
                else FIELD_WIDE_RETRACT
            )
        assertion = Assertion(
            assertion_id=derive_assertion_id(
                item.scope_id,
                canonical_subject_key,
                item.predicate,
                item.operation,
                value_key,
                as_utc(item.valid_from),
                item.source_refs,
            ),
            scope_id=item.scope_id,
            subject_key=canonical_subject_key,
            subject_type=item.subject_type,
            predicate=item.predicate,
            operation=item.operation,
            object_value=item.object_value,
            object_key=value_key,
            valid_from=as_utc(item.valid_from),
            recorded_at=self._clock(),
            source_refs=item.source_refs,
            origin=Origin.human,
        )
        rejection = self._structure_rejection(assertion)
        if rejection is not None:
            self._record_structure_rejection(item.scope_id, rejection)
            raise ValueError(
                f"{rejection.value}: rejected {item.predicate} edge"
            )
        with self.store.transaction():
            for source_ref in item.source_refs:
                self.store.observe_source(item.scope_id, source_ref)
            self.store.add_assertion(assertion)
            self._rebuild(item.scope_id)
        return assertion

    def merge_subjects(
        self,
        scope_id: str,
        source_subject_key: str,
        target_subject_key: str,
        *,
        source_refs: list[SourceRef],
        valid_from: datetime,
    ) -> ChangeEvent:
        refs = [
            ref if isinstance(ref, SourceRef) else SourceRef.model_validate(ref)
            for ref in source_refs
        ]
        merge = SubjectMerge(
            scope_id=scope_id,
            source_subject_key=source_subject_key,
            target_subject_key=target_subject_key,
            source_refs=refs,
            valid_from=valid_from,
        )
        merge = merge.model_copy(
            update={"valid_from": as_utc(merge.valid_from)}
        )
        with self.store.transaction():
            subjects = {
                subject.subject_key: subject
                for subject in self.store.subjects(scope_id)
            }
            if source_subject_key not in subjects:
                raise ResourceNotFoundError(
                    "source subject does not exist in scope"
                )
            if target_subject_key not in subjects:
                raise ResourceNotFoundError(
                    "target subject does not exist in scope"
                )
            if source_subject_key == target_subject_key:
                raise SubjectMergeConflictError(
                    "source and target subjects MUST differ"
                )
            edges = _merge_edges(self.store.subject_merges(scope_id))
            if source_subject_key in edges:
                raise SubjectMergeConflictError(
                    "source subject is already merged; unmerge it first"
                )
            if (
                _canonical_subject_key(target_subject_key, edges)
                == source_subject_key
            ):
                raise SubjectMergeConflictError(
                    "subject merge would create a cycle"
                )
            for source_ref in refs:
                self.store.observe_source(scope_id, source_ref)
            recorded_at = self._clock()
            self.store.add_subject_merge(merge)
            self._rebuild(scope_id)
            event = _subject_merge_event(
                event_type=EventType.subject_merged,
                merge=merge,
                recorded_at=recorded_at,
            )
            self.store.add_event(event)
        return event

    def unmerge_subjects(
        self,
        scope_id: str,
        source_subject_key: str,
        *,
        source_refs: list[SourceRef],
        valid_from: datetime,
    ) -> ChangeEvent:
        refs = [
            ref if isinstance(ref, SourceRef) else SourceRef.model_validate(ref)
            for ref in source_refs
        ]
        if not refs:
            raise ValueError("subject unmerges MUST have source_refs")
        with self.store.transaction():
            active = {
                merge.source_subject_key: merge
                for merge in self.store.subject_merges(scope_id)
            }.get(source_subject_key)
            if active is None:
                raise SubjectMergeConflictError(
                    "source subject is not actively merged"
                )
            for source_ref in refs:
                self.store.observe_source(scope_id, source_ref)
            recorded_at = self._clock()
            if not self.store.remove_subject_merge(scope_id, source_subject_key):
                raise SubjectMergeConflictError(
                    "source subject is not actively merged"
                )
            self._rebuild(scope_id)
            unmerge = SubjectMerge(
                scope_id=scope_id,
                source_subject_key=source_subject_key,
                target_subject_key=active.target_subject_key,
                source_refs=refs,
                valid_from=valid_from,
            )
            unmerge = unmerge.model_copy(
                update={"valid_from": as_utc(unmerge.valid_from)}
            )
            event = _subject_merge_event(
                event_type=EventType.subject_unmerged,
                merge=unmerge,
                recorded_at=recorded_at,
            )
            self.store.add_event(event)
        return event

    def canonical_subject_key(self, scope_id: str, subject_key: str) -> str:
        return _canonical_subject_key(
            subject_key,
            _merge_edges(self.store.subject_merges(scope_id)),
        )

    def _subject_aliases(self, scope_id: str) -> dict[str, list[str]]:
        subjects = {
            subject.subject_key: subject
            for subject in self.store.subjects(scope_id)
        }
        edges = _merge_edges(self.store.subject_merges(scope_id))
        aliases: dict[str, set[str]] = {}
        for source_key in edges:
            target_key = _canonical_subject_key(source_key, edges)
            source = subjects[source_key]
            target = subjects[target_key]
            if source.title != target.title:
                aliases.setdefault(target_key, set()).add(source.title)
        return {
            key: sorted(values, key=lambda value: value.encode("utf-8"))
            for key, values in aliases.items()
        }

    def replay(self, scope_id: str) -> ReplayReport:
        with self.store.transaction():
            emitted = self._rebuild(scope_id)
        return ReplayReport(
            scope_id=scope_id,
            intervals=len(self.store.intervals(scope_id)),
            memory_cards=len(self.store.memory_cards(scope_id)),
            events_emitted=len(emitted),
        )

    def scope_exists(self, scope_id: str) -> bool:
        return self.store.scope_exists(scope_id)

    def subject_exists(self, scope_id: str, subject_key: str) -> bool:
        return any(
            subject.subject_key == subject_key
            for subject in self.store.subjects(scope_id)
        )

    def events(
        self, scope_id: str, *, since: datetime | str | None = None
    ) -> list[ChangeEvent]:
        parsed_since = (
            datetime.fromisoformat(since)
            if isinstance(since, str)
            else since
        )
        return self.store.events(scope_id, since=parsed_since)

    def export(self, scope_id: str) -> ExportEnvelope:
        if not self.scope_exists(scope_id):
            raise ResourceNotFoundError(f"unknown scope_id: {scope_id}")
        return ExportEnvelope(
            scope_id=scope_id,
            schema_profile=ExportSchemaProfile(
                id=self.profile.schema_id,
                version=_profile_version(self.profile),
            ),
            subjects=[
                ExportSubject(
                    scope_id=item.scope_id,
                    subject_key=item.subject_key,
                    subject_type=item.subject_type,
                    title=item.title,
                    normalized_title=item.normalized_title,
                    source_ids=sorted(item.source_ids),
                    parent_subject_key=item.parent_subject_key,
                    thread_ids=sorted(item.thread_ids),
                )
                for item in self.store.subjects(scope_id)
            ],
            assertions=self.store.assertions(scope_id),
            source_states=[
                ExportSourceState(
                    source_id=item.source_id,
                    uri=item.uri,
                    revoked_at=item.revoked_at,
                )
                for item in self.store.source_metadata(scope_id)
            ],
            events=self.store.events(scope_id),
            merges=self.store.subject_merges(scope_id),
        )

    def import_snapshot(
        self, envelope: ExportEnvelope | dict[str, Any]
    ) -> ImportReport:
        snapshot = (
            envelope
            if isinstance(envelope, ExportEnvelope)
            else ExportEnvelope.model_validate(envelope)
        )
        if snapshot.schema_profile.id != self.profile.schema_id:
            raise ImportRefusedError(
                "export requires unavailable local schema profile "
                f"{snapshot.schema_profile.id!r}; active profile is "
                f"{self.profile.schema_id!r}"
            )
        local_version = _profile_version(self.profile)
        if snapshot.schema_profile.version != local_version:
            raise ImportRefusedError(
                "export schema profile version is not available locally: "
                f"{snapshot.schema_profile.id}@{snapshot.schema_profile.version}"
            )
        if self.scope_exists(snapshot.scope_id):
            raise ImportRefusedError(
                f"import target scope {snapshot.scope_id!r} MUST be empty"
            )
        with self.store.transaction():
            for item in snapshot.subjects:
                if item.scope_id != snapshot.scope_id:
                    raise ImportRefusedError(
                        "export subject scope_id does not match envelope scope_id"
                    )
                self.store.upsert_subject(
                    SubjectRecord(
                        scope_id=item.scope_id,
                        subject_key=item.subject_key,
                        subject_type=item.subject_type,
                        title=item.title,
                        normalized_title=item.normalized_title,
                        source_ids=frozenset(item.source_ids),
                        parent_subject_key=item.parent_subject_key,
                        thread_ids=frozenset(item.thread_ids),
                    )
                )
            for assertion in snapshot.assertions:
                if assertion.scope_id != snapshot.scope_id:
                    raise ImportRefusedError(
                        "export assertion scope_id does not match envelope scope_id"
                    )
                self.store.add_assertion(assertion)
            for source in snapshot.source_states:
                self.store.put_source_state(
                    snapshot.scope_id,
                    EvidenceRef(
                        source_id=source.source_id,
                        uri=source.uri,
                        status=(
                            EvidenceStatus.revoked
                            if source.revoked_at is not None
                            else EvidenceStatus.active
                        ),
                        revoked_at=source.revoked_at,
                    ),
                )
            subject_keys = {
                subject.subject_key for subject in self.store.subjects(snapshot.scope_id)
            }
            merge_edges: dict[str, str] = {}
            for merge in snapshot.merges:
                if merge.scope_id != snapshot.scope_id:
                    raise ImportRefusedError(
                        "export merge scope_id does not match envelope scope_id"
                    )
                if (
                    merge.source_subject_key not in subject_keys
                    or merge.target_subject_key not in subject_keys
                ):
                    raise ImportRefusedError(
                        "export merge references an unknown subject"
                    )
                if merge.source_subject_key in merge_edges:
                    raise ImportRefusedError(
                        "export contains duplicate active subject merges"
                    )
                merge_edges[merge.source_subject_key] = merge.target_subject_key
                try:
                    _canonical_subject_key(
                        merge.source_subject_key,
                        merge_edges,
                    )
                except ValueError as error:
                    raise ImportRefusedError(str(error)) from error
                self.store.add_subject_merge(merge)
            self._rebuild(snapshot.scope_id, emit_events=False)
            for event in snapshot.events:
                if event.scope_id != snapshot.scope_id:
                    raise ImportRefusedError(
                        "export event scope_id does not match envelope scope_id"
                    )
                self.store.add_event(event)
        return ImportReport(
            scope_id=snapshot.scope_id,
            subjects=len(snapshot.subjects),
            assertions=len(snapshot.assertions),
            events=len(snapshot.events),
            intervals=len(self.store.intervals(snapshot.scope_id)),
            memory_cards=len(self.store.memory_cards(snapshot.scope_id)),
        )

    def projection_statistics(self, scope_id: str):
        return self.store.projection_stats(scope_id)

    def gate_statistics(self, scope_id: str):
        return self.store.gate_statistics(scope_id)

    def dream(self, scope_id: str, limit: int | None = None) -> DreamReport:
        if limit is not None and limit < 0:
            raise ValueError("limit MUST be non-negative")
        queued = self.store.distill_queue_count(scope_id)
        items = self.store.distill_queue(scope_id, limit)
        processed = failed = accepted_count = rejected_count = new_assertions = 0
        new_subjects = 0
        for item in items:
            prompt_subject_key = self.canonical_subject_key(
                scope_id,
                item.subject_key,
            )
            prompt = build_prompt(
                self.profile,
                item.card,
                subject_key=prompt_subject_key,
                subject_type=item.subject_type,
            )
            try:
                raw = self._write_gateway.complete(
                    system=prompt.system,
                    user=prompt.user,
                    response_schema=prompt.response_schema,
                )
                try:
                    decoded = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    restored_raw = raw
                else:
                    restored_raw = json.dumps(
                        restore_source_aliases(
                            decoded,
                            collection_key="candidates",
                            aliases=prompt.source_aliases,
                            available_source_ids=(
                                ref.source_id for ref in item.card.source_refs
                            ),
                        ),
                        ensure_ascii=False,
                    )
                gate = validate_response(
                    restored_raw,
                    card=item.card,
                    profile=self.profile,
                    subjects=self.store.subjects(scope_id),
                )
                item_new_assertions = 0
                item_new_subjects = 0
                structure_rejections: dict[str, int] = {}
                with self.store.transaction():
                    for accepted in gate.accepted:
                        candidate = accepted.candidate
                        assertion_subject_key = self.canonical_subject_key(
                            scope_id,
                            candidate.subject_key,
                        )
                        if (
                            candidate.parent_subject_key is not None
                            and candidate.subject_title is not None
                        ):
                            current_subjects = {
                                subject.subject_key: subject
                                for subject in self.store.subjects(scope_id)
                            }
                            existing_subject = current_subjects.get(
                                candidate.subject_key
                            )
                            source_ids = frozenset(
                                ref.source_id for ref in accepted.source_refs
                            )
                            if existing_subject is None:
                                self.store.upsert_subject(
                                    SubjectRecord(
                                        scope_id=scope_id,
                                        subject_key=candidate.subject_key,
                                        subject_type=candidate.subject_type,
                                        title=candidate.subject_title,
                                        normalized_title=normalize_title(
                                            candidate.subject_title
                                        ),
                                        source_ids=source_ids,
                                        parent_subject_key=candidate.parent_subject_key,
                                    )
                                )
                                item_new_subjects += 1
                            else:
                                self.store.upsert_subject(
                                    SubjectRecord(
                                        scope_id=existing_subject.scope_id,
                                        subject_key=existing_subject.subject_key,
                                        subject_type=existing_subject.subject_type,
                                        title=existing_subject.title,
                                        normalized_title=existing_subject.normalized_title,
                                        source_ids=existing_subject.source_ids
                                        | source_ids,
                                        parent_subject_key=(
                                            existing_subject.parent_subject_key
                                        ),
                                        thread_ids=existing_subject.thread_ids,
                                    )
                                )
                        value_key = (
                            object_key(candidate.object_value)
                            if candidate.operation == Operation.ASSERT
                            or candidate.object_value is not None
                            else FIELD_WIDE_RETRACT
                        )
                        assertion = Assertion(
                            assertion_id=derive_assertion_id(
                                scope_id,
                                assertion_subject_key,
                                candidate.predicate,
                                candidate.operation,
                                value_key,
                                as_utc(candidate.valid_from),
                                accepted.source_refs,
                            ),
                            scope_id=scope_id,
                            subject_key=assertion_subject_key,
                            subject_type=candidate.subject_type,
                            predicate=candidate.predicate,
                            operation=candidate.operation,
                            object_value=candidate.object_value,
                            object_key=value_key,
                            valid_from=as_utc(candidate.valid_from),
                            recorded_at=datetime.max.replace(tzinfo=UTC),
                            source_refs=accepted.source_refs,
                            origin=Origin.model,
                        )
                        rejection = self._structure_rejection(assertion)
                        if rejection is not None:
                            structure_rejections[rejection.value] = (
                                structure_rejections.get(rejection.value, 0) + 1
                            )
                            continue
                        assertion = assertion.model_copy(
                            update={"recorded_at": self._clock()}
                        )
                        if self.store.add_assertion(assertion):
                            item_new_assertions += 1
                    admitted_count = (
                        gate.accepted_count - sum(structure_rejections.values())
                    )
                    self.store.record_gate_report(
                        scope_id,
                        accepted=admitted_count,
                        rejections=_merge_counts(
                            gate.rejection_counts,
                            structure_rejections,
                        ),
                    )
                    self.store.remove_distill_item(scope_id, item.card_id)
                    self._rebuild(scope_id)
            # A failed queue item must be retained regardless of failure type.
            except Exception as error:  # noqa: BLE001
                with self.store.transaction():
                    self.store.fail_distill_item(
                        scope_id, item.card_id, f"{type(error).__name__}: {error}"
                    )
                failed += 1
                continue

            processed += 1
            accepted_count += admitted_count
            rejected_count += gate.rejected_count + sum(
                structure_rejections.values()
            )
            new_assertions += item_new_assertions
            new_subjects += item_new_subjects
        return DreamReport(
            scope_id=scope_id,
            queued=queued,
            processed=processed,
            failed=failed,
            accepted_candidates=accepted_count,
            rejected_candidates=rejected_count,
            new_assertions=new_assertions,
            new_subjects=new_subjects,
            remaining=self.store.distill_queue_count(scope_id),
        )

    def _rebuild(
        self, scope_id: str, *, emit_events: bool = True
    ) -> list[ChangeEvent]:
        previous = self.store.intervals(scope_id)
        subjects = self.store.subjects(scope_id)
        merges = self.store.subject_merges(scope_id)
        assertions = _canonicalized_assertions(
            self.store.assertions(scope_id),
            merges,
        )
        intervals, stats = project_assertions(assertions, self.profile)
        cards = materialize(
            _canonicalized_subjects(subjects, merges),
            intervals,
            self.profile,
        )
        self.store.replace_projection(scope_id, intervals, cards, stats)
        if not emit_events:
            return []
        candidates = derive_change_events(
            previous,
            intervals,
            assertions,
            self.profile,
        )
        return [event for event in candidates if self.store.add_event(event)]


def _merge_edges(merges: Iterable[SubjectMerge]) -> dict[str, str]:
    return {
        merge.source_subject_key: merge.target_subject_key
        for merge in merges
    }


def _canonical_subject_key(subject_key: str, edges: dict[str, str]) -> str:
    current = subject_key
    visited: set[str] = set()
    while current in edges:
        if current in visited:
            raise ValueError("subject merge graph contains a cycle")
        visited.add(current)
        current = edges[current]
    return current


def _normalized_title_tokens(*values: str) -> frozenset[str]:
    return frozenset(
        token
        for token in normalize_title(" ".join(values)).split()
        if token
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _canonicalized_subject(
    subject: SubjectRecord,
    existing: list[SubjectRecord],
    merges: list[SubjectMerge],
) -> SubjectRecord:
    edges = _merge_edges(merges)
    target_key = _canonical_subject_key(subject.subject_key, edges)
    if target_key == subject.subject_key:
        return subject
    target = next(item for item in existing if item.subject_key == target_key)
    return SubjectRecord(
        scope_id=target.scope_id,
        subject_key=target.subject_key,
        subject_type=target.subject_type,
        title=target.title,
        normalized_title=target.normalized_title,
        source_ids=target.source_ids | subject.source_ids,
        parent_subject_key=target.parent_subject_key,
        thread_ids=target.thread_ids | subject.thread_ids,
    )


def _canonicalized_subjects(
    subjects: list[SubjectRecord],
    merges: list[SubjectMerge],
) -> list[SubjectRecord]:
    edges = _merge_edges(merges)
    by_key = {subject.subject_key: subject for subject in subjects}
    grouped: dict[str, list[SubjectRecord]] = {}
    for subject in subjects:
        canonical_key = _canonical_subject_key(subject.subject_key, edges)
        grouped.setdefault(canonical_key, []).append(subject)

    result = []
    for canonical_key, merged_subjects in grouped.items():
        canonical = by_key[canonical_key]
        source_ids = frozenset(
            source_id
            for subject in merged_subjects
            for source_id in subject.source_ids
        )
        thread_ids = frozenset(
            thread_id
            for subject in merged_subjects
            for thread_id in subject.thread_ids
        )
        parent_subject_key = canonical.parent_subject_key
        if parent_subject_key is not None:
            parent_subject_key = _canonical_subject_key(
                parent_subject_key,
                edges,
            )
        result.append(
            SubjectRecord(
                scope_id=canonical.scope_id,
                subject_key=canonical.subject_key,
                subject_type=canonical.subject_type,
                title=canonical.title,
                normalized_title=canonical.normalized_title,
                source_ids=source_ids,
                parent_subject_key=parent_subject_key,
                thread_ids=thread_ids,
            )
        )
    return sorted(
        result,
        key=lambda item: (
            item.scope_id.encode("utf-8"),
            item.subject_key.encode("utf-8"),
        ),
    )


def _canonicalized_assertions(
    assertions: list[Assertion],
    merges: list[SubjectMerge],
) -> list[Assertion]:
    return canonicalize_graph_assertions(assertions, merges)


def _subject_merge_event(
    *,
    event_type: EventType,
    merge: SubjectMerge,
    recorded_at: datetime,
) -> ChangeEvent:
    source_ids = list(
        dict.fromkeys(ref.source_id for ref in merge.source_refs)
    )
    event_id = stable_hash(
        [
            event_type.value,
            merge.scope_id,
            merge.source_subject_key,
            merge.target_subject_key,
            instant_text(merge.valid_from),
            [
                ref.model_dump(mode="json")
                for ref in merge.source_refs
            ],
        ]
    )
    merged = event_type == EventType.subject_merged
    return ChangeEvent(
        event_id=event_id,
        event_type=event_type,
        scope_id=merge.scope_id,
        subject_key=merge.source_subject_key,
        predicate=SUBJECT_MERGE_PREDICATE,
        old_value=None if merged else merge.target_subject_key,
        new_value=merge.target_subject_key if merged else None,
        valid_from=merge.valid_from,
        recorded_at=recorded_at,
        origin=Origin.human,
        source_ids=source_ids,
    )


def _db_path(store: str | Path) -> str | Path:
    if isinstance(store, Path):
        return store
    if store.startswith("sqlite:///"):
        return store.removeprefix("sqlite:///")
    return store


def _resolve_store(store: str | Path | Store) -> Store:
    if not isinstance(store, (str, Path)):
        return store
    value = str(store)
    if value.startswith(("postgresql://", "postgres://")):
        from matterhorn.store.postgres import PostgresStore

        return PostgresStore(value)
    return SQLiteStore(_db_path(store))


def _clock_callable(
    clock: Clock | Iterable[datetime] | None,
) -> Clock:
    if clock is None:
        return lambda: datetime.now(UTC)
    if callable(clock):
        return lambda: as_utc(clock())
    iterator: Iterator[datetime] = iter(clock)
    return lambda: as_utc(next(iterator))


def validate_staging_retention_days(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError("staging retention days MUST be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "staging retention days MUST be a positive finite number"
        ) from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("staging retention days MUST be a positive finite number")
    return parsed


def validate_max_batch_delay_minutes(value: object) -> float:
    if isinstance(value, bool):
        raise TypeError("max batch delay minutes MUST be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "max batch delay minutes MUST be a positive finite number"
        ) from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("max batch delay minutes MUST be a positive finite number")
    return parsed


_OPAQUE_SEGMENT = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$|^[0-9a-f]{16,}$")


def _disambiguated_conversation_names(names: dict[str, str]) -> dict[str, str]:
    """Names are display only; identity is always the conversation key.

    Two different conversations may share one display name — render each with
    a short key suffix in that case so the wall never LOOKS merged when the
    data is not.
    """

    counts: dict[str, int] = {}
    for display in names.values():
        counts[display] = counts.get(display, 0) + 1
    return {
        key: (
            display
            if counts[display] == 1
            else f"{display}({stable_hash([key])[:6]})"
        )
        for key, display in names.items()
    }


def _shorten_opaque_segments(key: str) -> str:
    return ":".join(
        f"{part[:8]}…" if _OPAQUE_SEGMENT.match(part) else part
        for part in key.split(":")
    )


def _conversation_labels(
    scope_id: str,
    source_ids: frozenset[str] | set[str],
    *,
    names: dict[str, str] | None = None,
    limit: int = 3,
) -> list[str]:
    """Deterministic source-conversation labels from evidence record ids.

    Record ids follow "<scope>:<conversation>:<native_id>"; corrections and
    other non-record provenance (single-segment after scope-strip) are
    skipped. Zero-model, display only.
    """

    labels = set()
    prefix = f"{scope_id}:"
    for source_id in source_ids:
        rest = source_id.removeprefix(prefix)
        cut = rest.rfind(":")
        if cut <= 0:
            continue
        key = rest[:cut]
        # Hook message ids embed the conversation prefix, so a record id can
        # yield an "X:X" doubled key; collapse it back to the conversation.
        segments = key.split(":")
        half = len(segments) // 2
        if half and len(segments) == half * 2 and segments[:half] == segments[half:]:
            key = ":".join(segments[:half])
        named = (names or {}).get(key)
        labels.add(named if named else _shorten_opaque_segments(key))
    ordered = sorted(labels, key=lambda item: item.encode("utf-8"))
    if len(ordered) > limit:
        return ordered[:limit] + [f"+{len(ordered) - limit}"]
    return ordered


def _record_observed_at(record: Record) -> datetime:
    return max(
        instant
        for instant in (record.sent_at, record.edited_at, record.revoked_at)
        if instant is not None
    )


def _conversation_extraction_chunks(
    records: list[Record], batch_size: int
) -> list[list[Record]]:
    """Order conversation units and pack whole matter boundaries into chunks."""

    if batch_size < 1:
        raise ValueError("batch_size MUST be positive")

    by_container: dict[str, list[Record]] = {}
    for record in records:
        by_container.setdefault(record.container_id, []).append(record)

    units = sorted(
        by_container.items(),
        key=lambda item: (
            min(as_utc(record.sent_at) for record in item[1]),
            item[0].encode("utf-8"),
        ),
    )
    chunks: list[list[Record]] = []
    for _, unit_records in units:
        ordered = sorted(
            unit_records,
            key=lambda record: (
                as_utc(record.sent_at),
                record.record_id.encode("utf-8"),
            ),
        )
        by_boundary: dict[str, list[Record]] = {}
        for record in ordered:
            by_boundary.setdefault(record.matter_boundary, []).append(record)

        current: list[Record] = []
        for boundary_records in by_boundary.values():
            if len(boundary_records) > batch_size:
                if current:
                    chunks.append(current)
                    current = []
                chunks.append(boundary_records)
                continue
            if current and len(current) + len(boundary_records) > batch_size:
                chunks.append(current)
                current = []
            current.extend(boundary_records)
        if current:
            chunks.append(current)
    return chunks


def _message_to_record(scope_id: str, message: Message) -> Record:
    container_id = (
        f"{scope_id}:{message.conversation_id}"
        if message.conversation_id is not None
        else scope_id
    )
    return Record.model_validate(
        {
            "record_id": f"{container_id}:{message.id}",
            "container_id": container_id,
            "thread_id": (
                f"{container_id}:{message.reply_to}"
                if message.reply_to is not None
                else None
            ),
            "sent_at": message.sent_at,
            "author": {
                "id": message.sender.id,
                "display_name": message.sender.name,
                "kind": "human",
            },
            "content": message.text,
            "native_id": message.id,
            "kind": "message",
        }
    )


def _brief_group_assignments(
    patterns: dict[str, list[str]],
    scopes: list[str],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for scope in sorted(scopes, key=lambda value: value.encode("utf-8")):
        matched = next(
            (
                group
                for group, values in patterns.items()
                if any(
                    scope.startswith(pattern[:-1])
                    if pattern.endswith("*")
                    else scope == pattern
                    for pattern in values
                )
            ),
            None,
        )
        result[scope] = matched or scope
    return result


def _contains_identity_handle(value: Any, handles: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_identity_handle(item, handles) for item in value.values()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity_handle(item, handles) for item in value)
    if value is None:
        return False
    text = str(value)
    return any(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(handle)}(?![A-Za-z0-9_])",
            text,
            flags=re.IGNORECASE,
        )
        is not None
        for handle in handles
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _merge_counts(
    left: dict[str, int], right: dict[str, int]
) -> dict[str, int]:
    result = dict(left)
    for key, value in right.items():
        result[key] = result.get(key, 0) + value
    return result


def _source_refs(
    values: list[SourceRef | dict[str, Any]],
    *,
    operation: str,
) -> list[SourceRef]:
    refs = [
        value if isinstance(value, SourceRef) else SourceRef.model_validate(value)
        for value in values
    ]
    if not refs:
        raise ValueError(f"{operation} MUST have source_refs")
    return refs


def _stable_source_refs(values: Iterable[SourceRef]) -> list[SourceRef]:
    result: list[SourceRef] = []
    seen: set[str] = set()
    for value in values:
        if value.source_id in seen:
            continue
        seen.add(value.source_id)
        result.append(value)
    return result


def _disagrees(winner: str, lower: Iterable[str | None]) -> bool:
    return any(value is not None and value != winner for value in lower)


def _bounded_recent_evidence(
    values: list[tuple[datetime, bytes, str]],
    *,
    limit: int = 300,
) -> str:
    ordered = sorted(values, key=lambda item: (item[0], item[1]), reverse=True)
    result = ""
    seen: set[str] = set()
    for _, _, excerpt in ordered:
        if excerpt in seen:
            continue
        seen.add(excerpt)
        separator = "\n" if result else ""
        remaining = limit - len(result) - len(separator)
        if remaining <= 0:
            break
        result += separator + excerpt[:remaining]
    return result


def _group_handle_matches(matches: Iterable[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for match in matches:
        key = (match.handle_type, match.normalized_value)
        entry = grouped.setdefault(
            key,
            {
                "handle_value": match.handle_value,
                "source_refs": [],
                "source_ids": set(),
            },
        )
        for source_ref in match.source_refs:
            if source_ref.source_id in entry["source_ids"]:
                continue
            entry["source_ids"].add(source_ref.source_id)
            entry["source_refs"].append(source_ref)
    return {
        key: grouped[key]
        for key in sorted(
            grouped,
            key=lambda item: (item[0].encode("utf-8"), item[1].encode("utf-8")),
        )
    }


def _handle_sort_key(handle: SubjectHandle) -> tuple[bytes, bytes, bytes]:
    return (
        handle.handle_type.encode("utf-8"),
        handle.normalized_value.encode("utf-8"),
        handle.binding_id.encode("utf-8"),
    )


def _task_error_summary(error: BaseException) -> str:
    kind = type(error).__name__
    message = " ".join(str(error).split())
    if _TASK_ERROR_SECRET_MARKER.search(message):
        message = "[REDACTED]"
    else:
        message = _TASK_ERROR_LONG_TOKEN.sub("[REDACTED]", message)
    return (f"{kind}: {message}" if message else kind)[:500]


def _profile_version(profile: SchemaProfile) -> str:
    return stable_hash(profile.model_dump(mode="json", by_alias=True))
