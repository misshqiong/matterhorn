"""Deterministic Markdown rendering for the public development ledger."""

from __future__ import annotations

import html
from typing import Any

from matterhorn.canonical import canonical_json

REPRODUCE_COMMAND = (
    "git clone https://github.com/misshqiong/matterhorn && cd matterhorn "
    "&& pip install -e . "
    "&& mh import ledger/assertions.json --db ledger/dev.db "
    "&& mh matters dev --db ledger/dev.db"
)


def render_scope_markdown(engine: Any, scope_id: str) -> str:
    """Render stable primary-matter summaries and interval evidence."""

    human_assertion_ids = {
        assertion.assertion_id
        for assertion in engine.store.assertions(scope_id)
        if assertion.origin == "human"
    }
    lines = [
        "# Development ledger",
        "",
        f"Scope: {_code(scope_id)}",
        "",
    ]
    matters = sorted(
        engine.matters(scope_id),
        key=lambda item: item.subject_key.encode("utf-8"),
    )
    for matter in matters:
        lines.extend(
            [
                f"## {_text(matter.title)}",
                "",
                f"- Status: {_summary_value(matter.status)}",
                f"- Owners: {_summary_value(matter.owners)}",
                f"- Blocked by: {_summary_value(matter.blocked_by)}",
                f"- Next step: {_summary_value(matter.next_step)}",
                f"- Due: {_summary_value(matter.due)}",
                "",
                "<details>",
                "<summary>Timeline</summary>",
                "",
            ]
        )
        changes = []
        for predicate in engine.profile.predicates:
            if predicate.subject != engine.profile.primary_subject.type:
                continue
            changes.extend(
                engine.query.timeline(
                    scope_id,
                    matter.subject_key,
                    predicate.name,
                )
            )
        changes.sort(
            key=lambda item: (
                item.valid_from.encode("utf-8"),
                item.predicate.encode("utf-8"),
                item.assertion_id.encode("utf-8"),
            )
        )
        if not changes:
            lines.append("No interval changes.")
        for change in changes:
            badge = (
                " **[human correction]**"
                if human_assertion_ids.intersection(
                    change.supporting_assertion_ids
                )
                else ""
            )
            lines.append(
                f"- {_code(change.valid_from)} — {_code(change.predicate)} → "
                f"{_code(canonical_json(change.value))}{badge}"
            )
            evidence = ", ".join(
                _evidence(ref.source_id, ref.uri)
                for ref in change.source_refs
            )
            lines.append(f"  - Evidence: {evidence}")
        lines.extend(["", "</details>", ""])
    lines.append(
        f"<!-- generated from assertions; reproduce: {REPRODUCE_COMMAND} -->"
    )
    return "\n".join(lines) + "\n"


def _summary_value(value: Any) -> str:
    if value is None or value == []:
        return "—"
    if isinstance(value, list):
        return ", ".join(_code(_plain(item)) for item in value)
    return _code(_plain(value))


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    return canonical_json(value)


def _code(value: str) -> str:
    return f"<code>{html.escape(value, quote=False)}</code>"


def _text(value: str) -> str:
    return html.escape(value, quote=False)


def _evidence(source_id: str, uri: str | None) -> str:
    label = _text(source_id).replace("[", "&#91;").replace("]", "&#93;")
    if uri is None:
        return _code(source_id)
    destination = uri.replace(">", "%3E")
    return f"[{label}](<{destination}>)"
