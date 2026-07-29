# Matterhorn Normative Specification

Version: 0.4.0

This document is the language-neutral source of truth for Matterhorn
implementations. The key words **MUST**, **MUST NOT**, **SHOULD**, and **MAY**
are normative.

## 1. Principles

- **P1 — LLM confinement.** An implementation MUST confine every LLM call to
  the write path and MUST NOT invoke an LLM from any read path.
- **P2 — Schema-contracted extraction.** Extracted output MUST be closed JSON
  under the active `SchemaProfile`; it MUST NOT contain an unregistered
  predicate or free-form digest in place of a predicate.
- **P3 — Pre-persistence validation.** An implementation MUST validate type,
  predicate registration, evidence, and configured conservative filters before
  persisting a candidate, and MUST reject a candidate that fails validation.
- **P4 — Zero-model answers.** Every answer MUST be derived deterministically
  from persisted data by SQL or equivalent deterministic computation; the
  answer itself MUST NOT pass through a generative model.
- **P5 — Provenance in the contract.** Every input card and every persisted
  assertion MUST contain at least one `source_ref`; a candidate without
  provenance MUST be rejected, and every query result MUST remain traceable to
  its assertion evidence, including a source URI when the input Record provides
  one.
- **P6 — Bi-temporal time.** Every assertion MUST keep business effective time
  (`valid_from`, projected `valid_to`) separate from system observation time
  (`recorded_at`); an implementation MUST NOT substitute one axis for the
  other.
- **P7 — Append-only assertions and rebuildable projection.** Assertions MUST
  be immutable and append-only, while intervals MUST be a pure, disposable
  projection of the assertion set and MUST be rebuildable in full.
- **P8 — First-class correction.** Human correction MUST be a core write
  protocol, MUST create assertions in the same assertion set, and human
  assertions MUST outrank model assertions at the same effective instant.
- **P9 — Idempotent replay.** Retrying the same input MUST NOT create duplicate
  cards, assertions, intervals, or materializations; every pipeline stage MUST
  be safe to replay.

## 2. Invariants

- **INV-1 — Closed predicates.** A persisted assertion's predicate MUST exist
  in the active profile and MUST be registered for the assertion's subject
  type; an assertion with an unregistered predicate MUST be rejected.
- **INV-2 — Immutable, idempotent assertions.** Assertions MUST be append-only.
  `assertion_id` MUST equal the hash algorithm in section 6. The same ID and
  same immutable payload MUST be a no-op; the same ID with a different payload
  MUST be rejected.
- **INV-3 — Pure interval projection.** The complete interval set, including
  supporting assertion IDs and accumulated evidence, and projection statistics
  MUST be a pure function of the complete assertion set
  plus its `SchemaProfile`. Deleting all intervals and memory cards and
  replaying MUST produce a byte-identical canonical result.
- **INV-4 — Symmetric retraction guard.** Null or absent fields MUST NOT imply
  clearing under `explicit` or `never`; null, absent, or empty fields MUST
  imply clearing under `implicit`. The condition that emits a `RETRACT`
  and the condition that says the observation updates projection/materialized
  state MUST be literally the same shared predicate function. Consequently,
  `value → missing → same value` MUST retain one uninterrupted interval and
  MUST NOT flicker.
- **INV-5 — Evidence-based identity merge.** Subject resolution MUST use the
  order and thresholds in section 7. Evidence merge MUST occur if and only if
  `shared >= 2 AND (shared >= min_shared_sources OR
  shared / len(card.source_refs) >= or_share_ratio)`; otherwise a new subject
  MUST be created. A single shared source MUST NEVER cause a merge.
- **INV-6 — Read-after-write consistency.** Card validation, identity,
  extraction, assertion persistence, projection, statistics, and
  materialization for a batch MUST commit in one store transaction. A query
  issued after `ingest()` returns MUST see that committed projection.
- **INV-7 — Mandatory and retained provenance.** `EpisodeCard.source_refs`,
  `Assertion.source_refs`, and `Correction.source_refs` MUST be non-empty; a
  value without source evidence MUST be rejected before persistence. Every
  assertion that opens or reconfirms a live interval MUST remain reachable
  through that interval's `supporting_assertion_ids`, and its source evidence
  MUST be included in interval and query evidence.
- **INV-8 — Stable SINGLE conflict resolution and accounting.** At an
  effective instant, competing SINGLE assertions MUST be resolved by maximum
  `(valid_from, origin_rank, recorded_at, assertion_id)`, where human is `1`
  and model is `0`. Every discarded distinct competing value MUST increment
  `conflicts_resolved` for its `(scope_id, predicate)`, and the statistic MUST
  be returned by projection and stored.
- **INV-9 — Human precedence.** At the same `valid_from`, a human assertion
  MUST beat every model assertion regardless of `recorded_at`; a correction
  MUST participate in the ordinary projection and MUST NOT mutate or delete a
  model assertion.
- **INV-10 — No LLM on reads.** `current`, `timeline`, `at`, `by_person`,
  `list_matters`, and `completion` MUST NOT import, instantiate, or call the
  distillation/LLM package. Installing a gateway that raises on every access
  MUST NOT affect any read.
- **INV-11 — Immutable source lifecycle.** An edited Record is a new
  observation. It MUST produce a new deterministic card observation and new
  assertions with a distinct non-null `observation_id` and `recorded_at`;
  prior assertions MUST remain untouched. A revoked Record MUST persist the
  revocation for its `record_id`, MUST NOT invoke card extraction, and MUST NOT
  mutate or delete any assertion, interval, or materialization. Every value
  query MUST expose each source's `uri`, `status` (`active` or `revoked`), and
  nullable `revoked_at`, plus aggregate `evidence_status`: `active` when none
  are revoked, `partially_revoked` when some are revoked, and `revoked` when
  all are revoked. A conclusion supported only by revoked evidence therefore
  remains queryable but is visibly flagged.

