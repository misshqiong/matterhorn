"""Public SDK composition with Matterhorn's replaceable built-in plugins."""

from __future__ import annotations

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
    ):
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
