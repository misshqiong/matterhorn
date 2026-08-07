"""Typed deterministic capacity parameters for the layered memory network."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

DEFAULT_LAYER_DEPTH = 2
DEFAULT_THEME_MIN_CLUSTER = 3
DEFAULT_THEME_MIN_BACKLOG = 6
DEFAULT_CONVERSATION_FANOUT = 8
DEFAULT_HOT_MIN_AUTHORS = 3
DEFAULT_HOT_MIN_MESSAGES = 5
DEFAULT_HUMAN_EDGE_WEIGHT = 10
DEFAULT_DECK_WIDTH = 0

# These coefficients encode the previous comparator lexicographically for the
# complete datetime range: blocker count, unseen, then Unix-second recency.
DEFAULT_ACTIVATION_BLOCKED_WEIGHT = 10_000_000_000_000.0
DEFAULT_ACTIVATION_UNSEEN_WEIGHT = 1_000_000_000_000.0
DEFAULT_ACTIVATION_RECENCY_WEIGHT = 1.0
DEFAULT_ACTIVATION_HOTNESS_WEIGHT = 0.0
DEFAULT_ACTIVATION_PINNED_WEIGHT = 0.0

_ACTIVATION_NAMES = ("blocked", "unseen", "recency", "hotness", "pinned")
_ACTIVATION_ALIASES = dict(zip(("w1", "w2", "w3", "w4", "w5"), _ACTIVATION_NAMES))
_LOSS_NAMES = ("missing", "spurious", "mis_attached", "mis_typed", "mis_structured")
_LOSS_ALIASES = dict(zip(("c1", "c2", "c3", "c4", "c5"), _LOSS_NAMES))


def _validate_finite_weights(
    value: ActivationWeights | LossWeights,
    label: str,
    *,
    nonnegative: bool = False,
) -> None:
    for field in fields(value):
        weight = getattr(value, field.name)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise TypeError(f"{label}.{field.name} MUST be numeric")
        if not math.isfinite(weight) or (nonnegative and weight < 0):
            qualifier = "finite and non-negative" if nonnegative else "finite"
            raise ValueError(f"{label}.{field.name} MUST be {qualifier}")


@dataclass(frozen=True, slots=True)
class ActivationWeights:
    blocked: float = DEFAULT_ACTIVATION_BLOCKED_WEIGHT
    unseen: float = DEFAULT_ACTIVATION_UNSEEN_WEIGHT
    recency: float = DEFAULT_ACTIVATION_RECENCY_WEIGHT
    hotness: float = DEFAULT_ACTIVATION_HOTNESS_WEIGHT
    pinned: float = DEFAULT_ACTIVATION_PINNED_WEIGHT

    def __post_init__(self) -> None:
        _validate_finite_weights(self, "activation_weights")


@dataclass(frozen=True, slots=True)
class LossWeights:
    missing: float = 1.0
    spurious: float = 1.0
    mis_attached: float = 1.0
    mis_typed: float = 1.0
    mis_structured: float = 1.0

    def __post_init__(self) -> None:
        _validate_finite_weights(self, "loss_weights", nonnegative=True)


@dataclass(frozen=True, slots=True)
class CapacitySettings:
    """The single S4 registry consumed by runtime, wall, and evaluation code."""

    layer_depth: int = DEFAULT_LAYER_DEPTH
    theme_min_cluster: int = DEFAULT_THEME_MIN_CLUSTER
    theme_min_backlog: int = DEFAULT_THEME_MIN_BACKLOG
    conversation_fanout: int = DEFAULT_CONVERSATION_FANOUT
    hot_min_authors: int = DEFAULT_HOT_MIN_AUTHORS
    hot_min_messages: int = DEFAULT_HOT_MIN_MESSAGES
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT
    activation_weights: ActivationWeights = ActivationWeights()
    loss_weights: LossWeights = LossWeights()
    deck_width: int = DEFAULT_DECK_WIDTH

    def __post_init__(self) -> None:
        if self.layer_depth != DEFAULT_LAYER_DEPTH:
            raise ValueError("layer_depth MUST be 2 until deeper layers are instantiated")
        if isinstance(self.theme_min_cluster, bool) or self.theme_min_cluster < 2:
            raise ValueError("theme_min_cluster MUST be an integer >= 2")
        for name in (
            "theme_min_backlog",
            "conversation_fanout",
            "hot_min_authors",
            "hot_min_messages",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} MUST be a positive integer")
        if (
            isinstance(self.human_edge_weight, bool)
            or not isinstance(self.human_edge_weight, int)
            or self.human_edge_weight < 2
        ):
            raise ValueError("human_edge_weight MUST be an integer >= 2")
        if (
            isinstance(self.deck_width, bool)
            or not isinstance(self.deck_width, int)
            or self.deck_width < 0
        ):
            raise ValueError("deck_width MUST be a non-negative integer")


DEFAULT_CAPACITY = CapacitySettings()


def resolve_capacity(
    *,
    explicit: CapacitySettings | Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    environment: Mapping[str, str] | None = None,
) -> CapacitySettings:
    """Resolve every capacity value as explicit > environment > TOML > default."""

    if isinstance(explicit, CapacitySettings):
        return explicit
    explicit_values = _mapping(explicit, "explicit capacity")
    config_values = _mapping(config, "capacity config")
    env = os.environ if environment is None else environment

    capacity_table = _table(config_values, "capacity")
    themes = _table(config_values, "themes")
    distill = _table(config_values, "distill")
    signals = _table(config_values, "signals")
    wall = _table(config_values, "wall")
    evaluation = _table(config_values, "eval")

    def scalar(
        name: str,
        env_name: str,
        toml_value: Any,
        default: int,
        *,
        explicit_alias: str | None = None,
    ) -> int:
        raw = explicit_values.get(
            name,
            explicit_values.get(explicit_alias) if explicit_alias else None,
        )
        if raw is None:
            raw = env.get(env_name)
        if raw is None:
            raw = toml_value
        if raw is None:
            raw = default
        if isinstance(raw, bool):
            raise TypeError(f"{name} MUST be an integer")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            digits = text[1:] if text.startswith(("+", "-")) else text
            if digits.isdigit():
                return int(text)
        raise ValueError(f"{name} MUST be an integer")

    activation = _resolve_weights(
        ActivationWeights,
        names=_ACTIVATION_NAMES,
        aliases=_ACTIVATION_ALIASES,
        explicit=explicit_values.get("activation_weights"),
        env=env,
        env_name="MATTERHORN_ACTIVATION_WEIGHTS",
        env_prefix="MATTERHORN_ACTIVATION_WEIGHT_",
        toml=wall.get("activation_weights"),
    )
    loss = _resolve_weights(
        LossWeights,
        names=_LOSS_NAMES,
        aliases=_LOSS_ALIASES,
        explicit=explicit_values.get("loss_weights"),
        env=env,
        env_name="MATTERHORN_LOSS_WEIGHTS",
        env_prefix="MATTERHORN_LOSS_WEIGHT_",
        toml=evaluation.get("loss_weights"),
    )
    return CapacitySettings(
        layer_depth=scalar(
            "layer_depth",
            "MATTERHORN_LAYER_DEPTH",
            capacity_table.get("layer_depth"),
            DEFAULT_LAYER_DEPTH,
        ),
        theme_min_cluster=scalar(
            "theme_min_cluster",
            "MATTERHORN_THEME_MIN_CLUSTER",
            themes.get("theme_min_cluster", distill.get("theme_min_cluster")),
            DEFAULT_THEME_MIN_CLUSTER,
        ),
        theme_min_backlog=scalar(
            "theme_min_backlog",
            "MATTERHORN_THEME_MIN_BACKLOG",
            themes.get("theme_min_backlog", distill.get("theme_min_backlog")),
            DEFAULT_THEME_MIN_BACKLOG,
        ),
        conversation_fanout=scalar(
            "conversation_fanout",
            "MATTERHORN_THEME_CONVERSATION_FANOUT",
            themes.get(
                "theme_conversation_fanout",
                distill.get("theme_conversation_fanout"),
            ),
            DEFAULT_CONVERSATION_FANOUT,
            explicit_alias="theme_conversation_fanout",
        ),
        hot_min_authors=scalar(
            "hot_min_authors",
            "MATTERHORN_HOT_MIN_AUTHORS",
            signals.get("hot_min_authors"),
            DEFAULT_HOT_MIN_AUTHORS,
        ),
        hot_min_messages=scalar(
            "hot_min_messages",
            "MATTERHORN_HOT_MIN_MESSAGES",
            signals.get("hot_min_messages"),
            DEFAULT_HOT_MIN_MESSAGES,
        ),
        human_edge_weight=scalar(
            "human_edge_weight",
            "MATTERHORN_HUMAN_EDGE_WEIGHT",
            themes.get("human_edge_weight", distill.get("human_edge_weight")),
            DEFAULT_HUMAN_EDGE_WEIGHT,
        ),
        activation_weights=activation,
        loss_weights=loss,
        deck_width=scalar(
            "deck_width",
            "MATTERHORN_DECK_WIDTH",
            wall.get("deck_width"),
            DEFAULT_DECK_WIDTH,
        ),
    )


def activation_score(
    *,
    blocked: float,
    unseen: bool | float,
    recency: datetime | str | float | None,
    hotness: float = 0,
    pinned: bool | float = False,
    weights: ActivationWeights = DEFAULT_CAPACITY.activation_weights,
) -> float:
    """Evaluate §29.4's pure deterministic activation function."""

    values = (
        float(blocked),
        float(unseen),
        _recency_value(recency),
        float(hotness),
        float(pinned),
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("activation features MUST be finite")
    return float(
        weights.blocked * values[0]
        + weights.unseen * values[1]
        + weights.recency * values[2]
        + weights.hotness * values[3]
        + weights.pinned * values[4]
    )


def matter_activation(
    matter: Mapping[str, Any],
    *,
    weights: ActivationWeights = DEFAULT_CAPACITY.activation_weights,
) -> float:
    """Score one wall payload using the features of the pre-S4 deck comparator."""

    blockers = matter.get("bubbled_blockers")
    blocked = len(blockers) if isinstance(blockers, list) else 0
    return activation_score(
        blocked=blocked,
        unseen=bool(matter.get("unseen")),
        recency=matter.get("latest_activity") or matter.get("updated_at"),
        hotness=matter.get("hotness", 0) or 0,
        pinned=bool(matter.get("pinned", False)),
        weights=weights,
    )


def loss_value(
    counts: Mapping[str, int | float],
    *,
    weights: LossWeights = DEFAULT_CAPACITY.loss_weights,
) -> float:
    """Evaluate §29.5's five-term objective from classified diff counts."""

    values = {name: float(counts.get(name, 0)) for name in _LOSS_NAMES}
    if not all(math.isfinite(value) and value >= 0 for value in values.values()):
        raise ValueError("loss counts MUST be finite and non-negative")
    return float(sum(getattr(weights, name) * values[name] for name in _LOSS_NAMES))


def _resolve_weights(
    weight_type: type[ActivationWeights | LossWeights],
    *,
    names: tuple[str, ...],
    aliases: Mapping[str, str],
    explicit: Any,
    env: Mapping[str, str],
    env_name: str,
    env_prefix: str,
    toml: Any,
) -> ActivationWeights | LossWeights:
    defaults = weight_type()
    explicit_values = _weight_mapping(explicit, names, aliases, "explicit")
    env_values = _weight_mapping(env.get(env_name), names, aliases, env_name)
    toml_values = _weight_mapping(toml, names, aliases, "TOML")
    values: dict[str, float] = {}
    for name in names:
        raw = explicit_values.get(name)
        if raw is None:
            raw = env.get(env_prefix + name.upper(), env_values.get(name))
        if raw is None:
            raw = toml_values.get(name, getattr(defaults, name))
        try:
            values[name] = float(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} weight MUST be numeric") from error
    return weight_type(**values)


def _weight_mapping(
    value: Any,
    names: tuple[str, ...],
    aliases: Mapping[str, str],
    label: str,
) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, (ActivationWeights, LossWeights)):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pairs = [item.strip() for item in value.split(",") if item.strip()]
            if not pairs or any("=" not in item for item in pairs):
                raise ValueError(f"{label} weights MUST be a mapping") from None
            value = dict(item.split("=", 1) for item in pairs)
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} weights MUST be a mapping")
    normalized = {aliases.get(str(key), str(key)): item for key, item in value.items()}
    unknown = sorted(set(normalized) - set(names))
    if unknown:
        raise ValueError(f"{label} weights contain unknown keys: {', '.join(unknown)}")
    return normalized


def _mapping(value: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} MUST be a mapping")
    return value


def _table(config: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, Mapping):
        raise TypeError(f"[{name}] MUST be a TOML table")
    return value


def _recency_value(value: datetime | str | float | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value)
        except ValueError:
            return 0.0
    else:
        return float(value)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.timestamp()


__all__ = [
    "DEFAULT_CAPACITY",
    "ActivationWeights",
    "CapacitySettings",
    "LossWeights",
    "activation_score",
    "loss_value",
    "matter_activation",
    "resolve_capacity",
]
