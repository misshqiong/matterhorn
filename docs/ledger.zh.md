# 自托管开发账本

Matterhorn 用自身追踪项目开发，并把结果作为公开、自托管账本。夜间写侧会使用 LLM；
所有公开读取与本地复现都是确定性的，不需要模型或凭证。

## 写路径

GitHub Actions 的 `Development ledger` workflow 每晚运行，也支持手动触发：

1. checkout 完整 Git 历史；任何必需的写侧网关 secret 缺失时，在 fill 前明确失败；
2. 删除可丢弃的 `ledger/dev.db`；若已有提交的 `ledger/assertions.json` ownership
   envelope，则先导入它；
3. `scripts/ledger_fill.py` 把 Git commit、受追踪的 devlog、GitHub issue 与 comment
   映射为 Record。默认每次提取最多发送八条 Record，绝不拆开同一 thread；超过八条
   的 thread 独占一个超大 batch；
4. prompt 把证据显示为 `m1`、`m2` 等短别名。提取器在不变的溯源 gate 之前，把已知
   别名映射回真实 provider ID；未知别名保持未知，并以
   `SOURCE_NOT_TRACEABLE` 拒绝；
5. 接受的卡与断言完成投影后，`mh export` 替换持久 JSON envelope 与确定性的
   `MATTERS.md`；
6. 仅当 `ledger/` 或 `MATTERS.md` 有变化时 CI 才提交。没有新增 source ID 时，
   导入的 source lifecycle 就是账本 checkpoint，不调用 LLM，连续第二次运行无 diff。

SQLite 数据库被 gitignore。`ledger/assertions.json` 内的断言、subject、source
lifecycle 与事件历史才是持久状态。它遵循规范规定的单 JSON 文档 scope export；
本项目不使用 JSONL 变体。

## 读路径

读路径导入 ownership envelope，重建 interval 与 MemoryCard，然后运行
`mh matters` 或 `mh export --format markdown`；它不会加载 gateway。Markdown
排序稳定，不含生成时间。每个 interval 的证据在有 URI 时渲染为链接；没有 URI 时
显示裸 source ID。由 `origin=human` 断言打开的 interval 会显示醒目的
**[human correction]** 徽标。

`MATTERS.md` 为每个事项展示标题、状态、负责人、阻塞、下一步、截止时间，以及可折叠
的带证据 interval 时间线。

## 公开不变量证明什么

| 公开性质 | 它证明的内容 |
| --- | --- |
| 已提交的 ownership envelope | 断言、人工纠错、证据生命周期与事件是可携带的项目资产，而非托管服务依赖。 |
| 重建数据库 | interval 与 MemoryCard 是可丢弃的纯投影，不是第二事实源。 |
| 稳定的 Markdown 字节 | 相同 store 状态生成相同公开账本，不含墙钟值，也不使用读侧模型。 |
| 证据链接 | 每次显示的变化都能追溯到 commit、issue、comment、文档或裸 provider source ID。 |
| 人工徽标 | 纠错进入普通断言时间线，同时与模型输出公开区分。 |
| 第二次运行无 diff | 已导出的 source identity 不会被重新解释，夜间闭环的幂等性可观察。 |

## 无 LLM key 复现

```console
git clone https://github.com/misshqiong/matterhorn
cd matterhorn
pip install -e .
mh import ledger/assertions.json --db ledger/dev.db
mh matters dev --db ledger/dev.db
```

如需复现渲染文件：

```console
mh export dev --format markdown --out MATTERS.md --db ledger/dev.db
```

## 用 Ollama 在本地跑一次 fill

启动 Ollama 的 OpenAI-compatible endpoint，并选择已安装的本地模型：

```console
export MATTERHORN_PROVIDER=openai-compatible
export MATTERHORN_BASE_URL=http://localhost:11434/v1
export MATTERHORN_MODEL=qwen3:4b
export MATTERHORN_API_KEY=ollama
export MATTERHORN_TIMEOUT=600
python scripts/ledger_fill.py --db ledger/dev.db --batch-size 8
```

`MATTERHORN_TIMEOUT` 接受正浮点秒数，默认 `60`。小型本地模型可能产生粗糙账本或
gate 拒绝，应如实报告。CI 级模型从已提交断言重建，并幂等替换同一导出与渲染目标。
