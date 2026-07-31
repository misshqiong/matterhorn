"""Write-path gateway configuration shared by launch surfaces."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from matterhorn.distill import (
    AnthropicGateway,
    LlmGateway,
    NullGateway,
    OpenAICompatibleGateway,
)
from matterhorn.runtime_ai import (
    AIConfig,
    AIRuntime,
    load_ai_config,
    save_ai_config,
)

DEFAULT_TIMEOUT_SECONDS = 60.0


class FixtureFileGateway:
    """Offline gateway used by the shipped quickstart and conformance transcripts."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._payload = json.loads(self.path.read_text(encoding="utf-8"))
        self._indices: dict[str, int] = {}

    def complete(
        self, *, system: str, user: str, response_schema: dict
    ) -> str:
        del system, user
        properties = response_schema.get("properties", {})
        key = "record_extraction" if "cards" in properties else "distillation"
        configured: Any = self._payload.get(key)
        if configured is None:
            raise ValueError(f"fixture gateway has no {key!r} response")
        responses = configured if isinstance(configured, list) else [configured]
        index = self._indices.get(key, 0)
        if index >= len(responses):
            raise ValueError(f"fixture gateway exhausted {key!r} responses")
        self._indices[key] = index + 1
        return json.dumps(responses[index], ensure_ascii=False)


def configured_gateway(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    fixture_path: str | Path | None = None,
    timeout: float | None = None,
) -> LlmGateway:
    selected = provider or os.environ.get("MATTERHORN_PROVIDER", "null")
    resolved_base_url = (
        base_url if base_url is not None else os.environ.get("MATTERHORN_BASE_URL")
    )
    provider_fallback_key = {
        "openai-compatible": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(selected)
    resolved_api_key = (
        api_key if api_key is not None else os.environ.get("MATTERHORN_API_KEY")
    )
    if resolved_api_key is None and provider_fallback_key is not None:
        resolved_api_key = os.environ.get(provider_fallback_key)
    resolved_model = model or os.environ.get("MATTERHORN_MODEL")
    resolved_timeout = timeout if timeout is not None else _timeout_seconds()
    if not math.isfinite(resolved_timeout) or resolved_timeout <= 0:
        raise ValueError("gateway timeout MUST be a positive finite number")

    if selected == "null":
        return NullGateway()
    if selected == "fixture":
        resolved_fixture = fixture_path or os.environ.get(
            "MATTERHORN_FIXTURE_PATH"
        )
        if resolved_fixture is None:
            raise ValueError(
                "fixture requires MATTERHORN_FIXTURE_PATH pointing to a JSON file"
            )
        return FixtureFileGateway(resolved_fixture)
    if selected == "openai-compatible":
        if not all((resolved_base_url, resolved_api_key, resolved_model)):
            raise ValueError(
                "openai-compatible requires a base URL, API key, and model; "
                "use MATTERHORN_BASE_URL, MATTERHORN_MODEL, and "
                "MATTERHORN_API_KEY/OPENAI_API_KEY or explicit overrides"
            )
        return OpenAICompatibleGateway(
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            model=resolved_model,
            timeout=resolved_timeout,
        )
    if selected == "anthropic":
        if not all((resolved_api_key, resolved_model)):
            raise ValueError(
                "anthropic requires an API key and model; use MATTERHORN_MODEL "
                "and MATTERHORN_API_KEY/ANTHROPIC_API_KEY or explicit overrides"
            )
        kwargs = {
            "api_key": resolved_api_key,
            "model": resolved_model,
            "timeout": resolved_timeout,
        }
        if resolved_base_url is not None:
            kwargs["base_url"] = resolved_base_url
        return AnthropicGateway(**kwargs)
    raise ValueError(f"unknown provider: {selected}")


def _timeout_seconds() -> float:
    raw = os.environ.get("MATTERHORN_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("MATTERHORN_TIMEOUT MUST be a number of seconds") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError("MATTERHORN_TIMEOUT MUST be a positive finite number")
    return value


__all__ = [
    "AIConfig",
    "AIRuntime",
    "FixtureFileGateway",
    "configured_gateway",
    "load_ai_config",
    "save_ai_config",
]
