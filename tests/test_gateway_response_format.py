"""The OpenAI-compatible gateway degrades response_format across providers.

DeepSeek (and several local OpenAI-compatible servers) reject
``response_format: json_schema`` with a 400. The validation gate is the real
enforcement (P3); the response_format is an optimization, so the gateway falls
back json_schema -> json_object -> none and remembers the first variant the
provider accepts.
"""

from __future__ import annotations

import json

import httpx
import pytest

from matterhorn.distill.gateway import OpenAICompatibleGateway

SCHEMA = {"type": "object", "properties": {"cards": {"type": "array"}}}
BODY_OK = {"choices": [{"message": {"content": json.dumps({"cards": []})}}]}


def _gateway_with(transport: httpx.MockTransport) -> OpenAICompatibleGateway:
    gateway = OpenAICompatibleGateway(
        base_url="https://provider.test/v1", api_key="k", model="m"
    )
    gateway._client = httpx.Client(transport=transport)
    return gateway


def test_json_schema_accepted_first_try() -> None:
    seen: list[dict | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content).get("response_format"))
        return httpx.Response(200, json=BODY_OK)

    gateway = _gateway_with(httpx.MockTransport(handler))
    gateway.complete(system="s", user="u", response_schema=SCHEMA)
    assert seen[0] is not None and seen[0]["type"] == "json_schema"


def test_falls_back_to_json_object_and_remembers() -> None:
    seen: list[dict | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fmt = json.loads(request.content).get("response_format")
        seen.append(fmt)
        if fmt is not None and fmt.get("type") == "json_schema":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "This response_format type is unavailable now"
                    }
                },
            )
        return httpx.Response(200, json=BODY_OK)

    gateway = _gateway_with(httpx.MockTransport(handler))
    gateway.complete(system="s", user="u", response_schema=SCHEMA)
    assert [f and f["type"] for f in seen] == ["json_schema", "json_object"]

    gateway.complete(system="s", user="u", response_schema=SCHEMA)
    # The failing variant is not retried on subsequent calls.
    assert [f and f["type"] for f in seen] == [
        "json_schema",
        "json_object",
        "json_object",
    ]


def test_unrelated_400_is_raised_not_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "model not found"}})

    gateway = _gateway_with(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        gateway.complete(system="s", user="u", response_schema=SCHEMA)


def test_tool_loop_executes_calls_and_returns_tool_results_to_model() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_candidates",
                                            "arguments": '{"text":"octo audit"}',
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
            json={"choices": [{"message": {"content": "done"}}]},
        )

    gateway = _gateway_with(httpx.MockTransport(handler))
    handled: list[tuple[str, dict]] = []
    result = gateway.tool_loop(
        system="system",
        user="user",
        tools=[{"type": "function", "function": {"name": "search_candidates"}}],
        handler=lambda name, arguments: (
            handled.append((name, arguments)) or {"subjects": []}
        ),
    )

    assert result.final_message == "done"
    assert result.tool_calls == 1
    assert not result.exhausted
    assert handled == [("search_candidates", {"text": "octo audit"})]
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "search_candidates",
        "content": '{"subjects":[]}',
    }


def test_tool_loop_stops_before_fifth_emission() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"emit-{calls}",
                                    "type": "function",
                                    "function": {
                                        "name": "emit",
                                        "arguments": '{"assertions":[]}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    gateway = _gateway_with(httpx.MockTransport(handler))
    handled: list[str] = []
    result = gateway.tool_loop(
        system="system",
        user="user",
        tools=[{"type": "function", "function": {"name": "emit"}}],
        handler=lambda name, _arguments: handled.append(name) or {},
    )

    assert result.exhausted
    assert result.tool_calls == 4
    assert result.emissions == 4
    assert handled == ["emit"] * 4
