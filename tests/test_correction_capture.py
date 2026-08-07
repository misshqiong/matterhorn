from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from itertools import count

import pytest
import yaml

from matterhorn.canonical import derive_assertion_id, object_key
from matterhorn.contracts import (
    Assertion,
    CorrectionCapture,
    CorrectionCaptureKind,
    CorrectionCaptureStatus,
    EpisodeCard,
    Operation,
    Origin,
    Record,
    ReviewItem,
    SourceRef,
)
from matterhorn.defaults import Engine
from matterhorn.store import SQLiteStore

NOW = datetime(2026, 8, 7, 9, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
def capture_engine(request, tmp_path):
    scope_id = f"octo-capture-e2e-{request.param}"
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "capture.db")
    else:
        dsn = os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("MATTERHORN_TEST_POSTGRES_DSN is unset")
        from matterhorn.store.postgres import PostgresStore

        store = PostgresStore(dsn)
    store.clear_scope(scope_id)
    ticks = count()
    engine = Engine(
        store,
        clock=lambda: NOW + timedelta(hours=1, seconds=next(ticks)),
    )
    try:
        yield engine, scope_id
    finally:
        store.clear_scope(scope_id)
        store.close()


def _source(source_id: str, minute: int, sender: str = "Dana Reyes") -> SourceRef:
    return SourceRef(
        source_id=source_id,
        sent_at=NOW + timedelta(minutes=minute),
        sender=sender,
        excerpt=f"Fictional octo-org evidence {source_id}.",
    )


def _seed(engine: Engine, scope_id: str) -> None:
    engine._ingest_cards_sync(
        [
            {
                "card_id": f"card-{key}",
                "scope_id": scope_id,
                "subject_key": key,
                "date": "2026-08-07",
                "occurred_at": NOW + timedelta(minutes=index),
                "title": f"Fictional {key}",
                "status": "open",
                "source_refs": [
                    _source(f"octo-org:seed:{key}", index).model_dump(mode="json")
                ],
            }
            for index, key in enumerate(
                ("release", "child", "model-parent", "human-parent")
            )
        ]
    )


def _add_model_edge(engine: Engine, scope_id: str) -> Assertion:
    refs = [_source("octo-org:model:placement", 10, "Octo Agent")]
    assertion = Assertion(
        assertion_id=derive_assertion_id(
            scope_id,
            "child",
            "part_of",
            Operation.ASSERT,
            object_key("model-parent"),
            NOW + timedelta(minutes=10),
            refs,
        ),
        scope_id=scope_id,
        subject_key="child",
        subject_type="MATTER",
        predicate="part_of",
        operation=Operation.ASSERT,
        object_value="model-parent",
        object_key=object_key("model-parent"),
        valid_from=NOW + timedelta(minutes=10),
        recorded_at=NOW + timedelta(minutes=10, seconds=30),
        source_refs=refs,
        origin=Origin.model,
    )
    assert engine._structure_rejection(assertion) is None
    with engine.store.transaction():
        engine.store.observe_source(scope_id, refs[0])
        assert engine._add_assertion(assertion)
        engine._rebuild(scope_id)
    return assertion


def _correct_status(engine: Engine, scope_id: str) -> Assertion:
    return engine.correct(
        {
            "scope_id": scope_id,
            "subject_key": "release",
            "subject_type": "MATTER",
            "predicate": "status",
            "operation": "ASSERT",
            "object_value": "completed",
            "valid_from": NOW + timedelta(minutes=20),
            "source_refs": [
                _source("octo-org:human:status", 20).model_dump(mode="json")
            ],
        }
    )


