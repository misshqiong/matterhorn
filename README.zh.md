# Matterhorn

面向 agent 的确定性、可溯源时态记忆：喂入消息，得到事项；状态、负责人、阻塞与历史
全部由持久化证据推导，而不是在回答时让模型临场生成。

## 在线演示 →

**[邮件事项台账](https://misshqiong.github.io/matterhorn/demo/email-ledger.html)**——
从 18 封往来邮件中蒸馏出的供应商项目：交期两次推迟的时间线分段、一次决策反转、
一次以 ✏️ 人工纠错入账的负责人交接，以及首屏红色标出的逾期承诺。页面上每个事实
都能点回底部的原始邮件。另见
**[本项目自己的开发台账](https://misshqiong.github.io/matterhorn/demo/self-ledger.html)**，
由同一渲染器从仓库开发历史生成。两个页面都是 `mh export <scope> --format html`
产出的单文件自包含 HTML——无服务端、无前端框架、零外部请求。

## 📒 开发账本

Matterhorn 用自身追踪这个项目的开发过程。公开的
[MATTERS.md](MATTERS.md) 每晚由 CI 重新生成；CI 是账本唯一运行 LLM 的地方。
任何人都能从已提交的 `ledger/assertions.json` 在本地复现读侧，无需 LLM key。

写路径把新增 Git commit 与 GitHub 活动转换成经过 gate 的断言，再替换持久断言导出；
读路径从断言重建可丢弃的 SQLite 投影，并确定性渲染人类可读账本。完整设计见
[开发账本说明](docs/ledger.zh.md)。

## 五分钟旅程

```console
pip install 'matterhorn-memory[mcp]'
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh add demo-messages.yaml
mh flush demo
mh matters demo
mh events demo
mh export demo --out demo-snapshot.json
```

`mh init` 会幂等地创建本地 SQLite 配置和一个离线 fixture 小样例。`mh matters`
会打印类似下面的投影事项：

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

## 写侧网关环境变量

| 变量 | 含义 |
| --- | --- |
| `MATTERHORN_PROVIDER` | `openai-compatible` 或 `anthropic` |
| `MATTERHORN_BASE_URL` | provider base URL；OpenAI-compatible 网关必填 |
| `MATTERHORN_MODEL` | provider 模型名 |
| `MATTERHORN_API_KEY` | 首选 provider 凭证；仍支持 provider 原生 key |
| `MATTERHORN_TIMEOUT` | 正浮点请求超时秒数，默认 `60` |

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
            "sender": {"id": "u1", "name": "Dana Reyes"},
            "text": "Production model success rate dropped; I added a fallback strategy",
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

## 输出面与数据所有权

- 查询答案与 MemoryCard 仍是带证据的确定性读取；
- 投影变化会生成 `status_changed`、`matter_completed`、
  `value_corrected` 等可溯源事件，可从 `engine.events()`、
  `GET /v1/scopes/{scope}/events` 或 `mh events` 读取；
- `mh serve --webhook-url URL` 以至少一次语义推送事件批次，并做有界重试；
  消费方按确定性的 `event_id` 去重；
- `mh export SCOPE` 是数据所有权的交付形式：一个版本化 JSON 文档，包含断言、
  subjects、证据生命周期与派生事件历史；`mh import` 只导入空 store，并复现相同
  投影和查询答案，人工纠错的 `origin=human` 原样保留。

断言始终是唯一事实资产；事件来自投影差异，区间与 MemoryCard 都可重建。这个可携带
的所有权边界，是开源记忆引擎承载团队长期知识时最重要的信任基础之一。

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
10 分钟）；它也可按 UTC `daily_flush_at = "HH:MM"` 每日 flush，并推送事件
webhook。嵌入模式仍由宿主调用 `flush()` 或 `wait=True`。

唯一规范是 [spec/SPEC.md](spec/SPEC.md)。47 个语言无关 golden 用例已经覆盖
消息入口、跨会话同 ID 隔离，以及 receipt/flush 幂等重放：

```console
$ mh conformance run
SUMMARY passed=47 failed=0 total=47
```

## 文档

- [快速开始](docs/getting-started.md)
- [自托管开发账本](docs/ledger.zh.md)
- [核心概念与承诺边界](docs/core-concepts.md)
- [MCP 与 Claude Code](docs/mcp-claude-code.md)
- [人工纠错](docs/corrections.md)
- [事件与 webhook 投递](docs/webhooks.md)
- [Slack / Record 集成](docs/slack.md)
- [Schema 编写](docs/schema-authoring.md)
- [PostgreSQL 部署](docs/postgresql.md)

Matterhorn 要求 Python 3.11+，使用 Apache-2.0 许可证。
