# Self-hosted development ledger

Matterhorn tracks its own development as a public, self-hosted ledger. The
nightly writer uses an LLM; every public read and local reproduction is
deterministic and requires no model or credential.

## Write path

The `Development ledger` GitHub Actions workflow runs nightly and on manual
dispatch:

1. It checks out full Git history and fails before filling if any required
   write-gateway secret is absent.
2. It deletes the disposable `ledger/dev.db` and imports the committed
   `ledger/assertions.json` ownership envelope when that file exists.
3. `scripts/ledger_fill.py` maps Git commits, tracked devlogs, GitHub issues,
   and comments to Records. It sends at most eight Records per extraction call
   by default, never splitting one thread; an oversized thread gets one
   oversized call.
4. Prompts expose evidence as `m1`, `m2`, and so on. The extractor maps known
   aliases back to real provider IDs before the unchanged traceability gate.
   Unknown aliases remain unknown and are rejected as `SOURCE_NOT_TRACEABLE`.
5. Accepted cards and assertions are projected, then `mh export` replaces the
   durable JSON envelope and deterministic `MATTERS.md`.
6. CI commits only when `ledger/` or `MATTERS.md` changed. With no new source
   IDs, the imported source lifecycle acts as the ledger checkpoint, no LLM
   call occurs, and a consecutive run has no diff.

The SQLite database is gitignored. Assertions, subjects, source lifecycle, and
event history in `ledger/assertions.json` are the durable state. This follows
the normative single-document scope export; the project does not use a JSONL
variant.

## Read path

The read path imports the ownership envelope, rebuilds intervals and
MemoryCards, and runs `mh matters` or `mh export --format markdown`. It never
loads a gateway. Markdown ordering is stable, has no generation timestamp, and
links every interval's evidence to its source URI when one exists. Evidence
without a URI is shown as its bare source ID. An interval opened by an
`origin=human` assertion carries a visible **[human correction]** badge.

`MATTERS.md` displays, per matter, title, status, owners, blockers, next step,
due date, and a collapsible evidence-backed interval timeline.

## What the public artifacts prove

| Public property | What it demonstrates |
| --- | --- |
| Committed ownership envelope | Assertions, human corrections, evidence lifecycle, and events are portable project assets, not a hosted-service dependency. |
| Rebuilt database | Intervals and MemoryCards are disposable pure projections rather than a second source of truth. |
| Stable Markdown bytes | The same store state produces the same public ledger without a wall-clock value or read-side model. |
| Evidence links | Every displayed change remains traceable to a commit, issue, comment, document, or bare provider source ID. |
| Human badge | Corrections participate in the ordinary assertion timeline and remain publicly distinguishable from model output. |
| No-diff second run | Already exported source identities do not get reinterpreted, so the nightly loop is observably idempotent. |

## Reproduce without an LLM key

```console
git clone https://github.com/misshqiong/matterhorn
cd matterhorn
pip install -e .
mh import ledger/assertions.json --db ledger/dev.db
mh matters dev --db ledger/dev.db
```

To reproduce the rendered file instead:

```console
mh export dev --format markdown --out MATTERS.md --db ledger/dev.db
```

## Run one fill locally with Ollama

Start Ollama's OpenAI-compatible endpoint and select a locally installed model:

```console
export MATTERHORN_PROVIDER=openai-compatible
export MATTERHORN_BASE_URL=http://localhost:11434/v1
export MATTERHORN_MODEL=qwen3:4b
export MATTERHORN_API_KEY=ollama
export MATTERHORN_TIMEOUT=600
python scripts/ledger_fill.py --db ledger/dev.db --batch-size 8
```

`MATTERHORN_TIMEOUT` accepts positive floating-point seconds and defaults to
`60`. Small local models can produce rough ledgers or gate rejections; report
those results honestly. CI-grade models rebuild from the committed assertions
and replace the same export/render targets idempotently.
