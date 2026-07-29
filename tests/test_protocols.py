from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from matterhorn.api import create_app
from matterhorn.contracts import SchemaProfile
from matterhorn.engine import Engine


class ExplodingGateway:
    def complete(self, **_kwargs) -> str:
        raise AssertionError("read path touched the LLM gateway")


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
        gateway=ExplodingGateway(),
        clock=[
            datetime(2026, 1, 1, 11, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
        ],
    )


def test_rest_round_trip_all_endpoints_and_correction(tmp_path) -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_app(engine=_engine(tmp_path)))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            add = await client.post(
                "/v1/add_episode_cards", json={"scope_id": "s", "cards": [_card()]}
            )
            assert add.status_code == 200
            predicate = {
                "scope_id": "s",
                "subject_key": "thing-1",
                "predicate": "phase",
            }
            current = await client.post("/v1/query_current", json=predicate)
            timeline = await client.post("/v1/query_timeline", json=predicate)
            at = await client.post(
                "/v1/query_at",
                json={**predicate, "instant": "2026-01-01T10:00:00Z"},
            )
            by_person = await client.post(
                "/v1/query_by_person", json={"scope_id": "s", "person_id": "p1"}
            )
            listed = await client.post("/v1/list_matters", json={"scope_id": "s"})
            for response in [current, timeline, at, by_person, listed]:
                assert response.status_code == 200
                assert response.json()
            correction = await client.post(
                "/v1/correct",
                json={
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
            assert correction.status_code == 200
            corrected = await client.post("/v1/query_current", json=predicate)
            assert corrected.json()[0]["value"] == "closed"
            invalid = await client.post("/v1/list_matters", json={})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"

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


def test_mcp_official_sdk_round_trip_all_seven_tools(tmp_path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from matterhorn.mcp.server import create_server
    from matterhorn.service import MatterhornService

    async def scenario() -> None:
        server = create_server(MatterhornService(_engine(tmp_path)))
        async with create_connected_server_and_client_session(server) as client:
            tools = await client.list_tools()
            assert [item.name for item in tools.tools] == [
                "add_episode_cards",
                "query_current",
                "query_timeline",
                "query_at",
                "query_by_person",
                "list_matters",
                "correct",
            ]
            added = _structured(
                await client.call_tool(
                    "add_episode_cards", {"scope_id": "s", "cards": [_card()]}
                )
            )
            assert added["ok"] is True
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
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("entrypoint", ["mh", "module"])
def test_mcp_stdio_entrypoints_use_official_protocol(entrypoint, tmp_path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def scenario() -> None:
        db = tmp_path / f"{entrypoint}.db"
        if entrypoint == "mh":
            parameters = StdioServerParameters(
                command=str(Path(sys.executable).with_name("mh")),
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
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 7

    asyncio.run(scenario())