def test_three_human_override_kinds_capture_end_to_end_on_both_backends(
    capture_engine,
) -> None:
    engine, scope_id = capture_engine
    _seed(engine, scope_id)
    staged = Record(
        record_id="octo-org:seed:release",
        container_id="octo-org:seed",
        sent_at=NOW,
        author={"id": "dana", "display_name": "Dana Reyes", "kind": "human"},
        content="Fictional release is open.",
    )
    engine.store.stage_records(scope_id, [staged], staged_at=NOW)

    _correct_status(engine, scope_id)

    review_card = EpisodeCard(
        card_id="held-card",
        scope_id=scope_id,
        date=NOW.date(),
        title="Fictional held card",
        source_refs=[_source("octo-org:review:model", 21, "Octo Agent")],
    )
    engine.store.add_review_item(
        ReviewItem(
            scope_id=scope_id,
            review_id="review-fictional-drop",
            card_json=review_card.model_dump(mode="json"),
            reasons=["FICTIONAL_REVIEW"],
            candidates_json=[{"subject_key": "release", "score": 0.51}],
            created_at=NOW + timedelta(minutes=21),
        )
    )
    engine.resolve_review(
        scope_id,
        "review-fictional-drop",
        action="drop",
        source_refs=[_source("octo-org:review:human", 22)],
    )

    _add_model_edge(engine, scope_id)
    engine.correct(
        {
            "scope_id": scope_id,
            "subject_key": "child",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "operation": "ASSERT",
            "object_value": "human-parent",
            "valid_from": NOW + timedelta(minutes=30),
            "source_refs": [
                _source("octo-org:human:placement", 30).model_dump(mode="json")
            ],
        }
    )

    captures = engine.correction_captures(scope_id)
    assert [capture.kind for capture in captures] == [
        CorrectionCaptureKind.correction,
        CorrectionCaptureKind.review_resolution,
        CorrectionCaptureKind.election_override,
    ]
    assert captures[0].window_json[0]["source"] == "staged_record"
    assert captures[1].model_output_json["card"]["card_id"] == "held-card"
    assert captures[1].human_output_json["action"] == "drop"
    assert captures[2].model_output_json["election"]["elected_target"] == (
        "model-parent"
    )
    assert captures[2].human_output_json["election"]["elected_target"] == (
        "human-parent"
    )


def test_human_detachment_of_a_model_edge_is_captured(capture_engine) -> None:
    """Emptying the slot is the strongest label a wrong placement can get."""

    engine, scope_id = capture_engine
    _seed(engine, scope_id)
    _add_model_edge(engine, scope_id)

    engine.correct(
        {
            "scope_id": scope_id,
            "subject_key": "child",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "operation": "RETRACT",
            "object_value": "model-parent",
            "valid_from": NOW + timedelta(minutes=30),
            "source_refs": [
                _source("octo-org:human:detach", 30).model_dump(mode="json")
            ],
        }
    )

    assert engine.query.current(scope_id, "child", "part_of") == []
    captures = [
        item
        for item in engine.correction_captures(scope_id)
        if item.kind == CorrectionCaptureKind.election_override
    ]
    assert len(captures) == 1
    assert captures[0].model_output_json["election"]["elected_target"] == (
        "model-parent"
    )
    # "No parent" is the human's answer, and the corpus must record it.
    assert captures[0].human_output_json["election"] is None


def test_capture_failure_is_logged_and_does_not_block_correction(
    tmp_path, monkeypatch, caplog
) -> None:
    store = SQLiteStore(tmp_path / "failure.db")
    engine = Engine(store, clock=lambda: NOW + timedelta(hours=1))
    scope_id = "octo-capture-failure-stub"
    _seed(engine, scope_id)

    def fail_capture(capture: CorrectionCapture) -> bool:
        del capture
        raise RuntimeError("fictional capture store failure")

    monkeypatch.setattr(store, "add_correction_capture", fail_capture)
    assertion = _correct_status(engine, scope_id)

    held = EpisodeCard(
        card_id="failure-held-card",
        scope_id=scope_id,
        date=NOW.date(),
        title="Fictional failure review",
        source_refs=[_source("octo-org:failure:review", 21)],
    )
    store.add_review_item(
        ReviewItem(
            scope_id=scope_id,
            review_id="failure-review",
            card_json=held.model_dump(mode="json"),
            reasons=["FICTIONAL_REVIEW"],
            created_at=NOW + timedelta(minutes=21),
        )
    )
    resolved = engine.resolve_review(
        scope_id,
        "failure-review",
        action="drop",
        source_refs=[_source("octo-org:failure:review-human", 22)],
    )
    _add_model_edge(engine, scope_id)
    election_assertion = engine.correct(
        {
            "scope_id": scope_id,
            "subject_key": "child",
            "subject_type": "MATTER",
            "predicate": "part_of",
            "operation": "ASSERT",
            "object_value": "human-parent",
            "valid_from": NOW + timedelta(minutes=30),
            "source_refs": [
                _source("octo-org:failure:placement", 30).model_dump(mode="json")
            ],
        }
    )

    assert assertion.object_value == "completed"
    assert resolved.resolution_json is not None
    assert resolved.resolution_json["action"] == "drop"
    assert election_assertion.object_value == "human-parent"
    assert engine.query.current(scope_id, "release", "status")[0].value == (
        "completed"
    )
    assert engine.matter_graph(scope_id, "child").root_subject_key == "human-parent"
    assert engine.correction_captures(scope_id) == []
    assert caplog.text.count("correction capture failed") >= 3


