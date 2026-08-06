from __future__ import annotations

import json
from pathlib import Path

import pytest

from matterhorn.distill import ToolLoopResult
from matterhorn.evalrunner import (
    ExpectedMatter,
    ProducedMatter,
    align_matters,
    default_dataset,
    discover_alignment_samples,
    load_alignment_sample,
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

    assert len(samples) == 5
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
    }
    assert scores["typing_accuracy"] == {
        "correct": 5,
        "total": 5,
        "rate": 1.0,
    }
    assert scores["by_type"] == {
        "matter": {"missing": 0, "spurious": 0, "mis_attached": 0},
        "topic": {"missing": 0, "spurious": 0, "mis_attached": 0},
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

    assert diff["counts"] == {"missing": 1, "spurious": 1, "mis_attached": 1}
    assert diff["mis_attached"][0]["expected_subject"] == "root-a"
    assert diff["mis_attached"][0]["actual_subject"] == "wrong-root"


class _LiveSampleGateway:
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
        del system, user, tools, max_tool_calls, max_emissions
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
    samples.mkdir()
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

    report = run_live_sample_comparison(
        samples=samples,
        gateway_factory=_LiveSampleGateway,
    )

    for mode in ("legacy", "unified"):
        assert report["aggregate"][mode]["counts"] == {
            "missing": 0,
            "spurious": 0,
            "mis_attached": 0,
        }
        assert report["aggregate"][mode]["typing_accuracy"]["rate"] == 1.0


def test_theme_rediscovery_fixture_recovers_both_stripped_groups() -> None:
    report = run_theme_rediscovery()

    assert report["mode"] == "theme-rediscovery"
    assert report["score"] == {"correct": 10, "total": 10, "fraction": 1.0}
    assert report["pass"]["edges_applied"] == 10
    assert report["pass"]["roots_created"] == 2