## 3. Record and EpisodeCard contracts

### 3.1 Record

`Record` is the provider-neutral communication input. Unknown fields MUST be
rejected. Date-times follow the canonical rules in this specification.

| Field | Type | Presence | Rule |
| --- | --- | --- | --- |
| `record_id` | string | required | Globally unique source identity, exactly namespaced as `<container_id>:<native_id>`. The suffix MUST be non-empty and MUST equal `native_id` when that field is present. |
| `container_id` | string | required | Channel, room, or conversation identity. |
| `thread_id` | string or null | optional, default null | Provider thread identity, namespaced by the same `container_id`; null means the record itself is the matter boundary. |
| `sent_at` | datetime | required | Original source-message time. |
| `author` | RecordAuthor | required | Required `id`, nullable `display_name`, and `kind` in `human`, `bot`, `app`. |
| `content` | string | required | Normalized human-readable content. |
| `uri` | string or null | optional, default null | Human-followable permalink when the provider exposes one. |
| `reactions` | array | optional, default `[]` | Each item has `name`, non-negative `count`, and unique `author_ids`. |
| `attachments` | array | optional, default `[]` | Normalized `attachment_id`, `kind`, nullable `title`, `mime_type`, `uri`, and non-negative nullable `size`. |
| `edited_at` | datetime or null | optional, default null | Stable source edit time; MUST NOT precede `sent_at`. |
| `revoked_at` | datetime or null | optional, default null | Stable source deletion/revocation time; MUST NOT precede `sent_at`. |
| `kind` | string | optional, default `message` | Normalized record kind, normally `message` or `revocation`. |
| `subtype` | string or null | optional, default null | Provider subtype after filtering. |
| `native_id` | string or null | optional | Provider identity used to validate the `record_id` suffix. |
| `workspace_id` | string or null | optional | Provider workspace/team identity. |
| `client_id` | string or null | optional | Provider client-generated id, when present. |
| `parent_author_id` | string or null | optional | Thread parent author identity, when present. |
| `broadcast` | boolean | optional, default `false` | Whether a thread reply was broadcast to the container. |

`record_id` namespacing is load-bearing. In particular, Slack `ts` is unique
only within a conversation. `C1:1699887654.123456` and
`C2:1699887654.123456` MUST remain distinct evidence; an implementation MUST
reject bare `1699887654.123456` rather than allowing INV-5 to count unrelated
messages as shared evidence.

`ChatMessage{message_id,sent_at,sender,content}` remains a deprecated
compatibility alias for the M3 Python API. New protocol and conformance inputs
MUST use `Record`.

### 3.2 EpisodeCard

Unknown fields MUST be rejected. Date-times MUST be RFC 3339 values. A date-time
without an offset is interpreted as UTC; canonical output is UTC.

| Field | Type | Presence | Rule |
| --- | --- | --- | --- |
| `card_id` | string | required | Idempotency key within `scope_id`. Reuse with changed payload MUST fail. |
| `scope_id` | string | required | Memory isolation domain. |
| `date` | calendar date | required | Supplies fallback effective date. |
| `title` | string | required | Subject identity evidence and human label. |
| `status` | string or null | optional, default null | Observation field. |
| `participants` | array of Participant | optional, default `[]` | Each Participant has required string `id`, optional nullable string `display_name`, and optional nullable string `role`. |
| `progress` | string or null | optional, default null | Observation field. |
| `blocker` | string or null | optional, default null | Observation field. |
| `next_step` | string or null | optional, default null | Observation field. |
| `due` | datetime or null | optional, default null | Observation field. |
| `outcome` | Outcome or null | optional, default null | Outcome has required string `type` and required string `content`. |
| `occurred_at` | datetime or null | optional, default null | Exact business effective time. |
| `last_active_at` | datetime or null | optional, default null | Input metadata; it does not replace `valid_from`. |
| `source_refs` | non-empty array of SourceRef | required | SourceRef has required string `source_id`, required datetime `sent_at`, required string `sender`, optional nullable string `excerpt`, and optional nullable string `uri`. A Record-derived card MUST copy `record_id`, content, and URI rather than synthesize evidence. |
| `cleared_fields` | array of unique strings | optional, default `[]` | A listed input field is explicitly cleared. |
| `subject_key` | string or null | optional, default null | Explicit subject identity override. |
| `thread_id` | string or null | optional, default null | Deterministic communication boundary assigned by Record extraction; it is not model-supplied. |

For `explicit` and `never`, a null or absent observation means “no observation
on this card” and MUST NOT retract prior knowledge. `cleared_fields` is the
explicit clearing channel. For `implicit`, absent, null, and extraction-rule
empty values are all explicit negative observations and MUST retract.

Card assertion `valid_from` MUST be `occurred_at` when non-null, otherwise
00:00:00 UTC at `date`. `recorded_at` MUST be the ingest wall clock. The clock
MUST be injectable; conformance cases provide a sequence consumed once for
each newly processed card or correction.

## 4. SchemaProfile contract

A profile is a YAML or JSON object. Unknown fields MUST be rejected.

| Field | Type | Required/default | Rule |
| --- | --- | --- | --- |
| `schema` | string | required | Stable profile/version identifier. |
| `subjects` | non-empty array | required | Each entry has required unique string `type`, optional nullable `parent` naming another declared type, and boolean `primary` default `false`. At most one is primary; otherwise the first is primary. |
| `predicates` | non-empty array | required | Names MUST be unique. See below. |
| `identity` | object | default `{}` | Contains `merge_evidence`. |
| `identity.merge_evidence.min_shared_sources` | integer >= 1 | default `2` | Absolute merge threshold. |
| `identity.merge_evidence.or_share_ratio` | number in `(0,1]` | default `0.5` | New-card evidence share threshold. |
| `completion` | object or null | default null | Optional registered `predicate` and string array `completed_values`. |
| `semantic` | object | default `{}` | Semantic extraction policy. |
| `semantic.conservative_confidence_threshold` | number in `[0,1]` | default `0.8` | Minimum confidence for predicates with `semantic_filter: conservative`. |

