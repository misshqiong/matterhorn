# Matterhorn 验收手册

这份手册让你**亲手验证**引擎是否兑现了立项承诺，而不是读代码或相信测试名字。

每个步骤都给出：**命令** → **期望输出** → **看到别的说明什么坏了**。
本文所有命令都在 macOS / Python 3.12 上实跑验证过。

预计耗时：核心验收 10 分钟，全量（含 PostgreSQL）30 分钟。

---

## 0. 准备

```bash
cd matterhorn
python3.12 -m venv .venv
./.venv/bin/pip install -e '.[api,mcp,postgres,dev]'
```

> 环境已经建好的话跳过即可。**但如果你想从零验证**，建议 `rm -rf .venv` 重建一次——
> 我在验收中发现过 venv 被污染的情况（详见 §8 已知问题记录）。

确认依赖是真包，不是替身：

```bash
./.venv/bin/pip list | grep -iE "^(typer|click|mcp|fastapi|psycopg) "
```

期望看到 `typer 0.27.0` 这类**正常版本号**。若出现 `0.0.0+verification` 之类的版本，
说明 venv 被污染了，删掉重建。

---

## 1. 冒烟（1 分钟）

```bash
./.venv/bin/python -m pytest -q
```

**期望**：`104 passed, 37 skipped`。
37 个 skip 是 PostgreSQL 用例——没设 DSN 时跳过，§6 会把它们跑起来。

```bash
./.venv/bin/mh conformance run
```

**期望**：最后一行 `SUMMARY passed=37 failed=0 total=37`。

---

## 2. 验证「规范是真门禁」，不是摆设

这是整个项目的地基：规范以语言无关的 golden YAML 存在，两个实现（本 Python 引擎 +
内部 Java 参照实现）跑同一套用例。**先验证这个门禁真的会拦人**：

```bash
# 造一个被破坏的用例，看它是否被抓住
D=$(mktemp -d); cp spec/conformance/01-basic-current.yaml $D/
sed -i '' 's/object_value: open/object_value: SABOTAGED/' $D/01-basic-current.yaml
./.venv/bin/mh conformance run --suite $D; echo "exit=$?"
```

**期望**：`SUMMARY passed=0 failed=1`，`exit=1`，并打印出实际值与期望值的详细 diff。

```bash
./.venv/bin/mh conformance run --suite /nonexistent; echo "exit=$?"
```

**期望**：`exit=2`。

> **退出码契约**：`0` 全过 / `1` 用例跑了但有失败 / `2` 用例集本身不可用。
> 这三档必须分得开——CI 才能区分「代码坏了」和「测试没跑起来」。
> 如果三种情况都退 0，这个门禁是假的。

---

## 3. 逐条验证不变量（核心验收）

这一节是手册的重点。**九条原则和十条不变量都是从生产 bug 里提炼的**，
下面每条都能被你亲眼看到。

### 3.1 INV-4：字段缺失 ≠ 显式清除（blocker/next_step/due 闪断 bug）

生产事故：某个字段在第二天的卡里没提，系统就当它被清除了，第三天又出现时状态断了一次。

```bash
D=$(mktemp -d)
cat > $D/c1.yaml <<'YAML'
- {card_id: d1, scope_id: team-a, date: 2026-02-01, title: Ship billing v2, status: open,
   blocker: waiting on legal,
   source_refs: [{source_id: msg-1, sent_at: "2026-02-01T09:00:00Z", sender: u1}]}
YAML
cat > $D/c2.yaml <<'YAML'
- {card_id: d2, scope_id: team-a, date: 2026-02-02, title: Ship billing v2, status: open,
   source_refs: [{source_id: msg-2, sent_at: "2026-02-02T09:00:00Z", sender: u1}]}
YAML
cat > $D/c3.yaml <<'YAML'
- {card_id: d3, scope_id: team-a, date: 2026-02-03, title: Ship billing v2, status: open,
   blocker: waiting on legal,
   source_refs: [{source_id: msg-3, sent_at: "2026-02-03T09:00:00Z", sender: u1}]}
YAML
for f in c1 c2 c3; do ./.venv/bin/mh ingest --db $D/t.db $D/$f.yaml >/dev/null; done

SK=$(./.venv/bin/mh query list --db $D/t.db team-a \
  | ./.venv/bin/python -c 'import sys,json;print(json.load(sys.stdin)[0]["subject_key"])')

./.venv/bin/mh query timeline --db $D/t.db team-a $SK blocked_by \
  | ./.venv/bin/python -c 'import sys,json
for r in json.load(sys.stdin): print(r["valid_from"],"->",r["valid_to"],"|",r["value"],"| sources:",r["source_ids"])'
```

