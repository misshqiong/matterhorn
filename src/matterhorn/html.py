"""Deterministic, self-contained HTML rendering for scope export snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from matterhorn.contracts import (
    Assertion,
    Cardinality,
    ExportEnvelope,
    Interval,
    Operation,
    Origin,
    SchemaProfile,
    SourceRef,
)
from matterhorn.engine.canonical import as_utc, canonical_json, instant_text
from matterhorn.engine.extractor import FIELD_WIDE_RETRACT
from matterhorn.engine.projector import project_assertions

SourceLinkResolver = Callable[[SourceRef, str], tuple[str, bool]]


@dataclass(frozen=True)
class EvidenceLink:
    label: str
    href: str
    external: bool


@dataclass(frozen=True)
class TimelineEntry:
    instant: str
    date: str
    kind: str
    text: str
    evidence: list[EvidenceLink]
    human: bool
    sort_key: tuple[str, int, bytes, bytes]


@dataclass(frozen=True)
class FieldValue:
    value: Any
    display: str
    human: bool


@dataclass(frozen=True)
class MatterView:
    subject_key: str
    title: str
    status: FieldValue
    status_class: str
    owners: FieldValue
    participants: FieldValue
    blockers: FieldValue
    next_step: FieldValue
    due: FieldValue
    overdue: bool
    open: bool
    timeline: list[TimelineEntry]


@dataclass(frozen=True)
class CommitmentView:
    subject_key: str
    title: str
    due: str
    due_instant: str
    overdue: bool
    owners: str
    next_step: str


@dataclass(frozen=True)
class PersonView:
    person: str
    in_progress: int
    blocked: int


@dataclass(frozen=True)
class SourceAppendix:
    anchor_id: str
    source_id: str
    subject: str
    sender: str
    sent_at: str
    recipients: str | None
    body: str
    email: bool


def render_scope_html(
    snapshot: ExportEnvelope | dict[str, Any],
    profile: SchemaProfile,
    *,
    as_of: datetime | str | None = None,
    source_link_resolvers: Mapping[str, SourceLinkResolver] | None = None,
    related: list[tuple[str, str]] | None = None,
) -> str:
    """Render one byte-stable HTML document from an export envelope.

    Link handling is selected by the source-id namespace before falling back
    to URI or generic in-page evidence. Hosts can replace a namespace resolver
    without changing the template or the export model.
    """

    envelope = (
        snapshot
        if isinstance(snapshot, ExportEnvelope)
        else ExportEnvelope.model_validate(snapshot)
    )
    resolved_as_of, default_as_of = _resolve_as_of(envelope.assertions, as_of)
    intervals, _ = project_assertions(envelope.assertions, profile)
    assertions_by_id = {
        assertion.assertion_id: assertion for assertion in envelope.assertions
    }
    sources = _source_refs(envelope)
    source_states = {
        item.source_id: item for item in envelope.source_states
    }
    resolvers = {
        "email": _email_link,
        **(source_link_resolvers or {}),
    }
    in_page_sources: set[str] = {
        source_id
        for source_id in sources
        if _source_kind(source_id) == "email"
    }

    def evidence_links(refs: list[SourceRef]) -> list[EvidenceLink]:
        links: list[EvidenceLink] = []
        for ref in sorted(
            refs,
            key=lambda item: (
                item.sent_at,
                item.source_id.encode("utf-8"),
            ),
        ):
            state = source_states.get(ref.source_id)
            uri = (
                state.uri
                if state is not None and state.uri is not None
                else ref.uri
            )
            effective = ref.model_copy(update={"uri": uri})
            kind = _source_kind(ref.source_id)
            resolver = resolvers.get(kind)
            if resolver is not None:
                href, external = resolver(effective, _anchor_id(ref.source_id))
            elif uri is not None:
                href, external = uri, True
            else:
                href, external = f"#{_anchor_id(ref.source_id)}", False
            if not external:
                in_page_sources.add(ref.source_id)
            links.append(
                EvidenceLink(
                    label=ref.source_id,
                    href=href,
                    external=external,
                )
            )
        return links

    subjects = {
        subject.subject_key: subject for subject in envelope.subjects
    }
    children: dict[str, list[str]] = {}
    for subject in envelope.subjects:
        if subject.parent_subject_key is not None:
            children.setdefault(subject.parent_subject_key, []).append(
                subject.subject_key
            )
    current_by_subject = _current_intervals(intervals)
    completed_values = (
        {
            canonical_json(value)
            for value in profile.completion.completed_values
        }
        if profile.completion is not None
        else set()
    )
    matters: list[MatterView] = []
    for subject in sorted(
        (
            item
            for item in envelope.subjects
            if item.subject_type == profile.primary_subject.type
        ),
        key=lambda item: item.subject_key.encode("utf-8"),
    ):
        current = current_by_subject.get(subject.subject_key, {})
        status = _field_value(
            current,
            profile,
            "status",
            assertions_by_id,
        )
        owners = _field_value(
            current,
            profile,
            "participants",
            assertions_by_id,
            role="owner",
        )
        participants = _field_value(
            current,
            profile,
            "participants",
            assertions_by_id,
            role="participant",
        )
        blockers = _field_value(
            current,
            profile,
            "blocker",
            assertions_by_id,
        )
        next_step = _field_value(
            current,
            profile,
            "next_step",
            assertions_by_id,
        )
        due = _field_value(
            current,
            profile,
            "due",
            assertions_by_id,
        )
        due_instant = _datetime_value(due.value)
        status_key = canonical_json(status.value)
        is_open = not completed_values or status_key not in completed_values
        matter_subjects = {
            subject.subject_key,
            *children.get(subject.subject_key, []),
        }
        timeline = _timeline(
            matter_subjects,
            subjects,
            intervals,
            envelope.assertions,
            profile,
            assertions_by_id,
            evidence_links,
        )
        matters.append(
            MatterView(
                subject_key=subject.subject_key,
                title=subject.title,
                status=status,
                status_class=_status_class(status.value),
                owners=owners,
                participants=participants,
                blockers=blockers,
                next_step=next_step,
                due=due,
                overdue=bool(
                    is_open
                    and due_instant is not None
                    and due_instant < resolved_as_of
                ),
                open=is_open,
                timeline=timeline,
            )
        )

    commitments = sorted(
        (
            CommitmentView(
                subject_key=matter.subject_key,
                title=matter.title,
                due=matter.due.display,
                due_instant=instant_text(_datetime_value(matter.due.value)),
                overdue=matter.overdue,
                owners=matter.owners.display,
                next_step=matter.next_step.display,
            )
            for matter in matters
            if matter.open and _datetime_value(matter.due.value) is not None
        ),
        key=lambda item: (
            item.due_instant,
            item.subject_key.encode("utf-8"),
        ),
    )
    people = _people(matters)
    appendix = [
        _source_appendix(sources[source_id])
        for source_id in sorted(
            in_page_sources,
            key=lambda item: (
                sources[item].sent_at,
                item.encode("utf-8"),
            ),
        )
    ]
    template_root = files("matterhorn").joinpath("templates")
    environment = Environment(
        loader=FileSystemLoader(str(template_root)),
        autoescape=select_autoescape(("html", "xml")),
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    template = environment.get_template("scope.html.j2")
    return template.render(
        scope_id=envelope.scope_id,
        as_of_date=resolved_as_of.date().isoformat(),
        as_of_instant=instant_text(resolved_as_of),
        default_as_of=default_as_of,
        assertion_count=len(envelope.assertions),
        commitments=commitments,
        people=people,
        matters=matters,
        appendix=appendix,
        related_links=[
            {"label": label, "href": href} for label, href in (related or [])
        ],
    )


def _resolve_as_of(
    assertions: list[Assertion],
    value: datetime | str | None,
) -> tuple[datetime, bool]:
    if value is None:
        if not assertions:
            raise ValueError(
                "HTML export requires --as-of when the scope has no assertions"
            )
        return max(as_utc(item.recorded_at) for item in assertions), True
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return as_utc(parsed), False


def _source_refs(envelope: ExportEnvelope) -> dict[str, SourceRef]:
    result: dict[str, SourceRef] = {}
    for assertion in sorted(
        envelope.assertions,
        key=lambda item: (
            item.valid_from,
            item.recorded_at,
            item.assertion_id.encode("utf-8"),
        ),
    ):
        for ref in assertion.source_refs:
            result.setdefault(ref.source_id, ref)
    return result


def _current_intervals(
    intervals: list[Interval],
) -> dict[str, dict[str, list[Interval]]]:
    result: dict[str, dict[str, list[Interval]]] = {}
    for interval in intervals:
        if interval.valid_to is not None:
            continue
        result.setdefault(interval.subject_key, {}).setdefault(
            interval.predicate, []
        ).append(interval)
    for current in result.values():
        for values in current.values():
            values.sort(
                key=lambda item: (
                    item.object_key.encode("utf-8"),
                    item.assertion_id.encode("utf-8"),
                )
            )
    return result


def _field_value(
    current: dict[str, list[Interval]],
    profile: SchemaProfile,
    source_field: str,
    assertions_by_id: dict[str, Assertion],
    *,
    role: str | None = None,
) -> FieldValue:
    definitions = [
        item
        for item in profile.predicates
        if item.source_field == source_field
        and (
            role is None
            or role in item.role_filter
            or (role == "participant" and not item.role_filter)
        )
    ]
    if role == "participant":
        unrestricted = [item for item in definitions if not item.role_filter]
        definitions = unrestricted or definitions
    elif role == "owner":
        restricted = [item for item in definitions if role in item.role_filter]
        definitions = restricted
    selected: list[Interval] = []
    for definition in definitions:
        selected.extend(current.get(definition.name, []))
    selected.sort(
        key=lambda item: (
            item.object_key.encode("utf-8"),
            item.assertion_id.encode("utf-8"),
        )
    )
    if not selected:
        return FieldValue(value=None, display="—", human=False)
    is_collection = any(
        profile.predicate(item.predicate).cardinality == Cardinality.SET
        for item in selected
    )
    value: Any = (
        [item.object_value for item in selected]
        if is_collection
        else selected[-1].object_value
    )
    human = any(
        assertions_by_id[assertion_id].origin == Origin.human
        for interval in selected
        for assertion_id in interval.supporting_assertion_ids
    )
    display = (
        _display_due(value)
        if source_field == "due"
        else _display(value)
    )
    return FieldValue(value=value, display=display, human=human)


def _timeline(
    matter_subjects: set[str],
    subjects: Mapping[str, Any],
    intervals: list[Interval],
    assertions: list[Assertion],
    profile: SchemaProfile,
    assertions_by_id: dict[str, Assertion],
    evidence_links: Callable[[list[SourceRef]], list[EvidenceLink]],
) -> list[TimelineEntry]:
    relevant = [
        item for item in intervals if item.subject_key in matter_subjects
    ]
    entries: list[TimelineEntry] = []
    category_order = {
        "status": 0,
        "due": 1,
        "owner": 2,
        "blocker": 3,
        "decision": 4,
    }
    previous_single: dict[tuple[str, str], Any] = {}
    for interval in sorted(
        relevant,
        key=lambda item: (
            item.valid_from,
            item.predicate.encode("utf-8"),
            item.object_key.encode("utf-8"),
            item.assertion_id.encode("utf-8"),
        ),
    ):
        definition = profile.predicate(interval.predicate)
        kind: str | None = None
        text: str | None = None
        if definition.source_field == "status":
            kind = "status"
            old = previous_single.get(
                (interval.subject_key, interval.predicate)
            )
            text = (
                f"Status set to {_display(interval.object_value)}"
                if old is None
                else f"Status changed from {_display(old)} to "
                f"{_display(interval.object_value)}"
            )
            previous_single[(interval.subject_key, interval.predicate)] = (
                interval.object_value
            )
        elif definition.source_field == "due":
            kind = "due"
            old = previous_single.get(
                (interval.subject_key, interval.predicate)
            )
            text = (
                f"Due set to {_display_due(interval.object_value)}"
                if old is None
                else f"Due changed from {_display_due(old)} to "
                f"{_display_due(interval.object_value)}"
            )
            previous_single[(interval.subject_key, interval.predicate)] = (
                interval.object_value
            )
        elif (
            definition.source_field == "participants"
            and "owner" in definition.role_filter
        ):
            kind = "owner"
            text = f"Owner assigned: {_display(interval.object_value)}"
        elif definition.source_field == "blocker":
            kind = "blocker"
            text = f"Blocker opened: {_display(interval.object_value)}"
        elif definition.extraction.value == "semantic":
            kind = "decision"
            title = subjects[interval.subject_key].title
            verb = (
                "adopted"
                if interval.object_value is not False
                else "reversed"
            )
            text = f"Decision {verb}: {title}"
        elif definition.source_field == "outcome":
            outcome = interval.object_value
            if (
                isinstance(outcome, dict)
                and str(outcome.get("type", "")).casefold() == "decision"
            ):
                kind = "decision"
                text = f"Decision: {_display(outcome.get('content'))}"
        if kind is not None and text is not None:
            entries.append(
                _entry(
                    interval.valid_from,
                    kind,
                    text,
                    interval.source_refs,
                    _interval_is_human(interval, assertions_by_id),
                    category_order[kind],
                    interval.subject_key,
                    interval.assertion_id,
                    evidence_links,
                )
            )
        if interval.valid_to is not None and kind in {"owner", "blocker"}:
            closing = _closing_assertion(interval, assertions)
            closing_refs = (
                closing.source_refs
                if closing is not None
                else interval.source_refs
            )
            closing_text = (
                f"Owner released: {_display(interval.object_value)}"
                if kind == "owner"
                else f"Blocker closed: {_display(interval.object_value)}"
            )
            entries.append(
                _entry(
                    interval.valid_to,
                    kind,
                    closing_text,
                    closing_refs,
                    closing is not None and closing.origin == Origin.human,
                    category_order[kind],
                    interval.subject_key,
                    closing.assertion_id if closing else interval.assertion_id,
                    evidence_links,
                )
            )
    return sorted(entries, key=lambda item: item.sort_key)


def _entry(
    instant: datetime,
    kind: str,
    text: str,
    refs: list[SourceRef],
    human: bool,
    category_order: int,
    subject_key: str,
    unique_id: str,
    evidence_links: Callable[[list[SourceRef]], list[EvidenceLink]],
) -> TimelineEntry:
    normalized = instant_text(instant)
    return TimelineEntry(
        instant=normalized,
        date=normalized[:10],
        kind=kind,
        text=text,
        evidence=evidence_links(refs),
        human=human,
        sort_key=(
            normalized,
            category_order,
            subject_key.encode("utf-8"),
            unique_id.encode("utf-8"),
        ),
    )


def _closing_assertion(
    interval: Interval,
    assertions: list[Assertion],
) -> Assertion | None:
    matches = [
        item
        for item in assertions
        if item.subject_key == interval.subject_key
        and item.predicate == interval.predicate
        and item.valid_from == interval.valid_to
        and item.operation == Operation.RETRACT
        and item.object_key in {interval.object_key, FIELD_WIDE_RETRACT}
    ]
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            item.valid_from,
            1 if item.origin == Origin.human else 0,
            item.recorded_at,
            item.assertion_id.encode("utf-8"),
        ),
    )


def _interval_is_human(
    interval: Interval,
    assertions_by_id: Mapping[str, Assertion],
) -> bool:
    return any(
        assertions_by_id[item].origin == Origin.human
        for item in interval.supporting_assertion_ids
    )


def _people(matters: list[MatterView]) -> list[PersonView]:
    counts: dict[str, list[int]] = {}
    for matter in matters:
        if not matter.open:
            continue
        people = {
            str(value)
            for value in [
                *_list_value(matter.owners.value),
                *_list_value(matter.participants.value),
            ]
        }
        blocked = bool(_list_value(matter.blockers.value)) or (
            str(matter.status.value).casefold() == "blocked"
        )
        for person in people:
            values = counts.setdefault(person, [0, 0])
            values[1 if blocked else 0] += 1
    return [
        PersonView(
            person=person,
            in_progress=values[0],
            blocked=values[1],
        )
        for person, values in sorted(
            counts.items(), key=lambda item: item[0].encode("utf-8")
        )
    ]


def _source_appendix(ref: SourceRef) -> SourceAppendix:
    is_email = _source_kind(ref.source_id) == "email"
    subject, recipients, body = _split_email_excerpt(ref.excerpt or "")
    return SourceAppendix(
        anchor_id=_anchor_id(ref.source_id),
        source_id=ref.source_id,
        subject=subject if is_email else ref.source_id,
        sender=ref.sender,
        sent_at=instant_text(ref.sent_at),
        recipients=recipients,
        body=body,
        email=is_email,
    )


def _split_email_excerpt(value: str) -> tuple[str, str | None, str]:
    head, separator, body = value.partition("\n\n")
    headers: dict[str, str] = {}
    for line in head.splitlines():
        name, marker, content = line.partition(":")
        if marker and name in {"Subject", "To", "Cc"}:
            headers[name] = content.strip()
    recipients = ", ".join(
        item for item in [headers.get("To"), headers.get("Cc")] if item
    )
    if "Subject" not in headers:
        return "(source email)", None, value
    return (
        headers["Subject"] or "(no subject)",
        recipients or None,
        body if separator else "",
    )


def _email_link(ref: SourceRef, anchor_id: str) -> tuple[str, bool]:
    del ref
    return f"#{anchor_id}", False


def _source_kind(source_id: str) -> str:
    namespace, marker, _ = source_id.partition(":")
    return namespace if marker else "generic"


def _anchor_id(source_id: str) -> str:
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:20]
    return f"source-{_source_kind(source_id)}-{digest}"


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, str):
        try:
            return as_utc(datetime.fromisoformat(value))
        except ValueError:
            return None
    return None


def _display(value: Any) -> str:
    if value is None or value == []:
        return "—"
    if isinstance(value, datetime):
        return instant_text(value)[:10]
    if isinstance(value, list):
        return ", ".join(_display(item) for item in value)
    if isinstance(value, dict):
        return canonical_json(value)
    return str(value)


def _display_due(value: Any) -> str:
    parsed = _datetime_value(value)
    return parsed.date().isoformat() if parsed is not None else _display(value)


def _list_value(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _status_class(value: Any) -> str:
    normalized = str(value or "").casefold()
    if normalized in {"blocked", "paused"}:
        return "blocked"
    if normalized in {"done", "completed", "closed"}:
        return "done"
    if normalized in {"in_progress", "ready"}:
        return "progress"
    if normalized in {"open", "pending"}:
        return "open"
    return "neutral"
