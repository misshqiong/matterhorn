from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from matterhorn.contracts import EpisodeCard, SchemaProfile
from matterhorn.distill import GateReason, validate_response
from matterhorn.engine.identity import SubjectRecord


def _profile() -> SchemaProfile:
    return SchemaProfile.model_validate(
        {
            "schema": "gate/v1",
            "subjects": [
                {"type": "THING", "primary": True},
                {"type": "CHILD", "parent": "THING"},
                {"type": "OTHER"},
            ],
            "predicates": [
                {
                    "name": "state",
                    "subject": "THING",
                    "cardinality": "SINGLE",
                    "extraction": "deterministic",
                    "source_field": "status",
                },
                {
                    "name": "signal",
                    "subject": "THING",
                    "cardinality": "SINGLE",
                    "extraction": "semantic",
                    "semantic_filter": "conservative",
                    "value_domain": ["yes", "no"],
                },
                {
                    "name": "child_signal",
                    "subject": "CHILD",
                    "cardinality": "SINGLE",
                    "extraction": "semantic",
                },
            ],
            "semantic": {"conservative_confidence_threshold": 0.8},
        }
    )


def _card() -> EpisodeCard:
    return EpisodeCard.model_validate(
        {
            "card_id": "c1",
            "scope_id": "s",
            "subject_key": "thing-1",
            "date": "2026-01-01",
            "title": "Thing",
            "source_refs": [
                {
                    "source_id": "m1",
                    "sent_at": "2026-01-01T10:00:00Z",
                    "sender": "u",
                }
            ],
        }
    )


def _subject() -> SubjectRecord:
    return SubjectRecord(
        "s", "thing-1", "THING", "Thing", "thing", frozenset({"m1"})
    )


def _candidate(**updates):
    value = {
        "subject_key": "thing-1",
        "subject_type": "THING",
        "predicate": "signal",
        "operation": "ASSERT",
        "object_value": "yes",
        "valid_from": "2026-01-01T10:00:00Z",
        "source_ids": ["m1"],
        "confidence": 0.95,
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("raw", "candidate", "reason"),
    [
        ("not-json", None, GateReason.UNPARSEABLE),
        (None, {"predicate": "invented"}, GateReason.UNREGISTERED_PREDICATE),
        (None, {"predicate": "state"}, GateReason.NOT_SEMANTIC),
        (None, {"subject_type": "OTHER"}, GateReason.SUBJECT_TYPE_MISMATCH),
        (None, {"subject_key": "missing"}, GateReason.UNKNOWN_SUBJECT),
        (None, {"source_ids": []}, GateReason.NO_SOURCES),
        (
            None,
            {"source_ids": ["fabricated-message"]},
            GateReason.SOURCE_NOT_TRACEABLE,
        ),
        (None, {"object_value": "maybe"}, GateReason.VALUE_OUT_OF_DOMAIN),
        (None, {"confidence": 0.79}, GateReason.LOW_CONFIDENCE),
        (
            None,
            {"valid_from": "2026-01-02T00:00:00Z"},
            GateReason.VALID_FROM_OUT_OF_WINDOW,
        ),
    ],
)
def test_gate_rejects_each_named_reason(raw, candidate, reason) -> None:
    if raw is None:
        raw = json.dumps({"candidates": [_candidate(**candidate)]})
    report = validate_response(
        raw,
        card=_card(),
        profile=_profile(),
        subjects=[_subject()],
    )
    assert report.accepted_count == 0
    assert [item.reason for item in report.rejections] == [reason]


def test_gate_accepts_traceable_in_domain_candidate() -> None:
    report = validate_response(
        json.dumps({"candidates": [_candidate()]}),
        card=_card(),
        profile=_profile(),
        subjects=[_subject()],
    )
    assert report.rejected_count == 0
    assert report.accepted[0].source_refs[0].source_id == "m1"


@pytest.mark.parametrize(
    ("updates", "subjects", "reason"),
    [
        (
            {
                "subject_key": None,
                "subject_type": "CHILD",
                "predicate": "child_signal",
                "parent_subject_key": "missing",
                "subject_title": "New child",
            },
            [_subject()],
            GateReason.UNKNOWN_PARENT_SUBJECT,
        ),
        (
            {
                "subject_key": None,
                "subject_type": "CHILD",
                "predicate": "child_signal",
                "parent_subject_key": "other-1",
                "subject_title": "New child",
            },
            [
                _subject(),
                SubjectRecord(
                    "s",
                    "other-1",
                    "OTHER",
                    "Other",
                    "other",
                    frozenset({"m1"}),
                ),
            ],
            GateReason.INVALID_SUBJECT_PARENT,
        ),
        (
            {
                "subject_key": None,
                "subject_type": "CHILD",
                "predicate": "child_signal",
                "parent_subject_key": "thing-1",
                "subject_title": "   ",
            },
            [_subject()],
            GateReason.MISSING_SUBJECT_TITLE,
        ),
    ],
)
def test_gate_rejects_invalid_child_creation(updates, subjects, reason) -> None:
    report = validate_response(
        json.dumps({"candidates": [_candidate(**updates)]}),
        card=_card(),
        profile=_profile(),
        subjects=subjects,
    )
    assert report.accepted_count == 0
    assert [item.reason for item in report.rejections] == [reason]


def test_gate_derives_child_key_and_still_requires_traceable_sources() -> None:
    child = _candidate(
        subject_key=None,
        subject_type="CHILD",
        predicate="child_signal",
        parent_subject_key="thing-1",
        subject_title="New Child!",
    )
    accepted = validate_response(
        json.dumps({"candidates": [child]}),
        card=_card(),
        profile=_profile(),
        subjects=[_subject()],
    )
    assert accepted.accepted_count == 1
    assert accepted.accepted[0].create_subject is True
    assert accepted.accepted[0].candidate.subject_key.startswith("sub_")

    child["source_ids"] = ["invented"]
    rejected = validate_response(
        json.dumps({"candidates": [child]}),
        card=_card(),
        profile=_profile(),
        subjects=[_subject()],
    )
    assert [item.reason for item in rejected.rejections] == [
        GateReason.SOURCE_NOT_TRACEABLE
    ]


def test_rejected_candidate_does_not_abort_valid_sibling() -> None:
    report = validate_response(
        json.dumps(
            {
                "candidates": [
                    _candidate(source_ids=["invented"]),
                    _candidate(object_value="no"),
                ]
            }
        ),
        card=_card(),
        profile=_profile(),
        subjects=[_subject()],
    )
    assert report.accepted_count == 1
    assert report.rejected_count == 1
    assert report.rejections[0].reason == GateReason.SOURCE_NOT_TRACEABLE
    assert report.model_dump()["rejection_counts"] == {
        "SOURCE_NOT_TRACEABLE": 1
    }
