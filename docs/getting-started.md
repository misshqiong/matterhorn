# Getting started

Install the surface you need:

```console
pip install matterhorn-memory
pip install 'matterhorn-memory[mcp]'
pip install 'matterhorn-memory[api,postgres]'
```

`Engine("memory.db", profile)` embeds SQLite. A `postgresql://` DSN selects
PostgreSQL. Both stores implement the same transactional Store SPI and run the
same golden suite.

An input card must have a stable `(scope_id, card_id)` and at least one
`source_ref`. Ingest resolves identity, emits deterministic assertions, queues
semantic work, projects intervals, and materializes cards in one transaction.
`ingest()` never calls a model. Call `dream(scope_id)` explicitly to drain
write-side semantic work.

Useful commands:

```console
mh ingest card.yaml --db memory.db
mh query current team release status --db memory.db
mh query timeline team release status --db memory.db
mh replay team --db memory.db
mh conformance run
mh serve --db memory.db
mh mcp --db memory.db
```

Continue with [core concepts](core-concepts.md), then run the
[correction example](../examples/correction/README.md).
