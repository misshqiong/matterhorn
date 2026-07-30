# Matterhorn

Deterministic, evidence-backed temporal memory for agents. Feed messages in;
get matters whose status, owners, blockers, and history are derived from
persisted evidence—not generated at answer time.

## Live demo →

**[Email matter ledger](https://misshqiong.github.io/matterhorn/demo/email-ledger.html)** —
a vendor project distilled from 18 emails: slipped deadlines as timeline
segments, a decision reversal, an owner handoff filed as a ✏️ human
correction, and an overdue commitment flagged in red. Every fact links to the
source email at the bottom of the page. Also:
**[this project's own ledger](https://misshqiong.github.io/matterhorn/demo/self-ledger.html)**,
rendered by the same exporter from the repository's development history.
Both pages are single self-contained HTML files produced by
`mh export <scope> --format html` — no server, no JavaScript framework,
no external requests.

## Console

Matterhorn ships a Console operating surface for viewing scopes and matters,
running deterministic queries, making first-class human corrections, feeding
pasted chat/YAML/JSON/email, and optionally asking a tool-using model whose only
tools are the five public deterministic queries.

The Console also configures and runs the memory-only-credential
[IMAP mail connector](docs/mail.md), accepts file uploads, and provides a quick
single-message jot.

```console
pip install 'matterhorn-memory[api]'
mh console
```

The static Console and REST API share `127.0.0.1:8000`; the browser opens
`/console`. The browser talks only to documented public REST endpoints. The
built-in fictional Dana Reyes / octo-org sample uses a packaged fixture and
needs no key.

> **Screenshot placeholder:** scope browser, matter detail and correction,
> query workbench, feed receipt, and evidence-bearing chat.

Matterhorn still has no built-in business consumption UI: end-user boards
belong to hosts. The Console is the product’s operator/developer/demo surface.
See the [Console guide](docs/console.md).

The default loopback bind is intentional. A public deployment must add
authentication and a trusted network boundary in front; v1 multi-tenant
authentication remains a non-goal.

**Next version (listed, not built):** operations views for task receipts, sync
status, and the events feed; multi-scope comparison; export buttons.

## 📒 Development ledger

This project's own development is tracked by Matterhorn itself. The public
[MATTERS.md](MATTERS.md) is regenerated nightly by CI, the only place the
ledger runs an LLM. Anyone can reproduce the read side locally without an LLM
key from the committed `ledger/assertions.json`.

The write path turns new Git commits and GitHub activity into gated assertions,
then replaces the durable assertion export. The read path rebuilds a disposable
SQLite projection from those assertions and deterministically renders the
human-readable ledger. See [the ledger design](docs/ledger.md).

## Five-minute journey

```console
pip install 'matterhorn-memory[mcp]'
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh add demo-messages.yaml
mh flush demo
mh matters demo
mh events demo
mh export demo --out demo-snapshot.json
```

`mh init` creates an idempotent local SQLite setup and a tiny offline fixture
demo. `mh matters` prints a projected matter such as:

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

## Write-gateway environment

| Variable | Meaning |
| --- | --- |
| `MATTERHORN_PROVIDER` | `openai-compatible` or `anthropic` |
| `MATTERHORN_BASE_URL` | Provider base URL; required for OpenAI-compatible gateways |
| `MATTERHORN_MODEL` | Provider model name |
| `MATTERHORN_API_KEY` | Preferred provider credential; provider-native keys remain supported |
| `MATTERHORN_TIMEOUT` | Positive floating-point request timeout in seconds; defaults to `60` |

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
            "sender": {"id": "u1", "name": "Dana Reyes"},
            "text": "Production model success rate dropped; I added a fallback strategy",
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

## Output surfaces and ownership

- Query answers and MemoryCards remain deterministic, evidence-backed reads.
- Projection changes become traceable events such as `status_changed`,
  `matter_completed`, and `value_corrected`; read them with
  `engine.events()`, `GET /v1/scopes/{scope}/events`, or `mh events`.
- `mh serve --webhook-url URL` delivers event batches at least once with
  bounded retry. Consumers deduplicate by deterministic `event_id`.
- `mh export SCOPE` is the data-ownership handoff: one versioned JSON document
  containing the assertions, subjects, evidence lifecycle, and derived event
  history. `mh import` accepts it into an empty store and reproduces the same
  projections and query answers without changing human correction origin.

Assertions remain the asset and the only source of truth. Events are generated
from projection diffs, while intervals and MemoryCards are rebuildable views.
This portable ownership boundary is central to trusting an open-source memory
engine with durable team knowledge.

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
it can also honor a UTC `daily_flush_at = "HH:MM"` and push event webhooks.
Embedded mode remains host-driven through `flush()` or `wait=True`.

The normative contract is [spec/SPEC.md](spec/SPEC.md). Its 47
language-neutral golden cases include the Message door, conversation-scoped ID
collision protection, and receipt/flush replay:

```console
$ mh conformance run
SUMMARY passed=47 failed=0 total=47
```

## Guides

- [Getting started](docs/getting-started.md)
- [Console operating surface](docs/console.md)
- [IMAP mail connector](docs/mail.md)
- [Self-hosted development ledger](docs/ledger.md)
- [Core concepts and the promise boundary](docs/core-concepts.md)
- [MCP and Claude Code](docs/mcp-claude-code.md)
- [Human correction](docs/corrections.md)
- [Events and webhook delivery](docs/webhooks.md)
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
