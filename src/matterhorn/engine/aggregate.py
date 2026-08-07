from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from matterhorn.contracts import ReviewItem
from matterhorn.store import Store


@dataclass(frozen=True)
class ClosedCandidateWorld:
    """The exact parent keys surfaced by one deterministic recall group."""

    keys: tuple[str, ...]

    @classmethod
    def from_keys(cls, keys: Iterable[str]) -> ClosedCandidateWorld:
        ordered: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if not isinstance(key, str) or not key:
                raise ValueError("candidate keys MUST be non-empty strings")
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        return cls(tuple(ordered))

    def contains(self, key: str | None) -> bool:
        return key is not None and key in self.keys

    def contains_all(self, keys: Iterable[str]) -> bool:
        return set(keys).issubset(self.keys)


@dataclass(frozen=True)
class AliasNamespace:
    """One deterministic model-visible alias namespace."""

    entries: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, aliases: Mapping[str, str]) -> AliasNamespace:
        return cls(tuple(aliases.items()))

    @classmethod
    def sequential(
        cls,
        values: Iterable[str],
        *,
        prefix: str,
    ) -> AliasNamespace:
        return cls(
            tuple(
                (f"{prefix}{index}", value)
                for index, value in enumerate(values, start=1)
            )
        )

    def __post_init__(self) -> None:
        aliases: set[str] = set()
        for alias, value in self.entries:
            if not alias or not value:
                raise ValueError("aliases and values MUST be non-empty strings")
            if alias in aliases:
                raise ValueError(f"duplicate alias: {alias}")
            aliases.add(alias)

    def as_dict(self) -> dict[str, str]:
        return dict(self.entries)

    def resolve(self, value: str) -> str:
        return self.as_dict().get(value, value)

    def resolve_all(self, values: Iterable[str]) -> tuple[str, ...]:
        aliases = self.as_dict()
        return tuple(aliases.get(value, value) for value in values)


@dataclass
class RejectionCounter:
    """Shared rejection accounting without imposing layer-specific names."""

    counts: dict[str, int] = field(default_factory=dict)

    def increment(self, reason: str, count: int = 1) -> None:
        if count < 1:
            raise ValueError("rejection increments MUST be positive")
        self.counts[reason] = self.counts.get(reason, 0) + count

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self.counts.items()))


@dataclass(frozen=True)
class AggregateContext:
    unit: Any
    recall: Any
    world: ClosedCandidateWorld
    aliases: AliasNamespace


class KeyStrategy(Protocol):
    """Layer plugin for deterministic parent-key recall."""

    def recall(self, unit: Any) -> Iterable[Any]: ...

    def candidate_keys(self, unit: Any, recall: Any) -> Iterable[str]: ...

    def aliases(self, unit: Any, recall: Any) -> AliasNamespace: ...


class AggregateStrategy(KeyStrategy, Protocol):
    """Layer plugin around the shared recall/adjudicate/gate/commit pipeline."""

    def adjudicate(
        self,
        context: AggregateContext,
        rejections: RejectionCounter,
    ) -> Any: ...

    def gate(
        self,
        context: AggregateContext,
        decision: Any,
        rejections: RejectionCounter,
    ) -> Any: ...

    def commit(
        self,
        prepared: PreparedAggregation,
        operator: AggregateOperator,
    ) -> Any: ...


@dataclass(frozen=True)
class PreparedAggregation:
    context: AggregateContext
    decision: Any
    outcome: Any
    rejection_counts: dict[str, int]
    strategy: AggregateStrategy


class AggregateOperator:
    """Recursive aggregation skeleton shared by routing and themes.

    Transactions remain a strategy concern because layer 0 commits inside the
    enclosing Record transaction while layer 1 opens its own short commit.
    Review insertion is centralized here and therefore follows either caller's
    existing transaction boundary.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    def recall(
        self,
        unit: Any,
        strategy: AggregateStrategy,
    ) -> tuple[AggregateContext, ...]:
        return tuple(
            AggregateContext(
                unit=unit,
                recall=recalled,
                world=ClosedCandidateWorld.from_keys(
                    strategy.candidate_keys(unit, recalled)
                ),
                aliases=strategy.aliases(unit, recalled),
            )
            for recalled in strategy.recall(unit)
        )

    def prepare_contexts(
        self,
        contexts: Iterable[AggregateContext],
        strategy: AggregateStrategy,
    ) -> tuple[PreparedAggregation, ...]:
        prepared: list[PreparedAggregation] = []
        for context in contexts:
            rejections = RejectionCounter()
            decision = strategy.adjudicate(context, rejections)
            outcome = strategy.gate(context, decision, rejections)
            prepared.append(
                PreparedAggregation(
                    context=context,
                    decision=decision,
                    outcome=outcome,
                    rejection_counts=rejections.snapshot(),
                    strategy=strategy,
                )
            )
        return tuple(prepared)

    def prepare(
        self,
        unit: Any,
        strategy: AggregateStrategy,
    ) -> tuple[PreparedAggregation, ...]:
        return self.prepare_contexts(self.recall(unit, strategy), strategy)

    def commit(self, prepared: PreparedAggregation) -> Any:
        return prepared.strategy.commit(prepared, self)

    def run(self, unit: Any, strategy: AggregateStrategy) -> tuple[Any, ...]:
        return tuple(self.commit(item) for item in self.prepare(unit, strategy))

    def enqueue_reviews(self, items: Sequence[ReviewItem]) -> int:
        return sum(int(self.store.add_review_item(item)) for item in items)
