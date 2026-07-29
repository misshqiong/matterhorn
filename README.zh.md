# Matterhorn

面向 agent 的确定性、可溯源时态记忆：喂入消息，得到事项；状态、负责人、阻塞与历史
全部由持久化证据推导，而不是在回答时让模型临场生成。

## 五分钟旅程

```console
pip install 'matterhorn-memory[mcp]'
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh add demo-messages.yaml
mh flush demo
mh matters demo
```

`mh init` 会幂等地创建本地 SQLite 配置和一个离线 fixture 小样例。最后一条命令会
打印类似下面的投影事项：

```json
{
  "title": "Payment refactor",
  "status": "in_progress",
  "owners": ["u1"],
  "next_step": "Integration testing"
}
```

fixture 只替代演示里的「消息→卡」提取器；处理真实消息时，请在
`matterhorn.toml` 或环境变量里配置 OpenAI-compatible / Anthropic 写侧网关。

在当前目录写入 Claude Code 的 `.mcp.json`：

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

然后问：**“谁负责支付重构？证据是什么？”** agent 会调用 `list_matters` 和查询
工具。Matterhorn 不让模型组装答案；答案和证据都来自确定性投影。

## 两个动词的 SDK

```python
from matterhorn import Engine

engine = Engine("sqlite:///team.db", llm=my_write_gateway)
receipt = engine.add(
    scope_id="team-a",
    messages=[
        {
            "id": "m1",
            "sender": {"id": "u1", "name": "王腾"},
            "text": "线上模型成功率异常，我加了降级策略",
            "sent_at": "2026-07-28T14:00:00+08:00",
        }
    ],
)
engine.flush("team-a")

for matter in engine.matters("team-a"):
    print(matter.title, matter.status, matter.owners, matter.blocked_by)

print(engine.task(receipt.task_id).gate)
```

`add()` 只持久化任务并立即返回，不调用 LLM；`wait=True` 会同步跑同一条流水线。
任务跨进程保留，并公开 gate 的接受数与分原因拒绝数。包括 `matters()` 在内的所有
读取都不会调用 LLM。

## 承诺边界

```text
默认入口: add(messages)
        │
        ▼
[内置提取器: Message → EpisodeCard，LLM best-effort，可替换]
        │
════════╪════ 引擎承诺边界 ═════════════════════════════════════════════
        ▼
   EpisodeCard ──► 校验 ──► 断言 ──► 区间 ──► 答案
        ▲          确定、幂等、可重放（INV-1…INV-11）
        │
高级入口: add_cards(episode_cards)
```

卡以下是 best-effort 提取；从带证据的卡到最终答案，是 Matterhorn 的硬确定性承诺。
一种新输入只有在能无损映射成带溯源 EpisodeCard 时才可准入，绝不为接入便利松动
P5。

## 渐进披露

- `engine.query.current/timeline/at/by_person` 提供双时间轴细节；
- `engine.correct(...)` 追加优先级更高的人工断言，不删除历史；
- `engine.add_cards(...)` 是高级卡级直入口；
- `engine.add_records(...)` 仍可供 provider 集成使用，但不再占 README 首屏；
- `ingest(...)` 是 `add_cards(...)` 的弃用别名。

服务模式提供 `/v1/scopes/{scope_id}` 资源式 REST 和
`/v1/tasks/{task_id}` 持久任务。只有 `mh serve` 运行静默期自动 flush（默认
10 分钟）；嵌入模式仍由宿主调用 `flush()` 或 `wait=True`。

唯一规范是 [spec/SPEC.md](spec/SPEC.md)。43 个语言无关 golden 用例已经覆盖
消息入口、跨会话同 ID 隔离，以及 receipt/flush 幂等重放：

```console
$ mh conformance run
SUMMARY passed=43 failed=0 total=43
```

## 文档

- [快速开始](docs/getting-started.md)
- [核心概念与承诺边界](docs/core-concepts.md)
- [MCP 与 Claude Code](docs/mcp-claude-code.md)
- [人工纠错](docs/corrections.md)
- [Slack / Record 集成](docs/slack.md)
- [Schema 编写](docs/schema-authoring.md)
- [PostgreSQL 部署](docs/postgresql.md)

Matterhorn 要求 Python 3.11+，使用 Apache-2.0 许可证。
