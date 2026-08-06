"""Deterministic message-to-matter evaluation harness.

The runner executes the ordinary write pipeline, then scores only persisted
read-side state and the accepted cards observed at the extractor boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from matterhorn.adapters.messages import MessageCardExtractor
from matterhorn.contracts import Message, Record, SubjectRecord
from matterhorn.defaults import Engine
from matterhorn.gateway_config import configured_gateway

REPORT_SCHEMA = "matterhorn-eval/v1"
FIELD_NAMES = ("status", "owner", "next_step")
NEW_SUBJECT_PREFIX = "$new:"
SCORED_SUBJECT_TYPES = ("MATTER", "TOPIC")


class EvalHarnessError(RuntimeError):
    """The dataset, fixture, or configured gateway could not be executed."""


class AlignmentSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    source_kind: str = Field(pattern=r"^(mail|im|agent)$")
    scope_id: str = Field(min_length=1)
    window: list[dict[str, Any]] = Field(min_length=1)
    expected_assertions: list[dict[str, Any]]


class ExpectedMatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    status: str | None = None
    owner: str | None = None
    next_step: str | None = None
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_unique(self) -> ExpectedMatter:
        if len(self.evidence) != len(set(self.evidence)):
            raise ValueError("expected matter evidence MUST be unique")
        return self

    @property
    def declared_fields(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in FIELD_NAMES
            if name in self.model_fields_set
        }


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1)
    scope_id: str | None = None
    schema_profile: str = "org-matters/v1"
    rounds: list[list[Message]] = Field(min_length=1)
    expected: list[ExpectedMatter] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_names_real_unique_messages(self) -> EvalCase:
        if any(not round_messages for round_messages in self.rounds):
            raise ValueError("eval rounds MUST NOT be empty")
        message_ids = [message.id for items in self.rounds for message in items]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("message ids MUST be unique within an eval case")
        known = set(message_ids)
        for matter in self.expected:
            unknown = sorted(set(matter.evidence) - known)
            if unknown:
                raise ValueError(
                    "expected evidence MUST name input message ids: "
                    + ", ".join(unknown)
                )
        return self

    @property
    def resolved_scope_id(self) -> str:
        return self.scope_id or f"eval:{self.case_id}"


@dataclass(frozen=True)
class ProducedMatter:
    subject_key: str
    title: str
    evidence: frozenset[str]
    first_round: int
    status: Any = None
    owner: Any = None
    next_step: Any = None


@dataclass(frozen=True)
class Alignment:
    expected_index: int
    subject_key: str
    overlap: int
    title_score: float
    title_match: bool


class EvalFixtureGateway:
    """Ordered extraction fixtures with deterministic empty semantic output."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.index = 0
        self.failure: Exception | None = None

    def complete(
        self, *, system: str, user: str, response_schema: dict[str, Any]
    ) -> str:
        del system, user
        properties = response_schema.get("properties", {})
        if response_schema.get("$id") == "matterhorn-identity-adjudication/v1":
            return json.dumps(
                {
                    "decision": "abstain",
                    "subject_key": None,
                    "confidence": 0.0,
                    "evidence_source_ids": [],
                }
            )
        if "candidates" in properties:
            return '{"candidates":[]}'
        if "cards" not in properties:
            error = EvalHarnessError("fixture received an unknown response schema")
            self.failure = error
            raise error
        if self.index >= len(self.responses):
            error = EvalHarnessError("extractor response fixture was exhausted")
            self.failure = error
            raise error
        value = self.responses[self.index]
        self.index += 1
        return value if isinstance(value, str) else json.dumps(value, default=str)

    def assert_consumed(self) -> None:
        if self.index != len(self.responses):
            raise EvalHarnessError(
                "extractor response fixture has "
                f"{len(self.responses) - self.index} unused response(s)"
            )


class TrackingGateway:
    """Retain the real provider exception that Engine task handling contains."""

    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.failure: Exception | None = None

    def complete(self, **kwargs: Any) -> str:
        try:
            return self.delegate.complete(**kwargs)
        except Exception as error:
            self.failure = error
            raise


class RecordingExtractor:
    """Observe accepted cards without changing extraction behavior."""

    def __init__(self, gateway: Any, schema: str):
        self.delegate = MessageCardExtractor(gateway, schema)
        self.cards: list[Any] = []

    def extract(self, **kwargs: Any) -> Any:
        report = self.delegate.extract(**kwargs)
        self.cards.extend(report.cards)
        return report


class EvalClock:
    """Stable, inexhaustible write clock for fixture reproducibility."""

    def __init__(self) -> None:
        self.value = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def default_dataset() -> Path:
    source_tree = Path(__file__).resolve().parents[2] / "spec" / "eval"
    if source_tree.is_dir():
        return source_tree
    installed = Path(sys.prefix) / "share" / "matterhorn" / "spec" / "eval"
    if installed.is_dir():
        return installed
    raise FileNotFoundError("packaged eval dataset was not found; pass --dataset DIR")


