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

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "status": self.status,
            "owners": self.owners,
            "participants": self.participants,
            "blocked_by": self.blocked_by,
            "next_step": self.next_step,
            "due": self.due,
            "subject_key": self.subject_key,
            "owners_display": self.owners_display or self.owners,
            "participants_display": self.participants_display or self.participants,
            "aliases": self.aliases,
            "updated_at": self.updated_at,
        }


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
                self.store.upsert_subject(subject)
                recorded_at = self._clock()
                assertions = extract_card(
                    card,
                    subject.subject_key,
                    subject.subject_type,
                    self.profile,
                    recorded_at,
                )
                for assertion in assertions:
                    if self.store.add_assertion(assertion):
                        emitted.append(assertion)
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
            with self.store.transaction():
                self.store.stage_records(
                    scope_id,
                    validated,
                    staged_at=datetime.now(UTC),
                )
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
        if active and self._extractor is None:
            raise RuntimeError("record extraction requires a RecordExtractor")
        chunks = _conversation_extraction_chunks(active, batch_size) if active else []
        pending_by_record_id = {
            record.record_id: (record, observation_hash)
            for record, observation_hash in pending
        }
        cards: list[EpisodeCard] = []
        rejection_counts: dict[str, int] = {}
        emitted: list[Assertion] = []
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
            handles_bound=handle_counts.bound,
            handles_already_bound=handle_counts.already_bound,
            handle_conflicts=handle_counts.conflicts,
            **route_counts.to_dict(),
            sync_positions=self.store.sync_positions(scope_id),
        )

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
    ) -> tuple[list[Assertion], _HandleCounts, _RouteCounts]:
        emitted: list[Assertion] = []
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
                card_emitted, card_handles, card_routes = self._apply_route_plan(
                    card,
                    plan,
                    record_by_id=record_by_id,
                )
                emitted.extend(card_emitted)
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
        return emitted, handle_counts, route_counts

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
        positive = sorted(
            (item for item in recalled if item[0] > 0),
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
    ) -> tuple[list[Assertion], _HandleCounts, _RouteCounts]:
        handles = _HandleCounts()
        routes = _RouteCounts(
            route_disagreements=int(plan.disagreement),
        )
        if plan.duplicate:
            return [], _HandleCounts(), _RouteCounts()
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
            return [], handles, routes

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
        self.store.upsert_subject(subject)
        recorded_at = self._clock()
        assertions = extract_card(
            card,
            subject.subject_key,
            subject.subject_type,
            self.profile,
            recorded_at,
        )
        emitted = [
            assertion
            for assertion in assertions
            if self.store.add_assertion(assertion)
        ]
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
            handle_conflicts=handles.conflicts,
            route_counts=routes.to_dict(),
        )
        self._rebuild(card.scope_id)
        return emitted, handles, routes

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
        source_refs: list[SourceRef | dict[str, Any]],
    ) -> ReviewItem:
        if action not in {"attach", "new"}:
            raise ValueError("review action MUST be 'attach' or 'new'")
        if action == "attach" and not subject_key:
            raise ValueError("attach review action requires subject_key")
        if action == "new" and subject_key is not None:
            raise ValueError("new review action MUST NOT include subject_key")
        refs = _source_refs(source_refs, operation="review resolutions")
        with self.store.transaction():
            item = self.store.review_item(scope_id, review_id)
            if item is None:
                raise ResourceNotFoundError(f"unknown review_id: {review_id}")
            if item.resolved_at is not None:
                raise ReviewConflictError(
                    f"review_id {review_id!r} is already resolved"
                )
            original_card = EpisodeCard.model_validate(item.card_json)
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

    def matters(self, scope_id: str) -> list[Matter]:
        """Return ergonomic projected matters without touching the LLM."""

        result = []
        aliases = self._subject_aliases(scope_id)
        names = self.store.person_names(scope_id)
        updated_at: dict[str, datetime] = {}
        for assertion in _canonicalized_assertions(
            self.store.assertions(scope_id),
            self.store.subject_merges(scope_id),
        ):
            previous = updated_at.get(assertion.subject_key)
            if previous is None or assertion.recorded_at > previous:
                updated_at[assertion.subject_key] = assertion.recorded_at
        for subject in self.query.list_matters(scope_id):
            current = subject.current
            result.append(
                Matter(
                    title=subject.title,
                    status=current.get("status"),
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
                    updated_at=updated_at.get(subject.subject_key),
                )
            )
        return result

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
                    self._ingest_cards_sync(cards, scope_id=scope_id)
                    gate_accepted += cards_produced
                else:
                    raise ValueError(f"unknown task kind: {row.kind}")

                dream = self.dream(scope_id)
                gate_accepted += dream.accepted_candidates
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
                    gate_accepted=gate_accepted,
                    gate_rejected=gate_rejected,
                    handle_conflicts=handle_conflicts,
                    route_counts=task_routes.to_dict(),
                    last_error=last_error,
                )
            processed.append(row.task_id)
        if not pending and self.store.distill_queue_count(scope_id):
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
                    self.store.stage_records(
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

    def sync_positions(self, scope_id: str):
        return self.store.sync_positions(scope_id)

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
                            recorded_at=self._clock(),
                            source_refs=accepted.source_refs,
                            origin=Origin.model,
                        )
                        if self.store.add_assertion(assertion):
                            item_new_assertions += 1
                    self.store.record_gate_report(
                        scope_id,
                        accepted=gate.accepted_count,
                        rejections=gate.rejection_counts,
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
            accepted_count += gate.accepted_count
            rejected_count += gate.rejected_count
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
    edges = _merge_edges(merges)
    return [
        assertion.model_copy(
            update={
                "subject_key": _canonical_subject_key(
                    assertion.subject_key,
                    edges,
                )
            }
        )
        for assertion in assertions
    ]


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
