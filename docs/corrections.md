# Correction guide

A correction never edits or deletes history. It appends an `origin=human`
assertion to the same assertion set used by model output. At equal
`valid_from`, human origin wins regardless of `recorded_at`.

Required fields are scope, existing subject, subject type, registered
predicate, operation/value, business effective instant, and non-empty source
evidence. Use the human statement, ticket, or approved document as the source;
do not reuse an unrelated model source.

After correction:

1. `query_current` shows the corrected winner.
2. `query_timeline` explains the effective interval and evidence.
3. the older assertion remains available for audit.
4. `replay` derives the same result.

Run [the full example](../examples/correction/README.md) to see the answer
change from `blocked`/model to `open`/human.
