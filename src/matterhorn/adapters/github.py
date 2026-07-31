"""Pure GitHub development-history normalization.

The git adapter expects output from this exact command shape::

    git log --pretty=format:%H%x00%aN%x00%aE%x00%aI%x00%s%x00%b%x00

NUL is the field and record delimiter because Git commit messages cannot
contain NUL bytes. Newlines and other Unicode in commit bodies are therefore
preserved without relying on a line-oriented parser.

Git author identities use the email-free, readable ``git:<name-slug>`` form.
The slug is derived only from the case-folded display name; the raw name stays
available as ``display_name``. Consequently, two people who commit under the
same display name but different email addresses intentionally merge into one
person id. That collision trade-off is accepted for a readable development
ledger, and no email address is included in either mapped identity field.

The issue adapter accepts the JSON arrays returned by GitHub's repository
issues and issue-comments REST endpoints. ``gh api --paginate --slurp`` output
(an array of page arrays) is accepted as well. The module performs no network
calls and never calls an LLM.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from matterhorn.contracts import Record

GIT_LOG_FORMAT = "%H%x00%aN%x00%aE%x00%aI%x00%s%x00%b%x00"
GIT_LOG_FIELDS = 6
_COMMIT_SHA = re.compile(r"[0-9a-f]{40,64}")
_DEVLOG_NAME = re.compile(r"(?P<number>[0-9]{4})-[a-z0-9][a-z0-9-]*\.md")


def map_git_log(
    payload: str | bytes,
    *,
    owner: str,
    repo: str,
) -> list[Record]:
    """Map NUL-delimited ``git log`` output to deterministic Records."""

    text = _decode(payload)
    if not text:
        return []
    fields = text.split("\0")
    if fields[-1] != "":
        raise ValueError("git log payload MUST end with a NUL delimiter")
    fields.pop()
    if len(fields) % GIT_LOG_FIELDS:
        raise ValueError(
            "git log payload does not match the documented six-field format"
        )

    container_id = _repo_container(owner, repo)
    records = []
    for offset in range(0, len(fields), GIT_LOG_FIELDS):
        sha, author_name, author_email, author_date, subject, body = fields[
            offset : offset + GIT_LOG_FIELDS
        ]
        sha = sha.strip()
        if _COMMIT_SHA.fullmatch(sha) is None:
            raise ValueError(f"invalid commit SHA in git log payload: {sha!r}")
        name = _required_text(author_name, "git author name")
        _required_text(author_email, "git author email")
        subject = _required_text(subject, "git commit subject")
        native_id = f"commit:{sha}"
        records.append(
            Record.model_validate(
                {
                    "record_id": f"{container_id}:{native_id}",
                    "native_id": native_id,
                    "container_id": container_id,
                    "sent_at": _required_instant(author_date, "git author date"),
                    "author": {
                        "id": _git_author_id(name),
                        "display_name": name,
                        "kind": "human",
                    },
                    "content": _join_title_body(subject, body),
                    "uri": (
                        f"https://github.com/{owner}/{repo}/commit/{sha}"
                    ),
                    "kind": "commit",
                    "workspace_id": f"{owner}/{repo}",
                }
            )
        )
    return records


def map_github_issues(
    issues_payload: Any,
    comments_payload: Any = None,
    *,
    owner: str,
    repo: str,
) -> list[Record]:
    """Map GitHub issue/PR and issue-comment REST JSON to Records.

    ``issues_payload`` is the response from ``GET /repos/{owner}/{repo}/issues``.
    That endpoint includes pull requests and marks them with ``pull_request``.
    ``comments_payload`` may be a flat repository comment array, one endpoint
    array, or page arrays emitted by ``gh api --paginate --slurp``.
    """

    issues = _api_objects(issues_payload, "issues")
    if not issues:
        if _api_objects(comments_payload, "comments"):
            raise ValueError("GitHub comments cannot be mapped without parent issues")
        return []

    container_id = _repo_container(owner, repo)
    roots: dict[int, tuple[str, bool]] = {}
    records = []
    for issue in issues:
        number = _required_int(issue.get("number"), "issues[].number")
        native_id = f"issue:{number}"
        record_id = f"{container_id}:{native_id}"
        is_pull_request = isinstance(issue.get("pull_request"), Mapping)
        roots[number] = (record_id, is_pull_request)
        created_at = _required_instant(
            issue.get("created_at"), "issues[].created_at"
        )
        records.append(
            Record.model_validate(
                {
                    "record_id": record_id,
                    "native_id": native_id,
                    "container_id": container_id,
                    "thread_id": record_id,
                    "sent_at": created_at,
                    "author": _github_author(
                        issue.get("user"), "issues[].user"
                    ),
                    "content": _join_title_body(
                        _required_text(issue.get("title"), "issues[].title"),
                        _optional_body(issue.get("body"), "issues[].body"),
                    ),
                    "uri": _github_html_url(
                        issue.get("html_url"), owner=owner, repo=repo
                    ),
                    "kind": "pull_request" if is_pull_request else "issue",
                    "subtype": str(issue.get("state") or "unknown"),
                    "workspace_id": f"{owner}/{repo}",
                }
            )
        )

    for comment in _api_objects(comments_payload, "comments"):
        issue_number = _comment_issue_number(comment.get("issue_url"))
        parent = roots.get(issue_number)
        if parent is None:
            raise ValueError(
                f"GitHub comment references unknown issue {issue_number}"
            )
        comment_id = _required_int(comment.get("id"), "comments[].id")
        native_id = f"issue-comment:{comment_id}"
        created_at = _required_instant(
            comment.get("created_at"), "comments[].created_at"
        )
        updated_at = _required_instant(
            comment.get("updated_at"), "comments[].updated_at"
        )
        records.append(
            Record.model_validate(
                {
                    "record_id": f"{container_id}:{native_id}",
                    "native_id": native_id,
                    "container_id": container_id,
                    "thread_id": parent[0],
                    "sent_at": created_at,
                    "edited_at": updated_at if updated_at > created_at else None,
                    "author": _github_author(
                        comment.get("user"), "comments[].user"
                    ),
                    "content": _required_text(
                        comment.get("body"), "comments[].body"
                    ),
                    "uri": _github_html_url(
                        comment.get("html_url"), owner=owner, repo=repo
                    ),
                    "kind": "comment",
                    "subtype": (
                        "pull_request_comment"
                        if parent[1]
                        else "issue_comment"
                    ),
                    "workspace_id": f"{owner}/{repo}",
                }
            )
        )
    return sorted(records, key=lambda item: (item.sent_at, item.record_id))


def map_devlog(
    files: Iterable[tuple[str | Path, str | datetime]],
    *,
    owner: str,
    repo: str,
    maintainer_id: str | None = None,
    maintainer_name: str | None = None,
    branch: str = "main",
) -> list[Record]:
    """Map ``devlog/NNNN-*.md`` files using caller-supplied git author dates."""

    author_id = maintainer_id or f"github:{owner}"
    author_name = maintainer_name or owner
    records = []
    for path_value, author_date in files:
        path = Path(path_value)
        match = _DEVLOG_NAME.fullmatch(path.name)
        if match is None or path.parent.name != "devlog":
            raise ValueError(
                "devlog path MUST match devlog/NNNN-lowercase-slug.md"
            )
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            raise ValueError(f"devlog file is empty: {path}")
        number = match.group("number")
        native_id = number
        records.append(
            Record.model_validate(
                {
                    "record_id": f"devlog:{native_id}",
                    "native_id": native_id,
                    "container_id": "devlog",
                    "sent_at": _required_instant(
                        author_date, f"git author date for {path.name}"
                    ),
                    "author": {
                        "id": author_id,
                        "display_name": author_name,
                        "kind": "human",
                    },
                    "content": content,
                    "uri": (
                        f"https://github.com/{owner}/{repo}/blob/"
                        f"{branch}/devlog/{path.name}"
                    ),
                    "kind": "document",
                    "subtype": "devlog",
                    "workspace_id": f"{owner}/{repo}",
                }
            )
        )
    return sorted(records, key=lambda item: item.record_id)


def _api_objects(payload: Any, field: str) -> list[Mapping[str, Any]]:
    if payload is None:
        return []
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        raise TypeError(f"GitHub {field} payload MUST be an array")
    result: list[Mapping[str, Any]] = []
    for item in payload:
        if isinstance(item, Mapping):
            result.append(item)
            continue
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            for nested in item:
                if not isinstance(nested, Mapping):
                    raise TypeError(
                        f"GitHub {field} page entries MUST be objects"
                    )
                result.append(nested)
            continue
        raise TypeError(f"GitHub {field} entries MUST be objects or page arrays")
    return result


def _repo_container(owner: str, repo: str) -> str:
    return (
        "github:"
        + _required_text(owner, "repository owner")
        + "/"
        + _required_text(repo, "repository name")
    )


def _git_author_id(name: str) -> str:
    folded = name.casefold()
    slug_chars = []
    for char in folded:
        if char.isspace():
            slug_chars.append("-")
        elif char == "-" or (char != "_" and re.fullmatch(r"\w", char)):
            slug_chars.append(char)
    slug = re.sub(r"-+", "-", "".join(slug_chars)).strip("-")
    if not slug:
        suffix = hashlib.sha256(folded.encode("utf-8")).hexdigest()[:12]
        slug = f"author-{suffix}"
    return f"git:{slug}"


def _github_author(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"GitHub {field} MUST be an object")
    login = _required_text(value.get("login"), f"{field}.login")
    identifier = _required_int(value.get("id"), f"{field}.id")
    user_type = value.get("type")
    kind = (
        "bot"
        if user_type == "Bot" or login.casefold().endswith("[bot]")
        else "human"
    )
    return {
        "id": f"github-user:{identifier}",
        "display_name": login,
        "kind": kind,
    }


def _github_html_url(value: Any, *, owner: str, repo: str) -> str:
    url = _required_text(value, "html_url")
    parsed = urlparse(url)
    prefix = f"/{owner}/{repo}/".casefold()
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or not parsed.path.casefold().startswith(prefix)
    ):
        raise ValueError(
            "GitHub html_url MUST be a public https://github.com/"
            f"{owner}/{repo}/... URL"
        )
    return url


def _comment_issue_number(value: Any) -> int:
    url = _required_text(value, "comments[].issue_url")
    try:
        number = int(url.rstrip("/").rsplit("/", 1)[1])
    except (IndexError, ValueError) as error:
        raise ValueError("GitHub comments[].issue_url has no issue number") from error
    if number < 1:
        raise ValueError("GitHub issue number MUST be positive")
    return number


def _join_title_body(title: str, body: str | None) -> str:
    normalized_body = (body or "").rstrip("\n")
    return title if not normalized_body else f"{title}\n\n{normalized_body}"


def _optional_body(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"GitHub {field} MUST be a string or null")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _required_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"GitHub {field} MUST be a positive integer")
    return value


def _required_instant(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value:
        try:
            result = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"{field} MUST be an RFC 3339 datetime") from error
    else:
        raise ValueError(f"{field} is required")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{field} MUST include a UTC offset")
    return result


def _decode(payload: str | bytes) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("git log payload MUST be UTF-8") from error
    raise TypeError("git log payload MUST be str or bytes")
