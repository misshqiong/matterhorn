from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from matterhorn import Engine, Record


class SequenceGateway:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def complete(self, **_kwargs):
        self.calls += 1
        return json.dumps(next(self.responses))


def _record(
    *,
    content: str = "Release is blocked.",
    edited_at: str | None = None,
    revoked_at: str | None = None,
):
    return {
        "record_id": "C1:1699887654.123456",
        "native_id": "1699887654.123456",
        "container_id": "C1",
        "thread_id": "C1:1699887654.123456",
        "sent_at": "2026-07-29T09:00:00Z",
        "author": {"id": "U1", "display_name": "Ada", "kind": "human"},
        "content": content,
        "uri": "https://matterhorn.slack.com/archives/C1/p1699887654123456",
        "edited_at": edited_at,
        "revoked_at": revoked_at,
        "kind": "revocation" if revoked_at else "message",
        "subtype": "message_deleted" if revoked_at else None,
    }


def _response(status: str):
    return {
        "cards": [
            {
                "date": "2026-07-29",
                "title": "Release",
                "status": status,
                "occurred_at": "2026-07-29T09:00:00Z",
                "source_ids": ["C1:1699887654.123456"],
            }
        ]
    }


def test_record_contract_enforces_container_namespace() -> None:
    with pytest.raises(ValidationError, match="record_id MUST be namespaced"):
        Record.model_validate({**_record(), "record_id": "1699887654.123456"})
    with pytest.raises(ValidationError, match="native component MUST equal"):
        Record.model_validate({**_record(), "native_id": "different"})


def test_add_records_uses_injected_extractor(tmp_path) -> None:
    class StubReport:
        def __init__(self):
            self.cards = []
            self.rejection_counts = {}

    class RecordingExtractor:
        def __init__(self):
            self.calls = []

        def extract(self, **kwargs):
            self.calls.append(kwargs)
            return StubReport()

    extractor = RecordingExtractor()
    engine = Engine(tmp_path / "injected.db", extractor=extractor)
    report = engine.add_records([_record()], scope_id="team", batch_size=3)

    assert extractor.calls == [
        {
            "scope_id": "team",
            "records": [Record.model_validate(_record())],
            "batch_size": 3,
            "anchors": [],
        }
    ]
    assert report.records_processed == 1
    assert report.cards_accepted == 0


def test_engine_offers_only_newest_canonical_anchors_with_bytewise_ties(
    tmp_path,
) -> None:
    class RecordingExtractor:
        def __init__(self):
            self.anchors = None

        def extract(self, **kwargs):
            self.anchors = kwargs["anchors"]
            return SimpleNamespace(cards=[], rejection_counts={})

    extractor = RecordingExtractor()
    engine = Engine(
        tmp_path / "anchors.db",
        extractor=extractor,
        clock=lambda: datetime(2026, 7, 31, 12, tzinfo=UTC),
    )
    engine._ingest_cards_sync(
        [
            {
                "card_id": f"card-{index:02d}",
                "scope_id": "team",
                "subject_key": f"matter-{index:02d}",
                "date": "2026-07-31",
                "title": f"Matter {index:02d}",
                "status": "open",
                "occurred_at": f"2026-07-31T09:{index:02d}:00Z",
                "source_refs": [
                    {
                        "source_id": f"evidence-{index:02d}",
                        "sent_at": f"2026-07-31T09:{index:02d}:00Z",
                        "sender": "Ada",
                    }
                ],
            }
            for index in range(42)
        ]
    )
    engine.merge_subjects(
        "team",
        "matter-41",
        "matter-40",
        source_refs=[
            {
                "source_id": "anchor-merge",
                "sent_at": "2026-07-31T10:00:00Z",
                "sender": "Ada",
            }
        ],
        valid_from="2026-07-31T10:00:00Z",
    )
    engine.add_records([_record()], scope_id="team")

    assert extractor.anchors is not None
    assert len(extractor.anchors) == 40
    assert extractor.anchors[0].subject_key == "matter-40"
    assert extractor.anchors[-1].subject_key == "matter-01"
    assert "matter-41" not in {
        anchor.subject_key for anchor in extractor.anchors
    }


def test_record_edit_appends_and_delete_revokes_without_removing(tmp_path) -> None:
    gateway = SequenceGateway([_response("blocked"), _response("open")])
    engine = Engine(
        tmp_path / "records.db",
        "org-matters/v1",
        gateway=gateway,
        clock=[
            datetime(2026, 7, 29, 9, 1, tzinfo=UTC),
            datetime(2026, 7, 29, 9, 6, tzinfo=UTC),
        ],
    )
    first = engine.add_records(
        [_record()],
        scope_id="team",
        cursors={"C1": "page-1"},
    )
    edited = engine.add_records(
        [_record(content="Release is open.", edited_at="2026-07-29T09:05:00Z")],
        scope_id="team",
        cursors={"C1": "page-2"},
    )
    subject = engine.query.list_matters("team")[0]
    before_delete = engine.query.current("team", subject.subject_key, "status")[0]
    assertions_before = engine.store.assertions("team")

    deleted = engine.add_records(
        [
            _record(
                content="Release is open.",
                edited_at="2026-07-29T09:05:00Z",
                revoked_at="2026-07-29T09:10:00Z",
            )
        ],
        scope_id="team",
        cursors={"C1": "page-3"},
    )
    after_delete = engine.query.current("team", subject.subject_key, "status")[0]

    assert first.assertions_emitted == edited.assertions_emitted == 1
    assert gateway.calls == 2
    status_assertions = [
        item for item in assertions_before if item.predicate == "status"
    ]
    assert {
        item.object_value: item.recorded_at for item in status_assertions
    } == {
        "open": datetime(2026, 7, 29, 9, 6, tzinfo=UTC),
        "blocked": datetime(2026, 7, 29, 9, 1, tzinfo=UTC),
    }
    assert len({item.observation_id for item in status_assertions}) == 2
    assert before_delete.value == after_delete.value == "open"
    assert before_delete.evidence_status == "active"
    assert deleted.records_revoked == 1
    assert deleted.assertions_emitted == 0
    assert engine.store.assertions("team") == assertions_before
    assert after_delete.evidence_status == "revoked"
    assert after_delete.source_refs[0].status.value == "revoked"
    assert after_delete.source_refs[0].uri.endswith("p1699887654123456")


