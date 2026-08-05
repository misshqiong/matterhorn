# Matterhorn Normative Specification

Version: 0.6.0

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

Change events do not create a second source of truth. They MUST be derived from
the previous and replacement interval projections during rebuild; assertions
remain the only authoritative asset under P7. The event log is an append-only,
deterministically keyed delivery artifact.

**Input admission rule.** A new input form is admissible if and only if it maps
losslessly to an `EpisodeCard` with traceable sources. An implementation MUST
reject an input that cannot satisfy that rule rather than weakening P5.

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
  issued after the synchronous card-application stage returns MUST see that
  committed projection.
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
  assertion candidates with a distinct non-null `observation_id` and
  `recorded_at`; prior assertions MUST remain untouched. An unchanged-value
  origin-model candidate is nevertheless dropped by INV-16, while an actual
  projected-value change MUST persist the new assertion. A revoked Record MUST
  persist the revocation for its `record_id`, MUST NOT invoke card extraction,
  and MUST NOT mutate or delete any assertion, interval, or materialization.
  Every value query MUST expose each source's `uri`, `status` (`active` or `revoked`), and
  nullable `revoked_at`, plus aggregate `evidence_status`: `active` when none
  are revoked, `partially_revoked` when some are revoked, and `revoked` when
  all are revoked. A conclusion supported only by revoked evidence therefore
  remains queryable but is visibly flagged.
- **INV-12 — Reversible canonical subject merge.** An active subject merge
  MUST be an acyclic, provenance-bearing `source_subject_key →
  target_subject_key` edge. Projection and every subsequent write resolution
  MUST follow the complete edge chain to its canonical target without mutating
  or deleting subjects or assertions. Merged-away primary subjects MUST be
  hidden from matter reads and extraction anchors; unmerge MUST remove only the
  active edge and MUST restore the independently projected subjects. Replay
  MUST preserve the active edge set and produce byte-identical canonical
  projection state.
- **INV-13 — Conversation-scoped rolling extraction.** Record extraction units
  MUST contain exactly one `container_id` and MUST be processed in the
  deterministic order and boundary-preserving chunks defined by section 16.
  Each extractor call MUST receive the deterministic, retention-bounded raw
  context window defined by section 16.1; context complements and MUST NOT
  replace the canonical SubjectAnchor snapshot.
  The Engine MUST recompute canonical SubjectAnchors before every extractor
  call and MUST make every card accepted from earlier calls in the same
  `add_records` operation visible to that snapshot. Consequently, later
  chunks and conversations can attach to a matter born earlier in the same
  operation without mixing conversation evidence or waiting for another
  flush.
- **INV-14 — Evidenced subject-handle registry.** A handle binding MUST be a
  provenance-bearing, revocable assertion and MUST NOT be silently deleted.
  At most one active binding may exist for a
  `(scope_id, handle_type, normalized_value)` tuple. Active handles from every
  subject in a merge chain MUST read and look up as a union on the canonical
  subject; unmerge MUST restore the original per-subject view. Replay MUST
  reproduce the complete active and revoked binding state.
- **INV-15 — Deterministic identity routing.** The handle, thread, and shared-
  evidence rungs of section 23 MUST be reproducible from committed Store state
  and the accepted card alone. Model adjudication MUST occur only after those
  rungs miss, and an attach outcome MUST pass the independent deterministic
  candidate, evidence, and confidence gate. Replay MUST NOT rerun routing or
  any gateway call: persisted assertions already carry their resolved subject.
- **INV-16 — Change-only model assertion admission.** Before storing an
  origin-model ASSERT extracted from an EpisodeCard, the Engine MUST compare
  its `object_key` with the resolved subject's projected values for that
  predicate at admission time. A SINGLE assertion is redundant when its key
  equals the live value; a SET assertion is redundant when that member is
  already live; and an APPEND assertion is redundant when an identical-key
  point already exists. A redundant assertion MUST NOT be stored or emit an
  event and MUST increment `unchanged_dropped`. RETRACT, `cleared_fields`, and
  origin-human assertions MUST NOT be dropped by this rule.
- **INV-17 — Deterministic signal lane.** Signals MUST be derived at staging
  time from the staged record and static configuration alone, with no gateway
  call, and MUST be idempotent under replay: re-staging the same record MUST
  NOT duplicate or alter its signals. Critical-path linkage MUST be resolved
  only by the deterministic handle matcher of section 24.3 against committed
  Store state. Signal acknowledgement is terminal and MUST survive replay.
- **INV-18 — Zero-model briefing and watermark reads.** The briefing
  projection, hotness metrics, and every watermark-relative freshness
  computation MUST be pure functions of committed Store state plus the
  request window, MUST NOT invoke a gateway or mutate assertion state, and
  MUST return identical output for identical store state and window.
- **INV-19 — Evidenced acyclic goal graph.** The structure predicates of
  section 25 (`part_of`, `spawned_from`, `decision`) MUST be ordinary
  provenance-bearing assertions subject to every existing admission rule.
  Admission of a structure edge MUST reject self-reference, a target subject
  that does not exist in the same scope, and any edge that would create a
  cycle in the canonical active `part_of`/`spawned_from` graph at admission
  time. Graph neighborhoods, structural rollups, and root/descendant
  classification MUST be pure zero-model reads, and replay MUST reproduce
  the complete active and superseded edge state.

## 3. Message, Record, and EpisodeCard contracts

### 3.1 Minimal public Message

`Message` is the default public input. Unknown fields MUST be rejected. Its
closed contract contains exactly four required top-level fields and two
optional fields:

| Field | Type | Presence | Rule |
| --- | --- | --- | --- |
| `id` | string | required | Non-empty provider/native message identity. |
| `sender` | object | required | Exactly required non-empty `id` and optional nullable `name`; unknown fields are rejected. |
| `text` | string | required | Original human-readable message content. |
| `sent_at` | datetime | required | Original message time, under the canonical datetime rules. |
| `conversation_id` | string or null | optional | Provider conversation identity. |
| `reply_to` | string or null | optional | Provider/native parent message identity. |

These fields preserve the gate's traceability, time, and participant
requirements and therefore do not weaken P5. Before a Message becomes Record
evidence, its public `id` MUST be namespaced exactly as:

```text
scope_id + ":" + conversation_id + ":" + id   when conversation_id is present
scope_id + ":" + id                            otherwise
```

The resulting value is the Record `record_id` and eventual
`SourceRef.source_id`. The Record `container_id` is `scope_id:conversation_id`
when present and `scope_id` otherwise. A non-null `reply_to` becomes a thread
boundary under that same container namespace. Consequently, equal native IDs
from different scopes or conversations MUST remain distinct INV-5 evidence.

### 3.2 Record

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

### 3.3 EpisodeCard

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

### 3.4 SubjectAnchor

`SubjectAnchor` is write-path context supplied to Record extraction. Its closed
contract contains required `subject_key` and `title`, plus nullable `status` and
`last_active_at`. Before each extractor call, the Engine MUST derive anchors
from every currently materialized primary subject that is not merged away,
order them by newest projected activity first with UTF-8 bytewise ascending
`subject_key` as the tie-break, and retain at most the first 40. `status` and
`last_active_at` MUST be copied from the projected matter when available.
Anchor construction MUST NOT call a model. During one `add_records` operation,
each anchor snapshot MUST reflect every accepted card already applied from an
earlier extraction call in that operation.

## 4. SchemaProfile contract

A profile is a YAML or JSON object. Unknown fields MUST be rejected.

| Field | Type | Required/default | Rule |
| --- | --- | --- | --- |
| `schema` | string | required | Stable profile/version identifier. |
| `subjects` | non-empty array | required | Each entry has required unique string `type`, optional nullable `parent` naming another declared type, and boolean `primary` default `false`. At most one is primary; otherwise the first is primary. |
| `predicates` | non-empty array | required | Names MUST be unique. See below. |
| `identity` | object | default `{}` | Contains routing and merge policy. |
| `identity.merge_evidence.min_shared_sources` | integer >= 1 | default `2` | Absolute merge threshold. |
| `identity.merge_evidence.or_share_ratio` | number in `(0,1]` | default `0.5` | New-card evidence share threshold. |
| `identity.adjudication_confidence_threshold` | number in `[0,1]` | default `0.6` | Minimum confidence for a rung-4 attach decision. |
| `handle_patterns` | array | default `[]` | Ordered conservative structured-identifier patterns. Each entry has a required unique `handle_type`, required regular-expression `pattern`, and optional `normalization` options. |
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

Each `handle_patterns` entry's `normalization` object MAY contain
`strip_prefixes`, an ordered unique array of literal textual prefixes, and
`strip_leading_zeros`, a boolean defaulting to false. Patterns MUST compile as
regular expressions. `handle_type` values MUST be unique within a profile.
Pattern order is normative for deterministic scanning, although the resulting
bindings are deduplicated by their normalized routing key.

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
later admitted assertion that reconfirms the same live `object_key` MUST be
appended in stable rank order without closing or reopening the interval.
Origin-model reconfirmations from EpisodeCard ingestion are not admitted under
INV-16; origin-human affirmations and assertions from paths outside that
admission rule remain eligible support. `source_refs` MUST be the stable union
of evidence from every supporting assertion: traverse supporting assertions in
list order, preserve each assertion's source order, and retain the first
SourceRef for each `source_id`. Non-point intervals are half-open
`[valid_from, valid_to)`; null `valid_to` means open-ended.

