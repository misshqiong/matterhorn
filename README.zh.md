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

## Console

Matterhorn 自带成熟的三栏个人 Console：左栏配置多邮箱、AI 与 Feed；中心统一展示
全部 scope 的事项卡片墙；右栏提供带 scope 的 Chat 与确定性查询。详情 modal 可用
Timeline 展示 status/progress/outcome 的来源发送者与 excerpt，也可用带人工来源的
方式把重复卡归并到 canonical 事项；被归并标题保留为「又名」，且可撤销。

Console 也能配置并运行凭证仅驻留内存的 [IMAP 邮件连接器](docs/mail.zh.md)，支持
文件上传和 quick single-message jot。

```console
pip install 'matterhorn-memory[api]'
mh console
```

静态 Console 与 REST API 共用 `127.0.0.1:8000`，浏览器自动打开 `/console`，
agent 客户端则在同一进程的 `/mcp` 挂载。浏览器只调用 OpenAPI 中公开的 REST
接口；activity、连接状态、scope 与事项列表约每五秒自动刷新。内置的虚构
Dana Reyes / octo-org 样例使用随包 fixture，零 key 即可完整运行。

<!-- screenshot: console-wall -->
![Matterhorn Console — unified matter wall](docs/images/console-wall.png)
> **截图占位：** 多邮箱与 AI 配置、统一事项墙、scope-aware 详情/纠错/归并和带证据
> Chat；事项详情与纠错在 modal 中打开。详见
> [Console 指南](docs/console.zh.md)。

默认只绑定 loopback 是刻意设计；公网部署必须在服务前增加认证与可信网络边界，
v1 多租户认证仍是非目标。

多人、多 agent 共享同一服务的方式见
[Agent 团队 Hub 拓扑](docs/agent-team.zh.md)。

## 📒 开发账本

Matterhorn 用自身追踪这个项目的开发过程。公开的
[MATTERS.md](MATTERS.md) 每晚由 CI 重新生成；CI 是账本唯一运行 LLM 的地方。
任何人都能从已提交的 `ledger/assertions.json` 在本地复现读侧，无需 LLM key。

写路径把新增 Git commit 与 GitHub 活动转换成经过 gate 的断言，再替换持久断言导出；
读路径从断言重建可丢弃的 SQLite 投影，并确定性渲染人类可读账本。完整设计见
[开发账本说明](docs/ledger.zh.md)。

## 五分钟旅程

```console
pip install 'matterhorn-memory[api]'
mkdir matterhorn-demo && cd matterhorn-demo
mh init
mh add demo-messages.yaml
mh flush demo
mh matters demo
mh console
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

`mh console` 随后打开产品：配置邮箱与 AI，在统一事项墙上处理所有 scope，在 modal
中查看详情/纠错，并针对选定 scope Chat。随包的 Dana Reyes / octo-org 样例不需要
key；真实邮箱同步需要邮箱凭证，真实提取与 Chat 需要配置 AI key。

fixture 只替代演示里的「消息→卡」提取器；处理真实消息时，请在
Console AI 面板、`matterhorn.toml` 或环境变量里配置 OpenAI-compatible /
Anthropic 写侧网关。

## 写侧网关配置

Console 的 AI 面板可在运行时配置 provider。面板输入的 key 只驻留当前进程，
不会写进 TOML，并在该进程内覆盖环境凭证；环境变量仍是非交互启动的 fallback：

| 变量 | 含义 |
| --- | --- |
| `MATTERHORN_PROVIDER` | `openai-compatible` 或 `anthropic` |
| `MATTERHORN_BASE_URL` | provider base URL；OpenAI-compatible 网关必填 |
| `MATTERHORN_MODEL` | provider 模型名 |
| `MATTERHORN_API_KEY` | 首选 provider 凭证；仍支持 provider 原生 key |
| `MATTERHORN_TIMEOUT` | 正浮点请求超时秒数，默认 `60` |
| `MATTERHORN_UNIFIED_LOOP` | 覆盖 `[distill] unified_loop`；section 26 loop 默认 `false` |

可在 `matterhorn.toml` 中用 `[distill] unified_loop = true` 显式启用；环境覆盖
接受 true/false、yes/no、on/off 或 1/0。

## Claude Code

在 Claude Code 项目目录运行：

```console
mh setup claude-code
mh setup claude-code --url http://127.0.0.1:8000
```

第一条向 `.mcp.json` 写入 stdio `matterhorn`；第二条写入指向 hub `/mcp` 的
URL-type entry。两者都会把使用 `mh` 绝对路径的 `SessionStart` /
`SessionEnd` command hooks 合并进 `.claude/settings.json`；hub 模式还会安装
`Stop`，每轮结束即投递，不必等整个会话结束。所有 hook 都 fail-open，setup 写入的
总上限不超过两秒；服务不可用时保持静默。

也可手工配置 `.mcp.json`。嵌入式 stdio：

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

Hub URL type：

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

然后问：**“谁负责支付重构？证据是什么？”** agent 会调用 `list_matters` 和查询
工具。Matterhorn 不让模型组装答案；答案和证据都来自确定性投影。

## 当前 CLI

- 写入与投影：`mh init`、`mh add`、`mh ingest`、`mh extract`、`mh flush`、
  `mh dream`、`mh replay`、`mh correct`、`mh merge`、`mh unmerge`；
- 读取与搬运：`mh matters`、`mh task`、`mh events`、`mh export`、
  `mh import`、`mh sync-status`、`mh query`（`current`、`timeline`、`at`、
  `by-person`、`list`）；
- 运行与集成：`mh console`、`mh serve`、`mh mcp`、`mh mail`（`setup`、
  `sync`、`reset`）、`mh setup`（`claude-code`）、`mh hook`
  （`session-start`、`session-end`、`turn-end`）；
- 检查与验收：`mh schema`（`list`、`show`）、`mh conformance`（`run`）和
  `mh eval`（`run`）。

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
  subjects、active subject merges、证据生命周期与派生事件历史；`mh import`
  只导入空 store，并复现相同投影和查询答案，人工纠错的 `origin=human` 原样保留。

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
        ▲          确定、幂等、可重放（INV-1…INV-13）
        │
高级入口: add_cards(episode_cards)
```

