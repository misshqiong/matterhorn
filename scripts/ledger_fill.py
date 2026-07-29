"""Fill Matterhorn's self-hosted development ledger.

The default source path reads this repository's NUL-delimited git history,
tracked devlog author dates, and GitHub issues/comments via ``gh api``. A real
write gateway is selected from the same MATTERHORN_* / provider-specific
environment conventions as ``mh dream``. Tests and offline review runs pass an
explicit fixture gateway and JSON source fixtures; no silent fixture fallback
exists.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from matterhorn.adapters.github import (
    GIT_LOG_FORMAT,
    map_devlog,
    map_git_log,
    map_github_issues,
)
from matterhorn.contracts import Record
from matterhorn.engine import Engine
from matterhorn.gateway_config import configured_gateway

DEFAULT_OWNER = "misshqiong"
DEFAULT_REPO = "matterhorn"
DEFAULT_SCOPE = "dev"


@dataclass(frozen=True)
class GatewaySelection:
    gateway: Any
    label: str


@dataclass(frozen=True)
class FillResult:
    gateway: str
    sources: dict[str, int]
    add_records: dict[str, Any]
    queued_before_flush: int
    flush: dict[str, Any]
    gate: dict[str, Any]
    matters: list[dict[str, Any]]
    evidence: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_gateway(
    *,
    provider: str | None = None,
    fixture_path: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> GatewaySelection:
    """Resolve a write gateway before any database or ingest side effect."""

    env = os.environ if environ is None else environ
    if fixture_path is not None:
        path = Path(fixture_path).resolve()
        gateway = configured_gateway(provider="fixture", fixture_path=path)
        return GatewaySelection(gateway, f"fixture ({path})")

    selected = provider or env.get("MATTERHORN_PROVIDER")
    if selected == "fixture":
        configured_path = env.get("MATTERHORN_FIXTURE_PATH")
        if not configured_path:
            raise ValueError(
                "MATTERHORN_PROVIDER=fixture requires MATTERHORN_FIXTURE_PATH"
            )
        path = Path(configured_path).resolve()
        gateway = configured_gateway(provider="fixture", fixture_path=path)
        return GatewaySelection(gateway, f"fixture ({path})")

    if selected is None:
        if env.get("OPENAI_API_KEY"):
            selected = "openai-compatible"
        elif env.get("ANTHROPIC_API_KEY"):
            selected = "anthropic"
        elif env.get("MATTERHORN_API_KEY") and env.get("MATTERHORN_BASE_URL"):
            selected = "openai-compatible"
        elif env.get("MATTERHORN_API_KEY"):
            raise ValueError(
                "MATTERHORN_API_KEY is set but the provider is ambiguous; "
                "set MATTERHORN_PROVIDER to openai-compatible or anthropic"
            )
        else:
            raise ValueError(
                "no usable LLM credential is configured; set "
                "MATTERHORN_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY "
                "with the mh dream provider settings, or pass --fixture-gateway "
                "for an explicit deterministic offline run"
            )

    if selected not in {"openai-compatible", "anthropic"}:
        raise ValueError(
            "ledger fill provider MUST be openai-compatible or anthropic"
        )
    credential_name = (
        "MATTERHORN_API_KEY"
        if env.get("MATTERHORN_API_KEY")
        else (
            "OPENAI_API_KEY"
            if selected == "openai-compatible"
            else "ANTHROPIC_API_KEY"
        )
    )
    gateway = configured_gateway(provider=selected)
    return GatewaySelection(
        gateway,
        f"{selected} ({credential_name}, model={env.get('MATTERHORN_MODEL')})",
    )


def fill_records(
    *,
    db_path: str | Path,
    records: list[Record],
    gateway_selection: GatewaySelection,
    scope_id: str = DEFAULT_SCOPE,
    source_counts: dict[str, int] | None = None,
) -> FillResult:
    """Ingest unseen provider Records, drain the write queue, and read back."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(path, gateway=gateway_selection.gateway)
    try:
        add_report = engine.add_records(records, scope_id=scope_id)
        queued_before_flush = engine.store.distill_queue_count(scope_id)
        flush_report = engine.flush(scope_id)
        queued_after_flush = engine.store.distill_queue_count(scope_id)
        if queued_after_flush:
            raise RuntimeError(
                f"ledger fill left {queued_after_flush} distillation item(s) queued"
            )

        matters = [item.to_dict() for item in engine.matters(scope_id)]
        evidence = []
        for subject in engine.query.list_matters(scope_id):
            for predicate in engine.profile.predicates:
                if predicate.subject != subject.subject_type:
                    continue
                timeline = engine.query.timeline(
                    scope_id,
                    subject.subject_key,
                    predicate.name,
                )
                if not timeline:
                    continue
                evidence.append(
                    {
                        "subject_key": subject.subject_key,
                        "title": subject.title,
                        "predicate": predicate.name,
                        "timeline": [item.to_dict() for item in timeline],
                    }
                )
        return FillResult(
            gateway=gateway_selection.label,
            sources=source_counts or {"records": len(records)},
            add_records=add_report.model_dump(mode="json"),
            queued_before_flush=queued_before_flush,
            flush=flush_report.model_dump(mode="json"),
            gate=engine.gate_statistics(scope_id).model_dump(mode="json"),
            matters=matters,
            evidence=evidence,
        )
    finally:
        engine.store.close()