**期望**：**恰好一行**

```
2026-02-01T00:00:00.000000Z -> None | waiting on legal | sources: ['msg-1', 'msg-3']
```

**这行里有三个验收点**：

1. **只有一条区间** —— 中间那张没提 blocker 的卡没有造成断裂。若看到两条区间（`02-01 → 02-02` 和 `02-03 → None`），INV-4 就是坏的。
2. **`valid_to` 是 `None`** —— 开区间表示「当前仍然有效」。
3. **`sources` 有 `msg-1` 和 `msg-3` 两条** —— 第三张卡的再确认证据被累积进来了。若只有 `msg-1`，说明「凭什么现在还是这个值」的证据丢了（违反 P5）。

想看显式清除仍然生效（守卫不是简单地关掉了）：

```bash
cat > $D/c4.yaml <<'YAML'
- {card_id: d4, scope_id: team-a, date: 2026-02-04, title: Ship billing v2, status: open,
   cleared_fields: [blocker],
   source_refs: [{source_id: msg-4, sent_at: "2026-02-04T09:00:00Z", sender: u1}]}
YAML
./.venv/bin/mh ingest --db $D/t.db $D/c4.yaml >/dev/null
./.venv/bin/mh query current --db $D/t.db team-a $SK blocked_by
```

**期望**：`[]`（区间已闭合）。
**关键点**：清除必须由 `cleared_fields` 显式表达，而不是靠「字段没出现」推断。

### 3.2 INV-5：单条共享消息不构成合并理由（两件事合成一张卡 bug）

```bash
D=$(mktemp -d)
cat > $D/m.yaml <<'YAML'
- {card_id: a1, scope_id: s, date: 2026-03-01, title: Website launch, status: open,
   source_refs: [{source_id: shared-msg, sent_at: "2026-03-01T08:00:00Z", sender: u1}]}
- {card_id: a2, scope_id: s, date: 2026-03-02, title: Office lease, status: pending,
   source_refs: [{source_id: shared-msg, sent_at: "2026-03-01T08:00:00Z", sender: u1}]}
YAML
./.venv/bin/mh ingest --db $D/t.db $D/m.yaml >/dev/null
./.venv/bin/mh query list --db $D/t.db s \
  | ./.venv/bin/python -c 'import sys,json
d=json.load(sys.stdin); print("subjects =",len(d)); [print(" -",x["title"]) for x in d]'
```

**期望**：

```
subjects = 2
 - Office lease
 - Website launch
```

**若输出 `subjects = 1`，INV-5 就是坏的**——「网站上线」和「办公室租约」两件不相干的事
仅仅因为共享一条消息就被合成一件了。这正是需求书记录的那个生产 bug。

> 注意这两张卡各自**只有一条来源**且完全共享。按 `min_shared_sources: 2 / or_share_ratio: 0.5`
> 的字面 OR 语义，共享比例是 1.0 ≥ 0.5，会合并。引擎实现的是带绝对下限的版本：
> `共享数 >= 2 且 (共享数 >= min_shared_sources 或 比例 >= or_share_ratio)`。
> **这是我对需求书的一处判读**，详见 §7。

### 3.3 INV-9 + P8：人是记忆的第二写入者

