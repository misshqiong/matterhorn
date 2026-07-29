# Authoring a third SchemaProfile

Start from a new YAML file; do not add domain vocabulary to engine code.

```yaml
schema: incident-response/v1
subjects:
  - type: INCIDENT
    primary: true
predicates:
  - name: phase
    subject: INCIDENT
    cardinality: SINGLE
    extraction: deterministic
    source_field: status
    value_domain: [investigating, mitigated, resolved]
  - name: responder
    subject: INCIDENT
    cardinality: SET
    extraction: deterministic
    object: person
    source_field: participants
    extraction_rule: participant_ids
  - name: update
    subject: INCIDENT
    cardinality: APPEND
    extraction: deterministic
    retract_guard: never
    source_field: progress
completion:
  predicate: phase
  completed_values: [resolved]
```

Choose one primary subject. A non-primary subject must declare its parent type.
Every predicate declares subject, cardinality, extraction mode, and object
shape. Deterministic predicates name an EpisodeCard `source_field`; semantic
predicates do not. Use `value_domain` when a closed enum is required.

Validate and inspect:

```console
mh schema show ./incident-response.yaml
mh ingest incident.yaml --schema ./incident-response.yaml --db incident.db
```

Before shipping a built-in profile, add language-neutral golden cases covering
each cardinality, retract guard, completion meaning, identity behavior,
semantic gate rule, replay, and correction precedence.
