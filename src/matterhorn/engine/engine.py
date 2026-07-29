from __future__ import annotations

import json
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
    EvidenceRef,
    EvidenceStatus,
    ExportEnvelope,
    ExportSchemaProfile,
    ExportSourceState,
    ExportSubject,
    FlushReport,
    ImportReport,
    Message,
    Operation,
    Origin,
    Record,
    RecordExtractor,
    ReplayReport,
    SchemaProfile,
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
from matterhorn.engine.identity import resolve_subject
from matterhorn.engine.materializer import materialize
from matterhorn.errors import ImportRefusedError, ResourceNotFoundError
from matterhorn.projection import project_assertions
from matterhorn.query import QueryService
from matterhorn.store import SQLiteStore, Store

Clock = Callable[[], datetime]


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
        }


class Engine:
    def __init__(
        self,
        store: str | Path | Store,
        schema: str | Path | SchemaProfile = "org-matters/v1",
        *,
        clock: Clock | Iterable[datetime] | None = None,
        llm: LlmGateway | None = None,
        gateway: LlmGateway | None = None,
        extractor: RecordExtractor | None = None,
    ):
        self.store = _resolve_store(store)
        self.profile = resolve_schema(schema)
        self._clock = _clock_callable(clock)
        if llm is not None and gateway is not None:
            raise ValueError("pass either llm or gateway, not both")
        self._write_gateway: LlmGateway = gateway or llm or NullGateway()
        self._extractor = extractor
        self.query = QueryService(self.store, self.profile)

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
                self.store.upsert_subject(subject)
                assertions = extract_card(
                    card,
                    subject.subject_key,
                    subject.subject_type,
                    self.profile,
                    self._clock(),
                )
                for assertion in assertions:
                    if self.store.add_assertion(assertion):
                        emitted.append(assertion)
                self.store.enqueue_distill(
                    card,
                    subject_key=subject.subject_key,
                    subject_type=subject.subject_type,
                )
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
        if active and self._extractor is None:
            raise RuntimeError("record extraction requires a RecordExtractor")
        extraction = (
            self._extractor.extract(
                scope_id=scope_id,
                records=active,
                batch_size=batch_size,
            )
            if active
            else None
        )
        cards = extraction.cards if extraction is not None else []
        emitted: list[Assertion] = []
        with self.store.transaction():
            for record, observation_hash in pending:
                source_ref = record.to_source_ref()
                self.store.observe_source(
                    scope_id,
                    source_ref,
                    revoked_at=record.revoked_at,
                )
                self.store.mark_record_observation(
                    scope_id,
                    record.record_id,
                    observation_hash,
                    record.container_id,
                    _record_observed_at(record),
                )
            if cards:
                emitted = self._ingest_cards_sync(cards, scope_id=scope_id)
            if pending and not backfill:
                by_container: dict[str, list[Record]] = {}
                for record, _ in pending:
                    by_container.setdefault(record.container_id, []).append(record)
                for container_id, items in by_container.items():
                    self.store.update_sync_position(
                        scope_id,
                        container_id,
                        watermark=max(_record_observed_at(item) for item in items),
                        cursor=(cursors or {}).get(container_id),
                    )

        rejection_counts = (
            extraction.rejection_counts if extraction is not None else {}
        )
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
            sync_positions=self.store.sync_positions(scope_id),
        )

    def matters(self, scope_id: str) -> list[Matter]:
        """Return ergonomic projected matters without touching the LLM."""

        result = []
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

        pending = self.store.tasks(scope_id, status=TaskStatus.pending)
        processed: list[str] = []
        for row in pending:
            with self.store.transaction():
                self.store.update_task(row.task_id, status=TaskStatus.running)
            before_assertions = {
                item.assertion_id for item in self.store.assertions(scope_id)
            }
            cards_produced = gate_accepted = 0
            gate_rejected: dict[str, int] = {}
            failed = False
            gate_before = self.gate_statistics(scope_id)
            try:
                if row.kind == "messages":
                    record_report = self.add_records(
                        row.payload["records"],
                        scope_id=scope_id,
                    )
                    cards_produced = record_report.cards_accepted
                    gate_accepted += record_report.cards_accepted
                    gate_rejected = _merge_counts(
                        gate_rejected, record_report.drop_reasons
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
            except Exception:  # noqa: BLE001
                failed = True

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
                )
            processed.append(row.task_id)
        if not pending and self.store.distill_queue_count(scope_id):
            self.dream(scope_id)
        return FlushReport(
            scope_id=scope_id,
            tasks_processed=len(processed),
            task_ids=processed,
            remaining=len(
                self.store.tasks(scope_id, status=TaskStatus.pending)
            ),
        )

    def flush_quiet(self, quiet_period_minutes: float = 10) -> list[FlushReport]:
        if quiet_period_minutes < 0:
            raise ValueError("quiet_period_minutes MUST be non-negative")
        cutoff = self._clock() - timedelta(minutes=quiet_period_minutes)
        return [self.flush(scope_id) for scope_id in self.store.quiet_scopes(cutoff)]

    def flush_quiet_at(
        self, quiet_period_minutes: float, instant: datetime
    ) -> list[FlushReport]:
        if quiet_period_minutes < 0:
            raise ValueError("quiet_period_minutes MUST be non-negative")
        cutoff = as_utc(instant) - timedelta(minutes=quiet_period_minutes)
        return [self.flush(scope_id) for scope_id in self.store.quiet_scopes(cutoff)]

    def flush_pending(self) -> list[FlushReport]:
        return [self.flush(scope_id) for scope_id in self.store.pending_scopes()]

    def now(self) -> datetime:
        return self._clock()

    def _enqueue_task(
        self,
        *,
        scope_id: str,
        kind: str,
        payload: dict[str, Any],
        accepted: int,
        newest_message_at: datetime | None,
    ) -> TaskReceipt:
        created_at = self._clock()
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
                item.subject_key,
                item.predicate,
                item.operation,
                value_key,
                as_utc(item.valid_from),
                item.source_refs,
            ),
            scope_id=item.scope_id,
            subject_key=item.subject_key,
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
            if not any(
                subject.subject_key == item.subject_key
                for subject in self.store.subjects(item.scope_id)
            ):
                raise ValueError("correction subject does not exist")
            for source_ref in item.source_refs:
                self.store.observe_source(item.scope_id, source_ref)
            self.store.add_assertion(assertion)
            self._rebuild(item.scope_id)
        return assertion

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
            prompt = build_prompt(
                self.profile,
                item.card,
                subject_key=item.subject_key,
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
                                candidate.subject_key,
                                candidate.predicate,
                                candidate.operation,
                                value_key,
                                as_utc(candidate.valid_from),
                                accepted.source_refs,
                            ),
                            scope_id=scope_id,
                            subject_key=candidate.subject_key,
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
        assertions = self.store.assertions(scope_id)
        intervals, stats = project_assertions(assertions, self.profile)
        cards = materialize(self.store.subjects(scope_id), intervals, self.profile)
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


def _record_observed_at(record: Record) -> datetime:
    return max(
        instant
        for instant in (record.sent_at, record.edited_at, record.revoked_at)
        if instant is not None
    )


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


def _profile_version(profile: SchemaProfile) -> str:
    return stable_hash(profile.model_dump(mode="json", by_alias=True))
