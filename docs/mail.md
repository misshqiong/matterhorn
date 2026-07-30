# Mail connector

Matterhorn’s mail connector pulls RFC822 messages from one IMAP folder, maps
them through the same email adapter used by `.eml` and mbox ingest, queues them
through the public `Engine.add` boundary, and flushes synchronously. Network
and message handling use only Python’s `imaplib` and `email` modules.

> **Screenshot placeholder:** the Console Connectors sheet showing Gmail
> settings, the memory-only credential note, UID watermark, last-run counts,
> and next scheduled run.

## CLI setup and sync

Configure a preset:

```console
mh mail setup \
  --provider gmail \
  --account dana@example.com \
  --folder INBOX \
  --interval 1h \
  --initial-window 50 \
  --scope work
```

The command writes only non-secret fields to `matterhorn.toml`:

```toml
[mail]
provider = "gmail"
host = "imap.gmail.com"
port = 993
ssl = true
user = "dana@example.com"
folder = "INBOX"
interval = "1h"
initial_window = 50
scope = "work"
```

Use `--provider manual --host ... --port ... --ssl` for another provider.
Without the necessary flags, `mh mail setup` prompts interactively.

The password or provider authorization code is never a setup flag and is never
written to TOML or the database. Supply it to the sync process through the
environment or a hidden interactive prompt:

```console
export MATTERHORN_MAIL_PASSWORD='provider-app-password'
mh mail sync
unset MATTERHORN_MAIL_PASSWORD
```

On the first normal sync, when no UID watermark exists, the connector selects
only the most recent `initial_window` messages (default `50`) and then stores
the mailbox's maximum UID. The next run is incremental from that watermark.
This bounds owner acceptance against an established mailbox instead of
pulling its entire history. Set `initial_window` with `mh mail setup
--initial-window N`, the Console form, or the `[mail]` TOML field.

`mh mail sync` reports successfully mapped `pulled` messages, all `filtered`
drops, `parse_errors`, the first-run `effective_window`, `cards_produced`,
`new_assertions`, `new_matters`, and the new UID watermark. A malformed message
or an HTML body that yields no readable text is counted and skipped without
aborting the remaining messages. HTML-only mail is converted to readable text;
when a non-attachment `text/plain` part exists, it remains preferred.

`mh mail sync --backfill` is the explicit full-history path and starts at UID
1, ignoring `initial_window`. IMAP message bodies are fetched in UID batches
rather than one network round trip per message. The connector stores a position for
`imap:<user>@<host>/<folder>`: the integer UID watermark is connector state,
and the opaque cursor is IMAP `UIDVALIDITY`.

If `UIDVALIDITY` changes, a normal sync stops before searching or fetching
messages. The report states the old and new values. Review the mailbox change,
then use `mh mail sync --backfill` to authorize a full re-pull. Matterhorn’s
ordinary idempotency rules still suppress observations it has already seen.

## Console flow

Start `mh console` and open **Produce → Connectors · Mail**:

1. Choose a provider preset or edit host, port, and SSL manually.
2. Enter the account, folder, interval, first-sync window, and app
   password/authorization code.
3. Save configuration. The active Console scope becomes the scheduled target.
4. Click **Sync now**. The button shows **Syncing…** and the status sheet keeps
   polling during a long run. It eventually shows the report or error, UID
   watermark, UIDVALIDITY, pulled/dropped/new-matter counts, and next run. The
   button is re-enabled after either success or failure.

The existing serve scheduler runs mail sync at `15min`, `1h`, or `6h`; `off`
disables periodic pulls. On restart, non-secret settings remain, but the
credential must be re-entered unless `MATTERHORN_MAIL_PASSWORD` is present in
the new process environment.

The public REST resources are:

- `POST /v1/connectors/mail/config`
- `GET /v1/connectors/mail/status`
- `POST /v1/connectors/mail/sync`

The config POST may accept `password`; neither its response nor status GET has
a password field.

## Credential rule

The mail password, app password, authorization code, or token lives only in
the current process’s memory:

- it is never written to `matterhorn.toml`, SQLite, or PostgreSQL;
- it is never returned by a GET response;
- it is never echoed by the CLI;
- provider authentication exception text is discarded, and the connector logs
  only a fixed redacted failure message.

This means a restart intentionally forgets a credential entered in Console.

## Provider settings and authorization

The presets were checked against provider documentation on 2026-07-30:

| Provider | IMAP preset | Authorization help |
| --- | --- | --- |
| Gmail | `imap.gmail.com:993`, SSL | [Google app passwords](https://support.google.com/accounts/answer/185833) |
| Outlook.com | `outlook.office365.com:993`, SSL/TLS | [Microsoft IMAP settings](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com) |
| iCloud | `imap.mail.me.com:993`, SSL | [Apple app-specific passwords](https://support.apple.com/en-us/102654) |
| QQ Mail | `imap.qq.com:993`, SSL | [QQ authorization code](https://wx.mail.qq.com/list/readtemplate?name=app_intro.html#/agreement/authorizationCode) |
| 163 Mail | `imap.163.com:993`, SSL | [163 client authorization help](https://help.mail.163.com/faq.do?m=list&categoryID=197) |

Important Outlook caveat: Microsoft’s current page says Outlook.com requires
OAuth2/Modern Auth, even though it also links app-password guidance for some
devices. This connector implements the requested stdlib IMAP LOGIN
app-password/code flow; a tenant that rejects basic IMAP authentication cannot
connect until Matterhorn gains an OAuth2 token flow. Console surfaces
Microsoft’s current help page on authentication failure.

## Public deployment warning

Matterhorn still binds to `127.0.0.1` by default. Do not expose Console or the
mail-config endpoint directly to the internet. Put authentication,
authorization, TLS, request-size enforcement, and a trusted reverse proxy in
front of any public deployment. The in-memory credential rule does not replace
transport security or endpoint access control.