A MemoryCard contains `scope_id`, `subject_key`, `subject_type`, title, a
profile-keyed current-value map, latest current `valid_from` as `updated_at`,
and sorted union of source IDs. It is disposable projection state.

A Correction contains `scope_id`, existing `subject_key`, `subject_type`,
registered `predicate`, `operation`, JSON `object_value`, optional
`object_key`, `valid_from`, and non-empty `source_refs`. It always emits origin
`human`. APPEND retraction MUST be rejected.

A SubjectMerge contains `scope_id`, `source_subject_key`,
`target_subject_key`, `valid_from`, and non-empty `source_refs`. It is an active
correction edge, not an assertion and not a destructive rewrite. Both subjects
MUST already exist in the scope, source and target MUST differ, and a source
that already has an active merge MUST be rejected until it is unmerged.
Following the target's active merge chain MUST NOT reach the source; an
operation that would create a cycle MUST be rejected atomically. A target MAY
itself be merged, in which case resolution follows the complete chain.

Unmerge MUST require non-empty `source_refs`, MUST reject a source without an
active merge, and MUST remove only that active edge. It MUST NOT delete or
rewrite either subject, any assertion, or any evidence. Card identity,
correction, semantic-write, and query paths that receive a merged subject key
MUST redirect to the canonical target.

A SubjectHandle contains `binding_id`, `scope_id`, `subject_key`, required
`handle_type`, `handle_value` preserving the exact matched or human-supplied
surface form, `normalized_value`, `origin` (`human`, `model`, or `system`),
non-empty ordered `source_refs`, and `bound_at`. Revocation adds nullable
`revoked_at`, nullable `revocation_origin`, and ordered
`revocation_source_refs`. A binding is active exactly when `revoked_at` is
null. Handle rows are historical assertions: bind appends a row, unbind marks
the active row revoked with mandatory provenance, and neither operation may
silently delete a row.

A ReviewItem contains `scope_id`, deterministic `review_id`, the complete
validated held `card_json`, a non-empty ordered `reasons` array,
`candidates_json` containing exactly the rich rung-4 candidates that were
offered, `created_at`, nullable `resolved_at`, and nullable `resolution_json`.
The row is pending exactly when `resolved_at` is null. The held card MUST NOT
be persisted as a subject, assertion, interval, MemoryCard, or distillation
queue item before resolution.

A ChangeEvent contains required deterministic `event_id`, `event_type`,
`scope_id`, `subject_key`, registered `predicate`, nullable JSON `old_value`
and `new_value`, `valid_from`, `recorded_at`, `origin`, and ordered unique
`source_ids`. Its traceability fields MUST come from the assertion that caused
the projected change. Event types are `matter_created`, `status_changed`,
`matter_completed`, `blocked`, `unblocked`, `decision_adopted`, and
`value_corrected`, plus `subject_merged` and `subject_unmerged`. Merge events
use the reserved predicate `subject_merge`; `subject_key` is the merged-away
source, and `old_value`/`new_value` are null/target for merge and target/null
for unmerge. Their ordered unique `source_ids` MUST come from the mandatory
operation `source_refs`.

For merge and unmerge events, `event_id` is lowercase hexadecimal SHA-256 over
canonical JSON for:

```text
[event_type, scope_id, source_subject_key, target_subject_key,
 valid_from_iso, source_refs]
```

`source_refs` retain request order and use the canonical datetime rules.
`recorded_at` does not participate in this correction-operation ID. Inserting
an equal derived event MUST remain idempotent.

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
deterministic card and assertion candidate whose admission is governed by
section 8.1.

## 7. Subject identity primitives

Normalize a title by lowercasing, replacing every Unicode punctuation or symbol
character with a space, collapsing whitespace, and trimming it.

Section 23 is the authoritative routing algorithm for every accepted card.
This section defines the deterministic matching and construction primitives
used by that ladder; it MUST NOT be interpreted as a competing priority order.

1. A trusted explicit `subject_key` from the direct EpisodeCard API or a
   connector-deterministic stamp uses or creates that exact subject before the
   ladder. A Record extractor's model-supplied anchor key is not trusted by
   this rule and is governed by section 23 rung 4.
2. For thread continuation, if `thread_id` is non-null, exact-match that thread boundary among subjects
   of the profile's primary type. On multiple matches choose lexicographically
   smallest key.
3. Shared-evidence matching computes source-ID overlap with every existing
   primary subject using the rule below. Normalized-title equality alone MUST
   NOT attach a card.
4. New-subject construction for a thread-bound card creates or reuses the exact key
   `sub_` plus the first 20 lowercase hex characters of SHA-256 over canonical
   JSON `[scope_id, primary_subject_type, "thread", thread_id]`. Persist the
   thread boundary on that subject.
5. Shared-evidence eligibility defines
   `shared = count(card source IDs intersect subject source IDs)` and
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
6. New-subject construction for a card with null `thread_id` creates the
   existing deterministic key derived from scope, primary type, normalized
   title, sorted source IDs, and card ID. It MUST NOT reuse another subject
   merely because the normalized titles are equal.

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

### 7.2 Canonical resolution after explicit merge

After the ordinary identity algorithm resolves a subject, the Engine MUST
follow active SubjectMerge edges until it reaches a subject without an outgoing
edge. The resulting key is canonical. New cards, corrections, and accepted
semantic assertions MUST write against that canonical key. This redirect MUST
preserve the canonical target's own title while accumulating the incoming
sources and thread boundaries. Active edges are acyclic by section 5, so
resolution MUST terminate deterministically.

### 7.3 Activity derivation

Subject activity is not stored as a lifecycle column. Where a caller needs an
active-versus-closed distinction, the Engine MUST derive it from the current
projected value of the profile's completion predicate: a subject is closed
exactly when that value is a member of `completion.completed_values`, and is
active otherwise. For `org-matters/v1`, this means current status `done`,
`completed`, or `closed` is closed. This derivation is zero-model and does not
change identity resolution.

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

### 8.1 Change-only assertion admission

For every candidate Assertion returned by `extract_card`, after subject
resolution and before `store.add_assertion`, the Engine MUST read only the
resolved subject's committed/projected store state and apply this rule:

- for `SINGLE`, an origin-model `ASSERT` is redundant when the current live
  value has the same `object_key`;
- for `SET`, an origin-model `ASSERT` is redundant when the same `object_key`
  member is already live; and
- for `APPEND`, an origin-model `ASSERT` is redundant when a projected point
  with the same `object_key` already exists for the subject and predicate.

Every redundant assertion MUST be dropped before persistence, MUST NOT create
or alter an interval, MemoryCard, or ChangeEvent, and MUST increment the
operation and persistent `unchanged_dropped` counters exactly once. The rule
MUST NOT drop a `RETRACT`, including a field-wide retraction produced by
`cleared_fields`, and MUST NOT drop an origin-human assertion. Human repetition
is an affirmation with provenance. The rule applies only to deterministic
card-ingest assertions; semantic `dream()` distillation and Correction writes
are outside this admission rule.

Sources from a dropped assertion are not lost as inputs: Record observations
and raw staging retain them under sections 11, 16, and 16.1. They MUST NOT be
attached to the subject, its live interval, or its MemoryCard through the
dropped assertion. Matterhorn timelines record **change**, not model
confirmation; this is an explicit evidence trade-off.

The check MUST execute in the same card-application transaction as assertion
persistence and projection replacement. It reads only projected Store state at
admission time. Cards and their extracted assertions are considered in stable
input and predicate order, so re-ingesting the same cards in the same order
reproduces the same admission outcomes. Exact duplicate `assertion_id`
handling remains the distinct INV-2 rule and MUST NOT increment
`unchanged_dropped`.

## 9. Exact projection algorithm

Before grouping, projection MUST resolve every assertion's persisted
`subject_key` through the active merge graph and project a transient copy under
the canonical key. The persisted assertion and its `assertion_id` MUST remain
unchanged. Assertions from every merged-away source then participate together
in the ordinary deterministic rank and conflict rules below. The canonical
materialized subject MUST union subject sources, assertion evidence, and thread
boundaries from the complete chain, while retaining the canonical target's
title.

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
  types and every merged-away primary subject, and MUST NOT assume a concrete
  type or status name. Each ergonomic Matter MUST expose `aliases`, the
  UTF-8-bytewise sorted, deduplicated titles of subjects that currently resolve
  to it, excluding a title equal to the canonical title.
- `completion(scope)`: if no completion config or no subjects, return
  `{completed: 0, total, ratio: 0.0}`. Otherwise count distinct subjects whose
  current configured predicate value is in canonical `completed_values`, and
  return `ratio = completed / total`.

## 11. Atomic ingest and replay

