# MCP and Claude Code

Install the official MCP SDK extra:

```console
pip install 'matterhorn-memory[mcp]'
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

The server exposes exactly nine typed tools. Use `add_messages` as the default
write door and `list_matters` as the default read door. `query_current`,
`query_timeline`, `query_at`, and `query_by_person` provide detail.
`add_cards` and `add_records` are advanced inputs; `correct` appends a human
correction.
