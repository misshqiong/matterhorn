from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from matterhorn.canonical import object_key
from matterhorn.contracts import (
    Assertion,
    Operation,
    SchemaProfile,
    SubjectMerge,
    SubjectRecord,
)
from matterhorn.engine.structure_election import (
    DEFAULT_HUMAN_EDGE_WEIGHT,
    PART_OF,
)
from matterhorn.projection import project_assertions

SPAWNED_FROM = "spawned_from"
DECISION = "decision"
STRUCTURE_EDGE_PREDICATES = frozenset({PART_OF, SPAWNED_FROM})
MAX_GRAPH_VISITS = 10_000


class StructureRejection(str, Enum):
    INVALID_TARGET = "STRUCTURE_INVALID_TARGET"
    UNKNOWN_TARGET = "STRUCTURE_UNKNOWN_TARGET"
    CROSS_SCOPE = "STRUCTURE_CROSS_SCOPE"
    SELF_REFERENCE = "STRUCTURE_SELF_REFERENCE"
    CYCLE = "STRUCTURE_CYCLE"


@dataclass(frozen=True)
class GraphNode:
    subject_key: str
    title: str
    status: Any
    blocker: list[Any]
    birth_instant: datetime | None
    parent_subject_key: str | None
    decisions: list[Any]
    decision_points: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StructuralRollup:
    descendants_total: int
    descendants_completed: int
    descendants_blocked: int
    bubbled_blockers: list[dict[str, Any]]
    latest_activity: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MatterGraph:
    scope_id: str
    subject_key: str
    root_subject_key: str
    node: GraphNode
    parent_chain: list[GraphNode]
    children: list[GraphNode]
    tree: dict[str, Any]
    rollup: StructuralRollup

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "subject_key": self.subject_key,
            "root_subject_key": self.root_subject_key,
            "node": self.node.to_dict(),
            "parent_chain": [item.to_dict() for item in self.parent_chain],
            "children": [item.to_dict() for item in self.children],
            "tree": self.tree,
            "rollup": self.rollup.to_dict(),
        }


@dataclass(frozen=True)
class GoalGraphProjection:
    nodes: dict[str, GraphNode]
    parents: dict[str, str]
    children: dict[str, list[str]]
    latest_activity: dict[str, datetime]
    completed_values: frozenset[str]

    def root_for(self, subject_key: str) -> str:
        current = subject_key
        visited: set[str] = set()
        while current in self.parents and len(visited) < MAX_GRAPH_VISITS:
            if current in visited:
                break
            visited.add(current)
            current = self.parents[current]
        return current

    def descendants(self, subject_key: str) -> list[str]:
        result: list[str] = []
        pending = list(reversed(self.children.get(subject_key, [])))
        visited = {subject_key}
        while pending and len(visited) < MAX_GRAPH_VISITS:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            result.append(current)
            pending.extend(reversed(self.children.get(current, [])))
        return result

    def rollup(self, root_subject_key: str) -> StructuralRollup:
        descendants = self.descendants(root_subject_key)
        completed = sum(
            str(self.nodes[key].status).casefold() in self.completed_values
            for key in descendants
        )
        bubbled = [
            {
                "subject_key": key,
                "blocker": self.nodes[key].blocker,
            }
            for key in descendants
            if self.nodes[key].blocker
        ]
        subtree = [root_subject_key, *descendants]
        latest = max(
            (
                self.latest_activity[key]
                for key in subtree
                if key in self.latest_activity
            ),
            default=None,
        )
        return StructuralRollup(
            descendants_total=len(descendants),
            descendants_completed=completed,
            descendants_blocked=len(bubbled),
            bubbled_blockers=bubbled,
            latest_activity=latest,
        )

    def tree(self, root_subject_key: str) -> dict[str, Any]:
        root = {**self.nodes[root_subject_key].to_dict(), "children": []}
        stack: list[tuple[str, dict[str, Any]]] = [(root_subject_key, root)]
        visited = {root_subject_key}
        while stack:
            subject_key, payload = stack.pop()
            pending: list[tuple[str, dict[str, Any]]] = []
            for child in self.children.get(subject_key, []):
                if child not in self.nodes:
                    continue
                child_payload = {
                    **self.nodes[child].to_dict(),
                    "children": [],
                }
                payload["children"].append(child_payload)
                if child in visited or len(visited) >= MAX_GRAPH_VISITS:
                    child_payload["truncated"] = True
                    continue
                visited.add(child)
                pending.append((child, child_payload))
            stack.extend(reversed(pending))
        return root


