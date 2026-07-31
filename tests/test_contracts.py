from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from matterhorn.canonical import derive_assertion_id, object_key
from matterhorn.contracts import EpisodeCard, Operation, SourceRef, SubjectAnchor


def test_assertion_id_matches_language_neutral_golden_bytes() -> None:
    sources = [
        SourceRef(
            source_id="z",
            sent_at=datetime(2026, 1, 1, tzinfo=UTC),
            sender="u",
        ),
        SourceRef(
            source_id="a",
            sent_at=datetime(2026, 1, 1, tzinfo=UTC),
            sender="u",
        ),
    ]
    result = derive_assertion_id(
        "scope",
        "subject",
        "phase",
        Operation.ASSERT,
        object_key("open"),
        datetime(2026, 1, 1, tzinfo=UTC),
        sources,
    )
    assert result == "aad315464957f7f00a756a7a6cb2277310fc359f5fbe817e693d62c3620b8868"


def test_episode_card_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EpisodeCard.model_validate(
            {
                "card_id": "c1",
                "scope_id": "s",
                "date": "2026-01-01",
                "title": "T",
                "source_refs": [
                    {
                        "source_id": "m1",
                        "sent_at": "2026-01-01T00:00:00Z",
                        "sender": "u",
                    }
                ],
                "free_form_fact": "forbidden",
            }
        )


def test_subject_anchor_is_closed_and_exported() -> None:
    anchor = SubjectAnchor(
        subject_key="release",
        title="Release readiness",
        status="open",
        last_active_at="2026-07-31T09:00:00Z",
    )
    assert anchor.subject_key == "release"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SubjectAnchor.model_validate(
            {"subject_key": "release", "title": "Release", "extra": True}
        )
