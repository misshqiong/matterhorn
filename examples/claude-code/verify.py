from __future__ import annotations

import json
import shlex
from pathlib import Path


root = Path(__file__).parent
repository = root.parents[1]


def load(relative: str) -> dict:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def hook_tokens(settings: dict, event: str) -> list[str]:
    handler = settings["hooks"][event][-1]["hooks"][0]
    assert handler["type"] == "command"
    assert handler["timeout"] == 2
    tokens = shlex.split(handler["command"])
    assert Path(tokens[0]).name == "mh"
    return tokens[1:]


embedded = load(".mcp.json")["mcpServers"]["matterhorn"]
assert embedded["type"] == "stdio"
assert embedded["command"] == "mh"
assert embedded["args"] == ["mcp"]
assert Path(embedded["env"]["MATTERHORN_DB"]).is_absolute()
assert embedded["env"]["MATTERHORN_SCHEMA"] == "org-matters/v1"

embedded_settings = load(".claude/settings.json")
assert list(embedded_settings["hooks"]) == ["SessionStart", "SessionEnd"]
assert hook_tokens(embedded_settings, "SessionStart") == [
    "hook",
    "session-start",
    "--scope",
    "claude-code",
]
assert hook_tokens(embedded_settings, "SessionEnd") == [
    "hook",
    "session-end",
    "--scope",
    "claude-code",
]

hub = load("hub/.mcp.json")["mcpServers"]["matterhorn"]
assert hub == {
    "type": "http",
    "url": "http://127.0.0.1:8000/mcp",
}
hub_settings = load("hub/.claude/settings.json")
assert list(hub_settings["hooks"]) == ["SessionStart", "SessionEnd", "Stop"]
for event, command_name in [
    ("SessionStart", "session-start"),
    ("SessionEnd", "session-end"),
    ("Stop", "turn-end"),
]:
    assert hook_tokens(hub_settings, event) == [
        "hook",
        command_name,
        "--url",
        "http://127.0.0.1:8000",
        "--scope",
        "claude-code",
    ]

skill = root / ".claude/skills/matterhorn/SKILL.md"
assert skill.is_file()
assert "query_timeline" in skill.read_text(encoding="utf-8")

tool_names = [
    "add_messages",
    "add_cards",
    "add_records",
    "query_current",
    "query_timeline",
    "query_at",
    "query_by_person",
    "list_matters",
    "correct",
]
server_source = (repository / "src/matterhorn/mcp/server.py").read_text(
    encoding="utf-8"
)
assert all(f"def {name}(" in server_source for name in tool_names)

print("embedded=stdio command=mh mcp hooks=SessionStart,SessionEnd")
print(
    "hub=http url=http://127.0.0.1:8000/mcp "
    "hooks=SessionStart,SessionEnd,Stop"
)
print("skill=.claude/skills/matterhorn/SKILL.md")
print(f"tools={len(tool_names)}")
