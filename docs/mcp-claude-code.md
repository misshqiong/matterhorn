# MCP and Claude Code

Install a service-capable extra. The API extra includes the official MCP Python
SDK because `mh serve` and `mh console` always expose Streamable HTTP at
`/mcp`:

```console
pip install 'matterhorn-memory[api]'
mh console
```

The same nine typed tools are registered once and served over both transports:
`add_messages`, `add_cards`, `add_records`, `query_current`,
`query_timeline`, `query_at`, `query_by_person`, `list_matters`, and
`correct`. `mh mcp` remains the stdio entry point for embedded use.

## One-command Claude Code setup

From a Claude Code project, choose one topology:

```console
# Embedded: Claude Code starts `mh mcp` over stdio.
mh setup claude-code

# Hub: Claude Code mounts the already-running service by URL.
mh setup claude-code --url http://127.0.0.1:8000 --scope agent-team
```

The default scope is the current directory name. Setup merges a `matterhorn`
entry into `.mcp.json` and lifecycle hooks into `.claude/settings.json`;
unrelated MCP servers, settings, and hook handlers are preserved.

Hub mode writes the current Claude Code HTTP shape:

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

This matches the current
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp), which
accepts `type: "http"` (`streamable-http` is an alias). Matterhorn uses the
official Python SDK's documented `streamable_http_app()` mount and runs its
session manager in the parent FastAPI lifespan.

Embedded mode writes:

```json
{
  "mcpServers": {
    "matterhorn": {
      "type": "stdio",
      "command": "mh",
      "args": ["mcp"],
      "env": {
        "MATTERHORN_DB": "/absolute/project/matterhorn.db",
        "MATTERHORN_SCHEMA": "org-matters/v1"
      }
    }
  }
}
```

If the default hub answers `/healthz` within 200 ms while embedded setup is
requested, setup prints a hint but does not switch modes.

## Lifecycle hooks

In hub mode, `SessionEnd` reads Claude Code's documented `transcript_path`,
extracts text-bearing user and assistant messages, and posts the minimal
Message contract to:

```text
POST /v1/scopes/{scope}/messages
{"messages": [...], "wait": false}
```

`SessionStart` gets the scope's matters, filters terminal statuses, and prints
a compact open-matters block. Claude Code adds plain `SessionStart` stdout to
the model context, as specified by its
[hooks reference](https://code.claude.com/docs/en/hooks).

The hook payload fields are documented, but Claude Code does not publish a
stable schema for every line in its transcript JSONL. Matterhorn therefore
accepts the current `type: user|assistant` plus `message.role/content` shape,
extracts only text blocks, and skips unknown or malformed records.

Every hook is fail-open: it uses a 1.5-second network timeout, catches malformed
input, unavailable services, and bad responses, writes no alarming stderr, and
exits zero. Setup gives each Claude Code command hook a two-second cap.

Embedded setup still installs the hook entries, but omits `--url`; those hooks
are silent no-ops. Automatic transcript ingestion and start-of-session context
are hub features. This avoids opening the embedded database from a second hook
process.

## The concurrency rule

**One process owns a database file.** In embedded mode, one `mh mcp` process
owns its configured file. Do not point multiple stdio servers, hooks, CLIs, or
services at that same SQLite file concurrently.

In hub mode, the database belongs exclusively to the `mh serve` / `mh console`
process. Every other process—Claude Code sessions, agent teams, scripts, and
the browser—must go through REST or MCP over HTTP. Sharing happens by mounting
the same service URL and scope, not by sharing a database path.

See [Agent-team topology](agent-team.md) for multi-person and multi-agent
patterns.