Each predicate entry has:

| Field | Type | Required/default | Allowed values/rule |
| --- | --- | --- | --- |
| `name` | string | required | Closed predicate identity. |
| `subject` | string | required | A declared subject type. |
| `cardinality` | string | required | `SINGLE`, `SET`, or `APPEND`. |
| `extraction` | string | required | `deterministic` or `semantic`. Deterministic extraction MUST skip `semantic`; only `dream()` MAY process it. |
| `retract_guard` | string | default `explicit` | `explicit`, `never`, or `implicit`. |
| `object` | string | default `string` | Object category; `person`, `datetime`, `object`, and application-defined strings are allowed. |
| `source_field` | string or null | required for deterministic | EpisodeCard field read by the rule. |
| `extraction_rule` | string | default `scalar` | `scalar`, `participant_ids`, or `list`. |
| `role_filter` | array of strings | default `[]` | For `participant_ids`, include all when empty, otherwise only matching roles. |
| `semantic_filter` | string or null | default null | Allowed values are `conservative` and null. `conservative` invokes the profile threshold; null adds no confidence filter. |
| `value_domain` | array or null | default null | When non-null, every semantic ASSERT value MUST be exactly equal to one member. |

The core MUST obtain all predicate names, subject types, field mappings,
person-valued predicates, and completion meaning from this profile. It MUST NOT
contain profile-domain vocabulary.

The two built-in profiles MUST be package resources under
`matterhorn.schemas`, at `org-matters/v1.yaml` and
`personal-decisions/v1.yaml`. A profile resolver MUST accept exactly three
input forms: a built-in profile ID such as `org-matters/v1`, a filesystem path
to YAML, or an already validated `SchemaProfile`. The SDK and every CLI command
MUST use this same resolver. Built-in ID resolution MUST NOT depend on the
current working directory, a source checkout, or a platform `share/` path.
An optional CLI schema directory MAY add or override profiles by ID.

## 5. Assertion, interval, materialization, and correction

Each persisted Subject contains `scope_id`, globally stable `subject_key` within
that scope, declared `subject_type`, title, normalized title, accumulated source
IDs, and nullable `parent_subject_key`. A primary subject resolved from an
EpisodeCard MUST have a null `parent_subject_key`. A non-primary subject created
by semantic distillation MUST name an existing parent instance whose type
equals the child type's declared `parent`.

An Assertion contains required `assertion_id`, `scope_id`, `subject_key`,
`subject_type`, registered `predicate`, `operation` (`ASSERT` or `RETRACT`),
JSON `object_value`, string `object_key`, `valid_from`, `recorded_at`, non-empty
`source_refs`, `origin` (`model` or `human`), and nullable `observation_id`.
Assertions extracted from Records MUST use the deterministic Record-derived
card ID as `observation_id`; card-native, semantic, and correction assertions
use null.

`object_key` for an asserted value is canonical JSON:
`json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)`
after converting datetimes to canonical instants. A field-wide explicit
clear uses the reserved key `"*"`. For SINGLE it closes the current value; for
SET it is defined as an explicit retraction of every currently live object key.

An Interval contains the opening assertion evidence plus `interval_id`,
`valid_from`, nullable `valid_to`, and `supporting_assertion_ids`.
`supporting_assertion_ids` MUST start with the opening `assertion_id`; every
later assertion that reconfirms the same live `object_key` MUST be appended in
stable rank order without closing or reopening the interval. `source_refs`
MUST be the stable union of evidence from every supporting assertion: traverse
supporting assertions in list order, preserve each assertion's source order,
and retain the first SourceRef for each `source_id`. Non-point intervals are
half-open `[valid_from, valid_to)`; null `valid_to` means open-ended.

A MemoryCard contains `scope_id`, `subject_key`, `subject_type`, title, a
profile-keyed current-value map, latest current `valid_from` as `updated_at`,
and sorted union of source IDs. It is disposable projection state.

A Correction contains `scope_id`, existing `subject_key`, `subject_type`,
registered `predicate`, `operation`, JSON `object_value`, optional
`object_key`, `valid_from`, and non-empty `source_refs`. It always emits origin
`human`. APPEND retraction MUST be rejected.

## 6. Exact assertion_id derivation

1. Convert `valid_from` to UTC RFC 3339 with six fractional digits and suffix
   `Z` (example `2026-01-02T00:00:00.000000Z`).
2. Sort source IDs lexicographically, retaining duplicates if supplied.
3. Construct this base JSON array exactly:

```text
[scope_id, subject_key, predicate, operation, object_key,
 valid_from_iso, sorted(source_ids)]
```

4. If and only if `observation_id` is non-null, append it as the eighth array
   member. This makes every source edit a distinct assertion observation while
   keeping retries of the same deterministic card idempotent.

5. Serialize exactly with:

```python
json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
```

6. UTF-8 encode and compute lowercase hexadecimal SHA-256.

No other field, including `recorded_at` or origin, participates. For
Record-derived assertions, the card ID participates only through the explicit
`observation_id` rule above. Equal observations therefore produce the same ID
and re-ingest is a no-op, while a changed/edited Record produces a different
deterministic card and assertion.

## 7. Exact subject identity algorithm

Normalize a title by lowercasing, replacing every Unicode punctuation or symbol
character with a space, collapsing whitespace, and trimming it.

For each card, resolve in this strict order:

1. If `subject_key` is non-null, use it. Create that subject if it is absent.
2. If `thread_id` is non-null, exact-match that thread boundary among subjects
   of the profile's primary type. On multiple matches choose lexicographically
   smallest key.
