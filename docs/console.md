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
- `GET /v1/stream` and scoped `/v1/scopes/{scope}/stream` raw staging reads;
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

### Center — focused matter ledger

The wall calls `GET /v1/matters` and defaults to **Focus**. A matter is active
when its status is `open`, `in_progress`, `blocked`, `pending`, or `ready`; or
its `updated_at` is within the last seven days; or `next_step`/`due` is set.
Active rows sort by newest `updated_at` (nulls last, then bytewise subject key).
Everything else is dimmed inside a collapsed **Archive (n)** drawer. The
**Focus / All** choice is stored in localStorage, while the scope chips still
select All or one scope and compose with that choice.

Scopes can be collected into ordered project sections in `matterhorn.toml`:

```toml
[console.groups]
dumbo = ["dumbo", "dumbo-dev", "cc-dumbo-server-*"]
matterhorn = ["dev", "matterhorn-dev"]
personal = ["mail"]
```

Values are scope patterns. Only a single trailing `*` wildcard is supported,
so `cc-dumbo-server-*` is a prefix match; all other patterns are exact. The
first group in declaration order that matches wins. Unmatched scopes enter the
final `other` section. Section headers show `active n · archived m`, and each
section's collapsed state is stored locally.

The wall is a horizontal register with one matter per row. From left to right,
each row contains the status and optional **updated** stamps; scope and strong
serif title; one-line current progress; a red blocker segment when `blocked_by`
is non-empty; then up to two source-conversation chips (`+n` for the rest),
owners, due date (red when overdue), next step, and compact update time. The
whole row opens the detail dialog.

Above the sections, **Today** shows up to three latest `status`/`progress`
changes per group whose `recorded_at` falls on the browser's current date. It
is built client-side from `GET /v1/events` and the matter payload, and is hidden
when there are no matching changes.

Opening a row opens a modal with its scope-aware current values, evidence
state/source IDs, aliases from merged titles, and per-value human correction action. A
ledger-style **Timeline** flattens the chronological `status`, `progress`, and
present `outcome` histories, showing each effective date, predicate, value,
source sender, and excerpt snippet. It is supplied by the same public
`GET /v1/scopes/{scope}/matters/{subject_key}` detail request; its timeline
values include additive `source_details` entries (`source_id`, `sender`,
`excerpt`, `uri`, `status`, `revoked_at`). The same detail modal has a
deterministic **Related** section. It links matters from any stored scope when
they share an active normalized handle or have normalized title-plus-alias
token Jaccard overlap of at least 0.5. Handle links rank first; clicking one
switches the existing dialog to that scope and matter. The read is skipped when
more than 20 scopes are present and never invokes a model. The detail modal
also has a ledger-style **Merge into…** form populated from the
other matters currently on the wall in that scope. It requires a reason and
name, reuses the correction sender preference, submits only to
`POST /v1/scopes/{scope}/merges`, then closes and refreshes the wall.
Correction uses its own modal, then refreshes the open detail modal and wall.

Seen state is client-side only. For each scope, localStorage key
`matterhorn.console.seen.<scope_id>` contains a JSON map from `subject_key` to
the ISO viewer time when its detail dialog was last opened. Opening the detail
writes that time and the next wall render clears the stamp. Matterhorn does
not persist per-user seen state on the server.

<!-- screenshot: console-detail-modal -->
> **Screenshot placeholder:** a matter detail modal over the unified wall,
> showing predicate values, origin, evidence state/source IDs, the progress
> timeline, merged-title aliases, Correct actions, and the Merge into form.

A collapsible **Raw stream** panel below the wall tails up to 50 non-revoked
staged messages, newest first, as monospaced time/scope/conversation/sender/
content lines. It follows the wall's All-or-one-scope filter. Its five-second
poll only re-renders when the newest `record_id` changes. Activity and
connections remain in a separate collapsible strip and refresh on the same
cycle. The open detail and its timeline follow the existing detail-fetch
pattern and are not refetched by that poll.

### Right — consumption

Chat and the deterministic query workbench share an explicit scope selector.
It follows a single wall filter; otherwise it follows the last-opened row,
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
2. Keep the **All** scope chip selected and open the new matter row. Its
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
