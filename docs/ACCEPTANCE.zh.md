# Matterhorn 验收手册

这份手册让你**亲手验证**引擎是否兑现了立项承诺，而不是读代码或相信测试名字。

每个步骤都给出：**命令** → **期望输出** → **看到别的说明什么坏了**。
本文所有命令都在 macOS / Python 3.12 上实跑验证过。M6 的 SQLite 命令于
2026-07-29 再次实跑；当前沙箱无法连接 PostgreSQL，所以 §6 保留为宿主机验收
步骤，不把它记作本轮观察结果。

预计耗时：核心验收 10 分钟，全量（含 PostgreSQL）30 分钟。

---

## 0. 准备

```bash
git clone https://github.com/misshqiong/matterhorn.git
cd matterhorn
python3.12 -m venv .venv
./.venv/bin/pip install -e '.[api,mcp,postgres,dev]'
```

本文后续所有命令都假定你在**仓库根目录**下执行。

> 环境已经建好的话跳过即可。**但如果你想从零验证**，建议 `rm -rf .venv` 重建一次——
> 我在验收中发现过 venv 被污染的情况（详见 §8 已知问题记录）。

确认依赖是真包，不是替身：

```bash
./.venv/bin/pip list | grep -iE "^(typer|click|mcp|fastapi|psycopg) "
```

期望看到 `typer 0.27.0` 这类**正常版本号**。若出现 `0.0.0+verification` 之类的版本，
说明 venv 被污染了，删掉重建。

---

## 五分钟旅程

下面严格模拟一个从临时空目录开始的新用户；使用 `mh init` 生成的本地 fixture
网关，不访问网络：

```bash
D=$(mktemp -d /tmp/matterhorn-m5-journey.XXXXXX)
cd "$D"
/absolute/path/to/.venv/bin/mh init
/absolute/path/to/.venv/bin/mh add demo-messages.yaml
/absolute/path/to/.venv/bin/mh flush demo
/absolute/path/to/.venv/bin/mh matters demo
```

**2026-07-29 本次实跑输出**：

```text
Initialized matterhorn.toml and matterhorn.db
Next:
  mh add demo-messages.yaml
  mh flush demo
  mh matters demo
{
  "accepted": 1,
  "task_id": "task_3a45070d9359d93df6cdba20ac2e8f96e637c637a24b7bb845ee0943d8f80a33"
}
{
  "scope_id": "demo",
  "tasks_processed": 1,
  "task_ids": [
    "task_3a45070d9359d93df6cdba20ac2e8f96e637c637a24b7bb845ee0943d8f80a33"
  ],
  "remaining": 0
}
[
  {
    "title": "Payment refactor",
    "status": "in_progress",
    "owners": ["u1"],
    "participants": ["u1"],
    "blocked_by": [],
    "next_step": "Integration testing",
    "due": null,
    "subject_key": "sub_4ad6c81d95364b1dc371"
  }
]
```

验收点：

1. `add` 只返回 receipt，事项在 `flush` 前不会凭空出现；
2. `matterhorn.toml` 让后续命令不再重复 `--db/--schema`；
3. `matters` 的 owner/status/next_step 来自确定性投影；
4. 在当前目录运行 `mh setup claude-code` 接入 embedded stdio（或加
   `--url http://127.0.0.1:8000` 接入 hub）后，问“谁负责支付重构？”，agent
   应先用 `list_matters`，答案为 `u1`，而不是让模型重新生成一份事实。

---

## 1. 冒烟（1 分钟）

```bash
./.venv/bin/python -m pytest -q
```

**2026-07-31 本次实跑**：`254 passed, 48 skipped, 7 warnings`。48 个 skip
包括 47 个 PostgreSQL conformance 用例和 1 个 PostgreSQL Console/store parity
用例——没设 DSN 时跳过，§6 会把它们跑起来。7 个 warning 全部来自旧
`ChatMessage` 兼容测试触发的预期弃用提示。

窄终端也实际跑了整套，而不只是看 help：

```bash
env COLUMNS=60 LINES=24 NO_COLOR=1 .venv/bin/python -m pytest -q
```

真实汇总同样是 `254 passed, 48 skipped, 7 warnings`。额外的 32 列 stress
运行当前会暴露一个 layout-sensitive 测试断言：Rich 把 `--account` 在两个连字符
之间换行，`tests/test_mail.py::test_mail_cli_appends_and_requires_selection_when_ambiguous`
因此失败；CLI 本身仍按契约退出 2 并显示完整错误。这不是双后端通过证据。