3. For a non-null `thread_id`, compute source-ID overlap with every existing
   primary subject using the rule below. This is the only cross-thread merge
   path; normalized-title equality MUST NOT merge two thread-bound cards.
4. If the thread-bound card did not merge, create or reuse the exact key
   `sub_` plus the first 20 lowercase hex characters of SHA-256 over canonical
   JSON `[scope_id, primary_subject_type, "thread", thread_id]`. Persist the
   thread boundary on that subject.
5. For a card with null `thread_id`, exact-match normalized title among
   subjects of the profile's primary type. On multiple matches choose
   lexicographically smallest key.
6. Otherwise compute source-ID overlap with each existing primary-type subject.
   Define `shared = count(card source IDs intersect subject source IDs)` and
   `ratio = shared / len(card.source_refs)`. Merge if and only if:

   ```text
   shared >= 2 AND
   (shared >= min_shared_sources OR ratio >= or_share_ratio)
   ```

   The absolute floor of two shared sources gates both branches because one
   shared message is never sufficient identity evidence, even when it is the
   new card's only message. The ratio branch remains a valid relaxation when
   `min_shared_sources > 2`. Among eligible matches choose maximum shared
   count, then lexicographically greatest key.
7. Otherwise create a new deterministic subject key.

The thread key in step 4 is a cross-language conformance field. Other generated
key formats are not cross-language fields unless a case provides `subject_key`;
legacy identity cases compare subject count/equality instead.

### 7.1 Semantic child subject identity

Only `dream()` MAY create a non-primary subject. A semantic candidate requests
child creation by supplying both `parent_subject_key` and `subject_title`; its
`subject_key` MAY be absent or null and MUST NOT control the created identity.
The parent MUST already exist in the originating scope. The requested
`subject_type` MUST be non-primary and its SchemaProfile `parent` MUST exactly
equal the existing parent's `subject_type`.

The child key is derived exactly as follows:

1. Normalize `subject_title` with the title algorithm above.
2. Construct `[scope_id, parent_subject_key, subject_type, normalized_title]`.
3. Serialize it with
   `json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)`.
4. UTF-8 encode, compute lowercase hexadecimal SHA-256, and prefix it with
   `sub_`.

The same tuple MUST resolve to the existing child and MUST NOT create a
duplicate. The child MUST persist the actual `parent_subject_key` and accumulate
the traceable source IDs of accepted assertions that request it. Validation
MUST remain side-effect free; subject creation and source accumulation MUST
occur only in the successful `dream()` transaction. `replay()` retains subjects
and their parent links and MUST rebuild identical projection state.

## 8. Exact extraction and retract guards

For each deterministic predicate applicable to the resolved primary subject,
the implementation MUST call one shared `observe_field(card, predicate)`
function. Its returned pair `(values, retract)` is both the sole emission gate
and the sole declaration that this field updates projected/materialized state:

```text
explicitly_cleared = source_field in card.cleared_fields
raw_value = null if explicitly_cleared else card[source_field]
values = extraction_rule(raw_value)

if values is non-empty:
    retract = false
else if retract_guard == "never":
    retract = false
else if retract_guard == "explicit":
    retract = explicitly_cleared
else if retract_guard == "implicit":
    retract = true

emit ASSERT for every value
emit field-wide RETRACT iff retract
update nothing iff values is empty AND retract is false
```

Thus `explicit` retracts only through `cleared_fields`; `never` never retracts
through card extraction; and `implicit` retracts whenever extraction yields no
value, including an absent field, explicit null, or empty list. The same
`observe_field` return value MUST remain the one shared gate for RETRACT
emission and projection/materialization update.

## 9. Exact projection algorithm

Projection first rejects assertions whose predicate/subject pair is not in the
profile. It groups by `(scope_id, subject_key, predicate)`. All ordering is
ascending unless `max` is stated.

```text
rank(a) = (a.valid_from, human(a.origin), a.recorded_at, a.assertion_id)
human("human") = 1; human("model") = 0

for each group:
  if cardinality == APPEND:
    for each ASSERT ordered by rank:
      emit [valid_from, valid_from] point interval
    ignore no assertions; RETRACT is invalid at the correction boundary

  if cardinality == SET:
    active = map object_key -> open interval
    for each effective instant:
      for each event at instant ordered by rank:
        if ASSERT and object_key not active: open it
        if ASSERT and object_key already active:
          append its assertion_id and union its evidence into the live interval
        if RETRACT(object_key) and object_key active: close it at instant
        if RETRACT("*"): close every active key at instant
    emit remaining open intervals

  if cardinality == SINGLE:
    active = optional open interval
    for each effective instant:
      events = all events at instant
      conflicts_resolved +=
        max(0, count(distinct ASSERT object_key in events) - 1)
      winner = max(events, key=rank)
      if winner is RETRACT: close active at instant, if any
      else if active.object_key == winner.object_key:
        append every same-key supporting ASSERT at that instant in stable rank
        order and union its evidence (MUST NOT close and reopen)
      else:
        close active at instant, if any
        open winner at instant and attach every same-key supporting ASSERT
    emit remaining open interval
```

The projector MUST return `conflicts_resolved` per `(scope_id, predicate)`,
including zero values for registered predicates in each projected scope, and
the store MUST replace the persisted statistics with that result.

## 10. Exact query semantics

All result orderings below are normative and every value result includes the
opening assertion ID, `supporting_assertion_ids`, origin, effective interval,
opening assertion `recorded_at`, value, and the ordered unique source IDs
accumulated from all supporting assertions. It also includes ordered
`source_refs` with `source_id`, nullable `uri`, `status`, and nullable
`revoked_at`, plus INV-11 aggregate `evidence_status`. Revocation state is
resolved at query time from persisted source lifecycle data; it MUST NOT alter
the interval projection.

