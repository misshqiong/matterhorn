# 邮件连接器

Matterhorn 邮件连接器从一个 IMAP 文件夹拉取 RFC822 邮件，复用 `.eml` / mbox
输入使用的同一个 email adapter，再通过公共 `Engine.add` 入口排队并同步 flush。
网络与邮件解析只使用 Python 标准库 `imaplib` 和 `email`。

> **截图占位：** Console Connectors sheet，包含 Gmail 配置、仅内存凭证提示、UID
> watermark、上次运行统计和下次调度时间。

## CLI 配置与同步

```console
mh mail setup \
  --provider gmail \
  --account dana@example.com \
  --folder INBOX \
  --interval 1h \
  --initial-window 50 \
  --scope work
```

命令只把非敏感字段写入 `matterhorn.toml`：

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

其他服务商可使用 `--provider manual --host ... --port ... --ssl`。缺少必要
flag 时，`mh mail setup` 会交互询问。密码或授权码不是 setup flag，也绝不会写入
TOML 或数据库；同步进程只从环境变量或隐藏输入 prompt 获取：

```console
export MATTERHORN_MAIL_PASSWORD='provider-app-password'
mh mail sync
unset MATTERHORN_MAIL_PASSWORD
```

首次普通同步没有 UID watermark 时，连接器只选择最近的 `initial_window`
封邮件（默认 `50`），随后把邮箱最大 UID 保存为 watermark；下一次从该位置增量
同步。因此，已有大量邮件的真实邮箱不会在首次验收时被默认全量拉取。可通过
`mh mail setup --initial-window N`、Console 表单或 `[mail]` TOML 字段设置。

`mh mail sync` 输出成功映射的 `pulled` 数、全部 `filtered` 丢弃数、
`parse_errors`、首次同步实际使用的 `effective_window`、`cards_produced`、
`new_assertions`、`new_matters` 与 UID watermark。单封畸形邮件或无法提取出可读
文本的 HTML 邮件会被计数并跳过，不会中断其余邮件。HTML-only 邮件会转换为
可读文本；存在非附件 `text/plain` part 时仍优先使用它。

`mh mail sync --backfill` 是明确的全历史入口：从 UID 1 开始并忽略
`initial_window`。邮件正文会按 UID 批量 FETCH，而不是每封邮件一次网络往返。
连接器以 `imap:<user>@<host>/<folder>` 保存位置：整数 UID watermark
是连接器位置，opaque cursor 是 IMAP `UIDVALIDITY`。

若 `UIDVALIDITY` 改变，普通同步会在 search/fetch 之前停止，报告明确显示新旧值。
确认邮箱重置后，再用 `mh mail sync --backfill` 授权全量重拉；Matterhorn 原有
幂等规则仍会过滤已经见过的 observation。

## Console 流程

启动 `mh console`，打开 **Produce → Connectors · Mail**：

1. 选择 provider 预设，或手动修改 host、port、SSL；
2. 输入账号、文件夹、间隔、首次同步窗口与 app password / 授权码；
3. 保存配置；当前 Console scope 会成为定时同步目标；
4. 点击 **Sync now**；按钮显示 **Syncing…**，长任务期间状态面板持续轮询，
   最终显示报告或错误、UID watermark、UIDVALIDITY、拉取/丢弃/新增事项数与
   下次运行时间；成功或失败后按钮都会重新启用。

现有 serve scheduler 支持 `15min`、`1h`、`6h`；`off` 关闭自动拉取。服务重启
后非敏感配置仍在，但 Console 输入的凭证已被遗忘，必须重新输入；若新进程带有
`MATTERHORN_MAIL_PASSWORD`，状态会显示 “loaded from environment”。

公共 REST 接口为：

- `POST /v1/connectors/mail/config`
- `GET /v1/connectors/mail/status`
- `POST /v1/connectors/mail/sync`

config POST 可以接收 `password`，但其响应和 status GET 都没有 password 字段。

## 凭证硬规则

邮件密码、app password、授权码或 token 只存在于当前进程内存：

- 不写入 `matterhorn.toml`、SQLite 或 PostgreSQL；
- 不从任何 GET 返回；
- CLI 不回显；
- provider 认证异常原文会被丢弃，日志只记录固定的脱敏错误。

因此服务重启会刻意忘记在 Console 中输入的凭证。

## Provider 设置与授权

以下预设已于 2026-07-30 对照服务商资料核验：

| Provider | IMAP 预设 | 授权帮助 |
| --- | --- | --- |
| Gmail | `imap.gmail.com:993`，SSL | [Google app passwords](https://support.google.com/accounts/answer/185833) |
| Outlook.com | `outlook.office365.com:993`，SSL/TLS | [Microsoft IMAP 设置](https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com) |
| iCloud | `imap.mail.me.com:993`，SSL | [Apple app-specific passwords](https://support.apple.com/en-us/102654) |
| QQ 邮箱 | `imap.qq.com:993`，SSL | [QQ 授权码](https://wx.mail.qq.com/list/readtemplate?name=app_intro.html#/agreement/authorizationCode) |
| 163 邮箱 | `imap.163.com:993`，SSL | [163 客户端授权帮助](https://help.mail.163.com/faq.do?m=list&categoryID=197) |

Outlook 特别说明：Microsoft 当前页面明确写明 Outlook.com 要求
OAuth2/Modern Auth，同时又为部分设备提供 app-password 指引。本连接器按本功能
约束实现标准库 IMAP LOGIN app-password/授权码流程；若 tenant 拒绝 basic IMAP
认证，则要等 Matterhorn 增加 OAuth2 token flow 才能连接。认证失败时 Console 会
展示 Microsoft 当前帮助链接。

## 公网部署警告

Matterhorn 默认仍只绑定 `127.0.0.1`。不要把 Console 或 mail-config 接口直接
暴露到互联网。任何公网部署都必须在前面增加认证、授权、TLS、请求体限制与可信反向
代理。凭证仅驻留内存并不能替代传输安全或接口访问控制。
