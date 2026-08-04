from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from threading import RLock
from typing import Any

from matterhorn.canonical import (
    as_utc,
    canonical_json,
    derive_event_id,
    instant_text,
    json_value,
)
from matterhorn.contracts import (
    Assertion,
    ChangeEvent,
    EpisodeCard,
    EventType,
    EvidenceRef,
    EvidenceStatus,
    GateStatistics,
    Interval,
    MemoryCard,
    ProjectionStats,
    SourceRef,
    SubjectMerge,
    SubjectRecord,
    SyncPosition,
    TaskGate,
    TaskResult,
    TaskStatus,
)
from matterhorn.store.base import (
    MAX_TASK_ATTEMPTS,
    DistillQueueItem,
    QuerySubjectRow,
    QueryValueRow,
    RecordObservationRow,
    TaskRow,
    thread_safe_store,
)

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError as error:  # pragma: no cover - exercised without the extra
    raise ImportError(
        "PostgreSQL support requires the 'matterhorn[postgres]' extra"
    ) from error


SCHEMA_SQL = """
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
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope_id, record_id, observation_hash)
);
CREATE INDEX IF NOT EXISTS idx_record_observations_container
    ON record_observations(scope_id, container_id, observed_at);
CREATE TABLE IF NOT EXISTS evidence_sources (
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    uri TEXT,
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (scope_id, source_id)
);
CREATE TABLE IF NOT EXISTS sync_positions (
    scope_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    watermark TIMESTAMPTZ NOT NULL,
    cursor TEXT,
    uid_watermark BIGINT,
    PRIMARY KEY (scope_id, container_id)
);
CREATE TABLE IF NOT EXISTS subjects (
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    parent_subject_key TEXT,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    source_ids_json JSONB NOT NULL,
    thread_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (scope_id, subject_key)
);
CREATE INDEX IF NOT EXISTS idx_subjects_title
    ON subjects(scope_id, subject_type, normalized_title);
CREATE TABLE IF NOT EXISTS subject_merges (
    scope_id TEXT NOT NULL,
    source_subject_key TEXT NOT NULL,
    target_subject_key TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    source_refs_json JSONB NOT NULL,
    PRIMARY KEY (scope_id, source_subject_key)
);
CREATE TABLE IF NOT EXISTS assertions (
    assertion_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    predicate TEXT NOT NULL,
    operation TEXT NOT NULL,
    object_value_json JSONB NOT NULL,
    object_key TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    source_refs_json JSONB NOT NULL,
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
    object_value_json JSONB NOT NULL,
    object_key TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    assertion_id TEXT NOT NULL,
    supporting_assertion_ids_json JSONB NOT NULL,
    source_refs_json JSONB NOT NULL,
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
    current_json JSONB NOT NULL,
    updated_at TIMESTAMPTZ,
    source_ids_json JSONB NOT NULL,
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
    card_json JSONB NOT NULL,
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
    payload_json JSONB NOT NULL,
    accepted INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    newest_message_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    cards_produced INTEGER NOT NULL DEFAULT 0,
    new_assertions INTEGER NOT NULL DEFAULT 0,
    gate_accepted INTEGER NOT NULL DEFAULT 0,
    gate_rejected_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_scope_status
    ON tasks(scope_id, status, created_at, task_id);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    old_value_json JSONB NOT NULL,
    new_value_json JSONB NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    origin TEXT NOT NULL,
    source_ids_json JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_scope_recorded
    ON events(scope_id, recorded_at, event_id);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    scope_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    delivered_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (event_id, webhook_url)
);
"""