Every comparison of a text ordering key MUST be locale-independent and ordered
by its UTF-8 byte sequence. This applies to every text component of a normative
rank or query order, including `subject_key`, `predicate`, `object_key`,
`assertion_id`, `card_id`, and counter names. PostgreSQL implementations MUST
use `COLLATE "C"` (or an exactly equivalent bytewise collation) for those
`ORDER BY` expressions; SQLite implementations MUST use `BINARY` or its
equivalent. A database or operating-system locale MUST NOT change any result.

- `current(scope, subject, predicate)`: validate the predicate. For SINGLE/SET,
  return all intervals with null `valid_to`, ordered by `object_key`. For APPEND,
  return the one point with maximum `(valid_from, assertion_id)`, or empty.
- `timeline(scope, subject, predicate)`: return every interval ordered by
  `(valid_from, object_key, assertion_id)`.
- `at(scope, subject, predicate, t)`: for SINGLE/SET return intervals where
  `valid_from <= t` and (`valid_to` is null or `t < valid_to`), ordered by
  `object_key`. For APPEND return the latest point with `valid_from <= t`.
- `by_person(scope, person_id)`: select subjects having a current interval
  whose predicate has `object: person` and whose object key equals the
  canonical JSON person ID. It returns every subject type named by such a
  predicate, including primary and non-primary types; it MUST NOT return a
  subject type merely because that type is related to a matching subject.
  Return distinct materialized subjects ordered by `subject_key`.
- `list_matters(scope)`: despite its compatibility name, return every
  materialized subject whose type equals `SchemaProfile.primary_subject.type`
  in the scope, ordered by `subject_key`. It MUST exclude all non-primary child
  types and MUST NOT assume a concrete type or status name.
- `completion(scope)`: if no completion config or no subjects, return
  `{completed: 0, total, ratio: 0.0}`. Otherwise count distinct subjects whose
  current configured predicate value is in canonical `completed_values`, and
  return `ratio = completed / total`.

## 11. Atomic ingest and replay

For a batch, card idempotency checks, subject writes, deterministic assertion
writes, distillation queue writes, projection replacement, statistics
replacement, and memory-card replacement MUST occur in one transaction. Any
failure MUST roll back the whole batch.

For `add_records`, observation-ledger writes, source lifecycle writes,
Record-derived card ingest, projection replacement, and non-backfill sync
position updates MUST share that transaction. Gateway I/O MAY occur before it.
An exact `(scope_id, record_id, canonical Record payload hash)` observation
already in the ledger MUST be filtered before gateway access and MUST be a
complete no-op. The same `record_id` with a changed canonical payload is a new
observation, as required by INV-11.

`replay(scope_id)` MUST retain assertions and subjects, delete/replace all
intervals, projection statistics, and memory cards for the scope, and rebuild
them using sections 9 and 10. Canonical JSON snapshots before and after MUST be
byte-identical.

## 12. Golden conformance YAML format

Each `spec/conformance/*.yaml` file contains one mapping:

| Field | Meaning |
| --- | --- |
| `case_id` | Unique stable kebab-case ID. |
| `title` | Human-readable title. |
| `invariants` | Non-empty list containing `P1`..`P9` and/or `INV-1`..`INV-11`. |
| `schema_profile` | Built-in profile ID resolved from package `matterhorn.schemas`, or an inline profile object. |
| `scope_id` | Scope under test. |
| `clock` | Ordered RFC 3339 instants injected for new cards, accepted semantic assertions, and corrections. |
| `cards` | Ordered EpisodeCard mappings. |
| `record_batches` | Optional ordered `{records,cursors?,backfill?}` batches passed to `add_records`. |
| `record_model_responses` | Optional ordered closed Record-to-card responses, one for each batch containing unseen non-revoked Records. |
| `corrections` | Ordered Correction mappings, default `[]`. |
| `model_responses` | Optional ordered list of closed response objects returned once per queued card during `dream()`. Absence means the semantic path is not run. |
| `expect_error` | Optional validation/error substring. If present, the case succeeds only on that rejection. |
| `expect.assertions` | Expected assertion mappings. |
| `expect.intervals` | Expected interval mappings; `supporting_assertion_ids`, when compared, is an order-sensitive exact list. |
| `expect.queries` | `{name,args,result}` query checks. |
| `expect.subject_count` | Optional exact subject count. |
| `expect.conflicts_resolved` | Optional predicate-to-count map. |
| `expect.gate_statistics` | Optional exact `{scope_id, accepted, rejections}` counter object. |
| `expect.dream_report` | Optional partial field mapping checked against the first `dream()` report. |
| `expect.second_dream` | Optional partial field mapping checked after identical card re-ingest and a second `dream()`. |
| `expect.record_reports` | Optional ordered partial mappings checked against first-pass `add_records` reports. |
| `expect.second_record_reports` | Optional ordered partial mappings checked after exact Record re-ingest. |

For assertions and intervals, each expected mapping declares its compared
fields. The runner projects each actual item onto exactly those fields, then
compares **order-insensitive exact multisets**: neither an extra nor a missing
projected mapping is allowed. Nested `supporting_assertion_ids` and query
`source_ids` lists remain order-sensitive. Datetimes use canonical UTC form.
Query results are order-sensitive according to section 10. Every case runner
MUST also re-ingest the same card and Record batches and compare a canonical
whole-store snapshot including observation ledger, source lifecycle, and sync
positions, then invoke replay and compare it again. Error cases MUST verify the
transaction left the scope empty.

## 13. Distillation, prompt, and gateway contract

`LlmGateway` has exactly one required operation:

```text
complete(system: string, user: string, response_schema: JSON object) -> string
```

