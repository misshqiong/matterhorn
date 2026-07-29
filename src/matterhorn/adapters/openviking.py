"""Map a best-effort OpenViking overview export into an EpisodeCard.

Supported input shape (normalized from ``overview(uri)`` plus metadata):

    {
      "uri": "viking://user/alice/memories/events/release",
      "name": "Release review",
      "overview": "The release candidate is ready.",
      "metadata": {
        "date": "2026-07-29",
        "status": "open",
        "source_refs": [
          {"uri": "viking://user/alice/sessions/s1/messages.jsonl#msg-7",
           "sent_at": "...", "sender": "alice", "excerpt": "..."}
        ]
      },
      "scope_id": "team-a"
    }

OpenViking publicly exposes filesystem-like Viking URIs and Markdown overview
content rather than a fixed temporal-card JSON contract. The adapter therefore
requires callers to attach source metadata in the normalized shape above.
Mapping is lossy: overview Markdown becomes ``progress`` and relations/chunks
are not retained.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from matterhorn.contracts import EpisodeCard
from matterhorn.engine.canonical import stable_hash


def map_openviking_digest(
    payload: Mapping[str, Any],
    *,
    scope_id: str | None = None,
) -> EpisodeCard:
    metadata = _mapping(payload.get("metadata"), "metadata")
    source_values = metadata.get("source_refs")
    if not isinstance(source_values, list) or not source_values:
        raise ValueError("OpenViking digest has no traceable metadata.source_refs")
    resolved_scope = scope_id or payload.get("scope_id")
    if not isinstance(resolved_scope, str) or not resolved_scope:
        raise ValueError("OpenViking digest requires scope_id")
    uri = payload.get("uri")
    if not isinstance(uri, str) or not uri.startswith("viking://"):
        raise ValueError("OpenViking digest requires a viking:// uri")
    digest_date = metadata.get("date")
    if digest_date is None:
        raise ValueError("OpenViking metadata.date is required")
    sources = [
        {
            "source_id": _required(source, "uri", "metadata.source_refs"),
            "sent_at": _required(source, "sent_at", "metadata.source_refs"),
            "sender": _required(source, "sender", "metadata.source_refs"),
            "excerpt": source.get("excerpt"),
            "uri": source.get("uri"),
        }
        for source in (_mapping(item, "metadata.source_refs[]") for item in source_values)
    ]
    normalized = {
        "uri": uri,
        "name": payload.get("name"),
        "overview": payload.get("overview"),
        "metadata": dict(metadata),
        "scope_id": resolved_scope,
    }
    title = payload.get("name") or PurePosixPath(uri.removeprefix("viking://")).name
    return EpisodeCard.model_validate(
        {
            "card_id": f"openviking_{stable_hash(normalized)}",
            "scope_id": resolved_scope,
            "date": date.fromisoformat(str(digest_date)),
            "title": title,
            "status": metadata.get("status"),
            "participants": metadata.get("participants", []),
            "progress": payload.get("overview"),
            "occurred_at": metadata.get("occurred_at"),
            "last_active_at": metadata.get("last_active_at"),
            "subject_key": metadata.get("subject_key"),
            "source_refs": sources,
        }
    )


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        # Adapter payload defects consistently use ValueError at this boundary.
        raise ValueError(  # noqa: TRY004
            f"OpenViking {location} MUST be an object"
        )
    return value


def _required(value: Mapping[str, Any], key: str, location: str) -> Any:
    result = value.get(key)
    if result is None or result == "":
        raise ValueError(f"OpenViking {location}.{key} is required")
    return result
