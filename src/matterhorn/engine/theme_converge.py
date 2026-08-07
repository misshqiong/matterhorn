from __future__ import annotations

import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from matterhorn.canonical import (
    as_utc,
    canonical_json,
    derive_assertion_id,
    normalize_title,
    object_key,
    stable_hash,
)
from matterhorn.contracts import (
    Assertion,
    EpisodeCard,
    Operation,
    Origin,
    ReviewItem,
    SourceRef,
    SubjectRecord,
    TaskStatus,
)
from matterhorn.contracts.models import StrictModel
from matterhorn.distill import ToolLoopGateway
from matterhorn.engine.aggregate import (
    AggregateContext,
    AggregateOperator,
    AliasNamespace,
    ClosedCandidateWorld,
    PreparedAggregation,
    RejectionCounter,
)
from matterhorn.engine.goal_graph import (
    PART_OF,
    canonicalize_graph_assertions,
    project_goal_graph,
    structure_rejection,
)
from matterhorn.engine.structure_election import (
    DEFAULT_HUMAN_EDGE_WEIGHT,
    validate_human_edge_weight,
)
from matterhorn.engine.unified_loop import run_bounded_tool_loop
from matterhorn.store.base import ThemeScheduleState

DEFAULT_THEME_MIN_CLUSTER = 3
DEFAULT_THEME_MIN_BACKLOG = 6
DEFAULT_THEME_INTERVAL_HOURS = 6.0
DEFAULT_THEME_CONVERSATION_FANOUT = 8
DEFAULT_THEME_CONVERGE = "review"
THEME_TASK_KIND = "themes"
THEME_REVIEW_MARKER = "theme_convergence"


class AffinityKind(str, Enum):
    handle = "handle"
    evidence = "evidence"
    title = "title"


@dataclass(frozen=True)
class ThemeSettings:
    mode: Literal["off", "review", "auto"] = DEFAULT_THEME_CONVERGE
    min_cluster: int = DEFAULT_THEME_MIN_CLUSTER
    min_backlog: int = DEFAULT_THEME_MIN_BACKLOG
    interval_hours: float = DEFAULT_THEME_INTERVAL_HOURS
    conversation_fanout: int = DEFAULT_THEME_CONVERSATION_FANOUT
    human_edge_weight: int = DEFAULT_HUMAN_EDGE_WEIGHT

    def __post_init__(self) -> None:
        if self.mode not in {"off", "review", "auto"}:
            raise ValueError("theme_converge MUST be off, review, or auto")
        if isinstance(self.min_cluster, bool) or self.min_cluster < 2:
            raise ValueError("theme_min_cluster MUST be an integer >= 2")
        if isinstance(self.min_backlog, bool) or self.min_backlog < 1:
            raise ValueError("theme_min_backlog MUST be a positive integer")
        if (
            isinstance(self.interval_hours, bool)
            or not isinstance(self.interval_hours, (int, float))
            or not math.isfinite(self.interval_hours)
            or self.interval_hours <= 0
        ):
            raise ValueError("theme_interval_hours MUST be positive")
        if isinstance(self.conversation_fanout, bool) or self.conversation_fanout < 1:
            raise ValueError("theme_conversation_fanout MUST be a positive integer")
        validate_human_edge_weight(self.human_edge_weight)


@dataclass(frozen=True)
class ThemeCandidate:
    subject_key: str
    subject_type: str
    title: str
    title_tokens: frozenset[str]
    handle_values: frozenset[str]
    conversations: frozenset[str]
    source_refs: tuple[SourceRef, ...]
    child_count: int = 0
    descendant_count: int = 0


@dataclass(frozen=True)
class AffinityEdge:
    left: str
    right: str
    kinds: frozenset[AffinityKind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "kinds": sorted(item.value for item in self.kinds),
        }


@dataclass(frozen=True)
class ThemeCluster:
    members: tuple[ThemeCandidate, ...]
    edges: tuple[AffinityEdge, ...]
    affinity_kinds: frozenset[AffinityKind]
    existing_target_root: str | None = None

    @property
    def member_keys(self) -> tuple[str, ...]:
        return tuple(item.subject_key for item in self.members)

    @property
    def confident(self) -> bool:
        return len(self.affinity_kinds) >= 2 or (
            len(self.members) >= 5 and len(self.affinity_kinds) == 1
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_subject_keys": list(self.member_keys),
            "affinity_kinds": sorted(item.value for item in self.affinity_kinds),
            "existing_target_root": self.existing_target_root,
            "confident": self.confident,
            "edges": [item.to_dict() for item in self.edges],
        }


