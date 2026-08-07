from __future__ import annotations

import json
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from matterhorn.canonical import (
    canonical_json,
    derive_assertion_id,
    instant_text,
    object_key,
)
from matterhorn.contracts import (
    FIELD_WIDE_RETRACT,
    Assertion,
    Operation,
    Origin,
    SchemaProfile,
    SourceRef,
)
from matterhorn.contracts.schema import resolve_schema
from matterhorn.defaults import Engine
from matterhorn.distill import ToolLoopResult
from matterhorn.engine.handles import normalize_handle
from matterhorn.store import SQLiteStore, Store


class ConformanceFailure(AssertionError):
    pass


@dataclass(frozen=True)
class ConformanceResult:
    case_id: str
    title: str
    passed: bool
    detail: str | None = None


_REQUIRED_CASE_FIELDS = {
    "case_id": str,
    "title": str,
    "invariants": list,
    "schema_profile": (str, dict),
    "scope_id": str,
    "clock": list,
    "cards": list,
}


class FixedClock:
    def __init__(self, values: list[Any]):
        self.values = [
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value)
            for value in values
        ]
        self.index = 0

    def __call__(self) -> datetime:
        if not self.values:
            raise ConformanceFailure("conformance clock was unexpectedly consumed")
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class FixtureGateway:
    def __init__(
        self,
        *,
        extraction: list[Any],
        adjudication: list[Any],
        semantic: list[Any],
        tool_loop: list[Any] | None = None,
    ):
        self.responses = {
            "extraction": list(extraction),
            "adjudication": list(adjudication),
            "semantic": list(semantic),
        }
        self.indexes = {kind: 0 for kind in self.responses}
        self.calls: list[dict[str, Any]] = []
        self.tool_loop_sessions = list(tool_loop or [])
        self.tool_loop_index = 0
        self.tool_loop_calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        schema = kwargs.get("response_schema", {})
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if schema.get("$id") == "matterhorn-identity-adjudication/v1":
            kind = "adjudication"
        elif "cards" in properties:
            kind = "extraction"
        else:
            kind = "semantic"
        index = self.indexes[kind]
        if index >= len(self.responses[kind]):
            raise ConformanceFailure(f"{kind} model response fixture was exhausted")
        value = self.responses[kind][index]
        self.indexes[kind] += 1
        return value if isinstance(value, str) else json.dumps(value, default=str)

    def tool_loop(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        handler: Callable[[str, dict[str, Any]], Any],
        max_tool_calls: int = 16,
        max_emissions: int = 4,
    ) -> ToolLoopResult:
        if self.tool_loop_index >= len(self.tool_loop_sessions):
            raise ConformanceFailure("tool-loop session fixture was exhausted")
        raw_session = self.tool_loop_sessions[self.tool_loop_index]
        self.tool_loop_index += 1
        turns = raw_session.get("turns") if isinstance(raw_session, dict) else raw_session
        if not isinstance(turns, list):
            raise ConformanceFailure("tool-loop session MUST be a turn sequence")
        call_log: dict[str, Any] = {
            "system": system,
            "user": user,
            "tools": tools,
            "calls": [],
        }
        self.tool_loop_calls.append(call_log)
        tool_calls = emissions = 0
        final_message: str | None = None
        for turn in turns:
            if not isinstance(turn, dict):
                raise ConformanceFailure("tool-loop turn MUST be a mapping")
            if "final_message" in turn:
                value = turn["final_message"]
                final_message = value if value is None or isinstance(value, str) else str(value)
                break
            requested = turn.get("tool_calls")
            if requested is None and "tool_call" in turn:
                requested = [turn["tool_call"]]
            if not isinstance(requested, list):
                raise ConformanceFailure(
                    "tool-loop turn requires tool_calls or final_message"
                )
            for call in requested:
                if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                    raise ConformanceFailure("invalid scripted tool call")
                name = call["name"]
                next_emissions = emissions + int(name == "emit")
                if tool_calls + 1 > max_tool_calls or next_emissions > max_emissions:
                    return ToolLoopResult(
                        final_message=final_message,
                        tool_calls=tool_calls,
                        emissions=emissions,
                        exhausted=True,
                    )
                arguments = call.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ConformanceFailure("scripted tool arguments MUST be a mapping")
                output = handler(name, arguments)
                tool_calls += 1
                emissions = next_emissions
                call_log["calls"].append(
                    {"name": name, "arguments": arguments, "output": output}
                )
        return ToolLoopResult(
            final_message=final_message,
            tool_calls=tool_calls,
            emissions=emissions,
        )


def discover_cases(suite: str | Path) -> list[Path]:
    directory = Path(suite)
    if not directory.is_dir():
        raise FileNotFoundError(f"conformance suite directory not found: {directory}")
    cases = sorted(directory.glob("*.yaml"))
    if not cases:
        raise ValueError(f"conformance suite contains no YAML cases: {directory}")
    return cases


