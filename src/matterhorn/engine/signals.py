from __future__ import annotations

import re
from dataclasses import dataclass

from matterhorn.capacity import DEFAULT_HOT_MIN_AUTHORS, DEFAULT_HOT_MIN_MESSAGES

DEFAULT_IDENTITY_HANDLES: tuple[str, ...] = ()
DEFAULT_MACHINE_SENDERS: tuple[str, ...] = (
    r"no-?reply",
    r"notif",
    r"alert",
    r"donotreply",
)
DEFAULT_ALERT_KEYWORDS: tuple[str, ...] = (
    r"alert",
    r"security",
    r"fail(ed|ure)?",
    r"error",
    r"警告",
    r"告警",
    r"失败",
    r"安全",
)
@dataclass(frozen=True)
class SignalConfig:
    identity_handles: tuple[str, ...] = DEFAULT_IDENTITY_HANDLES
    machine_senders: tuple[str, ...] = DEFAULT_MACHINE_SENDERS
    alert_keywords: tuple[str, ...] = DEFAULT_ALERT_KEYWORDS
    hot_min_authors: int = DEFAULT_HOT_MIN_AUTHORS
    hot_min_messages: int = DEFAULT_HOT_MIN_MESSAGES

    def __post_init__(self) -> None:
        if any(not value for value in self.identity_handles):
            raise ValueError("identity handles MUST be non-empty strings")
        if self.hot_min_authors < 1 or self.hot_min_messages < 1:
            raise ValueError("signal hotness thresholds MUST be positive integers")
        for label, patterns in (
            ("machine sender", self.machine_senders),
            ("alert keyword", self.alert_keywords),
        ):
            for pattern in patterns:
                if not pattern:
                    raise ValueError(f"{label} patterns MUST be non-empty")
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise ValueError(f"invalid {label} pattern: {error}") from error


def configured_signal_config(
    *,
    identity_handles: list[str] | tuple[str, ...] | None = None,
    machine_senders: list[str] | tuple[str, ...] | None = None,
    alert_keywords: list[str] | tuple[str, ...] | None = None,
    hot_min_authors: int = DEFAULT_HOT_MIN_AUTHORS,
    hot_min_messages: int = DEFAULT_HOT_MIN_MESSAGES,
) -> SignalConfig:
    return SignalConfig(
        identity_handles=tuple(identity_handles or ()),
        machine_senders=(*DEFAULT_MACHINE_SENDERS, *(machine_senders or ())),
        alert_keywords=(*DEFAULT_ALERT_KEYWORDS, *(alert_keywords or ())),
        hot_min_authors=hot_min_authors,
        hot_min_messages=hot_min_messages,
    )


def best_token_match(
    content: str,
    values: list[str] | tuple[str, ...],
    *,
    digit_prefixes: str,
) -> tuple[str, str] | None:
    """Return (configured value, matched surface) by the normative ranking."""

    matches: list[tuple[int, bytes, str, str]] = []
    for value in values:
        if not value:
            continue
        escaped = re.escape(value)
        if value.isdigit():
            pattern = rf"(?P<prefix>[{re.escape(digit_prefixes)}])(?P<value>{escaped})(?![A-Za-z0-9_])"
        else:
            pattern = rf"(?<![A-Za-z0-9_])(?P<value>{escaped})(?![A-Za-z0-9_])"
        found = re.search(pattern, content, flags=re.IGNORECASE)
        if found is not None:
            matches.append(
                (-len(value), value.encode("utf-8"), value, found.group("value"))
            )
    if not matches:
        return None
    _, _, configured, surface = min(matches)
    return configured, surface


def first_pattern_match(content: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, content, flags=re.IGNORECASE)
        if match is not None:
            return match.group(0)
    return None