@dataclass(frozen=True)
class ThemeSnapshot:
    scope_id: str
    candidates: tuple[ThemeCandidate, ...]
    subjects: tuple[SubjectRecord, ...]
    assertions: tuple[Assertion, ...]
    merges: tuple[Any, ...]
    pending_member_keys: frozenset[str] = frozenset()


class ThemeNamingEmission(StrictModel):
    title: str = Field(min_length=1)
    member_subject_keys: list[str] = Field(min_length=1)
    existing_root_subsumes: bool | None = None

    @model_validator(mode="after")
    def members_are_unique(self) -> ThemeNamingEmission:
        if len(self.member_subject_keys) != len(set(self.member_subject_keys)):
            raise ValueError("member_subject_keys MUST be unique")
        if not normalize_title(self.title):
            raise ValueError("theme title MUST contain normalized tokens")
        return self


@dataclass(frozen=True)
class ThemeNamingResult:
    proposal: ThemeNamingEmission | None
    rejection_counts: dict[str, int]
    tool_calls: int
    emissions: int
    exhausted: bool


@dataclass(frozen=True)
class ThemeProposalResult:
    title: str
    member_subject_keys: tuple[str, ...]
    parent_subject_key: str | None
    existing_target_root: str | None
    disposition: str
    edges_applied: int = 0
    reviews_enqueued: int = 0
    root_created: bool = False
    rejection_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThemePassReport:
    scope_id: str
    mode: str
    dry_run: bool
    clusters: tuple[ThemeCluster, ...]
    proposals: tuple[ThemeProposalResult, ...]
    tool_calls: int = 0
    emissions: int = 0

    @property
    def edges_applied(self) -> int:
        return sum(item.edges_applied for item in self.proposals)

    @property
    def reviews_enqueued(self) -> int:
        return sum(item.reviews_enqueued for item in self.proposals)

    @property
    def roots_created(self) -> int:
        return sum(item.root_created for item in self.proposals)

    @property
    def rejection_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for proposal in self.proposals:
            for reason, count in proposal.rejection_counts.items():
                result[reason] = result.get(reason, 0) + count
        return dict(sorted(result.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "clusters": [item.to_dict() for item in self.clusters],
            "proposals": [item.to_dict() for item in self.proposals],
            "edges_applied": self.edges_applied,
            "reviews_enqueued": self.reviews_enqueued,
            "roots_created": self.roots_created,
            "rejection_counts": self.rejection_counts,
            "tool_calls": self.tool_calls,
            "emissions": self.emissions,
        }


class ThemeNamingSession:
    """One proposal-only section 26 tool loop over a fixed cluster world."""

    def __init__(
        self,
        *,
        scope_id: str,
        cluster: ThemeCluster,
        world: ClosedCandidateWorld | None = None,
        rejections: RejectionCounter | None = None,
    ) -> None:
        self.scope_id = scope_id
        self.cluster = cluster
        self._world = world or ClosedCandidateWorld.from_keys(
            (
                *cluster.member_keys,
                *(
                    (cluster.existing_target_root,)
                    if cluster.existing_target_root is not None
                    else ()
                ),
            )
        )
        self.closed_world = frozenset(self._world.keys)
        self.proposal: ThemeNamingEmission | None = None
        self._rejections = rejections or RejectionCounter()

    def run(self, gateway: Any) -> ThemeNamingResult:
        if not isinstance(gateway, ToolLoopGateway):
            raise TypeError("theme convergence requires a tool-loop capable gateway")
        result = run_bounded_tool_loop(
            gateway,
            system=(
                "Name one deterministic candidate cluster. The closed world is "
                "exactly the supplied subjects. Call emit at most once with a "
                "concise theme title and the member subset that belongs under it; "
                "drop outliers but never invent a subject. When an existing target "
                "root is supplied, set existing_root_subsumes true only when it "
                "should be the parent. Do not emit assertions or other subjects."
            ),
            user=canonical_json(
                {
                    "scope_id": self.scope_id,
                    "closed_world": [
                        {
                            "subject_key": member.subject_key,
                            "title": member.title,
                            "existing_target": (
                                member.subject_key
                                == self.cluster.existing_target_root
                            ),
                        }
                        for member in self.cluster.members
                    ],
                    "affinity_kinds": sorted(
                        item.value for item in self.cluster.affinity_kinds
                    ),
                }
            ),
            tools=[_theme_emit_tool()],
            handler=self.handle_tool,
            max_emissions=1,
        )
        return ThemeNamingResult(
            proposal=self.proposal,
            rejection_counts=self._rejections.snapshot(),
            tool_calls=result.tool_calls,
            emissions=result.emissions,
            exhausted=result.exhausted,
        )

    def handle_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name != "emit":
            return self._reject("UNKNOWN_TOOL")
        if self.proposal is not None:
            return self._reject("TOO_MANY_EMISSIONS")
        try:
            proposal = ThemeNamingEmission.model_validate(arguments)
        except ValidationError as error:
            return self._reject("UNPARSEABLE", detail=str(error))
        if not self._world.contains_all(proposal.member_subject_keys):
            return self._reject("CLOSED_WORLD_VIOLATION")
        target = self.cluster.existing_target_root
        if target is not None and proposal.existing_root_subsumes is not True:
            return self._reject("EXISTING_ROOT_NOT_SUBSUMING")
        if target is None and proposal.existing_root_subsumes is not None:
            # Tolerable deviation, same precedent as adjudication abstain
            # normalization: the model volunteering a subsumption where no
            # target exists is noise, not a reason to lose the naming.
            self._rejections.increment("EXISTING_ROOT_IGNORED")
            proposal = proposal.model_copy(update={"existing_root_subsumes": None})
        self.proposal = proposal
        return {"accepted": True}

    def _reject(self, reason: str, *, detail: str | None = None) -> dict[str, Any]:
        self._rejections.increment(reason)
        result: dict[str, Any] = {"accepted": False, "reason": reason}
        if detail is not None:
            result["detail"] = detail
        return result


class _ThemeKeyStrategy:
    """Section 28 layer-1→2 affinity recall and admission plugin."""

    def __init__(
        self,
        engine: Any,
        *,
        dry_run: bool,
    ) -> None:
        self.engine = engine
        self.dry_run = dry_run
        self.settings: ThemeSettings = engine.theme_settings

    def recall(self, unit: Any) -> tuple[ThemeCluster, ...]:
        snapshot = unit
        return cluster_themes(
            snapshot.candidates,
            min_cluster=self.settings.min_cluster,
            conversation_fanout=self.settings.conversation_fanout,
        )

    def candidate_keys(
        self,
        _unit: Any,
        cluster: ThemeCluster,
    ) -> tuple[str, ...]:
        return (
            *cluster.member_keys,
            *(
                (cluster.existing_target_root,)
                if cluster.existing_target_root is not None
                else ()
            ),
        )

    def aliases(self, _unit: Any, _cluster: ThemeCluster) -> AliasNamespace:
        return AliasNamespace()

    def adjudicate(
        self,
        context: AggregateContext,
        rejections: RejectionCounter,
    ) -> ThemeNamingResult:
        return ThemeNamingSession(
            scope_id=context.unit.scope_id,
            cluster=context.recall,
            world=context.world,
            rejections=rejections,
        ).run(self.engine._write_gateway)

    def gate(
        self,
        _context: AggregateContext,
        decision: Any,
        rejections: RejectionCounter,
    ) -> ThemeNamingResult:
        naming = decision
        if naming.proposal is None and naming.exhausted:
            rejections.increment("LOOP_BOUNDS_EXHAUSTED")
        return naming

    def commit(
        self,
        prepared: PreparedAggregation,
        operator: AggregateOperator,
    ) -> ThemeProposalResult:
        snapshot = prepared.context.unit
        cluster = prepared.context.recall
        naming = prepared.outcome
        if naming.proposal is None:
            result = ThemeProposalResult(
                title="",
                member_subject_keys=(),
                parent_subject_key=cluster.existing_target_root,
                existing_target_root=cluster.existing_target_root,
                disposition="rejected",
                rejection_counts=prepared.rejection_counts,
            )
            if prepared.rejection_counts and not self.dry_run:
                with self.engine.store.transaction():
                    self.engine.store.record_gate_report(
                        snapshot.scope_id,
                        accepted=0,
                        rejections=prepared.rejection_counts,
                    )
            return result

        disposition: Literal["auto", "review"] = (
            "auto"
            if self.settings.mode == "auto" and cluster.confident
            else "review"
        )
        if self.dry_run:
            parent = cluster.existing_target_root or _theme_subject_key(
                snapshot.scope_id,
                naming.proposal.title,
                naming.proposal.member_subject_keys,
            )
            return ThemeProposalResult(
                title=naming.proposal.title,
                member_subject_keys=tuple(naming.proposal.member_subject_keys),
                parent_subject_key=parent,
                existing_target_root=cluster.existing_target_root,
                disposition=f"dry-run-{disposition}",
            )
        return _apply_proposal(
            self.engine,
            snapshot,
            cluster,
            naming.proposal,
            disposition=disposition,
            operator=operator,
        )


def configured_theme_settings(
    *,
    mode: str | None = None,
    min_cluster: int | None = None,
    min_backlog: int | None = None,
    interval_hours: float | None = None,
    conversation_fanout: int | None = None,
    human_edge_weight: int | None = None,
) -> ThemeSettings:
    selected_mode = _env_value("MATTERHORN_THEME_CONVERGE", mode)
    selected_min_cluster = _env_number(
        "MATTERHORN_THEME_MIN_CLUSTER", min_cluster, DEFAULT_THEME_MIN_CLUSTER, int
    )
    selected_min_backlog = _env_number(
        "MATTERHORN_THEME_MIN_BACKLOG", min_backlog, DEFAULT_THEME_MIN_BACKLOG, int
    )
    selected_interval = _env_number(
        "MATTERHORN_THEME_INTERVAL_HOURS",
        interval_hours,
        DEFAULT_THEME_INTERVAL_HOURS,
        float,
    )
    selected_conversation_fanout = _env_number(
        "MATTERHORN_THEME_CONVERSATION_FANOUT",
        conversation_fanout,
        DEFAULT_THEME_CONVERSATION_FANOUT,
        int,
    )
    selected_human_edge_weight = _env_number(
        "MATTERHORN_HUMAN_EDGE_WEIGHT",
        human_edge_weight,
        DEFAULT_HUMAN_EDGE_WEIGHT,
        int,
    )
    return ThemeSettings(
        mode=str(selected_mode or DEFAULT_THEME_CONVERGE).strip().casefold(),
        min_cluster=selected_min_cluster,
        min_backlog=selected_min_backlog,
        interval_hours=selected_interval,
        conversation_fanout=selected_conversation_fanout,
        human_edge_weight=selected_human_edge_weight,
    )


def snapshot_theme_state(engine: Any, scope_id: str) -> ThemeSnapshot:
    """Load one committed snapshot; clustering below is pure over this value."""

    with engine.store.transaction():
        subjects = tuple(engine.store.subjects(scope_id))
        assertions = tuple(engine.store.assertions(scope_id))
        merges = tuple(engine.store.subject_merges(scope_id))
        handles = tuple(engine.store.active_subject_handles(scope_id))
        observations = tuple(engine.store.record_observations(scope_id))
        reviews = tuple(engine.store.review_items(scope_id))

    graph = project_goal_graph(
        engine.profile,
        subjects,
        assertions,
        merges,
        human_edge_weight=engine.human_edge_weight,
    )
    canonical_assertions = canonicalize_graph_assertions(assertions, merges)
    by_subject: dict[str, list[Assertion]] = {}
    for assertion in canonical_assertions:
        by_subject.setdefault(assertion.subject_key, []).append(assertion)
    observation_containers: dict[str, set[str]] = {}
    for item in observations:
        observation_containers.setdefault(item.record_id, set()).add(item.container_id)
    handle_values: dict[str, set[str]] = {}
    for handle in handles:
        key = engine.canonical_subject_key(scope_id, handle.subject_key)
        handle_values.setdefault(key, set()).add(handle.normalized_value)
    pending_member_keys = frozenset(
        str(candidate["subject_key"])
        for review in reviews
        for candidate in review.candidates_json
        if candidate.get(THEME_REVIEW_MARKER) is True
        and isinstance(candidate.get("subject_key"), str)
    )

    canonical_subjects: dict[str, SubjectRecord] = {}
    for subject in subjects:
        key = engine.canonical_subject_key(scope_id, subject.subject_key)
        if key == subject.subject_key or key not in canonical_subjects:
            canonical_subjects[key] = subject
    matter_types = {
        item.type for item in engine.profile.subjects if item.type == "MATTER"
    }
    candidates: list[ThemeCandidate] = []
    for key in sorted(canonical_subjects, key=lambda value: value.encode("utf-8")):
        subject = canonical_subjects[key]
        node = graph.nodes.get(key)
        if subject.subject_type not in matter_types or node is None:
            continue
        if key in graph.parents or key in pending_member_keys:
            continue
        if str(node.status).casefold() in graph.completed_values:
            continue
        refs = _subject_source_refs(by_subject.get(key, []))
        conversations: set[str] = set()
        for ref in refs:
            containers = observation_containers.get(ref.source_id)
            if containers:
                conversations.update(containers)
        candidates.append(
            ThemeCandidate(
                subject_key=key,
                subject_type=subject.subject_type,
                title=subject.title,
                title_tokens=title_affinity_tokens(subject.title),
                handle_values=frozenset(handle_values.get(key, set())),
                conversations=frozenset(conversations),
                source_refs=refs,
                child_count=len(graph.children.get(key, [])),
                descendant_count=len(graph.descendants(key)),
            )
        )
    return ThemeSnapshot(
        scope_id=scope_id,
        candidates=tuple(candidates),
        subjects=subjects,
        assertions=assertions,
        merges=merges,
        pending_member_keys=pending_member_keys,
    )


def cluster_themes(
    candidates: tuple[ThemeCandidate, ...] | list[ThemeCandidate],
    *,
    min_cluster: int = DEFAULT_THEME_MIN_CLUSTER,
    conversation_fanout: int = DEFAULT_THEME_CONVERSATION_FANOUT,
) -> tuple[ThemeCluster, ...]:
    """Return deterministic connected components from an immutable snapshot."""

    ordered = sorted(candidates, key=lambda item: item.subject_key.encode("utf-8"))
    by_key = {item.subject_key: item for item in ordered}
    conversation_counts: dict[str, int] = {}
    for candidate in ordered:
        for conversation in candidate.conversations:
            conversation_counts[conversation] = (
                conversation_counts.get(conversation, 0) + 1
            )
    discounted_conversations = {
        conversation
        for conversation, count in conversation_counts.items()
        if count > conversation_fanout
    }
    edges: list[AffinityEdge] = []
    strong_pairs: list[tuple[str, str]] = []
    weak_pairs: list[tuple[str, str]] = []
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            kinds, weak = _affinity_kinds(
                left,
                right,
                discounted_conversations=discounted_conversations,
            )
            # A conversation is a venue, not a bond (spec 28.1): evidence
            # alone never forms an edge, it only strengthens the confidence
            # gate once a handle or title bond exists.
            if not (kinds - {AffinityKind.evidence}):
                continue
            pair = (left.subject_key, right.subject_key)
            (weak_pairs if weak else strong_pairs).append(pair)
            edges.append(AffinityEdge(pair[0], pair[1], kinds))

    # Two-tier union (spec 28.1): strong edges build components; a weak
    # edge may attach loners but never merges two strong components.
    root_of = {candidate.subject_key: candidate.subject_key for candidate in ordered}

    def find(key: str) -> str:
        while root_of[key] != key:
            root_of[key] = root_of[root_of[key]]
            key = root_of[key]
        return key

    strong_root: dict[str, bool] = dict.fromkeys(root_of, False)

    def union(left_key: str, right_key: str, *, strong: bool) -> None:
        left_root, right_root = find(left_key), find(right_key)
        if left_root == right_root:
            strong_root[left_root] = strong_root[left_root] or strong
            return
        winner, loser = sorted(
            (left_root, right_root), key=lambda value: value.encode("utf-8")
        )
        root_of[loser] = winner
        strong_root[winner] = strong_root[winner] or strong_root[loser] or strong

    for pair in sorted(strong_pairs):
        union(*pair, strong=True)
    for pair in sorted(weak_pairs):
        left_root, right_root = find(pair[0]), find(pair[1])
        if (
            left_root != right_root
            and strong_root[left_root]
            and strong_root[right_root]
        ):
            continue
        union(*pair, strong=False)

    components: dict[str, set[str]] = {}
    for key in root_of:
        components.setdefault(find(key), set()).add(key)

    result: list[ThemeCluster] = []
    for _, component in sorted(
        components.items(), key=lambda item: item[0].encode("utf-8")
    ):
        if len(component) < min_cluster:
            continue
        component_edges = tuple(
            edge
            for edge in edges
            if edge.left in component and edge.right in component
        )
        kinds = frozenset(
            kind for edge in component_edges for kind in edge.kinds
        )
        existing = [by_key[key] for key in component if by_key[key].child_count]
        target = (
            min(
                existing,
                key=lambda item: (
                    -item.descendant_count,
                    item.subject_key.encode("utf-8"),
                ),
            ).subject_key
            if existing
            else None
        )
        result.append(
            ThemeCluster(
                members=tuple(
                    by_key[key]
                    for key in sorted(component, key=lambda value: value.encode("utf-8"))
                ),
                edges=component_edges,
                affinity_kinds=kinds,
                existing_target_root=target,
            )
        )
    return tuple(sorted(result, key=lambda item: item.member_keys[0].encode("utf-8")))


def run_theme_pass(
    engine: Any,
    scope_id: str,
    *,
    dry_run: bool = False,
) -> ThemePassReport:
    settings: ThemeSettings = engine.theme_settings
    snapshot = snapshot_theme_state(engine, scope_id)
    operator: AggregateOperator = engine._aggregate
    strategy = _ThemeKeyStrategy(engine, dry_run=dry_run)
    contexts = operator.recall(snapshot, strategy)
    clusters = tuple(context.recall for context in contexts)
    if settings.mode == "off":
        return ThemePassReport(scope_id, settings.mode, dry_run, clusters, ())

    prepared = operator.prepare_contexts(contexts, strategy)
    proposals = tuple(operator.commit(item) for item in prepared)
    report = ThemePassReport(
        scope_id,
        settings.mode,
        dry_run,
        clusters,
        proposals,
        sum(item.decision.tool_calls for item in prepared),
        sum(item.decision.emissions for item in prepared),
    )
    if not dry_run:
        with engine.store.transaction():
            engine.store.set_theme_schedule_state(
                scope_id,
                last_run_at=engine.now(),
            )
    return report


def enqueue_theme_pass_if_due(engine: Any, scope_id: str) -> bool:
    settings: ThemeSettings = engine.theme_settings
    if settings.mode == "off":
        return False
    snapshot = snapshot_theme_state(engine, scope_id)
    if len(snapshot.candidates) < settings.min_backlog:
        return False
    now = engine.now()
    state = engine.store.theme_schedule_state(scope_id)
    last = _latest_schedule_instant(state)
    if last is not None and now < last + timedelta(hours=settings.interval_hours):
        return False
    pending = any(
        row.kind == THEME_TASK_KIND
        and row.result.status in {
            TaskStatus.pending,
            TaskStatus.running,
            TaskStatus.failed,
        }
        for row in engine.store.flushable_tasks(scope_id)
    )
    if pending:
        return False
    interval_seconds = settings.interval_hours * 60 * 60
    interval_bucket = int(as_utc(now).timestamp() // interval_seconds)
    task_id = "task_theme_" + stable_hash([scope_id, interval_bucket])
    with engine.store.transaction():
        inserted = engine.store.create_task(
            task_id=task_id,
            scope_id=scope_id,
            kind=THEME_TASK_KIND,
            payload={},
            accepted=0,
            created_at=now,
            newest_message_at=None,
        )
        if inserted:
            engine.store.set_theme_schedule_state(
                scope_id,
                last_enqueued_at=now,
            )
    return inserted


def _apply_proposal(
    engine: Any,
    snapshot: ThemeSnapshot,
    cluster: ThemeCluster,
    proposal: ThemeNamingEmission,
    *,
    disposition: Literal["auto", "review"],
    operator: AggregateOperator,
) -> ThemeProposalResult:
    with engine.store.transaction():
        return _apply_proposal_locked(
            engine,
            snapshot,
            cluster,
            proposal,
            disposition=disposition,
            operator=operator,
        )


def _apply_proposal_locked(
    engine: Any,
    snapshot: ThemeSnapshot,
    cluster: ThemeCluster,
    proposal: ThemeNamingEmission,
    *,
    disposition: Literal["auto", "review"],
    operator: AggregateOperator,
) -> ThemeProposalResult:
    selected = tuple(proposal.member_subject_keys)
    target = cluster.existing_target_root
    parent_key = target or _theme_subject_key(snapshot.scope_id, proposal.title, selected)
    now = engine.now()
    current_snapshot = snapshot_theme_state(engine, snapshot.scope_id)
    current_by_key = {item.subject_key: item for item in current_snapshot.candidates}
    original_by_key = {item.subject_key: item for item in cluster.members}
    parent = next(
        (item for item in current_snapshot.subjects if item.subject_key == parent_key),
        None,
    )
    root_created = False
    proposed_subject = parent
    if proposed_subject is None and target is None:
        source_ids = frozenset(
            ref.source_id
            for key in selected
            for ref in original_by_key[key].source_refs[:1]
        )
        proposed_subject = SubjectRecord(
            scope_id=snapshot.scope_id,
            subject_key=parent_key,
            subject_type="MATTER",
            title=proposal.title,
            normalized_title=normalize_title(proposal.title),
            source_ids=source_ids,
        )

    subjects_for_gate = [*current_snapshot.subjects]
    if proposed_subject is not None and not any(
        item.subject_key == proposed_subject.subject_key for item in subjects_for_gate
    ):
        subjects_for_gate.append(proposed_subject)
    assertions_for_gate = list(current_snapshot.assertions)
    accepted: list[tuple[Assertion, ThemeCandidate]] = []
    rejections: dict[str, int] = {}
    for member_key in selected:
        if member_key == parent_key:
            continue
        member = current_by_key.get(member_key)
        if member is None:
            rejections["ALREADY_PARENTED_OR_INACTIVE"] = (
                rejections.get("ALREADY_PARENTED_OR_INACTIVE", 0) + 1
            )
            continue
        source_refs = list(member.source_refs[:1])
        if not source_refs:
            rejections["NO_SOURCES"] = rejections.get("NO_SOURCES", 0) + 1
            continue
        subject = next(
            (item for item in current_snapshot.subjects if item.subject_key == member_key),
            None,
        )
        if subject is None:
            rejections["UNKNOWN_SUBJECT"] = rejections.get("UNKNOWN_SUBJECT", 0) + 1
            continue
        assertion = Assertion(
            assertion_id=derive_assertion_id(
                snapshot.scope_id,
                member_key,
                PART_OF,
                Operation.ASSERT,
                object_key(parent_key),
                now,
                source_refs,
            ),
            scope_id=snapshot.scope_id,
            subject_key=member_key,
            subject_type=subject.subject_type,
            predicate=PART_OF,
            operation=Operation.ASSERT,
            object_value=parent_key,
            object_key=object_key(parent_key),
            valid_from=now,
            recorded_at=now,
            source_refs=source_refs,
            origin=Origin.model,
        )
        rejection = structure_rejection(
            assertion,
            profile=engine.profile,
            subjects=subjects_for_gate,
            assertions=[*assertions_for_gate, *(item[0] for item in accepted)],
            merges=current_snapshot.merges,
            human_edge_weight=engine.human_edge_weight,
        )
        if rejection is not None:
            rejections[rejection.value] = rejections.get(rejection.value, 0) + 1
            continue
        if engine._model_assertion_is_unchanged(
            assertion,
            [*assertions_for_gate, *(item[0] for item in accepted)],
        ):
            rejections["UNCHANGED"] = rejections.get("UNCHANGED", 0) + 1
            continue
        accepted.append((assertion, member))

    edges_applied = reviews_enqueued = 0
    if accepted:
        if proposed_subject is not None and parent is None:
            engine.store.upsert_subject(proposed_subject)
            root_created = True
        if disposition == "auto":
            for assertion, _ in accepted:
                if engine._add_assertion(assertion):
                    edges_applied += 1
        else:
            parent_title = (
                proposed_subject.title
                if proposed_subject is not None
                else proposal.title
            )
            for assertion, member in accepted:
                review = _theme_review_item(
                    snapshot.scope_id,
                    assertion,
                    member,
                    parent_title=parent_title,
                    affinity_kinds=cluster.affinity_kinds,
                    created_at=now,
                )
                reviews_enqueued += operator.enqueue_reviews((review,))
        engine.store.record_gate_report(
            snapshot.scope_id,
            accepted=edges_applied,
            rejections={
                key: value
                for key, value in rejections.items()
                if key != "UNCHANGED"
            },
            unchanged_dropped=rejections.get("UNCHANGED", 0),
        )
        if edges_applied or root_created:
            engine._rebuild(snapshot.scope_id)
    elif rejections:
        engine.store.record_gate_report(
            snapshot.scope_id,
            accepted=0,
            rejections={
                key: value
                for key, value in rejections.items()
                if key != "UNCHANGED"
            },
            unchanged_dropped=rejections.get("UNCHANGED", 0),
        )
    return ThemeProposalResult(
        title=proposal.title,
        member_subject_keys=selected,
        parent_subject_key=parent_key,
        existing_target_root=target,
        disposition=disposition,
        edges_applied=edges_applied,
        reviews_enqueued=reviews_enqueued,
        root_created=root_created,
        rejection_counts=dict(sorted(rejections.items())),
    )


def _theme_review_item(
    scope_id: str,
    assertion: Assertion,
    member: ThemeCandidate,
    *,
    parent_title: str,
    affinity_kinds: frozenset[AffinityKind],
    created_at: datetime,
) -> ReviewItem:
    source_ref = assertion.source_refs[0]
    review_id = "review_theme_" + stable_hash(
        [scope_id, assertion.subject_key, assertion.object_value]
    )
    card = EpisodeCard(
        card_id="theme_" + stable_hash([scope_id, assertion.subject_key]),
        scope_id=scope_id,
        date=source_ref.sent_at.date(),
        title=member.title,
        source_refs=[source_ref],
        subject_key=assertion.subject_key,
    )
    return ReviewItem(
        scope_id=scope_id,
        review_id=review_id,
        card_json=card.model_dump(mode="json"),
        reasons=["PARENT_SUGGESTION"],
        candidates_json=[
            {
                "action": "attach_subgoal",
                "subject_key": assertion.subject_key,
                "parent_subject_key": assertion.object_value,
                "title": parent_title,
                THEME_REVIEW_MARKER: True,
                "affinity_kinds": sorted(item.value for item in affinity_kinds),
                "member_evidence": [
                    ref.model_dump(mode="json") for ref in assertion.source_refs
                ],
            }
        ],
        created_at=created_at,
    )


def _theme_emit_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "emit",
            "description": "Return one closed-world theme naming proposal.",
            "parameters": ThemeNamingEmission.model_json_schema(),
        },
    }


