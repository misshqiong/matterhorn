# Changelog

All notable changes follow Keep a Changelog conventions. Matterhorn uses
semantic versioning.

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
