from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from matterhorn.contracts import EpisodeCard, SchemaProfile
from matterhorn.engine.canonical import stable_hash


def normalize_title(value: str) -> str:
    lowered = value.lower()
    without_punctuation = "".join(
        " " if unicodedata.category(char).startswith(("P", "S")) else char
        for char in lowered
    )
    return re.sub(r"\s+", " ", without_punctuation).strip()


@dataclass(frozen=True)
class SubjectRecord:
    scope_id: str
    subject_key: str
    subject_type: str
    title: str
    normalized_title: str
    source_ids: frozenset[str]
    parent_subject_key: str | None = None


def derive_child_subject_key(
    scope_id: str,
    parent_subject_key: str,
    subject_type: str,
    title: str,
) -> str:
    normalized = normalize_title(title)
    digest = stable_hash([scope_id, parent_subject_key, subject_type, normalized])
    return f"sub_{digest}"


def resolve_subject(
    card: EpisodeCard,
    profile: SchemaProfile,
    existing: list[SubjectRecord],
) -> tuple[SubjectRecord, bool]:
    subject_type = profile.primary_subject.type
    sources = frozenset(ref.source_id for ref in card.source_refs)
    normalized = normalize_title(card.title)

    if card.subject_key:
        match = next(
            (item for item in existing if item.subject_key == card.subject_key), None
        )
        if match:
            return _with_sources(match, sources), False
        return (
            SubjectRecord(
                card.scope_id,
                card.subject_key,
                subject_type,
                card.title,
                normalized,
                sources,
            ),
            True,
        )

    title_matches = [
        item
        for item in existing
        if item.subject_type == subject_type and item.normalized_title == normalized
    ]
    if title_matches:
        chosen = min(title_matches, key=lambda item: item.subject_key)
        return _with_sources(chosen, sources), False

    thresholds = profile.identity.merge_evidence
    evidence_matches: list[tuple[int, SubjectRecord]] = []
    for item in existing:
        if item.subject_type != subject_type:
            continue
        shared = len(sources & item.source_ids)
        ratio = shared / len(card.source_refs)
        if shared >= 2 and (
            shared >= thresholds.min_shared_sources
            or ratio >= thresholds.or_share_ratio
        ):
            evidence_matches.append((shared, item))
    if evidence_matches:
        _, chosen = max(evidence_matches, key=lambda pair: (pair[0], pair[1].subject_key))
        return _with_sources(chosen, sources), False

    digest = stable_hash(
        [card.scope_id, subject_type, normalized, sorted(sources), card.card_id]
    )[:20]
    return (
        SubjectRecord(
            card.scope_id,
            f"sub_{digest}",
            subject_type,
            card.title,
            normalized,
            sources,
        ),
        True,
    )


def _with_sources(record: SubjectRecord, sources: frozenset[str]) -> SubjectRecord:
    return SubjectRecord(
        record.scope_id,
        record.subject_key,
        record.subject_type,
        record.title,
        record.normalized_title,
        record.source_ids | sources,
        record.parent_subject_key,
    )
