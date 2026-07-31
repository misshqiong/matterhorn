# Agent 团队 Hub 拓扑

Matterhorn 服务既是所有输入的汇聚点，也是所有消费者的挂载点。浏览器、Claude Code
会话、agent 与自动化只要访问同一个公开服务边界，就会看到同一组 scope。

```text
Claude 会话 ────┐
Agent SDK 团队 ─┼── messages / cards / records ──┐
IMAP 邮件 ──────┤                                │
REST 生产者 ────┘                                ▼
                                      ┌──────────────────────┐
                                      │ Matterhorn hub       │
                                      │ mh serve / console   │
                                      │ 单一 DB owner        │
                                      │ REST + /mcp + UI     │
                                      └──────────────────────┘
                                                │
                       ┌────────────────────────┼──────────────────────┐
                       ▼                        ▼                      ▼
                  浏览器 Console          Claude Code 团队       Agent 消费者
                  约每 5 秒刷新            MCP over HTTP         REST 或 MCP
```

## Claude Code 共享一个 scope

只启动一个 hub：

```console
mh console --no-open
```

所有要共享 `release-room` 的成员或 agent 工作区，都使用相同的项目级
`.mcp.json`：

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

下面的命令会生成该文件并合并生命周期 hooks：

```console
mh setup claude-code \
  --url http://127.0.0.1:8000 \
  --scope release-room
```

`SessionEnd` hook 把每个会话的 user / assistant 文本发到 `release-room`；
`SessionStart` 把该 scope 的未关闭事项注入上下文。所有已挂载会话的显式 MCP
调用都会到达同一个九工具服务。多机共享时，应把 loopback URL 换成下文安全章节
所述、具备认证与 TLS 的入口。

## Agent SDK 与 subagent

用稳定的 agent 名称作为 `sender.id`。这样，人值谓词提取后，这个 agent 就能像
人类参与者一样被 `query_by_person` 查询。一次团队执行或一个线程共用
`conversation_id`。

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

协调者可给每个 subagent 分配不同的 sender ID，例如 `planner-agent`、
`implementation-agent`、`review-agent`；它们使用相同 scope 和本次执行的
`conversation_id`。随后，`query_by_person` 可回答每个具名 agent 当前关联哪些事项。

## 所有权与并发

**一个数据库文件只能由一个进程拥有。** Hub 模式下，服务进程独占数据库。不要让
agent 进程直接挂载 SQLite 文件，也不要让每个成员都用 stdio `mh mcp` 打开同一个
文件。所有生产者和消费者都必须访问 hub 的 REST 或 MCP-HTTP URL。

嵌入式 stdio 仍适合单一的本地宿主，但它不是共享团队的拓扑。

## 安全边界

Matterhorn 默认绑定 `127.0.0.1`。这是 v1 的安全边界：Matterhorn 本身不提供认证
或租户授权。

跨机器挂载必须在 `/mcp` 与 `/v1` 前放置带认证的 TLS 反向代理或可信网关。
Matterhorn 保持在该边界后的 loopback，由代理认证所有客户端并限制 scope 访问。
把裸 v1 端口暴露到共享网络不属于 v1 安全模型。

浏览器实时 hub 面板见 [Matterhorn Console](console.zh.md)；传输与安装细节见
[MCP 与 Claude Code](mcp-claude-code.md)。
