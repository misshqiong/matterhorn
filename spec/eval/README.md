# Matterhorn message-to-matter evaluation contract

This directory is the normative, language-neutral contract for measuring
Record extraction and identity routing. The key words **MUST**, **MUST NOT**,
**SHOULD**, and **MAY** are normative. Evaluation is measurement, not a gate:
metric values MUST NOT change a completed run's successful exit status.

Section 26 exemplar samples live under `samples/`; only this partition may be
injected into a unified-loop prompt. Held-out samples live under `testset/`,
are scored only, and MUST NEVER be used as exemplars. `sample_id` values MUST
be unique within and across both partitions. Each sample is a closed mapping
with `sample_id`, `source_kind` (`mail`, `im`, or `agent`), `scope_id`, a
fictionalized `window`, and `expected_assertions`. Assertion-set scoring first
removes exact matches, then reports facts emitted on another subject as
`mis_attached`; the remaining expected and produced rows are `missing` and
`spurious` respectively.

The assertion-set differ additionally classifies a wrong or missing
`part_of`, `spawned_from`, or `gathers` assertion as `mis_structured`.
`mis_typed` is the number of samples whose existing typing classification is
incorrect. The reported objective is
`c1*missing + c2*spurious + c3*mis_attached + c4*mis_typed +
c5*mis_structured`, using `[eval].loss_weights` and its environment override.

## Case files

Every case MUST be one YAML mapping. Case files MUST end in `.yaml`; a sibling
`<case>.responses.yaml` is fixture data and is not itself a case. Unknown case
and expected-matter fields MUST be rejected.

| Field | Presence | Type and rule |
| --- | --- | --- |
| `case_id` | required | Unique stable kebab-case string. |
| `title` | required | Human-readable string; never used for scoring. |
| `scope_id` | optional | Non-empty scope; default `eval:<case_id>`. |
| `schema_profile` | optional | Profile ID or path; default `org-matters/v1`. |
| `rounds` | required | Non-empty ordered list of non-empty Message lists. |
| `expected` | required | Non-empty ground-truth matter list. |

Each Message MUST use the closed public SPEC section 3.1 shape
`{id,sender:{id,name},text,sent_at,conversation_id}`. The optional public
`reply_to` field remains valid, although the shipped cases do not require it.
Message `id` values MUST be unique within one case so expected evidence is
unambiguous. Round `r` MUST execute as a separate
`engine.add(scope_id, messages, wait=True)` call before round `r+1`; a runner
MUST NOT flatten rounds into one flush.

Each expected matter has this closed shape:

| Field | Presence | Type and rule |
| --- | --- | --- |
| `title` | required | Non-empty fuzzy title expectation. |
| `status` | optional | Exact string-or-null projected value. |
| `owner` | optional | Exact string-or-null sole owner value. |
| `next_step` | optional | Exact string-or-null projected value. |
| `evidence` | required | Non-empty unique list of real input Message ids. |

An input Message id MAY occur in more than one expected matter's evidence.
This means the Message legitimately touches each listed matter; it MUST NOT by
itself be classified as a merge error.

## Scripted responses

`<case>.responses.yaml` MUST contain exactly one `responses` list. Each item is
the closed `{cards:[...]}` Record-extraction response consumed by one extractor
call, in call order. Calls are ordered by the production rules in SPEC section
16, so one round can consume more than one response when it contains multiple
conversations or chunks. A mapping/list/scalar is JSON-serialized; a string is
returned verbatim. Exhaustion and unused responses are harness errors.

Fixture mode MUST send these responses through the ordinary built-in Record
extractor and Engine pipeline. Because these fixtures measure Record routing,
the fixture gateway MUST answer semantic-distillation response schemas with
the deterministic empty response `{candidates:[]}`; those calls do not consume
the extractor response list. Until the dataset gains dedicated adjudication
fixtures, fixture mode MUST answer the section 23 adjudication schema with a
deterministic `abstain`; this also does not consume the extractor list and makes
the measured abstention cost explicit. Live mode MUST instead use the
configured production gateway for every extraction, adjudication, and semantic
call.

## Produced evidence and alignment