```bash
D=$(mktemp -d)
cat > $D/c.yaml <<'YAML'
- {card_id: d1, scope_id: team-a, date: 2026-02-01, title: Ship billing v2, status: blocked,
   source_refs: [{source_id: msg-1, sent_at: "2026-02-01T09:00:00Z", sender: u1}]}
YAML
./.venv/bin/mh ingest --db $D/t.db $D/c.yaml >/dev/null
SK=$(./.venv/bin/mh query list --db $D/t.db team-a \
  | ./.venv/bin/python -c 'import sys,json;print(json.load(sys.stdin)[0]["subject_key"])')

echo -n "纠错前: "; ./.venv/bin/mh query current --db $D/t.db team-a $SK status \
  | ./.venv/bin/python -c 'import sys,json;r=json.load(sys.stdin)[0];print(r["value"],r["origin"])'

./.venv/bin/mh correct --db $D/t.db --scope-id team-a --subject-key $SK --subject-type MATTER \
  --predicate status --object-value open --valid-from 2026-02-02T00:00:00Z \
  --source-ref '{source_id: human-1, sent_at: "2026-02-02T10:00:00Z", sender: pm}' >/dev/null

echo -n "纠错后: "; ./.venv/bin/mh query current --db $D/t.db team-a $SK status \
  | ./.venv/bin/python -c 'import sys,json;r=json.load(sys.stdin)[0];print(r["value"],r["origin"],r["source_ids"])'
```

**期望**：

```
纠错前: blocked model
纠错后: open human ['human-1']
```

**验收点**：人工断言走的是**同一套投影**（不是旁路的覆盖表），且 `origin` 字段让你
永远分得清哪条结论是人写的、哪条是模型写的。

### 3.4 INV-10 + P4：答案零模型

这是本项目区别于 mem0/ReMe 的核心主张——**答案由查询推导，不由模型生成**。
验证方式是装一个「一被调用就爆炸」的 LLM 网关，然后跑遍所有查询：

```bash
cat > /tmp/inv10.py <<'PY'
from matterhorn import Engine, EpisodeCard
from datetime import datetime, timezone
import tempfile, os

class Exploding:
    def complete(self, **k):
        raise AssertionError("读路径调用了模型 —— INV-10 被违反")

db = os.path.join(tempfile.mkdtemp(), "t.db")
e = Engine(store=db, schema="org-matters/v1", llm=Exploding())
e.ingest([EpisodeCard(card_id="a", scope_id="s", date="2026-06-01", title="Thing",
    status="open",
    source_refs=[{"source_id":"m1","sent_at":"2026-06-01T09:00:00Z","sender":"u1"}])])
sk = e.query.list_matters("s")[0].subject_key

e.query.current("s", sk, "status")
e.query.timeline("s", sk, "status")
e.query.at("s", sk, "status", datetime(2026, 6, 1, 12, tzinfo=timezone.utc))
e.query.by_person("s", "u1")
e.query.list_matters("s")
e.query.completion("s")
print("INV-10 通过：六类查询在爆炸网关下全部跑通")
PY
./.venv/bin/python /tmp/inv10.py
```

**期望**：`INV-10 通过：六类查询在爆炸网关下全部跑通`
**若抛出 AssertionError**，说明某条读路径偷偷调用了模型。

结构性防守（比运行时测试更强）：

```bash
./.venv/bin/python -m pytest -q tests/test_protocols.py -k import_path 2>&1 | tail -2
```

**期望**：通过。这条测试做的是**导入图分析**——查询包在静态层面就够不到 `distill` 包。

### 3.5 INV-2 / INV-3 / P9：幂等重放与投影可重建

```bash
D=$(mktemp -d)
cat > $D/c.yaml <<'YAML'
- {card_id: d1, scope_id: s, date: 2026-02-01, title: T, status: open,
   source_refs: [{source_id: m1, sent_at: "2026-02-01T09:00:00Z", sender: u1}]}
YAML
./.venv/bin/mh ingest --db $D/t.db $D/c.yaml | grep assertions_emitted
./.venv/bin/mh ingest --db $D/t.db $D/c.yaml | grep assertions_emitted   # 同一批再喂一次
./.venv/bin/mh replay --db $D/t.db s | tail -3
```

