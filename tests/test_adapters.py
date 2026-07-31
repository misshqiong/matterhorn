from __future__ import annotations

import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from matterhorn.adapters import (
    MessageCardExtractor,
    map_openviking_digest,
    map_reme_digest,
)
from matterhorn.adapters.email_mbox import map_email_message
from matterhorn.canonical import stable_hash
from matterhorn.contracts import SubjectAnchor
from matterhorn.engine import Engine

FIXTURES = Path(__file__).parent / "fixtures"


class StaticGateway:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.response)


def _modern_record(
    native_id: str,
    *,
    thread_id: str | None = None,
    content: str | None = None,
) -> dict:
    return {
        "record_id": f"C1:{native_id}",
        "native_id": native_id,
        "container_id": "C1",
        "thread_id": thread_id,
        "sent_at": "2026-07-29T09:00:00Z",
        "author": {"id": "ada", "kind": "human"},
        "content": content or native_id,
    }


def _email_record(
    native_id: str,
    subject: str,
    *,
    references: str | None = None,
):
    message = EmailMessage()
    message["Message-ID"] = f"<{native_id}>"
    message["Date"] = "Wed, 29 Jul 2026 09:00:00 +0000"
    message["From"] = "Ada <ada@example.test>"
    message["To"] = "team@example.test"
    message["Subject"] = subject
    if references is not None:
        message["References"] = references
    message.set_content(f"Update from {native_id}.")
    record = map_email_message(message, container_id="imap:ada@example.test/INBOX")
    assert record is not None
    return record


def test_message_extractor_is_profile_driven_traceable_and_idempotent(tmp_path) -> None:
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Matterhorn release",
                    "status": "open",
                    "participants": [{"id": "ada", "role": "owner"}],
                    "progress": "Release candidate ready.",
                    "source_ids": ["m1", "m2"],
                    "subject_key": "release",
                }
            ]
        }
    )
    extractor = MessageCardExtractor(gateway, "org-matters/v1")
    messages = [
        {
            "message_id": "m1",
            "sent_at": "2026-07-29T09:00:00Z",
            "sender": "ada",
            "content": "Release candidate ready.",
        },
        {
            "message_id": "m2",
            "sent_at": "2026-07-29T09:01:00Z",
            "sender": "bob",
            "content": "I will verify it.",
        },
    ]
    first = extractor.extract(scope_id="team-a", messages=messages)
    second = extractor.extract(scope_id="team-a", messages=messages)
    assert first.rejection_counts == {}
    assert first.cards == second.cards
    assert [ref.source_id for ref in first.cards[0].source_refs] == ["m1", "m2"]
    assert "registered_semantic_predicates" not in gateway.calls[0]["system"]
    assert "active_card_fields" in gateway.calls[0]["system"]
    assert '"value_domain":["open","in_progress","blocked"' in gateway.calls[0]["system"]

    engine = Engine(tmp_path / "messages.db", "org-matters/v1")
    engine._ingest_cards_sync(first.cards)
    snapshot = engine.store.assertions("team-a")
    assert engine._ingest_cards_sync(second.cards) == []
    assert engine.store.assertions("team-a") == snapshot


@pytest.mark.parametrize(
    ("source_ids", "reason"),
    [
        ([], "NO_SOURCES"),
        (["invented"], "SOURCE_NOT_TRACEABLE"),
    ],
)
def test_message_extractor_shares_traceability_gate(source_ids, reason) -> None:
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Release",
                    "source_ids": source_ids,
                }
            ]
        }
    )
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="s",
        messages=[
            {
                "message_id": "m1",
                "sent_at": "2026-07-29T09:00:00Z",
                "sender": "ada",
                "content": "Release.",
            }
        ],
    )
    assert report.cards == []
    assert report.rejection_counts == {reason: 1}


@pytest.mark.parametrize("citation", ["m1", "C1:commit:" + "a" * 40])
def test_record_extractor_accepts_aliases_and_full_source_ids(citation) -> None:
    source_id = "C1:commit:" + "a" * 40
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Alias-safe release",
                    "source_ids": [citation],
                }
            ]
        }
    )
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="dev",
        records=[
            _modern_record(
                "commit:" + "a" * 40,
                content="Release alias-based extraction.",
            )
        ],
    )

    prompt = json.loads(gateway.calls[0]["user"])
    assert prompt["records"][0]["source_alias"] == "m1"
    assert source_id not in gateway.calls[0]["user"]
    assert report.rejection_counts == {}
    assert [ref.source_id for ref in report.cards[0].source_refs] == [source_id]


