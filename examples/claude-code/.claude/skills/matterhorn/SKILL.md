---
name: matterhorn
description: Read and correct evidence-backed temporal project memory.
---

# Matterhorn memory

Use `list_matters` to discover subjects, then `query_current` for what is true
now or `query_timeline` to explain a change. Use `query_at` for historical
state. Never invent a source reference. When the user explicitly corrects a
fact, call `correct` with the user's statement as evidence; do not delete the
older assertion.
