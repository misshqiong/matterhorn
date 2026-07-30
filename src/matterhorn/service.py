from __future__ import annotations

from datetime import datetime
from typing import Any

from matterhorn.contracts import (
    Correction,
    EpisodeCard,
    Message,
    Record,
    TaskResult,
)


class MatterhornService:
    """Transport-neutral application service shared by SDK, REST, and MCP."""

    def __init__(self, engine: Any):
        self.engine = engine

    def add_messages(
        self,
        *,
        messages: list[Message | dict[str, Any]],
        scope_id: str,
        wait: bool = False,
    ) -> dict[str, Any]:
        return self.engine.add(
            scope_id=scope_id, messages=messages, wait=wait
        ).model_dump(mode="json")

    def add_cards(
        self,
        *,
        cards: list[EpisodeCard | dict[str, Any]],
        scope_id: str,
        wait: bool = False,
    ) -> dict[str, Any]:
        return self.engine.add_cards(
            cards=cards, scope_id=scope_id, wait=wait
        ).model_dump(mode="json")

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
        self._require_subject(scope_id, subject_key)
        return [
            item.to_dict()
            for item in self.engine.query.current(scope_id, subject_key, predicate)
        ]

    def query_timeline(
        self, *, scope_id: str, subject_key: str, predicate: str
    ) -> list[dict[str, Any]]:
        self._require_subject(scope_id, subject_key)
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
        self._require_subject(scope_id, subject_key)
        return [
            item.to_dict()
            for item in self.engine.query.at(
                scope_id, subject_key, predicate, instant
            )
        ]

    def query_by_person(
        self, *, scope_id: str, person_id: str
    ) -> list[dict[str, Any]]:
        self._require_scope(scope_id)
        return [
            item.to_dict()
            for item in self.engine.query.by_person(scope_id, person_id)
        ]

    def list_scopes(self) -> list[str]:
        return self.engine.store.list_scopes()

    def list_matters(self, *, scope_id: str) -> list[dict[str, Any]]:
        self._require_scope(scope_id)
        return [item.to_dict() for item in self.engine.matters(scope_id)]

    def matter_detail(
        self, *, scope_id: str, subject_key: str
    ) -> dict[str, Any]:
        self._require_subject(scope_id, subject_key)
        subject = next(
            item
            for item in self.engine.query.list_matters(scope_id)
            if item.subject_key == subject_key
        )
        predicates = [
            item.name
            for item in self.engine.profile.predicates
            if item.subject == subject.subject_type
        ]
        current = [
            value.to_dict()
            for predicate in predicates
            for value in self.engine.query.current(
                scope_id, subject_key, predicate
            )
        ]
        timeline = {
            predicate: [
                value.to_dict()
                for value in self.engine.query.timeline(
                    scope_id, subject_key, predicate
                )
            ]
            for predicate in predicates
        }
        return {
            "subject_key": subject.subject_key,
            "subject_type": subject.subject_type,
            "title": subject.title,
            "current": current,
            "timeline": {
                predicate: values
                for predicate, values in timeline.items()
                if values
            },
        }

    def ingest_raw(
        self,
        *,
        scope_id: str,
        text: str,
        wait: bool = False,
        now: Any = None,
    ) -> dict[str, Any]:
        from matterhorn.ingest import detect_ingest

        detected = detect_ingest(text, now=now)
        task = self.engine.add(
            scope_id=scope_id,
            messages=detected.messages,
            wait=wait,
        )
        payload = task.model_dump(mode="json")
        result = {
            "input_format": detected.input_format,
            "synthesized_timestamps": detected.synthesized_timestamps,
            "accepted": len(detected.messages),
            **payload,
        }
        if isinstance(task, TaskResult):
            result["accepted"] = len(detected.messages)
        return result

    def chat(
        self,
        *,
        scope_id: str,
        message: str,
        history: list[dict[str, str]],
        runner: Any,
    ) -> dict[str, Any]:
        self._require_scope(scope_id)
        return runner.run(
            service=self,
            scope_id=scope_id,
            message=message,
            history=history,
        )

    def task(self, *, task_id: str) -> dict[str, Any]:
        return self.engine.task(task_id).model_dump(mode="json")

    def correct(
        self,
        *,
        correction: Correction | dict[str, Any],
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        if scope_id is not None and not isinstance(correction, Correction):
            correction = {**correction, "scope_id": scope_id}
        elif scope_id is not None and correction.scope_id != scope_id:
            raise ValueError("correction scope_id MUST match the resource scope")
        selected_scope = (
            correction.scope_id
            if isinstance(correction, Correction)
            else correction["scope_id"]
        )
        selected_subject = (
            correction.subject_key
            if isinstance(correction, Correction)
            else correction["subject_key"]
        )
        self._require_subject(selected_scope, selected_subject)
        return self.engine.correct(correction).model_dump(mode="json")

    def events(
        self, *, scope_id: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        self._require_scope(scope_id)
        return [
            item.model_dump(mode="json")
            for item in self.engine.events(scope_id, since=since)
        ]

    def export(self, *, scope_id: str) -> dict[str, Any]:
        self._require_scope(scope_id)
        return self.engine.export(scope_id).model_dump(mode="json")

    def _require_scope(self, scope_id: str) -> None:
        from matterhorn.errors import ResourceNotFoundError

        if not self.engine.scope_exists(scope_id):
            raise ResourceNotFoundError(f"unknown scope_id: {scope_id}")

    def _require_subject(self, scope_id: str, subject_key: str) -> None:
        from matterhorn.errors import ResourceNotFoundError

        self._require_scope(scope_id)
        if not self.engine.subject_exists(scope_id, subject_key):
            raise ResourceNotFoundError(
                f"unknown subject_key {subject_key!r} in scope {scope_id!r}"
            )
