from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from matterhorn.api import create_app
from matterhorn.contracts import SchemaProfile
from matterhorn.engine import Engine


class ExtractingGateway:
    def complete(self, **kwargs) -> str:
        if kwargs["response_schema"].get("$id") == (
            "matterhorn-identity-adjudication/v1"
        ):
            return json.dumps(
                {
                    "decision": "new",
                    "subject_key": None,
                    "confidence": 1.0,
                    "evidence_source_ids": [],
                }
            )
        payload = json.loads(kwargs["user"])
        if "records" not in payload:
            return json.dumps({"candidates": []})
        source_id = payload["records"][0]["source_alias"]
        return json.dumps(
            {
                "cards": [
                    {
                        "date": "2026-01-01",
                        "title": "Record thing",
                        "status": "open",
                        "source_ids": [source_id],
                    }
                ]
            }
        )


def _profile() -> SchemaProfile:
    return SchemaProfile.model_validate(
        {
            "schema": "protocol/v1",
            "subjects": [{"type": "THING", "primary": True}],
            "predicates": [
                {
                    "name": "phase",
                    "subject": "THING",
                    "cardinality": "SINGLE",
                    "extraction": "deterministic",
                    "source_field": "status",
                },
                {
                    "name": "owner",
                    "subject": "THING",
                    "cardinality": "SET",
                    "extraction": "deterministic",
                    "object": "person",
                    "source_field": "participants",
                    "extraction_rule": "participant_ids",
                },
            ],
        }
    )


def _card():
    return {
        "card_id": "c1",
        "scope_id": "s",
        "subject_key": "thing-1",
        "date": "2026-01-01",
        "title": "Thing",
        "status": "open",
        "participants": [{"id": "p1"}],
        "source_refs": [
            {
                "source_id": "m1",
                "sent_at": "2026-01-01T10:00:00Z",
                "sender": "u",
            }
        ],
    }


def _engine(tmp_path) -> Engine:
    return Engine(
        tmp_path / "protocol.db",
        _profile(),
        gateway=ExtractingGateway(),
        clock=lambda: datetime(2026, 1, 1, 13, tzinfo=UTC),
    )


def _record():
    return {
        "record_id": "C1:1.000001",
        "native_id": "1.000001",
        "container_id": "C1",
        "sent_at": "2026-01-01T10:30:00Z",
        "author": {"id": "u", "kind": "human"},
        "content": "Record thing is open.",
        "uri": "https://example.slack.com/archives/C1/p1000001",
        "kind": "message",
    }