**期望**：第一次 `"assertions_emitted": 1`，**第二次 `0`**（幂等），`replay` 报 `"status": "rebuilt"`。

> 这两条不用你单独验也行——**conformance runner 对 37 个用例中的每一个都会自动
> 额外跑两遍**：重复 ingest 断言状态不变、replay 重建后区间集完全相等。
> 也就是说 INV-2/INV-3 是被 37 次而不是 1 次守住的。

### 3.6 INV-1 / INV-7：封闭谓词与溯源必备

```bash
./.venv/bin/python - <<'PY'
from matterhorn.contracts import EpisodeCard
try:
    EpisodeCard(card_id="x", scope_id="s", date="2026-06-01", title="t", source_refs=[])
    print("INV-7 失败：无溯源的卡被接受了")
except Exception:
    print("INV-7 通过：无 source_refs 的卡被拒绝")
PY
```

**期望**：`INV-7 通过`。

未注册谓词（INV-1）：上面 3.4 的脚本里把 `"status"` 换成 `"not_a_predicate"`，
应抛 `ValueError: unregistered predicate: not_a_predicate`。

---

## 4. 验证蒸馏层：模型受关卡管辖（P1 / P2 / P3）

这是「LLM 圈禁在写路径」的验收。**不需要真实 API key**——用假网关即可。

```bash
cat > /tmp/gate.py <<'PY'
import json, re, tempfile, os
from matterhorn import Engine, EpisodeCard

class FakeGateway:
    """一个不老实的模型：一条合法候选 + 三条应该被拒的。"""
    def __init__(self): self.calls = 0
    def complete(self, *, system, user, response_schema):
        self.calls += 1
        parent = re.search(r'(sub_[0-9a-f]+)', user).group(1)
        return json.dumps({"candidates": [
          # 合法：在已有 MATTER 下开一个 DECISION_SLOT
          {"parent_subject_key": parent, "subject_title": "Which vendor for billing",
           "subject_type": "DECISION_SLOT", "predicate": "decision_adopted",
           "operation": "ASSERT", "object_value": True,
           "valid_from": "2026-05-01T00:00:00Z",
           "source_ids": ["msg-1"], "confidence": 0.95},
          # 捏造证据：引用了原卡里不存在的消息
          {"parent_subject_key": parent, "subject_title": "Fabricated",
           "subject_type": "DECISION_SLOT", "predicate": "decision_adopted",
           "operation": "ASSERT", "object_value": True,
           "valid_from": "2026-05-01T00:00:00Z",
           "source_ids": ["msg-DOES-NOT-EXIST"], "confidence": 0.99},
          # 越界：试图改写确定性谓词
          {"parent_subject_key": parent, "subject_title": "x",
           "subject_type": "MATTER", "predicate": "status",
           "operation": "ASSERT", "object_value": "done",
           "valid_from": "2026-05-01T00:00:00Z",
           "source_ids": ["msg-1"], "confidence": 0.99},
          # 孤儿：父主语不存在
          {"parent_subject_key": "sub_NOPE", "subject_title": "orphan",
           "subject_type": "DECISION_SLOT", "predicate": "decision_adopted",
           "operation": "ASSERT", "object_value": True,
           "valid_from": "2026-05-01T00:00:00Z",
           "source_ids": ["msg-1"], "confidence": 0.95},
        ]})

gw = FakeGateway()
db = os.path.join(tempfile.mkdtemp(), "d.db")
e = Engine(store=db, schema="org-matters/v1", llm=gw)
e.ingest([EpisodeCard(card_id="k1", scope_id="s", date="2026-05-01",
    title="Billing vendor selection", status="open",
    source_refs=[{"source_id":"msg-1","sent_at":"2026-05-01T09:00:00Z","sender":"u1"}])])

print("① ingest 期间模型被调用次数 =", gw.calls, "（必须是 0）")

r = e.dream("s")
print("② dream:", r.model_dump())
print("③ 关卡账本:", e.gate_statistics("s"))

sk = e.query.list_matters("s")[0].subject_key
print("④ 确定性 status 未被模型污染:",
      [(v.value, v.origin) for v in e.query.current("s", sk, "status")])

r2 = e.dream("s")
print("⑤ 再次 dream 是否为空操作: new_assertions =", r2.new_assertions)
PY
./.venv/bin/python /tmp/gate.py
```

