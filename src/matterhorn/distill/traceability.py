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
