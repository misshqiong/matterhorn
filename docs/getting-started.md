# Getting started

Install the surface you need:

```console
pip install matterhorn-memory
pip install 'matterhorn-memory[mcp]'
pip install 'matterhorn-memory[api,postgres]'
```

Run the local five-minute journey:

```console
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh add demo-messages.yaml
mh flush demo
mh matters demo
```

`mh init` creates `matterhorn.toml`, SQLite storage, and a tiny offline fixture
demo. It is idempotent. The config supplies the default database, schema,
scope, provider, and service quiet period to later CLI commands.

For a real write gateway, set `MATTERHORN_PROVIDER`,
`MATTERHORN_BASE_URL`, `MATTERHORN_MODEL`, and `MATTERHORN_API_KEY`.
`MATTERHORN_TIMEOUT` is a positive floating-point timeout in seconds and
defaults to `60`. Provider-native `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`
remain credential fallbacks for their respective providers.

The embedded API has two front-door verbs:

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
`wait=True` does the same synchronously. `matters()` and every `query.*` method
are zero-LLM reads.

`add_cards()` is the advanced direct entry for evidence-backed EpisodeCards.
`add_records()` remains available to provider integrations. `ingest()` is a
deprecated alias for `add_cards()`.

Useful commands:

```console
mh add messages.yaml
mh add - --scope team
mh flush team
mh matters team
mh task TASK_ID
mh query current team SUBJECT status
mh correct correction.yaml
mh conformance run
mh serve
mh mcp
```

Only `mh serve` runs quiet-period scheduling. Embedded applications must call
`flush()` or use `wait=True`; Matterhorn does not hide a general cron system in
the library.

Continue with [core concepts](core-concepts.md), the
[MCP guide](mcp-claude-code.md), and the
[correction example](../examples/correction/README.md).
