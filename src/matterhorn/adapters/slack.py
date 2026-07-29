"""Pure Slack Web API / Events API payload normalization.

The adapter accepts `conversations.history` message objects and Events API
message payloads. It performs no network calls and never invents message
identity: Slack's per-conversation `ts` becomes `<channel>:<ts>`.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from matterhorn.contracts import Record

_CONVERSATIONAL_SUBTYPES = {
    None,
    "assistant_app_thread",
    "bot_message",
    "file_comment",
    "file_mention",
    "file_share",
    "me_message",
    "thread_broadcast",
}

_ANGLE_TOKEN = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True)
class SlackHistoryResult:
    records: list[Record]
    dropped: dict[str, int]
    next_cursor: str | None
    has_more: bool


def map_slack_history(
    payload: Mapping[str, Any],
    *,
    channel_id: str,
    workspace_domain: str,
    users: Mapping[str, Any] | None = None,
    channels: Mapping[str, Any] | None = None,
) -> SlackHistoryResult:
    """Map one `conversations.history` response without fetching another page."""

    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise TypeError("Slack conversations.history payload requires messages[]")
    records: list[Record] = []
    dropped: Counter[str] = Counter()
    for message in messages:
        if not isinstance(message, Mapping):
            raise TypeError("Slack messages[] entries MUST be objects")
        record = map_slack_message(
            message,
            channel_id=channel_id,
            workspace_domain=workspace_domain,
            team_id=_optional_string(message.get("team")),
            users=users,
            channels=channels,
        )
        if record is None:
            dropped[f"NON_CONVERSATIONAL:{message.get('subtype') or 'unknown'}"] += 1
        else:
            records.append(record)
    metadata = payload.get("response_metadata")
    cursor = (
        _optional_string(metadata.get("next_cursor"))
        if isinstance(metadata, Mapping)
        else None
    )
    return SlackHistoryResult(
        records=sorted(records, key=lambda item: (item.sent_at, item.record_id)),
        dropped=dict(sorted(dropped.items())),
        next_cursor=cursor or None,
        has_more=bool(payload.get("has_more", False)),
    )


def map_slack_event(
    payload: Mapping[str, Any],
    *,
    workspace_domain: str,
    users: Mapping[str, Any] | None = None,
    channels: Mapping[str, Any] | None = None,
    prior_record: Record | None = None,
) -> Record | None:
    """Map an Events API wrapper or bare message event.

    Slack delete events omit the original author and content. Callers MUST pass
    the previously stored Record for `message_deleted`; the adapter fails rather
    than fabricating those fields.
    """

    event = payload.get("event", payload)
    if not isinstance(event, Mapping):
        raise TypeError("Slack event payload requires an event object")
    team_id = _optional_string(payload.get("team_id") or event.get("team"))
    return map_slack_message(
        event,
        workspace_domain=workspace_domain,
        team_id=team_id,
        users=users,
        channels=channels,
        prior_record=prior_record,
    )


def map_slack_message(
    payload: Mapping[str, Any],
    *,
    workspace_domain: str,
    channel_id: str | None = None,
    team_id: str | None = None,
    users: Mapping[str, Any] | None = None,
    channels: Mapping[str, Any] | None = None,
    prior_record: Record | None = None,
) -> Record | None:
    """Map one Slack message object or hidden change/delete event."""

    if payload.get("type") != "message":
        raise ValueError("Slack payload type MUST be message")
    outer_subtype = _optional_string(payload.get("subtype"))
    channel = _required_string(payload.get("channel") or channel_id, "channel")

    if outer_subtype == "message_deleted":
        return _map_deleted(
            payload,
            channel=channel,
            workspace_domain=workspace_domain,
            team_id=team_id,
            prior_record=prior_record,
        )

    message = payload
    subtype = outer_subtype
    if outer_subtype == "message_changed":
        nested = payload.get("message")
        if not isinstance(nested, Mapping):
            raise ValueError("Slack message_changed requires message object")
        message = nested
        subtype = "message_changed"
    elif outer_subtype not in _CONVERSATIONAL_SUBTYPES:
        return None

    ts = _required_string(message.get("ts"), "ts")
    author = _author(message, users)
    content = _content(message, users=users, channels=channels)
    thread_ts = _optional_string(message.get("thread_ts"))
    record_id = f"{channel}:{ts}"
    edited = message.get("edited")
    edited_at = (
        _slack_instant(_required_string(edited.get("ts"), "edited.ts"))
        if isinstance(edited, Mapping)
        else None
    )
    return Record.model_validate(
        {
            "record_id": record_id,
            "native_id": ts,
            "container_id": channel,
            "thread_id": f"{channel}:{thread_ts}" if thread_ts else None,
            "sent_at": _slack_instant(ts),
            "author": author,
            "content": content,
            "uri": _permalink(workspace_domain, channel, ts),
            "reactions": _reactions(message.get("reactions")),
            "attachments": _files(message.get("files")),
            "edited_at": edited_at,
            "kind": "message",
            "subtype": subtype,
            "workspace_id": _optional_string(message.get("team")) or team_id,
            "client_id": _optional_string(message.get("client_msg_id")),
            "parent_author_id": _optional_string(message.get("parent_user_id")),
            "broadcast": outer_subtype == "thread_broadcast"
            or bool(message.get("thread_broadcast", False)),
        }
    )


def _map_deleted(
    event: Mapping[str, Any],
    *,
    channel: str,
    workspace_domain: str,
    team_id: str | None,
    prior_record: Record | None,
) -> Record:
    deleted_ts = _required_string(event.get("deleted_ts"), "deleted_ts")
    record_id = f"{channel}:{deleted_ts}"
    if prior_record is None:
        raise ValueError(
            "Slack message_deleted omits original author/content; "
            "pass prior_record instead of fabricating identity"
        )
    if prior_record.record_id != record_id:
        raise ValueError("prior_record does not match Slack channel + deleted_ts")
    deletion_ts = _required_string(
        event.get("event_ts") or event.get("ts"),
        "event_ts or ts",
    )
    return prior_record.model_copy(
        update={
            "uri": prior_record.uri
            or _permalink(workspace_domain, channel, deleted_ts),
            "revoked_at": _slack_instant(deletion_ts),
            "kind": "revocation",
            "subtype": "message_deleted",
            "workspace_id": prior_record.workspace_id or team_id,
        }
    )


def _author(
    message: Mapping[str, Any],
    users: Mapping[str, Any] | None,
) -> dict[str, Any]:
    bot_id = _optional_string(message.get("bot_id"))
    app_id = _optional_string(message.get("app_id"))
    user_id = _optional_string(message.get("user"))
    bot_profile = message.get("bot_profile")
    if bot_id:
        display = (
            _optional_string(bot_profile.get("name"))
            if isinstance(bot_profile, Mapping)
            else None
        ) or _optional_string(message.get("username"))
        return {"id": bot_id, "display_name": display, "kind": "bot"}
    if app_id:
        return {
            "id": app_id,
            "display_name": _optional_string(message.get("username")),
            "kind": "app",
        }
    if user_id:
        return {
            "id": user_id,
            "display_name": _lookup_name(users, user_id),
            "kind": "human",
        }
    raise ValueError("Slack conversational message has no user, bot_id, or app_id")


def _content(
    message: Mapping[str, Any],
    *,
    users: Mapping[str, Any] | None,
    channels: Mapping[str, Any] | None,
) -> str:
    blocks = message.get("blocks")
    if isinstance(blocks, list):
        rendered = _render_rich_blocks(blocks, users=users, channels=channels)
        if rendered:
            return rendered
    text = message.get("text")
    if not isinstance(text, str):
        if message.get("files"):
            return ""
        raise ValueError("Slack conversational message has no readable text or files")
    return _render_mrkdwn(text, users=users, channels=channels)


def _render_rich_blocks(
    blocks: list[Any],
    *,
    users: Mapping[str, Any] | None,
    channels: Mapping[str, Any] | None,
) -> str:
    rendered = [
        _render_rich_node(block, users=users, channels=channels)
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "rich_text"
    ]
    return "\n".join(part for part in rendered if part).strip()


def _render_rich_node(
    node: Mapping[str, Any],
    *,
    users: Mapping[str, Any] | None,
    channels: Mapping[str, Any] | None,
) -> str:
    node_type = node.get("type")
    elements = node.get("elements")
    if node_type in {"rich_text", "rich_text_section"} and isinstance(elements, list):
        return "".join(
            _render_rich_element(item, users=users, channels=channels)
            for item in elements
            if isinstance(item, Mapping)
        )
    if node_type == "rich_text_list" and isinstance(elements, list):
        ordered = node.get("style") == "ordered"
        lines = []
        for index, item in enumerate(elements, start=1):
            if not isinstance(item, Mapping):
                continue
            prefix = f"{index}. " if ordered else "- "
            lines.append(
                prefix + _render_rich_node(item, users=users, channels=channels)
            )
        return "\n".join(lines)
    if node_type == "rich_text_quote" and isinstance(elements, list):
        body = "".join(
            _render_rich_element(item, users=users, channels=channels)
            for item in elements
            if isinstance(item, Mapping)
        )
        return "\n".join(f"> {line}" for line in body.splitlines())
    if node_type == "rich_text_preformatted" and isinstance(elements, list):
        body = "".join(
            _render_rich_element(item, users=users, channels=channels)
            for item in elements
            if isinstance(item, Mapping)
        )
        return f"```\n{body}\n```"
    return _render_rich_element(node, users=users, channels=channels)


def _render_rich_element(
    element: Mapping[str, Any],
    *,
    users: Mapping[str, Any] | None,
    channels: Mapping[str, Any] | None,
) -> str:
    element_type = element.get("type")
    if element_type and str(element_type).startswith("rich_text_"):
        return _render_rich_node(element, users=users, channels=channels)
    if element_type == "text":
        return html.unescape(str(element.get("text", "")))
    if element_type == "link":
        url = str(element.get("url", ""))
        label = str(element.get("text") or url)
        return label if label == url else f"{label} ({url})"
    if element_type == "user":
        user_id = str(element.get("user_id", ""))
        return f"@{_lookup_name(users, user_id) or user_id}"
    if element_type == "channel":
        channel_id = str(element.get("channel_id", ""))
        return f"#{_lookup_name(channels, channel_id) or channel_id}"
    if element_type == "emoji":
        return f":{element.get('name', '')}:"
    if element_type == "broadcast":
        return f"@{element.get('range', 'channel')}"
    if element_type in {"usergroup", "user_group"}:
        return f"@{element.get('usergroup_id') or element.get('user_group_id') or ''}"
    if element_type == "date":
        return str(element.get("fallback") or element.get("timestamp") or "")
    if element_type in {"file", "attachment_mention"}:
        return f"[file:{element.get('file_id') or element.get('id') or ''}]"
    for key in ("text", "name", "url", "id"):
        value = element.get(key)
        if isinstance(value, str):
            return html.unescape(value)
    return ""


def _render_mrkdwn(
    value: str,
    *,
    users: Mapping[str, Any] | None,
    channels: Mapping[str, Any] | None,
) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        target, separator, label = token.partition("|")
        if target.startswith("@"):
            identifier = target[1:]
            return f"@{label or _lookup_name(users, identifier) or identifier}"
        if target.startswith("#"):
            identifier = target[1:]
            return f"#{label or _lookup_name(channels, identifier) or identifier}"
        if target.startswith("!"):
            if target.startswith("!date"):
                return html.unescape(label or target)
            return f"@{label or target.removeprefix('!')}"
        readable = html.unescape(label or target)
        target = html.unescape(target)
        return readable if not separator or readable == target else f"{readable} ({target})"

    return html.unescape(_ANGLE_TOKEN.sub(replace, value)).strip()


def _reactions(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Slack reactions MUST be an array")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("Slack reactions[] entries MUST be objects")
        users = item.get("users", [])
        if not isinstance(users, list) or not all(isinstance(user, str) for user in users):
            raise ValueError("Slack reaction users MUST be an array of ids")
        result.append(
            {
                "name": _required_string(item.get("name"), "reactions[].name"),
                "count": item.get("count", len(users)),
                "author_ids": list(dict.fromkeys(users)),
            }
        )
    return result


def _files(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("Slack files MUST be an array")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise TypeError("Slack files[] entries MUST be objects")
        file_id = _required_string(item.get("id"), "files[].id")
        result.append(
            {
                "attachment_id": file_id,
                "kind": _optional_string(item.get("mode")) or "file",
                "title": _optional_string(item.get("title") or item.get("name")),
                "mime_type": _optional_string(item.get("mimetype")),
                "uri": _optional_string(
                    item.get("permalink")
                    or item.get("external_url")
                    or item.get("url_private")
                ),
                "size": item.get("size") if isinstance(item.get("size"), int) else None,
            }
        )
    return result


def _permalink(workspace_domain: str, channel: str, ts: str) -> str:
    workspace = workspace_domain.strip().rstrip("/")
    if workspace.startswith("https://"):
        base = workspace
    elif workspace.startswith("http://"):
        raise ValueError("Slack workspace_domain MUST use https")
    else:
        base = f"https://{workspace}"
    if not workspace:
        raise ValueError("Slack workspace_domain is required")
    return f"{base}/archives/{channel}/p{ts.replace('.', '')}"


def _slack_instant(value: str) -> datetime:
    try:
        numeric = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"invalid Slack timestamp: {value}") from error
    seconds = int(numeric)
    micros = int((numeric - seconds) * 1_000_000)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=micros)


def _lookup_name(values: Mapping[str, Any] | None, identifier: str) -> str | None:
    if values is None:
        return None
    value = values.get(identifier)
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        profile = value.get("profile")
        if isinstance(profile, Mapping):
            for key in ("display_name", "real_name"):
                result = _optional_string(profile.get(key))
                if result:
                    return result
        for key in ("name", "display_name", "real_name"):
            result = _optional_string(value.get(key))
            if result:
                return result
    return None


def _required_string(value: Any, field: str) -> str:
    result = _optional_string(value)
    if result is None:
        raise ValueError(f"Slack {field} is required")
    return result


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
