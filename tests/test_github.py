from __future__ import annotations

import json
from pathlib import Path

import pytest

from matterhorn.adapters.github import (
    GIT_LOG_FORMAT,
    map_devlog,
    map_git_log,
    map_github_issues,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_map_git_log_preserves_bodies_unicode_urls_and_author_identity() -> None:
    fixture = _fixture("git-log.json")
    assert fixture["format"] == GIT_LOG_FORMAT

    records = map_git_log(
        fixture["output"],
        owner="misshqiong",
        repo="matterhorn",
    )

    assert len(records) == 2
    first, second = records
    assert first.native_id == "commit:" + "1" * 40
    assert first.record_id == "github:misshqiong/matterhorn:" + first.native_id
    assert first.uri == (
        "https://github.com/misshqiong/matterhorn/commit/" + "1" * 40
    )
    assert first.content == (
        "Add temporal ledger\n\n"
        "First paragraph.\n\nSecond paragraph with 中文 and café."
    )
    assert second.content == "Document zero-model reads"
    assert first.author.id == second.author.id
    assert first.author.display_name == "Ada Lovelace <ada@example.com>"
    assert first.sent_at.isoformat() == "2026-07-28T09:10:11+00:00"


def test_map_git_log_fails_loudly_on_wrong_delimiter_shape() -> None:
    with pytest.raises(ValueError, match="six-field format"):
        map_git_log(
            "1" * 40 + "\0Ada\0ada@example.com\0",
            owner="misshqiong",
            repo="matterhorn",
        )


def test_map_github_issues_maps_issue_pr_and_comments() -> None:
    records = map_github_issues(
        _fixture("issues.json"),
        _fixture("issue-comments.json"),
        owner="misshqiong",
        repo="matterhorn",
    )
    by_native_id = {record.native_id: record for record in records}

    issue = by_native_id["issue:7"]
    assert issue.kind == "issue"
    assert issue.thread_id == issue.record_id
    assert issue.content.endswith("This includes Unicode: 证据。")
    assert issue.uri == "https://github.com/misshqiong/matterhorn/issues/7"

    pull_request = by_native_id["issue:8"]
    assert pull_request.kind == "pull_request"
    assert pull_request.content == "Add the GitHub adapter"
    assert pull_request.uri == "https://github.com/misshqiong/matterhorn/pull/8"

    issue_comment = by_native_id["issue-comment:9001"]
    assert issue_comment.thread_id == issue.record_id
    assert issue_comment.subtype == "issue_comment"

    pr_comment = by_native_id["issue-comment:9002"]
    assert pr_comment.thread_id == pull_request.record_id
    assert pr_comment.subtype == "pull_request_comment"
    assert pr_comment.author.kind.value == "bot"
    assert pr_comment.edited_at is not None
    assert pr_comment.uri.endswith("/pull/8#issuecomment-9002")


def test_map_github_issues_handles_empty_repository() -> None:
    assert (
        map_github_issues(
            _fixture("issues-empty.json"),
            [],
            owner="misshqiong",
            repo="matterhorn",
        )
        == []
    )


def test_map_devlog_uses_supplied_git_date_and_future_public_url(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "devlog"
    directory.mkdir()
    path = directory / "0001-project-strategy.md"
    path.write_text("# Strategy\n\nEvidence-backed.\n", encoding="utf-8")

    first = map_devlog(
        [(path, "2026-07-29T12:00:00Z")],
        owner="misshqiong",
        repo="matterhorn",
    )
    second = map_devlog(
        [(path, "2026-07-29T12:00:00Z")],
        owner="misshqiong",
        repo="matterhorn",
    )

    assert first == second
    assert first[0].record_id == "devlog:0001"
    assert first[0].author.id == "github:misshqiong"
    assert first[0].content == "# Strategy\n\nEvidence-backed.\n"
    assert first[0].uri == (
        "https://github.com/misshqiong/matterhorn/"
        "blob/main/devlog/0001-project-strategy.md"
    )
