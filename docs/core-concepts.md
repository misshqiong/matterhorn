# Core concepts

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

## Answers are derived, not generated

The model may propose structured write candidates, but a validation gate can
reject them. Read paths only inspect persisted assertions, intervals, and
materializations. That makes current state, historical reconstruction, source
evidence, and correction precedence repeatable and testable.
