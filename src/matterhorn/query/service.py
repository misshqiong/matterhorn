from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from matterhorn.canonical import object_key
from matterhorn.contracts import (
    Cardinality,
    EvidenceRef,
    EvidenceStatus,
    EvidenceSummary,
    SchemaProfile,
)
from matterhorn.store import QuerySubjectRow, QueryValueRow, Store


@dataclass(frozen=True)
class ValueResult:
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

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_refs"] = [
            item.model_dump(mode="json") for item in self.source_refs
        ]
        return result


@dataclass(frozen=True)
class SubjectResult:
    subject_key: str
    subject_type: str
    title: str
    current: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QueryService:
    def __init__(
        self,
        store: Store,
        profile: SchemaProfile,
        subject_resolver: Callable[[str, str], str] | None = None,
    ):
        self._store = store
        self._profile = profile
        self._subject_resolver = subject_resolver or (lambda _scope, key: key)

    def current(
        self, scope_id: str, subject_key: str, predicate: str
    ) -> list[ValueResult]:
        definition = self._profile.predicate(predicate)
        subject_key = self._subject_resolver(scope_id, subject_key)
        rows = self._store.query_current_values(
            scope_id,
            subject_key,
            predicate,
            append=definition.cardinality == Cardinality.APPEND,
        )
        return [self._value(scope_id, row) for row in rows]

    def timeline(
        self, scope_id: str, subject_key: str, predicate: str
    ) -> list[ValueResult]:
        self._profile.predicate(predicate)
        subject_key = self._subject_resolver(scope_id, subject_key)
        rows = self._store.query_timeline_values(
            scope_id, subject_key, predicate
        )
        return [self._value(scope_id, row) for row in rows]

    def at(
        self, scope_id: str, subject_key: str, predicate: str, instant: datetime
    ) -> list[ValueResult]:
        definition = self._profile.predicate(predicate)
        subject_key = self._subject_resolver(scope_id, subject_key)
        rows = self._store.query_values_at(
            scope_id,
            subject_key,
            predicate,
            instant,
            append=definition.cardinality == Cardinality.APPEND,
        )
        return [self._value(scope_id, row) for row in rows]

    def by_person(self, scope_id: str, person_id: str) -> list[SubjectResult]:
        predicates = [
            item.name for item in self._profile.predicates if item.object == "person"
        ]
        if not predicates:
            return []
        rows = self._store.query_subjects_by_object(
            scope_id, predicates, object_key(person_id)
        )
        return [self._subject(row) for row in rows]

    def list_matters(self, scope_id: str) -> list[SubjectResult]:
        rows = self._store.query_subjects_by_type(
            scope_id, self._profile.primary_subject.type
        )
        return [self._subject(row) for row in rows]

    def completion(self, scope_id: str) -> dict[str, int | float]:
        config = self._profile.completion
        completed, total = self._store.query_completion_counts(
            scope_id,
            config.predicate if config is not None else None,
            [object_key(value) for value in config.completed_values]
            if config is not None
            else [],
        )
        return {
            "completed": completed,
            "total": total,
            "ratio": completed / total if total else 0.0,
        }

    def _value(self, scope_id: str, row: QueryValueRow) -> ValueResult:
        source_refs = self._store.source_states(scope_id, row.source_refs)
        revoked = sum(
            item.status == EvidenceStatus.revoked for item in source_refs
        )
        if source_refs and revoked == len(source_refs):
            evidence_status = EvidenceSummary.revoked
        elif revoked:
            evidence_status = EvidenceSummary.partially_revoked
        else:
            evidence_status = EvidenceSummary.active
        return ValueResult(
            subject_key=row.subject_key,
            predicate=row.predicate,
            value=row.value,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            recorded_at=row.recorded_at,
            assertion_id=row.assertion_id,
            supporting_assertion_ids=row.supporting_assertion_ids,
            source_ids=row.source_ids,
            source_refs=source_refs,
            evidence_status=evidence_status.value,
            origin=row.origin,
        )

    @staticmethod
    def _subject(row: QuerySubjectRow) -> SubjectResult:
        return SubjectResult(
            subject_key=row.subject_key,
            subject_type=row.subject_type,
            title=row.title,
            current=row.current,
        )