For a batch, card idempotency checks, subject writes, deterministic assertion
writes, distillation queue writes, projection replacement, statistics
replacement, and memory-card replacement MUST occur in one transaction. Any
failure MUST roll back the whole batch.

For `add_records`, atomicity is per successful extraction chunk rather than
per complete operation. The chunk's observation-ledger writes, source lifecycle
writes, Record-derived card ingest, projection replacement, and non-backfill
sync-position updates MUST share one short transaction. Gateway I/O MUST occur
before that transaction and while no store transaction is held. Revoked
Records MAY use a separate short bookkeeping transaction because they do not
invoke extraction. A failure MAY therefore leave earlier chunks committed;
P9 retry MUST skip those exact observations and retry the still-unobserved
chunk deterministically. An exact `(scope_id, record_id, canonical Record
payload hash)` observation already in the ledger MUST be filtered before
gateway access and MUST be a complete no-op. The same `record_id` with a
changed canonical payload is a new observation, as required by INV-11.

`replay(scope_id)` MUST retain assertions, subjects, active SubjectMerge edges,
and merge/unmerge events; delete/replace all intervals, projection statistics,
and memory cards for the scope; and rebuild them using sections 9 and 10.
Canonical JSON snapshots before and after, including the active merge set, MUST
be byte-identical. It MUST compare the retained prior interval set with the
replacement before committing the replacement, so an identical rebuild emits
zero new events.

### 11.1 Public receipts, persistent tasks, and flush

`Engine.add(scope_id, messages)` and `Engine.add_cards(scope_id, cards)` MUST
validate and persist a task, then return immediately without gateway access:

```text
{"accepted": integer, "task_id": string}
```

`accepted` is the number of validated input Messages or EpisodeCards. Task IDs,
creation instants, and status transitions MUST use the Engine's injectable
clock rather than an uninjectable wall-clock source. Task rows MUST be stored
by both SQLite and PostgreSQL and MUST survive Engine/process restart.

The default state is `pending`. `flush(scope_id)` MUST visit pending tasks plus
failed tasks whose `attempts` is less than five in stable creation order and
synchronously execute pending Message-to-Record,
Record-to-card extraction, deterministic card application, semantic
distillation, projection, and materialization. `add(..., wait=true)` and
`add_cards(..., wait=true)` MUST run that same flush path and return the
completed task result inline. `Engine.task(task_id)` and every task protocol
response have exactly:

```text
{
  "task_id": string,
  "status": "pending" | "running" | "completed" | "failed",
  "cards_produced": integer,
  "new_assertions": integer,
  "unchanged_dropped": integer,
  "attempts": integer,
  "last_error": string | null,
  "gate": {
    "accepted": integer,
    "rejected": {reason_code: count},
    "unchanged_dropped": integer,
    "handle_conflicts": integer,
    "route_handle": integer,
    "route_thread": integer,
    "route_evidence": integer,
    "route_model": integer,
    "route_new": integer,
    "route_review": integer,
    "route_disagreements": integer
  }
}
```

`unchanged_dropped`, `attempts`, and `last_error` are additive task-result
fields with compatible defaults of `0`, `0`, and null. Each failed flush run
MUST increment `attempts` once
and store a conservatively credential-redacted exception summary of at most
500 characters in `last_error`. A successful retry MUST retain the attempt
count, set status to `completed`, and clear `last_error`. A failed task with
five attempts MUST remain failed and MUST NOT be selected again. A semantic
`dream()` failure still makes its enclosing task fail and follows the same
retry policy.

Service-mode batching MUST be bounded by a maximum delay so a continuously
active conversation cannot starve. A quiet-triggered flush MAY additionally require a configured minimum number of accepted pending messages (default 1); the maximum-delay bound always overrides that minimum, so no message waits longer than the deadline. A scope is due when either the newest
pending Message time is at or before the quiet cutoff, or the oldest pending
or retryable Message task `created_at` is at or before the maximum-delay
cutoff. The default maximum delay is five minutes. It MUST be positive and
finite, is independent of the quiet period, and MUST NOT be weakened by newer
Messages arriving in the same scope.

The gate object MUST aggregate the task's Record-to-card and semantic gate
outcomes, section 8.1 unchanged-value drops, and section 23 routing counters
without borrowing prior scope counters. The TaskResult's top-level
`unchanged_dropped` MUST equal its gate's value. Repeating an identical
Message batch MUST create a new receipt, but the observation ledger and
assertion IDs MUST make the later completed task report zero new assertions.

`ingest()` is a deprecated Python alias for `add_cards()` and MUST retain its
receipt semantics. The synchronous card-application mechanism remains an
internal engine promise boundary, not the user's front door. `add_records`
remains importable as an advanced/internal integration entry.

Every completed `TaskResult`, including `wait=true` responses and
`GET /v1/tasks/{task_id}`, MUST include its `task_id`. An unknown task ID MUST
be a structured `404 NOT_FOUND`, never a transport traceback.

### 11.2 Projection-diff change events

After every projection rebuild, the implementation MUST compare the previous
interval set with the newly computed interval set. Only that comparison MAY
produce ChangeEvents; callers and transports MUST NOT append arbitrary events.
The minimum derivation rules are:

- the first projected interval for a subject emits `matter_created`;
- a changed current SINGLE predicate whose deterministic `source_field` is
  `status` emits `status_changed`, including the initial `null -> value`;
- entering a `SchemaProfile.completion.completed_values` member emits
  `matter_completed` in addition to `status_changed`;
- a current predicate whose deterministic `source_field` is `blocker`
  changing empty-to-non-empty or non-empty-to-empty emits `blocked` or
  `unblocked`;
- a newly projected interval for a semantic predicate emits
  `decision_adopted`; and
- a non-null current value changed by a winning origin-human assertion emits
  `value_corrected`, in addition to any more specific event above.

For a projected change, `old_value` and `new_value` MUST be the canonical
before/after current values. `valid_from`, `recorded_at`, `origin`, and
`source_ids` MUST come from the winning ASSERT or RETRACT. Source IDs preserve
the trigger assertion's SourceRef order with duplicates removed.

`event_id` is lowercase hexadecimal SHA-256 over canonical JSON for this exact
array:

```text
[event_type, scope_id, subject_key, predicate, old_value, new_value,
 valid_from_iso, recorded_at_iso, origin, source_ids]
```

The canonical JSON and instant rules are sections 6 and 10. Events MUST be
inserted into an append-only table keyed by `event_id`. The same ID and same
payload is a no-op; the same ID with another payload MUST fail. Consequently,
re-ingest and replay of equal projections emit zero new rows, and replay never
duplicates historical delivery artifacts.

`Engine.events(scope_id, since)` and
`GET /v1/scopes/{scope_id}/events?since=...` return events in
`(recorded_at,event_id)` byte order. `since` is an inclusive recorded-time
lower bound, deliberately permitting harmless overlap that consumers dedupe
by `event_id`.

Service mode MAY POST `{"events":[ChangeEvent,...]}` batches to one configured
webhook URL. Delivery MUST mark a batch delivered only after a successful HTTP
response, so a crash between response and acknowledgement can repeat a batch
(at-least-once). Each dispatch cycle MUST use a bounded three-attempt
exponential backoff. Consumers MUST deduplicate by deterministic `event_id`.
Embedded mode does not run a webhook loop.

### 11.3 Scope export and import

The ownership envelope is one JSON document with this closed top-level shape:

```text
{
  "format": "matterhorn-scope-export",
  "version": 1,
  "scope_id": string,
  "schema_profile": {"id": string, "version": sha256},
  "subjects": [ExportSubject, ...],
  "assertions": [Assertion, ...],
  "source_states": [ExportSourceState, ...],
  "events": [ChangeEvent, ...],
  "merges": [SubjectMerge, ...]
}
```

`schema_profile.id` is the profile's `schema` identifier.
`schema_profile.version` is SHA-256 of canonical JSON for the complete locally
validated profile. Subjects include identity, parent, source-ID, and thread-ID
state. Source states retain URI and revocation time. Assertions retain their
original `origin`, so human corrections survive without reinterpretation.
Events are included as append-only derived delivery history. Intervals,
projection statistics, and MemoryCards MUST NOT be exported because they are
disposable projections. `merges` is additive to the version-1 envelope; an
older version-1 envelope with no `merges` member MUST be read as an empty list.

Import MUST accept only an empty target scope. It MUST resolve the named schema
profile locally and require the same version hash; it MUST refuse an
unavailable or mismatched profile with a clear error rather than using the
envelope as an untrusted schema definition. Import MUST atomically restore
subjects, assertions, source state, active merges, rebuild projection without
event emission, and restore the deterministic event log. It MUST reject a
merge referencing an absent subject, a duplicate active source edge, or a
cyclic graph. A following replay MUST produce byte-identical intervals,
MemoryCards, query answers, active merges, and ownership export and emit zero
new events.

## 12. Golden conformance YAML format

Each `spec/conformance/*.yaml` file contains one mapping:

