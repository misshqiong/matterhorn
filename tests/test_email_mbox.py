from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from matterhorn.adapters.email_mbox import map_email_message, map_eml


def _message(
    *,
    message_id: str | None = "<root@project.example>",
    subject: str = "Project update",
    body: str = "The work is in progress.",
) -> EmailMessage:
    message = EmailMessage()
    if message_id is not None:
        message["Message-ID"] = message_id
    message["Date"] = "Tue, 12 May 2026 09:00:00 +0000"
    message["From"] = "Mira Venn <mira@project.example>"
    message["To"] = "Theo Rill <theo@vendor.example>"
    message["Cc"] = "Sora Pell <sora@project.example>"
    message["Subject"] = subject
    message.set_content(body)
    return message


def test_email_threading_uses_references_root_and_namespaced_ids() -> None:
    root = map_email_message(_message())
    reply_message = _message(
        message_id="<reply@vendor.example>",
        subject="Re: Project update",
    )
    reply_message["In-Reply-To"] = "<middle@project.example>"
    reply_message["References"] = (
        "<root@project.example> <middle@project.example>"
    )
    reply = map_email_message(reply_message)

    assert root is not None
    assert reply is not None
    assert root.record_id == "email:root@project.example"
    assert reply.record_id == "email:reply@vendor.example"
    assert root.thread_id == "email:root@project.example"
    assert reply.thread_id == root.thread_id
    assert reply.author.id == "mira@project.example"
    assert "Theo Rill <theo@vendor.example>" in reply.content
    assert "Sora Pell <sora@project.example>" in reply.content


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Precedence", "bulk"),
        ("Auto-Submitted", "auto-generated"),
    ],
)
def test_automated_email_is_filtered(header: str, value: str) -> None:
    message = _message()
    message[header] = value

    assert map_email_message(message) is None


def test_unicode_subject_sender_and_body_are_preserved() -> None:
    message = _message(
        message_id="<unicode@project.example>",
        subject="交付确认 — café",
        body="你好，München 团队确认方案 B。\n\n谢谢，\n米拉",
    )
    message.replace_header(
        "From",
        "米拉 · Venn <mira@project.example>",
    )

    record = map_email_message(message)

    assert record is not None
    assert record.author.display_name == "米拉 · Venn"
    assert "Subject: 交付确认 — café" in record.content
    assert "你好，München 团队确认方案 B。" in record.content


def test_missing_message_id_fails_loudly() -> None:
    with pytest.raises(ValueError, match="Message-ID is required"):
        map_email_message(_message(message_id=None))


def test_single_eml_file_maps_to_one_record(tmp_path: Path) -> None:
    path = tmp_path / "one.eml"
    path.write_bytes(_message().as_bytes())

    mapped = map_eml(path)

    assert mapped.dropped == {}
    assert [item.record_id for item in mapped.records] == [
        "email:root@project.example"
    ]