@thread_safe_store
class PostgresStore:
    """Single-primary PostgreSQL Store preserving Matterhorn's INV-6 boundary.

    The DSN MUST target the writable primary. Read/write splitting, transaction
    pooling that can switch servers, and replica routing are unsupported.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._lock = RLock()
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self._assert_writable_primary()
        with self.connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
            cursor.execute(
                """
                ALTER TABLE subjects
                ADD COLUMN IF NOT EXISTS thread_ids_json
                JSONB NOT NULL DEFAULT '[]'::jsonb
                """
            )
            cursor.execute(
                """
                ALTER TABLE assertions
                ADD COLUMN IF NOT EXISTS observation_id TEXT
                """
            )
            cursor.execute(
                """
                ALTER TABLE sync_positions
                ADD COLUMN IF NOT EXISTS uid_watermark BIGINT
                """
            )
            cursor.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0
                """
            )
            cursor.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN IF NOT EXISTS last_error TEXT
                """
            )

    def _assert_writable_primary(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('transaction_read_only') AS read_only, "
                "pg_is_in_recovery() AS in_recovery"
            )
            state = cursor.fetchone()
        if state["read_only"] == "on" or state["in_recovery"]:
            self.connection.close()
            raise RuntimeError(
                "Matterhorn PostgreSQL DSN MUST target a writable primary; "
                "read-only or replica connections violate INV-6"
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock, self.connection.transaction():
            yield

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
                "subject_merges",
                "subjects",
                "sync_positions",
                "evidence_sources",
                "record_observations",
                "ingested_cards",
            ):
                self._execute(f"DELETE FROM {table} WHERE scope_id=%s", (scope_id,))

    def scope_exists(self, scope_id: str) -> bool:
        tables = (
            "subjects",
            "assertions",
            "subject_merges",
            "tasks",
            "events",
            "ingested_cards",
            "record_observations",
        )
        return any(
            self._execute(
                f"SELECT 1 FROM {table} WHERE scope_id=%s LIMIT 1", (scope_id,)
            ).fetchone()
            is not None
            for table in tables
        )

    def list_scopes(self) -> list[str]:
        rows = self._execute(
            """
            SELECT DISTINCT scope_id COLLATE "C" AS scope_id FROM (
                SELECT scope_id FROM ingested_cards
                UNION ALL SELECT scope_id FROM record_observations
                UNION ALL SELECT scope_id FROM evidence_sources
                UNION ALL SELECT scope_id FROM sync_positions
                UNION ALL SELECT scope_id FROM subjects
                UNION ALL SELECT scope_id FROM subject_merges
                UNION ALL SELECT scope_id FROM assertions
                UNION ALL SELECT scope_id FROM intervals
                UNION ALL SELECT scope_id FROM memory_cards
                UNION ALL SELECT scope_id FROM projection_stats
                UNION ALL SELECT scope_id FROM distill_queue
                UNION ALL SELECT scope_id FROM gate_stats
                UNION ALL SELECT scope_id FROM tasks
                UNION ALL SELECT scope_id FROM events
                UNION ALL SELECT scope_id FROM webhook_deliveries
            ) AS stored_scopes
            ORDER BY scope_id
            """
        )
        return [row["scope_id"] for row in rows]

    def _execute(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> Any:
        return self.connection.execute(sql, parameters)

    def card_payload_hash(self, scope_id: str, card_id: str) -> str | None:
        row = self._execute(
            "SELECT payload_hash FROM ingested_cards WHERE scope_id=%s AND card_id=%s",
            (scope_id, card_id),
        ).fetchone()
        return row["payload_hash"] if row else None

    def mark_card(self, scope_id: str, card_id: str, payload_hash: str) -> None:
        self._execute(
            "INSERT INTO ingested_cards(scope_id,card_id,payload_hash) VALUES(%s,%s,%s)",
            (scope_id, card_id, payload_hash),
        )

    def has_record_observation(
        self, scope_id: str, record_id: str, observation_hash: str
    ) -> bool:
        row = self._execute(
            """
            SELECT 1 FROM record_observations
            WHERE scope_id=%s AND record_id=%s AND observation_hash=%s
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
        self._execute(
            """
            INSERT INTO record_observations(
              scope_id,record_id,observation_hash,container_id,observed_at
            ) VALUES(%s,%s,%s,%s,%s)
            """,
            (
                scope_id,
                record_id,
                observation_hash,
                container_id,
                as_utc(observed_at),
            ),
        )

    def record_observations(self, scope_id: str) -> list[RecordObservationRow]:
        rows = self._execute(
            """
            SELECT * FROM record_observations WHERE scope_id=%s
            ORDER BY record_id COLLATE "C",observation_hash COLLATE "C"
            """,
            (scope_id,),
        )
        return [
            RecordObservationRow(
                scope_id=row["scope_id"],
                record_id=row["record_id"],
                observation_hash=row["observation_hash"],
                container_id=row["container_id"],
                observed_at=instant_text(row["observed_at"]),
            )
            for row in rows
        ]

    def observe_source(
        self,
        scope_id: str,
        source_ref: SourceRef,
        *,
        revoked_at: datetime | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO evidence_sources(scope_id,source_id,uri,revoked_at)
            VALUES(%s,%s,%s,%s)
            ON CONFLICT(scope_id,source_id) DO UPDATE SET
              uri=COALESCE(evidence_sources.uri,excluded.uri),
              revoked_at=CASE
                WHEN evidence_sources.revoked_at IS NULL THEN excluded.revoked_at
                WHEN excluded.revoked_at IS NULL THEN evidence_sources.revoked_at
                ELSE LEAST(evidence_sources.revoked_at,excluded.revoked_at)
              END
            """,
            (
                scope_id,
                source_ref.source_id,
                source_ref.uri,
                as_utc(revoked_at) if revoked_at is not None else None,
            ),
        )

    def source_states(
        self, scope_id: str, source_refs: list[SourceRef]
    ) -> list[EvidenceRef]:
        if not source_refs:
            return []
        source_ids = list(dict.fromkeys(ref.source_id for ref in source_refs))
        rows = self._execute(
            """
            SELECT source_id,uri,revoked_at FROM evidence_sources
            WHERE scope_id=%s AND source_id=ANY(%s)
            """,
            (scope_id, source_ids),
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
                    revoked_at=(
                        as_utc(revoked_at) if revoked_at is not None else None
                    ),
                )
            )
        return result

    def source_metadata(self, scope_id: str) -> list[EvidenceRef]:
        rows = self._execute(
            """
            SELECT source_id,uri,revoked_at FROM evidence_sources
            WHERE scope_id=%s ORDER BY source_id COLLATE "C"
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
                revoked_at=(
                    as_utc(row["revoked_at"])
                    if row["revoked_at"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def put_source_state(self, scope_id: str, source: EvidenceRef) -> None:
        self._execute(
            """
            INSERT INTO evidence_sources(scope_id,source_id,uri,revoked_at)
            VALUES(%s,%s,%s,%s)
            ON CONFLICT(scope_id,source_id) DO UPDATE SET
              uri=excluded.uri,
              revoked_at=excluded.revoked_at
            """,
            (
                scope_id,
                source.source_id,
                source.uri,
                (
                    as_utc(source.revoked_at)
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
        self._execute(
            """
            INSERT INTO sync_positions(scope_id,container_id,watermark,cursor)
            VALUES(%s,%s,%s,%s)
            ON CONFLICT(scope_id,container_id) DO UPDATE SET
              watermark=GREATEST(sync_positions.watermark,excluded.watermark),
              cursor=COALESCE(excluded.cursor,sync_positions.cursor)
            """,
            (scope_id, container_id, as_utc(watermark), cursor),
        )

    def sync_positions(self, scope_id: str) -> list[SyncPosition]:
        rows = self._execute(
            """
            SELECT * FROM sync_positions WHERE scope_id=%s
            ORDER BY container_id COLLATE "C"
            """,
            (scope_id,),
        )
        return [
            SyncPosition(
                scope_id=row["scope_id"],
                container_id=row["container_id"],
                watermark=as_utc(row["watermark"]),
                cursor=row["cursor"],
                uid_watermark=row["uid_watermark"],
            )
            for row in rows
        ]

    def delete_sync_position(self, scope_id: str, container_id: str) -> bool:
        cursor = self._execute(
            """
            DELETE FROM sync_positions
            WHERE scope_id=%s AND container_id=%s
            """,
            (scope_id, container_id),
        )
        return cursor.rowcount > 0

    def update_mail_sync_position(
        self,
        scope_id: str,
        container_id: str,
        *,
        uid_watermark: int,
        uidvalidity: str,
        fallback_watermark: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO sync_positions(
              scope_id,container_id,watermark,cursor,uid_watermark
            )
            VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(scope_id,container_id) DO UPDATE SET
              cursor=excluded.cursor,
              uid_watermark=excluded.uid_watermark
            """,
            (
                scope_id,
                container_id,
                as_utc(fallback_watermark),
                uidvalidity,
                uid_watermark,
            ),
        )

    def subjects(self, scope_id: str) -> list[SubjectRecord]:
        rows = self._execute(
            """
            SELECT * FROM subjects
            WHERE scope_id=%s ORDER BY subject_key COLLATE "C"
            """,
            (scope_id,),
        )
        return [
            SubjectRecord(
                scope_id=row["scope_id"],
                subject_key=row["subject_key"],
                subject_type=row["subject_type"],
                title=row["title"],
                normalized_title=row["normalized_title"],
                source_ids=frozenset(row["source_ids_json"]),
                parent_subject_key=row["parent_subject_key"],
                thread_ids=frozenset(row["thread_ids_json"]),
            )
            for row in rows
        ]

    def upsert_subject(self, subject: SubjectRecord) -> None:
        self._execute(
            """
            INSERT INTO subjects(
              scope_id,subject_key,subject_type,parent_subject_key,title,
              normalized_title,source_ids_json,thread_ids_json
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
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
                self._json_param(sorted(subject.source_ids)),
                self._json_param(sorted(subject.thread_ids)),
            ),
        )

    def subject_merges(self, scope_id: str) -> list[SubjectMerge]:
        rows = self._execute(
            """
            SELECT * FROM subject_merges
            WHERE scope_id=%s
            ORDER BY source_subject_key COLLATE "C"
            """,
            (scope_id,),
        )
        return [
            SubjectMerge.model_validate(
                {
                    "scope_id": row["scope_id"],
                    "source_subject_key": row["source_subject_key"],
                    "target_subject_key": row["target_subject_key"],
                    "valid_from": as_utc(row["valid_from"]),
                    "source_refs": row["source_refs_json"],
                }
            )
            for row in rows
        ]

    def add_subject_merge(self, merge: SubjectMerge) -> None:
        self._execute(
            """
            INSERT INTO subject_merges(
              scope_id,source_subject_key,target_subject_key,valid_from,
              source_refs_json
            ) VALUES(%s,%s,%s,%s,%s)
            """,
            (
                merge.scope_id,
                merge.source_subject_key,
                merge.target_subject_key,
                as_utc(merge.valid_from),
                self._json_param(
                    [ref.model_dump(mode="json") for ref in merge.source_refs]
                ),
            ),
        )

    def remove_subject_merge(
        self, scope_id: str, source_subject_key: str
    ) -> bool:
        cursor = self._execute(
            """
            DELETE FROM subject_merges
            WHERE scope_id=%s AND source_subject_key=%s
            """,
            (scope_id, source_subject_key),
        )
        return cursor.rowcount == 1

    def add_assertion(self, assertion: Assertion) -> bool:
        existing = self._execute(
            "SELECT * FROM assertions WHERE assertion_id=%s",
            (assertion.assertion_id,),
        ).fetchone()
        if existing:
            prior = self._row_to_assertion(existing)
            prior_payload = self._immutable_assertion_payload(prior)
            incoming_payload = self._immutable_assertion_payload(assertion)
            if prior_payload != incoming_payload:
                raise ValueError("assertion_id collision with different immutable payload")
            return False
        self._execute(
            """
            INSERT INTO assertions(
              assertion_id,scope_id,subject_key,subject_type,predicate,operation,
              object_value_json,object_key,valid_from,recorded_at,source_refs_json,
              origin,observation_id
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            self._assertion_tuple(assertion),
        )
        return True

    def assertions(self, scope_id: str) -> list[Assertion]:
        rows = self._execute(
            """
            SELECT * FROM assertions WHERE scope_id=%s
            ORDER BY subject_key COLLATE "C",
                     predicate COLLATE "C",
                     valid_from,
                     assertion_id COLLATE "C"
            """,
            (scope_id,),
        )
        return [self._row_to_assertion(row) for row in rows]

    def intervals(self, scope_id: str) -> list[Interval]:
        rows = self._execute(
            """
            SELECT * FROM intervals WHERE scope_id=%s
            ORDER BY subject_key COLLATE "C",
                     predicate COLLATE "C",
                     valid_from,
                     object_key COLLATE "C",
                     interval_id COLLATE "C"
            """,
            (scope_id,),
        )
        return [self._row_to_interval(row) for row in rows]

    def memory_cards(self, scope_id: str) -> list[MemoryCard]:
        rows = self._execute(
            """
            SELECT * FROM memory_cards
            WHERE scope_id=%s ORDER BY subject_key COLLATE "C"
            """,
            (scope_id,),
        )
        return [
            MemoryCard.model_validate(
                {
                    "scope_id": row["scope_id"],
                    "subject_key": row["subject_key"],
                    "subject_type": row["subject_type"],
                    "title": row["title"],
                    "current": row["current_json"],
                    "updated_at": (
                        as_utc(row["updated_at"])
                        if row["updated_at"] is not None
                        else None
                    ),
                    "source_ids": row["source_ids_json"],
                }
            )
            for row in rows
        ]

    def projection_stats(self, scope_id: str) -> list[ProjectionStats]:
        rows = self._execute(
            """
            SELECT * FROM projection_stats
            WHERE scope_id=%s ORDER BY predicate COLLATE "C"
            """,
            (scope_id,),
        )
        return [ProjectionStats.model_validate(row) for row in rows]

    def enqueue_distill(
        self,
        card: EpisodeCard,
        *,
        subject_key: str,
        subject_type: str,
    ) -> bool:
        cursor = self._execute(
            """
            INSERT INTO distill_queue(
              scope_id,card_id,card_json,subject_key,subject_type
            ) VALUES(%s,%s,%s,%s,%s)
            ON CONFLICT(scope_id,card_id) DO NOTHING
            """,
            (
                card.scope_id,
                card.card_id,
                self._json_param(card.model_dump(mode="json")),
                subject_key,
                subject_type,
            ),
        )
        return cursor.rowcount == 1

    def distill_queue(
        self, scope_id: str, limit: int | None = None
    ) -> list[DistillQueueItem]:
        sql = (
            'SELECT * FROM distill_queue WHERE scope_id=%s '
            'ORDER BY card_id COLLATE "C"'
        )
        parameters: tuple[Any, ...] = (scope_id,)
        if limit is not None:
            sql += " LIMIT %s"
            parameters = (scope_id, limit)
        rows = self._execute(sql, parameters)
        return [
            DistillQueueItem(
                scope_id=row["scope_id"],
                card_id=row["card_id"],
                card=EpisodeCard.model_validate(row["card_json"]),
                subject_key=row["subject_key"],
                subject_type=row["subject_type"],
                attempt_count=row["attempt_count"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def remove_distill_item(self, scope_id: str, card_id: str) -> None:
        self._execute(
            "DELETE FROM distill_queue WHERE scope_id=%s AND card_id=%s",
            (scope_id, card_id),
        )

    def fail_distill_item(self, scope_id: str, card_id: str, error: str) -> None:
        self._execute(
            """
            UPDATE distill_queue SET attempt_count=attempt_count+1,last_error=%s
            WHERE scope_id=%s AND card_id=%s
            """,
            (error, scope_id, card_id),
        )

    def distill_queue_count(self, scope_id: str) -> int:
        return self._execute(
            "SELECT COUNT(*) AS n FROM distill_queue WHERE scope_id=%s",
            (scope_id,),
        ).fetchone()["n"]

    def record_gate_report(
        self,
        scope_id: str,
        *,
        accepted: int,
        rejections: dict[str, int],
    ) -> None:
        for counter, count in {"ACCEPTED": accepted, **rejections}.items():
            if count:
                self._execute(
                    """
                    INSERT INTO gate_stats(scope_id,counter,count) VALUES(%s,%s,%s)
                    ON CONFLICT(scope_id,counter) DO UPDATE
                    SET count=gate_stats.count+excluded.count
                    """,
                    (scope_id, counter, count),
                )

    def gate_statistics(self, scope_id: str) -> GateStatistics:
        rows = self._execute(
            """
            SELECT counter,count FROM gate_stats
            WHERE scope_id=%s ORDER BY counter COLLATE "C"
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
        payload: dict[str, Any],
        accepted: int,
        created_at: datetime,
        newest_message_at: datetime | None,
    ) -> bool:
        cursor = self._execute(
            """
            INSERT INTO tasks(
              task_id,scope_id,kind,payload_json,accepted,created_at,
              newest_message_at,status
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(task_id) DO NOTHING
            """,
            (
                task_id,
                scope_id,
                kind,
                self._json_param(payload),
                accepted,
                as_utc(created_at),
                (
                    as_utc(newest_message_at)
                    if newest_message_at is not None
                    else None
                ),
                TaskStatus.pending.value,
            ),
        )
        return cursor.rowcount == 1

    def task(self, task_id: str) -> TaskRow | None:
        row = self._execute(
            "SELECT * FROM tasks WHERE task_id=%s", (task_id,)
        ).fetchone()
        return self._task_row(row) if row is not None else None

    def tasks(
        self, scope_id: str, *, status: TaskStatus | None = None
    ) -> list[TaskRow]:
        sql = "SELECT * FROM tasks WHERE scope_id=%s"
        parameters: tuple[Any, ...] = (scope_id,)
        if status is not None:
            sql += " AND status=%s"
            parameters = (scope_id, status.value)
        sql += ' ORDER BY created_at,task_id COLLATE "C"'
        return [self._task_row(row) for row in self._execute(sql, parameters)]

    def flushable_tasks(
        self,
        scope_id: str,
        *,
        max_attempts: int = MAX_TASK_ATTEMPTS,
    ) -> list[TaskRow]:
        rows = self._execute(
            """
            SELECT * FROM tasks
            WHERE scope_id=%s AND (
              status=%s OR (status=%s AND attempts < %s)
            )
            ORDER BY created_at,task_id COLLATE "C"
            """,
            (
                scope_id,
                TaskStatus.pending.value,
                TaskStatus.failed.value,
                max_attempts,
            ),
        )
        return [self._task_row(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        cards_produced: int = 0,
        new_assertions: int = 0,
        gate_accepted: int = 0,
        gate_rejected: dict[str, int] | None = None,
        last_error: str | None = None,
    ) -> None:
        cursor = self._execute(
            """
            UPDATE tasks SET
              status=%s,cards_produced=%s,new_assertions=%s,
              gate_accepted=%s,gate_rejected_json=%s,
              attempts=attempts + CASE WHEN %s=%s THEN 1 ELSE 0 END,
              last_error=CASE
                WHEN %s=%s THEN %s
                WHEN %s=%s THEN NULL
                ELSE last_error
              END
            WHERE task_id=%s
            """,
            (
                status.value,
                cards_produced,
                new_assertions,
                gate_accepted,
                self._json_param(gate_rejected or {}),
                status.value,
                TaskStatus.failed.value,
                status.value,
                TaskStatus.failed.value,
                last_error,
                status.value,
                TaskStatus.completed.value,
                task_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown task_id: {task_id}")

    def quiet_scopes(
        self,
        cutoff: datetime,
        *,
        max_attempts: int = MAX_TASK_ATTEMPTS,
    ) -> list[str]:
        rows = self._execute(
            """
            SELECT scope_id FROM tasks
            WHERE (
              status=%s OR (status=%s AND attempts < %s)
            ) AND kind='messages' AND newest_message_at IS NOT NULL
            GROUP BY scope_id
            HAVING MAX(newest_message_at) <= %s
            ORDER BY scope_id COLLATE "C"
            """,
            (
                TaskStatus.pending.value,
                TaskStatus.failed.value,
                max_attempts,
                as_utc(cutoff),
            ),
        )
        return [row["scope_id"] for row in rows]

    def pending_scopes(
        self, *, max_attempts: int = MAX_TASK_ATTEMPTS
    ) -> list[str]:
        rows = self._execute(
            """
            SELECT DISTINCT scope_id FROM tasks
            WHERE status=%s OR (status=%s AND attempts < %s)
            ORDER BY scope_id COLLATE "C"
            """,
            (
                TaskStatus.pending.value,
                TaskStatus.failed.value,
                max_attempts,
            ),
        )
        return [row["scope_id"] for row in rows]

    def add_event(self, event: ChangeEvent) -> bool:
        if event.event_type not in {
            EventType.subject_merged,
            EventType.subject_unmerged,
        }:
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
                raise ValueError(
                    "event_id does not match the deterministic payload hash"
                )
        existing = self._execute(
            "SELECT * FROM events WHERE event_id=%s", (event.event_id,)
        ).fetchone()
        if existing is not None:
            prior = self._row_to_event(existing)
            if canonical_json(prior.model_dump(mode="json")) != canonical_json(
                event.model_dump(mode="json")
            ):
                raise ValueError("event_id collision with different event payload")
            return False
        self._execute(
            """
            INSERT INTO events(
              event_id,event_type,scope_id,subject_key,predicate,
              old_value_json,new_value_json,valid_from,recorded_at,origin,
              source_ids_json
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            self._event_tuple(event),
        )
        return True

    def events(
        self, scope_id: str, *, since: datetime | None = None
    ) -> list[ChangeEvent]:
        sql = "SELECT * FROM events WHERE scope_id=%s"
        parameters: tuple[Any, ...] = (scope_id,)
        if since is not None:
            sql += " AND recorded_at>=%s"
            parameters = (scope_id, as_utc(since))
        sql += ' ORDER BY recorded_at,event_id COLLATE "C"'
        return [self._row_to_event(row) for row in self._execute(sql, parameters)]

    def pending_webhook_events(
        self, webhook_url: str, *, limit: int = 100
    ) -> list[ChangeEvent]:
        rows = self._execute(
            """
            SELECT e.* FROM events e
            WHERE NOT EXISTS (
              SELECT 1 FROM webhook_deliveries d
              WHERE d.event_id=e.event_id AND d.webhook_url=%s
            )
            ORDER BY e.recorded_at,e.event_id COLLATE "C"
            LIMIT %s
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
        placeholders = ",".join("%s" for _ in event_ids)
        rows = self._execute(
            f"""
            SELECT event_id,scope_id FROM events
            WHERE event_id IN ({placeholders})
            """,
            tuple(event_ids),
        )
        scope_by_event = {row["event_id"]: row["scope_id"] for row in rows}
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO webhook_deliveries(
                  scope_id,event_id,webhook_url,delivered_at
                ) VALUES(%s,%s,%s,%s)
                ON CONFLICT(event_id,webhook_url) DO NOTHING
                """,
                [
                    (
                        scope_by_event[event_id],
                        event_id,
                        webhook_url,
                        as_utc(delivered_at),
                    )
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
            rows = self._execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=%s AND i.subject_key=%s AND i.predicate=%s
                ORDER BY i.valid_from DESC,
                         i.assertion_id COLLATE "C" DESC
                LIMIT 1
                """,
                (scope_id, subject_key, predicate),
            )
        else:
            rows = self._execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=%s AND i.subject_key=%s AND i.predicate=%s
                  AND i.valid_to IS NULL
                ORDER BY i.object_key COLLATE "C"
                """,
                (scope_id, subject_key, predicate),
            )
        return [self._query_value_row(row) for row in rows]

    def query_timeline_values(
        self, scope_id: str, subject_key: str, predicate: str
    ) -> list[QueryValueRow]:
        rows = self._execute(
            """
            SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
            JOIN assertions a ON a.assertion_id=i.assertion_id
            WHERE i.scope_id=%s AND i.subject_key=%s AND i.predicate=%s
            ORDER BY i.valid_from,
                     i.object_key COLLATE "C",
                     i.assertion_id COLLATE "C"
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
        moment = as_utc(instant)
        if append:
            rows = self._execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=%s AND i.subject_key=%s AND i.predicate=%s
                  AND i.valid_from<=%s
                ORDER BY i.valid_from DESC,
                         i.assertion_id COLLATE "C" DESC
                LIMIT 1
                """,
                (scope_id, subject_key, predicate, moment),
            )
        else:
            rows = self._execute(
                """
                SELECT i.*,a.recorded_at AS recorded_at FROM intervals i
                JOIN assertions a ON a.assertion_id=i.assertion_id
                WHERE i.scope_id=%s AND i.subject_key=%s AND i.predicate=%s
                  AND i.valid_from<=%s
                  AND (i.valid_to IS NULL OR i.valid_to>%s)
                ORDER BY i.object_key COLLATE "C"
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
        placeholders = ",".join("%s" for _ in predicates)
        rows = self._execute(
            f"""
            SELECT m.* FROM memory_cards m
            WHERE m.scope_id=%s
              AND EXISTS (
                SELECT 1 FROM intervals i
                WHERE i.scope_id=m.scope_id
                  AND i.subject_key=m.subject_key
                  AND i.predicate IN ({placeholders})
                  AND i.object_key=%s
                  AND i.valid_to IS NULL
              )
            ORDER BY m.subject_key COLLATE "C"
            """,
            (scope_id, *predicates, object_key),
        )
        return [self._query_subject_row(row) for row in rows]

    def query_subjects_by_type(
        self, scope_id: str, subject_type: str
    ) -> list[QuerySubjectRow]:
        rows = self._execute(
            """
            SELECT * FROM memory_cards
            WHERE scope_id=%s AND subject_type=%s
            ORDER BY subject_key COLLATE "C"
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
        total = self._execute(
            "SELECT COUNT(*) AS n FROM memory_cards WHERE scope_id=%s",
            (scope_id,),
        ).fetchone()["n"]
        if predicate is None or not completed_object_keys or total == 0:
            return 0, total
        placeholders = ",".join("%s" for _ in completed_object_keys)
        completed = self._execute(
            f"""
            SELECT COUNT(DISTINCT subject_key) AS n FROM intervals
            WHERE scope_id=%s AND predicate=%s AND valid_to IS NULL
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
        self._execute("DELETE FROM intervals WHERE scope_id=%s", (scope_id,))
        self._execute("DELETE FROM memory_cards WHERE scope_id=%s", (scope_id,))
        self._execute("DELETE FROM projection_stats WHERE scope_id=%s", (scope_id,))
        if intervals:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO intervals(
                      interval_id,scope_id,subject_key,subject_type,predicate,
                      object_value_json,object_key,valid_from,valid_to,assertion_id,
                      supporting_assertion_ids_json,source_refs_json,origin
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [self._interval_tuple(item) for item in intervals],
                )
        if cards:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO memory_cards(
                      scope_id,subject_key,subject_type,title,current_json,updated_at,
                      source_ids_json
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s)
                    """,
                    [
                        (
                            item.scope_id,
                            item.subject_key,
                            item.subject_type,
                            item.title,
                            self._json_param(item.current),
                            as_utc(item.updated_at) if item.updated_at else None,
                            self._json_param(item.source_ids),
                        )
                        for item in cards
                    ],
                )
        if stats:
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO projection_stats(
                      scope_id,predicate,conflicts_resolved
                    ) VALUES(%s,%s,%s)
                    """,
                    [
                        (item.scope_id, item.predicate, item.conflicts_resolved)
                        for item in stats
                    ],
                )

    @staticmethod
    def _assertion_tuple(assertion: Assertion) -> tuple[Any, ...]:
        return (
            assertion.assertion_id,
            assertion.scope_id,
            assertion.subject_key,
            assertion.subject_type,
            assertion.predicate,
            assertion.operation.value,
            PostgresStore._json_param(assertion.object_value),
            assertion.object_key,
            as_utc(assertion.valid_from),
            as_utc(assertion.recorded_at),
            PostgresStore._json_param(
                [ref.model_dump(mode="json") for ref in assertion.source_refs]
            ),
            assertion.origin.value,
            assertion.observation_id,
        )

    @staticmethod
    def _interval_tuple(interval: Interval) -> tuple[Any, ...]:
        return (
            interval.interval_id,
            interval.scope_id,
            interval.subject_key,
            interval.subject_type,
            interval.predicate,
            PostgresStore._json_param(interval.object_value),
            interval.object_key,
            as_utc(interval.valid_from),
            as_utc(interval.valid_to) if interval.valid_to else None,
            interval.assertion_id,
            PostgresStore._json_param(interval.supporting_assertion_ids),
            PostgresStore._json_param(
                [ref.model_dump(mode="json") for ref in interval.source_refs]
            ),
            interval.origin.value,
        )

    @staticmethod
    def _event_tuple(event: ChangeEvent) -> tuple[Any, ...]:
        return (
            event.event_id,
            event.event_type.value,
            event.scope_id,
            event.subject_key,
            event.predicate,
            PostgresStore._json_param(event.old_value),
            PostgresStore._json_param(event.new_value),
            as_utc(event.valid_from),
            as_utc(event.recorded_at),
            event.origin.value,
            PostgresStore._json_param(event.source_ids),
        )

    @staticmethod
    def _row_to_assertion(row: dict[str, Any]) -> Assertion:
        return Assertion.model_validate(
            {
                "assertion_id": row["assertion_id"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "subject_type": row["subject_type"],
                "predicate": row["predicate"],
                "operation": row["operation"],
                "object_value": row["object_value_json"],
                "object_key": row["object_key"],
                "valid_from": as_utc(row["valid_from"]),
                "recorded_at": as_utc(row["recorded_at"]),
                "source_refs": row["source_refs_json"],
                "origin": row["origin"],
                "observation_id": row["observation_id"],
            }
        )

    @staticmethod
    def _row_to_interval(row: dict[str, Any]) -> Interval:
        return Interval.model_validate(
            {
                "interval_id": row["interval_id"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "subject_type": row["subject_type"],
                "predicate": row["predicate"],
                "object_value": row["object_value_json"],
                "object_key": row["object_key"],
                "valid_from": as_utc(row["valid_from"]),
                "valid_to": (
                    as_utc(row["valid_to"])
                    if row["valid_to"] is not None
                    else None
                ),
                "assertion_id": row["assertion_id"],
                "supporting_assertion_ids": row["supporting_assertion_ids_json"],
                "source_refs": row["source_refs_json"],
                "origin": row["origin"],
            }
        )

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> ChangeEvent:
        return ChangeEvent.model_validate(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "predicate": row["predicate"],
                "old_value": row["old_value_json"],
                "new_value": row["new_value_json"],
                "valid_from": as_utc(row["valid_from"]),
                "recorded_at": as_utc(row["recorded_at"]),
                "origin": row["origin"],
                "source_ids": row["source_ids_json"],
            }
        )

    @staticmethod
    def _query_value_row(row: dict[str, Any]) -> QueryValueRow:
        source_refs = row["source_refs_json"]
        return QueryValueRow(
            subject_key=row["subject_key"],
            predicate=row["predicate"],
            value=row["object_value_json"],
            valid_from=instant_text(row["valid_from"]),
            valid_to=instant_text(row["valid_to"]) if row["valid_to"] else None,
            recorded_at=instant_text(row["recorded_at"]),
            assertion_id=row["assertion_id"],
            supporting_assertion_ids=row["supporting_assertion_ids_json"],
            source_refs=[SourceRef.model_validate(item) for item in source_refs],
            origin=row["origin"],
        )

    @staticmethod
    def _query_subject_row(row: dict[str, Any]) -> QuerySubjectRow:
        return QuerySubjectRow(
            subject_key=row["subject_key"],
            subject_type=row["subject_type"],
            title=row["title"],
            current=row["current_json"],
        )

    @staticmethod
    def _task_row(row: dict[str, Any]) -> TaskRow:
        status = TaskStatus(row["status"])
        return TaskRow(
            task_id=row["task_id"],
            scope_id=row["scope_id"],
            kind=row["kind"],
            payload=row["payload_json"],
            accepted=row["accepted"],
            created_at=instant_text(row["created_at"]),
            newest_message_at=(
                instant_text(row["newest_message_at"])
                if row["newest_message_at"] is not None
                else None
            ),
            result=TaskResult(
                task_id=row["task_id"],
                status=status,
                cards_produced=row["cards_produced"],
                new_assertions=row["new_assertions"],
                attempts=row["attempts"],
                last_error=row["last_error"],
                gate=TaskGate(
                    accepted=row["gate_accepted"],
                    rejected=row["gate_rejected_json"],
                ),
            ),
        )

    @staticmethod
    def _json_param(value: Any) -> Jsonb:
        return Jsonb(json_value(value))

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