The default gateway MUST be `NullGateway`, which raises if called.
`OpenAICompatibleGateway` and `AnthropicGateway` MUST construct their HTTP
clients lazily. Importing Matterhorn MUST NOT construct a client, resolve a
host, or make a network request.

The write path MUST derive the semantic prompt solely from the active
`SchemaProfile`. It MUST enumerate every declared subject type with its nullable
parent relationship and primary status, and only predicates whose `extraction`
is `semantic`, with each predicate's declared subject type, object type, and
nullable `value_domain`. It MUST NOT contain profile-domain vocabulary. The
prompt MUST demand one closed JSON object whose candidate objects have exactly
these fields:

```text
{
  "candidates": [
    {
      "subject_key": optional string or null,
      "subject_type": string,
      "parent_subject_key": optional string or null,
      "subject_title": optional string or null,
      "predicate": string,
      "operation": "ASSERT" | "RETRACT",
      "object_value": any JSON value,
      "valid_from": RFC 3339 datetime,
      "source_ids": unique array of strings,
      "confidence": number in [0,1]
    }
  ]
}
```

No additional top-level or candidate fields are allowed. `subject_key`,
`parent_subject_key`, and `subject_title` are the only optional candidate
fields; every other listed field is required. For an existing subject,
`subject_key` is required in practice and the two creation fields SHOULD be
absent. For child creation, `parent_subject_key` and a non-empty normalized
`subject_title` are required in practice; the gate replaces any supplied
`subject_key` with the section 7.1 derivation. The schema's predicate and
subject fields remain strings so that the validation gate can classify
attempted unregistered and wrong-mode writes with their specific reason codes;
the prompt's enumerated registry is the model-facing closed vocabulary.

`ingest()` MUST NOT call a gateway. In the same transaction as deterministic
ingest, each newly accepted card MUST be inserted once into `distill_queue`
with its resolved subject identity. Re-ingesting an identical card MUST NOT
enqueue a duplicate.

`dream(scope_id, limit)` MUST visit queued cards in ascending `card_id` order.
For each card it MUST build the profile-derived prompt, call the gateway, and
apply section 14. Gateway or processing exceptions MUST increment that queue
item's attempt counter, retain the item with a diagnostic, and MUST NOT change
assertions, projections, or gate counters. A successfully gated response,
including a response with zero accepted candidates, MUST atomically:

1. create or update every requested, gated child subject using section 7.1;
2. append every new accepted candidate as an origin `model` Assertion using the
   section 6 ID algorithm;
3. increment persistent gate counters;
4. remove that queue item; and
5. replace projection statistics, intervals, and materializations.

Those five steps MUST occur in one store transaction. Gateway I/O MAY occur
before that transaction. A second `dream()` after the item was successfully
drained MUST be a no-op. If an item is deliberately retried with identical
model output, assertion IDs MUST make every duplicate assertion a no-op.
Accepted-candidate `recorded_at` MUST use the injectable dream wall clock,
consumed once per accepted assertion.

`DreamReport` MUST include both `new_assertions` and `new_subjects`. Each counts
rows newly inserted during that invocation, not accepted candidates. A fully
drained second invocation MUST report zero for both.

## 14. Validation gate

The implementation has the right and obligation to reject model output.
Rejections MUST NOT abort other valid candidates from the same response.
A malformed top-level response rejects that response only and MUST NOT abort
other queued cards. Rules MUST be applied in this order:

| Order | Reason code | Normative rejection rule |
| --- | --- | --- |
| 1 | `UNPARSEABLE` | The response is not valid JSON or does not conform to the closed generated response schema. Reject the whole response as one rejection. |
| 2 | `UNREGISTERED_PREDICATE` | `predicate` does not exist in the active profile. |
| 3 | `NOT_SEMANTIC` | The predicate exists but has `extraction: deterministic`; a model MUST NOT write it. |
| 4 | `SUBJECT_TYPE_MISMATCH` | The predicate is not registered for the candidate's `subject_type`. |
| 5 | `UNKNOWN_PARENT_SUBJECT` | A child-creation request's `parent_subject_key` is absent, null, or does not name an existing subject in the originating scope. |
| 6 | `INVALID_SUBJECT_PARENT` | The requested child `subject_type` is primary, has no declared parent, or its declared parent type differs from the actual parent subject's type. |
| 7 | `MISSING_SUBJECT_TITLE` | A child-creation request's `subject_title` is absent, null, or normalizes to the empty string. |
| 8 | `UNKNOWN_SUBJECT` | A non-creation candidate's `(subject_key, subject_type)` does not exist in the originating scope. |
| 9 | `NO_SOURCES` | `source_ids` is empty. |
| 10 | `SOURCE_NOT_TRACEABLE` | Candidate `source_ids` is not a subset of the originating card's `source_refs[].source_id`. A model MUST NOT invent or borrow evidence. |
| 11 | `VALUE_OUT_OF_DOMAIN` | An ASSERT value is not exactly equal to a member of the predicate's non-null `value_domain`. |
| 12 | `LOW_CONFIDENCE` | The predicate has `semantic_filter: conservative` and confidence is below `semantic.conservative_confidence_threshold`. Conservative means that doubt MUST be dropped. |
| 13 | `VALID_FROM_OUT_OF_WINDOW` | `valid_from` is outside the originating card's inclusive temporal window defined below. |

The card temporal window starts at `occurred_at` when present, otherwise at
00:00:00 UTC on `date`. It ends at `last_active_at` when present, otherwise at
the later of the start and 23:59:59.999999 UTC on `date`. Both bounds are
inclusive.

The gate MUST return a structured `GateReport` containing accepted candidates,
rejections with reason and candidate when parseable, accepted count, rejected
count, and per-reason counts. The store MUST accumulate accepted and
per-reason rejection counters by scope. `gate_statistics(scope_id)` MUST
return `{scope_id, accepted, rejections}` without calling a gateway.

## 15. Protocol surfaces

