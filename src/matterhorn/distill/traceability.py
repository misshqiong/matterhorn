from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from matterhorn.contracts import SourceRef


class TraceabilityReason(str, Enum):
    NO_SOURCES = "NO_SOURCES"
    SOURCE_NOT_TRACEABLE = "SOURCE_NOT_TRACEABLE"


@dataclass(frozen=True)
class TraceabilityResult:
    source_refs: list[SourceRef]
    failure: TraceabilityReason | None = None


def source_aliases(source_ids: Iterable[str]) -> dict[str, str]:
    """Assign short model-facing aliases in deterministic input order."""

    return {
        f"m{index}": source_id
        for index, source_id in enumerate(source_ids, start=1)
    }


def restore_source_aliases(
    payload: object,
    *,
    collection_key: str,
    aliases: dict[str, str],
    available_source_ids: Iterable[str],
) -> object:
    """Restore known aliases in a decoded model response before gating.

    Existing full source IDs take precedence, so old fixtures and gateways
    remain valid. Unknown aliases are deliberately left untouched for the
    ordinary traceability gate to reject as SOURCE_NOT_TRACEABLE.
    """

    if not isinstance(payload, dict):
        return payload
    items = payload.get(collection_key)
    if not isinstance(items, list):
        return payload
    available = set(available_source_ids)
    for item in items:
        if not isinstance(item, dict):
            continue
        cited = item.get("source_ids")
        if not isinstance(cited, list):
            continue
        item["source_ids"] = [
            source_id
            if source_id in available or not isinstance(source_id, str)
            else aliases.get(source_id, source_id)
            for source_id in cited
        ]
    return payload


def resolve_traceable_sources(
    source_ids: Iterable[str],
    available_refs: Iterable[SourceRef],
) -> TraceabilityResult:
    """Resolve cited evidence without inventing, borrowing, or coercing sources."""
    requested = list(source_ids)
    if not requested:
        return TraceabilityResult([], TraceabilityReason.NO_SOURCES)
    available = {ref.source_id: ref for ref in available_refs}
    if not set(requested).issubset(available):
        return TraceabilityResult([], TraceabilityReason.SOURCE_NOT_TRACEABLE)
    return TraceabilityResult([available[source_id] for source_id in requested])
