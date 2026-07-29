from __future__ import annotations

from matterhorn.canonical import normalize_title, stable_hash
from matterhorn.contracts import EpisodeCard, SchemaProfile, SubjectRecord


def resolve_subject(
    card: EpisodeCard,
    profile: SchemaProfile,
    existing: list[SubjectRecord],
) -> tuple[SubjectRecord, bool]:
    subject_type = profile.primary_subject.type
    sources = frozenset(ref.source_id for ref in card.source_refs)
    normalized = normalize_title(card.title)
    threads = frozenset([card.thread_id]) if card.thread_id is not None else frozenset()

    if card.subject_key:
        match = next(
            (item for item in existing if item.subject_key == card.subject_key), None
        )
        if match:
            return _with_evidence(match, sources, threads), False
        return (
            SubjectRecord(
                card.scope_id,
                card.subject_key,
                subject_type,
                card.title,
                normalized,
                sources,
                thread_ids=threads,
            ),
            True,
        )

    if card.thread_id is not None:
        thread_matches = [
            item
            for item in existing
            if item.subject_type == subject_type and card.thread_id in item.thread_ids
        ]
        if thread_matches:
            chosen = min(thread_matches, key=lambda item: item.subject_key)
            return _with_evidence(chosen, sources, threads), False

        evidence_match = _evidence_match(card, profile, existing, sources)
        if evidence_match is not None:
            return _with_evidence(evidence_match, sources, threads), False

        digest = stable_hash(
            [card.scope_id, subject_type, "thread", card.thread_id]
        )[:20]
        key = f"sub_{digest}"
        match = next((item for item in existing if item.subject_key == key), None)
        if match is not None:
            return _with_evidence(match, sources, threads), False
        return (
            SubjectRecord(
                card.scope_id,
                key,
                subject_type,
                card.title,
                normalized,
                sources,
                thread_ids=threads,
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
        return _with_evidence(chosen, sources, threads), False

    evidence_match = _evidence_match(card, profile, existing, sources)
    if evidence_match is not None:
        return _with_evidence(evidence_match, sources, threads), False

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
            thread_ids=threads,
        ),
        True,
    )


def _evidence_match(
    card: EpisodeCard,
    profile: SchemaProfile,
    existing: list[SubjectRecord],
    sources: frozenset[str],
) -> SubjectRecord | None:
    subject_type = profile.primary_subject.type
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
    if not evidence_matches:
        return None
    return max(
        evidence_matches,
        key=lambda pair: (pair[0], pair[1].subject_key),
    )[1]


def _with_evidence(
    record: SubjectRecord,
    sources: frozenset[str],
    threads: frozenset[str],
) -> SubjectRecord:
    return SubjectRecord(
        record.scope_id,
        record.subject_key,
        record.subject_type,
        record.title,
        record.normalized_title,
        record.source_ids | sources,
        record.parent_subject_key,
        record.thread_ids | threads,
    )
