# Matterhorn

Deterministic, evidence-backed temporal memory for agents. Feed messages in;
get matters whose status, owners, blockers, and history are derived from
persisted evidence—not generated at answer time.

## Five-minute journey

```console
pip install 'matterhorn-memory[mcp]'
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh add demo-messages.yaml
mh flush demo
mh matters demo
```

`mh init` creates an idempotent local SQLite setup and a tiny offline fixture
demo. The last command prints a projected matter such as:

```json
{
  "title": "Payment refactor",
  "status": "in_progress",
  "owners": ["u1"],
  "next_step": "Integration testing"
}
```

The fixture is only the demo's replaceable message-to-card extractor. For real
messages, configure an OpenAI-compatible or Anthropic write gateway in
`matterhorn.toml` or the documented environment variables.

Connect Claude Code by adding `.mcp.json` in this directory:

```json
{
  "mcpServers": {
    "matterhorn": {
      "command": "mh",
      "args": ["mcp"]
    }
  }
}
```

Then ask: **“Who owns the payment refactor, and what evidence supports that?”**
The agent uses `list_matters` and the query tools. Matterhorn does not ask a
model to compose the answer: the answer and evidence come from deterministic
projection.

## Two-verb SDK

```python
from matterhorn import Engine
from matterhorn.distill import OpenAICompatibleGateway

engine = Engine(
    "sqlite:///team.db",
    llm=OpenAICompatibleGateway(
        base_url="https://llm.example/v1",
        api_key="...",
        model="...",
    ),
)
receipt = engine.add(
    scope_id="team-a",
    messages=[
        {
            "id": "m1",
            "sender": {"id": "u1", "name": "王腾"},
            "text": "线上模型成功率异常，我加了降级策略",
            "sent_at": "2026-07-28T14:00:00+08:00",
        }
    ],
)
engine.flush("team-a")

for matter in engine.matters("team-a"):
    print(matter.title, matter.status, matter.owners, matter.blocked_by)

print(engine.task(receipt.task_id).gate)
```

`add()` persists a task and returns immediately without calling the LLM.
`wait=True` runs the same pipeline synchronously. Tasks survive process
restart and expose accepted/rejected gate counts. Reads, including
`matters()`, never call an LLM.

## The promise boundary

```text
Public door: add(messages)
        │
        ▼
[built-in extractor: Message → EpisodeCard, LLM best-effort, replaceable]
        │
════════╪════ Engine promise boundary ═══════════════════════════════════
        ▼
   EpisodeCard ──► validation ──► assertions ──► intervals ──► answers
        ▲          deterministic, idempotent, replayable (INV-1…INV-11)
        │
Advanced door: add_cards(episode_cards)
```

Below the card is best-effort extraction. From the evidence-backed card through
every answer is Matterhorn's hard deterministic promise. A new input form is
accepted only when it maps losslessly to that card contract without weakening
provenance.

## Progressive disclosure

- `engine.query.current/timeline/at/by_person` exposes bi-temporal detail.
- `engine.correct(...)` appends a higher-priority human assertion without
  deleting history.
- `engine.add_cards(...)` is the advanced direct card door.
- `engine.add_records(...)` remains importable for provider integrations but
  is not the README front door.
- `ingest(...)` is a deprecated alias for `add_cards(...)`.

Service mode exposes resource-style REST endpoints under
`/v1/scopes/{scope_id}` and persistent tasks under `/v1/tasks/{task_id}`.
`mh serve` alone runs quiet-period auto-flush (10 minutes by default);
embedded mode remains host-driven through `flush()` or `wait=True`.

The normative contract is [spec/SPEC.md](spec/SPEC.md). Its 43
language-neutral golden cases include the Message door, conversation-scoped ID
collision protection, and receipt/flush replay:

```console
$ mh conformance run
SUMMARY passed=43 failed=0 total=43
```

## Guides

- [Getting started](docs/getting-started.md)
- [Core concepts and the promise boundary](docs/core-concepts.md)
- [MCP and Claude Code](docs/mcp-claude-code.md)
- [Human correction](docs/corrections.md)
- [Slack and Record integrations](docs/slack.md)
- [Schema authoring](docs/schema-authoring.md)
- [PostgreSQL deployment](docs/postgresql.md)
- [Positioning alongside L1 tools](docs/positioning.md)

Development:

```console
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

Matterhorn requires Python 3.11+ and is licensed under Apache-2.0.
