from __future__ import annotations

import asyncio
import json
import tomllib
from datetime import UTC, datetime

import httpx

from matterhorn.api import create_app
from matterhorn.defaults import Engine
from matterhorn.runtime_ai import (
    AIConfig,
    AIRuntime,
    load_ai_config,
    save_ai_config,
)


class FixtureExtractionGateway:
    def __init__(self, calls: list[dict]):
        self.calls = calls

    def complete(self, *, system, user, response_schema):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "response_schema": response_schema,
            }
        )
        properties = response_schema.get("properties", {})
        if "cards" in properties:
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-07-31",
                            "title": "octo-org AI routing",
                            "status": "in_progress",
                            "participants": [
                                {
                                    "id": "dana-reyes",
                                    "display_name": "Dana Reyes",
                                    "role": "owner",
                                }
                            ],
                            "next_step": "Review the runtime gateway",
                            "source_ids": ["m1"],
                        }
                    ]
                }
            )
        return json.dumps({"candidates": []})


def test_ai_toml_round_trip_never_serializes_key_and_preserves_mail(tmp_path) -> None:
    path = tmp_path / "matterhorn.toml"
    path.write_text(
        """db = "matterhorn.db"

[[mail.accounts]]
provider = "gmail"
host = "imap.gmail.com"
port = 993
ssl = true
user = "dana@example.test"
folder = "INBOX"
interval = "off"
initial_window = 50
""",
        encoding="utf-8",
    )
    config = AIConfig(
        provider="openai-compatible",
        base_url="https://ai.octo-org.example/v1",
        model="octo-small",
        timeout=12.5,
    )

    save_ai_config(path, config)

    text = path.read_text(encoding="utf-8")
    assert load_ai_config(path) == config
    assert tomllib.loads(text)["mail"]["accounts"][0]["user"] == (
        "dana@example.test"
    )
    assert tomllib.loads(text)["ai"] == {
        "provider": "openai-compatible",
        "base_url": "https://ai.octo-org.example/v1",
        "model": "octo-small",
        "timeout": 12.5,
    }
    assert "api_key" not in text.casefold()
    assert "password" not in text.casefold()


def test_runtime_ai_config_precedes_environment_and_rebuilds_extraction(
    tmp_path,
) -> None:
    async def scenario() -> None:
        config_path = tmp_path / "matterhorn.toml"
        save_ai_config(
            config_path,
            AIConfig(
                provider="openai-compatible",
                base_url="https://runtime.example/v1",
                model="runtime-model",
                timeout=9,
            ),
        )
        factory_calls: list[dict] = []
        completion_calls: list[dict] = []

        def factory(**kwargs):
            factory_calls.append(kwargs)
            return FixtureExtractionGateway(completion_calls)

        engine = Engine(
            tmp_path / "ai.db",
            clock=lambda: datetime(2026, 7, 31, 9, tzinfo=UTC),
        )
        runtime = AIRuntime(
            engine,
            config_path=config_path,
            environment={
                "MATTERHORN_PROVIDER": "anthropic",
                "MATTERHORN_BASE_URL": "https://environment.example",
                "MATTERHORN_MODEL": "environment-model",
                "MATTERHORN_API_KEY": "environment-secret",
            },
            gateway_factory=factory,
        )
        app = create_app(
            engine=engine,
            ai_runtime=runtime,
            mail_config_path=config_path,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            initial = await client.get("/v1/connectors/ai/status")
            assert initial.json()["source"] == "runtime"
            assert initial.json()["config"]["provider"] == "openai-compatible"
            assert initial.json()["config"]["model"] == "runtime-model"
            assert initial.json()["api_key_state"] == "loaded from environment"
            assert "environment-secret" not in initial.text

            configured = await client.post(
                "/v1/connectors/ai/config",
                json={
                    "provider": "openai-compatible",
                    "base_url": "https://console.example/v1",
                    "model": "console-model",
                    "timeout": 7,
                    "api_key": "console-secret",
                },
            )
            assert configured.status_code == 200
            assert configured.json()["api_key_state"] == (
                "loaded in process memory"
            )
            assert "console-secret" not in configured.text

            extracted = await client.post(
                "/v1/scopes/personal/ingest",
                json={
                    "wait": True,
                    "text": (
                        "Dana Reyes: The octo-org AI routing work is in progress."
                    ),
                },
            )
            assert extracted.status_code == 200, extracted.text
            assert extracted.json()["status"] == "completed"
            matters = await client.get("/v1/matters")
            assert matters.json()[0]["scope_id"] == "personal"
            assert matters.json()[0]["title"] == "octo-org AI routing"

            status = await client.get("/v1/connectors/ai/status")
            connections = await client.get("/v1/connections")
            assert "console-secret" not in status.text
            assert "console-secret" not in connections.text
            assert status.json()["config"]["model"] == "console-model"
            assert connections.json()["ai"]["chat_enabled"] is True
            paths = (await client.get("/openapi.json")).json()["paths"]
            for path in [
                "/v1/connectors/ai/config",
                "/v1/connectors/ai/status",
                "/v1/connectors/ai/test",
            ]:
                assert path in paths

        assert factory_calls[-1] == {
            "provider": "openai-compatible",
            "base_url": "https://console.example/v1",
            "api_key": "console-secret",
            "model": "console-model",
            "timeout": 7.0,
        }
        assert any(
            "cards" in item["response_schema"].get("properties", {})
            for item in completion_calls
        )
        text = config_path.read_text(encoding="utf-8")
        assert "console-secret" not in text
        assert "api_key" not in text.casefold()

    asyncio.run(scenario())


def test_ai_test_endpoint_is_mocked_redacted_and_does_not_save_on_failure(
    tmp_path,
) -> None:
    async def scenario() -> None:
        config_path = tmp_path / "matterhorn.toml"

        class FailingGateway:
            def complete(self, **_kwargs):
                raise RuntimeError("provider rejected test-secret")

        runtime = AIRuntime(
            Engine(tmp_path / "test-button.db"),
            config_path=config_path,
            environment={},
            gateway_factory=lambda **_kwargs: FailingGateway(),
        )
        app = create_app(
            engine=runtime.engine,
            ai_runtime=runtime,
            mail_config_path=config_path,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://matterhorn.test",
        ) as client:
            result = await client.post(
                "/v1/connectors/ai/test",
                json={
                    "provider": "openai-compatible",
                    "base_url": "https://failure.example/v1",
                    "model": "failure-model",
                    "timeout": 3,
                    "api_key": "test-secret",
                },
            )
            assert result.status_code == 200
            assert result.json()["reachable"] is False
            assert "[REDACTED]" in result.json()["message"]
            assert "test-secret" not in result.text
            status = await client.get("/v1/connectors/ai/status")
            assert status.json()["configured"] is False

        assert not config_path.exists()

    asyncio.run(scenario())


def test_restart_with_saved_ai_config_requires_key_without_environment(
    tmp_path,
) -> None:
    path = tmp_path / "matterhorn.toml"
    save_ai_config(
        path,
        AIConfig(
            provider="anthropic",
            base_url="https://api.anthropic.com",
            model="saved-model",
        ),
    )
    runtime = AIRuntime(
        Engine(tmp_path / "restart.db"),
        config_path=path,
        environment={},
    )

    assert runtime.status()["configured"] is True
    assert runtime.status()["api_key_state"] == "re-enter API key"
    assert runtime.status()["chat_enabled"] is False
