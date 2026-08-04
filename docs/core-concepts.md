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
        ▲          deterministic, idempotent, replayable (INV-1…INV-14)
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

## Conversation units and rolling anchors

Record extraction is conversation-scoped. During one `add_records` or Message
flush, the engine groups active Records by `container_id`, orders conversations
by their earliest message time (then bytewise container ID), and never sends
two conversations in one model call. Inside a conversation it orders Records
chronologically and packs whole reply/thread boundaries into size-limited
chunks; an oversized boundary stays whole.

Before every chunk, the engine derives a fresh list of canonical open-matter
anchors. It applies accepted cards and rebuilds the projection before moving to
the next chunk. A matter first recognized in an earlier conversation or chunk
is therefore available for evidence-backed attachment later in the same flush.
Replay remains model-free: it rebuilds from stored cards and assertions and
never repeats extraction.

## Answers are derived, not generated

The model may propose structured write candidates, but a validation gate can
reject them. Read paths only inspect persisted assertions, intervals, and
materializations. That makes current state, historical reconstruction, source
evidence, and correction precedence repeatable and testable.

`add()` and `add_cards()` return persistent task receipts before extraction.
`flush()` or service-mode quiet-period scheduling advances the task. A task's
gate report makes accepted candidates and per-reason rejections observable.
