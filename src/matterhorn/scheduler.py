from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from typing import Any


def parse_daily_flush_at(value: str | None) -> time | None:
    if value is None:
        return None
    try:
        hour_text, minute_text = value.split(":")
        if (
            len(hour_text) != 2
            or len(minute_text) != 2
            or not hour_text.isdigit()
            or not minute_text.isdigit()
        ):
            raise ValueError
        parsed = time(int(hour_text), int(minute_text))
    except ValueError as error:
        raise ValueError("daily_flush_at MUST use UTC HH:MM") from error
    if value != parsed.strftime("%H:%M"):
        raise ValueError("daily_flush_at MUST use UTC HH:MM")
    return parsed


class ServiceScheduler:
    """One injectable-clock tick for service-only flush policies."""

    def __init__(
        self,
        engine: Any,
        *,
        quiet_period_minutes: float | None = None,
        daily_flush_at: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.engine = engine
        self.quiet_period_minutes = quiet_period_minutes
        self.daily_flush_at = parse_daily_flush_at(daily_flush_at)
        self.clock = clock or engine.now
        self._last_daily_flush: date | None = None

    def tick(self) -> list[Any]:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        reports = []
        if self.quiet_period_minutes is not None:
            reports.extend(
                self.engine.flush_quiet_at(self.quiet_period_minutes, now)
            )
        if (
            self.daily_flush_at is not None
            and now.time().replace(tzinfo=None) >= self.daily_flush_at
            and self._last_daily_flush != now.date()
        ):
            reports.extend(self.engine.flush_pending())
            self._last_daily_flush = now.date()
        return reports