def test_unknown_source_alias_is_rejected_as_not_traceable() -> None:
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Unknown alias",
                    "source_ids": ["m999"],
                }
            ]
        }
    )
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="dev",
        records=[_modern_record("commit:" + "b" * 40)],
    )

    assert report.cards == []
    assert report.rejection_counts == {"SOURCE_NOT_TRACEABLE": 1}


def test_record_extraction_batches_without_splitting_threads() -> None:
    class EmptyGateway:
        def __init__(self):
            self.prompts = []

        def complete(self, **kwargs):
            self.prompts.append(json.loads(kwargs["user"]))
            return json.dumps({"cards": []})

    gateway = EmptyGateway()
    records = [
        _modern_record("t1-a", thread_id="C1:t1"),
        _modern_record("t2-a", thread_id="C1:t2"),
        _modern_record("t1-b", thread_id="C1:t1"),
        _modern_record("t1-c", thread_id="C1:t1"),
        _modern_record("t3-a", thread_id="C1:t3"),
    ]
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="dev",
        records=records,
        batch_size=2,
    )

    assert report.cards == []
    assert [
        [item["record"]["content"] for item in prompt["records"]]
        for prompt in gateway.prompts
    ] == [["t1-a", "t1-b", "t1-c"], ["t2-a", "t3-a"]]
    assert [
        [item["source_alias"] for item in prompt["records"]]
        for prompt in gateway.prompts
    ] == [["m1", "m2", "m3"], ["m1", "m2"]]


def test_email_subject_key_stamp_is_server_derived_and_deterministic() -> None:
    record = _email_record("one@example.test", "Re: Release readiness")
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Release readiness",
                    "progress": "QA started.",
                    "source_ids": ["m1"],
                }
            ]
        }
    )
    extractor = MessageCardExtractor(gateway, "org-matters/v1")

    first = extractor.extract(scope_id="mail", records=[record])
    second = extractor.extract(scope_id="mail", records=[record])

    expected = "mail:" + stable_hash(
        ["imap:ada@example.test/INBOX", "subject:release readiness"]
    )[:20]
    assert first.cards[0].subject_key == expected
    assert second.cards[0].subject_key == expected
    assert "subject_key" not in gateway.calls[0]["response_schema"]["$defs"][
        "MessageCardCandidate"
    ]["properties"]


def test_record_extractor_keeps_only_offered_anchor_subject_keys() -> None:
    anchors = [
        SubjectAnchor(
            subject_key="known-release",
            title="Release readiness",
            status="in_progress",
        )
    ]
    valid_gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Verify release candidate",
                    "progress": "QA verification started.",
                    "source_ids": ["m1"],
                    "subject_key": "known-release",
                }
            ]
        }
    )
    valid = MessageCardExtractor(valid_gateway, "org-matters/v1").extract(
        scope_id="dev",
        records=[_modern_record("verify-1")],
        anchors=anchors,
    )
    assert valid.cards[0].subject_key == "known-release"
    assert "Known open matters" in valid_gateway.calls[0]["system"]
    assert '"subject_key":"sub_known_release"' in valid_gateway.calls[0]["system"]
    assert "shared identifiers" in valid_gateway.calls[0]["system"]
    assert "complementary status transition" in valid_gateway.calls[0]["system"]
    assert "NO linking signal" in valid_gateway.calls[0]["system"]
    assert "paying, and adjusting or rescheduling" in valid_gateway.calls[0][
        "system"
    ]

    fabricated_gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Unrelated work",
                    "source_ids": ["m1"],
                    "subject_key": "fabricated",
                }
            ]
        }
    )
    fabricated = MessageCardExtractor(
        fabricated_gateway, "org-matters/v1"
    ).extract(
        scope_id="dev",
        records=[_modern_record("other-1")],
        anchors=anchors,
    )
    assert fabricated.rejection_counts == {}
    assert fabricated.cards[0].subject_key is None


def test_mail_subject_key_stamp_overrides_anchor_citation() -> None:
    record = _email_record("anchor-mail@example.test", "Release readiness")
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Release readiness",
                    "source_ids": ["m1"],
                    "subject_key": "known-release",
                }
            ]
        }
    )
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="mail",
        records=[record],
        anchors=[
            SubjectAnchor(
                subject_key="known-release",
                title="Release readiness",
            )
        ],
    )
    expected = "mail:" + stable_hash(
        ["imap:ada@example.test/INBOX", "subject:release readiness"]
    )[:20]
    assert report.cards[0].subject_key == expected


