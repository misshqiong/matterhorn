from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from matterhorn.api import create_app
from matterhorn.console import ConsoleSampleGateway
from matterhorn.console.chat import ConsoleChatRunner
from matterhorn.contracts import Record
from matterhorn.defaults import Engine
from matterhorn.distill import NullGateway
from matterhorn.errors import IngestFormatError
from matterhorn.ingest import detect_ingest
from matterhorn.service import MatterhornService
from matterhorn.store import SQLiteStore


def _card(
    *,
    scope_id: str = "s",
    subject_key: str = "octo-launch",
    status: str = "in_progress",
) -> dict[str, Any]:
    return {
        "card_id": f"{scope_id}-{subject_key}-{status}",
        "scope_id": scope_id,
        "subject_key": subject_key,
        "date": "2026-07-29",
        "title": "octo-org Console launch",
        "status": status,
        "participants": [
            {
                "id": "dana-reyes",
                "display_name": "Dana Reyes",
                "role": "owner",
            }
        ],
        "source_refs": [
            {
                "source_id": f"{scope_id}:message-1",
                "sent_at": "2026-07-29T09:00:00Z",
                "sender": "Dana Reyes",
                "uri": "https://octo-org.example/messages/1",
            }
        ],
    }


def _engine(store: Any) -> Engine:
    return Engine(
        store,
        "org-matters/v1",
        clock=lambda: datetime(2026, 7, 29, 12, tzinfo=UTC),
    )


def test_server_side_format_detection_accepts_all_three_formats() -> None:
    now = lambda: datetime(2026, 7, 29, 9, tzinfo=UTC)
    chat = detect_ingest(
        "Dana Reyes: Status is open.\nDana Reyes: Next step is review.",
        now=now,
        batch_id="paste",
    )
    assert chat.input_format == "chat"
    assert chat.synthesized_timestamps is True
    assert [item.sent_at.isoformat() for item in chat.messages] == [
        "2026-07-29T09:00:00+00:00",
        "2026-07-29T09:00:01+00:00",
    ]

    structured = detect_ingest(
        json.dumps(
            {
                "messages": [
                    {
                        "id": "m1",
                        "sender": {"id": "dana", "name": "Dana Reyes"},
                        "text": "Status is open.",
                        "sent_at": "2026-07-29T09:00:00Z",
                    }
                ]
            }
        )
    )
    assert structured.input_format == "messages"
    assert structured.messages[0].id == "m1"

    email = detect_ingest(
        """From: Dana Reyes <dana@octo-org.example>
To: team@octo-org.example
Date: Wed, 29 Jul 2026 09:00:00 +0000
Message-ID: <console-1@octo-org.example>
Subject: Console launch
Content-Type: text/plain

The launch status is open."""
    )
    assert email.input_format == "email"
    assert email.messages[0].sender.name == "Dana Reyes"
    assert "Subject: Console launch" in email.messages[0].text


def test_server_side_format_detection_rejects_garbage_clearly() -> None:
    with pytest.raises(IngestFormatError) as caught:
        detect_ingest("unstructured words without a sender")
    message = str(caught.value)
    assert "plain chat lines" in message
    assert "YAML/JSON" in message
    assert "EML/mbox" in message


