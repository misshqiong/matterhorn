from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from matterhorn import Engine
from matterhorn.render import render_scope_markdown

FIXTURES = Path(__file__).parent / "fixtures" / "markdown"


def fixture_markdown(tmp_path: Path) -> str:
    payload = json.loads(
        (FIXTURES / "ledger.json").read_text(encoding="utf-8")
    )
    engine = Engine(
        tmp_path / "markdown.db",
        clock=[datetime.fromisoformat(item) for item in payload["clock"]],
    )
    engine._ingest_cards_sync(payload["cards"])
    engine.correct(payload["correction"])
    first = render_scope_markdown(engine, payload["scope_id"])
    second = render_scope_markdown(engine, payload["scope_id"])
    assert second == first
    return first


def test_markdown_export_matches_golden_fixture(tmp_path) -> None:
    actual = fixture_markdown(tmp_path)
    expected = (FIXTURES / "MATTERS.md").read_text(encoding="utf-8")

    assert actual == expected
    assert "**[human correction]**" in actual
    assert "[github:matterhorn:commit:abc]" in actual
    assert "<code>human-note-1</code>" in actual


def test_human_reconfirmation_badges_the_supported_interval(tmp_path) -> None:
    engine = Engine(
        tmp_path / "human-reconfirmation.db",
        clock=[
            datetime.fromisoformat("2026-07-28T09:05:00Z"),
            datetime.fromisoformat("2026-07-29T09:05:00Z"),
        ],
    )
    engine._ingest_cards_sync(
        [
            {
                "card_id": "model-card",
                "scope_id": "dev",
                "subject_key": "release",
                "date": "2026-07-28",
                "title": "Release",
                "status": "open",
                "source_refs": [
                    {
                        "source_id": "model-source",
                        "sent_at": "2026-07-28T09:00:00Z",
                        "sender": "model",
                    }
                ],
            }
        ]
    )
    engine.correct(
        {
            "scope_id": "dev",
            "subject_key": "release",
            "subject_type": "MATTER",
            "predicate": "status",
            "object_value": "open",
            "valid_from": "2026-07-29T09:00:00Z",
            "source_refs": [
                {
                    "source_id": "human-source",
                    "sent_at": "2026-07-29T09:00:00Z",
                    "sender": "human",
                }
            ],
        }
    )

    rendered = render_scope_markdown(engine, "dev")

    assert rendered.count("**[human correction]**") == 1
    assert "<code>human-source</code>" in rendered
