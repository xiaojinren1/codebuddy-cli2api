# CodeBuddy CLI 免费资源端点逆向地图

> 逆向对象：本机实例 `@tencent-ai/codebuddy-code@2.122.0`
> 主 bundle：`~/.local/lib/node_modules/@tencent-ai/codebuddy-code/dist/codebuddy-headless.js`（webpack 压缩，16MB）
> 产品配置：`product.json` → `endpoint = https://www.codebuddy.ai`，`platform = CLI`，`auth.type = cli-external-link`，`prefixPath = /plugin`
>
> 本文由 4 个 subagent 并行逆向核实（WebSearch/WebFetch、图像、视频、鉴权模型）。

---

## 一、CLI 的鉴权模型（两套，务必分清）

| 凭据 | 用途 | 请求头 |
|---|---|---|
| **OAuth `accessToken`** | CLI 本体所有工具调用（搜索/图/视频/对话） | `Authorization: Bearer <accessToken>` |
| **`ck_` API key** | 仅网关项目代理的 `/v2/chat/completions` | `X-API-Key` / `Bearer <ck_>` |

CLI 登录流程（外链式，非 device-code）：

```
POST /v2/plugin/auth/state?platform=CLI
   → { state, authUrl: "https://www.codebuddy.ai/login?platform=CLI&state=..." }
打开 authUrl（浏览器）
轮询 GET /v2/plugin/auth/token?state=<state>        → { accessToken, refreshToken, expiresAt, ... }
GET  /v2/plugin/login/account?state=<state>         → { uid, nickname, enterpriseId, ... }
```

- 登录态落盘：`~/.codebuddy/auth/Tencent-Cloud.coding-copilot.info`，明文 JSON：
  ```json
  { "auth": { "accessToken": "...", "refreshToken": "...", "expiresAt": 0 },
    "account": { "uid": "...", "nickname": "..." }, "accounts": [ ... ] }
  ```
- 刷新：`POST /v2/plugin/auth/token/refresh`，头带 `X-Refresh-Token`。

---

## 二、免费资源端点地图

> 通用请求头（产品头，所有调用随行）：`X-Product: SaaS`、`X-IDE-Type: CLI`、`X-IDE-Name: CLI`、`X-IDE-Version: 2.118.2`、`X-Product-Version: 2.118.2`、`User-Agent: CLI/2.118.2 CodeBuddy/2.118.2`、`X-Request-From: cli`，以及 `Authorization: Bearer <accessToken>`。
> **注意**：这些路径**都不带 `/plugin` 前缀**（只有 auth 路径带 `/v2/plugin/...`）。

### 1. WebSearch — `POST /agenttool/v1/search`
- **超时**：20s（AbortController，非流式）
- **Request body**：
  - `query` (string)
  - `type`: 固定 `"text2text"`
  - `max_results`: 硬编码 `5`（非用户输入）
  - `query_keyword_groups` (string[], 可选, ≤5)
  - `topic` (可选): `general|news|programming|documentation|academic|finance|technology|legal|medical`
  - 域名改写：`allowed_domains` → query 追加 `(site:d1 OR site:d2)`；`blocked_domains` → 追加 `(-site:d1 -site:d2)`
- **Response**：`{ code, msg, provider, results: [{ title, url, snippet }], total_results, response_time_ms }`

### 2. WebFetch — `POST /agenttool/v1/webfetch`
- **超时**：远程 20s / 本地 fallback 30s（远程失败回退本地 fetch + turndown 转 markdown）
- **Request body**：`{ url, format: "markdown", prompt, timeout: 20, max_length: 100000 }`
- **Response**：`{ code, msg, content }`（`code` 真值或 content 空均报错）

### 3. 文生图 — `POST /v2/images/generations` ｜ 图生图 — `POST /v2/images/edits`
- **Auth**：`Bearer <accessToken>` + 全局产品头拦截器
- **Request body**：`{ model, prompt, size: "1024x1024", response_format: "b64_json", n?, quality?, style?, background? }`
  - 图生图额外带 `image`（**data-URI 字符串数组**；`processImageInput` 会把文件路径/http URL/裸 base64 转成 `data:image/...;base64,...`）。**无独立 mask 字段**。
