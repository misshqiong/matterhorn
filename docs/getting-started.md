# Getting started

Install the surface you need:

```console
pip install matterhorn-memory
pip install 'matterhorn-memory[mcp]'
pip install 'matterhorn-memory[api,postgres]'
```

## Human product path

For a person evaluating or operating Matterhorn, initialize a local project
and open the Console:

```console
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh console
```

`mh init` idempotently creates `matterhorn.toml`, SQLite storage, and the
offline fixture files. The Console then provides the product path: configure
multiple mailboxes and AI, feed messages/files, work from one all-scope matter
wall, open scope-aware detail and correction in a modal, and use scoped chat
or deterministic queries.

The built-in Dana Reyes / octo-org sample is backed by a packaged fixture and
needs no key. A real IMAP sync needs its mailbox credential. Real extraction
and chat need an OpenAI-compatible or Anthropic key; the Console AI panel keeps
an entered key in process memory and its runtime configuration overrides the
environment for that process.

## SDK embedding path

Applications embed the two front-door verbs and decide when write work runs:

```python
from matterhorn import Engine

engine = Engine("sqlite:///memory.db", llm=my_write_gateway)
receipt = engine.add("team", messages)
engine.flush("team")
matters = engine.matters("team")
result = engine.task(receipt.task_id)
```

`add()` validates the closed minimal Message contract, persists a task, and
returns without gateway access. `flush()` runs message extraction,
deterministic card application, semantic distillation, and projection.
`wait=True` runs the same pipeline synchronously. `matters()` and every
`query.*` method are zero-model reads.

`add_cards()` is the advanced direct entry for evidence-backed EpisodeCards.
`add_records()` remains available to provider integrations. `ingest()` is a
deprecated alias for `add_cards()`.

Only service mode runs quiet-period scheduling. Embedded applications call
`flush()` or use `wait=True`; the library does not hide a general cron system.

## Current CLI surface

- Write and project: `mh init`, `mh add`, `mh ingest`, `mh extract`,
  `mh flush`, `mh dream`, `mh replay`, and `mh correct`.
- Read and move data: `mh matters`, `mh task`, `mh events`, `mh export`,
  `mh import`, `mh sync-status`, and `mh query` (`current`, `timeline`, `at`,
  `by-person`, `list`).
- Operate and integrate: `mh console`, `mh serve`, `mh mcp`, `mh mail`
  (`setup`, `sync`, `reset`), `mh setup` (`claude-code`), and `mh hook`
  (`session-start`, `session-end`, `turn-end`).
- Inspect and verify: `mh schema` (`list`, `show`) and `mh conformance`
  (`run`).

Continue with [core concepts](core-concepts.md), the
[Console guide](console.md), the [MCP guide](mcp-claude-code.md), and the
[correction example](../examples/correction/README.md).
