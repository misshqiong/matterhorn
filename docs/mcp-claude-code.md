# MCP and Claude Code

Install the official MCP SDK extra:

```console
pip install 'matterhorn[mcp]'
```

Add `.mcp.json` at the Claude Code project root:

```json
{
  "mcpServers": {
    "matterhorn": {
      "command": "/absolute/venv/bin/mh",
      "args": ["mcp", "--db", "/absolute/project/memory.db",
               "--schema", "org-matters/v1"]
    }
  }
}
```

Copy the Matterhorn Skill to `.claude/skills/matterhorn/SKILL.md`. A complete
checked-in setup is under [examples/claude-code](../examples/claude-code).

The server exposes exactly eight typed tools. Use `list_matters` to discover
subjects, `query_current` for current facts, `query_timeline` to explain
changes, `query_at` for historical reconstruction, person queries for current
relationships, `add_records` for raw communication, `add_episode_cards` for
pre-built evidence-backed cards, and `correct` when a human explicitly fixes
memory.