卡以下是 best-effort 提取；从带证据的卡到最终答案，是 Matterhorn 的硬确定性承诺。
一种新输入只有在能无损映射成带溯源 EpisodeCard 时才可准入，绝不为接入便利松动
P5。

同一次 flush 中，引擎绝不把不同 conversation 混进一次提取调用；它以确定顺序处理
conversation unit 和保持完整 boundary 的 chunk，并在每个 chunk 落库后刷新事项
anchor。因此较早 conversation 创建的事项能在同一次 flush 接收后续 conversation
的关联进展，而不会产生重复事项。

## 渐进披露

- `engine.query.current/timeline/at/by_person` 提供双时间轴细节；
- `engine.correct(...)` 追加优先级更高的人工断言，不删除历史；
- `engine.add_cards(...)` 是高级卡级直入口；
- `engine.add_records(...)` 仍可供 provider 集成使用，但不再占 README 首屏；
- `ingest(...)` 是 `add_cards(...)` 的弃用别名。

服务模式提供 `/v1/scopes/{scope_id}` 资源式 REST 和
`/v1/tasks/{task_id}` 持久任务。`mh serve` 与 `mh console` 运行静默期自动 flush
（默认 10 分钟），并以默认 5 分钟 maximum batch delay 保证忙碌 conversation 不会
饿死；可用 `--max-batch-delay-minutes` 或 `MATTERHORN_MAX_BATCH_DELAY` 覆盖。
服务模式也可按 UTC `daily_flush_at = "HH:MM"` 每日 flush，并推送事件 webhook。
嵌入模式仍由宿主调用 `flush()` 或 `wait=True`。

唯一规范是 [spec/SPEC.md](spec/SPEC.md)。100 个语言无关 golden 用例已经覆盖
消息入口、conversation-scoped rolling extraction、boundary chunk 确定性，以及
receipt/flush 幂等重放：

```console
$ mh conformance run
SUMMARY passed=100 failed=0 total=100
```

### 提取质量评测

`mh eval run` 用 [`spec/eval`](spec/eval/README.md) 中的虚构用例测量当前
message-to-matter 提取路径。未设置 `MATTERHORN_PROVIDER` 时会自动使用同名离线
响应 fixture；配置生产 provider 后可记录真实基线。纯文本表格和可选的
`--json report.json` 会报告过切分、误合并、误挂/漏挂、字段准确率、证据有效率和
标题模糊匹配率。分数只是测量值，因此完整执行后即使分数很差也返回 0；数据集、
fixture、gateway 或输出错误仍会失败。

```console
$ mh eval run --provider fixture-file --json eval-report.json
```

## 文档

- [快速开始](docs/getting-started.md)
- [Console 操作面](docs/console.zh.md)
- [IMAP 邮件连接器](docs/mail.zh.md)
- [自托管开发账本](docs/ledger.zh.md)
- [核心概念与承诺边界](docs/core-concepts.md)
- [MCP 与 Claude Code](docs/mcp-claude-code.md)
- [Agent 团队 Hub 拓扑](docs/agent-team.zh.md)
- [人工纠错](docs/corrections.md)
- [事件与 webhook 投递](docs/webhooks.md)
- [Slack / Record 集成](docs/slack.md)
- [Schema 编写](docs/schema-authoring.md)
- [PostgreSQL 部署](docs/postgresql.md)

Matterhorn 要求 Python 3.11+，使用 Apache-2.0 许可证。
