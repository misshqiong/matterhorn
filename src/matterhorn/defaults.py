"""Public SDK composition with Matterhorn's replaceable built-in plugins."""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from matterhorn.adapters.messages import MessageCardExtractor
from matterhorn.contracts import RecordExtractor, SchemaProfile
from matterhorn.distill import LlmGateway, NullGateway
from matterhorn.engine.engine import Clock
from matterhorn.engine.engine import Engine as CoreEngine
from matterhorn.store import Store


class Engine(CoreEngine):
    """Engine composed with the built-in Record extractor by default."""

    def __init__(
        self,
        store: str | Path | Store,
        schema: str | Path | SchemaProfile = "org-matters/v1",
        *,
        clock: Clock | Iterable[datetime] | None = None,
        llm: LlmGateway | None = None,
        gateway: LlmGateway | None = None,
        extractor: RecordExtractor | None = None,
        staging_retention_days: float = CoreEngine.DEFAULT_STAGING_RETENTION_DAYS,
        max_batch_delay_minutes: float = CoreEngine.DEFAULT_MAX_BATCH_DELAY_MINUTES,
        min_batch_messages: int = CoreEngine.DEFAULT_MIN_BATCH_MESSAGES,
        identity_handles: list[str] | tuple[str, ...] | None = None,
        machine_senders: list[str] | tuple[str, ...] | None = None,
        alert_keywords: list[str] | tuple[str, ...] | None = None,
        hot_min_authors: int = CoreEngine.DEFAULT_HOT_MIN_AUTHORS,
        hot_min_messages: int = CoreEngine.DEFAULT_HOT_MIN_MESSAGES,
        unified_loop: bool | None = None,
        alignment_samples_dir: str | Path | None = None,
        theme_converge: str | None = None,
        theme_min_cluster: int | None = None,
        theme_min_backlog: int | None = None,
        theme_interval_hours: float | None = None,
        theme_conversation_fanout: int | None = None,
        human_edge_weight: int | None = None,
    ):
        from matterhorn.engine.theme_converge import configured_theme_settings

        theme_settings = configured_theme_settings(
            mode=theme_converge,
            min_cluster=theme_min_cluster,
            min_backlog=theme_min_backlog,
            interval_hours=theme_interval_hours,
            conversation_fanout=theme_conversation_fanout,
            human_edge_weight=human_edge_weight,
        )
        super().__init__(
            store,
            schema,
            clock=clock,
            llm=llm,
            gateway=gateway,
            extractor=extractor,
            staging_retention_days=staging_retention_days,
            max_batch_delay_minutes=max_batch_delay_minutes,
            min_batch_messages=min_batch_messages,
            identity_handles=identity_handles,
            machine_senders=machine_senders,
            alert_keywords=alert_keywords,
            hot_min_authors=hot_min_authors,
            hot_min_messages=hot_min_messages,
            unified_loop=_unified_loop_enabled(unified_loop),
            alignment_samples_dir=alignment_samples_dir,
            theme_converge=theme_settings.mode,
            theme_min_cluster=theme_settings.min_cluster,
            theme_min_backlog=theme_settings.min_backlog,
            theme_interval_hours=theme_settings.interval_hours,
            theme_conversation_fanout=theme_settings.conversation_fanout,
            human_edge_weight=theme_settings.human_edge_weight,
        )
        if extractor is None:
            self._extractor = MessageCardExtractor(
                self._write_gateway,
                self.profile,
            )

    def set_write_gateway(self, gateway: LlmGateway) -> None:
        """Replace write-side provider composition for subsequent work.

        Existing provider calls already executing retain their local gateway
        object. New extraction and distillation calls observe this replacement.
        """

        self._write_gateway = gateway
        self._extractor = MessageCardExtractor(gateway, self.profile)

    def build_runtime_gateway(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        gateway_factory=None,
    ) -> LlmGateway:
        if gateway_factory is None:
            from matterhorn.gateway_config import configured_gateway

            gateway_factory = configured_gateway
        return gateway_factory(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
        )

    def compose_runtime_ai(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float,
        gateway_factory=None,
        chat_runner_factory=None,
    ):
        if not api_key:
            return NullGateway(), None
        gateway = self.build_runtime_gateway(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            gateway_factory=gateway_factory,
        )
        if chat_runner_factory is None:
            from matterhorn.console.chat import ConsoleChatRunner

            chat_runner_factory = ConsoleChatRunner
        runner = chat_runner_factory(
            provider=provider,
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
        )
        return gateway, runner


def _unified_loop_enabled(value: bool | None) -> bool:
    if value is not None:
        if not isinstance(value, bool):
            raise ValueError("unified_loop MUST be a boolean")
        return value
    raw = os.environ.get("MATTERHORN_UNIFIED_LOOP")
    if raw is None:
        return False
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        "MATTERHORN_UNIFIED_LOOP MUST be true/false, yes/no, on/off, or 1/0"
    )
