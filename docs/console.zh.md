# Matterhorn Console

Console 是 Matterhorn 面向运维者、开发者和演示者的操作面。它是公共 REST API
的静态客户端，与 API 同源、同端口提供；没有数据库或引擎私有旁路。

> **截图占位：** scope 导航、事项卡、当前值纠错、查询工作台、Feed receipt 与
> evidence chips 的 Console 总览。

## 启动

安装 API extra，然后启动：

```console
pip install 'matterhorn-memory[api]'
mh console
```

默认监听 `127.0.0.1:8000`，打印 URL，并用默认浏览器打开
`http://127.0.0.1:8000/console`。自动化环境可加 `--no-open`。
`mh serve --console` 会挂载同一页面，但不会主动打开浏览器。

Console 与 REST API 共用一个端口。浏览器只调用已公开并进入 OpenAPI 的接口：

- `GET /v1/scopes` 与 scope 下的事项列表、事项详情；
- 四个确定性查询接口；
- `POST /v1/scopes/{scope}/corrections`；
- `POST /v1/scopes/{scope}/ingest`；
- 可选的 `POST /v1/scopes/{scope}/chat`。

默认只绑定 loopback 是刻意的。Matterhorn v1 不提供多租户认证与授权；任何公网部署
都必须在服务前增加认证与可信网络边界。

Dockerfile 也提供 `console` target：

```console
docker build --target console -t matterhorn-console .
docker run --rm -p 127.0.0.1:8000:8000 matterhorn-console
```

## Feed 输入格式

格式识别发生在服务端，而不是浏览器 JavaScript 中。支持三种粘贴内容：

1. 普通聊天行，如 `Dana Reyes: The launch is in progress.`；没有时间戳时，服务端
   按粘贴顺序合成递增时间；
2. 遵循最小 `Message` contract 的 YAML 或 JSON；
3. 原始 `.eml` / mbox 文本，由现有邮件 adapter 归一化。

无法识别时，错误会列出三种格式及各自的一行示例。ingest 和 chat 都有输入长度上限
和相互独立的进程内简单限流。

点击 **Load sample** 会载入虚构的 Dana Reyes / octo-org 对话，并用随包发布的预录
fixture gateway 响应生成事项，全程不需要 key。该 fixture 只匹配这份明确的虚构样例；
普通输入仍使用正常配置的写侧 gateway。

## 可选 Chat

只有通过 Matterhorn 原有环境变量配置了支持的 provider、model 和凭证时，Chat
窗口才会显示：

```console
export MATTERHORN_PROVIDER=openai-compatible  # 或 anthropic
export MATTERHORN_BASE_URL=https://provider.example/v1
export MATTERHORN_MODEL=provider-model
export MATTERHORN_API_KEY=...
export MATTERHORN_TIMEOUT=60
mh console
```

Anthropic 未设置 base URL 时默认使用 `https://api.anthropic.com`；仍支持
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY` provider 原生变量作为 fallback。

宿主侧循环最多执行六次工具调用。工具只有 `list_matters`、`query_current`、
`query_timeline`、`query_at`、`query_by_person`，并逐一映射到
`MatterhornService`。scope 由宿主路由固定，模型永远拿不到原始 records 或 store。
每条回答下方会渲染查询参数与返回证据 source ID 组成的 `依据/Evidence` chips。

## 30 秒演示

1. 运行 `mh console`，点击 **Load sample**，再点 **Extract**；完成态 receipt
   显示 gate breakdown，事项卡随即出现。
2. 打开 **octo-org Console launch**，在一个错误当前值旁点击 **Correct**，填写
   新值、原因和 `Dana Reyes` 并提交；页面立即刷新为带 `✏️ human` 徽标的新值，
   历史时间线仍保留。
3. 若已配置 Chat，询问 “What is the current progress?”；回答携带确定性查询和
   source ID 的 `依据/Evidence` chips，点击即可高亮对应事项。

## 下一版本（本次未实现）

- task receipts、sync status、events feed 运维视图；
- 多 scope 对比；
- export 按钮。
