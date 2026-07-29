"""Write-path gateway configuration shared by launch surfaces."""

from __future__ import annotations

import os

from matterhorn.distill import (
    AnthropicGateway,
    LlmGateway,
    NullGateway,
    OpenAICompatibleGateway,
)


def configured_gateway(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
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

    if selected == "null":
        return NullGateway()
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
        )
    if selected == "anthropic":
        if not all((resolved_api_key, resolved_model)):
            raise ValueError(
                "anthropic requires an API key and model; use MATTERHORN_MODEL "
                "and MATTERHORN_API_KEY/ANTHROPIC_API_KEY or explicit overrides"
            )
        kwargs = {"api_key": resolved_api_key, "model": resolved_model}
        if resolved_base_url is not None:
            kwargs["base_url"] = resolved_base_url
        return AnthropicGateway(**kwargs)
    raise ValueError(f"unknown provider: {selected}")