def default_suite() -> Path:
    source_tree = Path(__file__).resolve().parents[2] / "spec" / "conformance"
    if source_tree.is_dir():
        return source_tree
    installed = Path(sys.prefix) / "share" / "matterhorn" / "spec" / "conformance"
    if installed.is_dir():
        return installed
    raise FileNotFoundError(
        "packaged conformance suite was not found; pass --suite DIR"
    )


def run_case(case_path: str | Path, store: Store | str | Path) -> ConformanceResult:
    path = Path(case_path)
    case = _load_case(path)
    try:
        _execute_case(case, store)
    except Exception as error:
        if isinstance(error, KeyboardInterrupt):
            raise
        return ConformanceResult(
            case_id=case.get("case_id", path.stem),
            title=case.get("title", path.stem),
            passed=False,
            detail=f"{type(error).__name__}: {error}",
        )
    return ConformanceResult(
        case_id=case["case_id"],
        title=case["title"],
        passed=True,
    )


def run_suite(
    suite: str | Path,
    *,
    store_factory: Callable[[Path], Store] | None = None,
) -> list[ConformanceResult]:
    cases = discover_cases(suite)
    loaded_cases = [(case_path, _load_case(case_path)) for case_path in cases]
    with tempfile.TemporaryDirectory(prefix="matterhorn-conformance-") as directory:
        root = Path(directory)
        results = []
        for case_path, case in loaded_cases:
            store = (
                store_factory(case_path)
                if store_factory is not None
                else SQLiteStore(root / f"{case_path.stem}.db")
            )
            try:
                store.clear_scope(case["scope_id"])
                results.append(run_case(case_path, store))
            finally:
                store.close()
        return results


