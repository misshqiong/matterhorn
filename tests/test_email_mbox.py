from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from matterhorn.adapters.email_mbox import (
    map_email_bytes,
    map_email_message,
    map_eml,
)


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
    root_message = _message()
    root_message["References"] = "<root@project.example>"
    root = map_email_message(root_message)
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
    assert root.thread_id == "email:message-id:root@project.example"
    assert reply.thread_id == root.thread_id
    assert reply.author.id == "mira@project.example"
    assert "Theo Rill <theo@vendor.example>" in reply.content
    assert "Sora Pell <sora@project.example>" in reply.content


def test_email_subject_fallback_strips_reply_prefixes_including_chinese() -> None:
    records = []
    for message_id, subject in [
        ("plain@project.example", " Project   Update "),
        ("reply@project.example", "Re: Fwd: project update"),
        ("cn@project.example", "回复： 回复: PROJECT UPDATE"),
    ]:
        record = map_email_message(
            _message(message_id=f"<{message_id}>", subject=subject)
        )
        assert record is not None
        records.append(record)

    assert {record.thread_id for record in records} == {
        "email:subject:project update"
    }


def test_bulk_email_is_filtered_with_distinct_reason() -> None:
    message = _message()
    message["Precedence"] = "BuLk"

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.records == []
    assert mapped.dropped == {"bulk": 1}


def test_list_unsubscribe_email_is_filtered_with_distinct_reason() -> None:
    message = _message()
    message["List-Unsubscribe"] = "<mailto:leave@project.example>"

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.records == []
    assert mapped.dropped == {"unsubscribe": 1}


@pytest.mark.parametrize(
    "sender",
    [
        "No Reply <NO-REPLY@project.example>",
        "Notifications <notifications+travel@project.example>",
        "Mailer <mailer-daemon@project.example>",
    ],
)
def test_noreply_sender_is_filtered_with_distinct_reason(sender: str) -> None:
    message = _message()
    message.replace_header("From", sender)

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.records == []
    assert mapped.dropped == {"noreply": 1}


def test_auto_submitted_email_is_filtered_with_distinct_reason() -> None:
    message = _message()
    message["Auto-Submitted"] = "auto-generated"

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.records == []
    assert mapped.dropped == {"auto-submitted": 1}


def test_real_human_email_is_not_filtered() -> None:
    message = _message()
    message["Auto-Submitted"] = "no"

    mapped = map_email_bytes(message.as_bytes())

    assert len(mapped.records) == 1
    assert mapped.dropped == {}


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


def test_html_only_message_extracts_readable_text() -> None:
    message = _message(message_id="<html@project.example>")
    message.set_content(
        """
        <html>
          <head><title>Hidden title</title><style>.secret { color: red; }</style></head>
          <body>
            <p>The launch is <strong>ready</strong>.</p>
            <script>window.alert("hidden")</script>
            <div>Owner: Mira</div>
          </body>
        </html>
        """,
        subtype="html",
    )

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.dropped == {}
    assert len(mapped.records) == 1
    assert "The launch is ready.\nOwner: Mira" in mapped.records[0].content
    assert "Hidden title" not in mapped.records[0].content
    assert "window.alert" not in mapped.records[0].content


def test_nested_multipart_alternative_prefers_text_plain() -> None:
    message = _message(message_id="<alternative@project.example>")
    alternative = EmailMessage()
    alternative.set_content("Plain text wins.")
    alternative.add_alternative(
        "<p>HTML text must not win.</p>",
        subtype="html",
    )
    message.clear_content()
    message.make_mixed()
    message.attach(alternative)

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.dropped == {}
    assert "Plain text wins." in mapped.records[0].content
    assert "HTML text must not win." not in mapped.records[0].content


def test_html_empty_after_extraction_is_dropped_with_count() -> None:
    message = _message(message_id="<empty-html@project.example>")
    message.set_content(
        "<html><head><title>Ignored</title></head>"
        "<body><script>ignored()</script><style>ignored</style></body></html>",
        subtype="html",
    )

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.records == []
    assert mapped.dropped == {"EMPTY_CONTENT": 1}


def test_html_entities_and_breaks_are_readable() -> None:
    message = _message(message_id="<entities@project.example>")
    message.set_content(
        "<p>R&amp;D&nbsp;approved &lt;Plan A&gt;<br>Ship&nbsp;&nbsp;now</p>",
        subtype="html",
    )

    mapped = map_email_bytes(message.as_bytes())

    assert mapped.dropped == {}
    assert "R&D approved <Plan A>\nShip now" in mapped.records[0].content