A produced matter is a final primary matter returned by the deterministic read
side after all rounds. Its evidence is the union of `source_id` values in its
accepted cards' `source_refs`, mapped through SPEC section 3.1 namespacing back
to case Message ids. Its creation round is the first round after which its
`subject_key` exists.

Before any metric is computed, a runner MUST greedily construct a one-to-one
alignment using only positive evidence overlap. It MUST enumerate every
expected/produced pair with
`overlap = |expected.evidence intersect produced.evidence|`, order pairs by:

1. larger overlap first;
2. UTF-8 bytewise ascending produced `subject_key`; then
3. expected declaration order;

and accept a pair only while neither side has already been aligned. Zero-
overlap pairs remain unaligned. Titles MUST NOT affect alignment.

For the secondary title check, tokenize each title by Unicode case-folding,
replacing every punctuation or symbol character with a space, splitting on
whitespace, and taking the unique token set. The score MUST be token-set
Jaccard overlap `|intersection| / |union|`, or zero when either set is empty.
An aligned title matches when its score is at least `0.5`.

## Metrics

Rates with a zero denominator MUST be JSON `null` and print as `n/a`.

- **`over_split` (过切分).** Count expected matters whose evidence intersects
  at least two produced matters. Total is `matters_expected`; rate is
  `count / matters_expected`.
- **`wrong_merge` (误合并).** For each produced matter, discard every Message
  that legitimately belongs to zero or multiple expected matters. Count the
  produced matter when the remaining exclusive Messages name at least two
  distinct expected matters. Total is `matters_produced`; rate is
  `count / matters_produced`.
- **`wrong_attach` (误挂).** Count each input Message once when it is attributed
  to at least one produced matter aligned to an expected matter outside that
  Message's complete ground-truth set. An unaligned produced matter does not
  satisfy this definition. Total is all input Messages; rate is
  `count / input_messages`.
- **`missed_attach` (漏挂).** A Message in round `r` is eligible for an expected
  matter when that expected matter has an aligned produced matter containing
  some ground-truth evidence from a round before `r`. Count the Message once
  when it does not extend that aligned matter and instead appears in a
  different produced matter first created in round `r`. A Message eligible for
  more than one expected matter is still counted once. Total is the unique
  eligible Message set; rate is `count / eligible_messages`.
- **`field_accuracy`.** For each of `status`, `owner`, and `next_step`, compare
  only declarations present in expected YAML. A missing alignment is
  incorrect. `owner` is correct only when the produced matter has exactly one
  owner equal to the declared scalar (or no owner for declared null). Report
  `{correct,total,rate}` per field and over all declared fields.
- **`evidence_validity`.** Count every `source_ref` occurrence on every accepted
  extraction card, including repeated citations on different cards. A ref is
  valid exactly when it equals the SPEC namespaced source id of a real input
  Message. Report `{valid,total,rate}` with `rate = valid / total`.
- **`title_match_rate`.** Report aligned pairs passing the title rule as
  `{matched,total,rate}` with `rate = matched / total`.
- **`zero_model_route_rate`.** Read the Engine's persisted counters after the
  case and compute `(route_handle + route_thread + route_evidence) /
  (route_handle + route_thread + route_evidence + route_model + route_new +
  route_review)`. Review-queued cards are included in the denominator. A zero
  denominator produces JSON `null`. Aggregate reports MUST sum all six route
  counters across cases before dividing.

Every case and aggregate report MUST also contain `matters_expected`,
`matters_produced`, `cards_accepted`, total `gate_rejections`, and gate
rejection counts by reason, all six route counters, and `review_queued`.
Aggregate metric rates MUST be micro-averages: sum case numerators and
denominators, then divide.
The plain-text case table MUST include `review_queued` as its own column.

## Reference command and report

```console
mh eval run
mh eval run --case lifecycle-five-rounds --provider fixture-file
mh eval run --provider openai-compatible --json baseline.json --seed-note
mh eval run --assertion-results produced-assertions.yaml
```

The human output MUST be plain text without ANSI or terminal-width-dependent
layout. `--json` MUST write the full report with schema
`matterhorn-eval/v1`. A completed measurement exits zero regardless of scores.
Unreadable/malformed cases, fixture errors, gateway failures, and report write
failures MUST exit non-zero.
