# Mail connectors

Matterhorn can pull multiple IMAP accounts into independent scopes while
keeping every password in process memory. Each account has its own settings,
credential state, UID watermark, UIDVALIDITY, schedule, last report, and next
run. The serve scheduler ticks every configured account.

<!-- screenshot: console-mailboxes -->
> **Screenshot placeholder:** the Console left rail with two fictional
> mailboxes (`Dana Reyes · personal` and `Dana Reyes · octo-org`), independent
> watermarks and schedules, plus the Add mailbox sheet.

## Configuration shape and migration

`matterhorn.toml` uses repeated account tables:

```toml
[[mail.accounts]]
name = "personal"
provider = "gmail"
host = "imap.gmail.com"
port = 993
ssl = true
user = "dana.reyes@example.test"
folder = "INBOX"
interval = "1h"
initial_window = 50
scope = "personal"

[[mail.accounts]]
name = "octo-org"
provider = "manual"
host = "imap.octo-org.example"
port = 993
ssl = true
user = "dana@octo-org.example"
folder = "Matters"
interval = "15min"
initial_window = 25
scope = "octo-org"
```

`name` is optional. It becomes the stable `account_id`; otherwise Matterhorn
derives `user@host/folder`. A legacy single `[mail]` table is loaded as one
account and is rewritten to `[[mail.accounts]]` on the next mail save. Other
TOML tables and every existing account are retained.

Passwords and authorization codes are never TOML keys.

## CLI

`mh mail setup` appends or updates one account instead of replacing the mail
configuration:

```console
mh mail setup \
  --name personal \
  --provider gmail \
  --account dana.reyes@example.test \
  --folder INBOX \
  --interval 1h \
  --initial-window 50 \
  --scope personal

mh mail setup \
  --account-id octo-org \
  --provider manual \
  --host imap.octo-org.example \
  --account dana@octo-org.example \
  --folder Matters \
  --interval 15min \
  --scope octo-org
```

Select an account for sync and reset:

```console
export MATTERHORN_MAIL_PASSWORD='provider-app-password'
mh mail sync --account personal
mh mail reset --account personal --yes
unset MATTERHORN_MAIL_PASSWORD
```

`--account` is optional only when exactly one account exists. With multiple
accounts, the CLI fails safely and lists the available IDs. A per-account
environment variable can be formed by uppercasing the account ID and replacing
non-alphanumeric characters with `_`, for example
`MATTERHORN_MAIL_PASSWORD_OCTO_ORG`; the legacy
`MATTERHORN_MAIL_PASSWORD` remains a fallback.

## Sync semantics

On a first normal sync, only the newest `initial_window` messages are pulled.
Later runs fetch UIDs above that account's watermark. `--backfill` explicitly
starts at UID 1. The durable position key remains
`imap:<user>@<host>/<folder>`, so accounts and folders cannot share a
watermark accidentally.

If UIDVALIDITY changes, a normal run stops before re-pulling. Inspect the
reported old/new values, then explicitly backfill. Reset removes only the
selected account's sync position. Removing an account through REST removes its
configuration and in-memory password but deliberately retains its watermark
data; the delete response states that retention.

## Console and REST

The Console left rail lists every mailbox with provider, login, folder, target
scope, watermark, next run, password state, and **Sync now**, **Re-pull
recent**, and **Remove** actions. The Add mailbox form uses the same provider
presets. Accounts can sync simultaneously into different scopes, and their
matters appear together on the center wall.

Canonical collection resources:

- `GET /v1/connectors/mail/accounts`
- `POST /v1/connectors/mail/accounts`
- `DELETE /v1/connectors/mail/accounts/{account_id}`
- `POST /v1/connectors/mail/accounts/{account_id}/sync`
- `POST /v1/connectors/mail/accounts/{account_id}/reset`

The former `/v1/connectors/mail/config`, `/status`, `/sync`, and `/reset`
resources remain compatibility aliases to the first account. New callers
should use the collection resources.

No GET or response model contains a password. Authentication errors are
redacted before logging or returning. A restart intentionally forgets
Console-entered passwords; status then says `re-enter password`, unless the
new process loaded an environment credential.

## Provider presets

| Provider | IMAP preset | Authorization help |
| --- | --- | --- |
| Gmail | `imap.gmail.com:993`, SSL | [Google app passwords](https://support.google.com/accounts/answer/185833) |
| Outlook.com | `outlook.office365.com:993`, SSL/TLS | [Microsoft IMAP settings](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com) |
| iCloud | `imap.mail.me.com:993`, SSL | [Apple app-specific passwords](https://support.apple.com/en-us/102654) |
| QQ Mail | `imap.qq.com:993`, SSL | [QQ authorization code](https://wx.mail.qq.com/list/readtemplate?name=app_intro.html#/agreement/authorizationCode) |
| 163 Mail | `imap.163.com:993`, SSL | [163 client authorization help](https://help.mail.163.com/faq.do?m=list&categoryID=197) |

Outlook.com may require OAuth2/Modern Auth; the current connector implements
IMAP LOGIN and cannot connect to a tenant that rejects it.

Matterhorn binds to `127.0.0.1` by default. Put authentication, authorization,
TLS, and a trusted proxy in front before any public deployment.