def _affinity_kinds(
    left: ThemeCandidate,
    right: ThemeCandidate,
    *,
    discounted_conversations: set[str] | frozenset[str] = frozenset(),
) -> tuple[frozenset[AffinityKind], bool]:
    """Return (kinds, weak): weak bonds carry a lone distinctive token only."""

    kinds: set[AffinityKind] = set()
    if left.handle_values & right.handle_values:
        kinds.add(AffinityKind.handle)
    if (left.conversations & right.conversations) - discounted_conversations:
        kinds.add(AffinityKind.evidence)
    shared_tokens = left.title_tokens & right.title_tokens
    strong_title = len(shared_tokens) >= 2
    weak_title = not strong_title and any(
        _is_distinctive_token(token) for token in shared_tokens
    )
    if strong_title or weak_title:
        kinds.add(AffinityKind.title)
    weak = weak_title and AffinityKind.handle not in kinds
    return frozenset(kinds), weak


_DISTINCTIVE_TOKEN = re.compile(r"^[a-z][a-z0-9_+-]{3,}$")


def _is_distinctive_token(token: str) -> bool:
    """A lone shared latin technical identifier (hook, console) still bonds."""

    return bool(_DISTINCTIVE_TOKEN.match(token))


def title_affinity_tokens(title: str) -> frozenset[str]:
    """Normalized word tokens plus CJK character bigrams (spec 28.1)."""

    tokens: set[str] = set()
    for word in normalize_title(title).split():
        tokens.add(word)
        runs = re.findall(r"[㐀-鿿]{2,}", word)
        for run in runs:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(tokens)


