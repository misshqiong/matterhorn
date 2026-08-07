from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from matterhorn.capacity import LossWeights
from matterhorn.distill import ToolLoopResult
from matterhorn.engine.unified_loop import alignment_samples
from matterhorn.evalrunner import (
    EvalHarnessError,
    ExpectedMatter,
    ProducedMatter,
    align_matters,
    default_dataset,
    discover_alignment_samples,
    discover_testset_samples,
    format_live_sample_table,
    format_report_table,
    load_alignment_sample,
    load_corpus_partitions,
    run_eval_dataset,
    run_live_sample_comparison,
    run_theme_rediscovery,
    score_alignment_samples,
    score_assertion_set,
    score_metrics,
    title_overlap,
)


def _expected(title: str, evidence: list[str], **fields) -> ExpectedMatter:
    return ExpectedMatter(title=title, evidence=evidence, **fields)


def test_alignment_prefers_overlap_then_bytewise_subject_key() -> None:
    expected = [
        _expected("First", ["m"]),
        _expected("Second", ["m"]),
    ]
    produced = [
        ProducedMatter("sub_é", "Second", frozenset({"m"}), 0),
        ProducedMatter("sub_z", "First", frozenset({"m"}), 0),
    ]

    aligned = align_matters(expected, produced)

    assert [(item.expected_index, item.subject_key) for item in aligned] == [
        (0, "sub_z"),
        (1, "sub_é"),
    ]


def test_title_overlap_is_normalized_jaccard_and_secondary() -> None:
    assert title_overlap("PO-7301 Lab Monitor Purchase", "Purchase PO 7301") == pytest.approx(
        3 / 5
    )
    expected = [_expected("Completely different title", ["m1", "m2"])]
    produced = [
        ProducedMatter(
            "sub_a",
            "No shared title token",
            frozenset({"m1", "m2"}),
            0,
        )
    ]
    alignment = align_matters(expected, produced)
    assert alignment[0].overlap == 2
    assert alignment[0].title_match is False


def test_failure_metrics_and_rates_cover_each_routing_mode() -> None:
    expected = [
        _expected(
            "Alpha migration",
            ["a1", "a2", "a3"],
            status="done",
            owner="dana",
            next_step="Archive",
        ),
        _expected(
            "Beta budget",
            ["b1", "b2", "b3", "shared"],
            status="open",
        ),
    ]
    produced = [
        ProducedMatter(
            "sub_a",
            "Alpha migration",
            frozenset({"a1", "a2", "b3"}),
            0,
            status="done",
            owner="dana",
            next_step="Archive",
        ),
        ProducedMatter(
            "sub_b",
            "Beta budget",
            frozenset({"b1", "b2", "shared"}),
            0,
            status="blocked",
        ),
        ProducedMatter(
            "sub_new",
            "Alpha follow-up",
            frozenset({"a3"}),
            1,
            status="done",
        ),
    ]
    message_rounds = {
        "a1": 0,
        "a2": 0,
        "a3": 1,
        "b1": 0,
        "b2": 0,
        "b3": 0,
        "shared": 0,
    }
    metrics, alignment = score_metrics(
        expected=expected,
        produced=produced,
        message_rounds=message_rounds,
        accepted_source_ids=[["src:a1", "bogus"], ["src:b1"]],
        source_to_message={f"src:{key}": key for key in message_rounds},
    )

    assert [(item.expected_index, item.subject_key) for item in alignment] == [
        (0, "sub_a"),
        (1, "sub_b"),
    ]
    assert metrics["over_split"] == {"count": 2, "total": 2, "rate": 1.0}
    assert metrics["wrong_merge"] == {
        "count": 1,
        "total": 3,
        "rate": 1 / 3,
    }
    assert metrics["wrong_attach"] == {
        "count": 1,
        "total": 7,
        "rate": 1 / 7,
    }
    assert metrics["missed_attach"] == {"count": 1, "total": 1, "rate": 1.0}
    assert metrics["field_accuracy"]["fields"]["status"] == {
        "correct": 1,
        "total": 2,
        "rate": 0.5,
    }
    assert metrics["field_accuracy"]["aggregate"] == {
        "correct": 3,
        "total": 4,
        "rate": 0.75,
    }
    assert metrics["evidence_validity"] == {
        "valid": 2,
        "total": 3,
        "rate": 2 / 3,
    }
    assert metrics["title_match_rate"] == {
        "matched": 2,
        "total": 2,
        "rate": 1.0,
    }
    assert metrics["zero_model_route_rate"] is None