def test_rest_round_trip_all_endpoints_and_correction(tmp_path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path)
        transport = httpx.ASGITransport(app=create_app(engine=engine))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            add = await client.post(
                "/v1/scopes/s/cards", json={"cards": [_card()]}
            )
            assert add.status_code == 200
            assert add.json()["accepted"] == 1
            engine.flush("s")
            waited_card = {
                **_card(),
                "card_id": "c-wait",
                "subject_key": "thing-wait",
                "title": "Waited card",
                "source_refs": [
                    {
                        "source_id": "m-wait",
                        "sent_at": "2026-01-01T10:00:00Z",
                        "sender": "u",
                    }
                ],
            }
            waited_cards = await client.post(
                "/v1/scopes/s/cards",
                json={"cards": [waited_card], "wait": True},
            )
            assert waited_cards.status_code == 200
            assert waited_cards.json()["status"] == "completed"
            assert waited_cards.json()["task_id"].startswith("task_")

            message = await client.post(
                "/v1/scopes/s/messages",
                json={
                    "messages": [
                        {
                            "id": "m2",
                            "sender": {"id": "u2", "name": "User Two"},
                            "text": "Record thing is open.",
                            "sent_at": "2026-01-01T10:30:00Z",
                            "conversation_id": "C1",
                        }
                    ]
                },
            )
            assert message.status_code == 200
            task_id = message.json()["task_id"]
            assert (await client.get(f"/v1/tasks/{task_id}")).json()["status"] == (
                "pending"
            )
            engine.flush("s")
            task = await client.get(f"/v1/tasks/{task_id}")
            assert task.status_code == 200
            assert task.json()["gate"] == {
                "accepted": 1,
                "rejected": {},
                "handle_conflicts": 0,
                "route_handle": 0,
                "route_thread": 0,
                "route_evidence": 0,
                "route_model": 0,
                "route_new": 1,
                "route_review": 0,
                "route_disagreements": 0,
            }
            assert task.json()["attempts"] == 0
            assert task.json()["last_error"] is None
            waited_messages = await client.post(
                "/v1/scopes/s/messages",
                json={
                    "wait": True,
                    "messages": [
                        {
                            "id": "m-wait",
                            "sender": {"id": "u3"},
                            "text": "Waited message is open.",
                            "sent_at": "2026-01-01T11:30:00Z",
                            "conversation_id": "C-wait",
                        }
                    ],
                },
            )
            assert waited_messages.status_code == 200
            assert waited_messages.json()["status"] == "completed"
            assert waited_messages.json()["task_id"].startswith("task_")

            params = {"subject_key": "thing-1", "predicate": "phase"}
            current = await client.get(
                "/v1/scopes/s/query/current", params=params
            )
            timeline = await client.get(
                "/v1/scopes/s/query/timeline", params=params
            )
            at = await client.get(
                "/v1/scopes/s/query/at",
                params={**params, "instant": "2026-01-01T10:00:00Z"},
            )
            by_person = await client.get(
                "/v1/scopes/s/query/by-person", params={"person_id": "p1"}
            )
            listed = await client.get("/v1/scopes/s/matters")
            for response in [current, timeline, at, by_person, listed, task]:
                assert response.status_code == 200
                assert response.json()
            correction = await client.post(
                "/v1/scopes/s/corrections",
                json={
                    "subject_key": "thing-1",
                    "subject_type": "THING",
                    "predicate": "phase",
                    "object_value": "closed",
                    "valid_from": "2026-01-01T00:00:00Z",
                    "source_refs": [
                        {
                            "source_id": "human-note",
                            "sent_at": "2026-01-01T12:00:00Z",
                            "sender": "human",
                        }
                    ],
                },
            )
            assert correction.status_code == 200
            corrected = await client.get(
                "/v1/scopes/s/query/current", params=params
            )
            assert corrected.json()[0]["value"] == "closed"
            invalid = await client.get("/v1/scopes/s/query/current")
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
            unknown_task = await client.get("/v1/tasks/unknown")
            assert unknown_task.status_code == 404
            assert unknown_task.json() == {
                "error": {
                    "code": "NOT_FOUND",
                    "message": "unknown task_id: unknown",
                }
            }
            unknown_scope = await client.get("/v1/scopes/unknown/matters")
            assert unknown_scope.status_code == 404
            assert unknown_scope.json()["error"]["code"] == "NOT_FOUND"
            unknown_subject = await client.get(
                "/v1/scopes/s/query/current",
                params={"subject_key": "unknown", "predicate": "phase"},
            )
            assert unknown_subject.status_code == 404
            assert unknown_subject.json()["error"]["code"] == "NOT_FOUND"
            exported = await client.get("/v1/scopes/s/export")
            assert exported.status_code == 200
            assert exported.json()["format"] == "matterhorn-scope-export"
            events = await client.get("/v1/scopes/s/events")
            assert events.status_code == 200
            assert events.json()[0]["event_id"]
            assert (await client.post("/v1/add_episode_cards", json={})).status_code == 404

    asyncio.run(scenario())


def test_read_packages_have_no_import_path_to_distill() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "matterhorn"
    module_files = {
        "matterhorn.query": package_root / "query" / "__init__.py",
        "matterhorn.api": package_root / "api" / "__init__.py",
        "matterhorn.mcp": package_root / "mcp" / "__init__.py",
    }
    visited: set[str] = set()

    def walk(module: str, path: Path) -> None:
        if module in visited:
            return
        visited.add(module)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        assert not any(name.startswith("matterhorn.distill") for name in names)
        for name in names:
            if not name.startswith("matterhorn."):
                continue
            relative = name.removeprefix("matterhorn.").replace(".", "/")
            candidate = package_root / f"{relative}.py"
            init = package_root / relative / "__init__.py"
            if candidate.is_file():
                walk(name, candidate)
            elif init.is_file():
                walk(name, init)

    for module, path in module_files.items():
        walk(module, path)


def test_service_mode_quiet_period_auto_flushes_old_messages(tmp_path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path)
        app = create_app(
            engine=engine,
            quiet_period_minutes=10,
            scheduler_poll_seconds=0.01,
        )
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://matterhorn.test",
            ) as client,
        ):
            added = await client.post(
                "/v1/scopes/s/messages",
                json={
                    "messages": [
                        {
                            "id": "m1",
                            "sender": {"id": "u1"},
                            "text": "Record thing is open.",
                            "sent_at": "2026-01-01T10:30:00Z",
                        }
                    ]
                },
            )
            task_id = added.json()["task_id"]
            for _ in range(100):
                status = (
                    await client.get(f"/v1/tasks/{task_id}")
                ).json()["status"]
                if status == "completed":
                    break
                await asyncio.sleep(0.01)
            assert status == "completed"

    asyncio.run(scenario())