**期望**（逐条对照）：

```
① ingest 期间模型被调用次数 = 0                       ← P1：写入路径不同步调模型
② dream: {... 'accepted_candidates': 1, 'rejected_candidates': 3,
           'new_assertions': 1, 'new_subjects': 1 ...}
③ 关卡账本: accepted=1 rejections={'SOURCE_NOT_TRACEABLE': 1,
           'NOT_SEMANTIC': 1, 'UNKNOWN_PARENT_SUBJECT': 1}
④ 确定性 status 未被模型污染: [('open', 'model')]      ← 模型改不动确定性谓词
⑤ 再次 dream 是否为空操作: new_assertions = 0          ← P9
```

**四个验收点**：

- **`SOURCE_NOT_TRACEABLE`**：模型引用的 source_id 必须是原卡证据的**子集**。
  这是整个关卡里最要紧的一条——它保证「每条结论可回指原始消息」不是口号。
  同一份实现也被 message→card 提取器复用，不是两套。
- **`NOT_SEMANTIC`**：只有 schema 里标了 `extraction: semantic` 的谓词才交给模型。
  模型碰不到 `status` 这类确定性谓词。
- **`UNKNOWN_PARENT_SUBJECT`**：模型可以提议开子主语，但父主语必须真实存在。
- **拒绝不会中断批次**：一条候选被拒不影响其他候选，且拒绝理由被计数留痕
  （运维能看到模型被拒的比例，而不是静默丢弃）。

---

## 5. 协议面验收

### 5.1 REST

```bash
D=$(mktemp -d)
./.venv/bin/mh serve --db $D/s.db --host 127.0.0.1 --port 8899 &
sleep 4
curl -s http://127.0.0.1:8899/healthz; echo
curl -s -X POST http://127.0.0.1:8899/v1/add_episode_cards -H 'content-type: application/json' \
 -d '{"cards":[{"card_id":"s1","scope_id":"demo","date":"2026-02-01","title":"Pick vendor",
      "status":"open","source_refs":[{"source_id":"m1","sent_at":"2026-02-01T09:00:00Z","sender":"u1"}]}]}'; echo
curl -s -X POST http://127.0.0.1:8899/v1/list_matters -H 'content-type: application/json' \
 -d '{"scope_id":"demo"}'; echo
curl -s -o /dev/null -w "docs=%{http_code}\n" http://127.0.0.1:8899/docs
pkill -f "mh serve"
```

**期望**（实跑输出）：

```
{"status":"ok"}
{"cards":1,"assertions_emitted":1,"assertion_ids":["d8cacac7..."]}
[{"subject_key":"sub_e7db73bc...","subject_type":"MATTER","title":"Pick vendor","current":{"status":"open"}}]
docs=200
```

`/docs` 是交互式 OpenAPI 页面，浏览器打开 <http://127.0.0.1:8899/docs> 可以逐个试端点。

### 5.2 MCP（Claude Code 主通道）

```bash
./.venv/bin/python - <<'PY'
import asyncio, os, tempfile, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
db = os.path.join(tempfile.mkdtemp(), "mcp.db")
async def main():
    p = StdioServerParameters(command=".venv/bin/python", args=["-m","matterhorn.mcp"],
        env={**os.environ, "MATTERHORN_DB": db, "MATTERHORN_SCHEMA": "org-matters/v1"})
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", sorted(t.name for t in (await s.list_tools()).tools))
asyncio.run(main())
PY
```

**期望**：

