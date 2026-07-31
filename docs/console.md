# Matterhorn Console

The Console is Matterhorn’s operator, developer, and demo surface. It is a
static client of the public REST API, served on the same origin as the API. It
does not have private database or engine routes.

> **Screenshot placeholder:** Console overview with scope rail, matter cards,
> current-value correction, query workbench, Feed upload/quick jot, Mail
> Connectors sheet, and evidence chips.

## Launch

Install the API extra and start the Console:

```console
pip install 'matterhorn-memory[api]'
mh console
```

Matterhorn binds to `127.0.0.1:8000`, prints the URL, and opens
`http://127.0.0.1:8000/console` in the default browser. Use `--no-open` in
automation. `mh serve --console` mounts the same page without opening a browser.
Both commands also mount the same nine-tool MCP server at `/mcp`.

The Console and REST API share one port. The browser calls only documented
routes, including:

- `GET /v1/scopes` and scope matter/detail resources;
- service-wide `GET /v1/events` and `GET /v1/connections`;
- the four deterministic query resources;
- `POST /v1/scopes/{scope}/corrections`;
- `POST /v1/scopes/{scope}/ingest`;
- `POST /v1/scopes/{scope}/upload` and `/quick-message`;
- the public `/v1/connectors/mail/...` resources;
- optional `POST /v1/scopes/{scope}/chat`.

The default loopback bind is deliberate. Matterhorn v1 has no multi-tenant
authentication or authorization. Before any public deployment, put
authentication and an appropriate trusted network boundary in front of the
service.

## Live hub view

The top of the Console answers two operating questions without leaving the
page:

- **Activity stream:** latest projection events across every scope, including
  matter title, predicate, old and new values, origin, and recorded time.
- **Connections:** redacted mail state, UID watermark and next run; each
  scope's latest ingestion time and message/record observation count; and the
  service-wide distill queue length.

The browser polls those public endpoints about every five seconds. The scope
rail and active matter list refresh on the same cadence, so Claude sessions,
agent posts, and new email become visible without a manual refresh. The page
remains a self-contained vanilla-JavaScript REST client with no external
resources.

The Dockerfile also provides a `console` target:

```console
docker build --target console -t matterhorn-console .
docker run --rm -p 127.0.0.1:8000:8000 matterhorn-console
```

## Feed formats

The server—not browser JavaScript—detects three pasted forms:

1. Plain chat lines, such as `Dana Reyes: The launch is in progress.` The
   server synthesizes increasing timestamps in paste order.
2. YAML or JSON using the minimal `Message` contract.
3. Raw `.eml` or mbox text, normalized by the email adapter.

Unknown input reports all three formats with a one-line example. Ingest and
chat have input caps and independent process-local rate limits.

The same Feed sheet uploads `.mbox`, `.eml`, `.yaml`, and `.json` through the
server-side detector; upload immediately extracts and flushes. The quick jot
form writes one sender/text message. Its optional `sent_at` defaults to the
server clock when blank.

“Load sample” inserts fictional Dana Reyes / octo-org chat. Its pre-recorded,
packaged fixture gateway produces a matter without an API key. It is only
selected for that exact fictional sample; ordinary extraction still uses the
configured write gateway.

## Connectors

The collapsible **Connectors · Mail** sheet configures a preset or manual IMAP
account, holds its app password only in process memory, triggers an immediate
sync, and shows the UID watermark, UIDVALIDITY, last-run counts, credential
state, error/help link, and next scheduled run. See the dedicated
[mail connector guide](mail.md).

## Optional chat

Chat is hidden unless a supported provider, model, and credential are
configured with the normal Matterhorn variables:

```console
export MATTERHORN_PROVIDER=openai-compatible  # or anthropic
export MATTERHORN_BASE_URL=https://provider.example/v1
export MATTERHORN_MODEL=provider-model
export MATTERHORN_API_KEY=...
export MATTERHORN_TIMEOUT=60
mh console
```

Anthropic defaults to `https://api.anthropic.com` when no base URL is set.
Provider-native `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` fallbacks remain
supported.

The host-side loop can execute at most six calls. Its only tools are
`list_matters`, `query_current`, `query_timeline`, `query_at`, and
`query_by_person`. Each maps one-for-one to `MatterhornService`; the route
scope is fixed by the host, and the model never receives raw records or store
access. Assistant replies include the query arguments and returned evidence
source IDs as clickable evidence chips.

## 30-second demo

1. Run `mh console`, click **Load sample**, then **Extract**. The completed
   receipt shows its gate breakdown and the matter card appears.
2. Open **octo-org Console launch**, click **Correct** beside a wrong current
   value, enter the new value, reason, and `Dana Reyes`, then record it. The
   value refreshes immediately with a `✏️ human` badge and remains in the
   timeline.
3. If chat is configured, ask “What is the current progress?” The answer
   carries `依据/Evidence` chips for the deterministic queries and source IDs;
   click a chip to highlight the matching matter.

For shared Claude Code and agent-team mounting, see
[Agent-team hub topology](agent-team.md).
