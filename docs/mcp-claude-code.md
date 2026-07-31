# MCP and Claude Code

Matterhorn registers the same nine typed tools once for both transports:
`add_messages`, `add_cards`, `add_records`, `query_current`,
`query_timeline`, `query_at`, `query_by_person`, `list_matters`, and
`correct`. Install the API extra for a hub or the MCP extra for embedded
stdio.

## Set up Claude Code

Run setup in the Claude Code project and choose one ownership model:

```console
# Embedded: Claude Code starts one stdio database owner.
mh setup claude-code

# Hub: Claude Code mounts the already-running service.
mh setup claude-code --url http://127.0.0.1:8000
```

The default scope is the project directory name. Setup read-modify-writes the
`matterhorn` entry in `.mcp.json` and Matterhorn hook entries in
`.claude/settings.json`; unrelated servers, settings, and hook handlers remain
in place.

Embedded mode writes a stdio entry with `command: mh`, `args: [mcp]`, and
absolute `MATTERHORN_DB` plus `MATTERHORN_SCHEMA` environment values. Hub mode
writes the current Claude Code URL shape:

```json
{
  "mcpServers": {
    "matterhorn": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

Passing either a service base URL or its `/mcp` URL produces the normalized
`/mcp` endpoint. If the default hub answers `/healthz` within 200 ms while
embedded setup is requested, setup prints a hint but keeps embedded mode.

## Lifecycle and per-turn delivery

Setup writes command hooks with an absolute path to the running `mh` executable
and a two-second outer timeout:

- `SessionStart` requests the scope's matters, filters terminal statuses, and
  prints a compact open-matters block for Claude Code context.
- In hub mode, `Stop` delivers the most recent 40 text-bearing user/assistant
  transcript messages after every completed turn, so the shared wall can
  update before the session ends.
- In hub mode, `SessionEnd` delivers the full text-bearing transcript. The
  overlap with `Stop` is safe because message IDs are deterministic from the
  Claude session plus the record UUID, or a stable content fallback when no
  UUID exists.

Both delivery hooks post the minimal Message contract with `wait: false`.
Unknown or malformed transcript JSONL records are skipped. Embedded setup also
writes `SessionStart` and `SessionEnd`, but without a hub URL they are silent
no-ops; this prevents a second process from opening the embedded database.

Hook work has its own 1.5-second total deadline inside Claude Code's two-second
command cap. Malformed input, a missing transcript, a down service, a timeout,
or a bad response produces no alarming stdout/stderr and still exits zero.
Hooks never block or break a Claude session.

## Hub transport and concurrency

`mh serve` and `mh console` mount the official SDK's Streamable HTTP
application at `/mcp`; `mh mcp` is the embedded stdio entry point.

**One process owns a database.** In embedded mode, the one stdio server owns
its configured database. Do not point multiple stdio servers, hooks, CLIs, or
services at that SQLite file concurrently.

In hub mode, the `mh serve` or `mh console` process owns the database
exclusively. Claude Code sessions, browsers, agents, and automation share it
through REST or MCP over HTTP, never by opening the database path. Use an
explicit port in the URL, such as `http://127.0.0.1:8000/mcp`.

## Troubleshooting

- **A hook works now but fails after moving the environment:** generated hook
  commands intentionally contain the absolute `mh` path. Rerun setup from the
  replacement environment to refresh that path.
- **The service is down:** hub hooks are intentionally silent and exit zero in
  at most two seconds. Start the hub and the next `Stop`/`SessionEnd` delivery
  will resume; deterministic IDs make overlapping retries no-ops.
- **Claude Code cannot mount the hub:** confirm the service base URL has an
  explicit port and that `/mcp` is reachable through the same authenticated
  boundary as `/v1`.
- **Multiple local clients need the same scope:** run one hub and give each
  project the same `--url` and `--scope`. Do not share the SQLite path.

See [Agent-team topology](agent-team.md) for multi-person and multi-agent
patterns.
