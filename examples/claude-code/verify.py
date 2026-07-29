from __future__ import annotations

import json
from pathlib import Path


root = Path(__file__).parent
config = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
server = config["mcpServers"]["matterhorn"]
skill = root / ".claude/skills/matterhorn/SKILL.md"
assert server["command"] == "mh"
assert server["args"][0] == "mcp"
assert skill.is_file()
assert "query_timeline" in skill.read_text(encoding="utf-8")
print("mcp_server=matterhorn command=mh mcp")
print("skill=.claude/skills/matterhorn/SKILL.md")
print("tools=9")
