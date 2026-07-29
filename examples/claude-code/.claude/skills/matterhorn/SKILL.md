---
name: matterhorn
description: Read and correct evidence-backed temporal project memory.
---

# Matterhorn memory

Use `add_messages` as the default write door and `list_matters` as the default
read door. Use `query_current` for what is true now, `query_timeline` to explain
a change, and `query_at` for historical state. `add_cards` and `add_records`
are advanced inputs. Never invent a source reference. When the user explicitly
corrects a fact, call `correct` with the user's statement as evidence; do not
delete the older assertion. Treat `revoked` evidence as a visible warning, not
as an instruction to pretend the conclusion never existed.