```bash
./.venv/bin/mh conformance run
```

**期望**：最后一行 `SUMMARY passed=54 failed=0 total=54`。

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

这一节是手册的重点。**九条原则和十一条不变量都是从生产 bug 里提炼的**，
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

class EmptySemantic:
    def complete(self, **k):
        return '{"candidates":[]}'

db = os.path.join(tempfile.mkdtemp(), "t.db")
e = Engine(store=db, schema="org-matters/v1", llm=EmptySemantic())
e.add_cards([EpisodeCard(card_id="a", scope_id="s", date="2026-06-01", title="Thing",
    status="open",
    source_refs=[{"source_id":"m1","sent_at":"2026-06-01T09:00:00Z","sender":"u1"}])],
    wait=True)
e._write_gateway = Exploding()  # 验收探针：此后任何网关访问都会失败
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

> 这两条不用你单独验也行——**conformance runner 对 47 个用例中的每一个都会自动
> 额外跑两遍**：重复 ingest 断言状态不变、replay 重建后区间集完全相等。
> 也就是说 INV-2/INV-3 是被 47 次而不是 1 次守住的。

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
receipt = e.add_cards([EpisodeCard(card_id="k1", scope_id="s", date="2026-05-01",
    title="Billing vendor selection", status="open",
    source_refs=[{"source_id":"msg-1","sent_at":"2026-05-01T09:00:00Z","sender":"u1"}])])

print("① add_cards 期间模型被调用次数 =", gw.calls, "（必须是 0）")

e.flush("s")
print("② task:", e.task(receipt.task_id).model_dump())
print("③ 关卡账本:", e.gate_statistics("s"))

sk = e.query.list_matters("s")[0].subject_key
print("④ 确定性 status 未被模型污染:",
      [(v.value, v.origin) for v in e.query.current("s", sk, "status")])

r2 = e.flush("s")
print("⑤ 再次 flush 是否为空操作: tasks_processed =", r2.tasks_processed)
PY
./.venv/bin/python /tmp/gate.py
```

**期望**（逐条对照）：

```
① add_cards 期间模型被调用次数 = 0                    ← P1：add 只入队
② task: {... 'status': 'completed', 'new_assertions': 2,
           'gate': {'accepted': 2, 'rejected': {...}} ...}
③ 关卡账本: accepted=1 rejections={'SOURCE_NOT_TRACEABLE': 1,
           'NOT_SEMANTIC': 1, 'UNKNOWN_PARENT_SUBJECT': 1}
④ 确定性 status 未被模型污染: [('open', 'model')]      ← 模型改不动确定性谓词
⑤ 再次 flush 是否为空操作: tasks_processed = 0         ← P9
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
cd $D
/absolute/path/to/.venv/bin/mh init >/dev/null
/absolute/path/to/.venv/bin/mh serve --host 127.0.0.1 --port 8899 &
sleep 4
curl -s http://127.0.0.1:8899/healthz; echo
/absolute/path/to/.venv/bin/python -c \
  'import json,yaml; print(json.dumps(yaml.safe_load(open("demo-messages.yaml"))))' \
  > demo-messages.json
R=$(curl -s -X POST http://127.0.0.1:8899/v1/scopes/demo/messages \
  -H 'content-type: application/json' -d @demo-messages.json)
echo "$R"
TASK=$(echo "$R" | /absolute/path/to/.venv/bin/python -c \
  'import json,sys; print(json.load(sys.stdin)["task_id"])')
/absolute/path/to/.venv/bin/mh flush demo >/dev/null
curl -s http://127.0.0.1:8899/v1/tasks/$TASK; echo
curl -s http://127.0.0.1:8899/v1/scopes/demo/matters; echo
curl -s -o /dev/null -w "docs=%{http_code}\n" http://127.0.0.1:8899/docs
pkill -f "mh serve"
```

**期望核心输出**：

```
{"status":"ok"}
{"accepted":1,"task_id":"task_..."}
{"status":"completed","cards_produced":1,"new_assertions":5,
 "gate":{"accepted":1,"rejected":{}}}
[{"title":"Payment refactor","status":"in_progress","owners":["u1"],...}]
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
tools: ['add_cards', 'add_messages', 'add_records', 'correct', 'list_matters',
        'query_at', 'query_by_person', 'query_current', 'query_timeline']
