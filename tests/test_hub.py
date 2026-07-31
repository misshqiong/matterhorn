from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session
from typer.testing import CliRunner

from matterhorn.api import create_app
from matterhorn.claude_code import (
    configure_project,
    session_end,
    session_start,
)
from matterhorn.cli.app import app as cli_app
from matterhorn.defaults import Engine
from matterhorn.mcp.server import create_server
from matterhorn.service import MatterhornService

TOOL_NAMES = [
    "add_messages",
    "add_cards",
    "add_records",
    "query_current",
    "query_timeline",
    "query_at",
    "query_by_person",
    "list_matters",
    "correct",
]


def _card() -> dict[str, Any]:
    return {
        "card_id": "hub-card",
        "scope_id": "shared",
        "subject_key": "hub-launch",
        "date": "2026-07-30",
        "title": "Hub launch",
        "status": "open",
        "source_refs": [
            {
                "source_id": "hub-source",
                "sent_at": "2026-07-30T09:00:00Z",
                "sender": "agent-a",
            }
        ],
    }


def _structured(result: Any) -> dict[str, Any]:
    assert result.isError is False
    assert result.structuredContent is not None
    return result.structuredContent


def test_mcp_streamable_http_round_trip_matches_stdio_and_calls_tools(
    tmp_path,
) -> None:
    async def scenario() -> None:
        engine = Engine(tmp_path / "mcp-http.db")
        engine._ingest_cards_sync([_card()])
        service = MatterhornService(engine)
        stdio_server = create_server(service)
        async with create_connected_server_and_client_session(
            stdio_server
        ) as stdio:
            stdio_names = [
                tool.name for tool in (await stdio.list_tools()).tools
            ]

        app = create_app(engine=engine)

        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            follow_redirects=True,
        ) as http_client, streamable_http_client(
            "http://127.0.0.1:8000/mcp",
            http_client=http_client,
        ) as (read_stream, write_stream, _), ClientSession(
            read_stream,
            write_stream,
        ) as session:
            await session.initialize()
            http_names = [
                tool.name
                for tool in (await session.list_tools()).tools
            ]
            assert http_names == stdio_names == TOOL_NAMES

            added = _structured(
                await session.call_tool(
                    "add_messages",
                    {
                        "scope_id": "shared",
                        "messages": [
                            {
                                "id": "claude-message",
                                "sender": {"id": "agent-b"},
                                "text": "The hub is shared.",
                                "sent_at": "2026-07-30T09:05:00Z",
                                "conversation_id": "claude-session",
                            }
                        ],
                    },
                )
            )
            current = _structured(
                await session.call_tool(
                    "query_current",
                    {
                        "scope_id": "shared",
                        "subject_key": "hub-launch",
                        "predicate": "status",
                    },
                )
            )
            corrected = _structured(
                await session.call_tool(
                    "correct",
                    {
                        "correction": {
                            "scope_id": "shared",
                            "subject_key": "hub-launch",
                            "subject_type": "MATTER",
                            "predicate": "status",
                            "object_value": "done",
                            "valid_from": "2026-07-30T09:10:00Z",
                            "source_refs": [
                                {
                                    "source_id": "human-hub-fix",
                                    "sent_at": "2026-07-30T09:10:00Z",
                                    "sender": "Dana",
                                }
                            ],
                        }
                    },
                )
            )
            assert added["ok"] is True
            assert added["data"]["accepted"] == 1
            assert current["data"][0]["value"] == "open"
            assert corrected["ok"] is True

    asyncio.run(scenario())


def test_setup_claude_code_embedded_and_hub_merge_existing_settings(
    tmp_path,
) -> None:
    embedded = tmp_path / "embedded"
    embedded.mkdir()
    (embedded / ".claude").mkdir()
    (embedded / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "existing": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (embedded / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "existing-hook",
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [{"matcher": "Bash", "hooks": []}],
                },
            }
        ),
        encoding="utf-8",
    )

    configure_project(
        embedded,
        url=None,
        scope=None,
        db="memory.db",
        schema="org-matters/v1",
    )

    embedded_mcp = json.loads((embedded / ".mcp.json").read_text())
    assert "existing" in embedded_mcp["mcpServers"]
    assert embedded_mcp["mcpServers"]["matterhorn"] == {
        "type": "stdio",
        "command": "mh",
        "args": ["mcp"],
        "env": {
            "MATTERHORN_DB": str((embedded / "memory.db").resolve()),
            "MATTERHORN_SCHEMA": "org-matters/v1",
        },
    }
    embedded_settings = json.loads(
        (embedded / ".claude" / "settings.json").read_text()
    )
    assert embedded_settings["theme"] == "dark"
    assert embedded_settings["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": []}
    ]
    assert embedded_settings["hooks"]["SessionStart"][0]["hooks"][0][
        "command"
    ] == "existing-hook"

    hub = tmp_path / "hub"
    hub.mkdir()
    configure_project(
        hub,
        url="http://127.0.0.1:8123/",
        scope="agent-team",
        db="ignored.db",
        schema="org-matters/v1",
    )
    assert json.loads((hub / ".mcp.json").read_text()) == {
        "mcpServers": {
            "matterhorn": {
                "type": "http",
                "url": "http://127.0.0.1:8123/mcp",
            }
        }
    }
    hub_settings = json.loads(
        (hub / ".claude" / "settings.json").read_text()
    )
    assert hub_settings["hooks"]["SessionEnd"][-1]["hooks"][0] == {
            "type": "command",
            "command": (
                "mh hook session-end --url http://127.0.0.1:8123 --scope agent-team"
            ),
            "timeout": 2,
        }


