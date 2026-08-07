# Matterhorn language-neutral conformance format

Every `*.yaml` file is one mapping. Files run in lexicographic filename order;
case behavior does not depend on that order. A runner must create an empty
store view for the case's `scope_id`.

## Top-level fields

| Field | Required | Type and semantics |
| --- | --- | --- |
| `case_id` | yes | Unique stable kebab-case string used in reports. |
| `title` | yes | Human-readable string; never used for behavior. |
| `invariants` | yes | Non-empty list of `P1`…`P9` and/or `INV-1`…`INV-23`. |
| `schema_profile` | yes | Built-in profile ID string or complete inline SchemaProfile mapping. |
| `scope_id` | yes | Scope supplied to ingest, dream, correction, and queries. |
| `clock` | yes | Ordered RFC 3339 timestamps. Consume one for task creation, each flush retention reference, each newly processed card, accepted semantic assertion, or correction. |
| `cards` | yes | Ordered EpisodeCard mappings, validated exactly as SPEC section 3.3. |
| `federated_cards` | no | EpisodeCards seeded in additional scopes before cross-scope structure operations; replay covers every declared scope. |
| `message_batches` | no | Ordered `{messages}` batches passed to `add`, each followed by `flush`. A batch may instead declare `flush: {mode: quiet, at, quiet_period_minutes, max_batch_delay_minutes}` to run deadline-aware quiet selection at the injected instant. |
| `message_model_responses` | no | Ordered closed Message/Record-to-card fixture responses, one per extractor call made by message batches. |
| `record_batches` | no | Ordered `{records, cursors?, backfill?, batch_size?, purge_staging_at?}` mappings. A `purge_staging_at` RFC 3339 instant runs the configured retention purge immediately before that batch. |
| `record_model_responses` | no | Ordered Record-to-card response fixtures, one per extractor call over unseen, non-revoked Records. |
| `corrections` | no | Ordered Correction mappings; default empty list. |
| `merge_operations` | no | Ordered merge/unmerge mappings with `operation`, source key, merge-only target key, `valid_from`, non-empty `source_refs`, and optional operation-level `expect_error`. |
| `handle_normalization_cases` | no | Ordered `{handle_type,value,normalized_value}` mappings evaluated without persistence. |
| `handle_operations` | no | Ordered human `bind`/`unbind` mappings with mandatory `source_refs`. |
| `adjudication_model_responses` | no | Ordered closed identity-adjudication fixtures, consumed only by calls using the section 23 routing response schema. |
| `unified_loop` | no | Boolean section 26 feature flag; default false. |
| `tool_loop_sessions` | no | Ordered scripted section 26 sessions. Each session contains `turns` of generic tool calls and one optional final message. |
| `theme_config` | no | Section 28/29 settings (`mode`, cluster/backlog/interval thresholds, conversation fanout, and `human_edge_weight`). |
| `theme_operations` | no | Ordered section 28 operations. `observe_record` commits evidence-conversation provenance; `run` invokes one theme pass and may declare `dry_run`. |
| `model_responses` | no | Ordered model response fixtures described below. Presence, including `[]`, means the runner invokes `dream(scope_id)` after ingest. Absence means it does not. |
| `review_operations` | no | Ordered review resolutions with `review_id`, `action`, nullable `subject_key`, mandatory `source_refs`, and optional `expect_error`. |
| `signal_config` | no | Engine signal settings: optional identity handles, pattern extensions, and hotness thresholds. |
| `signal_operations` | no | Ordered terminal signal acknowledgements with record id, kind, and acknowledgement instant. |
| `watermark_operations` | no | Ordered matter read-watermark upserts with subject key and last-seen instant. |
| `structure_operations` | no | Ordered gated goal-graph assertions, each optionally carrying `expect_error`; human correction origin is the default, while `origin: model` represents one distinct admitted model assertion. These run after merge operations so canonical merge-chain cycle gates are expressible. |
| `expect_error` | no | Error-message regular-expression/substring. The case passes only if ingest/correction rejects and the scope has no assertions or intervals. |
| `expect` | for success | Expected partial-field multisets, queries, counters, and reports. |