```
tools: ['add_episode_cards', 'correct', 'list_matters', 'query_at',
        'query_by_person', 'query_current', 'query_timeline']
```

**验收点**：这是**真的 MCP 协议往返**（官方 SDK 客户端 + stdio），不是内存里调函数。

接到 Claude Code：见 [examples/claude-code/](../examples/claude-code/)，
里面有可直接用的 `.mcp.json` 和随附的 Skill。

### 5.3 SDK 嵌入模式

```bash
./.venv/bin/python examples/embedded/demo.py
./.venv/bin/python examples/correction/demo.py
```

**期望**：后者输出 `before=blocked origin=model` → `after=open origin=human`。

---

## 6. 双后端验收（PostgreSQL）

**这一步是最容易被跳过、也最容易藏 bug 的一步。**
第二后端存在的意义就是证明规范能被实现两次——所以整套 conformance 必须在两个后端上跑。

```bash
# 起一个用完即弃的实例（不碰你已有的任何 PG 配置）
export PATH="${PG_BIN:-$(pg_config --bindir 2>/dev/null)}:$PATH" LC_ALL=C LANG=C
PGDIR=$(mktemp -d)/pgdata && mkdir -p $PGDIR
initdb -D $PGDIR -U matterhorn --auth=trust --locale=C --encoding=UTF8 >/dev/null
pg_ctl -D $PGDIR -o "-p 55432 -k /tmp" -l $PGDIR/server.log start
createdb -h 127.0.0.1 -p 55432 -U matterhorn matterhorn

# 整套测试跑双后端
MATTERHORN_TEST_POSTGRES_DSN="postgresql://matterhorn@127.0.0.1:55432/matterhorn" \
  ./.venv/bin/python -m pytest -q

# conformance 单独在 PG 上跑
./.venv/bin/mh conformance run --backend postgres \
  --dsn "postgresql://matterhorn@127.0.0.1:55432/matterhorn"
```

**期望**：`141 passed`（无 skip），`SUMMARY passed=37 failed=0 total=37`。

若只跑到 `104 passed, 37 skipped`，说明 DSN 没生效，PG 根本没被验证。

**进阶：验证排序不依赖数据库 locale**（一个答案取决于部署 locale 的规范不算规范）：

```bash
psql -h 127.0.0.1 -p 55432 -U matterhorn -d postgres \
  -c "CREATE DATABASE mh_collate TEMPLATE template0 ENCODING 'UTF8' \
      LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8';"
MATTERHORN_TEST_POSTGRES_DSN="postgresql://matterhorn@127.0.0.1:55432/mh_collate" \
  ./.venv/bin/python -m pytest -q
```

**期望**：同样 `141 passed`。换了 collation 结果一模一样。

用完清理：

```bash
pg_ctl -D $PGDIR stop && rm -rf $PGDIR
```

---

## 7. 我对需求书做的判读（请你复核）

这些地方需求书留白或自相矛盾，我做了决定。**它们改变了语义，值得你确认**：

| # | 判读 | 理由 |
| --- | --- | --- |
| 1 | **命名 `matterhorn`**，包名/CLI 同名，命令 `mh` | 三个候选里的推荐项 |
| 2 | **INV-5 合并公式加了绝对下限 2**：`共享数 >= 2 且 (共享数 >= min_shared 或 比例 >= ratio)` | 需求书 SchemaProfile 写的是 OR 语义，字面执行会让单来源卡合并；INV-5 正文明写「单条共享消息不构成合并理由」。我判定**正文优先于配置示例**。副作用：`or_share_ratio` 只在 `min_shared_sources > 2` 时才有意义 |
| 3 | **EpisodeCard 加 `cleared_fields`** | INV-4 要求区分「本卡未观察」与「显式清除」，原合同里没有任何字段能表达后者 |
| 4 | **EpisodeCard 加 `subject_key`** | 身份覆盖，让接入方能显式指定主语 |
| 5 | **Interval 加 `supporting_assertion_ids`** | 同值再确认时区间不断开是对的，但原设计只保留开区间那条断言的证据，用户问「凭什么现在还是 open」只能看到第一条消息，与 P5 冲突 |
| 6 | **`schemas/` 放进包内**（`src/matterhorn/schemas/`）而非仓库根 | 需求书 §12 目录图是根级；但放根目录装成 wheel 后 `importlib.resources` 找不到，pip 安装即坏 |
| 7 | **子主语由模型提议 + 关卡放行创建** | 需求书把 `DECISION_SLOT` 设为 `MATTER` 的子类型，却没说它怎么产生；不补这条路径，旗舰 profile 唯一的语义谓词 `decision_adopted` 永远无法落库 |
| 8 | **排序强制字节序**（PG `COLLATE "C"` / SQLite `BINARY`） | INV-8 的稳定序不能取决于部署时的数据库 locale |

