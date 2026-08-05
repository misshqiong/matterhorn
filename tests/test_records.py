from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from matterhorn import Engine, Record
from matterhorn.canonical import stable_hash


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
            "context": [],
            "batch_size": 3,
            "anchors": [],
        }
    ]
    assert report.records_processed == 1
    assert report.cards_accepted == 0


def test_add_records_stages_before_extractor_and_latest_edit_wins(tmp_path) -> None:
    class FailingExtractor:
        def extract(self, **_kwargs):
            raise RuntimeError("fictional extraction failure")

    engine = Engine(tmp_path / "direct-staging.db", extractor=FailingExtractor())

    with pytest.raises(RuntimeError, match="fictional extraction failure"):
        engine.add_records([_record()], scope_id="team")
    with pytest.raises(RuntimeError, match="fictional extraction failure"):
        engine.add_records(
            [_record(content="Edited raw content")],
            scope_id="team",
        )

    staged = engine.store.staged_records(
        "team",
        "C1",
        sent_at_from=datetime(2026, 7, 28, tzinfo=UTC),
        sent_at_before=datetime(2026, 7, 30, tzinfo=UTC),
        thread_id="C1:1699887654.123456",
        exclude_record_ids=[],
    )
    assert [record.content for record in staged] == ["Edited raw content"]


def test_revoked_add_records_is_staged_but_never_returned_as_context(tmp_path) -> None:
    engine = Engine(tmp_path / "revoked-staging.db")
    revoked_at = datetime(2026, 7, 29, 9, 10, tzinfo=UTC)

    report = engine.add_records(
        [_record(revoked_at=revoked_at.isoformat())],
        scope_id="team",
    )

    assert report.records_revoked == 1
    assert engine.store.staged_records(
        "team",
        "C1",
        sent_at_from=datetime(2026, 7, 28, tzinfo=UTC),
        sent_at_before=datetime(2026, 7, 30, tzinfo=UTC),
        thread_id="C1:1699887654.123456",
        exclude_record_ids=[],
    ) == []
    assert engine.purge_staging(
        "team",
        as_of=datetime(2026, 8, 6, 9, 1, tzinfo=UTC),
    ) == 1


def test_record_extractor_runs_without_store_lock_held(tmp_path) -> None:
    class LockProbeExtractor:
        def __init__(self):
            self.store = None

        def extract(self, **_kwargs):
            assert self.store is not None
            assert not self.store._lock._is_owned()
            return SimpleNamespace(cards=[], rejection_counts={})

    extractor = LockProbeExtractor()
    engine = Engine(tmp_path / "lock-probe.db", extractor=extractor)
    extractor.store = engine.store

    report = engine.add_records([_record()], scope_id="fictional-team")

    assert report.records_processed == 1


def test_add_records_retry_skips_only_committed_chunks(tmp_path) -> None:
    class FailSecondChunkOnce:
        def __init__(self):
            self.calls = []
            self.failed = False

        def extract(self, **kwargs):
            container_id = kwargs["records"][0].container_id
            self.calls.append(container_id)
            if container_id == "fictional-b" and not self.failed:
                self.failed = True
                raise RuntimeError("temporary extractor failure")
            return SimpleNamespace(cards=[], rejection_counts={})

    records = [
        {
            "record_id": "fictional-a:m1",
            "native_id": "m1",
            "container_id": "fictional-a",
            "sent_at": "2026-08-04T09:00:00Z",
            "author": {"id": "ada", "kind": "human"},
            "content": "Fictional alpha update.",
        },
        {
            "record_id": "fictional-b:m2",
            "native_id": "m2",
            "container_id": "fictional-b",
            "sent_at": "2026-08-04T10:00:00Z",
            "author": {"id": "bert", "kind": "human"},
            "content": "Fictional beta update.",
        },
    ]
    extractor = FailSecondChunkOnce()
    engine = Engine(tmp_path / "chunk-retry.db", extractor=extractor)

    with pytest.raises(RuntimeError, match="temporary extractor failure"):
        engine.add_records(records, scope_id="fictional-team")

    assert [
        observation.record_id
        for observation in engine.store.record_observations("fictional-team")
    ] == ["fictional-a:m1"]

    report = engine.add_records(records, scope_id="fictional-team")

    assert extractor.calls == ["fictional-a", "fictional-b", "fictional-b"]
    assert report.records_processed == 1
    assert report.records_skipped == 1
    assert {
        observation.record_id
        for observation in engine.store.record_observations("fictional-team")
    } == {"fictional-a:m1", "fictional-b:m2"}


