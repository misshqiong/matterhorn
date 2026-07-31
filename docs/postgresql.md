# PostgreSQL deployment

Install `matterhorn-memory[postgres]` and pass a psycopg v3 DSN to `Engine` or
`mh serve --db`.

The DSN must remain pinned to one writable primary. Matterhorn checks
`transaction_read_only` and `pg_is_in_recovery()` during Store initialization
and fails before serving traffic if either identifies a read-only/replica
connection. Do not use statement-level read/write splitting or transaction
pooling that can move one Engine between database servers.

INV-6 requires card idempotency, identity, extraction, assertion writes,
projection replacement, statistics, and materialization to commit as one
transaction. Queries use that same primary connection, so a query issued after
the synchronous card-application stage returns sees the committed projection.

Run the full cross-backend gate:

```console
docker compose -f compose.postgres.yml up --build --abort-on-container-exit \
  --exit-code-from conformance
```

Or point pytest at an existing throwaway database:

```console
MATTERHORN_TEST_POSTGRES_DSN=postgresql://... \
  .venv/bin/python -m pytest -q tests/test_conformance.py
```

The language-neutral reference harness can target the same writable primary:

```console
mh conformance run --backend postgres --dsn postgresql://...
```