def _subject_source_refs(assertions: list[Assertion]) -> tuple[SourceRef, ...]:
    by_id: dict[str, SourceRef] = {}
    for assertion in assertions:
        for ref in assertion.source_refs:
            by_id.setdefault(ref.source_id, ref)
    return tuple(by_id[key] for key in sorted(by_id, key=lambda value: value.encode("utf-8")))


def _theme_subject_key(scope_id: str, title: str, members: list[str] | tuple[str, ...]) -> str:
    return "sub_" + stable_hash(
        [scope_id, "theme", normalize_title(title), sorted(members)]
    )[:20]


def _latest_schedule_instant(state: ThemeScheduleState | None) -> datetime | None:
    if state is None:
        return None
    values = [item for item in (state.last_enqueued_at, state.last_run_at) if item]
    return max(values, default=None)


def _env_value(name: str, explicit: Any) -> Any:
    return os.environ.get(name, explicit)


def _env_number(
    name: str,
    explicit: Any,
    default: float,
    cast: type[int | float],
) -> Any:
    raw = _env_value(name, explicit)
    if raw is None:
        raw = default
    if isinstance(raw, bool):
        raise TypeError(f"{name} MUST be numeric")
    if cast is int and isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f"{name} MUST be an integer")
    try:
        return cast(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} MUST be numeric") from error


__all__ = [
    "AffinityEdge",
    "AffinityKind",
    "ThemeCandidate",
    "ThemeCluster",
    "ThemeNamingSession",
    "ThemePassReport",
    "ThemeSettings",
    "cluster_themes",
    "configured_theme_settings",
    "enqueue_theme_pass_if_due",
    "run_theme_pass",
    "snapshot_theme_state",
]
