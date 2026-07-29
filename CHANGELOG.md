# Changelog

All notable changes follow Keep a Changelog conventions. Matterhorn uses
semantic versioning.

## [0.6.0] - 2026-07-29

### Added

- Projection-diff change events with deterministic IDs, SQLite/PostgreSQL
  persistence, SDK/REST/CLI reads, completion/blocking/correction semantics,
  and replay-safe golden conformance.
- Optional at-least-once webhook delivery with bounded exponential retry;
  consumers deduplicate by `event_id`.
- Versioned scope export/import carrying the assertion asset, subject identity,
  evidence lifecycle, human correction origin, and derived event history.
- Service-only UTC `daily_flush_at` scheduling with an injectable clock.
- Four language-neutral output-surface cases, bringing the suite to 47.

### Fixed

- Unknown task, scope, and subject resources now return structured 404 errors
  instead of leaking lookup exceptions.
- Synchronous message and card REST writes now include `task_id` in the
  completed result.

## [0.5.0] - 2026-07-29

### Added

- Two-verb SDK facade: `add(messages)` plus zero-LLM `matters()`, with the
  closed minimal Message contract and scope/conversation source namespacing.
- Persistent SQLite/PostgreSQL task receipts, `task()`, synchronous `flush()`,
  `wait=True`, and per-task gate rejection breakdowns.
- Resource-style REST routes, MCP `add_messages`, advanced `add_cards`, and CLI
  `mh add`, `mh matters`, `mh flush`, and `mh task`.
- Idempotent `mh init`, `matterhorn.toml` defaults, an offline demo message
  file, and service-mode-only quiet-period auto-flush.
- Three language-neutral Message-door conformance cases covering the happy
  path, colliding IDs, and receipt/flush replay.

### Changed

- `ingest()` is now a deprecated alias for asynchronous `add_cards()`;
  deterministic card application remains the internal promise boundary.
- README and Skill front doors now lead with `add_messages` and
  `list_matters`, and document the best-effort/deterministic onion boundary.
- REST removes all legacy RPC-style endpoints.

## [0.4.0] - 2026-07-29

### Added

- Provider-neutral, namespaced Record contract with authors, threads,
  reactions, attachments, edit/revocation timestamps, and permalinks.
- Pure Slack history/Events adapter, realistic fixtures, readable Block Kit and
  mrkdwn normalization, subtype filtering, and an offline end-to-end example.
- `mh extract`, `mh sync-status`, MCP `add_records`, and REST
  `POST /v1/add_records` write surfaces.
- Per-container cursors/watermarks and idempotent overlapping-window/backfill
  support.
- INV-11 golden conformance for edit and deletion lifecycle, plus a
  cross-channel same-`ts` INV-5 regression case.

### Changed

- SourceRef and query evidence now carry human-followable URIs and active or
  revoked lifecycle status.
- Thread identity is the first deterministic matter boundary; cross-thread
  merging retains INV-5's absolute floor of two shared sources.
- Edited messages append observations/assertions, while deletions retain
  immutable assertions and revoke only their evidence.

## [0.3.0] - 2026-07-29

### Added

- Gated, profile-derived message-to-card extraction with shared evidence
  traceability checks and deterministic card IDs.
- Pure ReMe and OpenViking digest adapters with realistic fixtures.
- psycopg v3 PostgreSQL Store with writable-primary checks.
- Dual-backend golden conformance execution and `mh conformance run`.
- Docker images, disposable PostgreSQL conformance compose, REST service
  compose, Python 3.11–3.13 CI, release metadata, documentation, and four
  runnable examples.

### Changed

- Extended the normative specification to cover M3 without changing the nine
  principles or ten core invariants.

## [0.2.0] - 2026-07-28

### Added

- Async semantic distillation, thirteen-reason validation gate, child-subject
  creation, official-SDK MCP server, FastAPI REST API, and Claude Skill.

## [0.1.0] - 2026-07-27

### Added

- Deterministic core, SQLite Store, query family, CLI, schema profiles, and
  language-neutral conformance suite.