```

**验收点**：这是**真的 MCP 协议往返**（官方 SDK 客户端 + stdio），不是内存里调函数。

接到 Claude Code：运行 `mh setup claude-code`（hub 加 `--url`）；实际生成的
embedded/hub `.mcp.json` 与 `.claude/settings.json` 形状，以及可选 Skill，见
[examples/claude-code/](../examples/claude-code/)。

### 5.3 SDK 嵌入模式

```bash
./.venv/bin/python examples/embedded/demo.py
./.venv/bin/python examples/correction/demo.py
```

**期望**：后者输出 `before=blocked origin=model` → `after=open origin=human`。

### 5.4 M4：Slack fixture → Record → card → ingest → query

完整离线实录不需要 Slack token、LLM token 或网络：

```bash
./.venv/bin/python examples/slack/demo.py
```

逐段检查输出：

1. `Slack payload -> Records` 中两个 Record ID 都是 `C0123:<ts>`，并带
   `https://.../archives/.../p...` permalink；
2. `Records -> extracted EpisodeCards` 中卡片只引用输入窗口内的 Record ID；
3. `normal deterministic ingest -> query_current` 返回 `open`，证据 URI 可点击且
   `evidence_status` 为 `active`；
4. 编辑段有两个不同的 `observation_id`、`assertion_id` 和 `recorded_at`，旧断言
   仍在；
5. 删除段的断言数前后均为 2，但 `evidence_status` 与单条来源状态均为
   `revoked`；
6. 最后一段两个频道的 native `ts` 相同，namespaced Record ID 不同，
   `shared_source_ids: []` 且 `matter_count: 2`。

再验证三种协议面都真的接到了同一个 `add_records` 服务：

```bash
./.venv/bin/python -m pytest -q \
  tests/test_cli.py::test_extract_cli_wires_records_to_cards_ingest_and_sync_status \
  tests/test_protocols.py::test_rest_round_trip_all_endpoints_and_correction \
  tests/test_protocols.py::test_mcp_official_sdk_round_trip_all_nine_tools
```

**实跑输出**：`3 passed in 0.52s`。

### 5.5 M6：事件 → 人工纠错 → export/import → 零事件重放

下面是 2026-07-29 在一个全新临时目录里的实际命令链；仍使用 `mh init` 的离线
fixture，不访问网络：

```bash
D=$(mktemp -d /tmp/matterhorn-m6-journey.XXXXXX)
cd "$D"
/absolute/path/to/.venv/bin/mh init
/absolute/path/to/.venv/bin/mh add demo-messages.yaml
/absolute/path/to/.venv/bin/mh flush demo
/absolute/path/to/.venv/bin/mh matters demo
/absolute/path/to/.venv/bin/mh events demo
```

首批事件的真实核心输出（ID 与时间来自本次实跑）：

```text
status_changed id=06820184... old=null new=in_progress
matter_created id=84615d7f... predicate=next_step
subject_key=sub_4ad6c81d95364b1dc371
```

接着用同一有效时间做人工纠错，再查看事件：

```bash
/absolute/path/to/.venv/bin/mh correct \
  --scope-id demo \
  --subject-key sub_4ad6c81d95364b1dc371 \
  --subject-type MATTER \
  --predicate status \
  --object-value done \
  --valid-from 2026-07-28T00:00:00Z \
  --source-ref \
  '{"source_id":"human-acceptance","sent_at":"2026-07-29T08:00:00Z","sender":"human"}'
/absolute/path/to/.venv/bin/mh events demo
```

真实新增事件：

```text
value_corrected id=19f182a2... old=in_progress new=done
  origin=human source_ids=["human-acceptance"]
matter_completed id=a5b7aa3d... old=in_progress new=done
status_changed id=ed6bd3d4... old=in_progress new=done
```

最后交付资产并在全新数据库恢复：

```bash
/absolute/path/to/.venv/bin/mh export demo --out demo-export.json
/absolute/path/to/.venv/bin/mh import demo-export.json --db restored.db
/absolute/path/to/.venv/bin/mh matters demo
/absolute/path/to/.venv/bin/mh matters demo --db restored.db
/absolute/path/to/.venv/bin/mh query current \
  demo sub_4ad6c81d95364b1dc371 status
/absolute/path/to/.venv/bin/mh query current \
  demo sub_4ad6c81d95364b1dc371 status --db restored.db
/absolute/path/to/.venv/bin/mh replay demo --db restored.db
```

