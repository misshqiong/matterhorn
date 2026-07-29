from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Iterator

from matterhorn.contracts import (
    Assertion,
    EpisodeCard,
    GateStatistics,
    Interval,
    MemoryCard,
    ProjectionStats,
)
from matterhorn.engine.canonical import canonical_json, instant_text
from matterhorn.engine.identity import SubjectRecord
from matterhorn.store.base import DistillQueueItem, QuerySubjectRow, QueryValueRow


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ingested_cards (
    scope_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (scope_id, card_id)
);
CREATE TABLE IF NOT EXISTS subjects (
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    parent_subject_key TEXT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
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
    origin TEXT NOT NULL
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
                "distill_queue",
                "gate_stats",
                "projection_stats",
                "memory_cards",
                "intervals",
                "assertions",
                "subjects",
                "ingested_cards",
            ):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE scope_id=?", (scope_id,)
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
            )
            for row in rows
        ]

    def upsert_subject(self, subject: SubjectRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO subjects(
              scope_id,subject_key,subject_type,parent_subject_key,title,
              normalized_title,source_ids_json
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(scope_id,subject_key) DO UPDATE SET
              source_ids_json=excluded.source_ids_json
            """,
            (
                subject.scope_id,
                subject.subject_key,
                subject.subject_type,
                subject.parent_subject_key,
                subject.title,
                subject.normalized_title,
                canonical_json(sorted(subject.source_ids)),
            ),
        )

    def add_assertion(self, assertion: Assertion) -> bool:
        payload = self._assertion_tuple(assertion)
        existing = self.connection.execute(
            "SELECT * FROM assertions WHERE assertion_id=?", (assertion.assertion_id,)
        ).fetchone()
        if existing:
            prior = self._row_to_assertion(existing)
            identity_fields = (
                prior.scope_id,
                prior.subject_key,
                prior.predicate,
                prior.operation,
                prior.object_key,
                prior.valid_from,
                sorted(ref.source_id for ref in prior.source_refs),
            )
            incoming_identity_fields = (
                assertion.scope_id,
                assertion.subject_key,
                assertion.predicate,
                assertion.operation,
                assertion.object_key,
                assertion.valid_from,
                sorted(ref.source_id for ref in assertion.source_refs),
            )
            if identity_fields != incoming_identity_fields:
                raise ValueError("assertion_id collision with different immutable payload")
            return False
        self.connection.execute(
            """
            INSERT INTO assertions(
              assertion_id,scope_id,subject_key,subject_type,predicate,operation,
              object_value_json,object_key,valid_from,recorded_at,source_refs_json,origin
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
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
            source_ids=[
                item["source_id"] for item in json.loads(row["source_refs_json"])
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
