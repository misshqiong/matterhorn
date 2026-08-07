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
            _require_card_layer(match, card)
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
                layer=card.layer,
            ),
            True,
        )

    if card.thread_id is not None:
        chosen = thread_match(card, profile, existing)
        if chosen is not None:
            return _with_evidence(chosen, sources, threads), False

        matched_evidence = evidence_match(card, profile, existing)
        if matched_evidence is not None:
            return _with_evidence(matched_evidence, sources, threads), False

        digest = stable_hash(
            [card.scope_id, subject_type, "thread", card.thread_id]
        )[:20]
        key = f"sub_{digest}"
        match = next((item for item in existing if item.subject_key == key), None)
        if match is not None:
            _require_card_layer(match, card)
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
                layer=card.layer,
            ),
            True,
        )

    title_matches = [
        item
        for item in existing
        if item.subject_type == subject_type
        and item.normalized_title == normalized
        and item.layer == card.layer
    ]
    if title_matches:
        chosen = min(title_matches, key=lambda item: item.subject_key)
        return _with_evidence(chosen, sources, threads), False

    matched_evidence = evidence_match(card, profile, existing)
    if matched_evidence is not None:
        return _with_evidence(matched_evidence, sources, threads), False

    digest = stable_hash(
        [card.scope_id, subject_type, normalized, sorted(sources), card.card_id]
    )[:20]
    return (
        new_subject(card, profile, existing, digest=digest),
        True,
    )


def thread_match(
    card: EpisodeCard,
    profile: SchemaProfile,
    existing: list[SubjectRecord],
) -> SubjectRecord | None:
    if card.thread_id is None:
        return None
    matches = [
        item
        for item in existing
        if item.subject_type == profile.primary_subject.type
        and item.layer == card.layer
        and card.thread_id in item.thread_ids
    ]
    return min(matches, key=lambda item: item.subject_key) if matches else None


def evidence_match(
    card: EpisodeCard,
    profile: SchemaProfile,
    existing: list[SubjectRecord],
) -> SubjectRecord | None:
    subject_type = profile.primary_subject.type
    sources = frozenset(ref.source_id for ref in card.source_refs)
    thresholds = profile.identity.merge_evidence
    evidence_matches: list[tuple[int, SubjectRecord]] = []
    for item in existing:
        if item.subject_type != subject_type:
            continue
        if item.layer != card.layer:
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


def attach_subject(record: SubjectRecord, card: EpisodeCard) -> SubjectRecord:
    _require_card_layer(record, card)
    return _with_evidence(
        record,
        frozenset(ref.source_id for ref in card.source_refs),
        (
            frozenset([card.thread_id])
            if card.thread_id is not None
            else frozenset()
        ),
    )


def new_subject(
    card: EpisodeCard,
    profile: SchemaProfile,
    existing: list[SubjectRecord],
    *,
    digest: str | None = None,
) -> SubjectRecord:
    subject_type = profile.primary_subject.type
    sources = frozenset(ref.source_id for ref in card.source_refs)
    threads = (
        frozenset([card.thread_id])
        if card.thread_id is not None
        else frozenset()
    )
    if card.thread_id is not None:
        generated = stable_hash(
            [card.scope_id, subject_type, "thread", card.thread_id]
        )[:20]
    else:
        generated = digest or stable_hash(
            [
                card.scope_id,
                subject_type,
                normalize_title(card.title),
                sorted(sources),
                card.card_id,
            ]
        )[:20]
    key = f"sub_{generated}"
    match = next((item for item in existing if item.subject_key == key), None)
    if match is not None:
        return attach_subject(match, card)
    return SubjectRecord(
        card.scope_id,
        key,
        subject_type,
        card.title,
        normalize_title(card.title),
        sources,
        thread_ids=threads,
        layer=card.layer,
    )


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
        record.layer,
    )


def _require_card_layer(record: SubjectRecord, card: EpisodeCard) -> None:
    if record.layer != card.layer:
        raise ValueError(
            f"subject layer is immutable: existing={record.layer}, card={card.layer}"
        )
