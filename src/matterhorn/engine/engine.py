from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from matterhorn.contracts import (
    Assertion,
    Correction,
    DreamReport,
    EpisodeCard,
    Operation,
    Origin,
    SchemaProfile,
)
from matterhorn.contracts.schema import resolve_schema
from matterhorn.engine.canonical import (
    as_utc,
    derive_assertion_id,
    object_key,
    stable_hash,
)
from matterhorn.engine.extractor import FIELD_WIDE_RETRACT, extract_card
from matterhorn.engine.identity import SubjectRecord, normalize_title, resolve_subject
from matterhorn.engine.materializer import materialize
from matterhorn.engine.projector import project_assertions
from matterhorn.query import QueryService
from matterhorn.store import SQLiteStore, Store
from matterhorn.distill import LlmGateway, NullGateway, build_prompt, validate_response


Clock = Callable[[], datetime]


class Engine:
    def __init__(
        self,
        store: str | Path | Store,
        schema: str | Path | SchemaProfile,
        *,
        clock: Clock | Iterable[datetime] | None = None,
        llm: LlmGateway | None = None,
        gateway: LlmGateway | None = None,
    ):
        self.store = _resolve_store(store)
        self.profile = resolve_schema(schema)
        self._clock = _clock_callable(clock)
        if llm is not None and gateway is not None:
            raise ValueError("pass either llm or gateway, not both")
        self._write_gateway: LlmGateway = gateway or llm or NullGateway()
        self.query = QueryService(self.store, self.profile)

    def ingest(
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
            self.store.add_assertion(assertion)
            self._rebuild(item.scope_id)
        return assertion

    def replay(self, scope_id: str) -> None:
        with self.store.transaction():
            self._rebuild(scope_id)

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
                gate = validate_response(
                    raw,
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
            except Exception as error:
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

    def _rebuild(self, scope_id: str) -> None:
        intervals, stats = project_assertions(
            self.store.assertions(scope_id), self.profile
        )
        cards = materialize(self.store.subjects(scope_id), intervals, self.profile)
        self.store.replace_projection(scope_id, intervals, cards, stats)


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
        return lambda: datetime.now(timezone.utc)
    if callable(clock):
        return lambda: as_utc(clock())
    iterator: Iterator[datetime] = iter(clock)
    return lambda: as_utc(next(iterator))
