"""Server-side raw input detection for the public ingest resource."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import yaml
from pydantic import ValidationError

from matterhorn.adapters.email_mbox import (
    email_record_to_message,
    map_email_text,
)
from matterhorn.contracts import Message
from matterhorn.errors import IngestFormatError

FORMAT_ERROR = (
    "Input must be one of: plain chat lines (`Dana Reyes: status is open`); "
    "YAML/JSON minimal messages (`messages: [{id: m1, sender: {id: dana}, "
    "text: status is open, sent_at: 2026-07-29T09:00:00Z}]`); or raw "
    "EML/mbox email (`From: Dana Reyes <dana@octo-org.example>` plus Date "
    "and Message-ID headers)."
)
_CHAT_LINE = re.compile(r"^(?P<name>[^:\r\n]{1,80}):\s+(?P<text>\S.*)$")
_EMAIL_HEADERS = ("from:", "date:", "message-id:")


@dataclass(frozen=True)
class DetectedIngest:
    input_format: str
    messages: list[Message]
    synthesized_timestamps: bool = False


def detect_ingest(
    text: str,
    *,
    now: Callable[[], datetime] | None = None,
    batch_id: str | None = None,
) -> DetectedIngest:
    """Detect chat lines, the minimal message contract, or raw email."""

    stripped = text.strip()
    if not stripped:
        raise IngestFormatError(FORMAT_ERROR)
    if _looks_like_email(text):
        return _email_messages(text)

    structured = _structured_messages(stripped)
    if structured is not None:
        return DetectedIngest("messages", structured)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    parsed = [_CHAT_LINE.fullmatch(line) for line in lines]
    if lines and all(parsed):
        clock = now or (lambda: datetime.now(UTC))
        start = clock()
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        selected_batch = batch_id or uuid.uuid4().hex
        messages = [
            Message.model_validate(
                {
                    "id": f"{selected_batch}-{index}",
                    "sender": {
                        "id": _sender_id(match.group("name")),
                        "name": match.group("name").strip(),
                    },
                    "text": match.group("text").strip(),
                    "sent_at": start + timedelta(seconds=index - 1),
                    "conversation_id": f"console-{selected_batch}",
                }
            )
            for index, match in enumerate(parsed, start=1)
            if match is not None
        ]
        return DetectedIngest("chat", messages, synthesized_timestamps=True)

    raise IngestFormatError(FORMAT_ERROR)


def _structured_messages(text: str) -> list[Message] | None:
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(payload, dict) and "messages" in payload:
        values = payload["messages"]
    elif isinstance(payload, list):
        values = payload
    else:
        return None
    if not isinstance(values, list):
        raise IngestFormatError(FORMAT_ERROR)
    try:
        return [Message.model_validate(item) for item in values]
    except (ValidationError, TypeError) as error:
        raise IngestFormatError(f"Invalid minimal message contract: {error}") from error


def _looks_like_email(text: str) -> bool:
    lowered = text.casefold()
    if text.startswith("From "):
        return True
    return all(
        re.search(rf"(?m)^{re.escape(header)}", lowered)
        for header in _EMAIL_HEADERS
    )


def _email_messages(text: str) -> DetectedIngest:
    try:
        mapped = map_email_text(text)
    except (TypeError, ValueError) as error:
        raise IngestFormatError(f"Invalid EML/mbox input: {error}") from error
    messages = [email_record_to_message(record) for record in mapped.records]
    if not messages:
        raise IngestFormatError("Email input contained no human messages.")
    return DetectedIngest("email", messages)


def _sender_id(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return normalized or "unknown"