def test_setup_claude_code_cli_writes_project_files(
    monkeypatch,
    tmp_path,
) -> None:
    import matterhorn.claude_code as integration

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(integration, "probe_health", lambda: False)
    result = CliRunner().invoke(
        cli_app,
        [
            "setup",
            "claude-code",
            "--url",
            "http://127.0.0.1:9000",
            "--scope",
            "shared",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / ".claude" / "settings.json").is_file()

    embedded = tmp_path / "embedded-hint"
    embedded.mkdir()
    monkeypatch.chdir(embedded)
    monkeypatch.setattr(integration, "probe_health", lambda: True)
    hinted = CliRunner().invoke(cli_app, ["setup", "claude-code"])
    assert hinted.exit_code == 0, hinted.output
    assert "Embedded mode was kept." in hinted.output
    entry = json.loads((embedded / ".mcp.json").read_text())["mcpServers"][
        "matterhorn"
    ]
    assert entry["type"] == "stdio"
    assert "url" not in entry


def test_session_end_posts_minimal_messages_and_session_start_prints_context(
    monkeypatch,
    tmp_path,
) -> None:
    import matterhorn.claude_code as integration

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "timestamp": "2026-07-30T10:00:00Z",
                        "message": {
                            "role": "user",
                            "content": "Ship the shared hub.",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "timestamp": "2026-07-30T10:01:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "The hub is ready.",
                                },
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "input": {},
                                },
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def fake_request(url, *, method="GET", payload=None, timeout):
        calls.append(
            {
                "url": url,
                "method": method,
                "payload": payload,
                "timeout": timeout,
            }
        )
        if method == "GET":
            return [
                {
                    "title": "Open launch",
                    "status": "open",
                    "owners": ["agent-a"],
                    "next_step": "Run walkthrough",
                    "due": None,
                    "subject_key": "open-launch",
                },
                {
                    "title": "Old launch",
                    "status": "done",
                    "owners": [],
                    "subject_key": "old-launch",
                },
            ]
        return {"accepted": 2, "task_id": "task-hook"}

    monkeypatch.setattr(integration, "_request_json", fake_request)
    hook_payload = json.dumps(
        {
            "session_id": "session-1",
            "transcript_path": str(transcript),
            "hook_event_name": "SessionEnd",
            "agent_type": "release-agent",
        }
    )
    session_end(
        io.StringIO(hook_payload),
        url="http://127.0.0.1:8000",
        scope="agent-team",
    )
    posted = calls[-1]
    assert posted["url"].endswith("/v1/scopes/agent-team/messages")
    assert posted["method"] == "POST"
    assert posted["payload"]["wait"] is False
    assert posted["payload"]["messages"] == [
        {
            "id": "claude-code:session-1:user-1",
            "sender": {"id": "user"},
            "text": "Ship the shared hub.",
            "sent_at": "2026-07-30T10:00:00Z",
            "conversation_id": "claude-code:session-1",
        },
        {
            "id": "claude-code:session-1:assistant-1",
            "sender": {"id": "release-agent"},
            "text": "The hub is ready.",
            "sent_at": "2026-07-30T10:01:00Z",
            "conversation_id": "claude-code:session-1",
        },
    ]

    output = io.StringIO()
    session_start(
        io.StringIO(
            json.dumps(
                {
                    "session_id": "session-2",
                    "transcript_path": str(transcript),
                    "hook_event_name": "SessionStart",
                    "source": "startup",
                }
            )
        ),
        output,
        url="http://127.0.0.1:8000",
        scope="agent-team",
    )
    assert output.getvalue() == (
        "Matterhorn open matters (scope: agent-team)\n"
        "- Open launch [open · owners: agent-a · next: Run walkthrough]\n"
    )