def _load_case(path: Path) -> dict[str, Any]:
    try:
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"malformed conformance case {path}: {error}") from error
    if not isinstance(case, dict):
        # All unusable suite contents share the documented ValueError contract.
        raise ValueError(  # noqa: TRY004
            f"malformed conformance case {path}: top level must be a mapping"
        )
    for field, expected_type in _REQUIRED_CASE_FIELDS.items():
        if field not in case:
            raise ValueError(
                f"malformed conformance case {path}: missing {field}"
            )
        if not isinstance(case[field], expected_type):
            # Field shape errors are suite validation failures, not API misuse.
            raise ValueError(  # noqa: TRY004
                f"malformed conformance case {path}: invalid {field}"
            )
    if not case["case_id"] or not case["title"] or not case["invariants"]:
        raise ValueError(
            f"malformed conformance case {path}: "
            "case_id, title, and invariants must be non-empty"
        )
    if "corrections" in case and not isinstance(case["corrections"], list):
        raise ValueError(
            f"malformed conformance case {path}: invalid corrections"
        )
    if "model_responses" in case and not isinstance(
        case["model_responses"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid model_responses"
        )
    if "record_batches" in case and not isinstance(case["record_batches"], list):
        raise ValueError(
            f"malformed conformance case {path}: invalid record_batches"
        )
    if "record_model_responses" in case and not isinstance(
        case["record_model_responses"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid record_model_responses"
        )
    if "message_batches" in case and not isinstance(
        case["message_batches"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid message_batches"
        )
    for batch in case.get("message_batches", []):
        if not isinstance(batch, dict) or (
            "flush" in batch and not isinstance(batch["flush"], dict)
        ):
            raise ValueError(
                f"malformed conformance case {path}: invalid message batch flush"
            )
    if "message_model_responses" in case and not isinstance(
        case["message_model_responses"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid message_model_responses"
        )
    if "merge_operations" in case and not isinstance(
        case["merge_operations"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid merge_operations"
        )
    if "handle_normalization_cases" in case and not isinstance(
        case["handle_normalization_cases"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid handle_normalization_cases"
        )
    if "handle_operations" in case and not isinstance(
        case["handle_operations"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid handle_operations"
        )
    if "adjudication_model_responses" in case and not isinstance(
        case["adjudication_model_responses"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: "
            "invalid adjudication_model_responses"
        )
    if "tool_loop_sessions" in case and not isinstance(
        case["tool_loop_sessions"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid tool_loop_sessions"
        )
    if "unified_loop" in case and not isinstance(case["unified_loop"], bool):
        raise ValueError(f"malformed conformance case {path}: invalid unified_loop")
    if "theme_config" in case and not isinstance(case["theme_config"], dict):
        raise ValueError(f"malformed conformance case {path}: invalid theme_config")
    if "theme_operations" in case and not isinstance(
        case["theme_operations"], list
    ):
        raise ValueError(f"malformed conformance case {path}: invalid theme_operations")
    if "review_operations" in case and not isinstance(
        case["review_operations"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid review_operations"
        )
    for field in ("signal_operations", "watermark_operations"):
        if field in case and not isinstance(case[field], list):
            raise ValueError(f"malformed conformance case {path}: invalid {field}")
    if "structure_operations" in case and not isinstance(
        case["structure_operations"], list
    ):
        raise ValueError(
            f"malformed conformance case {path}: invalid structure_operations"
        )
    if "signal_config" in case and not isinstance(case["signal_config"], dict):
        raise ValueError(
            f"malformed conformance case {path}: invalid signal_config"
        )
    if "expect_error" in case:
        if not isinstance(case["expect_error"], str) or not case["expect_error"]:
            raise ValueError(
                f"malformed conformance case {path}: invalid expect_error"
            )
    elif not isinstance(case.get("expect"), dict):
        raise ValueError(f"malformed conformance case {path}: missing expect")
    return case


def _execute_case(case: dict[str, Any], store: Store | str | Path) -> None:
    profile_value = case["schema_profile"]
    profile = (
        resolve_schema(profile_value)
        if isinstance(profile_value, str)
        else SchemaProfile.model_validate(profile_value)
    )
    record_model_responses = case.get("record_model_responses", [])
    message_model_responses = case.get("message_model_responses", [])
    dream_model_responses = case.get("model_responses", [])
    adjudication_model_responses = case.get(
        "adjudication_model_responses", []
    )
    tool_loop_sessions = case.get("tool_loop_sessions", [])
    has_gateway_fixtures = (
        "record_model_responses" in case
        or "message_model_responses" in case
        or "adjudication_model_responses" in case
        or "model_responses" in case
        or "tool_loop_sessions" in case
    )
    fixture_gateway = (
        FixtureGateway(
            extraction=[
                *record_model_responses,
                *message_model_responses,
            ],
            adjudication=adjudication_model_responses,
            semantic=dream_model_responses,
            tool_loop=tool_loop_sessions,
        )
        if has_gateway_fixtures
        else None
    )
    engine = Engine(
        store,
        profile,
        clock=FixedClock(case.get("clock", [])),
        gateway=fixture_gateway,
        unified_loop=case.get("unified_loop", False),
        **case.get("signal_config", {}),
        **case.get("theme_config", {}),
    )
    for normalization_case in case.get("handle_normalization_cases", []):
        _equal(
            normalize_handle(
                profile,
                normalization_case["handle_type"],
                normalization_case["value"],
            ),
            normalization_case["normalized_value"],
            f"handle normalization {normalization_case['handle_type']}",
        )
    expected_error = case.get("expect_error")
    if expected_error:
        try:
            engine._ingest_cards_sync(
                case.get("cards", []), scope_id=case["scope_id"]
            )
            for correction in case.get("corrections", []):
                engine.correct(correction)
        except Exception as error:
            if not re.search(expected_error, str(error)):
                raise ConformanceFailure(
                    f"error {error!r} did not match {expected_error!r}"
                ) from error
        else:
            raise ConformanceFailure(
                f"expected error matching {expected_error!r}, but none was raised"
            )
        _equal(engine.store.assertions(case["scope_id"]), [], "error assertions")
        _equal(engine.store.intervals(case["scope_id"]), [], "error intervals")
        return

    engine._ingest_cards_sync(
        case.get("cards", []), scope_id=case["scope_id"]
    )
    _run_handle_operations(engine, case)
    record_reports, staging_purge_counts = _run_record_batches(engine, case)
    message_tasks, message_flush_reports = _run_message_batches(engine, case)
    first_dream = None
    if (
        case.get("model_responses") is not None
        and not case.get("message_batches")
    ):
        first_dream = engine.dream(case["scope_id"])
    for correction in case.get("corrections", []):
        engine.correct(correction)
    _run_merge_operations(engine, case)
    _run_structure_operations(engine, case)
    theme_reports = _run_theme_operations(engine, case)
    _run_review_operations(engine, case)
    _run_signal_operations(engine, case)
    _run_watermark_operations(engine, case)

    expect = case["expect"]
    if "record_reports" in expect:
        _equal(
            [
                {
                    key: _plain(report)[key]
                    for key in wanted
                }
                for report, wanted in zip(
                    record_reports,
                    expect["record_reports"],
                    strict=True,
                )
            ],
            expect["record_reports"],
            "record_reports",
        )
    if "staging_purge_counts" in expect:
        _equal(
            staging_purge_counts,
            expect["staging_purge_counts"],
            "staging_purge_counts",
        )
    if "task_results" in expect:
        _equal(
            [
                _project_partial(_plain(result), wanted)
                for result, wanted in zip(
                    message_tasks,
                    expect["task_results"],
                    strict=True,
                )
            ],
            expect["task_results"],
            "task_results",
        )
    if "flush_reports" in expect:
        _equal(
            [
                [
                    _project_partial(_plain(report), wanted_report)
                    for report, wanted_report in zip(
                        reports,
                        wanted_reports,
                        strict=True,
                    )
                ]
                for reports, wanted_reports in zip(
                    message_flush_reports,
                    expect["flush_reports"],
                    strict=True,
                )
            ],
            expect["flush_reports"],
            "flush_reports",
        )
    if "extraction_calls" in expect:
        if fixture_gateway is None:
            raise ConformanceFailure(
                "extraction_calls requires gateway fixture responses"
            )
        _assert_extraction_calls(
            fixture_gateway.calls,
            expect["extraction_calls"],
        )
    if "adjudication_calls" in expect:
        if fixture_gateway is None:
            raise ConformanceFailure(
                "adjudication_calls requires gateway fixture responses"
            )
        _assert_adjudication_calls(
            fixture_gateway.calls,
            expect["adjudication_calls"],
        )
    if "tool_loop_calls" in expect:
        if fixture_gateway is None:
            raise ConformanceFailure(
                "tool_loop_calls requires gateway fixture responses"
            )
        _equal(
            len(fixture_gateway.tool_loop_calls),
            expect["tool_loop_calls"],
            "tool_loop_calls",
        )
    if "theme_reports" in expect:
        _equal(
            [
                _project_partial(_plain(report.to_dict()), wanted)
                for report, wanted in zip(
                    theme_reports,
                    expect["theme_reports"],
                    strict=True,
                )
            ],
            expect["theme_reports"],
            "theme_reports",
        )
    if "dream_report" in expect:
        actual_report = _plain(first_dream)
        _equal(
            {key: actual_report[key] for key in expect["dream_report"]},
            expect["dream_report"],
            "dream_report",
        )
    _assert_partial_exact(
        engine.store.assertions(case["scope_id"]),
        expect.get("assertions", []),
        "assertions",
    )
    _assert_partial_exact(
        engine.store.intervals(case["scope_id"]),
        expect.get("intervals", []),
        "intervals",
    )
    if "events" in expect:
        _assert_partial_exact(
            engine.events(case["scope_id"]),
            expect["events"],
            "events",
        )
    if "subject_count" in expect:
        _equal(
            len(engine.store.subjects(case["scope_id"])),
            expect["subject_count"],
            "subject_count",
        )
    if "merge_count" in expect:
        _equal(
            len(engine.store.subject_merges(case["scope_id"])),
            expect["merge_count"],
            "merge_count",
        )
    _assert_handle_expectations(engine, case["scope_id"], expect)
    if "review_items" in expect:
        _assert_review_items(
            engine.review_items(case["scope_id"]),
            expect["review_items"],
        )
    if "matters" in expect:
        _assert_partial_exact(
            engine.matters(case["scope_id"]),
            expect["matters"],
            "matters",
        )
    if "signals" in expect:
        _assert_partial_exact(
            engine.signals(case["scope_id"]),
            expect["signals"],
            "signals",
        )
    if "watermarks" in expect:
        _equal(
            _plain(engine.store.read_watermarks(case["scope_id"])),
            _plain(expect["watermarks"]),
            "watermarks",
        )
    for index, query in enumerate(expect.get("hotness_queries", [])):
        actual = engine.hotness(
            _instant(query["window_start"]),
            _instant(query["window_end"]),
            scope_ids=[case["scope_id"]],
        )
        _assert_partial_exact(actual, query["result"], f"hotness_queries[{index}]")
    for index, query in enumerate(expect.get("brief_queries", [])):
        actual = _plain(
            engine.brief(
                _instant(query["window_start"]),
                _instant(query["window_end"]),
                console_groups=query.get("console_groups", {}),
                scope_ids=[case["scope_id"]],
            )
        )
        _equal(
            _project_partial(actual, query["result"]),
            _plain(query["result"]),
            f"brief_queries[{index}]",
        )
    for index, query in enumerate(expect.get("graph_queries", [])):
        actual = _plain(
            engine.matter_graph(
                case["scope_id"], query["subject_key"]
            ).to_dict()
        )
        _equal(
            _project_partial(actual, query["result"]),
            _plain(query["result"]),
            f"graph_queries[{index}]",
        )
    if "conflicts_resolved" in expect:
        actual_stats = {
            item.predicate: item.conflicts_resolved
            for item in engine.projection_statistics(case["scope_id"])
        }
        for predicate, count in expect["conflicts_resolved"].items():
            _equal(actual_stats[predicate], count, f"conflicts_resolved.{predicate}")
    if "gate_statistics" in expect:
        _equal(
            _project_partial(
                _plain(engine.gate_statistics(case["scope_id"])),
                expect["gate_statistics"],
            ),
            expect["gate_statistics"],
            "gate_statistics",
        )
    for query in expect.get("queries", []):
        actual = _run_query(engine, case["scope_id"], query)
        expected = query["result"]
        if isinstance(expected, list):
            _equal(len(actual), len(expected), f"query {query['name']} length")
            for index, (item, wanted) in enumerate(zip(actual, expected, strict=True)):
                _equal(
                    {key: item[key] for key in wanted},
                    _plain(wanted),
                    f"query {query['name']}[{index}]",
                )
        else:
            _equal(actual, expected, f"query {query['name']}")

    initial = _snapshot(engine, case["scope_id"])
    engine._ingest_cards_sync(
        case.get("cards", []), scope_id=case["scope_id"]
    )
    second_record_reports, _ = _run_record_batches(engine, case)
    second_message_tasks, second_message_flush_reports = _run_message_batches(
        engine, case
    )
    second_dream = None
    if (
        case.get("model_responses") is not None
        and not case.get("message_batches")
    ):
        second_dream = engine.dream(case["scope_id"])
    if "second_dream" in expect:
        actual_report = _plain(second_dream)
        _equal(
            {key: actual_report[key] for key in expect["second_dream"]},
            expect["second_dream"],
            "second_dream",
        )
    if "second_record_reports" in expect:
        _equal(
            [
                {
                    key: _plain(report)[key]
                    for key in wanted
                }
                for report, wanted in zip(
                    second_record_reports,
                    expect["second_record_reports"],
                    strict=True,
                )
            ],
            expect["second_record_reports"],
            "second_record_reports",
        )
    if "second_task_results" in expect:
        _equal(
            [
                _project_partial(_plain(result), wanted)
                for result, wanted in zip(
                    second_message_tasks,
                    expect["second_task_results"],
                    strict=True,
                )
            ],
            expect["second_task_results"],
            "second_task_results",
        )
    if "second_flush_reports" in expect:
        _equal(
            [
                [
                    _project_partial(_plain(report), wanted_report)
                    for report, wanted_report in zip(
                        reports,
                        wanted_reports,
                        strict=True,
                    )
                ]
                for reports, wanted_reports in zip(
                    second_message_flush_reports,
                    expect["second_flush_reports"],
                    strict=True,
                )
            ],
            expect["second_flush_reports"],
            "second_flush_reports",
        )
    for correction in case.get("corrections", []):
        engine.correct(correction)
    _equal(_snapshot(engine, case["scope_id"]), initial, "idempotent re-ingest")

    export_before_replay = (
        canonical_json(engine.export(case["scope_id"]).model_dump(mode="json"))
        if expect.get("export_replay_identity")
        else None
    )
    replay_report = engine.replay(case["scope_id"])
    if "replay_events_emitted" in expect:
        _equal(
            replay_report.events_emitted,
            expect["replay_events_emitted"],
            "replay events emitted",
        )
    _equal(_snapshot(engine, case["scope_id"]), initial, "replay snapshot")
    if export_before_replay is not None:
        _equal(
            canonical_json(
                engine.export(case["scope_id"]).model_dump(mode="json")
            ),
            export_before_replay,
            "replay export",
        )


def _run_message_batches(
    engine: Engine, case: dict[str, Any]
) -> tuple[list[Any], list[list[Any]]]:
    results = []
    flush_reports = []
    for batch in case.get("message_batches", []):
        receipt = engine.add(
            case["scope_id"],
            batch.get("messages", []),
        )
        flush = batch.get("flush")
        if flush is None:
            reports = [engine.flush(case["scope_id"])]
        elif flush.get("mode") == "quiet":
            instant = flush["at"]
            if not isinstance(instant, datetime):
                instant = datetime.fromisoformat(instant)
            reports = [
                report
                for report in engine.flush_quiet_at(
                    flush["quiet_period_minutes"],
                    instant,
                    max_batch_delay_minutes=flush["max_batch_delay_minutes"],
                )
                # flush_quiet is store-global; on a shared backend (the
                # PostgreSQL conformance database) another case's leftover
                # scope may also be due. Cases are scope-namespaced, so only
                # this case's reports are part of its contract.
                if report.scope_id == case["scope_id"]
            ]
        else:
            raise ConformanceFailure(
                f"unknown message batch flush mode {flush.get('mode')!r}"
            )
        flush_reports.append(reports)
        results.append(engine.task(receipt.task_id))
    return results, flush_reports


def _run_record_batches(
    engine: Engine,
    case: dict[str, Any],
) -> tuple[list[Any], list[int]]:
    reports = []
    purge_counts = []
    for batch in case.get("record_batches", []):
        purge_at = batch.get("purge_staging_at")
        if purge_at is not None:
            purge_counts.append(
                engine.purge_staging(
                    case["scope_id"],
                    as_of=(
                        purge_at
                        if isinstance(purge_at, datetime)
                        else datetime.fromisoformat(purge_at)
                    ),
                )
            )
        reports.append(
            engine.add_records(
                batch.get("records", []),
                scope_id=case["scope_id"],
                cursors=batch.get("cursors"),
                backfill=batch.get("backfill", False),
                batch_size=batch.get("batch_size", 8),
            )
        )
    return reports, purge_counts


def _run_merge_operations(engine: Engine, case: dict[str, Any]) -> None:
    scope_id = case["scope_id"]
    for operation in case.get("merge_operations", []):
        kind = operation.get("operation")

        def invoke(
            current_operation: dict[str, Any] = operation,
            current_kind: Any = kind,
        ) -> Any:
            if current_kind == "merge":
                return engine.merge_subjects(
                    scope_id,
                    current_operation["source_subject_key"],
                    current_operation["target_subject_key"],
                    source_refs=current_operation["source_refs"],
                    valid_from=current_operation["valid_from"],
                )
            if current_kind == "unmerge":
                return engine.unmerge_subjects(
                    scope_id,
                    current_operation["source_subject_key"],
                    source_refs=current_operation["source_refs"],
                    valid_from=current_operation["valid_from"],
                )
            raise ConformanceFailure(
                f"unknown merge operation {current_kind!r}"
            )

        expected_error = operation.get("expect_error")
        if expected_error is None:
            invoke()
            _assert_handle_expectations(
                engine,
                scope_id,
                {
                    "subject_handles": operation.get("expect_subject_handles"),
                    "handle_lookups": operation.get("expect_handle_lookups"),
                },
            )
            continue
        try:
            invoke()
        except Exception as error:
            if not re.search(expected_error, str(error)):
                raise ConformanceFailure(
                    f"merge error {error!r} did not match {expected_error!r}"
                ) from error
        else:
            raise ConformanceFailure(
                f"expected merge error matching {expected_error!r}"
            )


def _run_handle_operations(engine: Engine, case: dict[str, Any]) -> None:
    scope_id = case["scope_id"]
    for operation in case.get("handle_operations", []):
        kind = operation.get("operation")
        if kind == "bind":
            engine.bind_handle(
                scope_id,
                operation["subject_key"],
                operation["handle_type"],
                operation["handle_value"],
                source_refs=operation["source_refs"],
            )
        elif kind == "unbind":
            engine.unbind_handle(
                scope_id,
                operation["subject_key"],
                operation["handle_type"],
                operation["normalized_value"],
                source_refs=operation["source_refs"],
            )
        else:
            raise ConformanceFailure(f"unknown handle operation {kind!r}")


def _run_review_operations(engine: Engine, case: dict[str, Any]) -> None:
    scope_id = case["scope_id"]
    for operation in case.get("review_operations", []):

        def invoke(current: dict[str, Any] = operation) -> Any:
            return engine.resolve_review(
                scope_id,
                current["review_id"],
                action=current["action"],
                subject_key=current.get("subject_key"),
                parent_subject_key=current.get("parent_subject_key"),
                source_refs=current["source_refs"],
            )

        expected_error = operation.get("expect_error")
        if expected_error is None:
            invoke()
            continue
        try:
            invoke()
        except Exception as error:
            if not re.search(expected_error, str(error)):
                raise ConformanceFailure(
                    f"review error {error!r} did not match {expected_error!r}"
                ) from error
        else:
            raise ConformanceFailure(
                f"expected review error matching {expected_error!r}"
            )


def _run_structure_operations(engine: Engine, case: dict[str, Any]) -> None:
    for operation in case.get("structure_operations", []):
        payload = {
            key: value
            for key, value in operation.items()
            if key != "expect_error"
        }

        def invoke(current: dict[str, Any] = payload) -> Any:
            if current.get("origin", "human") == "model":
                return _admit_model_structure_operation(engine, current)
            return engine.correct(current)

        expected_error = operation.get("expect_error")
        if expected_error is None:
            invoke()
            continue
        try:
            invoke()
        except Exception as error:
            if not re.search(expected_error, str(error)):
                raise ConformanceFailure(
                    f"structure error {error!r} did not match "
                    f"{expected_error!r}"
                ) from error
        else:
            raise ConformanceFailure(
                f"expected structure error matching {expected_error!r}"
            )


def _admit_model_structure_operation(
    engine: Engine,
    operation: dict[str, Any],
) -> Assertion:
    operation_name = Operation(operation.get("operation", Operation.ASSERT))
    value = operation.get("object_value")
    value_key = operation.get("object_key")
    if value_key is None:
        value_key = (
            object_key(value)
            if operation_name == Operation.ASSERT or value is not None
            else FIELD_WIDE_RETRACT
        )
    source_refs = [SourceRef.model_validate(item) for item in operation["source_refs"]]
    valid_from = _instant(operation["valid_from"])
    recorded_at = engine.now()
    assertion = Assertion(
        assertion_id=derive_assertion_id(
            operation["scope_id"],
            operation["subject_key"],
            operation["predicate"],
            operation_name,
            value_key,
            valid_from,
            source_refs,
        ),
        scope_id=operation["scope_id"],
        subject_key=operation["subject_key"],
        subject_type=operation["subject_type"],
        predicate=operation["predicate"],
        operation=operation_name,
        object_value=value,
        object_key=value_key,
        valid_from=valid_from,
        recorded_at=recorded_at,
        source_refs=source_refs,
        origin=Origin.model,
    )
    rejection = engine._structure_rejection(assertion)
    if rejection is not None:
        engine._record_structure_rejection(assertion.scope_id, rejection)
        raise ValueError(f"{rejection.value}: rejected {assertion.predicate} edge")
    with engine.store.transaction():
        for source_ref in source_refs:
            engine.store.observe_source(assertion.scope_id, source_ref)
        engine._add_assertion(assertion)
        engine._rebuild(assertion.scope_id)
    return assertion


def _run_theme_operations(engine: Engine, case: dict[str, Any]) -> list[Any]:
    reports = []
    for operation in case.get("theme_operations", []):
        kind = operation.get("operation", "run")
        if kind == "observe_record":
            with engine.store.transaction():
                engine.store.mark_record_observation(
                    case["scope_id"],
                    operation["record_id"],
                    operation["observation_hash"],
                    operation["container_id"],
                    _instant(operation["observed_at"]),
                )
            continue
        if kind != "run":
            raise ConformanceFailure(
                f"unknown theme operation {kind!r}"
            )
        reports.append(
            engine.themes(
                case["scope_id"],
                dry_run=operation.get("dry_run", False),
            )
        )
    return reports


def _run_signal_operations(engine: Engine, case: dict[str, Any]) -> None:
    for operation in case.get("signal_operations", []):
        if operation.get("operation") != "ack":
            raise ConformanceFailure(
                f"unknown signal operation {operation.get('operation')!r}"
            )
        engine.acknowledge_signal(
            case["scope_id"],
            operation["record_id"],
            operation["kind"],
            acked_at=_instant(operation["acked_at"]),
        )


def _run_watermark_operations(engine: Engine, case: dict[str, Any]) -> None:
    for operation in case.get("watermark_operations", []):
        engine.set_seen(
            case["scope_id"],
            operation["subject_key"],
            last_seen_at=_instant(operation["last_seen_at"]),
        )


def _assert_handle_expectations(
    engine: Engine,
    scope_id: str,
    expect: dict[str, Any],
) -> None:
    if expect.get("handle_bindings") is not None:
        _assert_partial_exact(
            engine.store.subject_handle_bindings(scope_id),
            expect["handle_bindings"],
            "handle_bindings",
        )
    subject_handles = expect.get("subject_handles")
    if subject_handles is not None:
        for subject_key, wanted in subject_handles.items():
            _assert_partial_exact(
                engine.subject_handles(scope_id, subject_key),
                wanted,
                f"subject_handles.{subject_key}",
            )
    handle_lookups = expect.get("handle_lookups")
    if handle_lookups is not None:
        for index, lookup in enumerate(handle_lookups):
            _assert_partial_exact(
                engine.handle_lookup(
                    scope_id,
                    lookup["value"],
                    lookup.get("handle_type"),
                ),
                lookup["result"],
                f"handle_lookups[{index}]",
            )


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return instant_text(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="python"))
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset, tuple)):
        return [_plain(item) for item in sorted(value)]
    return value


def _instant(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _project_partial(actual: Any, wanted: Any) -> Any:
    if isinstance(wanted, dict):
        return {
            key: _project_partial(actual[key], value)
            for key, value in wanted.items()
        }
    if isinstance(wanted, list):
        return [
            _project_partial(item, expected)
            for item, expected in zip(actual, wanted, strict=True)
        ]
    return actual


def _assert_partial_exact(
    actual: list[Any],
    expected: list[dict[str, Any]],
    label: str,
) -> None:
    actual_dicts = [_plain(item) for item in actual]
    _equal(len(actual_dicts), len(expected), f"{label} length")
    unmatched = list(actual_dicts)
    for wanted in expected:
        wanted = _plain(wanted)
        for index, candidate in enumerate(unmatched):
            if {key: candidate[key] for key in wanted} == wanted:
                unmatched.pop(index)
                break
        else:
            raise ConformanceFailure(
                f"{label}: no actual item matched {wanted!r}; remaining={unmatched!r}"
            )
    _equal(unmatched, [], f"{label} unmatched")


def _assert_extraction_calls(
    gateway_calls: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    actual = []
    for call in gateway_calls:
        try:
            payload = json.loads(call["user"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(
            payload.get("records"), list
        ):
            continue
        actual.append(
            {
                "context": [
                    item["record"] for item in payload.get("context", [])
                ],
                "records": [item["record"] for item in payload["records"]]
            }
        )

    _equal(len(actual), len(expected), "extraction_calls length")
    for call_index, (actual_call, expected_call) in enumerate(
        zip(actual, expected, strict=True)
    ):
        wanted_context = expected_call.get("context", [])
        actual_context = actual_call["context"]
        _equal(
            len(actual_context),
            len(wanted_context),
            f"extraction_calls[{call_index}] context length",
        )
        for record_index, (record, wanted) in enumerate(
            zip(actual_context, wanted_context, strict=True)
        ):
            _equal(
                {key: record[key] for key in wanted},
                wanted,
                f"extraction_calls[{call_index}].context[{record_index}]",
            )
        wanted_records = expected_call["records"]
        actual_records = actual_call["records"]
        _equal(
            len(actual_records),
            len(wanted_records),
            f"extraction_calls[{call_index}] records length",
        )
        for record_index, (record, wanted) in enumerate(
            zip(actual_records, wanted_records, strict=True)
        ):
            _equal(
                {key: record[key] for key in wanted},
                wanted,
                f"extraction_calls[{call_index}].records[{record_index}]",
            )


def _assert_adjudication_calls(
    gateway_calls: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    actual = []
    for call in gateway_calls:
        schema = call.get("response_schema", {})
        if schema.get("$id") != "matterhorn-identity-adjudication/v1":
            continue
        payload = json.loads(call["user"])
        actual.append(payload["candidates"])
    _equal(len(actual), len(expected), "adjudication_calls length")
    for call_index, (candidates, wanted) in enumerate(
        zip(actual, expected, strict=True)
    ):
        _equal(
            [item["subject_key"] for item in candidates],
            wanted["candidate_keys"],
            f"adjudication_calls[{call_index}].candidate_keys",
        )
        if "candidates" in wanted:
            _equal(
                len(candidates),
                len(wanted["candidates"]),
                f"adjudication_calls[{call_index}].candidates length",
            )
            for candidate_index, (candidate, partial) in enumerate(
                zip(candidates, wanted["candidates"], strict=True)
            ):
                _equal(
                    {key: candidate[key] for key in partial},
                    partial,
                    f"adjudication_calls[{call_index}]"
                    f".candidates[{candidate_index}]",
                )


def _assert_review_items(
    actual: list[Any], expected: list[dict[str, Any]]
) -> None:
    unmatched = [_plain(item) for item in actual]
    _equal(len(unmatched), len(expected), "review_items length")
    for wanted in expected:
        wanted = _plain(wanted)
        for index, candidate in enumerate(unmatched):
            projected = {}
            for key, value in wanted.items():
                if key == "card_json" and isinstance(value, dict):
                    projected[key] = {
                        nested: candidate[key][nested] for nested in value
                    }
                else:
                    projected[key] = candidate[key]
            if projected == wanted:
                unmatched.pop(index)
                break
        else:
            raise ConformanceFailure(
                f"review_items: no actual item matched {wanted!r}; "
                f"remaining={unmatched!r}"
            )
    _equal(unmatched, [], "review_items unmatched")


def _snapshot(engine: Engine, scope_id: str) -> str:
    assertions = engine.store.assertions(scope_id)
    merges = engine.store.subject_merges(scope_id)
    source_refs = []
    seen_sources: set[str] = set()
    for assertion in assertions:
        for source_ref in assertion.source_refs:
            if source_ref.source_id not in seen_sources:
                source_refs.append(source_ref)
                seen_sources.add(source_ref.source_id)
    for merge in merges:
        for source_ref in merge.source_refs:
            if source_ref.source_id not in seen_sources:
                source_refs.append(source_ref)
                seen_sources.add(source_ref.source_id)
    handles = engine.store.subject_handle_bindings(scope_id)
    for handle in handles:
        for source_ref in [*handle.source_refs, *handle.revocation_source_refs]:
            if source_ref.source_id not in seen_sources:
                source_refs.append(source_ref)
                seen_sources.add(source_ref.source_id)
    return canonical_json(
        {
            "assertions": [_plain(item) for item in assertions],
            "intervals": [_plain(item) for item in engine.store.intervals(scope_id)],
            "memory_cards": [
                _plain(item) for item in engine.store.memory_cards(scope_id)
            ],
            "stats": [
                _plain(item) for item in engine.store.projection_stats(scope_id)
            ],
            "subjects": [
                _plain(item.__dict__) for item in engine.store.subjects(scope_id)
            ],
            "merges": [_plain(item) for item in merges],
            "subject_handles": [_plain(item) for item in handles],
            "review_queue": [
                _plain(item)
                for item in engine.store.review_items(
                    scope_id,
                    pending_only=False,
                )
            ],
            "record_observations": [
                _plain(item)
                for item in engine.store.record_observations(scope_id)
            ],
            "source_states": [
                _plain(item)
                for item in engine.store.source_states(scope_id, source_refs)
            ],
            "sync_positions": [
                _plain(item) for item in engine.store.sync_positions(scope_id)
            ],
            "events": [
                _plain(item) for item in engine.store.events(scope_id)
            ],
            "signals": [
                _plain(item) for item in engine.store.signals(scope_id)
            ],
            "read_watermarks": _plain(
                engine.store.read_watermarks(scope_id)
            ),
            "theme_schedule_state": _plain(
                engine.store.theme_schedule_state(scope_id)
            ),
        }
    )


def _run_query(engine: Engine, scope_id: str, query: dict[str, Any]) -> Any:
    args = dict(query.get("args", {}))
    if "instant" in args and isinstance(args["instant"], str):
        args["instant"] = datetime.fromisoformat(args["instant"])
    result = getattr(engine.query, query["name"])(scope_id, **args)
    if isinstance(result, list):
        return [_plain(item.to_dict()) for item in result]
    return _plain(result)


def _equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ConformanceFailure(
            f"{label} mismatch\nexpected={expected!r}\nactual={actual!r}"
        )
