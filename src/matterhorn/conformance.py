from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import yaml
from pydantic import BaseModel

from matterhorn.contracts import SchemaProfile
from matterhorn.contracts.schema import resolve_schema
from matterhorn.engine.canonical import canonical_json, instant_text
from matterhorn.engine.engine import Engine
from matterhorn.store import SQLiteStore, Store


class ConformanceFailure(AssertionError):
    pass


@dataclass(frozen=True)
class ConformanceResult:
    case_id: str
    title: str
    passed: bool
    detail: str | None = None


class FixedClock:
    def __init__(self, values: list[Any]):
        self.values = [
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
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
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.index = 0

    def complete(self, **_kwargs: Any) -> str:
        if self.index >= len(self.responses):
            raise ConformanceFailure("model_responses fixture was exhausted")
        value = self.responses[self.index]
        self.index += 1
        return value if isinstance(value, str) else json.dumps(value, default=str)


def discover_cases(suite: str | Path) -> list[Path]:
    directory = Path(suite)
    if not directory.is_dir():
        raise FileNotFoundError(f"conformance suite directory not found: {directory}")
    return sorted(directory.glob("*.yaml"))


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
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
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
    with tempfile.TemporaryDirectory(prefix="matterhorn-conformance-") as directory:
        root = Path(directory)
        results = []
        for case_path in cases:
            store = (
                store_factory(case_path)
                if store_factory is not None
                else SQLiteStore(root / f"{case_path.stem}.db")
            )
            try:
                case = yaml.safe_load(case_path.read_text(encoding="utf-8"))
                if not isinstance(case, dict) or not isinstance(
                    case.get("scope_id"), str
                ):
                    raise ValueError(
                        f"invalid conformance case scope_id: {case_path}"
                    )
                store.clear_scope(case["scope_id"])
                results.append(run_case(case_path, store))
            finally:
                store.close()
        return results


def _execute_case(case: dict[str, Any], store: Store | str | Path) -> None:
    profile_value = case["schema_profile"]
    profile = (
        resolve_schema(profile_value)
        if isinstance(profile_value, str)
        else SchemaProfile.model_validate(profile_value)
    )
    engine = Engine(
        store,
        profile,
        clock=FixedClock(case.get("clock", [])),
        gateway=FixtureGateway(case.get("model_responses", []))
        if case.get("model_responses") is not None
        else None,
    )
    expected_error = case.get("expect_error")
    if expected_error:
        try:
            engine.ingest(case.get("cards", []), scope_id=case["scope_id"])
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

    engine.ingest(case.get("cards", []), scope_id=case["scope_id"])
    first_dream = None
    if case.get("model_responses") is not None:
        first_dream = engine.dream(case["scope_id"])
    for correction in case.get("corrections", []):
        engine.correct(correction)

    expect = case["expect"]
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
    if "subject_count" in expect:
        _equal(
            len(engine.store.subjects(case["scope_id"])),
            expect["subject_count"],
            "subject_count",
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
            _plain(engine.gate_statistics(case["scope_id"])),
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
    engine.ingest(case.get("cards", []), scope_id=case["scope_id"])
    second_dream = None
    if case.get("model_responses") is not None:
        second_dream = engine.dream(case["scope_id"])
    if "second_dream" in expect:
        actual_report = _plain(second_dream)
        _equal(
            {key: actual_report[key] for key in expect["second_dream"]},
            expect["second_dream"],
            "second_dream",
        )
    for correction in case.get("corrections", []):
        engine.correct(correction)
    _equal(_snapshot(engine, case["scope_id"]), initial, "idempotent re-ingest")

    engine.replay(case["scope_id"])
    _equal(_snapshot(engine, case["scope_id"]), initial, "replay snapshot")


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return instant_text(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _plain(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset, tuple)):
        return [_plain(item) for item in sorted(value)]
    return value


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


def _snapshot(engine: Engine, scope_id: str) -> str:
    return canonical_json(
        {
            "assertions": [_plain(item) for item in engine.store.assertions(scope_id)],
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
        }
    )


def _run_query(engine: Engine, scope_id: str, query: dict[str, Any]) -> Any:
    args = dict(query.get("args", {}))
    if "instant" in args and isinstance(args["instant"], str):
        args["instant"] = datetime.fromisoformat(
            args["instant"].replace("Z", "+00:00")
        )
    result = getattr(engine.query, query["name"])(scope_id, **args)
    if isinstance(result, list):
        return [_plain(item.to_dict()) for item in result]
    return _plain(result)


def _equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ConformanceFailure(
            f"{label} mismatch\nexpected={expected!r}\nactual={actual!r}"
        )
