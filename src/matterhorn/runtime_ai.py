"""Pure process-local AI settings and credential lifecycle."""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class AIConfig:
    provider: str
    base_url: str
    model: str
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.provider not in {"openai-compatible", "anthropic"}:
            raise ValueError(
                "AI provider MUST be openai-compatible or anthropic"
            )
        if not self.base_url.strip():
            raise ValueError("AI base_url is required")
        if not self.model.strip():
            raise ValueError("AI model is required")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("AI timeout MUST be a positive finite number")

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "timeout": self.timeout,
        }


_TOML_SECTION = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")


def load_ai_config(path: str | Path) -> AIConfig | None:
    source = Path(path)
    if not source.is_file():
        return None
    try:
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not load {source.name}: {error}") from error
    raw = payload.get("ai")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("[ai] MUST be a TOML table")
    return AIConfig(
        provider=str(raw.get("provider", "")),
        base_url=str(raw.get("base_url", "")),
        model=str(raw.get("model", "")),
        timeout=float(raw.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
    )


def save_ai_config(path: str | Path, config: AIConfig) -> None:
    """Replace only [ai], never serializing any credential."""

    target = Path(path)
    original = target.read_text(encoding="utf-8") if target.is_file() else ""
    retained: list[str] = []
    skipping = False
    for line in original.splitlines():
        match = _TOML_SECTION.match(line)
        if match is not None:
            skipping = match.group(1) == "ai" or match.group(1).startswith("ai.")
            if skipping:
                continue
        if not skipping:
            retained.append(line)
    while retained and not retained[-1].strip():
        retained.pop()
    rendered = [
        *retained,
        *([""] if retained else []),
        "[ai]",
        *[
            f"{key} = {_toml_scalar(value)}"
            for key, value in config.public_dict().items()
        ],
        "",
    ]
    target.write_text("\n".join(rendered), encoding="utf-8")


def _toml_scalar(value: Any) -> str:
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


class AIRuntime:
    """Process-local AI credential and provider composition."""

    def __init__(
        self,
        engine: Any,
        *,
        config_path: str | Path,
        environment: dict[str, str] | None = None,
        gateway_factory: Any = None,
        chat_runner_factory: Any = None,
    ):
        self.engine = engine
        self.config_path = Path(config_path)
        self._environment = environment if environment is not None else os.environ
        self._gateway_factory = gateway_factory
        self._chat_runner_factory = chat_runner_factory
        self.config = load_ai_config(self.config_path)
        self._api_key = self._environment_key(
            self.config.provider if self.config is not None else None
        )
        self._key_source = "environment" if self._api_key else None
        self._source = "runtime" if self.config is not None else None
        self.chat_runner = None
        effective = self.config or self._environment_config()
        if effective is not None:
            if self.config is None:
                self._source = "environment"
                self.config = effective
            self._apply(effective)

    def configure(
        self,
        config: AIConfig,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        candidate_key = api_key or self._api_key
        try:
            gateway, runner = self._build(config, candidate_key)
        except Exception as error:  # noqa: BLE001
            message = str(error)
            if candidate_key:
                message = message.replace(candidate_key, "[REDACTED]")
            raise ValueError(f"AI configuration failed: {message}") from None
        save_ai_config(self.config_path, config)
        if api_key:
            self._api_key = api_key
            self._key_source = "memory"
        self.config = config
        self._source = "runtime"
        self._install(gateway, runner)
        return self.status()

    def test(
        self,
        config: AIConfig,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        candidate_key = api_key or self._api_key or self._environment_key(
            config.provider
        )
        if not candidate_key:
            return {
                "reachable": False,
                "message": "Re-enter API key before testing.",
            }
        try:
            gateway = self._gateway(config, candidate_key)
            gateway.complete(
                system="Return only JSON matching the supplied schema.",
                user='Return {"ok":true}.',
                response_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            )
        except Exception as error:  # noqa: BLE001
            message = str(error).replace(candidate_key, "[REDACTED]")
            return {
                "reachable": False,
                "message": f"AI provider test failed: {message}",
            }
        return {"reachable": True, "message": "AI provider is reachable."}

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.config is not None,
            "config": (
                self.config.public_dict() if self.config is not None else None
            ),
            "source": self._source,
            "api_key_state": (
                "loaded from environment"
                if self._key_source == "environment"
                else (
                    "loaded in process memory"
                    if self._key_source == "memory"
                    else "re-enter API key"
                )
            ),
            "chat_enabled": self.chat_runner is not None,
        }

    def _apply(self, config: AIConfig) -> None:
        gateway, runner = self._build(config, self._api_key)
        self._install(gateway, runner)

    def _build(
        self,
        config: AIConfig,
        api_key: str | None,
    ) -> tuple[Any, Any]:
        composer = getattr(self.engine, "compose_runtime_ai", None)
        if composer is None:
            raise TypeError(
                "runtime AI configuration requires matterhorn.defaults.Engine"
            )
        return composer(
            provider=config.provider,
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            gateway_factory=self._gateway_factory,
            chat_runner_factory=self._chat_runner_factory,
        )

    def _gateway(self, config: AIConfig, api_key: str) -> Any:
        builder = getattr(self.engine, "build_runtime_gateway", None)
        if builder is None:
            raise TypeError(
                "runtime AI configuration requires matterhorn.defaults.Engine"
            )
        return builder(
            provider=config.provider,
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            gateway_factory=self._gateway_factory,
        )

    def _install(self, gateway: Any, runner: Any) -> None:
        setter = getattr(self.engine, "set_write_gateway", None)
        if setter is None:
            raise TypeError(
                "runtime AI configuration requires matterhorn.defaults.Engine"
            )
        setter(gateway)
        self.chat_runner = runner

    def _environment_key(self, provider: str | None) -> str | None:
        fallback = {
            "openai-compatible": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }.get(provider or self._environment.get("MATTERHORN_PROVIDER"))
        return self._environment.get("MATTERHORN_API_KEY") or (
            self._environment.get(fallback) if fallback is not None else None
        )

    def _environment_config(self) -> AIConfig | None:
        provider = self._environment.get("MATTERHORN_PROVIDER")
        model = self._environment.get("MATTERHORN_MODEL")
        base_url = self._environment.get("MATTERHORN_BASE_URL")
        if provider == "anthropic":
            base_url = base_url or "https://api.anthropic.com"
        if provider not in {"openai-compatible", "anthropic"}:
            return None
        if not model or not base_url or not self._api_key:
            return None
        raw_timeout = self._environment.get(
            "MATTERHORN_TIMEOUT",
            str(DEFAULT_TIMEOUT_SECONDS),
        )
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError(
                "MATTERHORN_TIMEOUT MUST be a number of seconds"
            ) from error
        return AIConfig(
            provider=provider,
            base_url=base_url,
            model=model,
            timeout=timeout,
        )
