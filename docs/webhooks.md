# Events, webhooks, and ownership exports

Matterhorn derives change events from projection diffs. Assertions remain the
only source of truth; the event table is an append-only delivery artifact keyed
by deterministic `event_id`.

## Read events

```console
mh events team-a
mh events team-a --since 2026-07-29T00:00:00Z
curl 'http://127.0.0.1:8000/v1/scopes/team-a/events?since=2026-07-29T00:00:00Z'
```

`since` is inclusive. Results are ordered by `(recorded_at,event_id)`.

## Webhook quickstart

Configure the receiver in `matterhorn.toml`:

```toml
webhook_url = "http://127.0.0.1:9000/matterhorn-events"
daily_flush_at = "02:00" # optional, UTC
```

Or override it when starting service mode:

```console
mh serve --webhook-url http://127.0.0.1:9000/matterhorn-events
```

The receiver gets one JSON object:

```json
{
  "events": [
    {
      "event_id": "deterministic-sha256",
      "event_type": "status_changed",
      "scope_id": "team-a",
      "subject_key": "release",
      "predicate": "status",
      "old_value": "open",
      "new_value": "done",
      "valid_from": "2026-07-29T00:00:00Z",
      "recorded_at": "2026-07-29T08:00:00Z",
      "origin": "human",
      "source_ids": ["human-note"]
    }
  ]
}
```

Delivery is at least once. Matterhorn marks a batch delivered only after a
successful HTTP response and uses up to three exponentially backed-off
attempts per dispatch cycle. A crash after the receiver accepts a batch but
before the local acknowledgement can repeat it, so receivers must deduplicate
by `event_id`.

Tests can stay fully offline by passing `httpx.ASGITransport` to
`WebhookDispatcher`; the repository acceptance suite uses an in-process
FastAPI receiver this way.

## Export and import

```console
mh export team-a --out team-a.json
mh import team-a.json --db restored.db
mh replay team-a --db restored.db
```

The versioned JSON envelope carries the schema profile ID and content
fingerprint, subjects, every assertion, evidence lifecycle state, and derived
event history. Import requires an empty target scope and the same profile to be
available locally. Intervals and MemoryCards are rebuilt, proving that the
portable assertion set—not a proprietary index—is the owned memory asset.
