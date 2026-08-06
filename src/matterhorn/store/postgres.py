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
    HandleOrigin,
    Interval,
    MemoryCard,
    ProjectionStats,
    Record,
    ReviewItem,
    Signal,
    SourceRef,
    SubjectHandle,
    SubjectMerge,
    SubjectRecord,
    SyncPosition,
    TaskGate,
    TaskResult,
    TaskStatus,
)
from matterhorn.store.base import (
    MAX_TASK_ATTEMPTS,
    ROUTE_COUNTER_NAMES,
    ConversationHotnessRow,
    DistillQueueItem,
    QuerySubjectRow,
    QueryValueRow,
    RecordObservationRow,
    StagedRecordRow,
    TaskRow,
    ThemeScheduleState,
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
CREATE TABLE IF NOT EXISTS staged_records (
    scope_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    thread_id TEXT,
    sent_at TIMESTAMPTZ NOT NULL,
    author_json JSONB NOT NULL,
    content TEXT,
    kind TEXT NOT NULL,
    revoked_at TIMESTAMPTZ,
    record_json JSONB NOT NULL,
    staged_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_staged_records_conversation
    ON staged_records(
        scope_id COLLATE "C",
        container_id COLLATE "C",
        sent_at,
        record_id COLLATE "C"
    );
CREATE TABLE IF NOT EXISTS signals (
    scope_id TEXT COLLATE "C" NOT NULL,
    record_id TEXT COLLATE "C" NOT NULL,
    kind TEXT COLLATE "C" NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL,
    matched_text TEXT NOT NULL,
    subject_key TEXT COLLATE "C",
    status TEXT NOT NULL CHECK(status IN ('open', 'acked')),
    acked_at TIMESTAMPTZ,
    PRIMARY KEY (scope_id, record_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_signals_status
    ON signals(scope_id, status, detected_at, record_id, kind);
CREATE TABLE IF NOT EXISTS read_watermarks (
    scope_id TEXT COLLATE "C" NOT NULL,
    subject_key TEXT COLLATE "C" NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope_id, subject_key)
);
CREATE TABLE IF NOT EXISTS evidence_sources (
    scope_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    uri TEXT,
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (scope_id, source_id)
);
CREATE TABLE IF NOT EXISTS person_names (
    scope_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope_id, person_id)
);
CREATE TABLE IF NOT EXISTS conversation_names (
    scope_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope_id, conversation_key)
);
CREATE TABLE IF NOT EXISTS sync_positions (
    scope_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    watermark TIMESTAMPTZ NOT NULL,
    cursor TEXT,
    uid_watermark BIGINT,
    PRIMARY KEY (scope_id, container_id)
);
CREATE TABLE IF NOT EXISTS mail_runtime_reports (
    account_id TEXT COLLATE "C" PRIMARY KEY,
    scope_id TEXT COLLATE "C" NOT NULL,
    report_json JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
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
CREATE TABLE IF NOT EXISTS subject_handles (
    binding_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    handle_type TEXT NOT NULL,
    handle_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    origin TEXT NOT NULL,
    source_refs_json JSONB NOT NULL,
    bound_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revocation_origin TEXT,
    revocation_source_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_subject_handles_lookup
    ON subject_handles(scope_id, handle_type, normalized_value);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subject_handles_active_unique
    ON subject_handles(scope_id, handle_type, normalized_value)
    WHERE revoked_at IS NULL;
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
CREATE TABLE IF NOT EXISTS review_queue (
    scope_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    card_json JSONB NOT NULL,
    reasons JSONB NOT NULL,
    candidates_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    resolution_json JSONB,
    PRIMARY KEY (scope_id, review_id)
);
CREATE INDEX IF NOT EXISTS idx_review_queue_pending
    ON review_queue(
        scope_id COLLATE "C", resolved_at, created_at, review_id COLLATE "C"
    );
CREATE TABLE IF NOT EXISTS gate_stats (
    scope_id TEXT NOT NULL,
    counter TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (scope_id, counter)
);
CREATE TABLE IF NOT EXISTS theme_schedule_state (
    scope_id TEXT PRIMARY KEY,
    last_enqueued_at TIMESTAMPTZ,
    last_run_at TIMESTAMPTZ
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
    unchanged_dropped INTEGER NOT NULL DEFAULT 0,
    gate_accepted INTEGER NOT NULL DEFAULT 0,
    gate_rejected_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    handle_conflicts INTEGER NOT NULL DEFAULT 0,
    route_handle INTEGER NOT NULL DEFAULT 0,
    route_thread INTEGER NOT NULL DEFAULT 0,
    route_evidence INTEGER NOT NULL DEFAULT 0,
    route_model INTEGER NOT NULL DEFAULT 0,
    route_new INTEGER NOT NULL DEFAULT 0,
    route_review INTEGER NOT NULL DEFAULT 0,
    route_disagreements INTEGER NOT NULL DEFAULT 0,
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
            cursor.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN IF NOT EXISTS handle_conflicts
                INTEGER NOT NULL DEFAULT 0
                """
            )
            cursor.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN IF NOT EXISTS unchanged_dropped
                INTEGER NOT NULL DEFAULT 0
                """
            )
            for counter in ROUTE_COUNTER_NAMES:
                cursor.execute(
                    f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {counter} "
                    "INTEGER NOT NULL DEFAULT 0"
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
                "review_queue",
                "distill_queue",
                "gate_stats",
                "theme_schedule_state",
                "projection_stats",
                "memory_cards",
                "intervals",
                "assertions",
                "subject_merges",
                "subject_handles",
                "subjects",
                "person_names",
                "conversation_names",
                "sync_positions",
                "mail_runtime_reports",
                "evidence_sources",
                "read_watermarks",
                "signals",
                "staged_records",
                "record_observations",
                "ingested_cards",
            ):
                self._execute(f"DELETE FROM {table} WHERE scope_id=%s", (scope_id,))

    def scope_exists(self, scope_id: str) -> bool:
        tables = (
            "subjects",
            "assertions",
            "subject_merges",
            "subject_handles",
            "tasks",
            "review_queue",
            "theme_schedule_state",
            "events",
            "ingested_cards",
            "record_observations",
            "staged_records",
            "signals",
            "read_watermarks",
            "mail_runtime_reports",
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
                UNION ALL SELECT scope_id FROM staged_records
                UNION ALL SELECT scope_id FROM signals
                UNION ALL SELECT scope_id FROM read_watermarks
                UNION ALL SELECT scope_id FROM mail_runtime_reports
                UNION ALL SELECT scope_id FROM evidence_sources
                UNION ALL SELECT scope_id FROM sync_positions
                UNION ALL SELECT scope_id FROM subjects
                UNION ALL SELECT scope_id FROM subject_handles
                UNION ALL SELECT scope_id FROM subject_merges
                UNION ALL SELECT scope_id FROM assertions
                UNION ALL SELECT scope_id FROM intervals
                UNION ALL SELECT scope_id FROM memory_cards
                UNION ALL SELECT scope_id FROM projection_stats
                UNION ALL SELECT scope_id FROM distill_queue
                UNION ALL SELECT scope_id FROM review_queue
                UNION ALL SELECT scope_id FROM gate_stats
                UNION ALL SELECT scope_id FROM theme_schedule_state
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

    def stage_records(
        self,
        scope_id: str,
        records: list[Record],
        *,
        staged_at: datetime,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO staged_records(
              scope_id,record_id,container_id,thread_id,sent_at,author_json,
              content,kind,revoked_at,record_json,staged_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(scope_id,record_id) DO UPDATE SET
              container_id=excluded.container_id,
              thread_id=excluded.thread_id,
              sent_at=excluded.sent_at,
              author_json=excluded.author_json,
              content=excluded.content,
              kind=excluded.kind,
              revoked_at=excluded.revoked_at,
              record_json=excluded.record_json,
              staged_at=excluded.staged_at
            """,
            [
                (
                    scope_id,
                    record.record_id,
                    record.container_id,
                    record.thread_id,
                    as_utc(record.sent_at),
                    Jsonb(json_value(record.author)),
                    record.content,
                    record.kind,
                    (
                        as_utc(record.revoked_at)
                        if record.revoked_at is not None
                        else None
                    ),
                    Jsonb(json_value(record)),
                    as_utc(staged_at),
                )
                for record in records
            ],
        )

    def staged_records(
        self,
        scope_id: str,
        container_id: str,
        *,
        sent_at_from: datetime,
        sent_at_before: datetime,
        thread_id: str | None,
        exclude_record_ids: list[str],
    ) -> list[Record]:
        clauses = [
            "scope_id=%s",
            "container_id=%s",
            "sent_at>=%s",
            "sent_at<%s",
            "revoked_at IS NULL",
        ]
        parameters: list[Any] = [
            scope_id,
            container_id,
            as_utc(sent_at_from),
            as_utc(sent_at_before),
        ]
        if thread_id is not None:
            clauses.append("thread_id=%s")
            parameters.append(thread_id)
        if exclude_record_ids:
            clauses.append("NOT (record_id = ANY(%s))")
            parameters.append(exclude_record_ids)
        rows = self._execute(
            f"""
            SELECT record_json FROM staged_records
            WHERE {' AND '.join(clauses)}
            ORDER BY sent_at,record_id COLLATE "C"
            """,
            tuple(parameters),
        )
        return [Record.model_validate(row["record_json"]) for row in rows]

    def recent_staged(
        self, scope_id: str | None, *, limit: int
    ) -> list[StagedRecordRow]:
        if limit < 1:
            raise ValueError("recent staged limit MUST be positive")
        where = "WHERE revoked_at IS NULL"
        parameters: list[Any] = []
        if scope_id is not None:
            where += " AND scope_id=%s"
            parameters.append(scope_id)
        parameters.append(limit)
        rows = self._execute(
            f"""
            SELECT scope_id,record_json,staged_at FROM staged_records
            {where}
            ORDER BY staged_at DESC,sent_at DESC,record_id COLLATE "C" DESC
            LIMIT %s
            """,
            tuple(parameters),
        )
        return [
            StagedRecordRow(
                scope_id=row["scope_id"],
                record=Record.model_validate(row["record_json"]),
                staged_at=instant_text(row["staged_at"]),
            )
            for row in rows
        ]

    def purge_staged_records(self, scope_id: str, *, before: datetime) -> int:
        cursor = self._execute(
            "DELETE FROM staged_records WHERE scope_id=%s AND sent_at<%s",
            (scope_id, as_utc(before)),
        )
        return cursor.rowcount

    def add_signal(self, signal: Signal) -> bool:
        cursor = self._execute(
            """
            INSERT INTO signals(
              scope_id,record_id,kind,detected_at,matched_text,subject_key,
              status,acked_at
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(scope_id,record_id,kind) DO NOTHING
            """,
            (
                signal.scope_id,
                signal.record_id,
                signal.kind,
                as_utc(signal.detected_at),
                signal.matched_text,
                signal.subject_key,
                signal.status.value,
                as_utc(signal.acked_at) if signal.acked_at else None,
            ),
        )
        return cursor.rowcount == 1

    def signals(
        self,
        scope_id: str | None = None,
        *,
        status: str | None = None,
    ) -> list[Signal]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if scope_id is not None:
            clauses.append("scope_id=%s")
            parameters.append(scope_id)
        if status is not None:
            clauses.append("status=%s")
            parameters.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._execute(
            f"""
            SELECT * FROM signals {where}
            ORDER BY (subject_key IS NULL), detected_at DESC,
                     scope_id COLLATE "C", record_id COLLATE "C",
                     kind COLLATE "C"
            """,
            tuple(parameters),
        )
        return [self._row_to_signal(row) for row in rows]

    def acknowledge_signal(
        self,
        scope_id: str,
        record_id: str,
        kind: str,
        *,
        acked_at: datetime,
    ) -> Signal | None:
        self._execute(
            """
            UPDATE signals SET status='acked',acked_at=%s
            WHERE scope_id=%s AND record_id=%s AND kind=%s AND status='open'
            """,
            (as_utc(acked_at), scope_id, record_id, kind),
        )
        row = self._execute(
            """
            SELECT * FROM signals
            WHERE scope_id=%s AND record_id=%s AND kind=%s
            """,
            (scope_id, record_id, kind),
        ).fetchone()
        return self._row_to_signal(row) if row is not None else None

    def set_read_watermark(
        self,
        scope_id: str,
        subject_key: str,
        *,
        last_seen_at: datetime,
    ) -> datetime:
        row = self._execute(
            """
            INSERT INTO read_watermarks(scope_id,subject_key,last_seen_at)
            VALUES(%s,%s,%s)
            ON CONFLICT(scope_id,subject_key) DO UPDATE SET
              last_seen_at=GREATEST(
                read_watermarks.last_seen_at,excluded.last_seen_at
              )
            RETURNING last_seen_at
            """,
            (scope_id, subject_key, as_utc(last_seen_at)),
        ).fetchone()
        return as_utc(row["last_seen_at"])

    def read_watermark(
        self, scope_id: str, subject_key: str
    ) -> datetime | None:
        row = self._execute(
            """
            SELECT last_seen_at FROM read_watermarks
            WHERE scope_id=%s AND subject_key=%s
            """,
            (scope_id, subject_key),
        ).fetchone()
        return as_utc(row["last_seen_at"]) if row is not None else None

    def read_watermarks(self, scope_id: str) -> dict[str, datetime]:
        rows = self._execute(
            """
            SELECT subject_key,last_seen_at FROM read_watermarks
            WHERE scope_id=%s ORDER BY subject_key COLLATE "C"
            """,
            (scope_id,),
        )
        return {
            row["subject_key"]: as_utc(row["last_seen_at"])
            for row in rows
        }

    def conversation_hotness(
        self,
        scope_ids: list[str],
        *,
        window_start: datetime,
        window_end: datetime,
        min_authors: int,
        min_messages: int,
    ) -> list[ConversationHotnessRow]:
        if not scope_ids:
            return []
        rows = self._execute(
            """
            WITH filtered AS (
              SELECT scope_id,container_id,author_json->>'id' AS author_id,
                     FLOOR(EXTRACT(EPOCH FROM sent_at) / 1800)::bigint AS bucket,
                     COALESCE((
                       SELECT SUM((reaction->>'count')::integer)
                       FROM jsonb_array_elements(record_json->'reactions') reaction
                     ),0) AS reactions
              FROM staged_records
              WHERE scope_id=ANY(%s)
                AND sent_at>=%s AND sent_at<%s AND revoked_at IS NULL
            ), buckets AS (
              SELECT scope_id,container_id,bucket,
                     COUNT(*) AS message_count,
                     COUNT(DISTINCT author_id) AS distinct_authors
              FROM filtered
              GROUP BY scope_id,container_id,bucket
            )
            SELECT f.scope_id,f.container_id,COUNT(*) AS message_count,
                   COUNT(DISTINCT f.author_id) AS distinct_authors,
                   SUM(f.reactions) AS reaction_total,
                   EXISTS(
                     SELECT 1 FROM buckets b
                     WHERE b.scope_id=f.scope_id
                       AND b.container_id=f.container_id
                       AND b.distinct_authors>=%s AND b.message_count>=%s
                   ) AS hot
            FROM filtered f
            GROUP BY f.scope_id,f.container_id
            ORDER BY f.scope_id COLLATE "C",f.container_id COLLATE "C"
            """,
            (
                scope_ids,
                as_utc(window_start),
                as_utc(window_end),
                min_authors,
                min_messages,
            ),
        )
        return [
            ConversationHotnessRow(
                scope_id=row["scope_id"],
                container_id=row["container_id"],
                message_count=row["message_count"],
                distinct_authors=row["distinct_authors"],
                reaction_total=row["reaction_total"],
                hot=bool(row["hot"]),
            )
            for row in rows
        ]

    def brief_assertions(
        self,
        scope_ids: list[str],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Assertion]:
        if not scope_ids:
            return []
        rows = self._execute(
            """
            SELECT * FROM assertions
            WHERE scope_id=ANY(%s) AND recorded_at>=%s AND recorded_at<%s
            ORDER BY scope_id COLLATE "C",recorded_at,
                     subject_key COLLATE "C",assertion_id COLLATE "C"
            """,
            (scope_ids, as_utc(window_start), as_utc(window_end)),
        )
        return [self._row_to_assertion(row) for row in rows]

    def brief_events(
        self,
        scope_ids: list[str],
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> list[ChangeEvent]:
        if not scope_ids:
            return []
        rows = self._execute(
            """
            SELECT * FROM events
            WHERE scope_id=ANY(%s) AND recorded_at>=%s AND recorded_at<%s
            ORDER BY scope_id COLLATE "C",recorded_at,event_id COLLATE "C"
            """,
            (scope_ids, as_utc(window_start), as_utc(window_end)),
        )
        return [self._row_to_event(row) for row in rows]

    def save_mail_runtime_report(
        self,
        account_id: str,
        scope_id: str,
        report: dict[str, Any],
        *,
        updated_at: datetime,
    ) -> None:
        self._execute(
            """
            INSERT INTO mail_runtime_reports(
              account_id,scope_id,report_json,updated_at
            ) VALUES(%s,%s,%s,%s)
            ON CONFLICT(account_id) DO UPDATE SET
              scope_id=excluded.scope_id,
              report_json=excluded.report_json,
              updated_at=excluded.updated_at
            """,
            (account_id, scope_id, Jsonb(report), as_utc(updated_at)),
        )

    def mail_runtime_report(self, account_id: str) -> dict[str, Any] | None:
        row = self._execute(
            """
            SELECT report_json FROM mail_runtime_reports WHERE account_id=%s
            """,
            (account_id,),
        ).fetchone()
        return row["report_json"] if row is not None else None

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

    def upsert_person_names(
        self,
        scope_id: str,
        names: dict[str, str],
        *,
        seen_at: datetime,
    ) -> None:
        if not names:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO person_names(
                  scope_id,person_id,display_name,last_seen_at
                ) VALUES(%s,%s,%s,%s)
                ON CONFLICT(scope_id,person_id) DO UPDATE SET
                  display_name=CASE
                    WHEN excluded.last_seen_at >= person_names.last_seen_at
                      THEN excluded.display_name
                    ELSE person_names.display_name
                  END,
                  last_seen_at=CASE
                    WHEN excluded.last_seen_at >= person_names.last_seen_at
                      THEN excluded.last_seen_at
                    ELSE person_names.last_seen_at
                  END
                """,
                [
                    (scope_id, person_id, display_name, as_utc(seen_at))
                    for person_id, display_name in sorted(names.items())
                ],
            )

    def person_names(self, scope_id: str) -> dict[str, str]:
        rows = self._execute(
            """
            SELECT person_id, display_name FROM person_names
            WHERE scope_id=%s ORDER BY person_id COLLATE "C"
            """,
            (scope_id,),
        )
        return {row["person_id"]: row["display_name"] for row in rows}

    def upsert_conversation_names(
        self,
        scope_id: str,
        names: dict[str, str],
        *,
        seen_at: datetime,
    ) -> None:
        if not names:
            return
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO conversation_names(
                  scope_id,conversation_key,display_name,last_seen_at
                ) VALUES(%s,%s,%s,%s)
                ON CONFLICT(scope_id,conversation_key) DO UPDATE SET
                  display_name=CASE
                    WHEN excluded.last_seen_at >= conversation_names.last_seen_at
                      THEN excluded.display_name
                    ELSE conversation_names.display_name
                  END,
                  last_seen_at=CASE
                    WHEN excluded.last_seen_at >= conversation_names.last_seen_at
                      THEN excluded.last_seen_at
                    ELSE conversation_names.last_seen_at
                  END
                """,
                [
                    (scope_id, key, display_name, as_utc(seen_at))
                    for key, display_name in sorted(names.items())
                ],
            )

    def conversation_names(self, scope_id: str) -> dict[str, str]:
        rows = self._execute(
            """
            SELECT conversation_key, display_name FROM conversation_names
            WHERE scope_id=%s ORDER BY conversation_key COLLATE "C"
            """,
            (scope_id,),
        )
        return {row["conversation_key"]: row["display_name"] for row in rows}

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

    def subject_handle_bindings(self, scope_id: str) -> list[SubjectHandle]:
        rows = self._execute(
            """
            SELECT * FROM subject_handles
            WHERE scope_id=%s
            ORDER BY handle_type COLLATE "C",
                     normalized_value COLLATE "C",
                     bound_at,
                     binding_id COLLATE "C"
            """,
            (scope_id,),
        )
        return [self._row_to_subject_handle(row) for row in rows]

    def active_subject_handles(
        self,
        scope_id: str,
        *,
        handle_type: str | None = None,
        normalized_value: str | None = None,
    ) -> list[SubjectHandle]:
        sql = (
            "SELECT * FROM subject_handles "
            "WHERE scope_id=%s AND revoked_at IS NULL"
        )
        parameters: list[str] = [scope_id]
        if handle_type is not None:
            sql += " AND handle_type=%s"
            parameters.append(handle_type)
        if normalized_value is not None:
            sql += " AND normalized_value=%s"
            parameters.append(normalized_value)
        sql += (
            ' ORDER BY handle_type COLLATE "C",'
            ' normalized_value COLLATE "C",binding_id COLLATE "C"'
        )
        rows = self._execute(sql, tuple(parameters))
        return [self._row_to_subject_handle(row) for row in rows]

    def active_subject_handles_across_scopes(
        self, scope_ids: list[str]
    ) -> list[SubjectHandle]:
        if not scope_ids:
            return []
        placeholders = ",".join("%s" for _ in scope_ids)
        rows = self._execute(
            f"""
            SELECT * FROM subject_handles
            WHERE revoked_at IS NULL AND scope_id IN ({placeholders})
            ORDER BY scope_id COLLATE "C",
                     handle_type COLLATE "C",
                     normalized_value COLLATE "C",
                     subject_key COLLATE "C",
                     binding_id COLLATE "C"
            """,
            tuple(scope_ids),
        )
        return [self._row_to_subject_handle(row) for row in rows]

    def add_subject_handle(self, handle: SubjectHandle) -> str:
        cursor = self._execute(
            """
            INSERT INTO subject_handles(
              binding_id,scope_id,subject_key,handle_type,handle_value,
              normalized_value,origin,source_refs_json,bound_at,revoked_at,
              revocation_origin,revocation_source_refs_json
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            (
                handle.binding_id,
                handle.scope_id,
                handle.subject_key,
                handle.handle_type,
                handle.handle_value,
                handle.normalized_value,
                handle.origin.value,
                self._json_param(
                    [ref.model_dump(mode="json") for ref in handle.source_refs]
                ),
                as_utc(handle.bound_at),
                as_utc(handle.revoked_at) if handle.revoked_at else None,
                (
                    handle.revocation_origin.value
                    if handle.revocation_origin is not None
                    else None
                ),
                self._json_param(
                    [
                        ref.model_dump(mode="json")
                        for ref in handle.revocation_source_refs
                    ]
                ),
            ),
        )
        if cursor.rowcount:
            return "bound"
        existing = self._execute(
            """
            SELECT * FROM subject_handles
            WHERE scope_id=%s AND handle_type=%s AND normalized_value=%s
              AND revoked_at IS NULL
            """,
            (handle.scope_id, handle.handle_type, handle.normalized_value),
        ).fetchone()
        if existing is not None:
            return (
                "already_bound"
                if existing["subject_key"] == handle.subject_key
                else "conflict"
            )
        prior_id = self._execute(
            "SELECT * FROM subject_handles WHERE binding_id=%s",
            (handle.binding_id,),
        ).fetchone()
        if prior_id is None or self._row_to_subject_handle(prior_id) != handle:
            raise ValueError("subject handle binding_id collision with different payload")
        return "already_bound"

    def revoke_subject_handle(
        self,
        scope_id: str,
        handle_type: str,
        normalized_value: str,
        *,
        revoked_at: datetime,
        revocation_origin: str,
        source_refs: list[SourceRef],
    ) -> SubjectHandle | None:
        row = self._execute(
            """
            SELECT * FROM subject_handles
            WHERE scope_id=%s AND handle_type=%s AND normalized_value=%s
              AND revoked_at IS NULL
            """,
            (scope_id, handle_type, normalized_value),
        ).fetchone()
        if row is None:
            return None
        self._execute(
            """
            UPDATE subject_handles
            SET revoked_at=%s,revocation_origin=%s,
                revocation_source_refs_json=%s
            WHERE binding_id=%s AND revoked_at IS NULL
            """,
            (
                as_utc(revoked_at),
                revocation_origin,
                self._json_param(
                    [ref.model_dump(mode="json") for ref in source_refs]
                ),
                row["binding_id"],
            ),
        )
        updated = self._execute(
            "SELECT * FROM subject_handles WHERE binding_id=%s",
            (row["binding_id"],),
        ).fetchone()
        assert updated is not None
        return self._row_to_subject_handle(updated)

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

    def add_review_item(self, item: ReviewItem) -> bool:
        existing = self.review_item(item.scope_id, item.review_id)
        if existing is not None:
            comparable = ("card_json", "reasons", "candidates_json")
            if any(getattr(existing, field) != getattr(item, field) for field in comparable):
                raise ValueError(
                    f"review_id {item.review_id!r} was reused with another payload"
                )
            return False
        self._execute(
            """
            INSERT INTO review_queue(
              scope_id,review_id,card_json,reasons,candidates_json,
              created_at,resolved_at,resolution_json
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                item.scope_id,
                item.review_id,
                self._json_param(item.card_json),
                self._json_param(item.reasons),
                self._json_param(item.candidates_json),
                as_utc(item.created_at),
                as_utc(item.resolved_at) if item.resolved_at else None,
                (
                    self._json_param(item.resolution_json)
                    if item.resolution_json is not None
                    else None
                ),
            ),
        )
        return True

    def review_item(self, scope_id: str, review_id: str) -> ReviewItem | None:
        row = self._execute(
            "SELECT * FROM review_queue WHERE scope_id=%s AND review_id=%s",
            (scope_id, review_id),
        ).fetchone()
        return self._row_to_review_item(row) if row is not None else None

    def review_items(
        self, scope_id: str, *, pending_only: bool = True
    ) -> list[ReviewItem]:
        sql = "SELECT * FROM review_queue WHERE scope_id=%s"
        if pending_only:
            sql += " AND resolved_at IS NULL"
        sql += ' ORDER BY created_at,review_id COLLATE "C"'
        return [
            self._row_to_review_item(row)
            for row in self._execute(sql, (scope_id,))
        ]

    def resolve_review_item(
        self,
        scope_id: str,
        review_id: str,
        *,
        resolved_at: datetime,
        resolution: dict[str, Any],
    ) -> ReviewItem:
        cursor = self._execute(
            """
            UPDATE review_queue SET resolved_at=%s,resolution_json=%s
            WHERE scope_id=%s AND review_id=%s AND resolved_at IS NULL
            """,
            (
                as_utc(resolved_at),
                self._json_param(resolution),
                scope_id,
                review_id,
            ),
        )
        if cursor.rowcount != 1:
            existing = self.review_item(scope_id, review_id)
            if existing is None:
                raise KeyError(f"unknown review_id: {review_id}")
            raise ValueError(f"review_id {review_id!r} is already resolved")
        resolved = self.review_item(scope_id, review_id)
        assert resolved is not None
        return resolved

    def record_gate_report(
        self,
        scope_id: str,
        *,
        accepted: int,
        rejections: dict[str, int],
        unchanged_dropped: int = 0,
        handle_conflicts: int = 0,
        route_counts: dict[str, int] | None = None,
    ) -> None:
        counters = {
            "ACCEPTED": accepted,
            "UNCHANGED_DROPPED": unchanged_dropped,
            "HANDLE_CONFLICTS": handle_conflicts,
            **{
                counter.upper(): (route_counts or {}).get(counter, 0)
                for counter in ROUTE_COUNTER_NAMES
            },
            **rejections,
        }
        for counter, count in counters.items():
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
            unchanged_dropped=counters.pop("UNCHANGED_DROPPED", 0),
            handle_conflicts=counters.pop("HANDLE_CONFLICTS", 0),
            **{
                counter: counters.pop(counter.upper(), 0)
                for counter in ROUTE_COUNTER_NAMES
            },
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
        unchanged_dropped: int = 0,
        gate_accepted: int = 0,
        gate_rejected: dict[str, int] | None = None,
        handle_conflicts: int = 0,
        route_counts: dict[str, int] | None = None,
        last_error: str | None = None,
    ) -> None:
        cursor = self._execute(
            """
            UPDATE tasks SET
              status=%s,cards_produced=%s,new_assertions=%s,unchanged_dropped=%s,
              gate_accepted=%s,gate_rejected_json=%s,handle_conflicts=%s,
              route_handle=%s,route_thread=%s,route_evidence=%s,route_model=%s,
              route_new=%s,route_review=%s,route_disagreements=%s,
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
                unchanged_dropped,
                gate_accepted,
                self._json_param(gate_rejected or {}),
                handle_conflicts,
                *((route_counts or {}).get(counter, 0) for counter in ROUTE_COUNTER_NAMES),
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
        quiet_cutoff: datetime,
        *,
        delay_cutoff: datetime | None = None,
        min_accepted: int = 1,
        max_attempts: int = MAX_TASK_ATTEMPTS,
    ) -> list[str]:
        rows = self._execute(
            """
            SELECT scope_id FROM tasks
            WHERE (
              status=%s OR (status=%s AND attempts < %s)
            ) AND kind='messages' AND newest_message_at IS NOT NULL
            GROUP BY scope_id
            HAVING (MAX(newest_message_at) <= %s AND SUM(accepted) >= %s)
              OR MIN(created_at) <= %s
            ORDER BY scope_id COLLATE "C"
            """,
            (
                TaskStatus.pending.value,
                TaskStatus.failed.value,
                max_attempts,
                as_utc(quiet_cutoff),
                min_accepted,
                as_utc(delay_cutoff) if delay_cutoff is not None else None,
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

    def theme_schedule_state(self, scope_id: str) -> ThemeScheduleState | None:
        row = self._execute(
            "SELECT * FROM theme_schedule_state WHERE scope_id=%s",
            (scope_id,),
        ).fetchone()
        if row is None:
            return None
        return ThemeScheduleState(
            scope_id=scope_id,
            last_enqueued_at=row["last_enqueued_at"],
            last_run_at=row["last_run_at"],
        )

    def set_theme_schedule_state(
        self,
        scope_id: str,
        *,
        last_enqueued_at: datetime | None = None,
        last_run_at: datetime | None = None,
    ) -> ThemeScheduleState:
        self._execute(
            """
            INSERT INTO theme_schedule_state(scope_id,last_enqueued_at,last_run_at)
            VALUES(%s,%s,%s)
            ON CONFLICT(scope_id) DO UPDATE SET
              last_enqueued_at=COALESCE(
                excluded.last_enqueued_at,theme_schedule_state.last_enqueued_at
              ),
              last_run_at=COALESCE(
                excluded.last_run_at,theme_schedule_state.last_run_at
              )
            """,
            (
                scope_id,
                as_utc(last_enqueued_at) if last_enqueued_at else None,
                as_utc(last_run_at) if last_run_at else None,
            ),
        )
        state = self.theme_schedule_state(scope_id)
        assert state is not None
        return state

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
    def _row_to_signal(row: dict[str, Any]) -> Signal:
        return Signal.model_validate(
            {
                "scope_id": row["scope_id"],
                "record_id": row["record_id"],
                "kind": row["kind"],
                "detected_at": as_utc(row["detected_at"]),
                "matched_text": row["matched_text"],
                "subject_key": row["subject_key"],
                "status": row["status"],
                "acked_at": (
                    as_utc(row["acked_at"])
                    if row["acked_at"] is not None
                    else None
                ),
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
    def _row_to_subject_handle(row: dict[str, Any]) -> SubjectHandle:
        return SubjectHandle.model_validate(
            {
                "binding_id": row["binding_id"],
                "scope_id": row["scope_id"],
                "subject_key": row["subject_key"],
                "handle_type": row["handle_type"],
                "handle_value": row["handle_value"],
                "normalized_value": row["normalized_value"],
                "origin": HandleOrigin(row["origin"]),
                "source_refs": row["source_refs_json"],
                "bound_at": as_utc(row["bound_at"]),
                "revoked_at": (
                    as_utc(row["revoked_at"])
                    if row["revoked_at"] is not None
                    else None
                ),
                "revocation_origin": row["revocation_origin"],
                "revocation_source_refs": row[
                    "revocation_source_refs_json"
                ],
            }
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
                unchanged_dropped=row["unchanged_dropped"],
                attempts=row["attempts"],
                last_error=row["last_error"],
                gate=TaskGate(
                    accepted=row["gate_accepted"],
                    rejected=row["gate_rejected_json"],
                    unchanged_dropped=row["unchanged_dropped"],
                    handle_conflicts=row["handle_conflicts"],
                    **{counter: row[counter] for counter in ROUTE_COUNTER_NAMES},
                ),
            ),
        )

    @staticmethod
    def _row_to_review_item(row: dict[str, Any]) -> ReviewItem:
        return ReviewItem(
            scope_id=row["scope_id"],
            review_id=row["review_id"],
            card_json=row["card_json"],
            reasons=row["reasons"],
            candidates_json=row["candidates_json"],
            created_at=as_utc(row["created_at"]),
            resolved_at=(
                as_utc(row["resolved_at"])
                if row["resolved_at"] is not None
                else None
            ),
            resolution_json=row["resolution_json"],
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