def test_correction_round_trip_through_rest_is_human(tmp_path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path / "correction.db")
        engine._ingest_cards_sync([_card()])
        app = create_app(engine=engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            correction = await client.post(
                "/v1/scopes/s/corrections",
                json={
                    "subject_key": "octo-launch",
                    "subject_type": "MATTER",
                    "predicate": "status",
                    "object_value": "done",
                    "valid_from": "2026-07-29T09:00:00Z",
                    "source_refs": [
                        {
                            "source_id": "console:correction-1",
                            "sent_at": "2026-07-29T12:00:00Z",
                            "sender": "Dana Reyes",
                            "excerpt": "Console correction. Reason: shipped.",
                        }
                    ],
                },
            )
            assert correction.status_code == 200
            assert correction.json()["origin"] == "human"
            detail = (
                await client.get("/v1/scopes/s/matters/octo-launch")
            ).json()
            status = next(
                item for item in detail["current"] if item["predicate"] == "status"
            )
            assert status["value"] == "done"
            assert status["origin"] == "human"
            assert status["source_ids"] == ["console:correction-1"]
            assert status["source_details"] == [
                {
                    "source_id": "console:correction-1",
                    "sender": "Dana Reyes",
                    "excerpt": "Console correction. Reason: shipped.",
                    "uri": None,
                    "status": "active",
                    "revoked_at": None,
                }
            ]

    asyncio.run(scenario())


def test_matter_detail_timeline_exposes_chronological_source_details(
    tmp_path,
) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path / "timeline.db")
        engine._ingest_cards_sync(
            [
                {
                    **_card(status="open"),
                    "progress": "Diagnosing order 1042.",
                    "occurred_at": "2026-07-29T09:00:00Z",
                    "source_refs": [
                        {
                            "source_id": "mail:first",
                            "sent_at": "2026-07-29T09:00:00Z",
                            "sender": "Ada",
                            "excerpt": "Order 1042 is awaiting payment.",
                        }
                    ],
                },
                {
                    **_card(status="done"),
                    "progress": "Payment verified.",
                    "outcome": {
                        "type": "payment",
                        "content": "Order 1042 paid.",
                    },
                    "occurred_at": "2026-07-30T10:00:00Z",
                    "source_refs": [
                        {
                            "source_id": "mail:second",
                            "sent_at": "2026-07-30T10:00:00Z",
                            "sender": "Bob",
                            "excerpt": "Payment succeeded for order 1042.",
                        }
                    ],
                },
            ]
        )
        app = create_app(engine=engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            response = await client.get(
                "/v1/scopes/s/matters/octo-launch"
            )

        assert response.status_code == 200
        timeline = response.json()["timeline"]
        assert [item["value"] for item in timeline["status"]] == [
            "open",
            "done",
        ]
        assert timeline["progress"][0]["source_details"][0] == {
            "source_id": "mail:first",
            "sender": "Ada",
            "excerpt": "Order 1042 is awaiting payment.",
            "uri": None,
            "status": "active",
            "revoked_at": None,
        }
        assert timeline["outcome"][0]["value"] == {
            "type": "payment",
            "content": "Order 1042 paid.",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_get_scopes_lists_both_backends(backend, tmp_path) -> None:
    if backend == "sqlite":
        store = SQLiteStore(tmp_path / "scopes.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "MATTERHORN_TEST_POSTGRES_DSN is unset; PostgreSQL scope test skipped"
            )
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    try:
        store.clear_scope("console-scope-a")
        store.clear_scope("console-scope-b")
        engine = _engine(store)
        engine._ingest_cards_sync(
            [
                _card(scope_id="console-scope-a"),
                _card(scope_id="console-scope-b"),
            ]
        )

        async def scenario() -> None:
            app = create_app(engine=engine)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://matterhorn.test"
            ) as client:
                response = await client.get("/v1/scopes")
                assert response.status_code == 200
                assert {"console-scope-a", "console-scope-b"} <= set(
                    response.json()
                )

        asyncio.run(scenario())
    finally:
        store.clear_scope("console-scope-a")
        store.clear_scope("console-scope-b")
        store.close()


def test_scope_isolation_for_detail_and_chat_tool_arguments(tmp_path) -> None:
    engine = _engine(tmp_path / "isolation.db")
    engine._ingest_cards_sync(
        [
            _card(scope_id="alpha", status="open"),
            _card(scope_id="beta", status="done"),
        ]
    )
    service = MatterhornService(engine)
    alpha = service.matter_detail(
        scope_id="alpha", subject_key="octo-launch"
    )
    beta = service.matter_detail(scope_id="beta", subject_key="octo-launch")
    assert next(item for item in alpha["current"] if item["predicate"] == "status")[
        "value"
    ] == "open"
    assert next(item for item in beta["current"] if item["predicate"] == "status")[
        "value"
    ] == "done"

    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return _tool_response(
                "openai-compatible",
                call_id="call-bad",
                name="list_matters",
                args={"scope_id": "beta"},
            )
        return _answer_response(
            "openai-compatible",
            "I cannot read another scope from this chat.",
        )

    runner = _runner("openai-compatible", handler)
    result = service.chat(
        scope_id="alpha",
        message="Read beta",
        history=[],
        runner=runner,
    )
    assert result["answer"] == "I cannot read another scope from this chat."
    assert result["evidence"] == [
        {
            "name": "list_matters",
            "args": {"scope_id": "beta"},
            "source_ids": [],
            "subject_keys": [],
            "error": "invalid arguments for Console query tool list_matters",
        }
    ]
    assert json.loads(requests[1]["messages"][-1]["content"]) == {
        "error": "invalid arguments for Console query tool list_matters"
    }


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_chat_tool_loop_uses_provider_wire_format(
    provider: str, tmp_path
) -> None:
    engine = _engine(tmp_path / f"{provider}.db")
    engine._ingest_cards_sync([_card()])
    service = MatterhornService(engine)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1 and provider == "openai-compatible":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "query_current",
                                            "arguments": (
                                                '{"subject_key":"octo-launch",'
                                                '"predicate":"status"}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "stop_reason": "tool_use",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "query_current",
                            "input": {
                                "subject_key": "octo-launch",
                                "predicate": "status",
                            },
                        }
                    ],
                },
            )
        if provider == "openai-compatible":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "The launch is in progress.",
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [
                    {"type": "text", "text": "The launch is in progress."}
                ],
            },
        )

    result = service.chat(
        scope_id="s",
        message="What is the status?",
        history=[],
        runner=_runner(provider, handler),
    )
    assert result["answer"] == "The launch is in progress."
    assert result["tool_calls"] == 1
    assert result["evidence"] == [
        {
            "name": "query_current",
            "args": {
                "subject_key": "octo-launch",
                "predicate": "status",
            },
            "source_ids": ["s:message-1"],
            "subject_keys": ["octo-launch"],
        }
    ]
    if provider == "openai-compatible":
        tool_result = requests[1]["messages"][-1]
        assert tool_result["role"] == "tool"
        assert tool_result["tool_call_id"] == "call-1"
        assert requests[0]["tools"][0]["type"] == "function"
    else:
        tool_results = requests[1]["messages"][-1]
        assert tool_results["role"] == "user"
        assert tool_results["content"][0]["type"] == "tool_result"
        assert tool_results["content"][0]["tool_use_id"] == "toolu-1"
        assert "input_schema" in requests[0]["tools"][0]
    definitions = {
        (
            tool["function"]["name"]
            if provider == "openai-compatible"
            else tool["name"]
        ): tool["function"] if provider == "openai-compatible" else tool
        for tool in requests[0]["tools"]
    }
    assert "complete current dictionary" in definitions["list_matters"][
        "description"
    ]
    for name in ["query_current", "query_timeline", "query_at"]:
        description = definitions[name]["description"]
        assert "status" in description
        assert "owned_by" in description
        assert "blocked_by" in description
        assert "next_step" in description
        assert "due_at" in description


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_chat_wildcard_then_recovers(provider: str, tmp_path) -> None:
    engine = _engine(tmp_path / f"wildcard-{provider}.db")
    engine._ingest_cards_sync([_card()])
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return _tool_response(
                provider,
                call_id="wildcard",
                name="query_current",
                args={"subject_key": "octo-launch", "predicate": "*"},
            )
        if len(requests) == 2:
            assert _tool_result(requests[-1], provider) == {
                "error": "unregistered predicate: *"
            }
            return _tool_response(
                provider,
                call_id="status",
                name="query_current",
                args={
                    "subject_key": "octo-launch",
                    "predicate": "status",
                },
            )
        return _answer_response(provider, "The launch is in progress.")

    response = _chat_response(
        engine=engine,
        runner=_runner(provider, handler),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["answer"] == "The launch is in progress."
    assert result["tool_calls"] == 2
    assert result["evidence"][0] == {
        "name": "query_current",
        "args": {"subject_key": "octo-launch", "predicate": "*"},
        "source_ids": [],
        "subject_keys": ["octo-launch"],
        "error": "unregistered predicate: *",
    }
    assert result["evidence"][1]["name"] == "query_current"
    assert result["evidence"][1]["args"]["predicate"] == "status"
    assert result["evidence"][1]["source_ids"] == ["s:message-1"]
    assert result["evidence"][1]["error"] is None


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_chat_cap_exhaustion_finalizes(provider: str, tmp_path) -> None:
    engine = _engine(tmp_path / f"cap-{provider}.db")
    engine._ingest_cards_sync([_card()])
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if _tools_disabled(payload, provider):
            return _answer_response(
                provider,
                "The available evidence says the launch is in progress.",
            )
        if len(requests) > 1:
            matters = _tool_result(payload, provider)
            assert matters[0]["title"] == "octo-org Console launch"
            assert matters[0]["current"]["status"] == "in_progress"
        return _tool_response(
            provider,
            call_id=f"list-{len(requests)}",
            name="list_matters",
            args={},
        )

    response = _chat_response(
        engine=engine,
        runner=_runner(provider, handler, max_tool_calls=2),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["answer"] == (
        "The available evidence says the launch is in progress."
    )
    assert result["tool_calls"] == 2
    assert len(result["evidence"]) == 2
    assert all(trace["name"] == "list_matters" for trace in result["evidence"])
    assert len(requests) == 3
    assert _tools_disabled(requests[-1], provider)
    assert "what you could not verify" in _final_instruction(
        requests[-1], provider
    )


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_chat_finalize_also_fails(provider: str, tmp_path) -> None:
    engine = _engine(tmp_path / f"finalize-fails-{provider}.db")
    engine._ingest_cards_sync([_card()])
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if _tools_disabled(payload, provider):
            return httpx.Response(
                503,
                json={"error": {"message": "provider unavailable"}},
            )
        return _tool_response(
            provider,
            call_id="status",
            name="query_current",
            args={"subject_key": "octo-launch", "predicate": "status"},
        )

    response = _chat_response(
        engine=engine,
        runner=_runner(provider, handler, max_tool_calls=1),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["answer"].startswith("I could not complete this query")
    assert result["tool_calls"] == 1
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["source_ids"] == ["s:message-1"]
    assert len(requests) == 2
    assert _tools_disabled(requests[-1], provider)


def test_keyless_console_hides_chat_and_serves_static_page(
    monkeypatch, tmp_path
) -> None:
    async def scenario() -> None:
        for key in [
            "MATTERHORN_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        ]:
            monkeypatch.delenv(key, raising=False)
        app = create_app(
            engine=_engine(tmp_path / "keyless.db"),
            console_enabled=True,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            page = await client.get("/console")
            assert page.status_code == 200
            assert 'id="chat-panel"' in page.text
            assert 'id="chat-panel" aria-labelledby="chat-title" hidden' in page.text
            config = await client.get("/v1/console/config")
            assert config.json() == {
                "chat_enabled": False,
                "chat_provider": None,
            }

    asyncio.run(scenario())


def test_keyless_console_sample_uses_packaged_fixture(tmp_path) -> None:
    async def scenario() -> None:
        engine = Engine(
            tmp_path / "sample.db",
            gateway=ConsoleSampleGateway(NullGateway()),
        )
        app = create_app(engine=engine, console_enabled=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            response = await client.post(
                "/v1/scopes/demo/ingest",
                json={
                    "wait": True,
                    "text": (
                        "Dana Reyes: I own the octo-org Console launch.\n"
                        "Dana Reyes: The REST scope browser is connected.\n"
                        "Dana Reyes: The correction demo is next."
                    ),
                },
            )
            receipt = response.json()
            assert receipt["status"] == "completed"
            assert receipt["gate"] == {
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
            matters = (await client.get("/v1/scopes/demo/matters")).json()
            assert matters[0]["title"] == "octo-org Console launch"

    asyncio.run(scenario())


def test_unified_matters_wall_spans_scopes_and_filters(tmp_path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path / "unified-wall.db")
        engine._ingest_cards_sync(
            [
                _card(scope_id="personal", subject_key="personal-launch"),
                {
                    **_card(
                        scope_id="work",
                        subject_key="work-launch",
                        status="blocked",
                    ),
                    "title": "octo-org work launch",
                    "next_step": "Dana Reyes reviews the blocker",
                    "due": "2026-07-30T17:00:00Z",
                },
            ]
        )
        app = create_app(engine=engine, console_enabled=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            all_matters = await client.get("/v1/matters")
            personal = await client.get(
                "/v1/matters",
                params={"scope": "personal"},
            )
            page = await client.get("/console")
            paths = (await client.get("/openapi.json")).json()["paths"]

        assert all_matters.status_code == 200
        assert {
            (item["scope_id"], item["subject_key"])
            for item in all_matters.json()
        } == {
            ("personal", "personal-launch"),
            ("work", "work-launch"),
        }
        assert [item["scope_id"] for item in personal.json()] == ["personal"]
        assert all(item["updated_at"] for item in all_matters.json())
        assert "/v1/matters" in paths
        for marker in [
            'id="matter-list"',
            'class="matter-wall"',
            'id="chat-scope"',
            'api(`/v1/matters${query}`)',
            "scope_id",
            "last-opened card",
            'id="merge-form"',
            'id="merge-target"',
            'id="review-list"',
            'id="review-count"',
            "data-review-action",
            "/reviews/",
            "reviewCount",
            "matterhorn.console.sender",
            "/merges`",
            "又名",
            "Timeline",
            "source_details",
            'new Set(["status", "progress", "outcome"])',
            'id="raw-stream-panel"',
            'id="raw-stream-list"',
            "/v1/stream?limit=50",
            "pollRawStream()",
            "streamNewestRecordId",
            'class="stamp updated"',
            "matterhorn.console.seen.",
            "markMatterSeen(matter)",
        ]:
            assert marker in page.text

    asyncio.run(scenario())


def test_stream_routes_filter_cap_truncate_and_exclude_revoked(tmp_path) -> None:
    async def scenario() -> None:
        engine = _engine(tmp_path / "stream.db")
        base = datetime(2026, 8, 4, 9, tzinfo=UTC)
        records = [
            Record(
                record_id=f"room-a:r{index:03}",
                native_id=f"r{index:03}",
                container_id="room-a",
                sent_at=base + timedelta(seconds=index),
                author={"id": "dana", "kind": "human"},
                content="x" * 600 if index == 204 else f"Fictional item {index}.",
            )
            for index in range(205)
        ]
        revoked = Record(
            record_id="room-a:revoked",
            native_id="revoked",
            container_id="room-a",
            sent_at=base.replace(minute=10),
            author={"id": "revoked-sender", "kind": "human"},
            content="This revoked fictional item must not appear.",
            revoked_at=base.replace(minute=11),
            kind="revocation",
        )
        other = Record(
            record_id="room-b:newest",
            native_id="newest",
            container_id="room-b",
            sent_at=base,
            author={
                "id": "ellis",
                "display_name": "Ellis Stone",
                "kind": "human",
            },
            content="Newest fictional cross-scope item.",
        )
        with engine.store.transaction():
            engine.store.stage_records(
                "scope-a", [*records, revoked], staged_at=base
            )
            engine.store.stage_records(
                "scope-b", [other], staged_at=base.replace(minute=20)
            )

        app = create_app(engine=engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            all_items = (await client.get("/v1/stream?limit=999")).json()
            scoped = (
                await client.get("/v1/scopes/scope-a/stream?limit=1")
            ).json()
            paths = (await client.get("/openapi.json")).json()["paths"]

        assert len(all_items) == 200
        assert all_items[0]["record_id"] == "room-b:newest"
        assert all_items[0]["sender"] == "Ellis Stone"
        assert scoped[0]["record_id"] == "room-a:r204"
        assert scoped[0]["sender"] == "dana"
        assert len(scoped[0]["content"]) == 500
        assert all(item["record_id"] != "room-a:revoked" for item in all_items)
        assert "/v1/stream" in paths
        assert "/v1/scopes/{scope_id}/stream" in paths

    asyncio.run(scenario())


def test_matter_updated_at_advances_on_new_evidence_for_existing_subject(
    tmp_path,
) -> None:
    async def scenario() -> None:
        current = [datetime(2026, 8, 4, 12, tzinfo=UTC)]
        engine = Engine(
            tmp_path / "matter-updated.db",
            "org-matters/v1",
            clock=lambda: current[0],
        )
        engine._ingest_cards_sync([_card(status="open")])
        app = create_app(engine=engine)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            first = (await client.get("/v1/matters")).json()[0]
            current[0] = datetime(2026, 8, 4, 13, tzinfo=UTC)
            engine._ingest_cards_sync(
                [
                    {
                        **_card(status="open"),
                        "card_id": "existing-subject-new-evidence",
                        "progress": "A fictional reviewer reconfirmed the launch.",
                        "source_refs": [
                            {
                                "source_id": "s:message-2",
                                "sent_at": "2026-07-29T10:00:00Z",
                                "sender": "Ellis Stone",
                            }
                        ],
                    }
                ]
            )
            second = (await client.get("/v1/matters")).json()[0]

        assert first["updated_at"] == "2026-08-04T12:00:00Z"
        assert second["updated_at"] == "2026-08-04T13:00:00Z"
        assert engine.store.memory_cards("s")[0].updated_at == datetime(
            2026, 7, 29, tzinfo=UTC
        )

    asyncio.run(scenario())


def test_ingest_rate_limit_and_openapi_paths(tmp_path) -> None:
    async def scenario() -> None:
        app = create_app(
            engine=_engine(tmp_path / "rate.db"),
            ingest_rate_limit=1,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            payload = {"text": "Dana Reyes: Status is open."}
            first = await client.post("/v1/scopes/rate/ingest", json=payload)
            second = await client.post("/v1/scopes/rate/ingest", json=payload)
            assert first.status_code == 200
            assert second.status_code == 429
            assert second.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
            paths = (await client.get("/openapi.json")).json()["paths"]
            for path in [
                "/v1/scopes",
                "/v1/scopes/{scope_id}/ingest",
                "/v1/scopes/{scope_id}/matters/{subject_key}",
                "/v1/scopes/{scope_id}/merges",
                "/v1/scopes/{scope_id}/merges/{source_subject_key}/unmerge",
                "/v1/scopes/{scope_id}/reviews",
                "/v1/scopes/{scope_id}/reviews/{review_id}/resolve",
                "/v1/scopes/{scope_id}/chat",
                "/v1/console/config",
            ]:
                assert path in paths

    asyncio.run(scenario())


def test_chat_rate_limit_is_independent(tmp_path) -> None:
    class StubChat:
        provider = "openai-compatible"

        def run(self, **_kwargs):
            return {"answer": "ok", "evidence": [], "tool_calls": 0}

    async def scenario() -> None:
        engine = _engine(tmp_path / "chat-rate.db")
        engine._ingest_cards_sync([_card()])
        app = create_app(
            engine=engine,
            chat_runner=StubChat(),
            chat_rate_limit=1,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://matterhorn.test"
        ) as client:
            payload = {"message": "Status?"}
            first = await client.post("/v1/scopes/s/chat", json=payload)
            second = await client.post("/v1/scopes/s/chat", json=payload)
            assert first.status_code == 200
            assert second.status_code == 429
            assert second.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"

    asyncio.run(scenario())


def _runner(
    provider: str,
    handler: Any,
    *,
    max_tool_calls: int = 6,
) -> ConsoleChatRunner:
    return ConsoleChatRunner(
        provider=provider,
        api_key="test-key",
        model="test-model",
        base_url=(
            "https://api.openai.test/v1"
            if provider == "openai-compatible"
            else "https://api.anthropic.test"
        ),
        timeout=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_tool_calls=max_tool_calls,
    )


def _tool_response(
    provider: str,
    *,
    call_id: str,
    name: str,
    args: dict[str, Any],
) -> httpx.Response:
    if provider == "openai-compatible":
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": json.dumps(args),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": name,
                    "input": args,
                }
            ],
        },
    )


def _answer_response(provider: str, answer: str) -> httpx.Response:
    if provider == "openai-compatible":
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": answer,
                        }
                    }
                ]
            },
        )
    return httpx.Response(
        200,
        json={
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": answer}],
        },
    )


def _tool_result(payload: dict[str, Any], provider: str) -> Any:
    message = payload["messages"][-1]
    if provider == "openai-compatible":
        assert message["role"] == "tool"
        return json.loads(message["content"])
    assert message["role"] == "user"
    return json.loads(message["content"][0]["content"])


def _tools_disabled(payload: dict[str, Any], provider: str) -> bool:
    expected: Any = "none"
    if provider == "anthropic":
        expected = {"type": "none"}
    return payload.get("tool_choice") == expected


def _final_instruction(payload: dict[str, Any], provider: str) -> str:
    message = payload["messages"][-1]
    assert message["role"] == "user"
    assert isinstance(message["content"], str)
    return message["content"]


def _chat_response(
    *,
    engine: Engine,
    runner: ConsoleChatRunner,
) -> httpx.Response:
    async def scenario() -> httpx.Response:
        app = create_app(engine=engine, chat_runner=runner)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            return await client.post(
                "/v1/scopes/s/chat",
                json={"message": "What is the status?"},
            )

    return asyncio.run(scenario())


def test_console_version_endpoint_is_stable_and_page_carries_banner(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from matterhorn import Engine
    from matterhorn.api.app import create_app

    client = TestClient(create_app(engine=Engine(tmp_path / "ver.db"), console_enabled=True))
    first = client.get("/v1/console/version").json()
    second = client.get("/v1/console/version").json()
    assert first == second
    assert len(first["version"]) == 12
    page = client.get("/console").text
    assert "stale-banner" in page
    assert "pollAssetVersion" in page
