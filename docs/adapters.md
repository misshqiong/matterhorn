# Ecosystem adapters

`MessageCardExtractor` is an LLM-backed write component. Its response is closed
JSON derived from the active SchemaProfile. Each candidate is validated as an
EpisodeCard; empty evidence or any source ID outside the supplied message
window is counted and dropped. Card IDs are deterministic for the input window
and candidate slot.

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
