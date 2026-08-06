from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
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
    FIELD_WIDE_RETRACT,
    Assertion,
    EpisodeCard,
    Operation,
    Origin,
    Record,
    SourceRef,
    SubjectRecord,
)
from matterhorn.contracts.models import StrictModel
from matterhorn.distill import ToolLoopGateway, ToolLoopResult
from matterhorn.distill.traceability import source_aliases
from matterhorn.engine.goal_graph import structure_rejection

MAX_TOOL_CALLS = 16
MAX_EMISSIONS = 4
NEW_SUBJECT_PREFIX = "$new:"


class NewSubjectDeclaration(StrictModel):
    ref: str = Field(min_length=1)
    subject_type: str = Field(min_length=1)
    title: str = Field(min_length=1)


class IntentSubject(StrictModel):
    subject_key: str | None = None
    new_subject: NewSubjectDeclaration | None = None

    @model_validator(mode="after")
    def exactly_one_reference(self) -> IntentSubject:
        if (self.subject_key is None) == (self.new_subject is None):
            raise ValueError("subject requires exactly one existing or new reference")
        return self


class AssertionIntent(StrictModel):
    subject: IntentSubject
    predicate: str
    operation: Operation
    object_value: Any = None
    evidence_aliases: list[str] = Field(min_length=1)
    valid_from: datetime | None = None

    @model_validator(mode="after")
    def evidence_is_unique(self) -> AssertionIntent:
        if len(self.evidence_aliases) != len(set(self.evidence_aliases)):
            raise ValueError("evidence_aliases MUST be unique")
        return self


class EmitArguments(StrictModel):
    assertions: list[AssertionIntent]


@dataclass(frozen=True)
class UnifiedLoopReport:
    assertion_ids: list[str]
    new_subjects: int
    rejection_counts: dict[str, int]
    unchanged_dropped: int
    tool_calls: int
    emissions: int
    exhausted: bool


@dataclass
class _EmissionState:
    assertion_ids: list[str] = field(default_factory=list)
    new_subjects: int = 0
    rejection_counts: dict[str, int] = field(default_factory=dict)
    unchanged_dropped: int = 0
    assertion_aliases: dict[str, list[SourceRef]] = field(default_factory=dict)


