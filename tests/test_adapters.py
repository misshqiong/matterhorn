from __future__ import annotations

import json
from pathlib import Path

import pytest

from matterhorn.adapters import (
    MessageCardExtractor,
    map_openviking_digest,
    map_reme_digest,
)
from matterhorn.engine import Engine


FIXTURES = Path(__file__).parent / "fixtures"


class StaticGateway:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return json.dumps(self.response)


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
    engine.ingest(first.cards)
    snapshot = engine.store.assertions("team-a")
    assert engine.ingest(second.cards) == []
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
    engine.ingest([first])
    assert engine.ingest([second]) == []
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
