# 多邮箱连接器

Matterhorn 可把多个 IMAP 账号分别同步进独立 scope，同时保证每个密码只存在于
进程内存。每个账号都有独立的配置、凭证状态、UID watermark、UIDVALIDITY、
调度、上次报告与下次运行时间；serve scheduler 会逐个 tick 全部账号。

<!-- screenshot: console-mailboxes -->
> **截图占位：** Console 左栏同时显示两个虚构邮箱（`Dana Reyes · personal` 与
> `Dana Reyes · octo-org`）、各自 watermark / 调度，以及 Add mailbox sheet。

## TOML 形状与迁移

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

可选 `name` 就是稳定的 `account_id`；未提供时按 `user@host/folder` 推导。
旧版单个 `[mail]` 会被读取为一个账号，并在下一次邮件配置保存时改写成
`[[mail.accounts]]`。其他 TOML table 与已有账号都不会丢失。密码和授权码永远
不是 TOML key。

## CLI

`mh mail setup` 现在追加/更新账号，而不是覆盖：

```console
mh mail setup --name personal --provider gmail \
  --account dana.reyes@example.test --folder INBOX \
  --interval 1h --scope personal

mh mail setup --account-id octo-org --provider manual \
  --host imap.octo-org.example --account dana@octo-org.example \
  --folder Matters --interval 15min --scope octo-org
```

同步和 reset 用 `--account` 选择：

```console
export MATTERHORN_MAIL_PASSWORD='provider-app-password'
mh mail sync --account personal
mh mail reset --account personal --yes
unset MATTERHORN_MAIL_PASSWORD
```

只有恰好一个账号时才可省略 `--account`；多账号时 CLI 会安全报错并列出可选 ID。
还可使用按账号生成的环境变量：把 ID 转成大写、非字母数字替换为 `_`，例如
`MATTERHORN_MAIL_PASSWORD_OCTO_ORG`。旧的
`MATTERHORN_MAIL_PASSWORD` 仍作为 fallback。

## 同步语义

首次普通同步只拉最近 `initial_window` 封；之后只拉高于该账号 watermark 的 UID。
`--backfill` 明确从 UID 1 开始。持久位置键仍是
`imap:<user>@<host>/<folder>`，不同账号/文件夹不会误共享 watermark。

UIDVALIDITY 变化时普通同步会在重拉前停止；确认后再明确 backfill。Reset 只删除
选中账号的位置。REST Remove 会删除配置与内存密码，但刻意保留 watermark 数据，
delete 响应会明确说明这一点。

## Console 与 REST

Console 左栏逐账号显示 provider、登录名、folder、目标 scope、watermark、下次
运行、密码状态，并提供 **Sync now / Re-pull recent / Remove**。Add mailbox
表单复用 provider preset。多个账号可同步进不同 scope，中心墙会统一显示其事项。

标准 collection API：

- `GET /v1/connectors/mail/accounts`
- `POST /v1/connectors/mail/accounts`
- `DELETE /v1/connectors/mail/accounts/{account_id}`
- `POST /v1/connectors/mail/accounts/{account_id}/sync`
- `POST /v1/connectors/mail/accounts/{account_id}/reset`

旧 `/config`、`/status`、`/sync`、`/reset` 继续作为“第一个账号”的兼容 alias；
新调用方应使用 collection API。

任何 GET 或响应模型都没有 password。认证异常会在日志/响应前脱敏。进程重启会
刻意忘记 Console 输入的密码，状态变成 `re-enter password`；新进程若加载环境
凭证则显示 `loaded from environment`。

Provider preset 与授权帮助见英文表格或 Console 链接。Outlook.com 可能强制
OAuth2/Modern Auth；当前 IMAP LOGIN 连接器无法连接拒绝该方式的 tenant。

Matterhorn 默认绑定 `127.0.0.1`；公网部署前必须增加认证、授权、TLS 与可信代理。