All transports MUST delegate to the same application service used by the SDK;
they MUST NOT duplicate identity, extraction, correction, projection, or query
logic. Transport errors MUST be returned as structured `{code,message}` errors
and MUST NOT expose Python tracebacks as protocol results.

The MCP server MUST expose exactly these eight tools:

| Tool | When an agent uses it |
| --- | --- |
| `add_episode_cards` | After a conversation, persist evidence-backed episode observations. |
| `add_records` | Ingest raw communication Records through gated card extraction. |
| `query_current` | Read value(s) currently true for one subject and predicate. |
| `query_timeline` | Explain changes and supporting evidence over time. |
| `query_at` | Reconstruct what was true at an effective-time instant. |
| `query_by_person` | Find current subjects related to a person identifier. |
| `list_matters` | Discover primary subjects available in a scope; the compatibility name has no concrete domain semantics. |
| `correct` | File an origin-human assertion when a human says memory is wrong. |

It MUST be launchable as `mh mcp` and `python -m matterhorn.mcp` over stdio.
Every tool MUST declare typed inputs and a typed `{ok,data,error}` output.
The `mcp` installation extra MUST require the official SDK version range
`mcp>=1.27,<2`. The server MUST use that SDK directly and MUST NOT silently
substitute a compatibility server or look-alike protocol. If the SDK is
missing, importing the MCP server MUST raise an actionable `ImportError` that
names the `matterhorn[mcp]` extra.

`mh dream` MUST treat `--api-key` and `--base-url` as explicit overrides.
Without an API-key override it MUST read `MATTERHORN_API_KEY` first, then
`OPENAI_API_KEY` for `openai-compatible` or `ANTHROPIC_API_KEY` for
`anthropic`. Without a base-URL override it MUST read
`MATTERHORN_BASE_URL`. Credentials MUST NOT be required as command-line
arguments, and `mh dream --help` MUST document these environment variables.
`mh extract` MUST expose the same provider settings and MUST run Record
extraction followed by normal deterministic ingest. It MUST also expose opaque
per-container cursor updates and a `backfill` mode. `mh sync-status` MUST print
the stored per-container watermark/cursor positions without invoking a model.
The same command MUST expose the pure ReMe and OpenViking digest adapters as
`--adapter reme` and `--adapter openviking`; those paths map directly to a
validated EpisodeCard and MUST NOT configure or call a gateway.

The REST app factory MUST expose OpenAPI and these endpoints:

```text
GET  /healthz
POST /v1/add_episode_cards
POST /v1/add_records
POST /v1/query_current
POST /v1/query_timeline
POST /v1/query_at
POST /v1/query_by_person
POST /v1/list_matters
POST /v1/correct
```

Each POST body and response MUST have a Pydantic contract. `mh serve` MUST
launch the app. MCP and REST read handlers, their shared service, and
`matterhorn.query` MUST NOT import or transitively reach `matterhorn.distill`.
Installing a gateway that raises on every call and invoking every read tool and
read endpoint MUST succeed.

## 16. Record-to-card extraction

The built-in Record extractor is a P1 write-path component and MUST use the
same `LlmGateway.complete(system, user, response_schema)` SPI as semantic
distillation. Its input is the closed section 3.1 Record contract.
`ChatMessage{message_id,sent_at,sender,content}` is a deprecated Python alias
only and is not a protocol input.

The response schema and prompt MUST be derived from the active
`SchemaProfile`. In addition to required card `date`, `title`, and
`source_ids`, the extractor MUST expose only EpisodeCard fields named by the
profile's deterministic `source_field` values plus temporal metadata,
and `cleared_fields`. A model MUST NOT supply `subject_key` or `thread_id` for
Record input. When a profile predicate declares a non-null `value_domain`, an
extracted field value for that predicate MUST equal one domain member.

Every proposed card MUST pass the ordinary closed EpisodeCard validation.
Rejection of one card MUST NOT abort other valid cards from the response. A
malformed response is one counted `UNPARSEABLE` rejection. Per-card rejection
reasons are `NO_SOURCES`, `SOURCE_NOT_TRACEABLE`, `FIELD_NOT_IN_PROFILE`,
`VALUE_OUT_OF_DOMAIN`, and `CARD_VALIDATION_FAILED`.

Source validation MUST call the same implementation used by section 14:
`source_ids` MUST be non-empty and MUST be a subset of the `record_id` values
in the exact input window. Accepted IDs are replaced by SourceRefs copied from
the corresponding Records, including the readable content excerpt and URI;
the extractor MUST NOT synthesize evidence.

For each accepted candidate, construct `card_payload` from the validated
candidate, scope, copied SourceRefs, and deterministic thread boundary.
Construct `observations` as the lexicographically sorted array of
`[record_id, sha256(canonical Record JSON)]` for cited Records. The exact card
ID is `rec_` plus SHA-256 of canonical JSON:

```text
{"schema": profile.schema,
 "scope_id": scope_id,
 "card": card_payload,
 "observations": observations}
```

This derivation is independent of response slot and unrelated window members.
An exact overlapping window is idempotent; any edit changes the canonical
Record hash and therefore produces a new observation card even when the
extracted fact is unchanged.

For cited Records, define each boundary as `thread_id` when non-null, otherwise
`record_id`. One unique boundary becomes the card's `thread_id`. Multiple
boundaries become `threads:` plus SHA-256 of their sorted canonical JSON array.
Section 7 then makes the thread the default matter boundary without model
involvement.

## 17. Slack adapter and incremental sync

### 17.1 Slack normalization

`matterhorn.adapters.slack` MUST be pure and deterministic. It MUST accept
`conversations.history` message objects and Events API message payloads and
MUST NOT call Slack or an LLM.

- Identity MUST be `record_id = channel + ":" + ts`; for a delete event use
  `channel + ":" + deleted_ts`. Bare `ts` MUST NOT be used.
