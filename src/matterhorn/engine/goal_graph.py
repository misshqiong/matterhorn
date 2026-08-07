from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from matterhorn.canonical import object_key
from matterhorn.contracts import (
    FIELD_WIDE_RETRACT,
    Assertion,
    Operation,
    Origin,
    SchemaProfile,
    SubjectMerge,
    SubjectRecord,
)
from matterhorn.engine.structure_election import (
    DEFAULT_HUMAN_EDGE_WEIGHT,
    PART_OF,
    elected_part_of,
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
    HUMAN_PLACEMENT_HELD = "STRUCTURE_HUMAN_PLACEMENT_HELD"


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
            if target == updates["subject_key"]:
                # A merge can collapse both endpoints of a stored structure
                # edge onto one subject. Admission rejects self-reference,
                # so such an edge is void: it must not project, vote in
                # elections, or count as an unchanged duplicate.
                continue
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
    if assertion.predicate not in STRUCTURE_EDGE_PREDICATES:
        return None
    if assertion.operation not in {Operation.ASSERT, Operation.RETRACT}:
        return None
    assertions = list(assertions)
    merges = list(merges)
    edges = merge_edges(merges)
    source = canonical_subject_key(assertion.subject_key, edges)
    if assertion.operation == Operation.ASSERT:
        if not isinstance(assertion.object_value, str) or not assertion.object_value:
            return StructureRejection.INVALID_TARGET
        keys = {item.subject_key for item in subjects}
        if assertion.object_value not in keys:
            return (
                StructureRejection.CROSS_SCOPE
                if target_exists_outside_scope
                else StructureRejection.UNKNOWN_TARGET
            )
        target = canonical_subject_key(assertion.object_value, edges)
        if source == target:
            return StructureRejection.SELF_REFERENCE
    # Minting test: RETRACT shifts votes exactly like ASSERT, so both run
    # it. One operation can change only the asserting subject's own elected
    # edges; reject exactly when it does change them AND leaves the subject
    # on an active cycle. A losing vote (edges unchanged) passes — blocking
    # it would also block votes that accumulate toward escaping a legacy
    # cycle. Known accepted corner: an operation that swaps the subject
    # from one cycle onto another is admitted (net cycle count unchanged);
    # the audit read reports the survivor.
    before = active_structure_adjacency(
        assertions,
        merges,
        profile=profile,
        human_edge_weight=human_edge_weight,
    )
    after = active_structure_adjacency(
        [*assertions, assertion],
        merges,
        profile=profile,
        human_edge_weight=human_edge_weight,
    )
    if before.get(source, set()) != after.get(source, set()) and _cycle_through(
        after, source
    ):
        return StructureRejection.CYCLE
    return None


def active_structure_adjacency(
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
    *,
    profile: SchemaProfile,
    human_edge_weight: int,
) -> dict[str, set[str]]:
    intervals, _ = project_assertions(
        canonicalize_graph_assertions(assertions, merges),
        profile,
        human_edge_weight=human_edge_weight,
    )
    adjacency: dict[str, set[str]] = {}
    for interval in intervals:
        if (
            interval.valid_to is None
            and interval.predicate in STRUCTURE_EDGE_PREDICATES
            and isinstance(interval.object_value, str)
        ):
            adjacency.setdefault(interval.subject_key, set()).add(
                interval.object_value
            )
    return adjacency


def _cycle_through(adjacency: dict[str, set[str]], origin: str) -> bool:
    """True when origin lies on a cycle of active structure edges.

    One admission can shift only the origin subject's own elected edges, so
    a cycle the admission creates must pass through origin. A projected
    cycle that predates the admission (vote shifts through retracts, an
    election-semantics migration) never vetoes an edge outside it.
    """

    seen: set[str] = set()
    stack = sorted(adjacency.get(origin, ()), key=lambda value: value.encode())
    while stack:
        node = stack.pop()
        if node == origin:
            return True
        if node in seen:
            continue
        seen.add(node)
        if len(seen) > MAX_GRAPH_VISITS:
            return True
        stack.extend(
            sorted(adjacency.get(node, ()), key=lambda value: value.encode())
        )
    return False


def automatic_reparent_rejection(
    assertion: Assertion,
    *,
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
) -> StructureRejection | None:
    """Reject a model re-parent that contradicts a standing human placement.

    INV-22 counts votes, and a human vote is worth ten. But votes only bound
    a decision that is made once. An automatic door re-proposes parentage on
    every window, so ten windows outvote one human -- and once a human
    retraction empties the slot, the next window re-asserts into it for free.
    This gate runs only on automatic doors: it governs which doors may
    PRODUCE admissions, leaving INV-22's arithmetic untouched for the
    correction door, where a human is doing the outvoting deliberately.

    A human placement is held when the human has spoken about this subject's
    parentage and the model now names something else: a live human placement
    on another target, or a human retraction of this exact (child, target)
    pair at any time. Naming the human's own live placement is never a
    re-parent and always passes.
    """

    if assertion.predicate != PART_OF or assertion.origin is not Origin.model:
        return None
    merges = list(merges)
    edges = merge_edges(merges)
    source = canonical_subject_key(assertion.subject_key, edges)
    target = (
        canonical_subject_key(assertion.object_value, edges)
        if isinstance(assertion.object_value, str) and assertion.object_value
        else None
    )
    # Read exactly the rows the election reads. Canonicalization rewrites a
    # structure edge's object_key to its canonical target and drops an edge
    # whose endpoints a merge collapsed onto one subject; reading raw rows
    # made this gate hold placements the projection had already withdrawn,
    # permanently closing both automatic doors for that subject.
    human_rows = [
        item
        for item in canonicalize_graph_assertions(assertions, merges)
        if item.predicate == PART_OF
        and item.origin is Origin.human
        and item.subject_key == source
    ]
    if not human_rows:
        return None
    # Ask the election what the human still holds rather than replaying the
    # rows again. Retraction, supersession and tie-breaking already live in
    # election_history; a second hand-written replay diverged from it in
    # four distinct ways before this call replaced it.
    election = elected_part_of(human_rows).get(
        (assertion.scope_id, source)
    )
    live = election.elected_target if election is not None else None
    if target is not None and target == live:
        # Agreeing with the human's own live placement is not a re-parent.
        return None
    for item in human_rows:
        if item.operation is not Operation.RETRACT:
            continue
        if item.object_key == FIELD_WIDE_RETRACT:
            return StructureRejection.HUMAN_PLACEMENT_HELD
        if isinstance(item.object_value, str) and item.object_value == target:
            return StructureRejection.HUMAN_PLACEMENT_HELD
    if live is not None:
        return StructureRejection.HUMAN_PLACEMENT_HELD
    return None


def merge_activates_cycle(
    profile: SchemaProfile,
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
    candidate: SubjectMerge,
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> bool:
    """True when applying the candidate merge would mint an active cycle.

    Every edge a merge rewrites gains the canonical survivor as an
    endpoint, so a minted cycle must pass through the survivor. A cycle
    the survivor already sits on is legacy, not minted — vetoing it would
    take the repair tool away from exactly the graphs that need repair.
    """

    assertions = list(assertions)
    merges = list(merges)
    before = active_structure_adjacency(
        assertions,
        merges,
        profile=profile,
        human_edge_weight=human_edge_weight,
    )
    survivor_before = canonical_subject_key(
        candidate.target_subject_key, merge_edges(merges)
    )
    after_merges = [*merges, candidate]
    after = active_structure_adjacency(
        assertions,
        after_merges,
        profile=profile,
        human_edge_weight=human_edge_weight,
    )
    survivor_after = canonical_subject_key(
        candidate.target_subject_key, merge_edges(after_merges)
    )
    return _cycle_through(after, survivor_after) and not _cycle_through(
        before, survivor_before
    )


def structure_cycles(
    profile: SchemaProfile,
    assertions: Iterable[Assertion],
    merges: Iterable[SubjectMerge],
    *,
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT,
) -> list[list[str]]:
    """List projected structure-edge cycles as canonical key sequences.

    Pure zero-model read for the cycle audit: each cycle appears once per
    back edge, nodes in traversal order starting at the earliest-reached
    member. An empty result certifies the scope's active graph is acyclic.
    """

    adjacency = active_structure_adjacency(
        assertions,
        merges,
        profile=profile,
        human_edge_weight=human_edge_weight,
    )
    nodes: set[str] = set(adjacency)
    for targets in adjacency.values():
        nodes |= targets
    state: dict[str, int] = {}
    cycles: list[list[str]] = []
    for start in sorted(nodes, key=lambda value: value.encode()):
        if state.get(start, 0) != 0:
            continue
        state[start] = 1
        path = [start]
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
                path.pop()
                continue
            if state.get(target, 0) == 1:
                cycles.append(path[path.index(target) :])
                continue
            if state.get(target, 0) == 2:
                continue
            state[target] = 1
            path.append(target)
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
    return cycles


def _single(values: list[Any]) -> Any:
    return values[0] if values else None
