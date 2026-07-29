"""Public SDK composition with Matterhorn's replaceable built-in plugins."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from matterhorn.adapters.messages import MessageCardExtractor
from matterhorn.contracts import RecordExtractor, SchemaProfile
from matterhorn.distill import LlmGateway
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
    ):
        super().__init__(
            store,
            schema,
            clock=clock,
            llm=llm,
            gateway=gateway,
            extractor=extractor,
        )
        if extractor is None:
            self._extractor = MessageCardExtractor(
                self._write_gateway,
                self.profile,
            )