class UnifiedLoopSession:
    """One closed-world, bounded distillation session for one Record chunk."""

    def __init__(
        self,
        *,
        engine: Any,
        scope_id: str,
        records: list[Record],
        context: list[Record],
        samples_dir: str | Path | None = None,
    ) -> None:
        self.engine = engine
        self.scope_id = scope_id
        self.records = list(records)
        self.context = list(context)
        self.window = [*self.context, *self.records]
        aliases = source_aliases(record.record_id for record in self.window)
        by_id = {record.record_id: record.to_source_ref() for record in self.window}
        self.evidence = {alias: by_id[source_id] for alias, source_id in aliases.items()}
        self.closed_world: set[str] = set()
        self.state = _EmissionState()
        self.source_kind = _source_kind(self.records)
        self.samples_dir = Path(samples_dir) if samples_dir is not None else None

    def run(self, gateway: Any) -> UnifiedLoopReport:
        if not isinstance(gateway, ToolLoopGateway):
            raise TypeError(
                "unified distillation requires a tool-loop capable gateway"
            )
        prompt = self._prompt()
        result = gateway.tool_loop(
            system=prompt["system"],
            user=prompt["user"],
            tools=tool_definitions(),
            handler=self.handle_tool,
            max_tool_calls=MAX_TOOL_CALLS,
            max_emissions=MAX_EMISSIONS,
        )
        if not isinstance(result, ToolLoopResult):
            raise TypeError("tool_loop MUST return ToolLoopResult")
        return UnifiedLoopReport(
            assertion_ids=list(self.state.assertion_ids),
            new_subjects=self.state.new_subjects,
            rejection_counts=dict(sorted(self.state.rejection_counts.items())),
            unchanged_dropped=self.state.unchanged_dropped,
            tool_calls=result.tool_calls,
            emissions=result.emissions,
            exhausted=result.exhausted,
        )

    def handle_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "read_neighborhood":
            keys = arguments.get("subject_keys")
            if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
                return {"error": "subject_keys MUST be an array of strings"}
            return {"subjects": self._read_neighborhood(keys)}
        if name == "search_candidates":
            text = arguments.get("text")
            if not isinstance(text, str):
                return {"error": "text MUST be a string"}
            return {"subjects": self._search_candidates(text)}
        if name == "emit":
            return self._emit(arguments)
        return {"error": f"unknown tool {name!r}"}

    def _prompt(self) -> dict[str, str]:
        samples = alignment_samples(
            self.source_kind,
            samples_dir=self.samples_dir,
        )
        predicates = [
            {
                "name": item.name,
                "subject_type": item.subject,
                "object_type": item.object,
                "value_domain": item.value_domain,
            }
            for item in self.engine.profile.predicates
        ]
        system = (
            "You distill one evidence window into assertion intents. Use only "
            "read_neighborhood, search_candidates, and emit. Existing subjects "
            "may be referenced only after a read/search returned them. New "
            "subjects use a stable declaration ref and may be referenced inside "
            "that same emission as '$new:<ref>'. Cite only supplied m1, m2, ... "
            "evidence aliases, or assertion aliases returned by an earlier emit. "
            "Do not invent people: person-valued assertions use exact author ids. "
            "Noise requires no emit call. Stop when the window is fully handled.\n"
            f"registered_predicates={canonical_json(predicates)}\n"
            f"alignment_samples={canonical_json(samples)}"
        )
        aliases_by_id = {ref.source_id: alias for alias, ref in self.evidence.items()}
        deterministic_keys = self.engine._unified_pre_route_keys(
            self.scope_id,
            self.records,
            self.context,
        )
        deterministic_routes = self._read_neighborhood(deterministic_keys)
        user = canonical_json(
            {
                "scope_id": self.scope_id,
                "source_kind": self.source_kind,
                "deterministic_pre_routes": deterministic_routes,
                "context": [
                    _record_payload(record, aliases_by_id[record.record_id])
                    for record in self.context
                ],
                "records": [
                    _record_payload(record, aliases_by_id[record.record_id])
                    for record in self.records
                ],
            }
        )
        return {"system": system, "user": user}

    def _read_neighborhood(self, requested: list[str]) -> list[dict[str, Any]]:
        subjects = {
            subject.subject_key: subject
            for subject in self.engine.store.subjects(self.scope_id)
        }
        result = []
        for key in requested:
            canonical = self.engine.canonical_subject_key(self.scope_id, key)
            if canonical not in subjects:
                result.append({"subject_key": key, "error": "UNKNOWN_SUBJECT"})
                continue
            self.closed_world.add(canonical)
            result.append(self._subject_payload(canonical))
        return result

    def _search_candidates(self, text: str) -> list[dict[str, Any]]:
        if not text.strip():
            return []
        card = self._search_card(text)
        with self.engine.store.transaction():
            subjects = self.engine.store.subjects(self.scope_id)
            merges = self.engine.store.subject_merges(self.scope_id)
            canonical = self.engine._canonical_subjects_for_loop(subjects, merges)
            recalled = self.engine._routing_candidates(card, canonical, subjects, merges)
            handle_targets = self.engine._handle_route_targets(
                card,
                {record.record_id: record for record in self.window},
                self.engine._merge_edges_for_loop(merges),
            )
        keys = set(handle_targets)
        keys.update(item.subject_key for item in recalled)
        ordered = sorted(keys, key=lambda key: key.encode("utf-8"))[:5]
        self.closed_world.update(ordered)
        return [self._subject_payload(key) for key in ordered]

    def _subject_payload(self, subject_key: str) -> dict[str, Any]:
        subjects = {
            subject.subject_key: subject
            for subject in self.engine.store.subjects(self.scope_id)
        }
        subject = subjects[subject_key]
        current_by_key = {
            card.subject_key: card.current
            for card in self.engine.store.memory_cards(self.scope_id)
        }
        handles = [
            f"{item.handle_type}:{item.handle_value}"
            for item in self.engine.store.active_subject_handles(self.scope_id)
            if self.engine.canonical_subject_key(self.scope_id, item.subject_key)
            == subject_key
        ]
        projection = self.engine._goal_projection(self.scope_id)
        node = projection.nodes.get(subject_key)
        parent_chain: list[dict[str, Any]] = []
        current = subject_key
        visited = {current}
        while current in projection.parents:
            parent = projection.parents[current]
            if parent in visited or parent not in projection.nodes:
                break
            parent_chain.append(projection.nodes[parent].to_dict())
            visited.add(parent)
            current = parent
        siblings = []
        parent = projection.parents.get(subject_key)
        if parent is not None:
            siblings = [
                projection.nodes[key].to_dict()
                for key in projection.children.get(parent, [])
                if key != subject_key
            ]
        return {
            "subject_key": subject_key,
            "subject_type": subject.subject_type,
            "title": subject.title,
            "current": current_by_key.get(subject_key, {}),
            "handles": sorted(handles, key=lambda value: value.encode("utf-8")),
            "neighborhood": {
                "node": node.to_dict() if node is not None else None,
                "parent_chain": parent_chain,
                "children": [
                    projection.nodes[key].to_dict()
                    for key in projection.children.get(subject_key, [])
                ],
                "siblings": siblings,
            },
        }

    def _search_card(self, text: str) -> EpisodeCard:
        refs = [record.to_source_ref() for record in self.window]
        sent_at = min((ref.sent_at for ref in refs), default=datetime.now(UTC))
        return EpisodeCard(
            card_id="loop_search_" + stable_hash(
                [self.scope_id, [record.record_id for record in self.records], text]
            ),
            scope_id=self.scope_id,
            date=as_utc(sent_at).date(),
            title=text,
            source_refs=refs or [
                SourceRef(
                    source_id="loop:empty",
                    sent_at=sent_at,
                    sender="system",
                )
            ],
        )

    def _emit(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            batch = EmitArguments.model_validate(raw)
        except ValidationError as error:
            self._record_rejection("UNPARSEABLE")
            return {
                "accepted": [],
                "rejected": [{"reason": "UNPARSEABLE", "detail": str(error)}],
            }
        accepted_results: list[dict[str, Any]] = []
        rejected_results: list[dict[str, Any]] = []
        unchanged_results: list[dict[str, Any]] = []
        inserted_assertion_ids: list[str] = []
        emitted_aliases: dict[str, list[SourceRef]] = {}
        committed_rejections: dict[str, int] = {}
        committed_unchanged = 0
        committed_new_subjects = 0
        with self.engine.store.transaction():
            subjects = self.engine.store.subjects(self.scope_id)
            subjects_by_key = {subject.subject_key: subject for subject in subjects}
            assertions = self.engine.store.assertions(self.scope_id)
            merges = self.engine.store.subject_merges(self.scope_id)
            new_declarations: dict[str, SubjectRecord] = {}
            proposed: list[tuple[int, Assertion, SubjectRecord | None]] = []
            rejections: dict[str, int] = {}
            unchanged = 0
            for index, intent in enumerate(batch.assertions):
                outcome = self._validate_intent(
                    intent,
                    index=index,
                    subjects=[*subjects, *new_declarations.values()],
                    subjects_by_key={**subjects_by_key, **new_declarations},
                    assertions=[*assertions, *(item[1] for item in proposed)],
                    merges=merges,
                    new_declarations=new_declarations,
                )
                if isinstance(outcome, str):
                    rejections[outcome] = rejections.get(outcome, 0) + 1
                    rejected_results.append({"index": index, "reason": outcome})
                    continue
                assertion, created = outcome
                if self.engine._model_assertion_is_unchanged(
                    assertion,
                    [*assertions, *(item[1] for item in proposed)],
                ):
                    unchanged += 1
                    unchanged_results.append({"index": index, "reason": "UNCHANGED"})
                    continue
                proposed.append((index, assertion, created))

            created_keys: set[str] = set()
            for index, assertion, created in proposed:
                if created is not None and created.subject_key not in subjects_by_key:
                    refs = frozenset(ref.source_id for ref in assertion.source_refs)
                    created = SubjectRecord(
                        scope_id=created.scope_id,
                        subject_key=created.subject_key,
                        subject_type=created.subject_type,
                        title=created.title,
                        normalized_title=created.normalized_title,
                        source_ids=refs,
                        parent_subject_key=created.parent_subject_key,
                        thread_ids=created.thread_ids,
                    )
                    self.engine.store.upsert_subject(created)
                    subjects_by_key[created.subject_key] = created
                    created_keys.add(created.subject_key)
                elif assertion.subject_key in subjects_by_key:
                    current = subjects_by_key[assertion.subject_key]
                    refs = frozenset(ref.source_id for ref in assertion.source_refs)
                    updated = SubjectRecord(
                        scope_id=current.scope_id,
                        subject_key=current.subject_key,
                        subject_type=current.subject_type,
                        title=current.title,
                        normalized_title=current.normalized_title,
                        source_ids=current.source_ids | refs,
                        parent_subject_key=current.parent_subject_key,
                        thread_ids=current.thread_ids,
                    )
                    self.engine.store.upsert_subject(updated)
                    subjects_by_key[current.subject_key] = updated
                assertion = assertion.model_copy(
                    update={"recorded_at": self.engine.now()}
                )
                inserted = self.engine.store.add_assertion(assertion)
                alias = (
                    f"a{len(self.state.assertion_aliases) + len(emitted_aliases) + 1}"
                )
                emitted_aliases[alias] = assertion.source_refs
                if inserted:
                    inserted_assertion_ids.append(assertion.assertion_id)
                accepted_results.append(
                    {
                        "index": index,
                        "assertion_id": assertion.assertion_id,
                        "assertion_alias": alias,
                        "subject_key": assertion.subject_key,
                        "inserted": inserted,
                    }
                )
            self.engine.store.record_gate_report(
                self.scope_id,
                accepted=len(proposed),
                rejections=rejections,
                unchanged_dropped=unchanged,
            )
            if proposed:
                self.engine._rebuild(self.scope_id)
            committed_rejections = rejections
            committed_unchanged = unchanged
            committed_new_subjects = len(created_keys)
        self.state.assertion_ids.extend(inserted_assertion_ids)
        self.state.assertion_aliases.update(emitted_aliases)
        self.state.new_subjects += committed_new_subjects
        self.state.unchanged_dropped += committed_unchanged
        for reason, count in committed_rejections.items():
            self.state.rejection_counts[reason] = (
                self.state.rejection_counts.get(reason, 0) + count
            )
        return {
            "accepted": accepted_results,
            "rejected": rejected_results,
            "unchanged": unchanged_results,
        }

    def _validate_intent(
        self,
        intent: AssertionIntent,
        *,
        index: int,
        subjects: list[SubjectRecord],
        subjects_by_key: dict[str, SubjectRecord],
        assertions: list[Assertion],
        merges: list[Any],
        new_declarations: dict[str, SubjectRecord],
    ) -> tuple[Assertion, SubjectRecord | None] | str:
        del index
        created: SubjectRecord | None = None
        if intent.subject.new_subject is not None:
            declaration = intent.subject.new_subject
            if declaration.subject_type != self.engine.profile.primary_subject.type:
                return "INVALID_SUBJECT_PARENT"
            normalized = normalize_title(declaration.title)
            if not normalized:
                return "MISSING_SUBJECT_TITLE"
            created = self._declared_subject(declaration)
            prior = new_declarations.get(declaration.ref)
            if prior is not None and prior != created:
                return "NEW_SUBJECT_REF_CONFLICT"
            new_declarations[declaration.ref] = created
            subject = subjects_by_key.get(created.subject_key, created)
        else:
            assert intent.subject.subject_key is not None
            requested = intent.subject.subject_key
            if requested.startswith(NEW_SUBJECT_PREFIX):
                subject = new_declarations.get(requested[len(NEW_SUBJECT_PREFIX) :])
                if subject is None:
                    return "CLOSED_WORLD_VIOLATION"
            else:
                canonical = self.engine.canonical_subject_key(
                    self.scope_id,
                    requested,
                )
                if canonical not in self.closed_world:
                    return "CLOSED_WORLD_VIOLATION"
                subject = subjects_by_key.get(canonical)
                if subject is None:
                    return "UNKNOWN_SUBJECT"

        try:
            definition = self.engine.profile.predicate(intent.predicate)
        except ValueError:
            return "UNREGISTERED_PREDICATE"
        if definition.subject != subject.subject_type:
            return "SUBJECT_TYPE_MISMATCH"
        source_refs = self._resolve_evidence(intent.evidence_aliases)
        if source_refs is None:
            return "SOURCE_NOT_TRACEABLE"
        if not source_refs:
            return "NO_SOURCES"
        value = intent.object_value
        if definition.object == "subject" and isinstance(value, str):
            if value.startswith(NEW_SUBJECT_PREFIX):
                ref = value[len(NEW_SUBJECT_PREFIX) :]
                target = new_declarations.get(ref)
                if target is None:
                    return "CLOSED_WORLD_VIOLATION"
                value = target.subject_key
            else:
                value = self.engine.canonical_subject_key(self.scope_id, value)
                if value not in self.closed_world:
                    return "CLOSED_WORLD_VIOLATION"
        if (
            intent.operation == Operation.ASSERT
            and definition.value_domain is not None
            and canonical_json(value)
            not in {canonical_json(item) for item in definition.value_domain}
        ):
            return "VALUE_OUT_OF_DOMAIN"
        if definition.object == "person" and value not in {
            record.author.id for record in self.window
        }:
            return "MACHINE_IDENTITY_VIOLATION"
        valid_from = as_utc(
            intent.valid_from
            or max(ref.sent_at for ref in source_refs)
        )
        window_start = min(as_utc(record.sent_at) for record in self.window)
        window_end = max(as_utc(record.sent_at) for record in self.window)
        if not window_start <= valid_from <= window_end:
            return "VALID_FROM_OUT_OF_WINDOW"
        value_key = (
            object_key(value)
            if intent.operation == Operation.ASSERT or value is not None
            else FIELD_WIDE_RETRACT
        )
        assertion = Assertion(
            assertion_id=derive_assertion_id(
                self.scope_id,
                subject.subject_key,
                intent.predicate,
                intent.operation,
                value_key,
                valid_from,
                source_refs,
            ),
            scope_id=self.scope_id,
            subject_key=subject.subject_key,
            subject_type=subject.subject_type,
            predicate=intent.predicate,
            operation=intent.operation,
            object_value=value,
            object_key=value_key,
            valid_from=valid_from,
            recorded_at=datetime.max.replace(tzinfo=UTC),
            source_refs=source_refs,
            origin=Origin.model,
        )
        outside = isinstance(value, str) and any(
            scope != self.scope_id
            and any(item.subject_key == value for item in self.engine.store.subjects(scope))
            for scope in self.engine.store.list_scopes()
        )
        rejection = structure_rejection(
            assertion,
            profile=self.engine.profile,
            subjects=subjects,
            assertions=assertions,
            merges=merges,
            target_exists_outside_scope=outside,
        )
        if rejection is not None:
            return rejection.value
        return assertion, created

    def _declared_subject(self, declaration: NewSubjectDeclaration) -> SubjectRecord:
        source_ids = frozenset(ref.source_id for ref in self.evidence.values())
        digest = stable_hash(
            [
                self.scope_id,
                declaration.subject_type,
                normalize_title(declaration.title),
                sorted(source_ids),
                [record.record_id for record in self.records],
            ]
        )[:20]
        return SubjectRecord(
            scope_id=self.scope_id,
            subject_key=f"sub_{digest}",
            subject_type=declaration.subject_type,
            title=declaration.title,
            normalized_title=normalize_title(declaration.title),
            source_ids=frozenset(),
            thread_ids=frozenset(
                {
                    record.thread_id
                    for record in self.records
                    if record.thread_id is not None
                }
            ),
        )

    def _resolve_evidence(self, aliases: list[str]) -> list[SourceRef] | None:
        refs: list[SourceRef] = []
        seen: set[str] = set()
        for alias in aliases:
            values = (
                [self.evidence[alias]]
                if alias in self.evidence
                else self.state.assertion_aliases.get(alias)
            )
            if values is None:
                return None
            for ref in values:
                if ref.source_id not in seen:
                    refs.append(ref)
                    seen.add(ref.source_id)
        return refs

    def _record_rejection(self, reason: str) -> None:
        self.state.rejection_counts[reason] = (
            self.state.rejection_counts.get(reason, 0) + 1
        )
        with self.engine.store.transaction():
            self.engine.store.record_gate_report(
                self.scope_id,
                accepted=0,
                rejections={reason: 1},
            )


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_neighborhood",
                "description": "Read projected values, handles, and goal-tree neighbors.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["subject_keys"],
                    "properties": {
                        "subject_keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        }
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_candidates",
                "description": "Recall up to five lexical and exact-handle candidates.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "emit",
                "description": "Validate and atomically commit an assertion-intent batch.",
                "parameters": EmitArguments.model_json_schema(),
            },
        },
    ]


def alignment_samples(
    source_kind: str,
    *,
    samples_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    directory = Path(samples_dir) if samples_dir is not None else _default_samples_dir()
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("source_kind") == source_kind:
            result.append(
                {
                    "sample_id": payload.get("sample_id"),
                    "window": payload.get("window", []),
                    "expected_assertions": payload.get("expected_assertions", []),
                }
            )
    return result


def _default_samples_dir() -> Path:
    source = Path(__file__).resolve().parents[3] / "spec" / "eval" / "samples"
    if source.is_dir():
        return source
    return Path(sys.prefix) / "share" / "matterhorn" / "spec" / "eval" / "samples"


def _record_payload(record: Record, alias: str) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["evidence_alias"] = alias
    return payload


def _source_kind(records: list[Record]) -> str:
    kinds = " ".join(record.kind.casefold() for record in records)
    if "mail" in kinds or "email" in kinds:
        return "mail"
    if "agent" in kinds:
        return "agent"
    return "im"
