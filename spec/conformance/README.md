# Matterhorn language-neutral conformance format

Every `*.yaml` file is one mapping. Files run in lexicographic filename order;
case behavior does not depend on that order. A runner must create an empty
store view for the case's `scope_id`.

## Top-level fields

| Field | Required | Type and semantics |
| --- | --- | --- |
| `case_id` | yes | Unique stable kebab-case string used in reports. |
| `title` | yes | Human-readable string; never used for behavior. |
| `invariants` | yes | Non-empty list of `P1`…`P9` and/or `INV-1`…`INV-10`. |
| `schema_profile` | yes | Built-in profile ID string or complete inline SchemaProfile mapping. |
| `scope_id` | yes | Scope supplied to ingest, dream, correction, and queries. |
| `clock` | yes | Ordered RFC 3339 timestamps. Consume one only for each newly processed card, accepted semantic assertion, or correction. A duplicate/no-op consumes none. |
| `cards` | yes | Ordered EpisodeCard mappings, validated exactly as SPEC section 3. |
| `corrections` | no | Ordered Correction mappings; default empty list. |
| `model_responses` | no | Ordered model response fixtures described below. Presence, including `[]`, means the runner invokes `dream(scope_id)` after ingest. Absence means it does not. |
| `expect_error` | no | Error-message regular-expression/substring. The case passes only if ingest/correction rejects and the scope has no assertions or intervals. |
| `expect` | for success | Expected partial-field multisets, queries, counters, and reports. |

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

## `expect:` fields

| Field | Meaning |
| --- | --- |
| `assertions` | List of partial Assertion mappings. |
| `intervals` | List of partial Interval mappings. |
| `queries` | Ordered query checks `{name, args, result}`. |
| `subject_count` | Exact integer subject count. |
| `conflicts_resolved` | Predicate-to-exact-count mapping. |
| `gate_statistics` | Exact `{scope_id, accepted, rejections}` mapping. |
| `dream_report` | Partial first-dream report mapping. |
| `second_dream` | Partial report after duplicate ingest and a second dream. |

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

1. snapshot assertions, intervals, memory cards, projection statistics, and
   subjects in canonical JSON;
2. ingest the identical card batch again;
3. if `model_responses` was present, call `dream()` again;
4. apply the same corrections again;
5. require the canonical snapshot to be byte-identical;
6. call `replay(scope_id)`; and
7. require another byte-identical snapshot.

This proves idempotent retry and projection rebuild for every golden case, not
only cases whose title mentions replay.

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
