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
requirements.txt  .env.example  README.md  LICENSE
```

---

## 🚀 Quick start

```bash
# 1. Clone & enter
git clone https://github.com/neipor/codebuddy-cli2api.git
cd codebuddy-cli2api

# 2. Set your CodeBuddy key (from the CodeBuddy CLI login)
export CODEBUDDY_API_KEY=ck_yourkeyid.yoursecret

# 3. Run — no install step, stdlib only
python3 -m codebuddy_gateway            # listens on http://127.0.0.1:8787
python3 -m codebuddy_gateway --print-models   # print the model catalog

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

# 2. Set the key (the gateway reads it; Hermes passes it through as Bearer)
echo 'CODEBUDDY_API_KEY=ck_yourkeyid.yoursecret' >> ~/.hermes/.env

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

All config is via environment variables (see `.env.example`).

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
| `CODEBUDDY_TIMEOUT` | `180` | Upstream request timeout (seconds). |
| `IMAGE_FETCH_MAX_BYTES` | `20971520` | Cap when inlining a remote image. |
| `GATEWAY_LOG_LEVEL` | `INFO` | Logging level. |

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

---

## 📄 License

MIT — see [LICENSE](LICENSE).

CodeBuddy is a product of Tencent. This project is an independent interoperability
adapter and is not affiliated with or endorsed by Tencent. Use your own API
credentials in accordance with CodeBuddy's terms.
