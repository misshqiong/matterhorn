# Matterhorn Console

Console 现在是 Matterhorn 的成熟个人产品面，同时仍是公共 REST API 的自包含
vanilla-JavaScript 客户端：浏览器没有 engine、store 或私有旁路。

<!-- screenshot: console-wall -->
![Matterhorn Console — unified matter wall](images/console-wall.png)
> **截图占位：** 三栏 Console：左栏多邮箱、AI 与 Feed 配置；中心跨全部 scope 的
> ledger-paper 事项墙；右栏带 scope selector 的 Chat 与查询工作台。

## 启动与边界

```console
pip install 'matterhorn-memory[api]'
mh console
```

默认打开 `http://127.0.0.1:8000/console`；`--no-open` 不拉起浏览器。
`mh serve --console` 挂载同一页面，`/mcp` 提供同一个九工具 hub。v1 没有多租户
鉴权，因此默认仍只绑定 loopback。

浏览器只调用公共接口：`GET /v1/matters` 与 `?scope=`、scope-aware 详情/纠错/
查询/摄入/Chat、`GET /v1/events`、`GET /v1/connections`、mail collection API
以及 AI 配置/脱敏状态/Test API。

## 三栏产品布局

### 左栏：来源与配置

**Mailboxes** 逐账号显示 provider、登录名、folder、目标 scope、watermark、
调度、密码状态与单账号操作；Add mailbox 支持 preset 与 manual IMAP。详见
[多邮箱连接器](mail.zh.md)。

<!-- screenshot: console-mailboxes -->
> **截图占位：** Dana Reyes 的虚构 personal / octo-org 两个邮箱，各自显示 scope、
> watermark、调度与操作。

**AI** 同时配置写网关与 Console Chat：

```toml
[ai]
provider = "openai-compatible" # 或 "anthropic"
base_url = "https://provider.example/v1"
model = "provider-model"
timeout = 60.0
```

`POST /v1/connectors/ai/config` 可接收 `api_key`，但 key 永不写 TOML、不从 GET
返回、不进入日志。`GET /v1/connectors/ai/status` 显示
`loaded in process memory`、`loaded from environment` 或
`re-enter API key`。重启后非敏感设置仍在，Console 输入的 key 会消失。

优先级是：

1. Console 运行时配置；
2. `MATTERHORN_PROVIDER`、`MATTERHORN_BASE_URL`、`MATTERHORN_MODEL`、
   `MATTERHORN_TIMEOUT`、`MATTERHORN_API_KEY`（以及 provider 原生 key）。

修改 AI 配置会替换 composed Engine 后续 extraction/distillation 使用的 gateway，
并重建 Chat runner；已经在运行的 provider 调用继续使用自己捕获的对象。

<!-- screenshot: console-ai -->
> **截图占位：** 运行时 AI provider、model、timeout、脱敏 key 状态，以及 Test /
> Save AI 控件。

**Test** 通过 `POST /v1/connectors/ai/test` 发起一次极小的结构化调用。探测失败时
候选配置与 key 都不会保存。没有可用 key 时 Chat 保持隐藏，依赖 extraction 的功能
显示现有的明确 gateway-required 错误。

**Feed input** 支持粘贴 chat、YAML/JSON Message、EML/mbox 与文件上传。精确匹配的
虚构 Dana Reyes / octo-org 样例可使用随包 fixture gateway。

### 中心：统一事项墙

默认调用 `GET /v1/matters` 展示全部 scope。每张 ledger-paper 卡显示 scope tag、
status stamp、owner、due（逾期红色）与 next step。顶部 chips 可切 All 或单 scope。
点击卡片会打开 modal，展示 scope-aware 当前值、evidence 状态/source ID 以及每个值
的人工纠错入口。纠错使用第二个 modal，提交后会刷新仍打开的详情 modal 与事项墙。

<!-- screenshot: console-detail-modal -->
> **截图占位：** 统一事项墙上方的详情 modal，含 predicate value、origin、evidence
> 状态/source ID 与 Correct 操作。

Activity 与 Connections 保留在墙下方的可折叠 strip，约每五秒刷新。

### 右栏：消费

Chat 与确定性查询工作台共用明确的 scope selector。单 scope wall filter 会带动它；
否则跟随最后打开的卡，最后 fallback 到第一个 scope。Chat 工具仍严格锁定 route
scope，只有 `list_matters`、`query_current`、`query_timeline`、`query_at`、
`query_by_person`。模型只能看到查询结果与 evidence ID，看不到 raw Record/store。

## AI 公共资源

- `POST /v1/connectors/ai/config`
- `GET /v1/connectors/ai/status`
- `POST /v1/connectors/ai/test`

脱敏 AI 状态也进入 `GET /v1/connections`。

## 30 秒 fixture 演示

1. 启动 `mh console`，展开 **Feed input**，依次点 **Load sample**、**Extract**；
   精确匹配的虚构文本走随包 fixture，不需要 AI key。
2. 保持 **All** scope chip，点击新事项卡；带证据详情会以 modal 覆盖在统一墙上。
3. 点击某个值旁的 **correct** 打开纠错 modal；只读演示可直接关闭而不提交。
4. 在 **Working scope** 选 `personal` 并跑一次确定性查询。配置真实 AI key 前，
   Chat 保持隐藏。

## 安全

凭证只驻留进程内存；浏览器只走 REST；读路径继续零模型，provider 只用于写侧提取
与可选 Chat 消费。公网部署前必须增加认证、授权、TLS、请求限制与可信反向代理。