def test_engine_orders_conversation_units_and_packs_boundaries(tmp_path) -> None:
    class RecordingExtractor:
        def __init__(self):
            self.calls = []

        def extract(self, **kwargs):
            self.calls.append(kwargs)
            rejection_counts = (
                {"UNPARSEABLE": 1}
                if len(self.calls) == 1
                else {"NO_SOURCES": len(self.calls)}
            )
            return SimpleNamespace(cards=[], rejection_counts=rejection_counts)

    def record(container, native_id, sent_at, thread_id=None):
        return {
            "record_id": f"{container}:{native_id}",
            "native_id": native_id,
            "container_id": container,
            "thread_id": thread_id,
            "sent_at": sent_at,
            "author": {"id": "ada", "kind": "human"},
            "content": native_id,
        }

    extractor = RecordingExtractor()
    engine = Engine(tmp_path / "units.db", extractor=extractor)
    report = engine.add_records(
        [
            record("B", "later", "2026-07-31T10:00:00Z"),
            record("A", "thread-3", "2026-07-31T09:02:00Z", "A:thread"),
            record("B", "earlier", "2026-07-31T08:00:00Z"),
            record("A", "single-2", "2026-07-31T09:04:00Z"),
            record("A", "thread-1", "2026-07-31T09:00:00Z", "A:thread"),
            record("A", "single-1", "2026-07-31T09:03:00Z"),
            record("A", "thread-2", "2026-07-31T09:01:00Z", "A:thread"),
        ],
        scope_id="team",
        batch_size=2,
    )

    assert [
        [record.record_id for record in call["records"]]
        for call in extractor.calls
    ] == [
        ["B:earlier", "B:later"],
        ["A:thread-1", "A:thread-2", "A:thread-3"],
        ["A:single-1", "A:single-2"],
    ]
    assert report.cards_dropped == 6
    assert report.drop_reasons == {"UNPARSEABLE": 1, "NO_SOURCES": 5}


def test_engine_rolls_new_anchor_between_conversations(tmp_path) -> None:
    first_boundary = "early:first"
    born_subject = "sub_" + stable_hash(
        ["team", "MATTER", "thread", first_boundary]
    )[:20]

    class RollingGateway:
        def __init__(self):
            self.calls = []

        def complete(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "cards": [
                            {
                                "date": "2026-07-31",
                                "title": "Order 1042 payment",
                                "status": "open",
                                "source_ids": ["m1"],
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-07-31",
                            "title": "Order 1042 payment succeeded",
                            "progress": "Payment succeeded for order 1042.",
                            "source_ids": ["m1"],
                            "subject_key": born_subject,
                        }
                    ]
                }
            )

    gateway = RollingGateway()
    engine = Engine(
        tmp_path / "rolling.db",
        gateway=gateway,
        clock=[
            datetime(2026, 7, 31, 9, 1, tzinfo=UTC),
            datetime(2026, 7, 31, 10, 1, tzinfo=UTC),
        ],
    )
    report = engine.add_records(
        [
            {
                "record_id": "late:second",
                "native_id": "second",
                "container_id": "late",
                "sent_at": "2026-07-31T10:00:00Z",
                "author": {"id": "bob", "kind": "human"},
                "content": "Payment succeeded for order 1042.",
            },
            {
                "record_id": first_boundary,
                "native_id": "first",
                "container_id": "early",
                "sent_at": "2026-07-31T09:00:00Z",
                "author": {"id": "ada", "kind": "human"},
                "content": "Order 1042 is awaiting payment.",
            },
        ],
        scope_id="team",
    )

    prompts = [json.loads(call["user"]) for call in gateway.calls]
    assert [prompt["records"][0]["record"]["container_id"] for prompt in prompts] == [
        "early",
        "late",
    ]
    assert born_subject not in gateway.calls[0]["system"]
    assert born_subject in gateway.calls[1]["system"]
    assert report.cards_accepted == 2
    assert report.assertions_emitted == 2
    assert [matter.subject_key for matter in engine.query.list_matters("team")] == [
        born_subject
    ]


