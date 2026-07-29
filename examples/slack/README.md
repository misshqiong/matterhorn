# Offline Slack ingestion

This example maps realistic `conversations.history`, `message_changed`, and
`message_deleted` fixtures into generic Matterhorn Records. A deterministic
fixture gateway stands in for write-side extraction, so the complete run needs
neither a Slack token nor an LLM API token:

```console
.venv/bin/python examples/slack/demo.py
```

The transcript shows:

- readable Slack content and clickable message permalinks;
- Record-to-EpisodeCard extraction followed by normal deterministic ingest;
- a message edit creating a second immutable observation and assertion;
- a deletion retaining assertions while marking their evidence `revoked`; and
- equal Slack `ts` values in different channels producing distinct Record IDs
  and matters.

Production hosts use `mh extract --adapter slack-history` for downloaded
history pages, or call the `add_records` MCP/REST/SDK surface after mapping
Events API payloads. See [the Slack guide](../../docs/slack.md).