def test_edit_metadata_alone_creates_new_assertion_for_same_fact(tmp_path) -> None:
    gateway = SequenceGateway([_response("open"), _response("open")])
    engine = Engine(
        tmp_path / "same-fact-edit.db",
        "org-matters/v1",
        gateway=gateway,
        clock=[
            datetime(2026, 7, 29, 9, 1, tzinfo=UTC),
            datetime(2026, 7, 29, 9, 6, tzinfo=UTC),
        ],
    )
    engine.add_records(
        [_record(content="Release is open.")],
        scope_id="team",
    )
    engine.add_records(
        [
            _record(
                content="Release is open.",
                edited_at="2026-07-29T09:05:00Z",
            )
        ],
        scope_id="team",
    )

    assertions = [
        item
        for item in engine.store.assertions("team")
        if item.predicate == "status"
    ]
    assert len(assertions) == 2
    assert {item.object_value for item in assertions} == {"open"}
    assert len({item.assertion_id for item in assertions}) == 2
    assert len({item.observation_id for item in assertions}) == 2
    assert len({item.recorded_at for item in assertions}) == 2


def test_overlapping_windows_are_noop_and_backfill_does_not_move_cursor(tmp_path) -> None:
    gateway = SequenceGateway(
        [_response("open"), _response("open"), _response("open")]
    )
    engine = Engine(tmp_path / "sync.db", "org-matters/v1", gateway=gateway)
    record = _record(content="Release is open.")
    engine.add_records(
        [record],
        scope_id="team",
        cursors={"C1": "next-page"},
    )
    snapshot = (
        engine.store.assertions("team"),
        engine.store.intervals("team"),
        engine.store.record_observations("team"),
        engine.sync_positions("team"),
    )
    overlap = engine.add_records(
        [record],
        scope_id="team",
        cursors={"C1": "should-not-replace"},
    )
    assert overlap.records_processed == 0
    assert overlap.records_skipped == 1
    assert overlap.cards_accepted == 0
    assert overlap.assertions_emitted == 0
    assert gateway.calls == 1
    assert (
        engine.store.assertions("team"),
        engine.store.intervals("team"),
        engine.store.record_observations("team"),
        engine.sync_positions("team"),
    ) == snapshot

    backfill_record = {
        **record,
        "record_id": "C1:1699000000.000001",
        "native_id": "1699000000.000001",
        "thread_id": "C1:1699000000.000001",
        "sent_at": "2026-07-01T09:00:00Z",
    }
    backfill = engine.add_records(
        [backfill_record],
        scope_id="team",
        cursors={"C1": "backfill-page"},
        backfill=True,
    )
    assert backfill.records_processed == 1
    assert engine.sync_positions("team")[0].cursor == "next-page"

    event_record = {
        **record,
        "record_id": "C1:1699999999.000001",
        "native_id": "1699999999.000001",
        "thread_id": "C1:1699999999.000001",
        "sent_at": "2026-07-30T09:00:00Z",
    }
    event = engine.add_records([event_record], scope_id="team")
    assert event.records_processed == 1
    assert engine.sync_positions("team")[0].cursor == "next-page"


def test_thread_identity_precedes_title_and_cross_thread_merge_needs_two_sources(
    tmp_path,
) -> None:
    engine = Engine(tmp_path / "identity.db", "org-matters/v1")

    def card(card_id, title, thread_id, source_ids):
        return {
            "card_id": card_id,
            "scope_id": "s",
            "date": "2026-07-29",
            "title": title,
            "status": "open",
            "thread_id": thread_id,
            "source_refs": [
                {
                    "source_id": source_id,
                    "sent_at": "2026-07-29T09:00:00Z",
                    "sender": "Ada",
                }
                for source_id in source_ids
            ],
        }

    engine._ingest_cards_sync(
        [
            card("a", "Same title", "C1:t1", ["C1:a", "C1:b"]),
            card("b", "Same title", "C1:t2", ["C1:c"]),
        ]
    )
    assert len(engine.query.list_matters("s")) == 2

    engine._ingest_cards_sync(
        [
            card(
                "c",
                "Different title",
                "C1:t3",
                ["C1:a", "C1:b", "C1:d"],
            )
        ]
    )
    assert len(engine.query.list_matters("s")) == 2