def merge_edges(merges: Iterable[SubjectMerge]) -> dict[str, str]:
    return {
        merge.source_subject_key: merge.target_subject_key
        for merge in merges
    }


def canonical_subject_key(subject_key: str, edges: dict[str, str]) -> str:
    current = subject_key
    visited: set[str] = set()
    while current in edges and len(visited) < MAX_GRAPH_VISITS:
        if current in visited:
            raise ValueError("subject merge graph contains a cycle")
        visited.add(current)
        current = edges[current]
    if len(visited) >= MAX_GRAPH_VISITS:
        raise ValueError("subject merge graph traversal exceeded its bound")
    return current


def canonicalize_graph_assertions(
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
) -> list[Assertion]:
    edges = merge_edges(merges)
    result = []
    for assertion in assertions:
        updates: dict[str, Any] = {
            "subject_key": canonical_subject_key(assertion.subject_key, edges)
        }
        if (
            assertion.predicate in STRUCTURE_EDGE_PREDICATES
            and isinstance(assertion.object_value, str)
        ):
            target = canonical_subject_key(assertion.object_value, edges)
            updates.update(
                {
                    "object_value": target,
                    "object_key": object_key(target),
                }
            )
        result.append(assertion.model_copy(update=updates))
    return result


def project_goal_graph(
    profile: SchemaProfile,
    subjects: Iterable[SubjectRecord],
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> GoalGraphProjection:
    merges = list(merges)
    edges = merge_edges(merges)
    original_subjects = list(subjects)
    by_original = {item.subject_key: item for item in original_subjects}
    canonical_subjects: dict[str, SubjectRecord] = {}
    for subject in original_subjects:
        key = canonical_subject_key(subject.subject_key, edges)
        canonical_subjects[key] = by_original.get(key, subject)

    canonical_assertions = canonicalize_graph_assertions(assertions, merges)
    intervals, _ = project_assertions(
        canonical_assertions,
        profile,
        human_edge_weight=human_edge_weight,
    )
    current: dict[tuple[str, str], list[Any]] = {}
    active_edges: dict[tuple[str, str], tuple[str, datetime]] = {}
    decisions: dict[str, list[tuple[datetime, str, Any]]] = {}
    for interval in intervals:
        if interval.predicate == DECISION:
            decisions.setdefault(interval.subject_key, []).append(
                (interval.valid_from, interval.assertion_id, interval.object_value)
            )
        if interval.valid_to is not None:
            continue
        current.setdefault((interval.subject_key, interval.predicate), []).append(
            interval.object_value
        )
        if (
            interval.predicate in STRUCTURE_EDGE_PREDICATES
            and isinstance(interval.object_value, str)
        ):
            active_edges[(interval.subject_key, interval.predicate)] = (
                interval.object_value,
                interval.valid_from,
            )

    earliest: dict[str, datetime] = {}
    latest: dict[str, datetime] = {}
    for assertion in canonical_assertions:
        previous_earliest = earliest.get(assertion.subject_key)
        if previous_earliest is None or assertion.valid_from < previous_earliest:
            earliest[assertion.subject_key] = assertion.valid_from
        previous_latest = latest.get(assertion.subject_key)
        if previous_latest is None or assertion.recorded_at > previous_latest:
            latest[assertion.subject_key] = assertion.recorded_at

    parents = {
        source: target
        for (source, predicate), (target, _) in active_edges.items()
        if predicate == PART_OF and target in canonical_subjects
    }
    nodes = {}
    for key, subject in canonical_subjects.items():
        spawned = active_edges.get((key, SPAWNED_FROM))
        blocker = current.get((key, "blocked_by"), [])
        nodes[key] = GraphNode(
            subject_key=key,
            title=subject.title,
            status=_single(current.get((key, "status"), [])),
            blocker=sorted(blocker, key=lambda value: str(value).encode("utf-8")),
            birth_instant=spawned[1] if spawned is not None else earliest.get(key),
            parent_subject_key=parents.get(key),
            decisions=[
                value
                for _, _, value in sorted(
                    decisions.get(key, []),
                    key=lambda item: (item[0], item[1].encode("utf-8")),
                )
            ],
            decision_points=[
                {"value": value, "at": instant}
                for instant, _, value in sorted(
                    decisions.get(key, []),
                    key=lambda item: (item[0], item[1].encode("utf-8")),
                )
            ],
        )
    children: dict[str, list[str]] = {}
    for child, parent in parents.items():
        if child not in nodes or parent not in nodes:
            continue
        children.setdefault(parent, []).append(child)
    for values in children.values():
        values.sort(
            key=lambda key: (
                nodes[key].birth_instant or datetime.min.replace(tzinfo=UTC),
                key.encode("utf-8"),
            )
        )
    completed_values = frozenset(
        str(value).casefold()
        for value in (
            profile.completion.completed_values if profile.completion else []
        )
    )
    return GoalGraphProjection(
        nodes=nodes,
        parents=parents,
        children=children,
        latest_activity=latest,
        completed_values=completed_values,
    )


def matter_graph(
    *,
    scope_id: str,
    subject_key: str,
    profile: SchemaProfile,
    subjects: Iterable[SubjectRecord],
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> MatterGraph:
    merges = list(merges)
    projection = project_goal_graph(
        profile,
        subjects,
        assertions,
        merges,
        human_edge_weight=human_edge_weight,
    )
    selected = canonical_subject_key(subject_key, merge_edges(merges))
    if selected not in projection.nodes:
        raise KeyError(selected)
    parents: list[GraphNode] = []
    current = selected
    visited = {selected}
    while current in projection.parents and len(visited) < MAX_GRAPH_VISITS:
        parent = projection.parents[current]
        if parent in visited or parent not in projection.nodes:
            break
        parents.append(projection.nodes[parent])
        visited.add(parent)
        current = parent
    root = current
    return MatterGraph(
        scope_id=scope_id,
        subject_key=selected,
        root_subject_key=root,
        node=projection.nodes[selected],
        parent_chain=parents,
        children=[
            projection.nodes[key]
            for key in projection.children.get(selected, [])
        ],
        tree=projection.tree(root),
        rollup=projection.rollup(root),
    )


def structure_rejection(
    assertion: Assertion,
    *,
    profile: SchemaProfile,
    subjects: Iterable[SubjectRecord],
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
    target_exists_outside_scope: bool = False,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> StructureRejection | None:
    if (
        assertion.operation != Operation.ASSERT
        or assertion.predicate not in STRUCTURE_EDGE_PREDICATES
    ):
        return None
    if not isinstance(assertion.object_value, str) or not assertion.object_value:
        return StructureRejection.INVALID_TARGET
    subjects = list(subjects)
    assertions = list(assertions)
    merges = list(merges)
    keys = {item.subject_key for item in subjects}
    if assertion.object_value not in keys:
        return (
            StructureRejection.CROSS_SCOPE
            if target_exists_outside_scope
            else StructureRejection.UNKNOWN_TARGET
        )
    edges = merge_edges(merges)
    source = canonical_subject_key(assertion.subject_key, edges)
    target = canonical_subject_key(assertion.object_value, edges)
    if source == target:
        return StructureRejection.SELF_REFERENCE
    projected = project_goal_graph(
        profile,
        subjects,
        [*assertions, assertion],
        merges,
        human_edge_weight=human_edge_weight,
    )
    adjacency: dict[str, set[str]] = {}
    canonical_assertions = canonicalize_graph_assertions(
        [*assertions, assertion], merges
    )
    intervals, _ = project_assertions(
        canonical_assertions,
        profile,
        human_edge_weight=human_edge_weight,
    )
    for interval in intervals:
        if (
            interval.valid_to is None
            and interval.predicate in STRUCTURE_EDGE_PREDICATES
            and isinstance(interval.object_value, str)
        ):
            adjacency.setdefault(interval.subject_key, set()).add(
                interval.object_value
            )
    if _has_cycle(adjacency, set(projected.nodes)):
        return StructureRejection.CYCLE
    return None


def _has_cycle(adjacency: dict[str, set[str]], nodes: set[str]) -> bool:
    state: dict[str, int] = {}
    visits = 0
    for start in sorted(nodes):
        if state.get(start, 0) != 0:
            continue
        state[start] = 1
        visits += 1
        stack = [
            (
                start,
                iter(
                    sorted(
                        adjacency.get(start, ()),
                        key=lambda value: value.encode(),
                    )
                ),
            )
        ]
        while stack:
            node, targets = stack[-1]
            try:
                target = next(targets)
            except StopIteration:
                state[node] = 2
                stack.pop()
                continue
            if target not in nodes or state.get(target, 0) == 2:
                continue
            if state.get(target, 0) == 1:
                return True
            visits += 1
            if visits > MAX_GRAPH_VISITS:
                return True
            state[target] = 1
            stack.append(
                (
                    target,
                    iter(
                        sorted(
                            adjacency.get(target, ()),
                            key=lambda value: value.encode(),
                        )
                    ),
                )
            )
    return False


def _single(values: list[Any]) -> Any:
    return values[0] if values else None
