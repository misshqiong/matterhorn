"""Claude Code project setup and fail-open lifecycle hooks."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any, TextIO
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_HUB_URL = "http://127.0.0.1:8000"
HOOK_TIMEOUT_SECONDS = 1.5
_TERMINAL_STATUSES = frozenset(
    {"cancelled", "closed", "complete", "completed", "done", "resolved"}
)


def configure_project(
    project_dir: Path,
    *,
    url: str | None,
    scope: str | None,
    db: str,
    schema: str,
) -> tuple[Path, Path, str]:
    """Merge Matterhorn MCP and hook entries into a Claude Code project."""

    project_dir = project_dir.resolve()
    selected_scope = scope or project_dir.name
    if not selected_scope:
        raise ValueError("Claude Code scope MUST be non-empty")
    selected_url = normalize_hub_url(url) if url is not None else None

    mcp_path = project_dir / ".mcp.json"
    mcp_payload = _load_json_object(mcp_path)
    servers = mcp_payload.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise TypeError(f"{mcp_path} field mcpServers MUST be an object")
    if selected_url is None:
        servers["matterhorn"] = {
            "type": "stdio",
            "command": "mh",
            "args": ["mcp"],
            "env": {
                "MATTERHORN_DB": _project_db_value(project_dir, db),
                "MATTERHORN_SCHEMA": schema,
            },
        }
    else:
        servers["matterhorn"] = {
            "type": "http",
            "url": f"{selected_url}/mcp",
        }
    _write_json(mcp_path, mcp_payload)

    settings_path = project_dir / ".claude" / "settings.json"
    settings = _load_json_object(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise TypeError(f"{settings_path} field hooks MUST be an object")
    hook_plan = [
        ("SessionStart", "session-start"),
        ("SessionEnd", "session-end"),
    ]
    if selected_url is not None:
        # Hub mode also ships each finished turn; deterministic record ids
        # make the overlapping re-delivery a no-op, so matters stay live
        # without waiting for session exit.
        hook_plan.append(("Stop", "turn-end"))
    for event, command_name in hook_plan:
        _merge_hook(
            hooks,
            event=event,
            command_name=command_name,
            scope=selected_scope,
            url=selected_url,
        )
    _write_json(settings_path, settings)
    return mcp_path, settings_path, selected_scope


def normalize_hub_url(url: str) -> str:
    selected = url.strip().rstrip("/")
    if not selected.startswith(("http://", "https://")):
        raise ValueError("--url MUST start with http:// or https://")
    selected = selected.removesuffix("/mcp")
    if not selected:
        raise ValueError("--url MUST identify a Matterhorn service")
    return selected


def probe_health(url: str = DEFAULT_HUB_URL, *, timeout: float = 0.2) -> bool:
    try:
        payload = _request_json(f"{normalize_hub_url(url)}/healthz", timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def session_end(
    stdin: TextIO,
    *,
    url: str | None,
    scope: str,
    tail: int | None = None,
) -> None:
    """Best-effort transcript delivery; every failure is intentionally silent.

    ``tail`` bounds the delivery to the most recent messages: the per-turn
    Stop hook re-delivers overlapping windows and relies on deterministic
    record ids to make replays free (P9), so payloads stay small on long
    sessions.
    """

    if url is None:
        return
    deadline = time.monotonic() + HOOK_TIMEOUT_SECONDS
    try:
        hook = json.load(stdin)
        if not isinstance(hook, dict):
            return
        transcript_path = hook.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path:
            return
        messages = transcript_messages(
            Path(transcript_path),
            hook,
            deadline=deadline,
        )
        if tail is not None and len(messages) > tail:
            messages = messages[-tail:]
        if not messages:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        _request_json(
            (
                f"{normalize_hub_url(url)}/v1/scopes/"
                f"{quote(scope, safe='')}/messages"
            ),
            method="POST",
            payload={"messages": messages, "wait": False},
            timeout=remaining,
        )
    except Exception:  # noqa: BLE001
        return


def session_start(
    stdin: TextIO,
    stdout: TextIO,
    *,
    url: str | None,
    scope: str,
) -> None:
    """Best-effort open-matter context; every failure is intentionally silent."""

    if url is None:
        return
    deadline = time.monotonic() + HOOK_TIMEOUT_SECONDS
    try:
        hook = json.load(stdin)
        if not isinstance(hook, dict):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        matters = _request_json(
            (
                f"{normalize_hub_url(url)}/v1/scopes/"
                f"{quote(scope, safe='')}/matters"
            ),
            timeout=remaining,
        )
        if not isinstance(matters, list):
            return
        block = open_matters_context(scope, matters)
        if block:
            stdout.write(block)
            stdout.write("\n")
    except Exception:  # noqa: BLE001
        return


def transcript_messages(
    path: Path,
    hook: dict[str, Any],
    *,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Extract text-bearing user/assistant records from Claude's current JSONL."""

    session_id = str(hook.get("session_id") or path.stem)
    agent_name = str(hook.get("agent_type") or "claude-code")
    conversation_id = f"claude-code:{session_id}"
    messages: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as transcript:
        for index, line in enumerate(transcript):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Claude transcript extraction exceeded its budget")
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            message = record.get("message")
            if record_type not in {"user", "assistant"} or not isinstance(
                message, dict
            ):
                continue
            role = message.get("role", record_type)
            if role != record_type:
                continue
            text = _message_text(message.get("content"))
            timestamp = record.get("timestamp") or message.get("timestamp")
            if not text or not _valid_timestamp(timestamp):
                continue
            native_id = record.get("uuid")
            if not isinstance(native_id, str) or not native_id:
                native_id = hashlib.sha256(
                    (
                        f"{session_id}\0{index}\0{record_type}\0{text}\0"
                        f"{timestamp}"
                    ).encode()
                ).hexdigest()
            sender_id = "user" if record_type == "user" else agent_name
            messages.append(
                {
                    "id": f"claude-code:{session_id}:{native_id}",
                    "sender": {"id": sender_id},
                    "text": text,
                    "sent_at": timestamp,
                    "conversation_id": conversation_id,
                }
            )
    return messages


