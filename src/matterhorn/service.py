from __future__ import annotations

from datetime import datetime
from typing import Any

from matterhorn.contracts import Correction, EpisodeCard, Record


class MatterhornService:
    """Transport-neutral application service shared by SDK, REST, and MCP."""

    def __init__(self, engine: Any):
        self.engine = engine

    def add_episode_cards(
        self, *, cards: list[EpisodeCard | dict[str, Any]], scope_id: str | None = None
    ) -> dict[str, Any]:
        assertions = self.engine.ingest(cards, scope_id=scope_id)
        return {
            "cards": len(cards),
            "assertions_emitted": len(assertions),
            "assertion_ids": [item.assertion_id for item in assertions],
        }

    def add_records(
        self,
        *,
        records: list[Record | dict[str, Any]],
        scope_id: str,
        cursors: dict[str, str] | None = None,
        backfill: bool = False,
    ) -> dict[str, Any]:
        return self.engine.add_records(
            records,
            scope_id=scope_id,
            cursors=cursors,
            backfill=backfill,
        ).model_dump(mode="json")

    def query_current(
        self, *, scope_id: str, subject_key: str, predicate: str
    ) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.engine.query.current(scope_id, subject_key, predicate)
        ]

    def query_timeline(
        self, *, scope_id: str, subject_key: str, predicate: str
    ) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.engine.query.timeline(scope_id, subject_key, predicate)
        ]

    def query_at(
        self,
        *,
        scope_id: str,
        subject_key: str,
        predicate: str,
        instant: datetime,
    ) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.engine.query.at(
                scope_id, subject_key, predicate, instant
            )
        ]

    def query_by_person(
        self, *, scope_id: str, person_id: str
    ) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.engine.query.by_person(scope_id, person_id)
        ]

    def list_matters(self, *, scope_id: str) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self.engine.query.list_matters(scope_id)
        ]

    def correct(
        self, *, correction: Correction | dict[str, Any]
    ) -> dict[str, Any]:
        return self.engine.correct(correction).model_dump(mode="json")