def test_capture_idempotency_lifecycle_clear_scope_and_export_exclusion(
    capture_engine,
) -> None:
    engine, scope_id = capture_engine
    _seed(engine, scope_id)
    first = _correct_status(engine, scope_id)
    second = _correct_status(engine, scope_id)

    assert first.assertion_id == second.assertion_id
    captures = engine.correction_captures(scope_id)
    assert len(captures) == 1
    capture_id = captures[0].capture_id
    export_payload = engine.export(scope_id).model_dump(mode="json")
    export_text = json.dumps(export_payload)
    assert "correction_captures" not in export_payload
    assert capture_id not in export_text

    resolved = engine.resolve_correction_capture(
        capture_id,
        status="curated",
        resolution_note="fictional-sample-id",
    )
    assert resolved.status == CorrectionCaptureStatus.curated
    assert resolved.resolution_note == "fictional-sample-id"
    with pytest.raises(ValueError, match="already resolved"):
        engine.resolve_correction_capture(
            capture_id,
            status="discarded",
            resolution_note="duplicate",
        )

    engine.store.clear_scope(scope_id)
    assert engine.correction_captures(scope_id) == []


def test_corpus_cli_pending_show_and_resolve(tmp_path) -> None:
    db = tmp_path / "corpus.db"
    store = SQLiteStore(db)
    capture = CorrectionCapture(
        capture_id="capture_cli_fictional",
        scope_id="octo-cli-corpus",
        kind=CorrectionCaptureKind.correction,
        created_at=NOW,
        window_json=[
            {
                "source": "evidence_snapshot",
                "source_ref": _source("octo-org:cli:model", 0).model_dump(
                    mode="json"
                ),
            }
        ],
        model_output_json={"assertions": [{"predicate": "status"}]},
        human_output_json={"assertion": {"object_value": "completed"}},
    )
    assert store.add_correction_capture(capture)
    discarded_capture = capture.model_copy(
        update={
            "capture_id": "capture_cli_discard",
            "created_at": NOW + timedelta(minutes=1),
        }
    )
    assert store.add_correction_capture(discarded_capture)
    store.close()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "matterhorn.cli", *args, "--db", str(db)],
            check=True,
            capture_output=True,
            encoding="utf-8",
        )

    pending = run("corpus", "pending", "--scope", "octo-cli-corpus")
    assert "capture_cli_fictional" in pending.stdout
    assert "capture_cli_discard" in pending.stdout
    assert "correction" in pending.stdout

    shown = run("corpus", "show", "capture_cli_fictional")
    assert "# model_output:" in shown.stdout
    skeleton = yaml.safe_load(shown.stdout)
    assert skeleton["window"] == capture.window_json
    assert skeleton["expected_assertions"] == []

    resolved = run(
        "corpus",
        "resolve",
        "capture_cli_fictional",
        "--curated",
        "fictional-cli-sample",
    )
    assert json.loads(resolved.stdout)["status"] == "curated"
    discarded = run(
        "corpus",
        "resolve",
        "capture_cli_discard",
        "--discard",
        "duplicate fictional sample",
    )
    assert json.loads(discarded.stdout)["status"] == "discarded"
    assert json.loads(discarded.stdout)["resolution_note"] == (
        "duplicate fictional sample"
    )
    final_pending = run("corpus", "pending").stdout
    assert "capture_cli_fictional" not in final_pending
    assert "capture_cli_discard" not in final_pending