| Field | Meaning |
| --- | --- |
| `case_id` | Unique stable kebab-case ID. |
| `title` | Human-readable title. |
| `invariants` | Non-empty list containing `P1`..`P9` and/or `INV-1`..`INV-16`. |
| `schema_profile` | Built-in profile ID resolved from package `matterhorn.schemas`, or an inline profile object. |
| `scope_id` | Scope under test. |
| `clock` | Ordered RFC 3339 instants injected for task creation, flush retention references, new cards, accepted semantic assertions, and corrections. |
| `cards` | Ordered EpisodeCard mappings. |
| `message_batches` | Optional ordered `{messages}` batches passed through `add`, each followed by `flush`. A batch MAY instead declare `flush: {mode: quiet, at, quiet_period_minutes, max_batch_delay_minutes}` to exercise service-mode due selection at an injected instant. |
| `message_model_responses` | Optional ordered closed Message/Record-to-card fixture responses, one per extractor call made by message batches. |
| `record_batches` | Optional ordered `{records,cursors?,backfill?,batch_size?,purge_staging_at?}` batches passed to `add_records`; the optional RFC 3339 instant runs retention purge immediately before that batch. |
| `record_model_responses` | Optional ordered closed Record-to-card responses, one per extractor call over unseen non-revoked Records. |
| `corrections` | Ordered Correction mappings, default `[]`. |
| `merge_operations` | Optional ordered merge/unmerge mappings. Each contains `operation`, source key, merge-only target key, `valid_from`, non-empty `source_refs`, and optional `expect_error` for an operation-level rejection. |
| `handle_normalization_cases` | Optional ordered `{handle_type,value,normalized_value}` mappings evaluated without persistence. |
| `handle_operations` | Optional ordered human bind/unbind mappings. Bind names `subject_key`, `handle_type`, `handle_value`, and non-empty `source_refs`; unbind names `handle_type`, `normalized_value`, and non-empty `source_refs`. |
| `adjudication_model_responses` | Optional ordered list of closed section 23 adjudication responses, consumed only by calls whose response schema is the routing schema. |
| `model_responses` | Optional ordered list of closed response objects returned once per queued card during `dream()`. Absence means the semantic path is not run. |
| `review_operations` | Optional ordered pending-review resolutions with `review_id`, `action`, optional `subject_key`, non-empty `source_refs`, and optional `expect_error`. |
| `expect_error` | Optional validation/error substring. If present, the case succeeds only on that rejection. |
| `expect.assertions` | Expected assertion mappings. |
| `expect.intervals` | Expected interval mappings; `supporting_assertion_ids`, when compared, is an order-sensitive exact list. |
| `expect.queries` | `{name,args,result}` query checks. |
| `expect.subject_count` | Optional exact subject count. |
| `expect.conflicts_resolved` | Optional predicate-to-count map. |
| `expect.gate_statistics` | Optional partial gate-counter object; declared fields compare exactly, including any declared route counters. |
| `expect.dream_report` | Optional partial field mapping checked against the first `dream()` report. |
| `expect.second_dream` | Optional partial field mapping checked after identical card re-ingest and a second `dream()`. |
| `expect.record_reports` | Optional ordered partial mappings checked against first-pass `add_records` reports. |
| `expect.second_record_reports` | Optional ordered partial mappings checked after exact Record re-ingest. |
| `expect.task_results` | Optional ordered partial task-result mappings for first-pass message batches. |
| `expect.second_task_results` | Optional ordered partial task-result mappings after exact message re-add. |
| `expect.flush_reports` | Optional ordered lists of partial FlushReport mappings produced by first-pass Message batch flush selection. |
| `expect.second_flush_reports` | Optional equivalent mappings after exact Message re-add. |
| `expect.extraction_calls` | Optional ordered extractor-call mappings. Each contains exact ordered `context` and `records` lists of partial Record mappings, proving context, unit, and chunk boundaries; omitted `context` asserts an empty list. |
| `expect.staging_purge_counts` | Optional exact ordered deleted-row counts for Record batches declaring `purge_staging_at`. |
| `expect.events` | Optional expected ChangeEvent mappings, compared as a partial-field exact multiset. |
| `expect.merge_count` | Optional exact active SubjectMerge count. |
| `expect.handle_bindings` | Optional partial-field exact multiset of all active and revoked SubjectHandle rows. |
| `expect.subject_handles` | Optional mapping from subject key to its exact active canonicalized handle list. |
| `expect.handle_lookups` | Optional ordered lookup mappings with `handle_type` or null, `value`, and an exact list of partial canonical SubjectHandle results. |
| `expect.review_items` | Optional partial-field exact multiset of pending or resolved ReviewItems. |
| `expect.adjudication_calls` | Optional ordered checks of the exact offered candidate subject keys and partial rich candidate payloads. |
| `expect.matters` | Optional partial-field exact multiset of ergonomic canonical Matters, including aliases. |
| `expect.export_replay_identity` | When true, the ownership export immediately before and after replay MUST be byte-identical. |
| `expect.replay_events_emitted` | Optional exact replay new-event count; event cases use zero. |

For assertions, intervals, and events, each expected mapping declares its compared
fields. The runner projects each actual item onto exactly those fields, then
compares **order-insensitive exact multisets**: neither an extra nor a missing
projected mapping is allowed. Nested `supporting_assertion_ids` and query
`source_ids` lists remain order-sensitive. Datetimes use canonical UTC form.
Query results are order-sensitive according to section 10. Every case runner
MUST also re-add the same Message, card, and Record batches and compare a
canonical whole-store snapshot including observation ledger, source lifecycle,
sync positions, active merges, and active and revoked SubjectHandle rows, then
invoke replay and compare it again.
Raw staging is intentionally excluded from replay-identity snapshots because
section 16.1 makes it expiring context rather than retained provenance.
Merge operations are applied once because an already-active source is
normatively rejected; their persisted state participates in both snapshot
comparisons. Human handle operations are also applied once because they are
historical corrections; their state participates in both snapshot comparisons.
Error cases MUST verify the transaction left the scope empty.

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

The internal synchronous card-application stage MUST NOT call a gateway. In
the same transaction as deterministic card application, each newly accepted
card MUST be inserted once into `distill_queue`
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
per-reason rejection counters by scope, plus `unchanged_dropped`,
`handle_conflicts`, `route_handle`, `route_thread`, `route_evidence`,
`route_model`, `route_new`, `route_review`, and `route_disagreements` from
section 23.
`gate_statistics(scope_id)` MUST return all of those fields with `scope_id`,
`accepted`, and `rejections` without calling a gateway.

## 15. Protocol surfaces

All transports MUST delegate to the same application service used by the SDK;
they MUST NOT duplicate identity, extraction, correction, projection, or query
logic. Transport errors MUST be returned as structured `{code,message}` errors
and MUST NOT expose Python tracebacks as protocol results.

The MCP server MUST expose exactly these nine tools:

| Tool | When an agent uses it |
| --- | --- |
| `add_messages` | Default write door: queue the section 3.1 minimal Message contract and return a receipt. |
| `add_cards` | Advanced write door for callers that already produce evidence-backed EpisodeCards; return a receipt. |
| `add_records` | Advanced/internal provider integration for normalized Records. |
| `query_current` | Read value(s) currently true for one subject and predicate. |
| `query_timeline` | Explain changes and supporting evidence over time. |
| `query_at` | Reconstruct what was true at an effective-time instant. |
| `query_by_person` | Find current subjects related to a person identifier. |
| `list_matters` | Default read door: discover ergonomic projected matters in a scope. |
| `correct` | File an origin-human assertion when a human says memory is wrong. |

It MUST be launchable as `mh mcp` and `python -m matterhorn.mcp` over stdio.
Every tool MUST declare typed inputs and a typed `{ok,data,error}` output.
The `mcp` installation extra MUST require the official SDK version range
`mcp>=1.27,<2`. The server MUST use that SDK directly and MUST NOT silently
substitute a compatibility server or look-alike protocol. If the SDK is
missing, importing the MCP server MUST raise an actionable `ImportError` that
names the `matterhorn[mcp]` extra.

The Python facade MUST expose `add`, `matters`, `flush`, `task`, `add_cards`,
`query.*`, `correct`, `merge_subjects`, `unmerge_subjects`, `bind_handle`,
`unbind_handle`, `subject_handles`, `handle_lookup`, `review_items`,
`resolve_review`, and
advanced/internal `add_records`. `matters(scope_id)` MUST return
projection-derived objects with at least `title`, `status`, `owners`,
`participants`, `blocked_by`, `next_step`, `due`, `subject_key`, and
`aliases`. Public matter-list objects MUST add `updated_at`, defined as the
newest canonicalized admitted assertion `recorded_at` for that subject, or null
when it has no assertion. It MUST advance when an admitted assertion adds new
evidence even when the business-effective current value is unchanged, as may
occur for an origin-human affirmation; a dropped section 8.1 model
reconfirmation MUST NOT advance it. Its computation MUST NOT call a gateway.

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