`expect.gather_queries` contains `{scope_id?, subject_key, result,
replay_identity?}` mappings. Results are partial deterministic gather views;
when `replay_identity` is true, the complete canonical view must remain
byte-identical after replay.

Unknown fields in cards, profiles, corrections, model candidates, and model
response envelopes are errors because their respective contracts are closed.

## `model_responses:` fixtures

Each list element is returned by exactly one `LlmGateway.complete()` call for a
queued card, in ascending `card_id` order. A mapping/list/scalar fixture is
serialized as JSON before the engine receives it. A string fixture is returned
verbatim, which allows an intentionally malformed JSON response.

The normal closed shape is:

```yaml
model_responses:
  - candidates:
      - subject_key: choice-1
        subject_type: CHOICE
        parent_subject_key: null
        subject_title: null
        predicate: rationale_class
        operation: ASSERT
        object_value: timing
        valid_from: 2026-01-02T09:00:00Z
        source_ids: [m1]
        confidence: 0.95
```

`subject_key`, `parent_subject_key`, and `subject_title` are the only optional
candidate fields; every other shown field is required. Additional fields are
forbidden. `source_ids` is unique, non-empty, and a subset of the originating
card's sources for acceptance.

`model_responses` is data, never source code, a callback, or an expression. A
Java runner can implement a queue fixture gateway whose `complete` method
JSON-serializes and returns the next element. It must fail clearly if the
fixture list is exhausted.

`record_model_responses` uses the closed `{cards: [...]}` response from SPEC
section 16. Each item is consumed by one conversation-scoped, boundary-packed
extractor call, not necessarily one input batch. Its `source_ids` must cite
`record_id` values in that call's exact context-plus-new-Records union. Record
fixtures use the closed section 3.2 contract. A revocation-only batch consumes
no model response. The runner
supplies fresh canonical matter anchors to each extraction call, so a response
may cite an exactly offered `subject_key`; a non-offered value is silently
stripped.

`message_model_responses` uses the same closed `{cards: [...]}` shape, but its
sources must be section 3.1 namespaced Message-derived Record IDs.

`adjudication_model_responses` uses the closed section 23 response shape:

```yaml
adjudication_model_responses:
  - decision: attach
    subject_key: candidate-1
    confidence: 0.86
    evidence_source_ids: [CONV:r1]
```

The fixture gateway MUST distinguish extraction, adjudication, and semantic
calls by their `response_schema`; one call kind MUST NOT consume another kind's
fixture queue.

`tool_loop_sessions` drives the optional bounded gateway capability without a
network. A turn declares either `tool_call: {name,arguments}`, a `tool_calls`
list of those mappings, or `final_message`. The runner executes calls through
the real tool handlers, applies the 16-call / 4-emission bounds, and fails when
the scripted session queue is exhausted.

Theme naming consumes the same scripted sessions independently of the section
26 feature flag. Its closed world is the proposed cluster (plus its existing
target root, when present), and each accepted session may emit at most one
theme proposal. `observe_record` theme operations must name an existing source
ID and supply a committed `RecordObservation` container before evidence
conversation affinity can be formed.

## `expect:` fields

