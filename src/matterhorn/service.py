from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matterhorn.capacity import DEFAULT_CAPACITY, matter_activation
from matterhorn.contracts import (
    Correction,
    EpisodeCard,
    Message,
    Record,
    SourceRef,
    TaskResult,
)

INGEST_TRACE_LOGGER = "matterhorn.ingest"
_TRACE_CONTENT_LIMIT = 500


def dev_trace_enabled() -> bool:
    return os.environ.get("MATTERHORN_DEV_TRACE", "") == "1"


class MatterhornService:
    """Transport-neutral application service shared by SDK, REST, and MCP."""

    def __init__(self, engine: Any, *, dev_trace: bool | None = None):
        self.engine = engine
        self.dev_trace = dev_trace_enabled() if dev_trace is None else dev_trace
        self._trace_logger = logging.getLogger(INGEST_TRACE_LOGGER)
        if self.dev_trace and not self._trace_logger.handlers:
            # Plain single-line handler: rich/uvicorn handlers wrap at terminal
            # width and swallow the content field, defeating the trace.
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self._trace_logger.addHandler(handler)
            self._trace_logger.propagate = False
            self._trace_logger.setLevel(logging.INFO)

    def _trace_ingest(self, *, scope_id: str, kind: str, items: list[Any]) -> None:
        # Dev-mode observability only: show exactly what arrived, before any
        # distillation. Content is user data in a local log; still truncated
        # per line to keep the log readable. Credentials never pass this path.
        if not self.dev_trace:
            return
        for item in items:
            try:
                if isinstance(item, dict):
                    payload = item
                else:
                    payload = item.model_dump(mode="json")
                if kind == "message":
                    sender = payload.get("sender") or {}
                    who = sender.get("name") or sender.get("id") or "?"
                    conv = payload.get("conversation_id") or "-"
                    text = payload.get("text") or ""
                    native = payload.get("id") or "?"
                else:
                    author = payload.get("author") or {}
                    who = author.get("display_name") or author.get("id") or "?"
                    conv = payload.get("container_id") or "-"
                    text = payload.get("content") or ""
                    native = payload.get("record_id") or "?"
                snippet = text[:_TRACE_CONTENT_LIMIT]
                suffix = "…" if len(text) > _TRACE_CONTENT_LIMIT else ""
                self._trace_logger.info(
                    "[ingest] scope=%s %s=%s sender=%s id=%s chars=%d content=%r%s",
                    scope_id, "conv" if kind == "message" else "container",
                    conv, who, native, len(text), snippet, suffix,
                )
            except Exception:  # noqa: BLE001 - tracing must never break ingest
                self._trace_logger.info(
                    "[ingest] scope=%s untraceable %s item", scope_id, kind
                )

    def add_messages(
        self,
        *,
        messages: list[Message | dict[str, Any]],
        scope_id: str,
        wait: bool = False,
    ) -> dict[str, Any]:
        self._trace_ingest(scope_id=scope_id, kind="message", items=messages)
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
        self._trace_ingest(scope_id=scope_id, kind="record", items=records)
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
        subject_key = self.engine.canonical_subject_key(scope_id, subject_key)
        return [
            item.to_dict()
            for item in self.engine.query.current(scope_id, subject_key, predicate)
        ]

    def query_timeline(
        self, *, scope_id: str, subject_key: str, predicate: str
    ) -> list[dict[str, Any]]:
        self._require_subject(scope_id, subject_key)
        subject_key = self.engine.canonical_subject_key(scope_id, subject_key)
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
        subject_key = self.engine.canonical_subject_key(scope_id, subject_key)
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

    def recent_stream(
        self,
        *,
        scope_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("stream limit MUST be positive")
        rows = self.engine.store.recent_staged(scope_id, limit=min(limit, 200))
        return [
            {
                "scope_id": row.scope_id,
                "container_id": row.record.container_id,
                "sender": (
                    row.record.author.display_name or row.record.author.id
                ),
                "sent_at": row.record.sent_at,
                "content": row.record.content[:500],
                "record_id": row.record.record_id,
                "staged_at": row.staged_at,
            }
            for row in rows
        ]

    def list_matters(self, *, scope_id: str) -> list[dict[str, Any]]:
        self._require_scope(scope_id)
        bundle = self.engine._scope_read_bundle(scope_id)
        unseen_map = self.engine.matters_unseen(scope_id, bundle=bundle)
        graph = getattr(bundle, "graph", None)
        capacity = getattr(self.engine, "capacity", DEFAULT_CAPACITY)
        result = []
        for item in self.engine.matters(scope_id, bundle=bundle):
            payload = item.to_dict()
            payload["unseen"] = unseen_map.get(item.subject_key, False)
            if graph is not None and payload.get("descendants_total"):
                payload["children_preview"] = [
                    {
                        "subject_key": child["subject_key"],
                        "title": child["title"],
                        "status": child["status"],
                        "birth_instant": child["birth_instant"],
                    }
                    for child in graph.tree(item.subject_key)["children"]
                ]
            payload["activation"] = matter_activation(
                payload,
                weights=capacity.activation_weights,
            )
            result.append(payload)
        return result

    def matter_graph(
        self,
        *,
        scope_id: str,
        subject_key: str,
    ) -> dict[str, Any]:
        self._require_subject(scope_id, subject_key)
        return self.engine.matter_graph(scope_id, subject_key).to_dict()

    def gather_view(
        self,
        *,
        scope_id: str,
        subject_key: str,
    ) -> dict[str, Any]:
        self._require_subject(scope_id, subject_key)
        return self.engine.gather_view(scope_id, subject_key).to_dict()

    def brief(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        console_groups: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        return self.engine.brief(
            window_start,
            window_end,
            console_groups=console_groups,
        )

    def set_seen(
        self,
        *,
        scope_id: str,
        subject_key: str,
        last_seen_at: datetime | None = None,
    ) -> dict[str, Any]:
        canonical = self.engine.canonical_subject_key(scope_id, subject_key)
        stored = self.engine.set_seen(
            scope_id,
            subject_key,
            last_seen_at=last_seen_at,
        )
        return {
            "scope_id": scope_id,
            "subject_key": canonical,
            "last_seen_at": stored,
        }

    def signals(
        self,
        *,
        scope_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.engine.signals(scope_id, status=status)
        ]

    def acknowledge_signal(
        self,
        *,
        scope_id: str,
        record_id: str,
        kind: str,
        acked_at: datetime | None = None,
    ) -> dict[str, Any]:
        return self.engine.acknowledge_signal(
            scope_id,
            record_id,
            kind,
            acked_at=acked_at,
        ).model_dump(mode="json")

    def all_matters(
        self,
        *,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scopes = [scope_id] if scope_id is not None else self.list_scopes()
        matters: list[dict[str, Any]] = []
        for selected_scope in scopes:
            for item in self.list_matters(scope_id=selected_scope):
                matters.append({"scope_id": selected_scope, **item})
        # A layer-2 subject gathers across scopes, so no per-scope read can
        # tell a member it is contained. This is the only place that sees
        # every scope at once: mark the members so a reader can fold them
        # under their federation the way part_of children already fold
        # under their root, instead of rendering the same project twice.
        gathered: dict[tuple[str, str], str] = {}
        for item in matters:
            for group in item.get("gather_members_by_scope") or []:
                for member in group.get("members") or []:
                    gathered[(member["scope_id"], member["subject_key"])] = (
                        item["subject_key"]
                    )
        for item in matters:
            key = (item["scope_id"], item["subject_key"])
            owner = gathered.get(key)
            if owner is not None and owner != item["subject_key"]:
                item["gathered_by"] = owner
        return matters

    def matter_detail(
        self, *, scope_id: str, subject_key: str
    ) -> dict[str, Any]:
        self._require_subject(scope_id, subject_key)
        subject_key = self.engine.canonical_subject_key(scope_id, subject_key)
        subject = next(
            item
            for item in self.engine.store.subjects(scope_id)
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
            "layer": subject.layer,
            "person_names": self.engine.store.person_names(scope_id),
            "conversation_names": self.engine.conversation_display_names(scope_id),
            "aliases": self.engine._subject_aliases(scope_id).get(subject_key, []),
            "handles": [
                item.model_dump(mode="json")
                for item in self.engine.subject_handles(scope_id, subject_key)
            ],
            "related": [
                item.to_dict()
                for item in (
                    self.engine.related_matters(scope_id, subject_key)
                    if subject.subject_type
                    == self.engine.profile.primary_subject.type
                    else []
                )
            ],
            "graph": self.engine.matter_graph(
                scope_id, subject_key
            ).to_dict(),
            "gather": (
                self.engine.gather_view(scope_id, subject_key).to_dict()
                if subject.layer >= 2
                else None
            ),
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

    def upload_raw(
        self,
        *,
        scope_id: str,
        filename: str,
        content: bytes,
        now: Any = None,
    ) -> dict[str, Any]:
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".mbox", ".eml", ".yaml", ".json"}:
            from matterhorn.errors import IngestFormatError

            raise IngestFormatError(
                "Upload filename MUST end in .mbox, .eml, .yaml, or .json."
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            from matterhorn.errors import IngestFormatError

            raise IngestFormatError("Upload MUST be UTF-8 text.") from error
        return self.ingest_raw(
            scope_id=scope_id,
            text=text,
            wait=True,
            now=now,
        )

    def quick_message(
        self,
        *,
        scope_id: str,
        sender: str,
        text: str,
        sent_at: datetime | None = None,
    ) -> dict[str, Any]:
        instant = sent_at or self.engine.now()
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        sender_id = re.sub(
            r"[^a-z0-9@._+-]+",
            "-",
            sender.casefold(),
        ).strip("-") or "unknown"
        message = Message.model_validate(
            {
                "id": f"quick-{uuid.uuid4().hex}",
                "sender": {"id": sender_id, "name": sender},
                "text": text,
                "sent_at": instant,
                "conversation_id": "console-quick",
            }
        )
        return self.add_messages(
            scope_id=scope_id,
            messages=[message],
            wait=True,
        )

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

    def merge_subjects(
        self,
        *,
        scope_id: str,
        source_subject_key: str,
        target_subject_key: str,
        source_refs: list[SourceRef | dict[str, Any]],
        valid_from: datetime | None = None,
    ) -> dict[str, Any]:
        return self.engine.merge_subjects(
            scope_id,
            source_subject_key,
            target_subject_key,
            source_refs=[
                ref
                if isinstance(ref, SourceRef)
                else SourceRef.model_validate(ref)
                for ref in source_refs
            ],
            valid_from=valid_from or self.engine.now(),
        ).model_dump(mode="json")

    def unmerge_subjects(
        self,
        *,
        scope_id: str,
        source_subject_key: str,
        source_refs: list[SourceRef | dict[str, Any]],
        valid_from: datetime | None = None,
    ) -> dict[str, Any]:
        return self.engine.unmerge_subjects(
            scope_id,
            source_subject_key,
            source_refs=[
                ref
                if isinstance(ref, SourceRef)
                else SourceRef.model_validate(ref)
                for ref in source_refs
            ],
            valid_from=valid_from or self.engine.now(),
        ).model_dump(mode="json")

    def subject_handles(
        self,
        *,
        scope_id: str,
        subject_key: str,
    ) -> list[dict[str, Any]]:
        self._require_subject(scope_id, subject_key)
        return [
            item.model_dump(mode="json")
            for item in self.engine.subject_handles(scope_id, subject_key)
        ]

    def bind_handle(
        self,
        *,
        scope_id: str,
        subject_key: str,
        handle_type: str,
        handle_value: str,
        source_refs: list[SourceRef | dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_subject(scope_id, subject_key)
        return self.engine.bind_handle(
            scope_id,
            subject_key,
            handle_type,
            handle_value,
            source_refs=source_refs,
        ).model_dump(mode="json")

    def unbind_handle(
        self,
        *,
        scope_id: str,
        subject_key: str,
        handle_type: str,
        normalized_value: str,
        source_refs: list[SourceRef | dict[str, Any]],
    ) -> dict[str, Any]:
        self._require_subject(scope_id, subject_key)
        return self.engine.unbind_handle(
            scope_id,
            subject_key,
            handle_type,
            normalized_value,
            source_refs=source_refs,
        ).model_dump(mode="json")

    def review_items(self, *, scope_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.engine.review_items(scope_id)
        ]

    def resolve_review(
        self,
        *,
        scope_id: str,
        review_id: str,
        action: str,
        subject_key: str | None,
        source_refs: list[SourceRef | dict[str, Any]],
        parent_subject_key: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.resolve_review(
            scope_id,
            review_id,
            action=action,
            subject_key=subject_key,
            parent_subject_key=parent_subject_key,
            source_refs=source_refs,
        ).model_dump(mode="json")

    def events(
        self, *, scope_id: str, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        self._require_scope(scope_id)
        return [
            item.model_dump(mode="json")
            for item in self.engine.events(scope_id, since=since)
        ]

    def activity(
        self,
        *,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("activity limit MUST be positive")
        activity: list[dict[str, Any]] = []
        for scope_id in self.list_scopes():
            titles = {
                item.subject_key: item.title
                for item in self.engine.store.subjects(scope_id)
            }
            for item in self.engine.events(scope_id, since=since):
                activity.append(
                    {
                        **item.model_dump(mode="json"),
                        "matter_title": titles.get(
                            item.subject_key,
                            item.subject_key,
                        ),
                    }
                )
        activity.sort(
            key=lambda item: (item["recorded_at"], item["event_id"]),
            reverse=True,
        )
        return activity[:limit]

    def connections(self) -> dict[str, Any]:
        scopes = []
        distill_queue_length = 0
        for scope_id in self.list_scopes():
            tasks = self.engine.store.tasks(scope_id)
            observations = self.engine.store.record_observations(scope_id)
            ingestion_times = [
                *(item.created_at for item in tasks),
                *(item.observed_at for item in observations),
            ]
            scopes.append(
                {
                    "scope_id": scope_id,
                    "last_ingestion_at": (
                        max(ingestion_times) if ingestion_times else None
                    ),
                    "message_count": (
                        sum(
                            item.accepted
                            for item in tasks
                            if item.kind == "messages"
                        )
                        + len(observations)
                    ),
                }
            )
            distill_queue_length += self.engine.store.distill_queue_count(
                scope_id
            )
        return {
            "scopes": scopes,
            "distill_queue_length": distill_queue_length,
        }

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