The CLI MUST additionally expose `mh init`, `mh add`, `mh matters`, `mh flush`,
`mh task`, `mh events`, `mh export`, `mh import`, `mh merge`, and
`mh unmerge`. `mh merge SCOPE SOURCE TARGET --reason TEXT --sender NAME` and
`mh unmerge SCOPE SOURCE --reason TEXT --sender NAME` MUST create
`console:<uuid>` provenance and invoke the same Engine operations as REST.
`mh add` MUST accept YAML/JSON from a file or stdin. `mh export SCOPE
[--out FILE]` MUST write the section 11.3 envelope; `mh import FILE` MUST
import it into an empty store.
`mh handles backfill SCOPE [--db PATH]` MUST scan retained evidence offline,
MUST NOT call a model, and MUST print `bound`, `skipped-conflict`, and
`already-bound` counts. Repeating it against unchanged state MUST bind zero new
rows.
`mh init
[--schema ID] [--db PATH]` MUST idempotently create the SQLite database and a
small `matterhorn.toml` containing default database, schema, scope, and
quiet-period settings, then print the next three runnable commands. CLI
commands MUST read that file so `--db` and `--schema` are not repeatedly
required.

The REST app factory MUST expose OpenAPI and these endpoints:

```text
GET  /healthz
GET  /v1/stream
POST /v1/scopes/{scope_id}/messages
POST /v1/scopes/{scope_id}/cards
GET  /v1/scopes/{scope_id}/stream
GET  /v1/scopes/{scope_id}/matters
GET  /v1/scopes/{scope_id}/query/current
GET  /v1/scopes/{scope_id}/query/timeline
GET  /v1/scopes/{scope_id}/query/at
GET  /v1/scopes/{scope_id}/query/by-person
GET  /v1/scopes/{scope_id}/events
GET  /v1/scopes/{scope_id}/export
POST /v1/scopes/{scope_id}/corrections
POST /v1/scopes/{scope_id}/merges
POST /v1/scopes/{scope_id}/merges/{source_subject_key}/unmerge
GET  /v1/scopes/{scope_id}/subjects/{subject_key}/handles
POST /v1/scopes/{scope_id}/subjects/{subject_key}/handles
POST /v1/scopes/{scope_id}/subjects/{subject_key}/handles/{handle_type}/{normalized_value}/unbind
GET  /v1/scopes/{scope_id}/reviews
POST /v1/scopes/{scope_id}/reviews/{review_id}/resolve
GET  /v1/tasks/{task_id}
```

The merge request contains `source_subject_key`, `target_subject_key`, and
non-empty `source_refs`; the unmerge request contains non-empty `source_refs`.
Request-shape failures MUST return 422, merge-state conflicts MUST return 409,
and missing subjects MUST return 404 through the normal structured error
envelope. Merge and unmerge events MUST appear in the ordinary events feed.

The handle bind request contains `handle_type`, `handle_value`, and non-empty
`source_refs`; unbind contains non-empty `source_refs`. The subject in the
unbind path is an ownership guard and MUST resolve to the active binding's
canonical subject. Matter detail responses MUST add active canonicalized
`handles`. All handle reads are P4 zero-model reads.

The review-resolution request contains `action` (`attach` or `new`), nullable
`subject_key`, and non-empty `source_refs`. `attach` requires an existing
subject key; `new` forbids one. A successful resolution returns the resolved
ReviewItem. Resolving an already resolved review MUST return a structured
409 conflict and MUST NOT ingest the card twice.

The stream endpoints are P4 reads over raw staging and MUST NOT call a model.
They accept `limit`, default 50 and capped at 200, and return newest-first
items with `scope_id`, `container_id`, display-name-or-ID `sender`, `sent_at`,
server-truncated `content` of at most 500 characters, `record_id`, and
`staged_at`. The unscoped endpoint spans every scope; the scoped endpoint
applies its path scope exactly. Revoked staged Records MUST be excluded.

The Console MUST present pending ReviewItems in its ledger-paper operating
surface with card title and summary, one attach button per offered candidate,
and a create-new action. It MUST use only the two public review REST endpoints.
The existing five-second wall poll MUST include the pending review count in its
change detector and refresh the Review section after a resolution.

The old `/v1/add_episode_cards`-style RPC endpoints MUST NOT be exposed, and
wire protocols MUST NOT retain legacy aliases. Each request and response MUST
have a Pydantic contract. `mh serve` MUST launch the app. MCP and REST read
handlers, their shared service, and
`matterhorn.query` MUST NOT import or transitively reach `matterhorn.distill`.
Installing a gateway that raises on every call and invoking every read tool and
read endpoint MUST succeed.

Only service mode has quiet-period scheduling. `mh serve` MUST run a background
loop that flushes a scope when either the newest pending Message in that scope
is at least N minutes old or the oldest pending-or-retryable Message task has
reached the independently configured maximum batch delay. N defaults to 10;
the maximum delay defaults to 5. `mh serve` and `mh console` MUST expose
`--max-batch-delay-minutes`, and `MATTERHORN_MAX_BATCH_DELAY` MUST be accepted
as a positive finite launch override. Embedded mode MUST remain host-driven
through `flush()` or `wait=true`. Service mode MUST also accept optional
`daily_flush_at = "HH:MM"` in UTC, from
`matterhorn.toml` or `mh serve --daily-flush-at`, and flush all pending scopes
once when that daily boundary is reached. Scheduler time MUST be injectable for
deterministic tests. v0.6 does not expose a general cron system.

## 16. Record-to-card extraction

The built-in Record extractor is a P1 write-path component and MUST use the
same `LlmGateway.complete(system, user, response_schema)` SPI as semantic
distillation. Its input is the closed section 3.2 Record contract.
`ChatMessage{message_id,sent_at,sender,content}` is a deprecated Python alias
only and is not a protocol input. The RecordExtractor operation is
`extract(*, scope_id, records, context, batch_size, anchors)`; all Engine calls
MUST supply the section 3.4 anchor list and the section 16.1 context list. A
legacy direct ChatMessage call MAY omit those keywords and retains its
permissive subject-key behavior.

For each `add_records` operation, after exact-observation filtering and removal
of revoked Records from extraction, the Engine MUST perform this orchestration:

1. Group active Records into conversation units by exact `container_id`. One
   extractor call MUST NOT contain Records from more than one unit.
2. Order units by their earliest `sent_at`, with UTF-8 bytewise ascending
   `container_id` as the tie-break.
3. Within a unit, order Records by `(sent_at, UTF-8 bytewise record_id)`. Group
   them by the section 16 boundary (`thread_id` when non-null, otherwise
   `record_id`) in first-record order, then pack complete groups into chunks of
   at most `batch_size`. A boundary group MUST NOT be split. If one group alone
   exceeds `batch_size`, it MUST be one oversized chunk.
4. Process units and their chunks serially. Immediately before each extractor
   call, recompute section 3.4 anchors in a short read transaction, release the
   store transaction, and then call the extractor. Immediately after the call,
   atomically persist that chunk's observation/source bookkeeping and run every
   accepted card through the ordinary gate, subject resolution,
   canonicalization, assertion, projection, materialization, and sync-position
   pipeline before computing the next snapshot. No extractor or gateway call
   may run while a store transaction is held.

The Engine MUST pass exactly one such chunk to each RecordExtractor call while
retaining the extractor Protocol signature and `batch_size` argument. A direct
SDK extractor call MAY retain its own boundary batching. `AddRecordsReport` and
the enclosing task gate MUST sum accepted cards, rejected cards, rejection
reasons, card IDs, emitted assertions, and `unchanged_dropped` across all
chunks exactly as if the operation had one response. Replay MUST rebuild only
from persisted cards/assertions and MUST NOT rerun any extraction call.

The response schema and prompt MUST be derived from the active
`SchemaProfile`. In addition to required card `date`, `title`, and
`source_ids`, the extractor MUST expose only EpisodeCard fields named by the
profile's deterministic `source_field` values plus temporal metadata,
`cleared_fields`, and optional `subject_key` when anchors are offered. A model
MUST NOT supply `thread_id` for Record input. When a profile predicate declares
a non-null `value_domain`, an extracted field value for that predicate MUST
equal one domain member.

The prompt MUST define investigating, diagnosing, developing, fixing, testing,
verifying, accepting, submitting, deploying, paying, and adjusting or
rescheduling as lifecycle progress on one underlying matter rather than new
matters. One purchase, incident, or change request MUST remain one matter from
start to finish.

When anchors are non-empty, the prompt MUST contain a “Known open matters”
section listing each offered `subject_key`, `title`, and nullable `status`. It
MUST instruct the model to attach by exact offered key when Records carry a
linking signal: shared identifiers (including order, ticket, flight, or issue
IDs, amounts, or names), a complementary status transition, close time
proximity plus the same topic, or lifecycle continuation. Because attachment
is an evidence-backed assertion, Records with no linking signal to any known
matter MUST omit `subject_key`; a separate matter can be merged later. The
prompt MUST require separate cards with their own `source_ids` when a call
touches several known matters. The closed-envelope instruction MUST retain a
literal two-card example with one attached card and one new card.

