# Matterhorn

面向 agent 的确定性、可溯源时态记忆。写入受封闭 schema 校验，答案由持久化断言
推导，读取路径绝不调用 LLM。

```console
pip install 'matterhorn-memory[api,mcp,postgres]'
```

Claude Code 项目级 `.mcp.json`：

```json
{"mcpServers":{"matterhorn":{"command":"mh","args":["mcp","--db",".matterhorn/memory.db","--schema","org-matters/v1"]}}}
```

十行嵌入式快速开始：

```python
from matterhorn import Engine
engine = Engine("memory.db", "org-matters/v1")
card = {"card_id": "1", "scope_id": "team", "subject_key": "release",
        "date": "2026-07-29", "title": "发布", "status": "open",
        "source_refs": [{"source_id": "m1", "sent_at": "2026-07-29T09:00:00Z",
                         "sender": "ada"}]}
engine.ingest([card])
answer = engine.query.current("team", "release", "status")
print(answer[0].value, answer[0].source_ids)
```

纠错是首屏协议，不是事后清理：

```python
engine.correct({"scope_id": "team", "subject_key": "release",
  "subject_type": "MATTER", "predicate": "status", "object_value": "closed",
  "valid_from": "2026-07-29T09:00:00Z",
  "source_refs": [{"source_id": "human-1", "sent_at": "2026-07-29T10:00:00Z",
                   "sender": "ada", "excerpt": "发布已经关闭。"}]})
```

下一次查询返回人工断言 `closed`，旧断言仍被保留。完整可运行流程见
[examples/correction](examples/correction/README.md)。

## 核心合同

- P1/P4：模型只在写路径抽取；读取和答案完全确定性。
- P5：卡、断言、区间与答案都保留证据。
- P6：业务有效时间与系统记录时间严格分离。
- P7/P9：断言只追加，投影可重建，重放幂等。
- P8：人工纠错是普通但优先级更高的断言。

规范唯一真相源是 [spec/SPEC.md](spec/SPEC.md)。40 个语言无关 golden 用例是
Python 与内部 Java 实现之间的防漂移资产：

```console
$ mh conformance run
SUMMARY passed=40 failed=0 total=40
```

SQLite 嵌入模式：

```python
engine = Engine("team.db", "org-matters/v1")
```

PostgreSQL 服务模式：

```python
engine = Engine("postgresql://matterhorn:secret@primary:5432/matterhorn",
                "org-matters/v1")
```

DSN 必须直连可写主库。副本读取与读写分离会破坏 INV-6；Matterhorn 会在可检测
时快速失败。

## Slack 优先的通信数据接入

Slack history 与 Events API payload 会先映射成与供应商无关的 `Record`。LLM
只参与写侧的 Record → EpisodeCard 抽取；线程身份、写入、投影和所有查询仍然是
确定性的。下面这条命令用真实 fixture 跑完整链路，不需要 Slack token、LLM token
或网络：

```console
.venv/bin/python examples/slack/demo.py
```

处理已下载的 `conversations.history` 页面：

```console
mh extract history.json --adapter slack-history --scope-id team \
  --container-id C0123 --workspace-domain acme.slack.com \
  --db memory.db --provider openai-compatible
mh sync-status team --db memory.db
```

同一个通用 Record 写入面也通过 MCP `add_records` 和 REST
`POST /v1/add_records` 暴露。重叠窗口重放是完全空操作；编辑追加新 observation；
删除保留断言，但查询会把证据显式标为 `revoked`。详见
[Slack 接入指南](docs/slack.md)。

ReMe/OpenViking digest 使用纯确定性适配器；若 payload 没有真实来源元数据，
所有适配器都会明确报错而不是伪造证据。

## 示例与文档

- [Claude Code MCP + Skill](examples/claude-code/README.md)
- [嵌入式 SQLite](examples/embedded/README.md)
- [REST + PostgreSQL](examples/service/README.md)
- [完整人工纠错](examples/correction/README.md)
- [离线 Slack 接入](examples/slack/README.md)
- [快速开始](docs/getting-started.md)
- [核心概念](docs/core-concepts.md)
- [Schema 编写](docs/schema-authoring.md)
- [MCP 与 Claude Code](docs/mcp-claude-code.md)
- [纠错指南](docs/corrections.md)
- [Slack 接入](docs/slack.md)
- [适配器](docs/adapters.md)
- [与 L1 工具的关系](docs/positioning.md)

开发：

```console
.venv/bin/python -m pytest -q
docker compose -f compose.postgres.yml up --build --abort-on-container-exit \
  --exit-code-from conformance
```

Matterhorn 要求 Python 3.11+，使用 Apache-2.0 许可证。
