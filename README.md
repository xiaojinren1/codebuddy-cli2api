# codebuddy-cli2api

> A zero-dependency gateway that turns **Tencent CodeBuddy Code**'s CLI inference
> API into a universal, OpenAI-compatible endpoint — and a **Hermes Agent**
> provider plugin. Speaks **OpenAI**, **Anthropic**, **Gemini**, and **Codex /
> Responses** natively, with multi-key cache-aware rotation and vision support.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Dependencies](https://img.shields.io/badge/dependencies-0-success.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Formats](https://img.shields.io/badge/formats-OpenAI%20·%20Anthropic%20·%20Gemini%20·%20Codex-orange.svg)
![Models](https://img.shields.io/badge/models-20+-purple.svg)

CodeBuddy Code (`@tencent-ai/codebuddy-code`) is Tencent's coding agent CLI. Its
backend (`POST https://www.codebuddy.cn/v2/chat/completions`) speaks standard
OpenAI Chat Completions wire format — but with two hard constraints:

1. **Streaming only.** `stream: false` is rejected with `400`. Yet most clients
   (Hermes subagents, compression, title-gen, health checks, the Anthropic /
   Gemini / Codex SDKs) issue non-streaming calls.
2. **Data-URI images only.** Remote `http(s)` image URLs are rejected with `400`.

This gateway sits in front of CodeBuddy and removes both constraints, while also
translating between every major API format and rotating keys to maximize the
per-account prompt cache.

---

## ✨ Features

- **Four API formats, one backend** — OpenAI Chat Completions, Anthropic Messages,
  Google Gemini, and OpenAI Responses (Codex). Point any client at it.
- **Non-stream transparently supported** — forces `stream: true` upstream and
  aggregates the SSE into a complete response object, so non-streaming clients
  just work.
- **Multi-key cache-aware rotation** — prefix-affinity routing keeps each key's
  prompt cache warm; new prefixes go to the key with the **highest cache-hit
  rate**, tie-broken by fewest requests (even distribution). 401 disables, 429
  backs off, 5xx fails over — all before any bytes reach the client.
- **Vision** — normalizes images from all four formats to OpenAI `image_url`
  data URIs, and **fetches + inlines** remote URLs (which CodeBuddy rejects).
- **Zero dependencies** — Python 3.11+ standard library only. No `pip install`.
- **Hermes Agent plugin** — a drop-in `ProviderProfile` that routes Hermes
  through the gateway. No core modifications.

- **Hot-reloadable model catalog** — models the upstream accepts but the CLI's
  cached catalog has not learned about yet can be added by editing
  [`models.extra.json`](models.extra.json); the next request picks them up with
  no restart. See [docs/adding-models.md](docs/adding-models.md).

---

## 🏗️ Architecture

```
   OpenAI SDK  ────────┐
   Anthropic SDK ──────┤
   Gemini SDK  ────────┼──► ┌──────────────────────────────────────────┐
   Codex CLI   ────────┤    │            codebuddy_gateway             │
   Hermes Agent ───────┘    │          (127.0.0.1:8787, stdlib)        │
                            │                                          │
                            │  1. parse request in client's format     │
                            │  2. normalize images → data URIs         │
                            │  3. pick key (prefix-affinity + cache)   │
                            │  4. POST codebuddy.cn/v2  (stream:true)  │
                            │  5. aggregate SSE → full response        │
                            │  6. emit in client's format              │
                            └──────────────────────────────────────────┘
                                          │
                                          ▼
                            https://www.codebuddy.cn/v2/chat/completions
                            (GLM · DeepSeek · Kimi · Hunyuan · MiniMax · Hy3)
```

The key trick: CodeBuddy is stream-only, so the gateway **always** streams
upstream and then either relays the SSE (for streaming clients) or aggregates it
into a single JSON object (for non-streaming clients). Every non-stream call in
every format flows through the same aggregator.

---

## 🔬 Reverse-engineered API (verified)

Reverse-engineered from `@tencent-ai/codebuddy-code@2.118.2` via packet capture
(Node `zlib`/`https` injection) and confirmed with `curl`.

| | |
|---|---|
| **Endpoint** | `POST https://www.codebuddy.cn/v2/chat/completions` |
| **Auth** | `Authorization: Bearer ck_…` **or** `X-API-Key: ck_…` (both accepted) |
| **Body** | Standard OpenAI Chat Completions; **`stream: true` required** |
| **Response** | OpenAI SSE — `data:{chunk}\n\n` … `data:[DONE]`; `delta.{content,reasoning_content,tool_calls}`; `usage` in the final chunk |
| **Caching** | Per-key prompt cache; `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` |
| **Vision** | `image_url` with **data URIs only** (http URLs → `400`); models `glm-4.6v`, `glm-5v-turbo`, `deepseek-v4-pro`, `hy3`, `kimi-k2.6`, … |
| **Catalog** | No server-side REST catalog (`/v2/models`, `/v2/config` all `404`); bundled in the npm package's `product.cloudhosted.json` |
| **Default model** | `hy3` |

> ⚠️ Keys starting with `ck_` are issued by `www.codebuddy.cn` and are **rejected
> by `www.codebuddy.ai`** with `401 {"message":"not_found"}`. The gateway
> defaults to the `.cn` host.

---

## 📦 Project layout

```
codebuddy_gateway/                      # the universal gateway (stdlib only)
  common.py        # config, model catalog, routing headers, errors
  upstream.py      # stream_codebuddy() — forces stream:true, yields OpenAI chunks
  aggregate.py     # aggregate() — assembles a full chat.completion from SSE
  images.py        # normalize images from all 4 formats → data URIs; fetch http
  rotation.py      # KeyPool — prefix-affinity + cache-aware multi-key rotation
  server.py        # ThreadingHTTPServer, routing, SSE flush, usage tee
  __main__.py      # CLI entry: python -m codebuddy_gateway
  formats/
    openai.py      # passthrough OpenAI Chat Completions
    anthropic.py   # Anthropic Messages ⇄ OpenAI
    gemini.py      # Google Gemini ⇄ OpenAI
    codex.py       # OpenAI Responses / Codex ⇄ OpenAI
plugins/model-providers/codebuddy/      # Hermes Agent provider plugin
  __init__.py      # CodeBuddyProfile(ProviderProfile)
  plugin.yaml      # plugin manifest
models.extra.json                       # hand-maintained model supplement (see docs/)
docs/adding-models.md                   # how to add a newly released model
requirements.txt  .env.example  README.md  LICENSE
```

---

## 🚀 Quick start

```bash
# 1. Clone & enter
git clone https://github.com/neipor/codebuddy-cli2api.git
cd codebuddy-cli2api

# 2. Create the repository-root `.env` (loaded automatically at startup)
cp .env.example .env
# Edit `.env` and replace the placeholder with your CodeBuddy key.

# 3. Run — no install step, stdlib only
python3 -m codebuddy_gateway            # listens on http://127.0.0.1:8787
python3 -m codebuddy_gateway --print-models   # print the model catalog
python3 -m codebuddy_gateway --print-codex-models > ~/.codex/codebuddy-models.json  # Codex catalog

# In ~/.codex/config.toml (machine-local Codex configuration):
# model_catalog_json = "/Users/<you>/.codex/codebuddy-models.json"

# Optional: register the read-only CodeBuddy consultation MCP server in
# ~/.codex/config.toml. Native Codex keeps all filesystem and shell control.
# [mcp_servers.codebuddy_consult]
# command = "python3"
# args = ["-m", "codebuddy_gateway.consult_mcp"]
# cwd = "/Users/data/project/codebuddy-cli2api"
# [mcp_servers.codebuddy_consult.env]
# CODEBUDDY_GATEWAY_URL = "http://127.0.0.1:8787"
# CODEBUDDY_CONSULT_MODEL = "glm-5.3"

# Optional: use CodeBuddy models directly from Pi Coding Agent. The template
# keeps Pi's native read/write/edit/bash tool loop and routes only model calls
# through this gateway. Copy it after starting the gateway once:
# mkdir -p ~/.pi/agent
# cp pi-models.example.json ~/.pi/agent/models.json
# If GATEWAY_API_KEY is enabled, replace the template's "apiKey": "local"
# with that shared key (or use an environment-resolved value in models.json).
# Then inspect/select models with:
# pi --list-models
# pi --provider codebuddy-local --model glm-5.3 --thinking high

# 4. Smoke test (non-stream → the gateway aggregates for you)
curl http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"hy3","messages":[{"role":"user","content":"say hi"}]}'
```

---

## 📡 Endpoints

| Method | Path | Format |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI Chat Completions |
| POST | `/v1/messages` | Anthropic Messages |
| POST | `/v1beta/models/{m}:generateContent` | Gemini (non-stream) |
| POST | `/v1beta/models/{m}:streamGenerateContent` | Gemini (stream, SSE) |
| POST | `/v1/responses` | OpenAI Responses / Codex |
| GET | `/v1/models` · `/v1beta/models` · `/models` | model catalog |
| GET | `/health` | health check |
| GET | `/debug/keys` | rotation stats (per-key hit rates) |

**Client auth:** if `GATEWAY_API_KEY` is set, clients must send it in
`Authorization: Bearer`, `x-api-key`, `x-goog-api-key`, or `?key=`. If unset,
no client auth is required (the gateway owns the upstream credential; run it on
localhost).

### Curl examples — all four formats

```bash
GW=http://127.0.0.1:8787

# OpenAI (stream)
curl -sS $GW/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.7","stream":true,"messages":[{"role":"user","content":"hi"}]}'

# Anthropic
curl -sS $GW/v1/messages -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"glm-4.7","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'

# Gemini
curl -sS "$GW/v1beta/models/glm-4.7:generateContent" -H 'Content-Type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"hi"}]}]}'

# Codex / Responses
curl -sS $GW/v1/responses -H 'Content-Type: application/json' \
  -d '{"model":"glm-4.7","input":"hi"}'
```

Tool calls, `reasoning_content`, and multi-turn conversations all work across
every format.

### Pi Coding Agent 直连

Pi 0.84.2 可以通过 `~/.pi/agent/models.json` 添加 OpenAI-compatible provider。
仓库中的 [`pi-models.example.json`](pi-models.example.json) 已配置
`codebuddy-local`，指向 `http://127.0.0.1:8787/v1`，并包含 GLM、DeepSeek、Kimi
示例模型。Pi 继续负责 `read`、`write`、`edit`、`bash` 工具循环，网关只负责把模型
请求转发到 CodeBuddy。

CodeBuddy 上游当前实际接受的推理参数只有 `low` 和 `high`，因此模板中的 Pi 档位映射为：

| Pi thinking | CodeBuddy reasoning_effort |
|---|---|
| `minimal` / `low` | `low` |
| `medium` / `high` / `xhigh` / `max` | `high` |
| `off` | `none`（关闭网关默认注入） |

`kimi-k3-2` 示例按当前 Codex 模型目录“不提供推理档位菜单”的结论按非推理使用，并固定
发送 `reasoning_effort=none`，避免网关的默认推理注入。若你的 `/v1/models` 返回了不同的
能力，以实际目录为准更新该模型条目。第三方模型的结构化 tool call 稳定性仍由模型本身
决定，不能保证与原生 Codex 模型完全一致。

---

## 🔑 Multi-key cache-aware rotation

CodeBuddy's prompt cache is **per-account**. The gateway exploits this with
three routing rules:

1. **Prefix affinity** — `sha256(system + first user message)` sticks to one key,
   so that key's cache stays warm. (Verified: the 2nd request with the same
   prefix returned **704 cache-hit tokens** vs 0 cold.)
2. **New prefix → highest hit-rate key** — a brand-new prefix is assigned to the
   healthy key with the **highest cache-hit rate**, tie-broken by **fewest
   requests** (even distribution), then LRU.
3. **Failover before first byte** — 401 disables the key (and reassigns its
   prefixes); 429 backs off 30s; 5xx/429 are retried on another key *before* any
   bytes are sent to the client.

```
   request ──► prefix_hash = sha256(system + first user msg)
                    │
            seen before? ──yes──► sticky key (warm cache)
                    │ no
                    ▼
        pick healthy key with highest cache-hit rate
        (tiebreak: fewest requests, then LRU)
```

Configure with `CODEBUDDY_API_KEYS` (comma/newline-separated) or
`CODEBUDDY_KEYS_FILE` (one per line). Inspect live stats:

```bash
curl -sS http://127.0.0.1:8787/debug/keys
# {
#   "keys": [
#     {"key_tail":"…a1b2c3","requests":4,"cache_hit_tokens":704,
#      "cache_miss_tokens":816,"hit_rate":0.4632,"prefixes":3,
#      "state":"active","last_used":"2026-07-11T22:18:01Z"}
#   ],
#   "total_keys":1
# }
```

---

## 🖼️ Vision / images

Send images in any format's native shape — OpenAI `image_url`, Anthropic
`image`/`source`, Gemini `inline_data`/`file_data`, Codex `input_image`. The
gateway normalizes them all to OpenAI `image_url` **data URIs**, and if you pass
an `http(s)` URL it fetches and inlines it (CodeBuddy rejects remote URLs). Use a
vision-capable model: `glm-4.6v`, `glm-5v-turbo`, `deepseek-v4-pro`, `hy3`,
`kimi-k2.6`, `minimax-m2.7`, `hunyuan-2.0-instruct`, …

```bash
# Synthetic 2×2 red PNG (no image file needed)
IMG=$(python3 -c "import base64,struct,zlib; w=h=2; raw=b''.join(b'\x00'+b'\xff\x00\x00'*w for _ in range(h));
def c(t,d):
 x=t+d; return struct.pack('>I',len(d))+x+struct.pack('>I',zlib.crc32(x)&0xffffffff)
print(base64.b64encode(b'\\x89PNG\\r\\n\\x1a\\n'+c(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+c(b'IDAT',zlib.compress(raw))+c(b'IEND',b'')).decode())")

curl -sS http://127.0.0.1:8787/v1/chat/completions -H 'Content-Type: application/json' \
  -d "{\"model\":\"glm-4.6v\",\"messages\":[{\"role\":\"user\",\"content\":[
    {\"type\":\"text\",\"text\":\"what color is this?\"},
    {\"type\":\"image_url\",\"image_url\":{\"url\":\"data:image/png;base64,$IMG\"}}]}]}"
```

---

## 🤖 Hermes Agent integration

The `plugins/model-providers/codebuddy/` directory is a Hermes `ProviderProfile`
plugin — no Hermes core changes required.

```bash
# 1. Install the plugin into Hermes' user plugin dir
mkdir -p ~/.hermes/plugins/model-providers
cp -r plugins/model-providers/codebuddy ~/.hermes/plugins/model-providers/

# 2. Put the key in the repository-root `.env` (the gateway loads it; Hermes
#    passes it through as Bearer)
cp .env.example .env
# Edit `.env` and replace the placeholder with your CodeBuddy key.

# 3. Start the gateway
python3 -m codebuddy_gateway &

# 4. Configure Hermes — in ~/.hermes/config.yaml:
#    model:
#      provider: codebuddy
#      default: hy3

# 5. Verify & run
hermes doctor     # health check → gateway's /v1/models
hermes            # chat (streaming); subagents/compression use non-stream aggregation
```

`hermes model` lists CodeBuddy models straight from the gateway's catalog. The
profile's `base_url` (`http://127.0.0.1:8787/v1`) is overridable via
`model.base_url` in `config.yaml`.

---

## 🔌 Other clients

Because the gateway speaks all four formats, any client works:

| Client | Setting |
|---|---|
| **Codex CLI** | `OPENAI_BASE_URL=http://127.0.0.1:8787/v1` |
| **OpenAI SDK** | `base_url="http://127.0.0.1:8787/v1"` |
| **Anthropic SDK** | `base_url="http://127.0.0.1:8787"` |
| **Google Gen AI SDK** | `client_options={"api_endpoint":"http://127.0.0.1:8787"}` |

---

## 🧠 How it works

1. **Parse** the inbound request in the client's format (`formats/*.py`) into a
   canonical OpenAI Chat Completions body, normalizing images along the way.
2. **Pick a key** via `KeyPool.pick()` — prefix-affinity, then cache-hit-rate.
3. **Stream upstream** with `stream: true` forced on, replaying the CLI's routing
   headers (`X-Product`, `X-IDE-Type`, …) for fingerprint parity.
4. **Tee the usage** from the final chunk back into the pool (`record()`), so the
   next routing decision is cache-aware.
5. **Emit** the result in the client's format: relay SSE for streaming clients,
   or aggregate the chunks into one JSON object for non-streaming clients.

If the upstream errors before any bytes are sent, `_connect()` retries on another
key — the client never sees the failure.

---

## ⚙️ Configuration

All config is via environment variables (see `.env.example`). When started
from the repository root, the gateway automatically loads `.env` and only uses
its values for variables that are not already present in the process
environment; explicit exports therefore take precedence.

| Variable | Default | Description |
|---|---|---|
| `CODEBUDDY_API_KEY` | — | A single CodeBuddy key (`ck_…`). Required. |
| `CODEBUDDY_API_KEYS` | — | Multiple keys, comma- or newline-separated (enables rotation). |
| `CODEBUDDY_KEYS_FILE` | — | Path to a file with one key per line (`#` comments allowed). |
| `CODEBUDDY_BASE_URL` | `https://www.codebuddy.cn` | CodeBuddy backend. `ck_` keys need the `.cn` host. |
| `CODEBUDDY_API_PATH` | `/v2/chat/completions` | Upstream chat path. |
| `GATEWAY_HOST` | `127.0.0.1` | Gateway listen address. |
| `GATEWAY_PORT` | `8787` | Gateway listen port. |
| `GATEWAY_API_KEY` | — | Optional shared secret clients must present. |
| `CODEBUDDY_DEFAULT_MODEL` | `hy3` | Model used when a request omits one. |
| `CODEBUDDY_MODEL_ALIASES` | built-in Codex aliases | Comma-separated `display-name=model-id` overrides. |
| `CODEBUDDY_CODEX_INSTRUCTIONS_MODE` | `compact` | Upstream Codex system prompt mode: `compact` avoids content filtering, `full` preserves the raw prompt for diagnostics, `drop` omits it. |
| `CODEBUDDY_REASONING_EFFORT` | `high` | Default CodeBuddy reasoning effort when a client omits one: `low`, `high`, or `none`/`off`/`minimal` to disable. Invalid values fail with HTTP 400. |
| `CODEBUDDY_MODEL_CATALOG_JSON` | — | Optional gateway-side catalog override (JSON or file path). Codex does not infer capabilities from `/v1/models`; generate a file with `--print-codex-models` and set Codex's `model_catalog_json` in `~/.codex/config.toml`. |
| `CODEBUDDY_MODELS_EXTRA_FILE` | `./models.extra.json` | Hand-maintained model supplement. Models routable upstream but missing from the CLI catalog go here; editing the file invalidates the catalog cache immediately, so no restart is needed. Set to an empty value to disable the layer. See [docs/adding-models.md](docs/adding-models.md). |
| `CODEBUDDY_MODELS_URL` | — | Explicit remote model catalog JSON. Highest precedence, above the CLI's own catalog. |
| `CODEBUDDY_TIMEOUT` | `180` | Upstream request timeout (seconds). |
| `IMAGE_FETCH_MAX_BYTES` | `20971520` | Cap when inlining a remote image. |
| `GATEWAY_LOG_LEVEL` | `INFO` | Logging level. |
| `CODEBUDDY_RESOURCE_BASE_URL` | `https://copilot.tencent.com` | Upstream for OAuth resource endpoints (see below). |
| `CODEBUDDY_AUTH_FILE` | auto | WorkBuddy/CodeBuddy session file for OAuth resources. |
| `CODEBUDDY_REMOTE` | — | `1` binds `0.0.0.0`; requires `GATEWAY_API_KEY`. |
| `CODEBUDDY_AUTO_CHECKIN` | `1` | Auto-claim the daily check-in credit once per UTC day. |

---

## 🎫 WorkBuddy 资源端点（OAuth）与远程部署

除 chat 外，网关还透传 WorkBuddy/CodeBuddy 的免费/付费资源端点。这些端点强制
`Bearer <OAuth accessToken>`（`ck_` key 无效），网关自己持有 OAuth session（默认读
桌面 WorkBuddy 的 `workbuddy-desktop.info` 或 CLI 的 `~/.codebuddy/auth/*.info`，自动
刷新），客户端只需 `GATEWAY_API_KEY`，永远接触不到 token。chat 路径不变，仍走
`ck_` key（无有效 key 时自动回落到 OAuth session）。

| 网关路径 | 上游 | 说明 |
|---|---|---|
| `POST /v1/checkin` | `/v2/billing/meter/daily-checkin` | 每日签到（空 body） |
| `POST /v1/checkin-status` | `/billing/meter/checkin-status` | 当日签到状态 |
| `POST /v1/checkin-activity` | `/v2/billing/meter/checkin-activity-status` | 活动状态 |
| `POST /v1/asr` | `/agenttool/v1/asr` | 录音转文字，multipart（字段 `audio`+`fileName`） |
| `POST /v1/tts` | `/agenttool/v1/tts` | 语音合成，JSON `{text}`，SSE 流式返回 |
| `POST /v1/tempkey` | `/agenttool/v1/tempkey` | 临时云凭证 |
| `POST /v1/search` / `/v1/web_search` | `/agenttool/v1/search` | 联网搜索 |
| `POST /v1/webfetch` | `/agenttool/v1/webfetch` | 抓取网页为 markdown |
| `POST /v1/images/generations` | `/v2/images/generations` | 文生图（`/v1` 别名把 `data.data[]` 归一为 `data[]`） |
| `POST /v1/images/edits` | `/v2/images/edits` | 图生图 |
| `POST /v1/videos/generations` | `/v2/videos/generations` | 文生视频（返回 task id） |
| `POST /v1/videos/tasks` | `/v2/videos/tasks` | 轮询视频任务 |

上游原始路径（`/agenttool/v1/...`、`/v2/images/...` 等）也直接可用，行为一致。

**域名勘误**：`www.codebuddy.ai` 会对 OAuth token 直接 401（边缘网关拒绝）；实测
`https://copilot.tencent.com`（桌面 WorkBuddy 默认端点）与 `www.codebuddy.cn` 全端点
可用，默认取前者，用 `CODEBUDDY_RESOURCE_BASE_URL` 覆盖。

**自动签到**：网关启动后内置守护线程，每个 UTC 日自动领一次 `/v1/checkin`（每日
100 积分），不依赖任何客户端（OMP 不开、端口不暴露也照常执行），失败每小时重试，
`CODEBUDDY_AUTO_CHECKIN=0` 关闭。

**远程部署（令牌跨机器，不暴露端口）**：默认网关只监听 `127.0.0.1`，其它机器用
SSH 隧道访问（不用开 `CODEBUDDY_REMOTE`）：

```bash
# 持有 session 的机器（常开/云主机）
GATEWAY_API_KEY=<随机串> python3 -m codebuddy_gateway
# 其它机器：一条隧道，之后所有客户端都指 127.0.0.1:8787
ssh -N -L 8787:127.0.0.1:8787 user@gateway-host
curl http://127.0.0.1:8787/health -H "Authorization: Bearer <随机串>"
```

可选：确实需要直连时（如 Tailscale 网段内）再设 `CODEBUDDY_REMOTE=1` 绑定
`0.0.0.0`，此时未设 `GATEWAY_API_KEY` 会拒绝启动。首次部署无 session 文件时运行
`python3 -m codebuddy_gateway --login` 走浏览器外链登录。

**OMP / oh-my-pi 集成**：`~/.omp/agent/extensions/workbuddy-tools.ts` 注册
`workbuddy` provider（`--model workbuddy/hy3`）与 `codebuddy_checkin`/`codebuddy_asr`/
`codebuddy_tts` 工具，并在每次 session 启动时自动签到一次。

---

## 🛠️ Troubleshooting

- **`401 {"message":"not_found"}`** — you're hitting `www.codebuddy.ai`; `ck_`
  keys are for `www.codebuddy.cn`. Leave `CODEBUDDY_BASE_URL` at its default.
- **`400 Non-stream chat request is currently not supported`** — only happens if
  you bypass the gateway and hit CodeBuddy directly with `stream: false`. Use the
  gateway (it forces streaming and aggregates).
- **`400 invalid parameter value` (images)** — you passed an `http` image URL
  directly to CodeBuddy. The gateway fetches & inlines it; direct mode doesn't.
- **Hermes subagent / compression fails** — you're in direct mode. Use the
  gateway; it aggregates non-stream calls transparently.
- **`no CodeBuddy API keys configured`** — set `CODEBUDDY_API_KEY` (or
  `CODEBUDDY_API_KEYS` / `CODEBUDDY_KEYS_FILE`) before starting the gateway.
- **A model the upstream supports is missing from `/v1/models`** — the CLI's
  cached catalog lags behind the service. Confirm the slug is routable, then add
  it to `models.extra.json` and save; no restart needed. Full procedure:
  [docs/adding-models.md](docs/adding-models.md).
- **Empty `content` from a reasoning model** — `max_tokens` was too small; the
  model spent the whole budget thinking and the answer is in
  `reasoning_content`. Raise `max_tokens` (≥ 2000).

---

## 📄 License

MIT — see [LICENSE](LICENSE).

CodeBuddy is a product of Tencent. This project is an independent interoperability
adapter and is not affiliated with or endorsed by Tencent. Use your own API
credentials in accordance with CodeBuddy's terms.
