# Project strategy

## Layer and promise boundary

Matterhorn is a deterministic, evidence-backed temporal memory layer for
agents. Messages enter on the write side; projected matters and their history
come from persisted evidence rather than generation at answer time. The project
positions this as an L3 layer that complements broad L1 capture and retrieval
tools: Matterhorn accepts traceable upstream material and adds closed
predicates, bi-temporal assertions, deterministic projection, replay, and
first-class human correction.

Sources:
[README — project description and promise boundary](../README.md#the-promise-boundary),
[Positioning alongside L1 memory tools](../docs/positioning.md),
[SPEC — principles](../spec/SPEC.md#1-principles).

## Name and package

The public distribution name is `matterhorn-memory` on PyPI. The Python import
name remains `matterhorn`, as shown by the SDK entry point
`from matterhorn import Engine`.

Source:
[README — installation and SDK entry point](../README.md#five-minute-journey).

## Two-verb facade

The default SDK facade is deliberately centered on two verbs:
`add(messages)` for writes and `matters(scope_id)` for reads. `add()` persists a
task without calling an LLM and `flush()` runs the queued write pipeline;
`matters()` is a deterministic projected read and never calls an LLM. Direct
card and Record ingestion, detailed temporal queries, and correction remain
progressively disclosed advanced surfaces.

Sources:
[README — Two-verb SDK](../README.md#two-verb-sdk),
[README — Progressive disclosure](../README.md#progressive-disclosure),
[CHANGELOG 0.5.0](../CHANGELOG.md#050---2026-07-29).

## Thread-first identity

A communication thread is the first deterministic matter boundary. Identity
does not depend on a model choosing a subject. Cross-thread evidence merging
still obeys the evidence threshold, including the absolute rule that a single
shared source never causes a merge.

Sources:
[CHANGELOG 0.4.0](../CHANGELOG.md#040---2026-07-29),
[SPEC — evidence-based identity merge](../spec/SPEC.md#2-invariants).

## Evidence revocation, not conclusion deletion

Edited Records append new observations and assertions. Deleting or revoking a
Record does not mutate or remove the immutable assertions, intervals, or
materializations it supported. Queries keep the conclusion visible while
marking each source as active or revoked and reporting aggregate evidence
status.

Sources:
[SPEC — INV-11 immutable source lifecycle](../spec/SPEC.md#2-invariants),
[CHANGELOG 0.4.0](../CHANGELOG.md#040---2026-07-29).

## Dual-backend conformance

SQLite and PostgreSQL implement the same Store boundary. Backend-specific SQL
and value normalization stay inside the Store layer, and a behavioral
difference between the two backends is a defect. Both backends run the same
language-neutral golden conformance cases. The current public suite contains
47 cases.

Sources:
[SPEC — PostgreSQL Store and consistency boundary](../spec/SPEC.md#19-postgresql-store-and-consistency-boundary),
[SPEC — Reference conformance harness](../spec/SPEC.md#20-reference-conformance-harness),
[README — conformance suite](../README.md#the-promise-boundary).

## Current milestone

The latest release listed in the changelog is 0.6.0, dated 2026-07-29. It adds
deterministic projection-diff events, at-least-once webhook delivery, versioned
scope export/import, service-only daily UTC flushing, and four output-surface
conformance cases that bring the suite to 47.

Source:
[CHANGELOG 0.6.0](../CHANGELOG.md#060---2026-07-29).
