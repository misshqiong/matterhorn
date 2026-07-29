# Claude Code over MCP

Copy `.mcp.json` and `.claude/skills/matterhorn/SKILL.md` into a Claude Code
project. Ensure `mh` is on `PATH`, then create `.matterhorn/` in that project.
Claude Code will launch the official-SDK stdio server and the project Skill
teaches it when to query timelines and file corrections.

Verify the checked-in wiring:

```console
$ .venv/bin/python examples/claude-code/verify.py
mcp_server=matterhorn command=mh mcp
skill=.claude/skills/matterhorn/SKILL.md
tools=7
```

The seven tools are `add_episode_cards`, `query_current`, `query_timeline`,
`query_at`, `query_by_person`, `list_matters`, and `correct`.
