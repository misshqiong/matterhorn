from datetime import UTC, datetime

from matterhorn.engine.engine import Engine


class ExplodingGateway:
    def __getattribute__(self, name):
        raise AssertionError(f"LLM gateway touched on read: {name}")


def test_every_query_ignores_exploding_llm_gateway(tmp_path) -> None:
    engine = Engine(
        tmp_path / "read.db",
        "org-matters/v1",
        clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        llm=ExplodingGateway(),
    )
    engine._ingest_cards_sync(
        [
            {
                "card_id": "c1",
                "scope_id": "s",
                "subject_key": "x",
                "date": "2026-01-01",
                "title": "Launch",
                "status": "done",
                "participants": [{"id": "u1", "role": "owner"}],
                "progress": "finished",
                "source_refs": [
                    {
                        "source_id": "m1",
                        "sent_at": "2026-01-01T08:00:00Z",
                        "sender": "u1",
                    }
                ],
            }
        ]
    )
    assert engine.query.current("s", "x", "status")
    assert engine.query.timeline("s", "x", "status")
    assert engine.query.at(
        "s", "x", "status", datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert engine.query.by_person("s", "u1")
    assert engine.query.list_matters("s")
    assert engine.query.completion("s")["completed"] == 1


def test_query_package_does_not_import_distill() -> None:
    from pathlib import Path

    query_dir = Path(__file__).resolve().parents[1] / "src/matterhorn/query"
    text = "\n".join(path.read_text() for path in query_dir.glob("*.py"))
    assert "matterhorn.distill" not in text
    assert "import distill" not in text
