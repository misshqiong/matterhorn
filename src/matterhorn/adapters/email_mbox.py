"""Pure RFC email-file normalization into traceable Matterhorn Records.

The adapter reads host-supplied mbox or EML data and performs no network or
model calls.
"""

from __future__ import annotations

import html
import mailbox
import re
import tempfile
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from email import policy
from email.message import Message as EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from matterhorn.contracts import Message, Record

EMAIL_CONTAINER_ID = "email"
_ANGLE_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:(?:re|fwd|fw|aw)\s*:|(?:回复|转发)\s*[：:]|答复\s*:)\s*",
    re.IGNORECASE,
)
_HTML_BLOCK_TAGS = frozenset({"br", "div", "p"})
_HTML_IGNORED_TAGS = frozenset({"head", "script", "style"})
_MACHINE_SENDER_LOCAL_PARTS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "notifications",
        "notification",
        "mailer-daemon",
        "bounce",
        "bounces",
    }
)


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
    return map_email_bytes(payload)


def map_email_bytes(
    payload: bytes,
    *,
    container_id: str = EMAIL_CONTAINER_ID,
) -> EmailMappingResult:
    """Map one RFC822 payload with an optional provider container namespace."""

    message = BytesParser(policy=policy.default).parsebytes(payload)
    return _map_messages([message], container_id=container_id)


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


def map_email_message(
    message: EmailMessage,
    *,
    container_id: str = EMAIL_CONTAINER_ID,
) -> Record | None:
    """Map one parsed email, or return None for a deliberately dropped mail."""

    record, _ = _map_email_message(message, container_id=container_id)
    return record


def coalesce_email_conversations(records: list[Record]) -> list[Record]:
    """Align roots with reference-linked replies visible in the same pull."""

    referenced_roots: set[tuple[str, str]] = set()
    for record in records:
        if record.thread_id is None:
            continue
        prefix = f"{record.container_id}:message-id:"
        if record.thread_id.startswith(prefix):
            referenced_roots.add(
                (record.container_id, record.thread_id.removeprefix(prefix))
            )
    result = []
    for record in records:
        identity = (record.container_id, record.native_id or "")
        subject_prefix = f"{record.container_id}:subject:"
        if (
            identity in referenced_roots
            and record.thread_id is not None
            and record.thread_id.startswith(subject_prefix)
        ):
            result.append(
                record.model_copy(
                    update={
                        "thread_id": (
                            f"{record.container_id}:message-id:{record.native_id}"
                        )
                    }
                )
            )
        else:
            result.append(record)
    return result


def _map_email_message(
    message: EmailMessage,
    *,
    container_id: str,
) -> tuple[Record | None, str | None]:
    """Map one parsed email and retain an adapter-level drop reason."""

    message_id = _required_message_id(message.get("Message-ID"))
    drop_reason = _machine_mail_drop_reason(message)
    if drop_reason is not None:
        return None, drop_reason
    sent_at = _sent_at(message)
    sender_id, sender_name = _single_address(message.get_all("From", []), "From")
    recipients = _addresses(
        [
            *message.get_all("To", []),
            *message.get_all("Cc", []),
        ]
    )
    subject = str(message.get("Subject") or "").strip()
    body = _readable_body(message)
    if not body.strip():
        return None, "EMPTY_CONTENT"
    native_id = message_id
    conversation_key = _conversation_key(message, message_id, subject)
    return (
        Record.model_validate(
            {
                "record_id": f"{container_id}:{native_id}",
                "native_id": native_id,
                "container_id": container_id,
                "thread_id": f"{container_id}:{conversation_key}",
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
            },
        ),
        None,
    )


def email_record_to_message(record: Record) -> Message:
    """Use an email Record at Matterhorn's minimal public add boundary."""

    thread_prefix = f"{record.container_id}:"
    conversation_key = (
        record.thread_id.removeprefix(thread_prefix)
        if record.thread_id is not None
        else None
    )
    return Message.model_validate(
        {
            "id": record.native_id
            or record.record_id.removeprefix(f"{record.container_id}:"),
            "sender": {
                "id": record.author.id,
                "name": record.author.display_name,
            },
            "text": record.content,
            "sent_at": record.sent_at,
            "conversation_id": f"mail:{record.container_id}",
            "reply_to": conversation_key,
        }
    )


def _map_messages(
    messages: Iterable[EmailMessage],
    *,
    container_id: str = EMAIL_CONTAINER_ID,
) -> EmailMappingResult:
    records: list[Record] = []
    dropped: Counter[str] = Counter()
    for message in messages:
        record, reason = _map_email_message(message, container_id=container_id)
        if reason is not None:
            dropped[reason] += 1
        else:
            assert record is not None
            records.append(record)
    return EmailMappingResult(
        records=sorted(
            coalesce_email_conversations(records),
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


def _conversation_key(
    message: EmailMessage,
    own_id: str,
    subject: str,
) -> str:
    references = _message_ids(message.get("References"))
    if references:
        return f"message-id:{references[0]}"
    parent = _message_ids(message.get("In-Reply-To"))
    if parent:
        return f"message-id:{parent[0]}"
    normalized_subject = normalize_email_subject(subject)
    return (
        f"subject:{normalized_subject}"
        if normalized_subject
        else f"message-id:{own_id}"
    )


def normalize_email_subject(value: str) -> str:
    """Return the deterministic conversation fallback for an email subject."""

    normalized = value
    while True:
        stripped = _SUBJECT_PREFIX.sub("", normalized, count=1)
        if stripped == normalized:
            break
        normalized = stripped
    return " ".join(normalized.split()).casefold()


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


def _sent_at(message: EmailMessage):
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


def _readable_body(message: EmailMessage) -> str:
    parts = list(message.walk()) if message.is_multipart() else [message]
    for content_type in ("text/plain", "text/html"):
        for part in parts:
            if (
                part.get_content_type() != content_type
                or part.get_content_disposition() == "attachment"
            ):
                continue
            content = part.get_content()
            text = content if isinstance(content, str) else content.decode()
            return text if content_type == "text/plain" else _html_to_text(text)
    return ""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        _attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.casefold()
        if normalized in _HTML_IGNORED_TAGS:
            self.ignored_depth += 1
        elif self.ignored_depth == 0 and normalized in _HTML_BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in _HTML_IGNORED_TAGS:
            self.ignored_depth = max(0, self.ignored_depth - 1)
        elif self.ignored_depth == 0 and normalized in {"div", "p"}:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth == 0:
            self.chunks.append(data)


def _html_to_text(value: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(value)
    extractor.close()
    raw = html.unescape("".join(extractor.chunks))
    lines = [
        " ".join(line.split())
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _machine_mail_drop_reason(message: EmailMessage) -> str | None:
    precedence = str(message.get("Precedence") or "").strip().casefold()
    if precedence == "bulk":
        return "bulk"
    if message.get("List-Unsubscribe") is not None:
        return "unsubscribe"
    senders = _addresses(message.get_all("From", []))
    if len(senders) == 1 and _is_machine_sender(senders[0][0]):
        return "noreply"
    auto_submitted = str(message.get("Auto-Submitted") or "").strip().casefold()
    if auto_submitted and auto_submitted != "no":
        return "auto-submitted"
    return None


def _is_machine_sender(address: str) -> bool:
    local_part, separator, _domain = address.casefold().partition("@")
    if not separator:
        return False
    base_local_part = local_part.split("+", 1)[0]
    return base_local_part in _MACHINE_SENDER_LOCAL_PARTS


# TODO: strip quoted reply chains after v1 has an explicit, tested policy.
# TODO: remove signatures after v1 has a deterministic signature boundary.