After decoding and before EpisodeCard validation, a modern Record extractor
MUST keep a model-supplied `subject_key` if and only if it exactly equals one
of the offered anchor keys. It MUST silently replace every other supplied key
with null so the card falls back to section 23 routing; it MUST NOT reject an
otherwise valid card for that fabrication. The retained key is a model-tier
suggestion only: section 23 rungs 1–3 MUST run first, and a disagreement loses
to the deterministic route and is counted. A deterministic connector-stamped
key, including the server-derived mail conversation key, MUST override any
model-supplied anchor key as rung 0. Legacy non-Record ChatMessage extraction
retains its existing permissive behavior.

Every proposed card MUST pass the ordinary closed EpisodeCard validation.
Rejection of one card MUST NOT abort other valid cards from the response. A
malformed response is one counted `UNPARSEABLE` rejection. Per-card rejection
reasons are `NO_SOURCES`, `SOURCE_NOT_TRACEABLE`, `FIELD_NOT_IN_PROFILE`,
`VALUE_OUT_OF_DOMAIN`, and `CARD_VALIDATION_FAILED`.

Source validation MUST call the same implementation used by section 14:
`source_ids` MUST be non-empty and MUST be a subset of the `record_id` values
in the exact union of the context and new-Record windows. Accepted IDs are
replaced by SourceRefs copied from the corresponding Records, including the
readable content excerpt and URI; the extractor MUST NOT synthesize evidence.
A card MAY cite only context Records. Such a card is legal evidence-backed
output and follows the same identity, observation-hash idempotency, gate, and
ingest rules as a card citing a new Record.

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

### 16.1 Raw staging and context windows

Both Store backends MUST provide a raw staging table keyed by
`(scope_id, record_id)`. It MUST retain at least `container_id`, nullable
`thread_id`, `sent_at`, author JSON, nullable content, kind, revocation state,
the complete validated Record payload, and `staged_at`. It MUST index
`(scope_id, container_id, sent_at)` and use locale-independent UTF-8 bytewise
text ordering, including `COLLATE "C"` on PostgreSQL ordering reads.

Both Stores MUST expose a bounded recent-staging read across containers with
an optional scope filter. It MUST exclude revoked rows and order by
`(staged_at DESC, sent_at DESC, record_id bytewise DESC)`, using `COLLATE "C"`
for the PostgreSQL record-ID ordering and `BINARY` or equivalent for SQLite.

`Engine.add()` MUST stage every validated Message-derived Record in the same
short transaction that enqueues its task. `Engine.add_records()` MUST stage
every validated Record in a short transaction before observation filtering or
extractor work begins. Staging is an idempotent upsert by
`(scope_id, record_id)`: the latest observation replaces the staged payload so
edited content is visible to later windows. Revoked Records MUST also be
staged, but context reads MUST exclude every staged row whose `revoked_at` is
non-null. Implementations MUST NOT backfill staging from historical task
payloads, observation rows, evidence, assertions, or other existing data;
staging fills organically from newly admitted traffic.

The default staging retention is seven days. The Engine MUST expose a
positive finite retention-days setting, and service and CLI launch surfaces
MUST resolve `MATTERHORN_STAGING_RETENTION_DAYS` as its override. At the start
of every `flush`, the Engine MUST execute a real `DELETE` for staged rows whose
`sent_at` is strictly earlier than `(flush reference instant - retention)`.
`mh staging purge [SCOPE] [--db ...]` MUST perform the same deletion using its
launch-time retention setting and print the deleted-row count. A staging purge
MUST NOT delete or mutate `evidence_sources`, assertions, intervals, subjects,
tasks, observations, or any other provenance or projection state.

Immediately before extracting a chunk, the Engine MUST read context from the
staging store under all of these rules:

1. Select the same `scope_id` and `container_id` as the chunk. If every Record
   in the chunk has one identical non-null `thread_id`, additionally require
   that exact `thread_id`; otherwise do not apply a thread filter.
2. Require `sent_at` to be strictly earlier than the chunk's earliest
   `sent_at`, at or after `(earliest sent_at - retention)`, not revoked, and not
   one of the chunk's `record_id` values.
3. Order candidates by `(sent_at, UTF-8 bytewise record_id)` oldest first.
4. Retain the newest tail while satisfying both default budgets: at most 20
   Records and at most 4000 total content characters. Equivalently, drop the
   oldest candidate until both limits hold. Nullable content contributes zero
   characters. Return the retained Records oldest first.

This selection is deterministic given staging state. An empty staging result
MUST produce the same extraction behavior as before context windows existed.
Anchors remain governed by sections 3.4 and 16 and MUST still be supplied.

The prompt MUST render a clearly separated context section before the new
Records and state exactly: "Prior conversation context, oldest first. Use it
to understand what the new Records continue. You MAY cite a context Record's
alias in source_ids when a card's claim rests on it." Context and new Records
MUST share one alias namespace, assigned context-first and then new-Record
order, so every accepted alias maps to one exact real Record. The traceability
gate MUST resolve citations across that complete union.

Replay MUST NOT rerun Record extraction. Consequently, staging contents and
purge timing MUST NOT affect card, assertion, projection, event, export, or
replay identity.

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
returning them across that boundary. Both backends MUST persist active
SubjectMerge edges with their complete ordered `source_refs` and `valid_from`;
enumeration MUST use UTF-8 bytewise source-key order (`COLLATE "C"` on
PostgreSQL and `BINARY` or equivalent on SQLite).

The connection MUST target one writable primary for the lifetime of an Engine.
The store MUST fail during initialization when
`transaction_read_only = on` or `pg_is_in_recovery() = true`. Replica reads,
read/write splitting, and transaction pooling that can move one Engine between
servers are unsupported because they violate INV-6.

Every direct EpisodeCard ingest batch and every successful dream item MUST use
one database transaction for the complete sequences in sections 11 and 13.
Record extraction uses the per-chunk transaction boundary in sections 11 and
16 so no gateway call holds the connection or store lock. No projection or
materialization read may use another connection or replica inside a committed
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

## 21. Message-to-matter evaluation

Message-to-matter extraction quality MUST be measured by the additive Phase 0
contract in `spec/eval/README.md`. The reference `mh eval run` harness executes
the current production write path against fresh stores and scores only
read-side results. Evaluation scores are measurements and MUST NOT be treated
as conformance gates. Distributed artifacts MUST include the eval README, case
YAML, and sibling scripted-response YAML.

For one case, `zero_model_route_rate` MUST be
`(route_handle + route_thread + route_evidence) /
(route_handle + route_thread + route_evidence + route_model + route_new +
route_review)`. Review-queued cards are therefore included in the denominator.
When the denominator is zero the metric is null. Aggregate evaluation MUST sum
the six counters before applying the same formula.

## 22. Subject handles

The topic library is the existing `subjects` table plus a `subject_handles`
index and the section 7.3 derived activity notion. An implementation MUST NOT
introduce a second entity parallel to Subject.

### 22.1 Entropy rule and shipped profile

Only structured identifiers MAY become handles. Bare numbers, amounts, and
person names are auxiliary evidence only and MUST NOT be handle types or
routing primary keys in shipped defaults. A false binding is worse than a
missed binding, so built-in patterns MUST favor precision over recall.

`org-matters/v1` ships conservative patterns for issue/ticket keys,
PR/MR references, explicitly prefixed order numbers, explicitly prefixed
invoice numbers, flight numbers, RFC Message-IDs, and explicitly labeled
thread IDs. The Engine and adapters MUST NOT hardcode any of those domains;
they consume only the active profile's `handle_patterns` configuration.

### 22.2 Exact normalization

Given one surface value and its profile pattern, normalization MUST execute in
this exact order:

1. Trim surrounding whitespace, then strip surrounding Unicode punctuation or
   symbol characters. Preserve the original input unchanged as `handle_value`.
2. Remove at most one configured textual `strip_prefixes` member by
   ASCII-case-insensitive comparison, then trim whitespace and surrounding
   punctuation or symbols again.
3. Remove leading `#` and `!` marker characters and following whitespace.
4. Convert ASCII `A` through `Z` to `a` through `z`; non-ASCII code points MUST
   remain unchanged.
5. Collapse every internal Unicode-whitespace run to one ASCII space and trim.
6. If and only if that pattern sets `strip_leading_zeros: true`, replace every
   maximal purely numeric segment with its base-10 spelling without leading
   zeros, using `0` when the segment contains only zeros. Numeric characters
   inside an alphanumeric segment are unchanged.

The result MUST be non-empty. `normalized_value` is the lookup and routing key;
normalization is deterministic and locale-independent.

### 22.3 Persistence, conflicts, and write-path maintenance

Storage MUST retain the SubjectHandle fields in section 5 and MUST enforce at
most one active row for each `(scope_id, handle_type, normalized_value)`, while
allowing an unbind followed by a new bind of the same tuple. Lookups and
ordered reads MUST use bytewise text ordering (`COLLATE "C"` on PostgreSQL and
`BINARY` or equivalent on SQLite). Both backends MUST evolve existing schemas
without requiring a destructive rebuild. Every Store method remains behind
the Store instance's reentrant lock.

After a Record-derived card is accepted and its subject has been resolved, the
Engine MUST scan the card title and only the cited input Records' `content`
using the active profile patterns. A title match uses the card's ordered
SourceRefs; a content match uses the matching cited Record's SourceRef. Matches
are grouped by `(handle_type, normalized_value)` and source provenance is a
stable first-seen union. The Engine binds each group to the resolved canonical
subject with `origin=system` and `bound_at` from the injectable clock, without
calling a model.