| Field | Meaning |
| --- | --- |
| `assertions` | List of partial Assertion mappings. |
| `intervals` | List of partial Interval mappings. |
| `queries` | Ordered query checks `{name, args, result}`. |
| `subject_count` | Exact integer subject count. |
| `conflicts_resolved` | Predicate-to-exact-count mapping. |
| `gate_statistics` | Partial gate-counter mapping; declared fields compare exactly, including `unchanged_dropped` and any declared route counters. |
| `dream_report` | Partial first-dream report mapping. |
| `second_dream` | Partial report after duplicate ingest and a second dream. |
| `record_reports` | Ordered partial reports from the first Record batches. |
| `second_record_reports` | Ordered partial reports after exact Record re-ingest. |
| `task_results` | Ordered partial task results for first-pass Message batches. |
| `second_task_results` | Ordered partial results after exact Message re-add. |
| `flush_reports` | Ordered lists of partial FlushReport mappings from first-pass Message batch flush selection. |
| `second_flush_reports` | Equivalent report lists after exact Message re-add. |
| `extraction_calls` | Ordered calls, each with exact ordered `context` and `records` lists of partial Record mappings. `context` may be omitted to assert the empty list. |
| `staging_purge_counts` | Exact ordered deleted-row counts for `record_batches` that declare `purge_staging_at`. |
| `events` | Partial ChangeEvent mappings compared as an exact multiset. |
| `merge_count` | Exact active SubjectMerge count. |
| `handle_bindings` | Partial-field exact multiset of all active and revoked SubjectHandle rows. |
| `subject_handles` | Mapping from subject key to its exact active canonicalized handle list. |
| `handle_lookups` | Ordered `{handle_type,value,result}` lookup checks; `handle_type` may be null and `result` is an exact list of partial canonical SubjectHandle mappings. |
| `review_items` | Partial-field exact multiset of pending or resolved ReviewItems. |
| `adjudication_calls` | Ordered calls with exact offered candidate keys and optional partial rich-candidate payload checks. |
| `tool_loop_calls` | Exact number of bounded tool-loop sessions executed before duplicate-ingest and replay checks. |
| `theme_reports` | Ordered partial section 28 pass reports produced by `theme_operations` whose operation is `run`. |
| `matters` | Partial-field exact multiset of canonical ergonomic Matters, including aliases. |
| `export_replay_identity` | Boolean requiring byte-identical ownership exports immediately before and after replay. |
| `replay_events_emitted` | Exact number of new events returned by replay. |
| `signals` | Partial-field exact multiset of deterministic Signal rows. |
| `watermarks` | Exact subject-key to canonical timestamp mapping. |
| `hotness_queries` | Ordered windowed hotness reads with partial result rows. |
| `brief_queries` | Ordered windowed briefing reads with deterministic ordered results. |
| `graph_queries` | Ordered `matter_graph` reads with `subject_key` and a partial deterministic graph/rollup result. |

For assertions and intervals, project each actual item onto exactly the keys in
one expected mapping, then compare an order-insensitive exact multiset. The
actual and expected list lengths must match, so extra actual items fail.
Nested lists such as `supporting_assertion_ids` remain order-sensitive.

Queries run in listed order after ingest, optional first dream, and corrections:

- convert an `args.instant` RFC 3339 string to an instant;
- call the query named by `name` with `scope_id` plus `args`;
- compare result list order exactly;
- for each result mapping, compare only fields declared in `result`;
- compare scalar/object results exactly.

Datetime comparison uses canonical UTC RFC 3339 with six fractional digits and
`Z`. Enum values compare as their string values. Sets compare as sorted lists.

## Mandatory implicit checks

Every successful case must additionally:

1. snapshot assertions, intervals, memory cards, projection statistics, events,
   subjects, active merges, all SubjectHandle rows, pending and resolved review
   rows, Signals, read watermarks, Record observations, source lifecycle, and
   sync positions in canonical JSON;
2. add the identical Message, card, and Record batches again;
3. if `model_responses` was present, call `dream()` again;
4. apply the same corrections again;
5. require the canonical snapshot to be byte-identical;
6. call `replay(scope_id)`; and
7. require another byte-identical snapshot.

This proves idempotent retry and projection rebuild for every golden case, not
only cases whose title mentions replay. Merge operations run once because
repeating an already-active source merge is normatively an error; their stored
state participates in both snapshots. Human handle and review-resolution
operations also run once because they are historical correction operations.
Theme operations run once as well; cases that require a repeated pass declare
it explicitly, and the persisted per-scope schedule state participates in both
snapshots. Raw staging is deliberately outside these replay-identity snapshots: cases
assert its contextual effects and purge counts directly, while replay remains
independent of expiring staging state.

## Reference runner

```console
mh conformance run
mh conformance run --suite /path/to/spec/conformance
mh conformance run --backend postgres --dsn postgresql://...
```

It prints `PASS|FAIL case_id - title`, then
`SUMMARY passed=N failed=N total=N`. Exit codes are 0 for all pass, 1 for any
valid case failure, and 2 when the suite cannot be loaded because its directory
or a case file is missing, unreadable, empty, or malformed. The Python pytest
suite uses the same runner against SQLite and PostgreSQL.
