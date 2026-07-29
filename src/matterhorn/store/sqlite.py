from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock

from matterhorn.canonical import canonical_json, derive_event_id, instant_text
from matterhorn.contracts import (
    Assertion,
    ChangeEvent,
    EpisodeCard,
    EvidenceRef,
    EvidenceStatus,
    GateStatistics,
    Interval,
    MemoryCard,
    ProjectionStats,
    SourceRef,
    SubjectRecord,
    SyncPosition,
    TaskGate,
    TaskResult,
    TaskStatus,
)
from matterhorn.store.base import (
    DistillQueueItem,
    QuerySubjectRow,
    QueryValueRow,
    RecordObservationRow,
    TaskRow,
)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ingested_cards (
    scope_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (scope_id, card_id)
);
CREATE TABLE IF NOT EXISTS record_observations (
    scope_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    observation_hash TEXT NOT NULL,
    container_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (scope_id, record_id, observation_hash)
);
CREATE INDEX IF NOT EXISTS idx_record_observations_container
    ON record_observations(scope_id, container_id, observed_at);
CREATE TABLE IF NOT EXISTS evidence_sources (
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    uri TEXT,
    revoked_at TEXT,
    PRIMARY KEY (scope_id, source_id)
);
CREATE TABLE IF NOT EXISTS sync_positions (
    scope_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    watermark TEXT NOT NULL,
    cursor TEXT,
    PRIMARY KEY (scope_id, container_id)
);
CREATE TABLE IF NOT EXISTS subjects (
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    parent_subject_key TEXT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    thread_ids_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (scope_id, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_subjects_title
    ON subjects(scope_id, subject_type, normalized_title);
CREATE TABLE IF NOT EXISTS assertions (
    assertion_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    predicate TEXT NOT NULL,
    operation TEXT NOT NULL,
    object_value_json TEXT,
    object_key TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    observation_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_assertions_projection
    ON assertions(scope_id, subject_key, predicate, valid_from);
CREATE INDEX IF NOT EXISTS idx_assertions_scope_predicate
    ON assertions(scope_id, predicate);
CREATE TABLE IF NOT EXISTS intervals (
    interval_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_value_json TEXT NOT NULL,
    object_key TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    assertion_id TEXT NOT NULL,
    supporting_assertion_ids_json TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    origin TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intervals_current
    ON intervals(scope_id, subject_key, predicate, valid_to);
CREATE INDEX IF NOT EXISTS idx_intervals_at
    ON intervals(scope_id, subject_key, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_intervals_object
    ON intervals(scope_id, predicate, object_key, valid_to);
CREATE TABLE IF NOT EXISTS memory_cards (
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    title TEXT NOT NULL,
    current_json TEXT NOT NULL,
    updated_at TEXT,
    source_ids_json TEXT NOT NULL,
    PRIMARY KEY (scope_id, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_cards_scope
    ON memory_cards(scope_id, subject_type);
CREATE TABLE IF NOT EXISTS projection_stats (
    scope_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    conflicts_resolved INTEGER NOT NULL,
    PRIMARY KEY (scope_id, predicate)
);
CREATE TABLE IF NOT EXISTS distill_queue (
    scope_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    card_json TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (scope_id, card_id)
);
CREATE INDEX IF NOT EXISTS idx_distill_queue_scope
    ON distill_queue(scope_id, card_id);
CREATE TABLE IF NOT EXISTS gate_stats (
    scope_id TEXT NOT NULL,
    counter TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (scope_id, counter)
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    newest_message_at TEXT,
    status TEXT NOT NULL,
    cards_produced INTEGER NOT NULL DEFAULT 0,
    new_assertions INTEGER NOT NULL DEFAULT 0,
    gate_accepted INTEGER NOT NULL DEFAULT 0,
    gate_rejected_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_tasks_scope_status
    ON tasks(scope_id, status, created_at, task_id);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    old_value_json TEXT NOT NULL,
    new_value_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    source_ids_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_scope_recorded
    ON events(scope_id, recorded_at, event_id);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    scope_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    PRIMARY KEY (event_id, webhook_url)
);
"""


class SQLiteStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self._transaction_depth = 0
        self._transaction_lock = RLock()

    def _migrate_schema(self) -> None:
        subject_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(subjects)")
        }
        if "parent_subject_key" not in subject_columns:
            self.connection.execute(
                "ALTER TABLE subjects ADD COLUMN parent_subject_key TEXT"
            )
        if "thread_ids_json" not in subject_columns:
            self.connection.execute(
                """
                ALTER TABLE subjects
                ADD COLUMN thread_ids_json TEXT NOT NULL DEFAULT '[]'
                """
            )
        assertion_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(assertions)")
        }
        if "observation_id" not in assertion_columns:
            self.connection.execute(
                "ALTER TABLE assertions ADD COLUMN observation_id TEXT"
            )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(intervals)")
        }
        if "supporting_assertion_ids_json" not in columns:
            self.connection.execute(
                """
                ALTER TABLE intervals
                ADD COLUMN supporting_assertion_ids_json TEXT NOT NULL DEFAULT '[]'
                """
            )
        legacy_rows = self.connection.execute(
            """
            SELECT interval_id, assertion_id FROM intervals
            WHERE supporting_assertion_ids_json = '[]'
            """
        )
        self.connection.executemany(
            """
            UPDATE intervals SET supporting_assertion_ids_json=?
            WHERE interval_id=?
            """,
            [
                (canonical_json([row["assertion_id"]]), row["interval_id"])
                for row in legacy_rows
            ],
        )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._transaction_lock:
            outer = self._transaction_depth == 0
            name = f"mh_sp_{self._transaction_depth}"
            if outer:
                self.connection.execute("BEGIN IMMEDIATE")
            else:
                self.connection.execute(f"SAVEPOINT {name}")
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                self._transaction_depth -= 1
                if outer:
                    self.connection.execute("ROLLBACK")
                else:
                    self.connection.execute(f"ROLLBACK TO {name}")
                    self.connection.execute(f"RELEASE {name}")
                raise
            else:
                self._transaction_depth -= 1
                if outer:
                    self.connection.execute("COMMIT")
                else:
                    self.connection.execute(f"RELEASE {name}")

    def close(self) -> None:
        self.connection.close()

    def clear_scope(self, scope_id: str) -> None:
        with self.transaction():
            for table in (
                "webhook_deliveries",
                "events",
                "tasks",
                "distill_queue",
                "gate_stats",
                "projection_stats",
                "memory_cards",
                "intervals",
                "assertions",
                "subjects",
                "sync_positions",
                "evidence_sources",
                "record_observations",
                "ingested_cards",
            ):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE scope_id=?", (scope_id,)
                )

    def scope_exists(self, scope_id: str) -> bool:
        tables = (
            "subjects",
            "assertions",
            "tasks",
            "events",
            "ingested_cards",
            "record_observations",
        )
        return any(
            self.connection.execute(
                f"SELECT 1 FROM {table} WHERE scope_id=? LIMIT 1", (scope_id,)
            ).fetchone()
            is not None
            for table in tables
        )

    def card_payload_hash(self, scope_id: str, card_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT payload_hash FROM ingested_cards WHERE scope_id=? AND card_id=?",
            (scope_id, card_id),
        ).fetchone()
        return row["payload_hash"] if row else None

    def mark_card(self, scope_id: str, card_id: str, payload_hash: str) -> None:
        self.connection.execute(
            "INSERT INTO ingested_cards(scope_id,card_id,payload_hash) VALUES(?,?,?)",
            (scope_id, card_id, payload_hash),
        )

    def has_record_observation(
        self, scope_id: str, record_id: str, observation_hash: str
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM record_observations
            WHERE scope_id=? AND record_id=? AND observation_hash=?
            """,
            (scope_id, record_id, observation_hash),
        ).fetchone()
        return row is not None

    def mark_record_observation(
        self,
        scope_id: str,
        record_id: str,
        observation_hash: str,
        container_id: str,
        observed_at: datetime,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO record_observations(
              scope_id,record_id,observation_hash,container_id,observed_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                scope_id,
                record_id,
                observation_hash,
                container_id,
                instant_text(observed_at),
            ),
        )

    def record_observations(self, scope_id: str) -> list[RecordObservationRow]:
        rows = self.connection.execute(
            """
            SELECT * FROM record_observations WHERE scope_id=?
            ORDER BY record_id COLLATE BINARY,observation_hash COLLATE BINARY
            """,
            (scope_id,),
        )
        return [RecordObservationRow(**dict(row)) for row in rows]

    def observe_source(
        self,
        scope_id: str,
        source_ref: SourceRef,
        *,
        revoked_at: datetime | None = None,
    ) -> None:
        revoked_text = instant_text(revoked_at) if revoked_at is not None else None
        self.connection.execute(
            """
            INSERT INTO evidence_sources(scope_id,source_id,uri,revoked_at)
            VALUES(?,?,?,?)
            ON CONFLICT(scope_id,source_id) DO UPDATE SET
              uri=COALESCE(evidence_sources.uri,excluded.uri),
              revoked_at=CASE
                WHEN evidence_sources.revoked_at IS NULL THEN excluded.revoked_at
                WHEN excluded.revoked_at IS NULL THEN evidence_sources.revoked_at
                WHEN evidence_sources.revoked_at <= excluded.revoked_at
                  THEN evidence_sources.revoked_at
                ELSE excluded.revoked_at
              END
            """,
            (scope_id, source_ref.source_id, source_ref.uri, revoked_text),
        )

    def source_states(
        self, scope_id: str, source_refs: list[SourceRef]
    ) -> list[EvidenceRef]:
        if not source_refs:
            return []
        source_ids = list(dict.fromkeys(ref.source_id for ref in source_refs))
        placeholders = ",".join("?" for _ in source_ids)
        rows = self.connection.execute(
            f"""
            SELECT source_id,uri,revoked_at FROM evidence_sources
            WHERE scope_id=? AND source_id IN ({placeholders})
            """,
            (scope_id, *source_ids),
        )
        state = {row["source_id"]: row for row in rows}
        result = []
        for ref in source_refs:
            row = state.get(ref.source_id)
            revoked_at = row["revoked_at"] if row is not None else None
            result.append(
                EvidenceRef(
                    source_id=ref.source_id,
                    uri=(row["uri"] if row is not None else None) or ref.uri,
                    status=(
                        EvidenceStatus.revoked
                        if revoked_at is not None
                        else EvidenceStatus.active
                    ),
                    revoked_at=revoked_at,
                )
            )
        return result

    def source_metadata(self, scope_id: str) -> list[EvidenceRef]:
        rows = self.connection.execute(
            """
            SELECT source_id,uri,revoked_at FROM evidence_sources
            WHERE scope_id=? ORDER BY source_id COLLATE BINARY
            """,
            (scope_id,),
        )
        return [
            EvidenceRef(
                source_id=row["source_id"],
                uri=row["uri"],
                status=(
                    EvidenceStatus.revoked
                    if row["revoked_at"] is not None
                    else EvidenceStatus.active
                ),
                revoked_at=row["revoked_at"],
            )
            for row in rows
        ]

    def put_source_state(self, scope_id: str, source: EvidenceRef) -> None:
        self.connection.execute(
            """
            INSERT INTO evidence_sources(scope_id,source_id,uri,revoked_at)
            VALUES(?,?,?,?)
            ON CONFLICT(scope_id,source_id) DO UPDATE SET
              uri=excluded.uri,
              revoked_at=excluded.revoked_at
            """,
            (
                scope_id,
                source.source_id,
                source.uri,
                (
                    instant_text(source.revoked_at)
                    if source.revoked_at is not None
                    else None
                ),
            ),
        )

    def update_sync_position(
        self,
        scope_id: str,
        container_id: str,
        *,
        watermark: datetime,
        cursor: str | None,
    ) -> None:
        watermark_text = instant_text(watermark)
        self.connection.execute(
            """
            INSERT INTO sync_positions(scope_id,container_id,watermark,cursor)
            VALUES(?,?,?,?)
            ON CONFLICT(scope_id,container_id) DO UPDATE SET
              watermark=CASE
                WHEN sync_positions.watermark >= excluded.watermark
                  THEN sync_positions.watermark
                ELSE excluded.watermark
              END,
              cursor=COALESCE(excluded.cursor,sync_positions.cursor)
            """,
            (scope_id, container_id, watermark_text, cursor),
        )

    def sync_positions(self, scope_id: str) -> list[SyncPosition]:
        rows = self.connection.execute(
            """
            SELECT * FROM sync_positions WHERE scope_id=?
            ORDER BY container_id COLLATE BINARY
            """,
            (scope_id,),
        )
        return [SyncPosition.model_validate(dict(row)) for row in rows]

    def subjects(self, scope_id: str) -> list[SubjectRecord]:
        rows = self.connection.execute(
            "SELECT * FROM subjects WHERE scope_id=? ORDER BY subject_key", (scope_id,)
        )
        return [
            SubjectRecord(
                scope_id=row["scope_id"],
                subject_key=row["subject_key"],
                subject_type=row["subject_type"],
                title=row["title"],
                normalized_title=row["normalized_title"],
                source_ids=frozenset(json.loads(row["source_ids_json"])),
                parent_subject_key=row["parent_subject_key"],
                thread_ids=frozenset(json.loads(row["thread_ids_json"])),
            )
            for row in rows
        ]

    def upsert_subject(self, subject: SubjectRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO subjects(
              scope_id,subject_key,subject_type,parent_subject_key,title,
              normalized_title,source_ids_json,thread_ids_json
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(scope_id,subject_key) DO UPDATE SET
              source_ids_json=excluded.source_ids_json,
              thread_ids_json=excluded.thread_ids_json
            """,
            (
                subject.scope_id,
                subject.subject_key,
                subject.subject_type,
                subject.parent_subject_key,
                subject.title,
                subject.normalized_title,
                canonical_json(sorted(subject.source_ids)),
                canonical_json(sorted(subject.thread_ids)),
            ),
        )

    def add_assertion(self, assertion: Assertion) -> bool:
        payload = self._assertion_tuple(assertion)
        existing = self.connection.execute(
            "SELECT * FROM assertions WHERE assertion_id=?", (assertion.assertion_id,)
        ).fetchone()
        if existing:
            prior = self._row_to_assertion(existing)
            if self._immutable_assertion_payload(
                prior
            ) != self._immutable_assertion_payload(assertion):
                raise ValueError("assertion_id collision with different immutable payload")
            return False
        self.connection.execute(
            """
            INSERT INTO assertions(
              assertion_id,scope_id,subject_key,subject_type,predicate,operation,
              object_value_json,object_key,valid_from,recorded_at,source_refs_json,
              origin,observation_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            payload,
        )
        return True

    def assertions(self, scope_id: str) -> list[Assertion]:
        rows = self.connection.execute(
            """
            SELECT * FROM assertions WHERE scope_id=?
            ORDER BY subject_key,predicate,valid_from,assertion_id
            """,
            (scope_id,),
        )
        return [self._row_to_assertion(row) for row in rows]

    def intervals(self, scope_id: str) -> list[Interval]:
        rows = self.connection.execute(
            """
            SELECT * FROM intervals WHERE scope_id=?
            ORDER BY subject_key,predicate,valid_from,object_key,interval_id
            """,
            (scope_id,),
        )
        return [self._row_to_interval(row) for row in rows]

    def memory_cards(self, scope_id: str) -> list[MemoryCard]:
        rows = self.connection.execute(
            "SELECT * FROM memory_cards WHERE scope_id=? ORDER BY subject_key",
            (scope_id,),
        )
        return [
            MemoryCard.model_validate(
                {
                    "scope_id": row["scope_id"],
                    "subject_key": row["subject_key"],
                    "subject_type": row["subject_type"],
                    "title": row["title"],
                    "current": json.loads(row["current_json"]),
                    "updated_at": row["updated_at"],
                    "source_ids": json.loads(row["source_ids_json"]),
                }
            )
            for row in rows
        ]

    def projection_stats(self, scope_id: str) -> list[ProjectionStats]:
        rows = self.connection.execute(
            "SELECT * FROM projection_stats WHERE scope_id=? ORDER BY predicate",
            (scope_id,),
        )
        return [ProjectionStats.model_validate(dict(row)) for row in rows]

    def enqueue_distill(
        self,
        card: EpisodeCard,
        *,
        subject_key: str,
        subject_type: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO distill_queue(
              scope_id,card_id,card_json,subject_key,subject_type
            ) VALUES(?,?,?,?,?)
            """,
            (
                card.scope_id,
                card.card_id,
                canonical_json(card.model_dump(mode="json")),
                subject_key,
                subject_type,
            ),
        )
        return cursor.rowcount == 1

    def distill_queue(
        self, scope_id: str, limit: int | None = None
    ) -> list[DistillQueueItem]:
        sql = """
            SELECT * FROM distill_queue
            WHERE scope_id=? ORDER BY card_id
        """
        parameters: tuple = (scope_id,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (scope_id, limit)
        rows = self.connection.execute(sql, parameters)
        return [
            DistillQueueItem(
                scope_id=row["scope_id"],
                card_id=row["card_id"],
                card=EpisodeCard.model_validate(json.loads(row["card_json"])),
                subject_key=row["subject_key"],
                subject_type=row["subject_type"],
                attempt_count=row["attempt_count"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def remove_distill_item(self, scope_id: str, card_id: str) -> None:
        self.connection.execute(
            "DELETE FROM distill_queue WHERE scope_id=? AND card_id=?",
            (scope_id, card_id),
        )

    def fail_distill_item(self, scope_id: str, card_id: str, error: str) -> None:
        self.connection.execute(
            """
            UPDATE distill_queue
            SET attempt_count=attempt_count+1,last_error=?
            WHERE scope_id=? AND card_id=?
            """,
            (error, scope_id, card_id),
        )

    def distill_queue_count(self, scope_id: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) AS n FROM distill_queue WHERE scope_id=?", (scope_id,)
        ).fetchone()["n"]

    def record_gate_report(
        self,
        scope_id: str,
        *,
        accepted: int,
        rejections: dict[str, int],
    ) -> None:
        counters = {"ACCEPTED": accepted, **rejections}
        self.connection.executemany(
            """
            INSERT INTO gate_stats(scope_id,counter,count) VALUES(?,?,?)
            ON CONFLICT(scope_id,counter) DO UPDATE SET count=count+excluded.count
            """,
            [
                (scope_id, counter, count)
                for counter, count in counters.items()
                if count
            ],
        )

    def gate_statistics(self, scope_id: str) -> GateStatistics:
        rows = self.connection.execute(
            """
            SELECT counter,count FROM gate_stats
            WHERE scope_id=? ORDER BY counter COLLATE BINARY
            """,
            (scope_id,),
        )
        counters = {row["counter"]: row["count"] for row in rows}
        return GateStatistics(
            scope_id=scope_id,
            accepted=counters.pop("ACCEPTED", 0),
            rejections=counters,
        )

    def create_task(
        self,
        *,
        task_id: str,
        scope_id: str,
        kind: str,
        payload: dict,
        accepted: int,
        created_at: datetime,
        newest_message_at: datetime | None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO tasks(
              task_id,scope_id,kind,payload_json,accepted,created_at,
              newest_message_at,status
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                task_id,
                scope_id,
                kind,
                canonical_json(payload),
                accepted,
                instant_text(created_at),
                (
                    instant_text(newest_message_at)
                    if newest_message_at is not None
                    else None
                ),
                TaskStatus.pending.value,
            ),
        )
        return cursor.rowcount == 1

    def task(self, task_id: str) -> TaskRow | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._task_row(row) if row is not None else None

    def tasks(
        self, scope_id: str, *, status: TaskStatus | None = None
    ) -> list[TaskRow]:
        sql = """
            SELECT * FROM tasks WHERE scope_id=?
        """
        parameters: tuple = (scope_id,)
        if status is not None:
            sql += " AND status=?"
            parameters = (scope_id, status.value)
        sql += " ORDER BY created_at,task_id COLLATE BINARY"
        return [
            self._task_row(row)
            for row in self.connection.execute(sql, parameters)
        ]

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        cards_produced: int = 0,
        new_assertions: int = 0,
        gate_accepted: int = 0,
        gate_rejected: dict[str, int] | None = None,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE tasks SET
              status=?,cards_produced=?,new_assertions=?,
              gate_accepted=?,gate_rejected_json=?
            WHERE task_id=?
            """,
            (
                status.value,
                cards_produced,
                new_assertions,
                gate_accepted,
                canonical_json(gate_rejected or {}),
                task_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown task_id: {task_id}")

    def quiet_scopes(self, cutoff: datetime) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT scope_id FROM tasks
            WHERE status=? AND kind='messages' AND newest_message_at IS NOT NULL
            GROUP BY scope_id
            HAVING MAX(newest_message_at) <= ?
            ORDER BY scope_id COLLATE BINARY
            """,
            (TaskStatus.pending.value, instant_text(cutoff)),
        )
        return [row["scope_id"] for row in rows]

    def pending_scopes(self) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT scope_id FROM tasks
            WHERE status=?
            ORDER BY scope_id COLLATE BINARY
            """,
            (TaskStatus.pending.value,),
        )
        return [row["scope_id"] for row in rows]

    def add_event(self, event: ChangeEvent) -> bool:
        expected_id = derive_event_id(
            event.event_type,
            event.scope_id,
            event.subject_key,
            event.predicate,
            event.old_value,
            event.new_value,
            event.valid_from,
            event.recorded_at,
            event.origin,
            event.source_ids,
        )
        if event.event_id != expected_id:
            raise ValueError("event_id does not match the deterministic payload hash")
        existing = self.connection.execute(
            "SELECT * FROM events WHERE event_id=?", (event.event_id,)
        ).fetchone()
        if existing is not None:
            prior = self._row_to_event(existing)
            if canonical_json(prior.model_dump(mode="json")) != canonical_json(
                event.model_dump(mode="json")
            ):
                raise ValueError("event_id collision with different event payload")
            return False
        self.connection.execute(
            """
            INSERT INTO events(
              event_id,event_type,scope_id,subject_key,predicate,
              old_value_json,new_value_json,valid_from,recorded_at,origin,
              source_ids_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            self._event_tuple(event),
        )
        return True

    def events(
        self, scope_id: str, *, since: datetime | None = None
    ) -> list[ChangeEvent]:
        sql = "SELECT * FROM events WHERE scope_id=?"
        parameters: tuple = (scope_id,)
        if since is not None:
            sql += " AND recorded_at>=?"
            parameters = (scope_id, instant_text(since))
        sql += " ORDER BY recorded_at,event_id COLLATE BINARY"
        return [
            self._row_to_event(row)
            for row in self.connection.execute(sql, parameters)
        ]

    def pending_webhook_events(
        self, webhook_url: str, *, limit: int = 100
    ) -> list[ChangeEvent]:
        rows = self.connection.execute(
            """
            SELECT e.* FROM events e
            WHERE NOT EXISTS (
              SELECT 1 FROM webhook_deliveries d
              WHERE d.event_id=e.event_id AND d.webhook_url=?
            )
            ORDER BY e.recorded_at,e.event_id COLLATE BINARY
            LIMIT ?
            """,
            (webhook_url, limit),
        )
        return [self._row_to_event(row) for row in rows]

    def mark_webhook_delivered(
        self,
        webhook_url: str,
        event_ids: list[str],
        *,
        delivered_at: datetime,
    ) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        rows = self.connection.execute(
            f"""
            SELECT event_id,scope_id FROM events
            WHERE event_id IN ({placeholders})
            """,
            tuple(event_ids),
        )
        scope_by_event = {row["event_id"]: row["scope_id"] for row in rows}
        delivered_text = instant_text(delivered_at)
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO webhook_deliveries(
              scope_id,event_id,webhook_url,delivered_at
            ) VALUES(?,?,?,?)
            """,
            [
                (scope_by_event[event_id], event_id, webhook_url, delivered_text)
                for event_id in event_ids
                if event_id in scope_by_event
            ],
        )

    def query_current_values(
        self,
        scope_id: str,
        subject_key: str,
        predicate: str,
        *,
        append: bool,
    ) -> list[QueryValueRow]:
        if append:
            rows = self.connection.execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=? AND i.subject_key=? AND i.predicate=?
                ORDER BY i.valid_from DESC,
                         i.assertion_id COLLATE BINARY DESC
                LIMIT 1
                """,
                (scope_id, subject_key, predicate),
            )
        else:
            rows = self.connection.execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=? AND i.subject_key=? AND i.predicate=?
                  AND i.valid_to IS NULL
                ORDER BY i.object_key COLLATE BINARY
                """,
                (scope_id, subject_key, predicate),
            )
        return [self._query_value_row(row) for row in rows]

    def query_timeline_values(
        self, scope_id: str, subject_key: str, predicate: str
    ) -> list[QueryValueRow]:
        rows = self.connection.execute(
            """
            SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
            JOIN assertions a ON a.assertion_id=i.assertion_id
            WHERE i.scope_id=? AND i.subject_key=? AND i.predicate=?
            ORDER BY i.valid_from,
                     i.object_key COLLATE BINARY,
                     i.assertion_id COLLATE BINARY
            """,
            (scope_id, subject_key, predicate),
        )
        return [self._query_value_row(row) for row in rows]

    def query_values_at(
        self,
        scope_id: str,
        subject_key: str,
        predicate: str,
        instant: datetime,
        *,
        append: bool,
    ) -> list[QueryValueRow]:
        moment = instant_text(instant)
        if append:
            rows = self.connection.execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=? AND i.subject_key=? AND i.predicate=?
                  AND i.valid_from<=?
                ORDER BY i.valid_from DESC,
                         i.assertion_id COLLATE BINARY DESC
                LIMIT 1
                """,
                (scope_id, subject_key, predicate, moment),
            )
        else:
            rows = self.connection.execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=? AND i.subject_key=? AND i.predicate=?
                  AND i.valid_from<=?
                  AND (i.valid_to IS NULL OR i.valid_to>?)
                ORDER BY i.object_key COLLATE BINARY
                """,
                (scope_id, subject_key, predicate, moment, moment),
            )
        return [self._query_value_row(row) for row in rows]

    def query_subjects_by_object(
        self,
        scope_id: str,
        predicates: list[str],
        object_key: str,
    ) -> list[QuerySubjectRow]:
        if not predicates:
            return []
        placeholders = ",".join("?" for _ in predicates)
        rows = self.connection.execute(
            f"""
            SELECT m.* FROM memory_cards m
            WHERE m.scope_id=?
              AND EXISTS (
                SELECT 1 FROM intervals i
                WHERE i.scope_id=m.scope_id
                  AND i.subject_key=m.subject_key
                  AND i.predicate IN ({placeholders})
                  AND i.object_key=?
                  AND i.valid_to IS NULL
              )
            ORDER BY m.subject_key COLLATE BINARY
            """,
            (scope_id, *predicates, object_key),
        )
        return [self._query_subject_row(row) for row in rows]

    def query_subjects_by_type(
        self, scope_id: str, subject_type: str
    ) -> list[QuerySubjectRow]:
        rows = self.connection.execute(
            """
            SELECT * FROM memory_cards
            WHERE scope_id=? AND subject_type=?
            ORDER BY subject_key COLLATE BINARY
            """,
            (scope_id, subject_type),
        )
        return [self._query_subject_row(row) for row in rows]

    def query_completion_counts(
        self,
        scope_id: str,
        predicate: str | None,
        completed_object_keys: list[str],
    ) -> tuple[int, int]:
        total = self.connection.execute(
            "SELECT COUNT(*) AS n FROM memory_cards WHERE scope_id=?",
            (scope_id,),
        ).fetchone()["n"]
        if predicate is None or not completed_object_keys or total == 0:
            return 0, total
        placeholders = ",".join("?" for _ in completed_object_keys)
        completed = self.connection.execute(
            f"""
            SELECT COUNT(DISTINCT subject_key) AS n FROM intervals
            WHERE scope_id=? AND predicate=? AND valid_to IS NULL
              AND object_key IN ({placeholders})
            """,
            (scope_id, predicate, *completed_object_keys),
        ).fetchone()["n"]
        return completed, total

    def replace_projection(
        self,
        scope_id: str,
        intervals: list[Interval],
        cards: list[MemoryCard],
        stats: list[ProjectionStats],
    ) -> None:
        self.connection.execute("DELETE FROM intervals WHERE scope_id=?", (scope_id,))
        self.connection.execute("DELETE FROM memory_cards WHERE scope_id=?", (scope_id,))
        self.connection.execute(
            "DELETE FROM projection_stats WHERE scope_id=?", (scope_id,)
        )
        self.connection.executemany(
            """
            INSERT INTO intervals(
              interval_id,scope_id,subject_key,subject_type,predicate,object_value_json,
              object_key,valid_from,valid_to,assertion_id,
              supporting_assertion_ids_json,source_refs_json,origin
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [self._interval_tuple(item) for item in intervals],
        )
        self.connection.executemany(
            """
            INSERT INTO memory_cards(
              scope_id,subject_key,subject_type,title,current_json,updated_at,source_ids_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            [
                (
                    item.scope_id,
                    item.subject_key,
                    item.subject_type,
                    item.title,
                    canonical_json(item.current),
                    instant_text(item.updated_at) if item.updated_at else None,
                    canonical_json(item.source_ids),
                )
                for item in cards
            ],
        )
        self.connection.executemany(
            """
            INSERT INTO projection_stats(scope_id,predicate,conflicts_resolved)
            VALUES(?,?,?)
            """,
            [
                (item.scope_id, item.predicate, item.conflicts_resolved)
                for item in stats
            ],
        )

    @staticmethod
    def _assertion_tuple(assertion: Assertion) -> tuple:
        return (
            assertion.assertion_id,
            assertion.scope_id,
            assertion.subject_key,
            assertion.subject_type,
            assertion.predicate,
            assertion.operation.value,
            canonical_json(assertion.object_value),
            assertion.object_key,
            instant_text(assertion.valid_from),
            instant_text(assertion.recorded_at),
            canonical_json(
                [ref.model_dump(mode="json") for ref in assertion.source_refs]
            ),
            assertion.origin.value,
            assertion.observation_id,
        )

    @staticmethod
    def _interval_tuple(interval: Interval) -> tuple:
        return (
            interval.interval_id,
            interval.scope_id,
            interval.subject_key,
            interval.subject_type,
            interval.predicate,
            canonical_json(interval.object_value),
            interval.object_key,
            instant_text(interval.valid_from),
            instant_text(interval.valid_to) if interval.valid_to else None,
            interval.assertion_id,
            canonical_json(interval.supporting_assertion_ids),
            canonical_json([ref.model_dump(mode="json") for ref in interval.source_refs]),
            interval.origin.value,
        )

    @staticmethod
    def _event_tuple(event: ChangeEvent) -> tuple:
        return (
            event.event_id,
            event.event_type.value,
            event.scope_id,
            event.subject_key,
            event.predicate,
            canonical_json(event.old_value),
            canonical_json(event.new_value),
            instant_text(event.valid_from),
            instant_text(event.recorded_at),
            event.origin.value,
            canonical_json(event.source_ids),
        )

    @staticmethod
    def _row_to_assertion(row: sqlite3.Row) -> Assertion:
        return Assertion.model_validate(
            {
                "assertion_id": row["assertion_id"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "subject_type": row["subject_type"],
                "predicate": row["predicate"],
                "operation": row["operation"],
                "object_value": json.loads(row["object_value_json"]),
                "object_key": row["object_key"],
                "valid_from": row["valid_from"],
                "recorded_at": row["recorded_at"],
                "source_refs": json.loads(row["source_refs_json"]),
                "origin": row["origin"],
                "observation_id": row["observation_id"],
            }
        )

    @staticmethod
    def _row_to_interval(row: sqlite3.Row) -> Interval:
        return Interval.model_validate(
            {
                "interval_id": row["interval_id"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "subject_type": row["subject_type"],
                "predicate": row["predicate"],
                "object_value": json.loads(row["object_value_json"]),
                "object_key": row["object_key"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "assertion_id": row["assertion_id"],
                "supporting_assertion_ids": json.loads(
                    row["supporting_assertion_ids_json"]
                ),
                "source_refs": json.loads(row["source_refs_json"]),
                "origin": row["origin"],
            }
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> ChangeEvent:
        return ChangeEvent.model_validate(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "predicate": row["predicate"],
                "old_value": json.loads(row["old_value_json"]),
                "new_value": json.loads(row["new_value_json"]),
                "valid_from": row["valid_from"],
                "recorded_at": row["recorded_at"],
                "origin": row["origin"],
                "source_ids": json.loads(row["source_ids_json"]),
            }
        )

    @staticmethod
    def _query_value_row(row: sqlite3.Row) -> QueryValueRow:
        return QueryValueRow(
            subject_key=row["subject_key"],
            predicate=row["predicate"],
            value=json.loads(row["object_value_json"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            recorded_at=row["recorded_at"],
            assertion_id=row["assertion_id"],
            supporting_assertion_ids=json.loads(
                row["supporting_assertion_ids_json"]
            ),
            source_refs=[
                SourceRef.model_validate(item)
                for item in json.loads(row["source_refs_json"])
            ],
            origin=row["origin"],
        )

    @staticmethod
    def _query_subject_row(row: sqlite3.Row) -> QuerySubjectRow:
        return QuerySubjectRow(
            subject_key=row["subject_key"],
            subject_type=row["subject_type"],
            title=row["title"],
            current=json.loads(row["current_json"]),
        )

    @staticmethod
    def _task_row(row: sqlite3.Row) -> TaskRow:
        status = TaskStatus(row["status"])
        return TaskRow(
            task_id=row["task_id"],
            scope_id=row["scope_id"],
            kind=row["kind"],
            payload=json.loads(row["payload_json"]),
            accepted=row["accepted"],
            created_at=row["created_at"],
            newest_message_at=row["newest_message_at"],
            result=TaskResult(
                task_id=row["task_id"],
                status=status,
                cards_produced=row["cards_produced"],
                new_assertions=row["new_assertions"],
                gate=TaskGate(
                    accepted=row["gate_accepted"],
                    rejected=json.loads(row["gate_rejected_json"]),
                ),
            ),
        )

    @staticmethod
    def _immutable_assertion_payload(assertion: Assertion) -> str:
        return canonical_json(
            {
                "scope_id": assertion.scope_id,
                "subject_key": assertion.subject_key,
                "subject_type": assertion.subject_type,
                "predicate": assertion.predicate,
                "operation": assertion.operation.value,
                "object_value": assertion.object_value,
                "object_key": assertion.object_key,
                "valid_from": instant_text(assertion.valid_from),
                "source_refs": [
                    ref.model_dump(mode="json") for ref in assertion.source_refs
                ],
                "origin": assertion.origin.value,
                "observation_id": assertion.observation_id,
            }
        )
