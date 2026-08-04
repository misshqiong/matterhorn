# Agent-team hub topology

Matterhorn's service is the convergence point for inputs and the mount point
for consumers. The browser, Claude Code sessions, agents, and automation see
the same scopes because they use one process and one public service boundary.

```text
Claude sessions ─┐
Agent SDK teams ─┼── messages / cards / records ──┐
IMAP mail ───────┤                                │
REST producers ──┘                                ▼
                                      ┌──────────────────────┐
                                      │ Matterhorn hub       │
                                      │ mh serve / console   │
                                      │ one DB owner         │
                                      │ REST + /mcp + UI     │
                                      └──────────────────────┘
                                                │
                       ┌────────────────────────┼──────────────────────┐
                       ▼                        ▼                      ▼
                Browser Console         Claude Code teams       Agent consumers
                live every ~5s          MCP over HTTP           REST or MCP
```

## Share one scope from Claude Code

Start one hub:

```console
mh console --no-open
```

Every teammate or agent workspace that should share `release-room` uses the
same project `.mcp.json`:

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

Generate that file and merge lifecycle hooks with:

```console
mh setup claude-code \
  --url http://127.0.0.1:8000 \
  --scope release-room
```

The `Stop` hook posts the recent user/assistant transcript tail after every
completed turn, while `SessionEnd` posts the full transcript and
`SessionStart` injects the scope's open matters. Overlap is harmless because
Claude transcript messages use deterministic IDs.

For a single GLOBAL configuration (hooks in `~/.claude/settings.json`
covering every project), pass `--scope auto` to the `mh hook` commands: each
project then resolves its own scope — a `.matterhorn-scope` file at the
project root (or any parent) names it explicitly, and projects without one
get a stable derived scope (`cc-<name>-<path digest>`), so one config never
mixes projects into one partition. Explicit MCP tool calls from
every mounted session reach the same nine-tool server. For multiple machines,
replace loopback with the authenticated TLS origin described under Security.

## Agent SDK and subagents

Use a stable agent name as `sender.id`. Person-valued extraction can then make
that agent queryable through `query_by_person`, just like a human participant.
Keep a team run or thread in `conversation_id`.

```python
import httpx

hub = "http://127.0.0.1:8000"
scope = "release-room"

message = {
    "id": "planner-agent:run-42:1",
    "sender": {"id": "planner-agent", "name": "Planner agent"},
    "text": "I own the release checklist; verification is the next step.",
    "sent_at": "2026-07-30T12:00:00Z",
    "conversation_id": "agent-team:run-42",
}

with httpx.Client(base_url=hub, timeout=2) as client:
    receipt = client.post(
        f"/v1/scopes/{scope}/messages",
        json={"messages": [message], "wait": False},
    ).json()
    related = client.get(
        f"/v1/scopes/{scope}/query/by-person",
        params={"person_id": "planner-agent"},
    ).json()
```

A coordinator can give each subagent a distinct sender ID such as
`planner-agent`, `implementation-agent`, and `review-agent`, while all use the
same scope and run-level `conversation_id`. `query_by_person` then answers
which current matters are related to each named agent.

## Ownership and concurrency

**One process owns a database file.** Hub mode means the service owns it
exclusively. Do not mount the SQLite file in agent processes and do not launch
one stdio `mh mcp` per teammate against that file. All producers and consumers
use the hub's REST or MCP-HTTP URL.

Embedded stdio remains useful for a single local host. It is not the topology
for a shared team.

## Security boundary

Matterhorn binds to `127.0.0.1` by default. That is the safe v1 boundary:
Matterhorn has no built-in authentication or tenant authorization.

Cross-machine mounting requires an authenticated TLS reverse proxy or trusted
gateway in front of both `/mcp` and `/v1`. Keep Matterhorn on loopback behind
that boundary, authenticate every client at the proxy, and restrict scope
access there. Exposing the raw v1 port on a shared network is outside the v1
security model.

For the browser's live hub panels, see [Matterhorn Console](console.md). For
transport and setup details, see [MCP and Claude Code](mcp-claude-code.md).
