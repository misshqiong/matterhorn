from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

Sleep = Callable[[float], Awaitable[None]]


class WebhookDispatcher:
    """Deliver deterministic event batches with at-least-once semantics."""

    def __init__(
        self,
        store: Any,
        webhook_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        batch_size: int = 100,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], datetime] | None = None,
    ):
        if max_attempts < 1:
            raise ValueError("webhook max_attempts MUST be positive")
        if backoff_seconds < 0:
            raise ValueError("webhook backoff_seconds MUST be non-negative")
        self.store = store
        self.webhook_url = webhook_url
        self.transport = transport
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.batch_size = batch_size
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(UTC))

    async def deliver_pending(self) -> int:
        events = self.store.pending_webhook_events(
            self.webhook_url, limit=self.batch_size
        )
        if not events:
            return 0
        payload = {
            "events": [event.model_dump(mode="json") for event in events]
        }
        async with httpx.AsyncClient(transport=self.transport) as client:
            for attempt in range(self.max_attempts):
                try:
                    response = await client.post(self.webhook_url, json=payload)
                    response.raise_for_status()
                except httpx.HTTPError:
                    if attempt + 1 == self.max_attempts:
                        return 0
                    await self.sleep(self.backoff_seconds * (2**attempt))
                else:
                    with self.store.transaction():
                        self.store.mark_webhook_delivered(
                            self.webhook_url,
                            [event.event_id for event in events],
                            delivered_at=self.clock(),
                        )
                    return len(events)
        return 0