def test_context_thread_filter_prevents_mail_container_cross_thread_mix(
    tmp_path,
) -> None:
    class RecordingExtractor:
        def __init__(self):
            self.calls = []

        def extract(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(cards=[], rejection_counts={})

    extractor = RecordingExtractor()
    engine = Engine(tmp_path / "thread-context.db", extractor=extractor)
    engine.add_records(
        [
            {
                "record_id": "mail:old-a",
                "native_id": "old-a",
                "container_id": "mail",
                "thread_id": "mail:thread-a",
                "sent_at": "2026-08-04T09:00:00Z",
                "author": {"id": "ada", "kind": "human"},
                "content": "Thread A context.",
            },
            {
                "record_id": "mail:old-b",
                "native_id": "old-b",
                "container_id": "mail",
                "thread_id": "mail:thread-b",
                "sent_at": "2026-08-04T09:01:00Z",
                "author": {"id": "bert", "kind": "human"},
                "content": "Thread B context.",
            },
        ],
        scope_id="fictional-mail",
    )
    engine.add_records(
        [
            {
                "record_id": "mail:new-a",
                "native_id": "new-a",
                "container_id": "mail",
                "thread_id": "mail:thread-a",
                "sent_at": "2026-08-04T10:00:00Z",
                "author": {"id": "cara", "kind": "human"},
                "content": "Thread A continuation.",
            }
        ],
        scope_id="fictional-mail",
    )

    assert [record.record_id for record in extractor.calls[-1]["context"]] == [
        "mail:old-a"
    ]


def test_context_only_card_ingests_and_recitation_deduplicates(tmp_path) -> None:
    gateway = SequenceGateway(
        [
            {"cards": []},
            {
                "cards": [
                    {
                        "date": "2026-08-04",
                        "title": "Fictional incident 77",
                        "status": "blocked",
                        "source_ids": ["m1"],
                    }
                ]
            },
            {
                "cards": [
                    {
                        "date": "2026-08-04",
                        "title": "Fictional incident 77",
                        "status": "blocked",
                        "source_ids": ["m1"],
                    }
                ]
            },
        ]
    )
    engine = Engine(
        tmp_path / "context-recitation.db",
        gateway=gateway,
        clock=lambda: datetime(2026, 8, 4, 12, tzinfo=UTC),
    )

    def record(native_id: str, sent_at: str, content: str) -> dict:
        return {
            "record_id": f"C77:{native_id}",
            "native_id": native_id,
            "container_id": "C77",
            "thread_id": "C77:incident",
            "sent_at": sent_at,
            "author": {"id": "ada", "kind": "human"},
            "content": content,
        }

    engine.add_records(
        [record("old", "2026-08-04T09:00:00Z", "Incident 77 is blocked.")],
        scope_id="team",
    )
    first_citation = engine.add_records(
        [record("new-1", "2026-08-04T10:00:00Z", "Capture the update.")],
        scope_id="team",
    )
    second_citation = engine.add_records(
        [record("new-2", "2026-08-04T11:00:00Z", "Capture it again.")],
        scope_id="team",
    )

    assertions = engine.store.assertions("team")
    assert first_citation.assertions_emitted == 1
    assert second_citation.assertions_emitted == 0
    assert len(assertions) == 1
    assert [ref.source_id for ref in assertions[0].source_refs] == ["C77:old"]
    assert len(engine.store.record_observations("team")) == 3


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


def test_edit_metadata_alone_drops_same_value_assertion(tmp_path) -> None:
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
    first = engine.add_records(
        [_record(content="Release is open.")],
        scope_id="team",
    )
    edited = engine.add_records(
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
    assert first.assertions_emitted == 1
    assert edited.assertions_emitted == 0
    assert edited.unchanged_dropped == 1
    assert len(assertions) == 1
    assert {item.object_value for item in assertions} == {"open"}
    assert engine.gate_statistics("team").unchanged_dropped == 1


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


def test_participants_display_resolves_person_names(tmp_path) -> None:
    import json as _json

    from matterhorn import Engine

    class NamedGateway:
        def complete(self, **kwargs):
            payload = _json.loads(kwargs["user"])
            if "records" not in payload:
                return _json.dumps({"candidates": []})
            return _json.dumps(
                {
                    "cards": [
                        {
                            "date": "2026-08-04",
                            "title": "Fictional naming matter",
                            "status": "open",
                            "participants": [
                                {"id": "u-42", "display_name": "Dana Reyes"}
                            ],
                            "source_ids": ["m1"],
                        }
                    ]
                }
            )

    engine = Engine(tmp_path / "names.db", llm=NamedGateway())
    engine.add(
        "named",
        [
            {
                "id": "m1",
                "sender": {"id": "u-42", "name": "Dana Reyes"},
                "text": "Fictional update from Dana.",
                "sent_at": "2026-08-04T09:00:00Z",
                "conversation_id": "room",
            }
        ],
        wait=True,
    )
    matter = engine.matters("named")[0]
    assert matter.participants == ["u-42"]  # ids stay the identity
    assert matter.participants_display == ["Dana Reyes"]  # names are display
    assert engine.store.person_names("named") == {"u-42": "Dana Reyes"}


def test_conversation_display_names_disambiguate_same_name_groups(tmp_path) -> None:
    from datetime import UTC, datetime

    from matterhorn import Engine

    engine = Engine(tmp_path / "convnames.db")
    engine.store.upsert_conversation_names(
        "grp",
        {
            "dumbo:im:0-111-groupchat": "项目讨论群",
            "dumbo:im:0-222-groupchat": "项目讨论群",
            "dumbo:im:0-333-groupchat": "独一无二群",
        },
        seen_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    display = engine.conversation_display_names("grp")
    # Identity stays the key; same-name groups render distinguishably.
    assert display["dumbo:im:0-333-groupchat"] == "独一无二群"
    assert display["dumbo:im:0-111-groupchat"] != display["dumbo:im:0-222-groupchat"]
    assert display["dumbo:im:0-111-groupchat"].startswith("项目讨论群(")
