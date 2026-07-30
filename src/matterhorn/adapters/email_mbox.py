"""Pure RFC email-file normalization into traceable Matterhorn Records.

The adapter reads host-supplied mbox or EML data and performs no network or
model calls. Version 1 deliberately supports clean plaintext bodies only.
"""

from __future__ import annotations

import mailbox
import re
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from matterhorn.contracts import Record

EMAIL_CONTAINER_ID = "email"
_ANGLE_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")


@dataclass(frozen=True)
class EmailMappingResult:
    records: list[Record]
    dropped: dict[str, int]


def map_email_file(path: str | Path) -> EmailMappingResult:
    """Map a host-supplied `.mbox` mailbox or one `.eml` message."""

    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix == ".mbox":
        return map_mbox(source)
    if suffix == ".eml":
        return map_eml(source)
    raise ValueError("email input MUST use a .mbox or .eml filename")


def map_email_text(text: str) -> EmailMappingResult:
    """Map raw EML or mbox text through the ordinary email adapter."""

    payload = text.encode("utf-8")
    if text.startswith("From "):
        with tempfile.NamedTemporaryFile(suffix=".mbox") as handle:
            handle.write(payload)
            handle.flush()
            return map_mbox(handle.name)
    message = BytesParser(policy=policy.default).parsebytes(payload)
    return _map_messages([message])


def map_mbox(path: str | Path) -> EmailMappingResult:
    """Map an mbox file while preserving deterministic message ordering."""

    source = Path(path)
    parsed = (
        BytesParser(policy=policy.default).parsebytes(item.as_bytes())
        for item in mailbox.mbox(source, create=False)
    )
    return _map_messages(parsed)


def map_eml(path: str | Path) -> EmailMappingResult:
    """Map one EML file."""

    source = Path(path)
    message = BytesParser(policy=policy.default).parsebytes(source.read_bytes())
    return _map_messages([message])


def map_email_message(message: Message) -> Record | None:
    """Map one parsed email, or return None for automated mail."""

    message_id = _required_message_id(message.get("Message-ID"))
    if _is_automated(message):
        return None
    sent_at = _sent_at(message)
    sender_id, sender_name = _single_address(message.get_all("From", []), "From")
    recipients = _addresses(
        [
            *message.get_all("To", []),
            *message.get_all("Cc", []),
        ]
    )
    subject = str(message.get("Subject") or "").strip()
    body = _plaintext_body(message)
    native_id = message_id
    root_id = _thread_root(message, message_id)
    return Record.model_validate(
        {
            "record_id": f"{EMAIL_CONTAINER_ID}:{native_id}",
            "native_id": native_id,
            "container_id": EMAIL_CONTAINER_ID,
            "thread_id": f"{EMAIL_CONTAINER_ID}:{root_id}",
            "sent_at": sent_at,
            "author": {
                "id": sender_id,
                "display_name": sender_name or sender_id,
                "kind": "human",
            },
            # Record has no recipient collection. Preserve To/Cc losslessly in
            # the normalized text that the record extractor receives.
            "content": _content(subject, recipients, body),
            "kind": "message",
            "subtype": "email",
        }
    )


def _map_messages(messages: Iterable[Message]) -> EmailMappingResult:
    records: list[Record] = []
    dropped: Counter[str] = Counter()
    for message in messages:
        record = map_email_message(message)
        if record is None:
            dropped["AUTOMATED"] += 1
        else:
            records.append(record)
    return EmailMappingResult(
        records=sorted(
            records,
            key=lambda item: (
                item.sent_at,
                item.record_id.encode("utf-8"),
            ),
        ),
        dropped=dict(sorted(dropped.items())),
    )


def _required_message_id(value: str | None) -> str:
    identifiers = _message_ids(value)
    if not identifiers:
        raise ValueError("email Message-ID is required for traceable identity")
    return identifiers[0]


def _thread_root(message: Message, own_id: str) -> str:
    references = _message_ids(message.get("References"))
    if references:
        return references[0]
    parent = _message_ids(message.get("In-Reply-To"))
    return parent[0] if parent else own_id


def _message_ids(value: str | None) -> list[str]:
    if value is None:
        return []
    matched = _ANGLE_MESSAGE_ID.findall(str(value))
    if matched:
        return matched
    return [
        item.strip().strip("<>")
        for item in re.split(r"[\s,]+", str(value))
        if item.strip().strip("<>")
    ]


def _sent_at(message: Message):
    raw = message.get("Date")
    if raw is None:
        raise ValueError("email Date is required")
    try:
        value = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid email Date: {raw}") from error
    if value is None:
        raise ValueError(f"invalid email Date: {raw}")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _single_address(values: list[str], field: str) -> tuple[str, str | None]:
    parsed = _addresses(values)
    if len(parsed) != 1:
        raise ValueError(f"email {field} MUST contain exactly one address")
    return parsed[0]


def _addresses(values: list[str]) -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    for name, address in getaddresses(values):
        normalized = address.strip()
        if not normalized:
            continue
        result.append((normalized, name.strip() or None))
    return result


def _content(
    subject: str,
    recipients: list[tuple[str, str | None]],
    body: str,
) -> str:
    rendered_recipients = ", ".join(
        f"{name} <{address}>" if name else address
        for address, name in recipients
    )
    return (
        f"Subject: {subject}\n"
        f"To: {rendered_recipients}\n\n"
        f"{body.strip()}"
    )


def _plaintext_body(message: Message) -> str:
    if not message.is_multipart():
        content_type = message.get_content_type()
        if content_type != "text/plain":
            raise ValueError(
                f"email v1 requires text/plain content, got {content_type}"
            )
        content = message.get_content()
        return content if isinstance(content, str) else content.decode()
    for part in message.walk():
        if (
            part.get_content_type() == "text/plain"
            and part.get_content_disposition() != "attachment"
        ):
            content = part.get_content()
            return content if isinstance(content, str) else content.decode()
    raise ValueError("email v1 requires a text/plain body")


def _is_automated(message: Message) -> bool:
    precedence = str(message.get("Precedence") or "").strip().casefold()
    auto_submitted = str(message.get("Auto-Submitted") or "").strip().casefold()
    return precedence == "bulk" or bool(
        auto_submitted and auto_submitted != "no"
    )


# TODO: strip quoted reply chains after v1 has an explicit, tested policy.
# TODO: normalize HTML-only mail after v1 has a safe text conversion contract.
# TODO: remove signatures after v1 has a deterministic signature boundary.