def test_datetime_drift_in_card_date_is_truncated_not_unparseable() -> None:
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-30T10:00:00Z",
                    "title": "Gateway H2 research",
                    "progress": "Research started.",
                    "source_ids": ["m1"],
                }
            ]
        }
    )
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="dev",
        records=[_modern_record("h2-1")],
    )
    assert report.rejection_counts == {}
    assert report.cards[0].date.isoformat() == "2026-07-30"


def test_email_subject_fallback_groups_reply_and_chinese_prefixes(tmp_path) -> None:
    records = [
        _email_record("one@example.test", "Launch plan"),
        _email_record("two@example.test", "Re: Fwd: launch plan"),
        _email_record("three@example.test", "回复: LAUNCH   PLAN"),
    ]
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Launch plan",
                    "progress": f"Update {index}.",
                    "source_ids": [f"m{index}"],
                }
                for index in range(1, 4)
            ]
        }
    )
    extractor = MessageCardExtractor(gateway, "org-matters/v1")

    report = extractor.extract(scope_id="mail", records=records)
    engine = Engine(tmp_path / "fallback.db", "org-matters/v1")
    engine._ingest_cards_sync(report.cards)

    matters = engine.query.list_matters("mail")
    assert len(matters) == 1
    assert len(
        engine.query.timeline("mail", matters[0].subject_key, "progress")
    ) == 3
    assert "aggregate all Records in each conversation" in gateway.calls[0]["system"]
    assert "回复:" in gateway.calls[0]["system"]
    assert "do not default status to done" in gateway.calls[0]["system"]


def test_distinct_email_subjects_get_distinct_matter_identities(tmp_path) -> None:
    records = [
        _email_record("one@example.test", "Launch plan"),
        _email_record("two@example.test", "Budget review"),
    ]
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": title,
                    "progress": "Updated.",
                    "source_ids": [f"m{index}"],
                }
                for index, title in enumerate(
                    ["Launch plan", "Budget review"],
                    start=1,
                )
            ]
        }
    )
    report = MessageCardExtractor(gateway, "org-matters/v1").extract(
        scope_id="mail",
        records=records,
    )
    engine = Engine(tmp_path / "distinct.db", "org-matters/v1")
    engine._ingest_cards_sync(report.cards)

    assert len({card.subject_key for card in report.cards}) == 2
    assert len(engine.query.list_matters("mail")) == 2


def test_message_extractor_drops_fields_outside_active_profile() -> None:
    gateway = StaticGateway(
        {
            "cards": [
                {
                    "date": "2026-07-29",
                    "title": "Choose a database",
                    "blocker": "Not a field in personal-decisions",
                    "source_ids": ["m1"],
                }
            ]
        }
    )
    report = MessageCardExtractor(gateway, "personal-decisions/v1").extract(
        scope_id="s",
        messages=[
            {
                "message_id": "m1",
                "sent_at": "2026-07-29T09:00:00Z",
                "sender": "ada",
                "content": "Choose a database.",
            }
        ],
    )
    assert report.rejection_counts == {"FIELD_NOT_IN_PROFILE": 1}


@pytest.mark.parametrize(
    ("fixture", "mapper", "source_id"),
    [
        (
            "reme/daily-release.json",
            map_reme_digest,
            "session-42:msg-7",
        ),
        (
            "openviking/release-overview.json",
            map_openviking_digest,
            "viking://user/ada/sessions/s1/messages.jsonl#msg-7",
        ),
    ],
)
def test_digest_adapter_round_trip(tmp_path, fixture, mapper, source_id) -> None:
    payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    first = mapper(payload)
    second = mapper(payload)
    assert first == second
    assert first.source_refs[0].source_id == source_id

    engine = Engine(tmp_path / f"{mapper.__name__}.db", "org-matters/v1")
    engine._ingest_cards_sync([first])
    assert engine._ingest_cards_sync([second]) == []
    current = engine.query.current("team-a", "release", "status")
    assert current[0].value == "open"
    assert current[0].source_ids == [source_id]


@pytest.mark.parametrize(
    ("mapper", "payload"),
    [
        (
            map_reme_digest,
            {
                "scope_id": "s",
                "frontmatter": {"name": "No evidence", "date": "2026-07-29"},
                "content": "digest",
            },
        ),
        (
            map_openviking_digest,
            {
                "scope_id": "s",
                "uri": "viking://user/memories/no-evidence",
                "overview": "digest",
                "metadata": {"date": "2026-07-29"},
            },
        ),
    ],
)
def test_digest_adapters_fail_loudly_without_evidence(mapper, payload) -> None:
    with pytest.raises(ValueError, match="no traceable"):
        mapper(payload)