If the tuple already has an active binding to the same canonical subject, the
operation is `already-bound` and MUST NOT append a row. If it is actively bound
to a different canonical subject, first binding wins: the Engine MUST NOT
rebind or throw, MUST append nothing, and MUST increment `handle_conflicts` in
the operation report, persistent gate statistics, and enclosing task gate.
Exact Record re-ingest is filtered before scanning and therefore changes no
binding or counter.

Human `bind_handle` and `unbind_handle` operations require non-empty
`source_refs` and use `origin=human`. A human bind conflict is rejected rather
than displacing the active owner. Unbind revokes exactly the active matching
row and records `revoked_at`, `revocation_origin=human`, and the mandatory
revocation SourceRefs. A following bind may append a new active generation.

### 22.4 Merge projection, backfill, and phase boundary

Handle merge-union uses transparent canonicalization at read time. Persisted
bindings remain attached to their original subjects; `subject_handles` unions
all active bindings whose original subjects currently resolve to the requested
canonical subject, and `handle_lookup` resolves the bound subject through the
same active merge chain. This preserves original provenance, makes replay a
pure retained-state operation, and makes unmerge restore the original binding
view without synthetic revoke/rebind rows.

The offline backfill MUST walk retained assertion `SourceRef` excerpts and
stored MemoryCard titles/current evidence, apply the same profile patterns and
normalization, and bind to the subject owning that evidence with
`origin=system`. It uses the same first-binding-wins conflict behavior and
reports `bound`, `skipped-conflict`, and `already-bound`; unchanged repeated
runs MUST bind nothing new.

Phase 1 created, maintained, and exposed the handle registry. Section 23 now
uses that retained registry as the highest ordinary routing rung. The Record
extraction prompt and offered-anchor mechanism remain in place until a later
phase; only the authority of an extracted `subject_key` changes as specified
below.

## 23. Identity routing

Every accepted card MUST resolve through this priority ladder, with the first
hit winning. Human corrections, subject merge/split decisions, and human
handle bind/unbind assertions outrank every rung. A connector-deterministic
identity stamp, including the server-derived mail conversation key, is rung 0
alongside those human decisions and MUST be applied before the ladder. A
trusted explicit key supplied through the direct EpisodeCard API retains the
same existing override behavior. Neither rung-0 path is reported as a ladder
route.

1. **Exact handle hit, zero model.** Scan the card title and only its cited
   Record contents, represented by the copied SourceRef excerpts, with the
   active profile's ordered `handle_patterns` and section 22 normalization.
   Resolve every active binding through the active merge graph. If all matched
   bindings name one canonical subject, route to it even when section 7.3 says
   it is closed. If distinct matched handles name different canonical
   subjects, do not route on this rung, increment `handle_conflicts` once, and
   continue.
2. **Thread continuation, zero model.** Apply the exact thread-boundary match
   from section 7 among canonical primary subjects. Closed subjects remain
   eligible.
3. **Shared evidence, zero model.** Apply INV-5 and section 7's unchanged
   threshold and tie-break. Closed subjects remain eligible.
4. **Model tier.** This rung is reached only when rungs 1–3 miss. A valid
   model-supplied `subject_key` from Record extraction short-circuits a second
   gateway call if and only if it exactly equals one of the extractor's offered
   anchors; it is counted as `route_model`. Otherwise perform the lexical
   candidate recall and one adjudication call below. Closed subjects MUST NOT
   be offered here; the handle rung is the only ordinary route back into a
   closed matter.
5. **New or review.** A clean adjudication decision of `new`, or an empty
   candidate set, MUST use section 7's deterministic new-subject construction
   and increment `route_new`. An explicit `abstain`, malformed response, or
   attach decision rejected by the deterministic gate MUST enqueue the card
   for review, ingest none of it, and increment `route_review`.

The extraction prompt MUST retain its current SubjectAnchor mechanism. An
extracted offered-anchor `subject_key` is only a model-tier suggestion: rungs
1–3 MUST be evaluated first. When a deterministic rung wins and the suggestion
names another subject, the deterministic subject wins and
`route_disagreements` increments once. More generally, implementations MUST
evaluate already-available lower deterministic matches and the extraction
suggestion without additional gateway calls; if any would name a different
subject than the winning higher rung, increment `route_disagreements` once for
that card. A lower-rung disagreement MUST NOT change the route.

Lexical recall MUST consider only canonical open primary matters. Tokenize by
concatenating the fields below, applying section 7 title normalization, and
taking the set of non-empty whitespace-delimited tokens. The card tokens come
from its title plus cited SourceRef excerpts. Candidate tokens come from the
subject title, aliases, active handle values, and recent evidence excerpts.
The score is the integer set-intersection size. Order positive-scoring matters
by descending score, then UTF-8-bytewise ascending `subject_key`, and offer at
most five. Zero-scoring matters MUST NOT be offered: adjudication exists to
resolve ambiguity among lexically related candidates, and a lineup of
unrelated matters manufactures a false dilemma — the adjudicator abstains, the
card is queued for review, no matter forms, and every later batch on the same
new topic repeats the loop. When no matter scores positively the card proceeds
directly to rung 5 as a clean new subject. For each candidate, stable-union
the newest evidence excerpts up to 300 Unicode characters total.

The dedicated adjudication prompt MUST carry the complete card title, summary
fields, participants, and real cited source IDs. For every offered candidate it
MUST carry `subject_key`, `title`, `aliases`, active handles as `type:value`,
current `status`, `next_step`, `participants`, and the bounded recent evidence
excerpt text. Title-only anchors are forbidden. The gateway response schema is
closed and has exactly:

```json
{
  "decision": "attach" | "new" | "abstain",
  "subject_key": "string or null",
  "confidence": 0.0,
  "evidence_source_ids": ["string"]
}
```

The prompt MUST include this pinned literal example:

```json
{"decision":"attach","subject_key":"matter-204","confidence":0.86,
 "evidence_source_ids":["chat:release:r17"]}
```

After decoding, an independent deterministic gate MUST require an `attach`
subject key to be one of the exact offered candidate keys, every unique
`evidence_source_ids` member to name a real source cited by the card, at least
one such evidence ID, and confidence greater than or equal to
`identity.adjudication_confidence_threshold`. `new` MUST have a null subject
key; its confidence does not gate creation, and its evidence list MAY be empty
but any supplied ID MUST still name a real cited card source. `abstain` MUST
have a null subject key. Any unknown field, wrong type,
invalid enum, invalid subject, untraceable or empty attach evidence,
out-of-range confidence, below-threshold attach, or malformed JSON MUST be
treated as abstention and queued, never as `new`.

Review rows MUST survive restart in both Stores. `review_id` is `review_` plus
lowercase SHA-256 of canonical JSON `[scope_id, card_id]`; enqueue of the same
held card is idempotent, while reuse of that ID with changed payload MUST fail.
`review_items(scope_id)` returns pending rows in `(created_at, review_id)`
bytewise order. `resolve_review(..., action="attach")` requires an existing
canonical subject. `action="new"` forbids `subject_key` and uses section 7's
normal new-subject construction. Both actions ingest the held card with
assertion `origin=human`; assertion evidence is the stable union of the held
card SourceRefs followed by the mandatory resolution SourceRefs. They then set
`resolved_at` and `resolution_json` in the same transaction. Resolving an
already resolved item is a conflict error and MUST NOT ingest twice. Replay
retains pending and resolved review rows and never reroutes them.

Every winning rung increments exactly one of `route_handle`, `route_thread`,
`route_evidence`, `route_model`, `route_new`, or `route_review`. These counters,
plus `handle_conflicts` and `route_disagreements`, MUST be persisted in
`gate_stats`, returned by `gate_statistics`, and included in the enclosing
task gate or direct `AddRecordsReport`, alongside the section 8.1
`unchanged_dropped` counter. Exact observation re-ingest MUST
increment none of them. Counter updates and the chosen route, subject write or
review enqueue MUST commit atomically in the card-application transaction.
Candidate reads MUST use a short Store transaction; the adjudication gateway
call MUST execute with no Store lock or transaction held; final routing and
ingest MUST use another short transaction.

## 24. Signals, hotness, read watermarks, and briefing

### 24.1 Signal detection

A Signal is a deterministic per-record marker with shape
`(scope_id, record_id, kind, detected_at, matched_text, subject_key?,
status, acked_at?)` where `status ∈ {open, acked}`. Detectors run at staging
enqueue time, after the write-through of section 16.2, in the fixed order
listed below. `detected_at` MUST equal the staged record's `staged_at`.
Storage key is `(scope_id, record_id, kind)`; re-staging the same record MUST
be a no-op for existing rows (INV-17).

v1 detector kinds, in order:

1. `mention_of_self` — fires when any configured identity handle from
   `[identity] handles` matches the record content case-insensitively on
   token boundaries (a handle consisting only of digits MUST additionally be
   preceded by `@` or `:` in the content to fire). With no configured
   handles the detector is disabled and MUST emit nothing.