def test_hooks_service_down_and_malformed_input_are_silent_and_fast(
    tmp_path,
) -> None:
    start = time.monotonic()
    down = subprocess.run(
        [
            sys.executable,
            "-m",
            "matterhorn.cli",
            "hook",
            "session-start",
            "--url",
            "http://127.0.0.1:1",
            "--scope",
            "test",
        ],
        input=json.dumps(
            {
                "session_id": "down",
                "transcript_path": str(tmp_path / "missing.jsonl"),
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        ),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    elapsed = time.monotonic() - start
    assert down.returncode == 0
    assert elapsed < 2
    assert down.stdout == ""
    assert down.stderr == ""

    transcript = tmp_path / "down.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "down-user",
                "timestamp": "2026-07-30T10:00:00Z",
                "message": {"role": "user", "content": "Still fail open."},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    end_start = time.monotonic()
    down_end = subprocess.run(
        [
            sys.executable,
            "-m",
            "matterhorn.cli",
            "hook",
            "session-end",
            "--url",
            "http://127.0.0.1:1",
            "--scope",
            "test",
        ],
        input=json.dumps(
            {
                "session_id": "down-end",
                "transcript_path": str(transcript),
                "hook_event_name": "SessionEnd",
                "reason": "other",
            }
        ),
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert down_end.returncode == 0
    assert time.monotonic() - end_start < 2
    assert down_end.stdout == ""
    assert down_end.stderr == ""

    malformed = subprocess.run(
        [
            sys.executable,
            "-m",
            "matterhorn.cli",
            "hook",
            "session-end",
            "--url",
            "http://127.0.0.1:1",
            "--scope",
            "test",
        ],
        input="{not-json",
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert malformed.returncode == 0
    assert malformed.stdout == ""
    assert malformed.stderr == ""


def test_console_contains_live_hub_panels_and_five_second_poll(tmp_path) -> None:
    async def scenario() -> None:
        app = create_app(
            engine=Engine(tmp_path / "console-hub.db"),
            console_enabled=True,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            page = await client.get("/console")
        assert page.status_code == 200
        for marker in [
            'id="connection-list"',
            'id="activity-list"',
            'api("/v1/events?limit=40")',
            'api("/v1/connections")',
            "loadScopes()",
            "loadMatters()",
            "}, 5000);",
        ]:
            assert marker in page.text

    asyncio.run(scenario())


def test_activity_and_connections_endpoints_are_public_and_cross_scope(
    tmp_path,
) -> None:
    class MailStatus:
        def status(self):
            return {
                "configured": False,
                "config": None,
                "scope_id": None,
                "password_state": "re-enter password",
                "last_sync_at": None,
                "last_run_at": None,
                "next_run_at": None,
                "syncing": False,
                "uid_watermark": None,
                "uidvalidity": None,
                "last_report": None,
                "error": None,
            }

        def tick(self):
            return None

    async def scenario() -> None:
        engine = Engine(
            tmp_path / "connections.db",
            clock=lambda: datetime(2026, 7, 30, 12, tzinfo=UTC),
        )
        engine._ingest_cards_sync(
            [
                _card(),
                {
                    **_card(),
                    "card_id": "other-card",
                    "scope_id": "other",
                    "subject_key": "other-launch",
                    "title": "Other launch",
                    "source_refs": [
                        {
                            "source_id": "other-source",
                            "sent_at": "2026-07-30T09:00:00Z",
                            "sender": "agent-c",
                        }
                    ],
                },
            ]
        )
        engine.add(
            scope_id="shared",
            messages=[
                {
                    "id": "message-count",
                    "sender": {"id": "agent-a"},
                    "text": "Count this message.",
                    "sent_at": "2026-07-30T11:00:00Z",
                }
            ],
        )
        with engine.store.transaction():
            engine.store.mark_record_observation(
                "shared",
                "C1:1",
                "observation-hash",
                "C1",
                datetime(2026, 7, 30, 11, 30, tzinfo=UTC),
            )
        app = create_app(
            engine=engine,
            mail_runtime=MailStatus(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            activity = await client.get("/v1/events")
            connections = await client.get("/v1/connections")
            paths = (await client.get("/openapi.json")).json()["paths"]

        assert activity.status_code == 200
        assert {item["scope_id"] for item in activity.json()} == {
            "other",
            "shared",
        }
        shared_activity = next(
            item for item in activity.json() if item["scope_id"] == "shared"
        )
        assert shared_activity["matter_title"] == "Hub launch"
        assert connections.status_code == 200
        scope = next(
            item
            for item in connections.json()["scopes"]
            if item["scope_id"] == "shared"
        )
        assert scope["scope_id"] == "shared"
        assert scope["message_count"] == 2
        assert scope["last_ingestion_at"] is not None
        assert connections.json()["distill_queue_length"] == (
            engine.store.distill_queue_count("shared")
            + engine.store.distill_queue_count("other")
        )
        assert "/v1/events" in paths
        assert "/v1/connections" in paths

    asyncio.run(scenario())