def test_shared_message_alone_does_not_make_wrong_merge() -> None:
    expected = [
        _expected("Alpha", ["shared", "a"]),
        _expected("Beta", ["shared", "b"]),
    ]
    produced = [
        ProducedMatter("sub_a", "Alpha", frozenset({"shared", "a"}), 0),
        ProducedMatter("sub_b", "Beta", frozenset({"shared", "b"}), 0),
    ]
    metrics, _ = score_metrics(
        expected=expected,
        produced=produced,
        message_rounds={"shared": 0, "a": 0, "b": 0},
        accepted_source_ids=[],
        source_to_message={},
    )
    assert metrics["wrong_merge"]["count"] == 0


def test_shipped_fixture_dataset_is_deterministic_and_json_serializable(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MATTERHORN_PROVIDER", raising=False)

    first = run_eval_dataset(default_dataset())
    second = run_eval_dataset(default_dataset())

    assert first == second
    assert json.loads(json.dumps(first)) == first
    assert first["schema"] == "matterhorn-eval/v1"
    assert len(first["cases"]) == 8
    assert first["aggregate"]["stats"] == {
        "matters_expected": 11,
        "matters_produced": 9,
        "cards_accepted": 21,
        "gate_rejections": 1,
        "gate_rejection_reasons": {"SOURCE_NOT_TRACEABLE": 1},
        "review_queued": 1,
        "route_counts": {
            "route_handle": 8,
            "route_thread": 0,
            "route_evidence": 0,
            "route_model": 4,
            "route_new": 9,
            "route_review": 0,
        },
    }
    assert first["aggregate"]["metrics"]["zero_model_route_rate"] == pytest.approx(
        8 / 21
    )
    for name in ("over_split", "wrong_merge", "wrong_attach"):
        assert first["aggregate"]["metrics"][name]["count"] > 0
    assert first["aggregate"]["metrics"]["missed_attach"]["count"] == 0
    table = format_report_table(first)
    assert "loss_exemplar | n/a" in table
    assert "loss_test | n/a" in table


def test_every_shipped_case_has_a_sibling_response_fixture() -> None:
    cases = [
        path
        for path in Path(default_dataset()).glob("*.yaml")
        if not path.name.endswith(".responses.yaml")
    ]
    assert len(cases) >= 8
    assert all(path.with_suffix(".responses.yaml").is_file() for path in cases)


def test_shipped_alignment_samples_cover_mail_im_and_agent() -> None:
    samples = [load_alignment_sample(path) for path in discover_alignment_samples()]

    # The library grows with every acceptance round: assert coverage and
    # validity, never an exact count.
    assert len(samples) >= 5
    assert {sample.source_kind for sample in samples} == {"mail", "im", "agent"}
    produced = {
        sample.sample_id: sample.expected_assertions
        for sample in samples
    }
    scores = score_alignment_samples(produced)
    assert scores["counts"] == {
        "missing": 0,
        "spurious": 0,
        "mis_attached": 0,
        "mis_structured": 0,
        "mis_typed": 0,
    }
    assert scores["typing_accuracy"] == {
        "correct": len(samples),
        "total": len(samples),
        "rate": 1.0,
    }
    assert scores["by_type"] == {
        "matter": {
            "missing": 0,
            "spurious": 0,
            "mis_attached": 0,
            "mis_structured": 0,
        },
        "topic": {
            "missing": 0,
            "spurious": 0,
            "mis_attached": 0,
            "mis_structured": 0,
        },
    }


def test_assertion_set_diff_separates_missing_spurious_and_misattached() -> None:
    expected = [
        {
            "subject_ref": "root-a",
            "predicate": "progress",
            "operation": "ASSERT",
            "object_value": "Verified.",
            "evidence_aliases": ["m1"],
        },
        {
            "subject_ref": "root-b",
            "predicate": "status",
            "operation": "ASSERT",
            "object_value": "open",
            "evidence_aliases": ["m2"],
        },
    ]
    actual = [
        {**expected[0], "subject_ref": "wrong-root"},
        {
            "subject_ref": "extra-root",
            "predicate": "decision",
            "operation": "ASSERT",
            "object_value": "Proceed.",
            "evidence_aliases": ["m3"],
        },
    ]

    diff = score_assertion_set(expected, actual)

    assert diff["counts"] == {
        "missing": 1,
        "spurious": 1,
        "mis_attached": 1,
        "mis_structured": 0,
    }
    assert diff["mis_attached"][0]["expected_subject"] == "root-a"
    assert diff["mis_attached"][0]["actual_subject"] == "wrong-root"


def test_weighted_loss_counts_structure_and_typing_errors(tmp_path) -> None:
    sample_path = tmp_path / "loss-sample.yaml"
    expected = [
        {
            "subject_ref": "matter-a",
            "subject_type": "MATTER",
            "predicate": "progress",
            "object_value": "Verified",
            "evidence_aliases": ["m1"],
        },
        {
            "subject_ref": "matter-b",
            "subject_type": "MATTER",
            "predicate": "status",
            "object_value": "open",
            "evidence_aliases": ["m1"],
        },
        {
            "subject_ref": "matter-a",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "object_value": "portfolio-a",
            "evidence_aliases": ["m1"],
        },
    ]
    actual = [
        {**expected[0], "subject_ref": "matter-c"},
        {**expected[2], "object_value": "portfolio-b"},
        {
            "subject_ref": "topic-extra",
            "subject_type": "TOPIC",
            "predicate": "viewpoint",
            "object_value": "A fictional extra view",
            "evidence_aliases": ["m1"],
        },
    ]
    # Use YAML's JSON compatibility to retain the exact assertion fixture.
    sample_path.write_text(
        "sample_id: loss-sample\nsource_kind: im\nscope_id: fictional-loss\n"
        "window:\n  - {record_id: fictional-loss:room:r1, container_id: "
        "fictional-loss:room, sent_at: '2026-08-07T09:00:00Z', author: "
        "{id: dana, kind: human}, content: Fictional input, kind: im}\n"
        f"expected_assertions: {json.dumps(expected)}\n",
        encoding="utf-8",
    )

    score = score_alignment_samples(
        {"loss-sample": actual},
        sample_paths=[sample_path],
        loss_weights=LossWeights(
            missing=2,
            spurious=3,
            mis_attached=5,
            mis_typed=7,
            mis_structured=11,
        ),
    )

    assert score["counts"] == {
        "missing": 1,
        "spurious": 1,
        "mis_attached": 1,
        "mis_structured": 1,
        "mis_typed": 1,
    }
    assert score["loss"] == 28.0


def test_corpus_partitions_reject_duplicate_ids_and_testset_as_exemplars(
    tmp_path,
) -> None:
    root = tmp_path / "eval"
    samples = root / "samples"
    testset = root / "testset"
    samples.mkdir(parents=True)
    testset.mkdir()
    fixture = """sample_id: duplicate-sample
source_kind: agent
scope_id: fictional-partition
window: [{record_id: r1}]
expected_assertions: []
"""
    (samples / "sample.yaml").write_text(fixture, encoding="utf-8")
    (testset / "heldout.yaml").write_text(fixture, encoding="utf-8")

    with pytest.raises(EvalHarnessError, match="duplicate sample_id across"):
        load_corpus_partitions(root)
    with pytest.raises(ValueError, match="cannot be used as alignment exemplars"):
        alignment_samples("agent", samples_dir=testset)


def test_shipped_testset_is_fictional_and_disjoint() -> None:
    partitions = load_corpus_partitions()

    assert discover_testset_samples()
    assert {sample.sample_id for _, sample in partitions["exemplar"]}.isdisjoint(
        sample.sample_id for _, sample in partitions["test"]
    )


def test_eval_report_prints_numeric_loss_for_both_scored_partitions(
    tmp_path,
) -> None:
    partitions = load_corpus_partitions()
    produced = {
        sample.sample_id: sample.expected_assertions
        for rows in partitions.values()
        for _, sample in rows
    }
    result_path = tmp_path / "assertions.yaml"
    result_path.write_text(json.dumps({"samples": produced}), encoding="utf-8")

    report = run_eval_dataset(assertion_results=result_path)
    table = format_report_table(report)

    assert report["corpus"]["exemplar"]["loss"] == 0.0
    assert report["corpus"]["test"]["loss"] == 0.0
    assert "loss_exemplar | 0.000" in table
    assert "loss_test | 0.000" in table


class _LiveSampleGateway:
    systems: ClassVar[list[str]] = []

    def complete(self, *, system, user, response_schema):
        del system
        properties = response_schema.get("properties", {})
        if "cards" in properties:
            alias = json.loads(user)["records"][0]["source_alias"]
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-08-06",
                            "title": "Fictional live sample",
                            "status": "open",
                            "source_ids": [alias],
                        }
                    ]
                }
            )
        if "candidates" in properties:
            return '{"candidates":[]}'
        raise AssertionError(response_schema)

    def tool_loop(
        self,
        *,
        system,
        user,
        tools,
        handler,
        max_tool_calls=16,
        max_emissions=4,
    ):
        del user, tools, max_tool_calls, max_emissions
        self.systems.append(system)
        handler(
            "emit",
            {
                "assertions": [
                    {
                        "subject": {
                            "new_subject": {
                                "ref": "live",
                                "subject_type": "MATTER",
                                "title": "Fictional live sample",
                            }
                        },
                        "predicate": "status",
                        "operation": "ASSERT",
                        "object_value": "open",
                        "evidence_aliases": ["m1"],
                    }
                ]
            },
        )
        return ToolLoopResult(
            final_message="done", tool_calls=1, emissions=1
        )


