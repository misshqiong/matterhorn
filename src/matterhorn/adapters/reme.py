"""Map a best-effort ReMe daily/digest export into an EpisodeCard.

Supported input shape (one parsed Markdown file):

    {
      "path": "daily/2026-07-29/session-42.md",
      "frontmatter": {
        "name": "Release review",
        "date": "2026-07-29",
        "status": "open",
        "participants": [{"id": "u1", "display_name": "Ada", "role": "owner"}],
        "sources": [
          {"id": "session-42:msg-7", "sent_at": "...", "sender": "u1",
           "excerpt": "..."}
        ]
      },
      "content": "The release candidate is ready.",
      "scope_id": "team-a"
    }

ReMe's public format is file-native Markdown and its frontmatter is extensible,
so this adapter intentionally supports the exact normalized export above rather
than pretending every ReMe workspace has one stable JSON schema. Mapping is
lossy: Markdown becomes ``progress`` and wikilinks/frontmatter outside the
listed fields are not retained.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from matterhorn.contracts import EpisodeCard
from matterhorn.engine.canonical import stable_hash


def map_reme_digest(
    payload: Mapping[str, Any],
    *,
    scope_id: str | None = None,
) -> EpisodeCard:
    frontmatter = _mapping(payload.get("frontmatter"), "frontmatter")
    source_values = frontmatter.get("sources")
    if not isinstance(source_values, list) or not source_values:
        raise ValueError("ReMe digest has no traceable frontmatter.sources")
    resolved_scope = scope_id or payload.get("scope_id")
    if not isinstance(resolved_scope, str) or not resolved_scope:
        raise ValueError("ReMe digest requires scope_id")
    digest_date = frontmatter.get("date")
    if digest_date is None:
        raise ValueError("ReMe digest frontmatter.date is required")
    sources = [
        {
            "source_id": _required(source, "id", "frontmatter.sources"),
            "sent_at": _required(source, "sent_at", "frontmatter.sources"),
            "sender": _required(source, "sender", "frontmatter.sources"),
            "excerpt": source.get("excerpt"),
            "uri": source.get("uri"),
        }
        for source in (_mapping(item, "frontmatter.sources[]") for item in source_values)
    ]
    normalized = {
        "path": payload.get("path"),
        "frontmatter": dict(frontmatter),
        "content": payload.get("content"),
        "scope_id": resolved_scope,
    }
    return EpisodeCard.model_validate(
        {
            "card_id": f"reme_{stable_hash(normalized)}",
            "scope_id": resolved_scope,
            "date": date.fromisoformat(str(digest_date)),
            "title": frontmatter.get("name") or frontmatter.get("title"),
            "status": frontmatter.get("status"),
            "participants": frontmatter.get("participants", []),
            "progress": payload.get("content"),
            "occurred_at": frontmatter.get("occurred_at"),
            "last_active_at": frontmatter.get("last_active_at"),
            "subject_key": frontmatter.get("subject_key"),
            "source_refs": sources,
        }
    )


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        # Adapter payload defects consistently use ValueError at this boundary.
        raise ValueError(  # noqa: TRY004
            f"ReMe {location} MUST be an object"
        )
    return value


def _required(value: Mapping[str, Any], key: str, location: str) -> Any:
    result = value.get(key)
    if result is None or result == "":
        raise ValueError(f"ReMe {location}.{key} is required")
    return result
