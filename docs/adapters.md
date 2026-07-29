# Ecosystem adapters

`MessageCardExtractor` (also exported as `RecordCardExtractor`) is an LLM-backed
write component over provider-neutral Records. Its response is closed JSON
derived from the active SchemaProfile. Each candidate is validated as an
EpisodeCard; empty evidence or any source ID outside the supplied Record window
is counted and dropped. Card IDs are deterministic across retry and change
when a cited Record becomes a new edited observation.

The deprecated `ChatMessage{message_id,sent_at,sender,content}` input remains a
thin compatibility wrapper. New integrations should use `Record`. Slack has a
first-party pure adapter and dedicated guide in [slack.md](slack.md).

`map_reme_digest` and `map_openviking_digest` are pure deterministic functions.
They never call an LLM. Their exact normalized input shapes are documented in
the module headers and represented by fixtures under `tests/fixtures/`.

These shapes are explicitly best-effort against public formats:

- [ReMe](https://github.com/agentscope-ai/ReMe) publishes a file-native
  workspace with Markdown daily and digest
  memories. Frontmatter is extensible, so Matterhorn requires a normalized
  `{path, frontmatter, content}` export with `frontmatter.sources`.
- [OpenViking](https://docs.openviking.ai/en/concepts/04-viking-uri) publishes
  filesystem-like `viking://` resources and Markdown overviews. Matterhorn
  requires a normalized `{uri, overview, metadata}` export with
  `metadata.source_refs`.

Both mappings are lossy: free-form Markdown becomes `progress`; ReMe wikilinks
and OpenViking relations/chunks are not retained. Missing traceable sources are
an error, never an invitation to synthesize a `source_ref`.

Both adapters are wired to the CLI without an LLM:

```console
mh extract reme.json --adapter reme --db memory.db
mh extract openviking.json --adapter openviking --db memory.db
```