def test_mcp_official_sdk_round_trip_all_nine_tools(tmp_path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from matterhorn.mcp.server import create_server
    from matterhorn.service import MatterhornService

    async def scenario() -> None:
        engine = _engine(tmp_path)
        server = create_server(MatterhornService(engine))
        async with create_connected_server_and_client_session(server) as client:
            tools = await client.list_tools()
            assert [item.name for item in tools.tools] == [
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
            added = _structured(
                await client.call_tool(
                    "add_cards", {"scope_id": "s", "cards": [_card()]}
                )
            )
            assert added["ok"] is True
            assert added["data"]["accepted"] == 1
            engine.flush("s")
            added_messages = _structured(
                await client.call_tool(
                    "add_messages",
                    {
                        "scope_id": "s",
                        "messages": [
                            {
                                "id": "m2",
                                "conversation_id": "C2",
                                "sender": {"id": "u2"},
                                "text": "Record thing is open.",
                                "sent_at": "2026-01-01T10:30:00Z",
                            }
                        ],
                    },
                )
            )
            assert added_messages["ok"] is True
            engine.flush("s")
            added_records = _structured(
                await client.call_tool(
                    "add_records",
                    {"scope_id": "s", "records": [_record()]},
                )
            )
            assert added_records["ok"] is True
            assert added_records["data"]["records_processed"] == 1
            common = {
                "scope_id": "s",
                "subject_key": "thing-1",
                "predicate": "phase",
            }
            current = _structured(await client.call_tool("query_current", common))
            timeline = _structured(await client.call_tool("query_timeline", common))
            at = _structured(
                await client.call_tool(
                    "query_at",
                    {**common, "instant": "2026-01-01T10:00:00Z"},
                )
            )
            by_person = _structured(
                await client.call_tool(
                    "query_by_person", {"scope_id": "s", "person_id": "p1"}
                )
            )
            listed = _structured(
                await client.call_tool("list_matters", {"scope_id": "s"})
            )
            for result in [current, timeline, at, by_person, listed]:
                assert result["ok"] is True
                assert result["data"]
            corrected = _structured(
                await client.call_tool(
                    "correct",
                    {
                        "correction": {
                            "scope_id": "s",
                            "subject_key": "thing-1",
                            "subject_type": "THING",
                            "predicate": "phase",
                            "object_value": "closed",
                            "valid_from": "2026-01-01T00:00:00Z",
                            "source_refs": [
                                {
                                    "source_id": "human-note",
                                    "sent_at": "2026-01-01T12:00:00Z",
                                    "sender": "human",
                                }
                            ],
                        }
                    },
                )
            )
            assert corrected["ok"] is True
            after = _structured(await client.call_tool("query_current", common))
            assert after["data"][0]["value"] == "closed"
            error = _structured(
                await client.call_tool(
                    "query_current",
                    {**common, "predicate": "not_registered"},
                )
            )
            assert error == {
                "ok": False,
                "data": None,
                "error": {
                    "code": "ValueError",
                    "message": "unregistered predicate: not_registered",
                },
            }

    asyncio.run(scenario())


def _structured(result) -> dict:
    assert result.isError is False
    assert result.structuredContent is not None
    return result.structuredContent


def test_missing_mcp_extra_has_actionable_import_error() -> None:
    script = """
import sys

class BlockMcp:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mcp" or fullname.startswith("mcp."):
            raise ModuleNotFoundError("blocked official SDK", name=fullname)
        return None

sys.meta_path.insert(0, BlockMcp())
try:
    import matterhorn.mcp.server
except ImportError as error:
    message = str(error)
    assert "official MCP SDK" in message
    assert "matterhorn[mcp]" in message
else:
    raise AssertionError("MCP import unexpectedly succeeded")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("entrypoint", ["mh", "module"])
def test_mcp_stdio_entrypoints_use_official_protocol(entrypoint, tmp_path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> None:
        db = tmp_path / f"{entrypoint}.db"
        if entrypoint == "mh":
            script_name = "mh.exe" if os.name == "nt" else "mh"
            candidate = Path(sys.executable).parent / script_name
            if not candidate.is_file():
                raise AssertionError("installed mh console script was not found")
            parameters = StdioServerParameters(
                command=str(candidate),
                args=[
                    "mcp",
                    "--db",
                    str(db),
                    "--schema",
                    "org-matters/v1",
                ],
            )
        else:
            env = dict(os.environ)
            env["MATTERHORN_DB"] = str(db)
            env["MATTERHORN_SCHEMA"] = "org-matters/v1"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "matterhorn.mcp"],
                env=env,
            )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert len(tools.tools) == 9

    asyncio.run(scenario())