def test_live_samples_runs_legacy_and_unified_with_mocked_gateway(
    tmp_path,
) -> None:
    samples = tmp_path / "samples"
    testset = tmp_path / "testset"
    samples.mkdir()
    testset.mkdir()
    (samples / "01-live.yaml").write_text(
        """sample_id: live-sample
source_kind: im
scope_id: live-scope
window:
  - evidence_alias: m1
    record_id: live-scope:room:r1
    container_id: live-scope:room
    sent_at: '2026-08-06T09:00:00Z'
    author: {id: dana-reyes, display_name: Dana Reyes, kind: human}
    content: Open the fictional live sample.
    kind: im
expected_assertions:
  - subject_ref: '$new:live'
    subject_type: MATTER
    predicate: status
    operation: ASSERT
    object_value: open
    evidence_aliases: [m1]
""",
        encoding="utf-8",
    )
    (testset / "01-heldout.yaml").write_text(
        """sample_id: heldout-live-sample
source_kind: im
scope_id: heldout-live-scope
window:
  - evidence_alias: m1
    record_id: heldout-live-scope:room:r1
    container_id: heldout-live-scope:room
    sent_at: '2026-08-06T10:00:00Z'
    author: {id: dana-reyes, display_name: Dana Reyes, kind: human}
    content: Open the fictional held-out sample; this text is not an exemplar.
    kind: im
expected_assertions:
  - subject_ref: '$new:live'
    subject_type: MATTER
    predicate: status
    operation: ASSERT
    object_value: open
    evidence_aliases: [m1]
""",
        encoding="utf-8",
    )
    _LiveSampleGateway.systems.clear()

    report = run_live_sample_comparison(
        samples=samples,
        testset=testset,
        gateway_factory=_LiveSampleGateway,
    )

    for partition in ("exemplar", "test"):
        for mode in ("legacy", "unified"):
            aggregate = report["partitions"][partition]["aggregate"][mode]
            assert aggregate["counts"] == {
                "missing": 0,
                "spurious": 0,
                "mis_attached": 0,
                "mis_structured": 0,
                "mis_typed": 0,
            }
            assert aggregate["typing_accuracy"]["rate"] == 1.0
            assert aggregate["loss"] == 0.0
    assert _LiveSampleGateway.systems
    assert all("heldout-live-sample" not in system for system in _LiveSampleGateway.systems)
    assert all("this text is not an exemplar" not in system for system in _LiveSampleGateway.systems)
    table = format_live_sample_table(report)
    assert "loss_exemplar | legacy=0.000 | unified=0.000" in table
    assert "loss_test | legacy=0.000 | unified=0.000" in table


def test_theme_rediscovery_fixture_recovers_both_stripped_groups() -> None:
    report = run_theme_rediscovery()

    assert report["mode"] == "theme-rediscovery"
    assert report["score"] == {"correct": 10, "total": 10, "fraction": 1.0}
    assert report["pass"]["edges_applied"] == 10
    assert report["pass"]["roots_created"] == 2