def discover_eval_cases(dataset: str | Path) -> list[Path]:
    directory = Path(dataset)
    if not directory.is_dir():
        raise FileNotFoundError(f"eval dataset directory not found: {directory}")
    cases = sorted(
        path
        for path in directory.glob("*.yaml")
        if not path.name.endswith(".responses.yaml")
    )
    if not cases:
        raise ValueError(f"eval dataset contains no YAML cases: {directory}")
    return cases


def load_eval_case(path: str | Path) -> EvalCase:
    case_path = Path(path)
    try:
        payload = yaml.safe_load(case_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("case root MUST be a mapping")
        return EvalCase.model_validate(payload)
    except Exception as error:
        raise EvalHarnessError(f"malformed eval case {case_path.name}: {error}") from error


def load_fixture_responses(path: str | Path) -> list[Any]:
    fixture_path = Path(path)
    try:
        payload = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise EvalHarnessError(
            f"could not load response fixture {fixture_path.name}: {error}"
        ) from error
    if isinstance(payload, dict):
        if set(payload) != {"responses"}:
            raise EvalHarnessError(
                f"response fixture {fixture_path.name} MUST contain only responses"
            )
        payload = payload["responses"]
    if not isinstance(payload, list):
        raise EvalHarnessError(
            f"response fixture {fixture_path.name} MUST be a list"
        )
    return payload


def discover_alignment_samples(samples: str | Path | None = None) -> list[Path]:
    directory = (
        Path(samples)
        if samples is not None
        else default_dataset() / "samples"
    )
    if not directory.is_dir():
        raise FileNotFoundError(f"alignment sample directory not found: {directory}")
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ValueError(f"alignment sample directory contains no YAML: {directory}")
    return paths


def load_alignment_sample(path: str | Path) -> AlignmentSample:
    sample_path = Path(path)
    try:
        payload = yaml.safe_load(sample_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("sample root MUST be a mapping")
        return AlignmentSample.model_validate(payload)
    except Exception as error:
        raise EvalHarnessError(
            f"malformed alignment sample {sample_path.name}: {error}"
        ) from error


def score_assertion_set(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> dict[str, Any]:
    """Diff assertion sets, classifying equivalent facts on the wrong subject."""

    expected_remaining = list(expected)
    actual_remaining = list(actual)
    exact_pairs: list[tuple[int, int]] = []
    for expected_index, wanted in enumerate(expected):
        wanted_key = _assertion_signature(wanted, include_subject=True)
        actual_index = next(
            (
                index
                for index, item in enumerate(actual_remaining)
                if _assertion_signature(item, include_subject=True) == wanted_key
            ),
            None,
        )
        if actual_index is not None:
            exact_pairs.append((expected_index, actual_index))
            actual_remaining.pop(actual_index)
    expected_remaining = [
        item
        for index, item in enumerate(expected_remaining)
        if index not in {pair[0] for pair in exact_pairs}
    ]

    mis_attached: list[dict[str, Any]] = []
    still_missing: list[dict[str, Any]] = []
    for wanted in expected_remaining:
        fact = _assertion_signature(wanted, include_subject=False)
        actual_index = next(
            (
                index
                for index, item in enumerate(actual_remaining)
                if _assertion_signature(item, include_subject=False) == fact
            ),
            None,
        )
        if actual_index is None:
            still_missing.append(wanted)
            continue
        produced = actual_remaining.pop(actual_index)
        mis_attached.append(
            {
                "expected_subject": _assertion_subject(wanted),
                "actual_subject": _assertion_subject(produced),
                "assertion": _assertion_fact(wanted),
            }
        )
    return {
        "missing": still_missing,
        "spurious": actual_remaining,
        "mis_attached": mis_attached,
        "counts": {
            "missing": len(still_missing),
            "spurious": len(actual_remaining),
            "mis_attached": len(mis_attached),
        },
    }


def score_alignment_samples(
    produced_by_sample: dict[str, list[dict[str, Any]]],
    *,
    samples: str | Path | None = None,
) -> dict[str, Any]:
    reports = []
    for path in discover_alignment_samples(samples):
        sample = load_alignment_sample(path)
        diff = score_assertion_set(
            sample.expected_assertions,
            produced_by_sample.get(sample.sample_id, []),
        )
        reports.append(
            {
                "sample_id": sample.sample_id,
                "source_kind": sample.source_kind,
                "by_type": {
                    subject_type.casefold(): score_assertion_set(
                        [
                            item
                            for item in sample.expected_assertions
                            if _assertion_type(item) == subject_type
                        ],
                        [
                            item
                            for item in produced_by_sample.get(sample.sample_id, [])
                            if _assertion_type(item) == subject_type
                        ],
                    )
                    for subject_type in SCORED_SUBJECT_TYPES
                },
                "typing": _typing_result(
                    sample.expected_assertions,
                    produced_by_sample.get(sample.sample_id, []),
                ),
                **diff,
            }
        )
    return {
        "samples": reports,
        "counts": {
            name: sum(item["counts"][name] for item in reports)
            for name in ("missing", "spurious", "mis_attached")
        },
        "by_type": {
            subject_type.casefold(): {
                name: sum(
                    item["by_type"][subject_type.casefold()]["counts"][name]
                    for item in reports
                )
                for name in ("missing", "spurious", "mis_attached")
            }
            for subject_type in SCORED_SUBJECT_TYPES
        },
        "typing_accuracy": _success_metric(
            sum(item["typing"]["correct"] for item in reports),
            len(reports),
        ),
    }


def run_live_sample_comparison(
    *,
    samples: str | Path | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    gateway_factory: Any | None = None,
) -> dict[str, Any]:
    """Run every alignment window once through each rollout path."""

    selected_provider = provider or os.environ.get("MATTERHORN_PROVIDER")
    if gateway_factory is None:
        if selected_provider in (None, "null", "fixture", "fixture-file"):
            raise EvalHarnessError(
                "--live-samples requires a configured real gateway provider"
            )

        def gateway_factory() -> Any:
            return configured_gateway(
                provider=selected_provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
            )

    produced: dict[str, dict[str, list[dict[str, Any]]]] = {
        "legacy": {},
        "unified": {},
    }
    for path in discover_alignment_samples(samples):
        sample = load_alignment_sample(path)
        for mode, unified in (("legacy", False), ("unified", True)):
            gateway = gateway_factory()
            if unified and not callable(getattr(gateway, "tool_loop", None)):
                raise EvalHarnessError(
                    "--live-samples requires a tool-loop capable gateway"
                )
            produced[mode][sample.sample_id] = _run_live_sample(
                sample,
                gateway,
                unified=unified,
                samples_root=samples,
            )

    scores = {
        mode: score_alignment_samples(assertions, samples=samples)
        for mode, assertions in produced.items()
    }
    sample_rows = []
    by_mode_sample = {
        mode: {item["sample_id"]: item for item in score["samples"]}
        for mode, score in scores.items()
    }
    for path in discover_alignment_samples(samples):
        sample_id = load_alignment_sample(path).sample_id
        sample_rows.append(
            {
                "sample_id": sample_id,
                "legacy": by_mode_sample["legacy"][sample_id],
                "unified": by_mode_sample["unified"][sample_id],
            }
        )
    return {
        "schema": REPORT_SCHEMA,
        "mode": "live-samples",
        "provider": selected_provider or "injected",
        "samples": sample_rows,
        "aggregate": {
            mode: {
                "counts": score["counts"],
                "by_type": score["by_type"],
                "typing_accuracy": score["typing_accuracy"],
            }
            for mode, score in scores.items()
        },
    }


def format_live_sample_table(report: dict[str, Any]) -> str:
    lines = [
        (
            "sample_id | legacy missing/spurious/mis_attached | "
            "unified missing/spurious/mis_attached | legacy typing | unified typing"
        )
    ]
    for item in report["samples"]:
        legacy = item["legacy"]
        unified = item["unified"]
        lines.append(
            f"{item['sample_id']} | {_diff_count_text(legacy['counts'])} | "
            f"{_diff_count_text(unified['counts'])} | "
            f"{legacy['typing']['expected']}->{legacy['typing']['actual']} | "
            f"{unified['typing']['expected']}->{unified['typing']['actual']}"
        )
    return "\n".join(lines)


def load_assertion_results(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    result_path = Path(path)
    try:
        payload = yaml.safe_load(result_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise EvalHarnessError(
            f"could not load assertion results {result_path.name}: {error}"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"samples"}:
        raise EvalHarnessError("assertion results MUST contain only a samples mapping")
    samples = payload["samples"]
    if not isinstance(samples, dict) or not all(
        isinstance(key, str) and isinstance(value, list)
        for key, value in samples.items()
    ):
        raise EvalHarnessError("assertion result samples MUST map ids to assertion arrays")
    return samples


def normalized_title_tokens(value: str) -> frozenset[str]:
    normalized = "".join(
        " " if unicodedata.category(character)[0] in {"P", "S"} else character
        for character in value.casefold()
    )
    return frozenset(normalized.split())


def title_overlap(expected: str, produced: str) -> float:
    left = normalized_title_tokens(expected)
    right = normalized_title_tokens(produced)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def align_matters(
    expected: list[ExpectedMatter], produced: list[ProducedMatter]
) -> list[Alignment]:
    """Greedily align positive-overlap pairs in deterministic priority order."""

    candidates: list[tuple[int, bytes, int, ProducedMatter]] = []
    for expected_index, wanted in enumerate(expected):
        expected_evidence = set(wanted.evidence)
        for actual in produced:
            overlap = len(expected_evidence & actual.evidence)
            if overlap:
                candidates.append(
                    (-overlap, actual.subject_key.encode("utf-8"), expected_index, actual)
                )
    used_expected: set[int] = set()
    used_produced: set[str] = set()
    result: list[Alignment] = []
    for negative_overlap, _, expected_index, actual in sorted(candidates):
        if expected_index in used_expected or actual.subject_key in used_produced:
            continue
        score = title_overlap(expected[expected_index].title, actual.title)
        result.append(
            Alignment(
                expected_index=expected_index,
                subject_key=actual.subject_key,
                overlap=-negative_overlap,
                title_score=score,
                title_match=score >= 0.5,
            )
        )
        used_expected.add(expected_index)
        used_produced.add(actual.subject_key)
    return sorted(result, key=lambda item: item.expected_index)


def score_metrics(
    *,
    expected: list[ExpectedMatter],
    produced: list[ProducedMatter],
    message_rounds: dict[str, int],
    accepted_source_ids: list[list[str]],
    source_to_message: dict[str, str],
    route_counts: dict[str, int] | None = None,
) -> tuple[dict[str, Any], list[Alignment]]:
    alignments = align_matters(expected, produced)
    produced_by_key = {item.subject_key: item for item in produced}
    expected_for_produced = {
        item.subject_key: item.expected_index for item in alignments
    }
    produced_for_expected = {
        item.expected_index: item.subject_key for item in alignments
    }
    ground_truth: dict[str, set[int]] = {key: set() for key in message_rounds}
    for expected_index, matter in enumerate(expected):
        for message_id in matter.evidence:
            ground_truth[message_id].add(expected_index)

    over_split = sum(
        sum(bool(set(matter.evidence) & item.evidence) for item in produced) >= 2
        for matter in expected
    )
    wrong_merge = 0
    for matter in produced:
        exclusive_expected = {
            next(iter(ground_truth[message_id]))
            for message_id in matter.evidence
            if len(ground_truth.get(message_id, set())) == 1
        }
        wrong_merge += len(exclusive_expected) >= 2

    produced_for_message: dict[str, set[str]] = {
        message_id: {
            matter.subject_key
            for matter in produced
            if message_id in matter.evidence
        }
        for message_id in message_rounds
    }
    wrong_attach_messages = {
        message_id
        for message_id, produced_keys in produced_for_message.items()
        if any(
            expected_for_produced[key] not in ground_truth[message_id]
            for key in produced_keys
            if key in expected_for_produced
        )
    }

    eligible_missed: set[str] = set()
    missed_messages: set[str] = set()
    for message_id, expected_indexes in ground_truth.items():
        round_index = message_rounds[message_id]
        for expected_index in expected_indexes:
            aligned_key = produced_for_expected.get(expected_index)
            if aligned_key is None:
                continue
            aligned = produced_by_key[aligned_key]
            prior_expected_evidence = {
                evidence_id
                for evidence_id in expected[expected_index].evidence
                if message_rounds[evidence_id] < round_index
            }
            if not (aligned.evidence & prior_expected_evidence):
                continue
            eligible_missed.add(message_id)
            if message_id in aligned.evidence:
                continue
            if any(
                key != aligned_key
                and produced_by_key[key].first_round == round_index
                for key in produced_for_message[message_id]
            ):
                missed_messages.add(message_id)

    field_metrics: dict[str, dict[str, int | float | None]] = {}
    field_correct = field_total = 0
    for field_name in FIELD_NAMES:
        correct = total = 0
        for expected_index, matter in enumerate(expected):
            if field_name not in matter.declared_fields:
                continue
            total += 1
            produced_key = produced_for_expected.get(expected_index)
            if produced_key is not None and (
                getattr(produced_by_key[produced_key], field_name)
                == matter.declared_fields[field_name]
            ):
                correct += 1
        field_metrics[field_name] = _success_metric(correct, total)
        field_correct += correct
        field_total += total

    valid_source_refs = sum(
        source_id in source_to_message
        for source_ids in accepted_source_ids
        for source_id in source_ids
    )
    source_refs_total = sum(len(source_ids) for source_ids in accepted_source_ids)
    title_matches = sum(item.title_match for item in alignments)
    metrics = {
        "over_split": _failure_metric(over_split, len(expected)),
        "wrong_merge": _failure_metric(wrong_merge, len(produced)),
        "wrong_attach": _failure_metric(
            len(wrong_attach_messages), len(message_rounds)
        ),
        "missed_attach": _failure_metric(
            len(missed_messages), len(eligible_missed)
        ),
        "field_accuracy": {
            "fields": field_metrics,
            "aggregate": _success_metric(field_correct, field_total),
        },
        "evidence_validity": _named_success_metric(
            "valid", valid_source_refs, source_refs_total
        ),
        "title_match_rate": _named_success_metric(
            "matched", title_matches, len(alignments)
        ),
        "zero_model_route_rate": _zero_model_route_rate(route_counts or {}),
    }
    return metrics, alignments


def run_eval_dataset(
    dataset: str | Path | None = None,
    *,
    case_id: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    responses: str | Path | None = None,
    seed_note: bool = False,
    assertion_results: str | Path | None = None,
) -> dict[str, Any]:
    selected_dataset = Path(dataset) if dataset is not None else default_dataset()
    loaded = [(path, load_eval_case(path)) for path in discover_eval_cases(selected_dataset)]
    if case_id is not None:
        loaded = [item for item in loaded if item[1].case_id == case_id]
        if not loaded:
            raise EvalHarnessError(f"eval case not found: {case_id}")
    if responses is not None and len(loaded) != 1:
        raise EvalHarnessError("--responses requires exactly one selected case")

    selected_provider = provider or os.environ.get("MATTERHORN_PROVIDER")
    if selected_provider is None:
        selected_provider = "fixture-file"
    case_reports = []
    for path, case in loaded:
        fixture_path = (
            Path(responses)
            if responses is not None
            else path.with_suffix(".responses.yaml")
        )
        if selected_provider == "fixture-file":
            if not fixture_path.is_file():
                raise EvalHarnessError(
                    f"response fixture not found for {case.case_id}: {fixture_path.name}"
                )
            gateway: Any = EvalFixtureGateway(load_fixture_responses(fixture_path))
        else:
            try:
                gateway = TrackingGateway(
                    configured_gateway(
                        provider=selected_provider,
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                    )
                )
            except Exception as error:
                raise EvalHarnessError(str(error)) from error
        case_reports.append(_run_case(case, gateway))
        if isinstance(gateway, EvalFixtureGateway):
            gateway.assert_consumed()

    report = {
        "schema": REPORT_SCHEMA,
        "provider": selected_provider,
        "seed_note": (
            "Matterhorn does not set a provider seed; record provider-side seed "
            "controls separately when comparing live baselines."
            if seed_note
            else None
        ),
        "cases": case_reports,
        "aggregate": aggregate_case_reports(case_reports),
    }
    if assertion_results is not None:
        report["assertion_samples"] = score_alignment_samples(
            load_assertion_results(assertion_results)
        )
    return report


def _run_case(case: EvalCase, gateway: Any) -> dict[str, Any]:
    scope_id = case.resolved_scope_id
    extractor = RecordingExtractor(gateway, case.schema_profile)
    first_seen_round: dict[str, int] = {}
    cards_accepted = 0
    rejection_reasons: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="matterhorn-eval-") as temp_dir:
        engine = Engine(
            Path(temp_dir) / "eval.db",
            case.schema_profile,
            clock=EvalClock(),
            gateway=gateway,
            extractor=extractor,
        )
        try:
            for round_index, messages in enumerate(case.rounds):
                result = engine.add(scope_id, messages, wait=True)
                if result.status.value != "completed":
                    failure = getattr(gateway, "failure", None)
                    detail = f": {failure}" if failure is not None else ""
                    raise EvalHarnessError(
                        f"gateway failed in {case.case_id} round {round_index + 1}{detail}"
                    )
                cards_accepted += result.cards_produced
                for reason, count in result.gate.rejected.items():
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + count
                for subject in engine.store.subjects(scope_id):
                    first_seen_round.setdefault(subject.subject_key, round_index)

            subject_by_key = {
                subject.subject_key: subject for subject in engine.store.subjects(scope_id)
            }
            produced = []
            source_to_message = _source_to_message(case)
            for matter in engine.matters(scope_id):
                subject = subject_by_key[matter.subject_key]
                evidence = frozenset(
                    source_to_message[source_id]
                    for source_id in subject.source_ids
                    if source_id in source_to_message
                )
                owner: Any
                if len(matter.owners) == 1:
                    owner = matter.owners[0]
                elif not matter.owners:
                    owner = None
                else:
                    owner = matter.owners
                produced.append(
                    ProducedMatter(
                        subject_key=matter.subject_key,
                        title=matter.title,
                        evidence=evidence,
                        first_round=first_seen_round[matter.subject_key],
                        status=matter.status,
                        owner=owner,
                        next_step=matter.next_step,
                    )
                )
            produced.sort(key=lambda item: item.subject_key.encode("utf-8"))
            routing_stats = engine.gate_statistics(scope_id)
            route_counts = {
                name: getattr(routing_stats, name)
                for name in (
                    "route_handle",
                    "route_thread",
                    "route_evidence",
                    "route_model",
                    "route_new",
                    "route_review",
                )
            }
            review_queued = len(engine.review_items(scope_id))
        finally:
            engine.store.close()

    message_rounds = {
        message.id: round_index
        for round_index, messages in enumerate(case.rounds)
        for message in messages
    }
    accepted_source_ids = [
        [ref.source_id for ref in card.source_refs] for card in extractor.cards
    ]
    metrics, alignments = score_metrics(
        expected=case.expected,
        produced=produced,
        message_rounds=message_rounds,
        accepted_source_ids=accepted_source_ids,
        source_to_message=_source_to_message(case),
        route_counts=route_counts,
    )
    return {
        "case_id": case.case_id,
        "title": case.title,
        "stats": {
            "matters_expected": len(case.expected),
            "matters_produced": len(produced),
            "cards_accepted": cards_accepted,
            "gate_rejections": sum(rejection_reasons.values()),
            "gate_rejection_reasons": dict(sorted(rejection_reasons.items())),
            "review_queued": review_queued,
            "route_counts": route_counts,
        },
        "alignment": [
            {
                "expected_index": item.expected_index,
                "expected_title": case.expected[item.expected_index].title,
                "subject_key": item.subject_key,
                "overlap": item.overlap,
                "title_score": item.title_score,
                "title_match": item.title_match,
            }
            for item in alignments
        ],
        "metrics": metrics,
    }


def aggregate_case_reports(case_reports: list[dict[str, Any]]) -> dict[str, Any]:
    stats = {
        key: sum(case["stats"][key] for case in case_reports)
        for key in (
            "matters_expected",
            "matters_produced",
            "cards_accepted",
            "gate_rejections",
        )
    }
    rejection_reasons: dict[str, int] = {}
    for case in case_reports:
        for reason, count in case["stats"]["gate_rejection_reasons"].items():
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + count
    stats["gate_rejection_reasons"] = dict(sorted(rejection_reasons.items()))

    metrics: dict[str, Any] = {}
    for name in ("over_split", "wrong_merge", "wrong_attach", "missed_attach"):
        count = sum(case["metrics"][name]["count"] for case in case_reports)
        total = sum(case["metrics"][name]["total"] for case in case_reports)
        metrics[name] = _failure_metric(count, total)
    field_metrics = {}
    aggregate_correct = aggregate_total = 0
    for field_name in FIELD_NAMES:
        correct = sum(
            case["metrics"]["field_accuracy"]["fields"][field_name]["correct"]
            for case in case_reports
        )
        total = sum(
            case["metrics"]["field_accuracy"]["fields"][field_name]["total"]
            for case in case_reports
        )
        field_metrics[field_name] = _success_metric(correct, total)
        aggregate_correct += correct
        aggregate_total += total
    metrics["field_accuracy"] = {
        "fields": field_metrics,
        "aggregate": _success_metric(aggregate_correct, aggregate_total),
    }
    for name, numerator in (
        ("evidence_validity", "valid"),
        ("title_match_rate", "matched"),
    ):
        correct = sum(
            case["metrics"][name][numerator] for case in case_reports
        )
        total = sum(case["metrics"][name]["total"] for case in case_reports)
        metrics[name] = _named_success_metric(numerator, correct, total)
    aggregate_route_counts = {
        name: sum(case["stats"]["route_counts"][name] for case in case_reports)
        for name in (
            "route_handle",
            "route_thread",
            "route_evidence",
            "route_model",
            "route_new",
            "route_review",
        )
    }
    stats["review_queued"] = sum(
        case["stats"]["review_queued"] for case in case_reports
    )
    stats["route_counts"] = aggregate_route_counts
    metrics["zero_model_route_rate"] = _zero_model_route_rate(
        aggregate_route_counts
    )
    return {"stats": stats, "metrics": metrics}


def format_report_table(report: dict[str, Any]) -> str:
    columns = (
        "case_id",
        "matters_expected",
        "matters_produced",
        "cards_accepted",
        "gate_rejections",
        "review_queued",
        "over_split",
        "wrong_merge",
        "wrong_attach",
        "missed_attach",
        "field_accuracy",
        "evidence_validity",
        "title_match_rate",
    )
    lines = [" | ".join(columns)]
    for case in report["cases"]:
        lines.append(_table_row(case["case_id"], case["stats"], case["metrics"]))
    lines.append(
        _table_row("AGGREGATE", report["aggregate"]["stats"], report["aggregate"]["metrics"])
    )
    zero_model_rate = report["aggregate"]["metrics"]["zero_model_route_rate"]
    lines.append(
        "zero_model_route_rate | "
        + ("n/a" if zero_model_rate is None else f"{zero_model_rate:.3f}")
    )
    if report.get("seed_note"):
        lines.append(f"seed_note | {report['seed_note']}")
    if "assertion_samples" in report:
        sample_scores = report["assertion_samples"]
        counts = sample_scores["counts"]
        lines.append(
            "assertion_set_diff | "
            f"missing={counts['missing']} | spurious={counts['spurious']} | "
            f"mis_attached={counts['mis_attached']}"
        )
        for subject_type in ("matter", "topic"):
            type_counts = sample_scores["by_type"][subject_type]
            lines.append(
                f"assertion_set_diff_{subject_type} | "
                f"missing={type_counts['missing']} | "
                f"spurious={type_counts['spurious']} | "
                f"mis_attached={type_counts['mis_attached']}"
            )
        typing = sample_scores["typing_accuracy"]
        lines.append(
            "typing_accuracy | "
            f"{typing['correct']}/{typing['total']} "
            f"({_rate_text(typing['rate'])})"
        )
    return "\n".join(lines)


def _assertion_subject(assertion: dict[str, Any]) -> Any:
    if "subject_ref" in assertion:
        return assertion["subject_ref"]
    if "subject_key" in assertion:
        return assertion["subject_key"]
    subject = assertion.get("subject")
    if isinstance(subject, dict):
        if subject.get("subject_key") is not None:
            return subject["subject_key"]
        declaration = subject.get("new_subject")
        if isinstance(declaration, dict):
            return NEW_SUBJECT_PREFIX + str(declaration.get("ref"))
    return None


def _assertion_fact(assertion: dict[str, Any]) -> dict[str, Any]:
    evidence = assertion.get("evidence_aliases", assertion.get("evidence", []))
    return {
        "predicate": assertion.get("predicate"),
        "operation": assertion.get("operation", "ASSERT"),
        "object_value": assertion.get("object_value"),
        "evidence": sorted(evidence) if isinstance(evidence, list) else evidence,
    }


def _assertion_type(assertion: dict[str, Any]) -> str:
    explicit = assertion.get("subject_type")
    if isinstance(explicit, str):
        return explicit.upper()
    subject = assertion.get("subject")
    if isinstance(subject, dict):
        declaration = subject.get("new_subject")
        if isinstance(declaration, dict) and isinstance(
            declaration.get("subject_type"), str
        ):
            return declaration["subject_type"].upper()
    if assertion.get("predicate") in {"viewpoint", "stated_by"}:
        return "TOPIC"
    return "MATTER"


def _typing_label(assertions: list[dict[str, Any]]) -> str:
    types = {
        _assertion_type(assertion).casefold()
        for assertion in assertions
        if _assertion_type(assertion) in SCORED_SUBJECT_TYPES
    }
    if not types:
        return "noise"
    return "+".join(sorted(types))


def _typing_result(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]]
) -> dict[str, Any]:
    expected_label = _typing_label(expected)
    actual_label = _typing_label(actual)
    return {
        "expected": expected_label,
        "actual": actual_label,
        "correct": int(expected_label == actual_label),
    }


def _run_live_sample(
    sample: AlignmentSample,
    gateway: Any,
    *,
    unified: bool,
    samples_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    records = [
        Record.model_validate(
            {key: value for key, value in item.items() if key != "evidence_alias"}
        )
        for item in sample.window
    ]
    aliases = {
        item["record_id"]: item.get("evidence_alias", f"m{index + 1}")
        for index, item in enumerate(sample.window)
    }
    with tempfile.TemporaryDirectory(prefix="matterhorn-live-sample-") as temp_dir:
        # Leave-one-out: the sample under evaluation MUST NOT appear in the
        # loop's few-shot exemplars, or the score measures memorization.
        exemplar_dir = Path(temp_dir) / "samples"
        exemplar_dir.mkdir()
        for path in discover_alignment_samples(samples_root):
            if load_alignment_sample(path).sample_id != sample.sample_id:
                shutil.copy(path, exemplar_dir / path.name)
        engine = Engine(
            Path(temp_dir) / "sample.db",
            "org-matters/v1",
            clock=EvalClock(),
            gateway=gateway,
            unified_loop=unified,
            alignment_samples_dir=exemplar_dir,
        )
        try:
            _seed_live_sample_subjects(engine, sample)
            engine.add_records(records, scope_id=sample.scope_id)
            actual = [
                {
                    "subject_ref": assertion.subject_key,
                    "subject_type": assertion.subject_type,
                    "predicate": assertion.predicate,
                    "operation": assertion.operation.value,
                    "object_value": assertion.object_value,
                    "evidence_aliases": sorted(
                        {
                            aliases[ref.source_id]
                            for ref in assertion.source_refs
                            if ref.source_id in aliases
                        }
                    ),
                }
                for assertion in engine.store.assertions(sample.scope_id)
            ]
            return _normalize_live_subject_refs(
                sample.expected_assertions,
                actual,
            )
        finally:
            engine.store.close()


def _seed_live_sample_subjects(engine: Engine, sample: AlignmentSample) -> None:
    expected_by_subject: dict[str, list[dict[str, Any]]] = {}
    for assertion in sample.expected_assertions:
        subject_ref = _assertion_subject(assertion)
        if (
            not isinstance(subject_ref, str)
            or subject_ref.startswith(NEW_SUBJECT_PREFIX)
        ):
            continue
        expected_by_subject.setdefault(subject_ref, []).append(assertion)
    with engine.store.transaction():
        for subject_key, assertions in expected_by_subject.items():
            subject_type = _assertion_type(assertions[0])
            engine.store.upsert_subject(
                SubjectRecord(
                    scope_id=sample.scope_id,
                    subject_key=subject_key,
                    subject_type=subject_type,
                    title=subject_key.replace("-", " ").title(),
                    normalized_title=subject_key.casefold(),
                    source_ids=frozenset(),
                )
            )
        if expected_by_subject:
            engine._rebuild(sample.scope_id, emit_events=False)


def _normalize_live_subject_refs(
    expected: list[dict[str, Any]],
    actual: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_new = sorted(
        {
            subject
            for assertion in expected
            if isinstance((subject := _assertion_subject(assertion)), str)
            and subject.startswith(NEW_SUBJECT_PREFIX)
        }
    )
    actual_subjects = sorted(
        {
            str(_assertion_subject(assertion))
            for assertion in actual
            if _assertion_subject(assertion) is not None
        }
    )
    candidates = []
    for actual_subject in actual_subjects:
        actual_facts = {
            _assertion_signature(item, include_subject=False)
            for item in actual
            if _assertion_subject(item) == actual_subject
        }
        for expected_subject in expected_new:
            expected_facts = {
                _assertion_signature(item, include_subject=False)
                for item in expected
                if _assertion_subject(item) == expected_subject
            }
            overlap = len(actual_facts & expected_facts)
            if overlap:
                candidates.append(
                    (-overlap, actual_subject.encode(), expected_subject.encode(), actual_subject, expected_subject)
                )
    mapped_actual: set[str] = set()
    mapped_expected: set[str] = set()
    mapping: dict[str, str] = {}
    for _, _, _, actual_subject, expected_subject in sorted(candidates):
        if actual_subject in mapped_actual or expected_subject in mapped_expected:
            continue
        mapping[actual_subject] = expected_subject
        mapped_actual.add(actual_subject)
        mapped_expected.add(expected_subject)
    return [
        {
            **assertion,
            "subject_ref": mapping.get(
                str(assertion.get("subject_ref")), assertion.get("subject_ref")
            ),
            "object_value": mapping.get(
                assertion.get("object_value"), assertion.get("object_value")
            )
            if isinstance(assertion.get("object_value"), str)
            else assertion.get("object_value"),
        }
        for assertion in actual
    ]


def _diff_count_text(counts: dict[str, int]) -> str:
    return f"{counts['missing']}/{counts['spurious']}/{counts['mis_attached']}"


def _assertion_signature(
    assertion: dict[str, Any], *, include_subject: bool
) -> str:
    payload = _assertion_fact(assertion)
    if include_subject:
        payload["subject"] = _assertion_subject(assertion)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _table_row(case_id: str, stats: dict[str, Any], metrics: dict[str, Any]) -> str:
    values: list[Any] = [
        case_id,
        stats["matters_expected"],
        stats["matters_produced"],
        stats["cards_accepted"],
        stats["gate_rejections"],
        stats["review_queued"],
        _failure_text(metrics["over_split"]),
        _failure_text(metrics["wrong_merge"]),
        _failure_text(metrics["wrong_attach"]),
        _failure_text(metrics["missed_attach"]),
        _rate_text(metrics["field_accuracy"]["aggregate"]["rate"]),
        _rate_text(metrics["evidence_validity"]["rate"]),
        _rate_text(metrics["title_match_rate"]["rate"]),
    ]
    return " | ".join(str(value) for value in values)


def _source_to_message(case: EvalCase) -> dict[str, str]:
    scope_id = case.resolved_scope_id
    result = {}
    for messages in case.rounds:
        for message in messages:
            container = (
                f"{scope_id}:{message.conversation_id}"
                if message.conversation_id is not None
                else scope_id
            )
            result[f"{container}:{message.id}"] = message.id
    return result


def _failure_metric(count: int, total: int) -> dict[str, int | float | None]:
    return {"count": count, "total": total, "rate": count / total if total else None}


def _zero_model_route_rate(route_counts: dict[str, int]) -> float | None:
    zero_model = sum(
        route_counts.get(name, 0)
        for name in ("route_handle", "route_thread", "route_evidence")
    )
    total = zero_model + sum(
        route_counts.get(name, 0)
        for name in ("route_model", "route_new", "route_review")
    )
    return zero_model / total if total else None


def _success_metric(correct: int, total: int) -> dict[str, int | float | None]:
    return {
        "correct": correct,
        "total": total,
        "rate": correct / total if total else None,
    }


def _named_success_metric(
    numerator: str, correct: int, total: int
) -> dict[str, int | float | None]:
    return {
        numerator: correct,
        "total": total,
        "rate": correct / total if total else None,
    }


def _rate_text(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.3f}"


def _failure_text(metric: dict[str, Any]) -> str:
    return (
        f"{metric['count']}/{metric['total']} "
        f"({_rate_text(metric['rate'])})"
    )


__all__ = [
    "Alignment",
    "EvalCase",
    "EvalHarnessError",
    "ExpectedMatter",
    "ProducedMatter",
    "aggregate_case_reports",
    "align_matters",
    "default_dataset",
    "discover_eval_cases",
    "format_report_table",
    "load_eval_case",
    "run_eval_dataset",
    "score_metrics",
    "title_overlap",
]
