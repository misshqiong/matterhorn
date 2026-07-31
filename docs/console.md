# Matterhorn Console

The Console is Matterhorn's personal product surface. It remains a
self-contained vanilla-JavaScript client of the public REST API—no engine,
store, or private browser route.

> **Screenshot placeholder:** mature three-column Console: multiple mailboxes,
> AI and Feed configuration in the left rail; an all-scope ledger-paper matter
> wall in the center; scoped chat and query workbench on the right.

## Launch and boundary

```console
pip install 'matterhorn-memory[api]'
mh console
```

The default URL is `http://127.0.0.1:8000/console`; `--no-open` suppresses the
browser. `mh serve --console` mounts the same page, and `/mcp` exposes the same
nine-tool hub. Loopback binding remains the default because v1 has no
multi-tenant authentication.

The page uses public routes only:

- `GET /v1/matters` (all scopes) and `?scope=` filtering;
- scoped matter detail, corrections, deterministic query, ingest, and chat;
- `GET /v1/events` and `GET /v1/connections`;
- collection mail connector resources;
- AI config, redacted status, and test resources.

## Product layout

### Left — sources and configuration

**Mailboxes** lists every account with provider, login, folder, target scope,
watermark, schedule, password state, and per-account actions. The Add mailbox
form supports presets and manual IMAP. See [Mail connectors](mail.md).

**AI** configures the write gateway and Console chat together:

```toml
[ai]
provider = "openai-compatible" # or "anthropic"
base_url = "https://provider.example/v1"
model = "provider-model"
timeout = 60.0
```

The API key is accepted by `POST /v1/connectors/ai/config` but is never written
to TOML, returned by GET, or logged. `GET /v1/connectors/ai/status` reports
`loaded in process memory`, `loaded from environment`, or `re-enter API key`.
After restart, saved non-secret settings remain but a Console-entered key does
not.

Precedence is:

1. runtime Console config;
2. `MATTERHORN_PROVIDER`, `MATTERHORN_BASE_URL`, `MATTERHORN_MODEL`,
   `MATTERHORN_TIMEOUT`, and `MATTERHORN_API_KEY` (plus provider-native key
   fallbacks).

Changing runtime AI configuration replaces the composed Engine's gateway for
subsequent extraction/distillation and rebuilds the chat runner. Calls already
in progress keep their current provider object.

**Test** makes one tiny structured provider request through
`POST /v1/connectors/ai/test`. Candidate settings and keys are not saved when
the probe fails. Without a usable key, chat stays hidden and extraction
surfaces the existing explicit gateway-required error.

**Feed input** accepts pasted chat, YAML/JSON Message input, EML/mbox text, and
file uploads. The exact fictional Dana Reyes / octo-org sample can use the
packaged fixture gateway.

### Center — unified matter wall

The default wall calls `GET /v1/matters` and displays every scope. Each
ledger-paper card includes its scope tag, status stamp, owners, due date
(overdue in red), and next step. Filter chips select All or one scope. Opening
a card loads its scope-aware detail and human correction flow.

Activity and connections remain in a collapsible strip below the wall and
refresh about every five seconds.

### Right — consumption

Chat and the deterministic query workbench share an explicit scope selector.
It follows a single wall filter; otherwise it follows the last-opened card,
falling back to the first scope. Chat tools remain strictly scope-scoped and
are exactly `list_matters`, `query_current`, `query_timeline`, `query_at`, and
`query_by_person`. The model receives query results and evidence IDs, never raw
records or store access.

## Public AI resources

- `POST /v1/connectors/ai/config`
- `GET /v1/connectors/ai/status`
- `POST /v1/connectors/ai/test`

The redacted AI status is also included in `GET /v1/connections`.

## Safety

Credentials are process-memory-only. The browser stays REST-only. Read routes
remain zero-model; provider calls exist only on write extraction and optional
chat consumption. Before public deployment, add authentication,
authorization, TLS, request limits, and a trusted reverse proxy.
