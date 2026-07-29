# Core concepts

## The onion and its promise boundary

```text
Public door: add(messages)
        │
        ▼
[Message → EpisodeCard extraction: LLM best-effort and replaceable]
        │
════════╪════ Engine promise boundary ═══════════════════════════════════
        ▼
   EpisodeCard ──► validation ──► assertions ──► intervals ──► answers
        ▲          deterministic, idempotent, replayable (INV-1…INV-11)
        │
Advanced door: add_cards(episode_cards)
```

The card is the narrow waist. Extraction below it is best-effort. At and above
the evidence-backed EpisodeCard, Matterhorn makes its hard deterministic
promise. A new input type is admissible only if it maps losslessly to that
contract with traceable sources.

## Two time axes

`valid_from` says when a fact became true in the business world.
`recorded_at` says when Matterhorn learned it. Backfilled events therefore keep
their historical effective time without pretending the system observed them
then. Queries such as `at()` use effective time; audit and conflict ranking can
still see observation time.

## Assertions and intervals

An assertion is immutable evidence: ASSERT or RETRACT, subject, predicate,
value, effective instant, recording instant, origin, and sources. An interval
is disposable projection state derived from the complete assertion set.
Deleting every interval and replaying must produce a byte-identical snapshot.

SINGLE predicates choose one stable winner. SET predicates track each value
independently. APPEND predicates produce point events. A repeated assertion of
the same live value adds supporting evidence without closing and reopening the
interval.

Communication Records add an immutable observation dimension. An edit produces
a new assertion even when extraction yields the same fact. A deletion revokes
the source, not the assertion or interval; queries return active, partially
revoked, or revoked evidence status with the original permalink.

## Answers are derived, not generated

The model may propose structured write candidates, but a validation gate can
reject them. Read paths only inspect persisted assertions, intervals, and
materializations. That makes current state, historical reconstruction, source
evidence, and correction precedence repeatable and testable.

`add()` and `add_cards()` return persistent task receipts before extraction.
`flush()` or service-mode quiet-period scheduling advances the task. A task's
gate report makes accepted candidates and per-reason rejections observable.
