# Matterhorn Console

The Console is Matterhorn's personal product surface. It remains a
self-contained vanilla-JavaScript client of the public REST API—no engine,
store, or private browser route.

<!-- screenshot: console-wall -->
![Matterhorn Console — unified matter wall](images/console-wall.png)
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
- scoped matter detail, corrections, reversible subject merges, deterministic
  query, ingest, and chat;
- `GET /v1/events` and `GET /v1/connections`;
- collection mail connector resources;
- AI config, redacted status, and test resources.

## Product layout

### Left — sources and configuration

**Mailboxes** lists every account with provider, login, folder, target scope,
watermark, schedule, password state, and per-account actions. The Add mailbox
form supports presets and manual IMAP. See [Mail connectors](mail.md).

<!-- screenshot: console-mailboxes -->
> **Screenshot placeholder:** Dana Reyes's fictional personal and octo-org
> mailbox accounts with independent scopes, watermarks, schedules, and
> actions.

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

<!-- screenshot: console-ai -->
> **Screenshot placeholder:** runtime AI provider, model, timeout, redacted key
> state, Test, and Save AI controls.

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
a card opens a modal with its scope-aware current values, evidence state/source
IDs, aliases from merged titles, and per-value human correction action. The
same detail modal has a ledger-style **Merge into…** form populated from the
other matters currently on the wall in that scope. It requires a reason and
name, reuses the correction sender preference, submits only to
`POST /v1/scopes/{scope}/merges`, then closes and refreshes the wall.
Correction uses its own modal, then refreshes the open detail modal and wall.

<!-- screenshot: console-detail-modal -->
> **Screenshot placeholder:** a matter detail modal over the unified wall,
> showing predicate values, origin, evidence state/source IDs, merged-title
> aliases, Correct actions, and the Merge into form.

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

## 30-second fixture demo

1. Start `mh console`, expand **Feed input**, choose **Load sample**, then
   **Extract**. The exact fictional text routes to the packaged fixture and
   does not need an AI key.
2. Keep the **All** scope chip selected and open the new matter card. Its
   evidence-bearing detail appears in a modal over the unified wall.
3. Choose **correct** beside a value to open the correction modal; close it
   without submitting if you only want a read-only walkthrough.
4. With two matters in the same scope, open the duplicate, choose its canonical
   target under **Merge into…**, and record a reason. The duplicate disappears
   from the wall and its title appears as an alias on the target; `mh unmerge`
   or the public unmerge REST resource reverses the correction.
5. Select `personal` in **Working scope** and run a deterministic query. Chat
   remains hidden until a real AI key is configured.

## Safety

Credentials are process-memory-only. The browser stays REST-only. Read routes
remain zero-model; provider calls exist only on write extraction and optional
chat consumption. Before public deployment, add authentication,
authorization, TLS, request limits, and a trusted reverse proxy.