- `user` maps to a human author. Presence of `bot_id`/`bot_profile` maps to a
  bot; `app_id` without a bot maps to an app. A conversational message with no
  traceable author identity MUST fail.
- `blocks[].type=rich_text` is the preferred content source. Its nested
  sections, lists, quotes, preformatted text, users, channels, links, emoji,
  and broadcasts MUST be rendered deterministically. Top-level `text` is the
  fallback; it MUST unescape `&amp;`, `&lt;`, and `&gt;` and make Slack user,
  channel, and link tokens readable.
- `thread_ts`, `parent_user_id`, `thread_broadcast`, `edited.ts`, reactions,
  and files MUST map to the corresponding Record fields. Reaction `count` is
  authoritative even when Slack supplies only a partial user list.
- Given workspace domain `W`, the message URI MUST be
  `https://W/archives/<channel>/p<ts-without-dot>`.
- System/non-conversational subtypes, including channel/group join, leave,
  archive, rename, topic, purpose, permission, pin, reminder, and hidden
  bookkeeping events, MUST NOT become evidence. Unknown subtypes MUST be
  filtered closed.
- `message_changed.message` is the updated observation. `message_deleted`
  identifies the original only by `channel + deleted_ts` and omits its author
  and content; a pure adapter MUST require the caller's cached prior Record and
  MUST fail rather than fabricate those fields.

### 17.2 Watermarks, cursors, and backfill

The Store MUST persist one `SyncPosition{scope_id,container_id,watermark,cursor}`
per container. `watermark` is the maximum of `sent_at`, `edited_at`, and
`revoked_at` among newly processed non-backfill observations. `cursor` is
opaque host/provider state and MUST NOT be parsed by the engine.

Normal `add_records` advances each affected position monotonically and stores
the supplied cursor; when no cursor is supplied it MUST retain the existing
opaque cursor. Backfill processes unseen observations but MUST NOT move the
position. Re-ingesting an exact overlapping window MUST be filtered by the
observation ledger before gateway access and MUST change no card, assertion,
interval, materialization, source lifecycle row, or sync position. Thus the
deterministic chain `record_id + observation -> card_id -> assertion_id`
preserves P9 and INV-2 end to end.

## 18. Deterministic digest adapters

ReMe and OpenViking adapters MUST be pure deterministic mappings and MUST NOT
import or call an LLM gateway. Each adapter module MUST state its exact
supported normalized input shape. Repeating an identical payload MUST produce
an equal EpisodeCard with an equal ID.

Both mappings MUST be reachable without Python glue through `mh extract` as
specified in section 15.

Because the upstream public formats are extensible file/overview formats, the
supported mappings are best-effort and lossy. ReMe Markdown content and
OpenViking overview content map to `progress`; unlisted metadata, relations,
chunks, and wikilinks are not part of the card contract. A ReMe input without
non-empty `frontmatter.sources`, or an OpenViking input without non-empty
`metadata.source_refs`, MUST raise an error. An adapter MUST NOT fabricate a
source from a file path, digest ID, or overview URI.

## 19. PostgreSQL Store and consistency boundary

PostgreSQL support MUST use psycopg v3 and be distributed in the `postgres`
installation extra. It is a second implementation of the same Store SPI, not a
service-specific fork.

The Store layer exclusively owns physical SQL, driver connections, parameter
styles, database-native JSON and timestamp adaptation, deterministic text
collation, and backend-specific upsert syntax. Modules outside
`matterhorn.store` MUST call typed Store SPI methods and MUST NOT access a
store's connection or execute handwritten SQL. In particular, the query
service owns query semantics but delegates every physical read to the Store
SPI. Each backend MUST normalize returned JSON values, booleans, and instants
to the same language-level values and canonical UTC representation before
returning them across that boundary.

The connection MUST target one writable primary for the lifetime of an Engine.
The store MUST fail during initialization when
`transaction_read_only = on` or `pg_is_in_recovery() = true`. Replica reads,
read/write splitting, and transaction pooling that can move one Engine between
servers are unsupported because they violate INV-6.

Every ingest batch and every successful dream item MUST use one database
transaction for the complete sequences in sections 11 and 13. No projection or
materialization read may use another connection or replica inside that
sequence. PostgreSQL and SQLite MUST pass every case in section 12; a behavioral
difference is a defect, never an allowed backend variance.

## 20. Reference conformance harness

The command `mh conformance run [--suite DIR]` MUST execute every `*.yaml`
file in lexicographic filename order through the same runner used by Python
tests. It MUST print one `PASS` or `FAIL` line per case and a final stable
`SUMMARY passed=N failed=N total=N`. Exit status MUST be zero only when every
case passes, one when one or more valid cases execute but fail, and two when
the suite is unusable because its directory or a case file is missing,
unreadable, empty, or malformed.

The distributed wheel and sdist MUST contain `SPEC.md`, the conformance README,
and every golden YAML file so another implementation can use the installed
artifact as a language-neutral contract.

## 中文摘要

Matterhorn 是 agent 的 L3 时态记忆层：同步写路径把团队通信 Record 经受控
提取转成带证据的 EpisodeCard，再确定性地转成不可变断言并入队；异步
`dream()` 只按 SchemaProfile 生成封闭
语义候选，再经十三项验证闸门过滤。读取、REST 读端点和 MCP 读工具只用 SQL，
绝不调用模型。规范固定了幂等哈希、双时间轴、人工纠错优先级、拒绝原因统计、
事务一致性与可重放性。`spec/conformance` 的语言无关 YAML 是 Python 与内部
Java 实现共同的验收资产。M4 增加通用 Record 合同、Slack 纯适配、线程优先
身份、增量游标，以及编辑追加/删除撤销证据的 INV-11；查询保留结论但明确标出
证据是否已撤销。