本次实跑结果：

```text
import: subjects=1 assertions=6 events=5 intervals=5 memory_cards=1
matters_identical = True
query_answers_identical = True
restored_origin = human
replay: intervals=5 memory_cards=1 events_emitted=0 status=rebuilt
```

这组结果同时守住四件事：事件来自投影变化；人工纠错的 `origin` 和证据不丢；
导出是完整的数据所有权交付；重放不会重复历史事件。

### 5.6 M6：本地 ASGI webhook 与结构化 404

仓库测试没有访问网络。下面的实际 receiver 是同进程 FastAPI app，
`httpx.ASGITransport` 直接把 webhook POST 送进去：

```bash
./.venv/bin/python -m pytest -q \
  tests/test_outputs.py::test_webhook_retries_to_in_process_asgi_receiver_and_dedupes
```

完整手工实跑还打印了 receiver 收到的批次：

```text
RECEIVED {"events":[
  {"event_type":"matter_created","event_id":"0a65de16..."},
  {"event_type":"status_changed","event_id":"34d47a04..."},
  {"event_type":"matter_completed","event_id":"ba808d94..."}
]}
delivered = 3
attempts = 1
second_delivery = 0
```

`second_delivery = 0` 证明成功批次已经本地确认；测试版本会让 receiver 第一次返回
503，再在第二次成功，且把 sleep 注入为空实现，因此覆盖有界退避但不做 wall-clock
等待。生产语义仍是 at-least-once，consumer 必须按 `event_id` 去重。

用同一个 ASGI 客户端实际请求未知资源，响应如下：

```text
unknown task 404
{"error":{"code":"NOT_FOUND","message":"unknown task_id: missing"}}
unknown scope 404
{"error":{"code":"NOT_FOUND","message":"unknown scope_id: missing"}}
unknown subject 404
{"error":{"code":"NOT_FOUND",
 "message":"unknown subject_key 'missing' in scope 'known'"}}
```

同一轮还实际调用了两个同步写端点：

```text
wait cards 200
{"status":"completed","task_id":"task_8eae2114...","cards_produced":1,...}
wait messages 200
{"status":"completed","task_id":"task_a1742ab1...","cards_produced":1,...}
```

因此 cards/messages 的 `wait: true` 都保留了可重新查询的 `task_id`。

---

## 6. 双后端验收（PostgreSQL）

**这一步是最容易被跳过、也最容易藏 bug 的一步。**
第二后端存在的意义就是证明规范能被实现两次——所以整套 conformance 必须在两个后端上跑。

需要本地有 PostgreSQL 二进制（`initdb` / `pg_ctl` / `createdb`）。
若它们不在 `PATH` 上，用 `PG_BIN` 指过去，例如
Homebrew：`export PG_BIN=$(brew --prefix postgresql@17)/bin`；
Debian/Ubuntu：`export PG_BIN=/usr/lib/postgresql/17/bin`。
不想装本地实例的话，改用仓库里的 [compose.postgres.yml](../compose.postgres.yml) 起容器。

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

**期望**：`302 passed`、零失败、零 skip，
`SUMMARY passed=54 failed=0 total=54`。这是 §1 的本地 conformance 通过项
加上 48 个 PostgreSQL 用例；本轮沙箱没有把它观察成实跑结果。

若仍看到 `48 skipped`，说明 DSN 没生效，PG 根本没被验证。

**进阶：验证排序不依赖数据库 locale**（一个答案取决于部署 locale 的规范不算规范）：

```bash
psql -h 127.0.0.1 -p 55432 -U matterhorn -d postgres \
  -c "CREATE DATABASE mh_collate TEMPLATE template0 ENCODING 'UTF8' \
      LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8';"
MATTERHORN_TEST_POSTGRES_DSN="postgresql://matterhorn@127.0.0.1:55432/mh_collate" \
  ./.venv/bin/python -m pytest -q
```

**期望**：同样零失败、零 skip。换了 collation 结果一模一样。

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
- **本轮沙箱没有 PostgreSQL 观察值**：SQLite、Ruff 与 GitHub Actions 门禁均已
  验证；PostgreSQL 17 仍由 CI 的 `postgres-conformance` 任务守护，但不要把该
  远端结果写成本沙箱本地实跑结果。

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

**通过标准**：零失败，零 skip；47 个 PostgreSQL conformance 用例全部执行。
