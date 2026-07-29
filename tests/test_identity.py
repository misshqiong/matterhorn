from datetime import UTC, date, datetime

from matterhorn.canonical import normalize_title
from matterhorn.contracts import EpisodeCard, SubjectRecord
from matterhorn.contracts.schema import resolve_schema
from matterhorn.engine.identity import resolve_subject


def _card(title, sources):
    return EpisodeCard.model_validate(
        {
            "card_id": title,
            "scope_id": "s",
            "date": date(2026, 1, 1),
            "title": title,
            "source_refs": [
                {
                    "source_id": item,
                    "sent_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "sender": "u",
                }
                for item in sources
            ],
        }
    )


def test_title_normalization_strips_punctuation_and_collapses_space() -> None:
    assert normalize_title("  Hello,   WORLD! ") == "hello world"


def test_one_of_three_shared_sources_does_not_merge() -> None:
    profile = resolve_schema("org-matters/v1")
    existing = [
        SubjectRecord(
            "s", "old", "MATTER", "Other", "other", frozenset({"shared"})
        )
    ]
    resolved, created = resolve_subject(
        _card("New", ["shared", "n1", "n2"]), profile, existing
    )
    assert created
    assert resolved.subject_key != "old"


def test_two_shared_sources_merge() -> None:
    profile = resolve_schema("org-matters/v1")
    existing = [
        SubjectRecord(
            "s", "old", "MATTER", "Other", "other", frozenset({"m1", "m2"})
        )
    ]
    resolved, created = resolve_subject(
        _card("New", ["m1", "m2", "n1"]), profile, existing
    )
    assert not created
    assert resolved.subject_key == "old"