2. `machine_alert` — fires when the record author id matches the
   machine-sender pattern (default `no-?reply|notif|alert|donotreply`,
   extendable via `[signals] machine_senders`) AND the content matches at
   least one alert keyword (default set
   `alert|security|fail(ed|ure)?|error|警告|告警|失败|安全`, extendable via
   `[signals] alert_keywords`). Both conditions are required.

Acknowledgement (`status → acked`, `acked_at` set) is terminal, MUST be
persisted, and MUST survive replay. Signals never create subjects and are
not assertions; they are a parallel deterministic lane.

### 24.2 Hotness metrics

Hotness is computed per `(scope_id, container_id)` over fixed UTC buckets of
30 minutes aligned to the hour. For a request window the Store MUST report,
per conversation: `message_count`, `distinct_authors`, `reaction_total`, and
`hot` — true iff at least one bucket inside the window has
`distinct_authors >= 3 AND message_count >= 5` (thresholds configurable via
`[signals] hot_min_authors` / `hot_min_messages`). Revoked records are
excluded. The computation reads `staged_records` only and MUST NOT trigger
or gate distillation in this phase.

### 24.3 Critical-path matching

At signal creation the Engine MUST attempt one deterministic linkage: the
normalized value of each active handle binding in the same scope is matched
against the record content on token boundaries (digit-only values require a
preceding `@`, `:` or `#`). If one or more bindings match, `subject_key` is
set to the canonical subject of the longest matched value; ties break by
lexicographically smallest subject key. Subjects whose projected `status` is
a closed value still match (reopened threads are critical too). No model
call, no title fuzzing: handles only.

### 24.4 Read watermarks

`read_watermarks(scope_id, subject_key, last_seen_at)` stores the single
user's per-matter read position. Setting a watermark is an upsert with a
caller-supplied or server-now timestamp; watermarks never move backwards.
A matter's *unseen delta* for a request is the set of its assertions with
`observed_at > last_seen_at`; with no watermark row the whole window is
unseen. Wall `updated` badges and the briefing MUST use the watermark when
present instead of the request window start.

### 24.5 Briefing projection

`brief(window_start, window_end)` is a pure read (INV-18). It MAY be
restricted to an explicit scope list (defaulting to every scope in the
Store); all sections below then only consider records, matters, and
signals from those scopes. It returns:

- `needs_me`: open signals — critical-path (with `subject_key`) first,
  newest next — followed by active matters whose projected `blocker`,
  `next_step`, or `owners` contains a configured identity handle;
- `groups`: per console group (section 15 `[console.groups]`, ungrouped
  scopes under their own name): matters with assertion activity in the
  window, each carrying title, status, latest progress value, blocker,
  and `unseen` (watermark-relative); plus counts
  `{touched, completed, blocked}` where `completed`/`blocked` count
  status transitions observed inside the window;
- `quiet`: conversations with staged volume in the window but no matter
  activity: `message_count`, `distinct_authors`, `hot` per section 24.2.

Ordering is deterministic: groups by name; matters by
(`unseen` desc, latest activity desc, subject key). The REST surface is
`GET /v1/console/brief`; `POST /v1/matters/{subject}/seen` sets watermarks;
`GET /v1/signals` lists and `POST /v1/signals/ack` acknowledges. The
console renders the brief as the first screen; `mh brief` prints it.

### 24.6 Conformance

Golden cases MUST cover: detector firing and non-firing (identity unset),
the two-condition `machine_alert` gate, replay idempotency of signals and
terminal acks, critical-path handle linkage including the digit-only guard,
watermark-relative unseen deltas, hotness bucket edges, and briefing
structure and ordering for a mixed store.

## 25. Goal graph and time tree

A matter is the root of a growing tree: chat crystallizes into a topic,
the topic forks into follow-up sub-matters, and sub-matter conclusions flow
back up. Section 25 fixes the deterministic skeleton of that tree; semantic
upward propagation (a model rewriting a parent from child conclusions) is
out of scope for this section and arrives with the distillation agent.

### 25.1 Structure predicates

Three predicates join the schema profile for matter subjects:

- `part_of` — SINGLE; object is a subject key in the same scope. The active
  value is the node's parent; superseding it re-parents the node with full
  history. A matter with no active `part_of` is a **root**.
- `spawned_from` — SINGLE; object is a subject key in the same scope. Records
  the birth edge: which subject the node forked from, with the fork-moment
  Record cited as evidence on the assertion itself.
- `decision` — APPEND; object is decision text. A decision that spawns a
  sub-matter is cited as evidence by the child's `spawned_from` assertion.

Admission gates (INV-19), enforced in the validation gate for model origin
and in the correction door for human origin alike: the object subject MUST
exist in the scope; self-reference is rejected; the edge MUST NOT create a
cycle in the canonical active graph formed by the union of `part_of` and
`spawned_from` edges, evaluated after merge canonicalization. Cross-scope
references are invalid. RETRACT restores the prior state and re-runs no
gate other than the standard correction rules.

### 25.2 Graph projection and structural rollup

`matter_graph(scope_id, subject_key)` is a pure read returning the node's
parent chain (active `part_of` ancestors, nearest first), its children
(subjects whose active `part_of` is this node) ordered by birth instant,
and per-node projected `status`, `blocker`, and birth instant. A node's
birth instant is its `spawned_from` assertion's `valid_from` when present,
else the node's earliest assertion `valid_from`.

The structural rollup of a root is deterministic: `descendants_total`,
`descendants_completed` (projected status in the profile's completed
values), `descendants_blocked` plus the list of `(subject_key, blocker)`
for blocked descendants (blocker bubble-up), and `latest_activity` (max
assertion `recorded_at` over the subtree). Cycles are impossible by
INV-19; implementations MUST still bound traversal.

Read surfaces change as follows: matters with an active `part_of` stop
appearing as top-level wall rows and briefing matter entries; they appear
under their root, which carries the rollup. Group counts
(`touched/completed/blocked`) keep counting every matter, root or not.

### 25.3 Structure operations

- Human: the existing correction door accepts `part_of`, `spawned_from`,
  and `decision` assertions (origin human, P8 precedence) subject to the
  same admission gates; the REST correction surface and console expose
  re-parent and decision entry. Creating a fresh root manually is the
  existing card ingest with no structure edges.
- Deterministic suggestion: when extraction produces a new subject whose
  card cites evidence from a conversation where exactly one active root
  matter has assertion activity within the staging retention window, the
  engine MAY enqueue a review item proposing `part_of` that root — it MUST
  NOT auto-attach. The review queue gains resolution
  `attach_subgoal(parent_subject_key)` which admits the edge through the
  standard gates with reviewer provenance.
- The console matter detail renders the time tree (children by birth
  instant, parent link, per-node status chips); root wall rows show
  `completed/total` descendants and the bubbled blockers.

### 25.4 Conformance

Golden cases MUST cover: edge admission with evidence; self-reference,
unknown-target, and cycle rejection (including a cycle attempted through a
merge chain); re-parenting with history; birth-instant derivation both with
and without `spawned_from`; rollup counts, blocker bubble-up, and root
classification on a three-level tree; wall/brief root filtering; review
`attach_subgoal` resolution; and byte-identical replay of the graph.

## 中文摘要

Matterhorn 是 agent 的 L3 时态记忆层：同步写路径把团队通信 Record 经受控
提取转成带证据的 EpisodeCard，再确定性地转成不可变断言并入队；异步
`dream()` 只按 SchemaProfile 生成封闭
语义候选，再经十三项验证闸门过滤。读取、REST 读端点和 MCP 读工具只用 SQL，
绝不调用模型。规范固定了幂等哈希、双时间轴、人工纠错优先级、拒绝原因统计、
事务一致性与可重放性。`spec/conformance` 的语言无关 YAML 是 Python 与内部
Java 实现共同的验收资产。M4 增加通用 Record 合同、Slack 纯适配、线程优先
身份、增量游标，以及编辑追加/删除撤销证据的 INV-11；查询保留结论但明确标出
证据是否已撤销。M6 增加完全由投影差异派生、确定 ID 且重放不重复的变更事件，
以及包含 schema 指纹、subjects、全量断言、证据状态和派生事件的 scope 所有权
导出；导入只接受空 scope 与本地可用的同版本 profile。INV-12 增加带来源、无环、
可撤销的主语归并：提取前把 canonical 事项作为 anchors 给模型，模型只能引用实际
提供的 key，connector 确定性 key 优先；归并后的投影与写入沿链落到 canonical
target，被合并标题作为别名显示，unmerge 与 replay 不丢失原断言。INV-13 要求按
`container_id` 划分绝不混合的 conversation extraction unit，以最早 `sent_at` 和
bytewise container key 确定顺序，按完整 boundary 分块；每次模型调用前重算 anchor，
并让本次 flush 已落库的卡立即进入下一次 anchor snapshot。INV-14
增加带证据、可撤销且活动绑定唯一的 subject handle 索引；归并时按 canonical
subject 联合读取，unmerge 与 replay 均恢复并保留原始绑定状态。
