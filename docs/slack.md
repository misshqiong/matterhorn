# Slack ingestion

Matterhorn treats Slack as the first communication provider over the generic
`Record` contract. The adapter is pure: it maps payloads already obtained by a
host and performs no Slack API call or LLM call.

## Supported payloads

`map_slack_history` accepts the `messages` returned by
[`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history/).
`map_slack_event` accepts message Events API payloads, including
[`message_changed`](https://docs.slack.dev/reference/events/message/message_changed/)
and
[`message_deleted`](https://docs.slack.dev/reference/events/message/message_deleted/).
The implementation follows Slack's
[message event](https://docs.slack.dev/reference/events/message/) and
[rich text block](https://docs.slack.dev/reference/block-kit/blocks/rich-text-block/)
contracts.

The normalization includes:

- `ts`, channel, client message ID, team, thread, parent user, and thread
  broadcast metadata;
- human, bot, and app author identity;
- Block Kit `rich_text` as the preferred content source, with readable mrkdwn
  fallback for user, channel, and link tokens;
- reactions and hosted file metadata; and
- a human-followable
  `https://<workspace>/archives/<channel>/p<ts-without-dot>` permalink.

Slack documents `ts` as unique within one conversation, not across the
workspace. Matterhorn therefore uses `record_id = "<channel>:<ts>"`. Removing
that namespace corrupts shared-evidence counting and is rejected by the Record
contract.

Membership, topic, purpose, archive, rename, and similar channel-management
subtypes are not conversational evidence and are filtered. An unknown
non-conversational subtype is also filtered rather than guessed into evidence.

## History pages and incremental sync

Download history with your Slack host, then pass the page to the CLI:

```console
mh extract history.json --adapter slack-history \
  --scope-id engineering --container-id C0123 \
  --workspace-domain acme.slack.com --db memory.db \
  --provider openai-compatible
```

Configure the write-side model with the `--base-url`, `--model`, and
`--api-key` overrides, a non-secret `[ai]` table plus a credential environment
variable, or the standard `MATTERHORN_*` environment. Anthropic is available
as `--provider anthropic`. A key entered in the Console is process-local to
that running service and does not leak into a separate CLI process. No
provider is configured on read commands.

`mh extract` persists the response cursor and the newest observed timestamp for
each container. Inspect them with:

```console
mh sync-status engineering --db memory.db
```

Pass a host cursor explicitly as `--cursor C0123=opaque-value`. Use
`--backfill` for older pages; backfill writes unseen observations without
advancing the forward cursor/watermark. Event batches that omit a cursor retain
the last opaque history cursor. Replaying an overlapping page is a complete
no-op before model invocation.

The SDK equivalent is:

```python
mapped = map_slack_history(
    payload,
    channel_id="C0123",
    workspace_domain="acme.slack.com",
)
report = engine.add_records(
    mapped.records,
    scope_id="engineering",
    cursors={"C0123": mapped.next_cursor or "end"},
)
```

The same Record array and scope are accepted by the advanced SDK and MCP
`add_records` entry. REST intentionally exposes the default Message door and
advanced card door, not a legacy Record RPC route.

## Events, edits, and deletion

An edit retains the channel/`ts` Record ID but changes the immutable observation
payload. Record-to-card extraction runs again, the card ID changes, and new
assertions receive a distinct `observation_id` and `recorded_at`. Prior
assertions are never updated.

Slack deletion events intentionally omit the original author and content.
Callers must retain the latest normalized Record and provide it as
`prior_record`:

```python
deleted = map_slack_event(
    event_payload,
    workspace_domain="acme.slack.com",
    prior_record=last_record,
)
engine.add_records([deleted], scope_id="engineering")
```

The adapter raises if the prior Record is unavailable or does not match the
event's channel and `deleted_ts`; it never fabricates identity. Ingest records
the revocation without deleting assertions or intervals. Every value query
returns `evidence_status` and per-source `source_refs[].status`, URI, and
`revoked_at`, so conclusions supported only by deleted messages are visibly
flagged.

## Offline verification

The complete fixture transcript uses no network and no API token:

```console
.venv/bin/python examples/slack/demo.py
```

It covers history mapping, extraction, deterministic ingest, clickable evidence
permalinks, edit append-only behavior, deletion revocation, and cross-channel
same-`ts` isolation.
