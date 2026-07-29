---
name: matterhorn
description: Consult and correct Matterhorn's evidence-backed temporal memory. Use before answering about known matters, decisions, people, status, ownership, blockers, next steps, due dates, progress, or historical state; when evidence or effective-time boundaries matter; and whenever a human says remembered information is wrong or outdated.
---

# Matterhorn

Treat Matterhorn as derived memory, not a text generator. Its answers come from
persisted assertions and projected intervals. Do not ask an LLM to reinterpret
or improve a query result.

## Default workflow

1. Use `add_messages` as the default write door for conversation messages.
2. Use `list_matters` as the default read door.
3. Use the query tools only when temporal or evidence detail is needed.
4. Use `add_cards` only when the caller already has evidence-backed
   EpisodeCards; `add_records` is an advanced provider-integration door.

## Read workflow

1. Use `list_matters` when the subject key is unknown.
2. Use `query_current` for what is true now.
3. Use `query_timeline` to explain changes or show supporting evidence.
4. Use `query_at` for “what was true then?”
5. Use `query_by_person` to find subjects currently related to a person.
6. Preserve returned `source_ids` when explaining why a value is believed.

Read time fields precisely:

- `valid_from` and `valid_to` are business effective time.
- `recorded_at` belongs to the supporting assertion and is when the system
  learned it.
- `valid_to: null` means the interval is open and therefore currently true.
- Non-null intervals are half-open: `valid_from <= t < valid_to`.

## Write workflow

Call `add_messages` with exactly the minimal message fields. Preserve native
message IDs, conversation IDs, senders, and timestamps. The tool returns a
persistent receipt; do not claim extraction completed merely because the task
was accepted.

For advanced `add_cards`, always include at least one source reference. Never
invent a `source_id`.

## Correction workflow

When a human says memory is wrong, do not merely apologize and do not overwrite
history:

1. Read the current value and note its `subject_key`, predicate, interval, and
   evidence.
2. Ask for the effective time only if it cannot be inferred safely.
3. Call `correct` with the existing subject and predicate, the human value, its
   effective `valid_from`, and a source reference to the human correction.
4. Call `query_current` again and use the corrected result in the answer.

Worked example:

```json
{
  "correction": {
    "scope_id": "team-a",
    "subject_key": "release",
    "subject_type": "MATTER",
    "predicate": "status",
    "operation": "ASSERT",
    "object_value": "closed",
    "valid_from": "2026-01-01T08:00:00Z",
    "source_refs": [
      {
        "source_id": "human-correction-message-42",
        "sent_at": "2026-01-01T09:00:00Z",
        "sender": "ada",
        "excerpt": "This was already closed."
      }
    ]
  }
}
```

After `correct` succeeds, query `status` again. A human assertion outranks a
model assertion at the same effective instant while retaining both facts in
the auditable history.
