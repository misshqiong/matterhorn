from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

from matterhorn.contracts import Cardinality, Interval, MemoryCard, SchemaProfile
from matterhorn.engine.identity import SubjectRecord


def materialize(
    subjects: Iterable[SubjectRecord],
    intervals: Iterable[Interval],
    profile: SchemaProfile,
) -> list[MemoryCard]:
    current: dict[tuple[str, str], dict[str, list[Interval]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for interval in intervals:
        if interval.valid_to is None:
            current[(interval.scope_id, interval.subject_key)][interval.predicate].append(
                interval
            )

    cards: list[MemoryCard] = []
    for subject in sorted(subjects, key=lambda item: (item.scope_id, item.subject_key)):
        values: dict[str, object] = {}
        updated: datetime | None = None
        source_ids: set[str] = set(subject.source_ids)
        for predicate_name, entries in current[
            (subject.scope_id, subject.subject_key)
        ].items():
            definition = profile.predicate(predicate_name)
            ordered = sorted(entries, key=lambda item: (item.valid_from, item.object_key))
            if definition.cardinality == Cardinality.SINGLE:
                values[predicate_name] = ordered[-1].object_value
            else:
                values[predicate_name] = [item.object_value for item in ordered]
            for item in ordered:
                updated = max(updated, item.valid_from) if updated else item.valid_from
                source_ids.update(ref.source_id for ref in item.source_refs)
        cards.append(
            MemoryCard(
                scope_id=subject.scope_id,
                subject_key=subject.subject_key,
                subject_type=subject.subject_type,
                title=subject.title,
                current=values,
                updated_at=updated,
                source_ids=sorted(source_ids),
            )
        )
    return cards