---

## 8. 已知缺口与验收过程记录

**尚未完成**（M3 出口标准的一部分）：

- **未推送 PyPI / Docker registry**：包已通过 `twine check`，Dockerfile 和 CI 都在，
  但对外发布是不可逆动作，等你发话。
- **未接第一个外部宿主**：需求书 M3 要求「一个外部宿主跑通」，
  适配器（ReMe / OpenViking / 通用消息流）已实现并有 fixture 测试，但没有真实宿主接入过。
- **CI 未在 GitHub 上实跑**：`.github/workflows/ci.yml` 包含 lint、
  Python 3.11/3.12/3.13 矩阵、以及 postgres:17-alpine 的跨后端 conformance 门禁，
  但仓库还没有远端，工作流从未被 GitHub 执行过。**PG 部分我在本地实跑验证了**（§6）。

**验收过程中发现并已修复的问题**（共 14 项，这里只列会影响你判断的）：

1. **INV-5 曾被真实违反**——两件不相干的事共享一条消息就合并了。
   它自带的 conformance 用例恰好用了 3 条来源（比例 0.33）绕开了这个洞。
2. **旗舰 profile 的语义谓词曾完全不可达**——`DECISION_SLOT` 只出现在 YAML 里，
   没有任何代码、测试、用例碰过它。
3. **MCP 曾会静默降级成山寨实现**——官方 SDK 装不上时 fallback 到手写仿制品，
   用户会以为在跑 MCP server 其实没有客户端连得上。
4. **PostgreSQL 后端曾装上就炸**——读路径把 SQLite 的 `?` 占位符写死在 store 连接上，
   35 个用例 19 个失败。这条是装了本地 PG 才挖出来的。
5. **venv 里曾被塞进一个伪造的 `typer`**——103 行手写 shim，
   dist-info 版本 `0.0.0+verification`。所有 CLI 测试都在对着这个替身跑。
   已删除并用真依赖重建，暴露出一个真实缺陷（`mh conformance run` 的退出码契约）。

> 第 5 条是为什么 §0 建议你 `rm -rf .venv` 重建一次，
> 以及为什么 §0 让你 `pip list` 看一眼版本号是否正常。

---

## 9. 一句话验收标准

如果你只想跑一条命令：

```bash
export PATH="${PG_BIN:-$(pg_config --bindir 2>/dev/null)}:$PATH" LC_ALL=C LANG=C
PGDIR=$(mktemp -d)/pgdata && mkdir -p $PGDIR \
  && initdb -D $PGDIR -U matterhorn --auth=trust --locale=C --encoding=UTF8 >/dev/null \
  && pg_ctl -D $PGDIR -o "-p 55432 -k /tmp" -l $PGDIR/server.log start >/dev/null \
  && createdb -h 127.0.0.1 -p 55432 -U matterhorn matterhorn \
  && MATTERHORN_TEST_POSTGRES_DSN="postgresql://matterhorn@127.0.0.1:55432/matterhorn" \
     ./.venv/bin/python -m pytest -q \
  ; pg_ctl -D $PGDIR stop >/dev/null && rm -rf $PGDIR
```

**通过标准**：`141 passed`，零失败，零 skip。
