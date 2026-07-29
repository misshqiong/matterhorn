from __future__ import annotations

import json
from pathlib import Path

import pytest

from matterhorn.adapters import (
    map_slack_event,
    map_slack_history,
    map_slack_message,
)

FIXTURES = Path(__file__).parent / "fixtures" / "slack"
USERS = {
    "U123": {"profile": {"display_name": "Ada"}},
    "U456": {"profile": {"display_name": "Bob"}},
    "U789": {"profile": {"display_name": "Cara"}},
}
CHANNELS = {"CENG": {"name": "engineering"}}


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_slack_history_maps_modern_content_authors_files_and_reactions() -> None:
    result = map_slack_history(
        _fixture("conversations-history.json"),
        channel_id="C0123",
        workspace_domain="matterhorn.slack.com",
        users=USERS,
        channels=CHANNELS,
    )
    assert len(result.records) == 2
    assert result.dropped == {"NON_CONVERSATIONAL:channel_join": 1}
    assert result.next_cursor == "dGVzdC1uZXh0LWN1cnNvcg=="
    root, reply = result.records
    assert root.record_id == "C0123:1699887654.123456"
    assert root.native_id == "1699887654.123456"
    assert root.thread_id == root.record_id
    assert root.author.model_dump() == {
        "id": "U123",
        "display_name": "Ada",
        "kind": "human",
    }
    assert root.content == (
        "Release & docs are ready for @Bob in #engineering. "
        "Runbook (https://example.com/runbook)"
    )
    assert root.uri == (
        "https://matterhorn.slack.com/archives/"
        "C0123/p1699887654123456"
    )
    assert root.reactions[0].count == 5
    assert root.reactions[0].author_ids == ["U123", "U456"]
    assert root.attachments[0].attachment_id == "F123"
    assert root.attachments[0].mime_type == "application/pdf"
    assert reply.author.kind.value == "bot"
    assert reply.author.id == "BVERIFY"
    assert reply.broadcast is True
    assert reply.parent_author_id == "U123"
    assert reply.content == "I will verify build 42 (https://example.com/build/42)"


def test_slack_mrkdwn_fallback_is_readable() -> None:
    record = map_slack_message(
        {
            "type": "message",
            "channel": "C1",
            "user": "U123",
            "ts": "1699887654.123456",
            "text": (
                "A &amp; B for <@U456> in <#CENG|engineering>: "
                "<https://example.com/x|runbook>"
            ),
        },
        workspace_domain="matterhorn.slack.com",
        users=USERS,
        channels=CHANNELS,
    )
    assert record is not None
    assert record.content == (
        "A & B for @Bob in #engineering: runbook (https://example.com/x)"
    )


def test_slack_edit_and_delete_events_preserve_identity_and_require_prior() -> None:
    edited = map_slack_event(
        _fixture("message-changed.json"),
        workspace_domain="matterhorn.slack.com",
        users=USERS,
    )
    assert edited is not None
    assert edited.record_id == "C0123:1699887654.123456"
    assert edited.subtype == "message_changed"
    assert edited.content == "Release is open."
    assert edited.edited_at.isoformat() == "2023-11-13T15:02:34.000001+00:00"

    deleted_payload = _fixture("message-deleted.json")
    with pytest.raises(ValueError, match="pass prior_record"):
        map_slack_event(
            deleted_payload,
            workspace_domain="matterhorn.slack.com",
        )
    deleted = map_slack_event(
        deleted_payload,
        workspace_domain="matterhorn.slack.com",
        prior_record=edited,
    )
    assert deleted is not None
    assert deleted.record_id == edited.record_id
    assert deleted.author == edited.author
    assert deleted.content == edited.content
    assert deleted.kind == "revocation"
    assert deleted.revoked_at.isoformat() == "2023-11-13T15:04:14.000002+00:00"


def test_same_slack_ts_in_different_channels_has_distinct_record_ids() -> None:
    first = map_slack_history(
        _fixture("conversations-history.json"),
        channel_id="C0123",
        workspace_domain="matterhorn.slack.com",
        users=USERS,
    ).records[0]
    second = map_slack_history(
        _fixture("same-ts-other-channel.json"),
        channel_id="C9999",
        workspace_domain="matterhorn.slack.com",
        users=USERS,
    ).records[0]
    assert first.native_id == second.native_id == "1699887654.123456"
    assert first.record_id == "C0123:1699887654.123456"
    assert second.record_id == "C9999:1699887654.123456"
    assert first.record_id != second.record_id


def test_slack_adapter_fails_loudly_without_traceable_identity() -> None:
    with pytest.raises(ValueError, match="Slack ts is required"):
        map_slack_message(
            {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "text": "No timestamp",
            },
            workspace_domain="matterhorn.slack.com",
        )