def collect_records(args: argparse.Namespace) -> tuple[list[Record], dict[str, int]]:
    root = args.repo_root.resolve()
    git_payload = (
        _read_git_fixture(args.git_log_file)
        if args.git_log_file
        else _git_log(root)
    )
    commits = map_git_log(git_payload, owner=args.owner, repo=args.repo)

    devlog_dates = _devlog_date_overrides(args.devlog_date)
    devlog_entries = []
    for path in sorted((root / "devlog").glob("*.md")):
        authored_at = devlog_dates.get(path.name) or _git_author_date(root, path)
        if authored_at is None:
            raise ValueError(
                f"{path.name} has no git author date yet; pass "
                f"--devlog-date {path.name}=RFC3339 and reuse the same value "
                "on retries"
            )
        devlog_entries.append((path, authored_at))
    devlogs = map_devlog(
        devlog_entries,
        owner=args.owner,
        repo=args.repo,
    )

    if args.issues_json:
        issues_payload = _read_json(args.issues_json)
        comments_payload = (
            _read_json(args.comments_json) if args.comments_json else []
        )
    else:
        issues_payload, comments_payload = _github_api(
            args.owner, args.repo, root
        )
    issues = map_github_issues(
        issues_payload,
        comments_payload,
        owner=args.owner,
        repo=args.repo,
    )
    return (
        [*commits, *devlogs, *issues],
        {
            "commits": len(commits),
            "devlogs": len(devlogs),
            "issues_and_comments": len(issues),
            "total": len(commits) + len(devlogs) + len(issues),
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--db", type=Path, default=Path("ledger/dev.db"))
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--provider",
        choices=["openai-compatible", "anthropic"],
        help="Override MATTERHORN_PROVIDER using mh dream conventions.",
    )
    parser.add_argument(
        "--fixture-gateway",
        type=Path,
        help="Explicit deterministic gateway JSON for tests/offline review.",
    )
    parser.add_argument(
        "--git-log-file",
        type=Path,
        help="Test/offline captured git-log JSON containing an output field.",
    )
    parser.add_argument(
        "--issues-json",
        type=Path,
        help="Captured gh-api issues JSON; otherwise call gh api.",
    )
    parser.add_argument(
        "--comments-json",
        type=Path,
        help="Captured gh-api issue-comments JSON used with --issues-json.",
    )
    parser.add_argument(
        "--devlog-date",
        action="append",
        default=[],
        metavar="FILE=RFC3339",
        help="Git author date override for an uncommitted devlog; repeatable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        gateway = select_gateway(
            provider=args.provider,
            fixture_path=args.fixture_gateway,
        )
        records, counts = collect_records(args)
        result = fill_records(
            db_path=args.db,
            records=records,
            gateway_selection=gateway,
            scope_id=args.scope,
            source_counts=counts,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        print(f"ledger fill failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def _git_log(root: Path) -> bytes:
    command = ["git", "log", f"--pretty=format:{GIT_LOG_FORMAT}"]
    result = subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(
            "git log failed: " + result.stderr.decode("utf-8", errors="replace")
        )
    return result.stdout


def _git_author_date(root: Path, path: Path) -> str | None:
    relative = path.relative_to(root)
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=format:%aI", "--", str(relative)],
        cwd=root,
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("git log failed for devlog: " + result.stderr)
    return result.stdout.strip() or None


def _github_api(owner: str, repo: str, root: Path) -> tuple[Any, Any]:
    issues = _gh_json(
        root,
        f"repos/{owner}/{repo}/issues?state=all&per_page=100",
    )
    comments = []
    for issue in _flatten_pages(issues):
        number = issue.get("number")
        if isinstance(number, int):
            comments.extend(
                _flatten_pages(
                    _gh_json(
                        root,
                        f"repos/{owner}/{repo}/issues/{number}/comments?per_page=100",
                    )
                )
            )
    return issues, comments


def _gh_json(root: Path, endpoint: str) -> Any:
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        cwd=root,
        check=False,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"gh api failed for {endpoint}: {result.stderr}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"gh api returned invalid JSON for {endpoint}") from error


def _flatten_pages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise TypeError("gh api --slurp output MUST be an array")
    result = []
    for item in payload:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, list) and all(
            isinstance(nested, dict) for nested in item
        ):
            result.extend(item)
        else:
            raise TypeError("gh api page entries MUST be objects")
    return result


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_git_fixture(path: Path) -> str:
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), str):
        raise TypeError("--git-log-file MUST contain a JSON object with output")
    return payload["output"]


def _devlog_date_overrides(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, instant = value.partition("=")
        if not separator or not name or not instant:
            raise ValueError("--devlog-date MUST be FILE=RFC3339")
        result[name] = instant
    return result


if __name__ == "__main__":
    raise SystemExit(main())