def open_matters_context(scope: str, matters: list[Any]) -> str:
    rows: list[str] = []
    for matter in matters:
        if not isinstance(matter, dict):
            continue
        status = matter.get("status")
        if (
            isinstance(status, str)
            and status.casefold() in _TERMINAL_STATUSES
        ):
            continue
        title = matter.get("title")
        if not isinstance(title, str) or not title:
            continue
        owners = matter.get("owners")
        owner_text = (
            ", ".join(str(owner) for owner in owners)
            if isinstance(owners, list) and owners
            else "unassigned"
        )
        suffixes = [
            str(status or "no status"),
            f"owners: {owner_text}",
        ]
        next_step = matter.get("next_step")
        if next_step:
            suffixes.append(f"next: {next_step}")
        due = matter.get("due")
        if due:
            suffixes.append(f"due: {due}")
        rows.append(f"- {title} [{' · '.join(suffixes)}]")
    if not rows:
        return ""
    return "\n".join(
        [
            f"Matterhorn open matters (scope: {scope})",
            *rows[:20],
        ]
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = [
        item["text"]
        for item in content
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
    ]
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _project_db_value(project_dir: Path, db: str) -> str:
    if "://" in db:
        return db
    path = Path(db).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return str(path.resolve())


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not load {path}: {error}") from error
    if not isinstance(value, dict):
        raise TypeError(f"{path} MUST contain a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.matterhorn-{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _merge_hook(
    hooks: dict[str, Any],
    *,
    event: str,
    command_name: str,
    scope: str,
    url: str | None,
) -> None:
    existing = hooks.get(event, [])
    if not isinstance(existing, list):
        raise TypeError(f"Claude Code hooks.{event} MUST be an array")
    retained: list[Any] = []
    for group in existing:
        if not isinstance(group, dict) or not isinstance(
            group.get("hooks"), list
        ):
            retained.append(group)
            continue
        handlers = [
            handler
            for handler in group["hooks"]
            if not _is_matterhorn_hook(handler, command_name)
        ]
        if handlers:
            retained.append({**group, "hooks": handlers})
    parts = [_mh_executable(), "hook", command_name]
    if url is not None:
        parts.extend(["--url", url])
    parts.extend(["--scope", scope])
    # Claude Code hook entries take one shell command string; there is no
    # args field in the hooks schema.
    command = " ".join(shlex.quote(part) for part in parts)
    retained.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": 2,
                }
            ]
        }
    )
    hooks[event] = retained


def _mh_executable() -> str:
    """Absolute path of the running mh so hooks work without PATH setup."""

    candidate = pathlib.Path(sys.argv[0]).resolve()
    if candidate.name in {"mh", "mh.exe"} and candidate.is_file():
        return str(candidate)
    found = shutil.which("mh")
    if found:
        return found
    return "mh"


def _is_matterhorn_hook(handler: Any, command_name: str) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
        return (
            len(tokens) >= 3
            and pathlib.Path(tokens[0]).name in {"mh", "mh.exe"}
            and tokens[1:3] == ["hook", command_name]
        )
    # Migrate entries written by the earlier command+args shape.
    args = handler.get("args")
    return (
        command == "mh"
        and isinstance(args, list)
        and len(args) >= 2
        and args[:2] == ["hook", command_name]
    )


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> Any:
    result: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def request() -> None:
        try:
            result.put(
                (
                    True,
                    _request_json_blocking(
                        url,
                        method=method,
                        payload=payload,
                        timeout=timeout,
                    ),
                )
            )
        except Exception as error:  # noqa: BLE001
            result.put((False, error))

    worker = Thread(target=request, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"request exceeded {timeout:.3f}s")
    succeeded, value = result.get_nowait()
    if succeeded:
        return value
    raise value


def _request_json_blocking(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> Any:
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    request = Request(
        url,
        data=body,
        method=method,
        headers=({"Content-Type": "application/json"} if body is not None else {}),
    )
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        return json.loads(response.read())