- **模型选择**：`resolveModel(TEXT_TO_IMAGE_MODEL_TAG)` → 回退 `"hunyuan-image-v3.0"`；图生图 `IMAGE_TO_IMAGE_MODEL_TAG` → `"hunyuan-image-v2.0-general-edit"`
- **Response**：`{ code, msg, request_id, data: { data: [ { b64_json } | { url } ] } }`（网关读 `data.data[i].b64_json` 或 `.url`）
- **同步**，无轮询。

### 4. 文生视频 — `POST /v2/videos/generations` → 轮询 `POST /v2/videos/tasks`
- **提交** `POST /v2/videos/generations` body：
  `{ prompt, model, seconds: 5, negative_prompt?, watermark?, image_url?, last_image_url?, extra_parameters: { resolution: "720P"|"1080P", aspect_ratio?, enable_audio? } }`
  - 返回 `{ code, msg, requestId, data: { id } }` → **task id = `data.id`**
  - 模型 tag：`kling-v3-t2v`(文生视频) / `kling-v3-i2v`(图生视频)
- **轮询** `POST /v2/videos/tasks` body：`{ task_id }`
  - 每 **5s** 一次，超时 **600s**；status 仅分支 `"completed"`（返回 `data`）/ `"failed"`（抛错），其余（queued/processing…）继续循环
  - 结果：`data[].url` 即 mp4 下载地址（代码中未引用过期字段，视为短时有效）

---

## 三、关键结论：ck_ 网关目前代理不了这些端点

> **实测勘误（2026-08-01）**：本文档早期结论「免费资源在 `www.codebuddy.ai`」不成立——
> 本账号的 OAuth token 在 `www.codebuddy.ai` 会被边缘网关直接 401（APISIX），
> 但在 `copilot.tencent.com`（桌面 WorkBuddy 默认端点）与 `www.codebuddy.cn`
> 上全部端点（search/webfetch/tempkey/tts/images/videos/checkin）均可正常调用。
> 网关资源路由默认指向 `https://copilot.tencent.com`（`CODEBUDDY_RESOURCE_BASE_URL` 可覆盖）。


4 个 agent 一致确认：**所有免费资源端点都强制 `Bearer <OAuth accessToken>`**，代码中 `prepareRequest()` 在无登录态时直接抛
`"Authentication required. Please login first."`，**不存在 ck_ key 兜底分支**。

全局 axios 拦截器虽会附带 `X-API-Key`（来自 `CODEBUDDY_API_KEY` env），但 **accessToken 始终必填**。

→ 要让网关也能暴露这些「免费资源」，有两条路：

1. **网关自己做 OAuth 登录**（推荐）：把上面「登录流程」搬进网关——`/v2/plugin/auth/state` 拿 state + authUrl，浏览器登录后轮询 `/auth/token` 取 accessToken 并缓存（可复用 `~/.codebuddy/auth/...` 那份 session 文件），之后免费资源路由用 `Bearer <accessToken>` 转发。
2. **实测 ck_ 是否被服务端接受**（待验证）：直接对 `/agenttool/v1/search`、`/v2/images/generations` 等发 `X-API-Key: ck_...` 不带 OAuth，看服务端是否放行。客户端从未尝试过这条路，成败未知，需要 live probe。

---

## 四、下一步（已实现）

- [x] **方案 1 已实现（2026-08-01）**：网关新增 OAuth session 模块（`codebuddy_gateway/auth.py`，复用桌面/CLI 的 `.info` session 文件，`--login` 交互登录）与资源路由（`codebuddy_gateway/resources.py`，`/v1/search|web_search|webfetch|images/*|videos/*|asr|tts|checkin*` 等 13 条别名 + 原始路径直通），全部 `Bearer <accessToken>` 转发，401 自动刷新重试。
- [x] **每日签到自动化**：网关内置守护线程每 UTC 日自动领 `/v2/billing/meter/daily-checkin`（100 积分/天），不依赖客户端，`CODEBUDDY_AUTO_CHECKIN=0` 关闭。
- [ ] **方案 2（ck_ 直连）**：未实测，保持开放——若服务端接受 `X-API-Key` 单凭据，可省掉 OAuth session 迁移。
- [x] 域名：资源端点默认 `https://copilot.tencent.com`（`CODEBUDDY_RESOURCE_BASE_URL` 可覆盖），`www.codebuddy.ai` 对 OAuth token 401，不可用。
