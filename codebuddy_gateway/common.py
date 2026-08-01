"""Shared config, model catalog, and constants for the CodeBuddy gateway.

Pure stdlib. No external dependencies.
"""

from __future__ import annotations

import base64
import glob
import gzip
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Any

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = os.environ.get("CODEBUDDY_BASE_URL", "https://www.codebuddy.cn")
DEFAULT_CHAT_PATH = os.environ.get("CODEBUDDY_API_PATH", "/v2/chat/completions")

# Resource (OAuth) upstream: search/speech/images/videos/billing. The desktop
# WorkBuddy app talks to copilot.tencent.com, and the token is accepted there
# (and on www.codebuddy.cn); www.codebuddy.ai rejects this token with an
# edge-level 401, so it is NOT the default. Chat (ck_) stays on
# CODEBUDDY_BASE_URL (.cn).
CODEBUDDY_RESOURCE_BASE_URL = os.environ.get(
    "CODEBUDDY_RESOURCE_BASE_URL", "https://copilot.tencent.com"
)

# CODEBUDDY_REMOTE=1 -> default to binding all interfaces so other machines
# (Tailscale/SSH) can reach the gateway; requires GATEWAY_API_KEY (enforced
# in server.serve). An explicit GATEWAY_HOST still wins.
CODEBUDDY_REMOTE = os.environ.get("CODEBUDDY_REMOTE", "").strip().lower() in (
    "1", "true", "yes", "on")
DEFAULT_HOST = os.environ.get("GATEWAY_HOST") or (
    "0.0.0.0" if CODEBUDDY_REMOTE else "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("GATEWAY_PORT", "8787"))

# Optional shared secret clients must present. If unset, no client auth
# (gateway trusts localhost / owns the upstream credential).
GATEWAY_API_KEY = os.environ.get("GATEWAY_API_KEY", "").strip()

# Upstream request timeout (seconds) for the streaming POST.
UPSTREAM_TIMEOUT = float(os.environ.get("CODEBUDDY_TIMEOUT", "180"))

# Max bytes to download when inlining a remote http(s) image as base64.
IMAGE_FETCH_MAX_BYTES = int(os.environ.get("IMAGE_FETCH_MAX_BYTES", str(20 * 1024 * 1024)))

# Default model for the gateway / hermes plugin.
DEFAULT_MODEL = os.environ.get("CODEBUDDY_DEFAULT_MODEL", "hy3")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("codebuddy.gateway")


# ── Model catalog (auto-discovered + cached) ───────────────────────────────
# CodeBuddy's servers expose NO REST model catalog (/v2/models, /v2/config,
# /v2/*models* all 404). The authoritative chat-model list is the product
# config the CLI mirrors into ~/.codebuddy/local_storage at login / on update
# (a JSON-string-wrapped gzip+base64 blob whose top-level ``models`` array is
# the live catalog). The CLI's bundled ``product.json`` is only a *generic
# default* (it lists gpt-5.5 / gemini etc., NOT the account's real models), so
# it must be a fallback, never the primary source. To avoid hand-editing this
# file every time Tencent ships a model, the gateway resolves the live catalog
# at runtime, in this priority:
#   1. CODEBUDDY_MODELS_URL   explicit remote JSON list (if set) - future-proof
#   2. ~/.codebuddy/local_storage entry_*.info (account-specific, live config)
#   3. the CLI's product.json (generic default - last-resort auto source)
#   4. the bundled MODELS constant below (hardcoded fallback)
# Result is cached in-memory for CODEBUDDY_MODELS_TTL seconds (default 1h), so
# `npm update @tencent-ai/codebuddy-code` (or the CLI auto-updating) makes new
# models appear in Hermes within one TTL - no gateway code edit required.
MODELS: list[dict[str, Any]] = [
    {"id": "hy3-preview-agent", "name": "Hy3 preview",            "img": True,  "max_out": 64000},
    {"id": "glm-5.1",           "name": "GLM-5.1",                 "img": False, "max_out": 48000},
    {"id": "glm-5.0",           "name": "GLM-5.0",                 "img": False, "max_out": 48000},
    {"id": "glm-5.0-turbo",     "name": "GLM-5.0-Turbo",           "img": False, "max_out": 48000},
    {"id": "glm-5v-turbo",      "name": "GLM-5v-Turbo",            "img": True,  "max_out": 38000},
    {"id": "glm-4.7",           "name": "GLM-4.7",                 "img": False, "max_out": 48000},
    {"id": "glm-4.6",           "name": "GLM-4.6",                 "img": False, "max_out": 32000},
    {"id": "glm-4.6v",          "name": "GLM-4.6V",                "img": True,  "max_out": 32000},
    {"id": "deepseek-v4-pro",   "name": "Deepseek-V4-Pro",         "img": True,  "max_out": 50000},
    {"id": "deepseek-v4-flash", "name": "Deepseek-V4-Flash",       "img": True,  "max_out": 50000},
    {"id": "deepseek-v3-2-volc","name": "DeepSeek-V3.2",           "img": False, "max_out": 32000},
    {"id": "deepseek-v3.1",     "name": "DeepSeek-V3.1-Terminus",  "img": False, "max_out": 8192},
    {"id": "kimi-k2.6",         "name": "Kimi-K2.6",               "img": True,  "max_out": 32000},
    {"id": "kimi-k2.5",         "name": "Kimi-K2.5",               "img": True,  "max_out": 32000},
    {"id": "minimax-m2.5",      "name": "MiniMax-M2.5",            "img": False, "max_out": 48000},
    {"id": "minimax-m2.7",      "name": "MiniMax-M2.7",            "img": True,  "max_out": 48000},
    {"id": "hunyuan-2.0-instruct","name": "Hunyuan-2.0-Instruct",  "img": True,  "max_out": 16000},
    {"id": "hunyuan-chat",      "name": "Hunyuan-Turbos",          "img": False, "max_out": 8192},
    {"id": "auto",              "name": "Auto",                    "img": True,  "max_out": 32000},
    {"id": "default",           "name": "Default",                 "img": False, "max_out": 24000},
]

MODEL_IDS = {m["id"] for m in MODELS}
VISION_MODELS = {m["id"] for m in MODELS if m["img"]}


def _norm_model(m: dict[str, Any]) -> dict[str, Any] | None:
    mid = m.get("id")
    if not mid:
        return None
    return {
        "id": mid,
        "name": m.get("name") or mid,
        "img": bool(m.get("supportsImages") or m.get("img")),
        "max_out": int(m.get("maxOutputTokens") or m.get("maxOut") or 0) or 32000,
    }


def _cli_product_models() -> list[dict[str, Any]] | None:
    """Read the CLI's bundled product.json - the same catalog the CLI uses."""
    cli = os.environ.get("CODEBUDDY_CLI_DIR")
    if not cli:
        for cand in (
            os.path.expanduser("~/.local/lib/node_modules/@tencent-ai/codebuddy-code"),
            "/usr/local/lib/node_modules/@tencent-ai/codebuddy-code",
        ):
            if os.path.isdir(cand):
                cli = cand
                break
    if not cli:
        return None
    pj = os.path.join(cli, "product.json")
    if not os.path.exists(pj):
        return None
    try:
        d = json.load(open(pj, encoding="utf-8"))
    except Exception:
        return None
    raw = d.get("models") if isinstance(d, dict) else None
    if not isinstance(raw, list):
        return None
    out = [x for x in (_norm_model(x) for x in raw) if x]
    return out or None


def _local_storage_models() -> list[dict[str, Any]] | None:
    """Authoritative source: the CLI's cached, account-specific product config
    in ~/.codebuddy/local_storage (mirrored from the server at login / on
    update). Each ``entry_*.info`` is a JSON-string-wrapped gzip+base64 blob
    whose unwrapped payload is a full product config; its top-level ``models``
    array is the live chat-model catalog. Some entries are unrelated (the
    literal string "internal", the copilot endpoint URL, an agents cache) - we
    skip those. When several product configs exist we keep the most recently
    modified one, because that is the active account's config (the CLI writes a
    fresh copy at every login), which wins over any stale generic default.
    """
    ls = os.path.expanduser("~/.codebuddy/local_storage")
    if not os.path.isdir(ls):
        return None
    candidates: list[tuple[float, list[dict[str, Any]]]] = []
    for f in glob.glob(os.path.join(ls, "entry_*.info")):
        try:
            raw = open(f, encoding="utf-8").read()
            # Most entries are JSON-string-wrapped. Unwrap the surrounding
            # quotes first; only gzip+base64 blobs decode further.
            if raw.startswith('"'):
                raw = json.loads(raw)
                try:
                    raw = gzip.decompress(base64.b64decode(raw)).decode("utf-8")
                except Exception:
                    continue  # e.g. "internal" / "https://copilot.tencent.com"
            d = json.loads(raw)
        except Exception:
            continue
        # Extract the models list from the various shapes we may encounter.
        arr = None
        if isinstance(d, dict):
            arr = d.get("models")
            if not isinstance(arr, list):
                sub = d.get("data")
                if isinstance(sub, dict) and isinstance(sub.get("models"), list):
                    arr = sub["models"]
        elif isinstance(d, list):
            # Either the list IS the models, or a list of objects each carrying
            # {"data": {"models": [...]}}.
            if d and isinstance(d[0], dict) and "id" in d[0]:
                arr = d
            elif (d and isinstance(d[0], dict)
                  and isinstance(d[0].get("data"), dict)
                  and isinstance(d[0]["data"].get("models"), list)):
                arr = d[0]["data"]["models"]
        if not isinstance(arr, list):
            continue
        parsed = [x for x in (_norm_model(x) for x in arr if isinstance(x, dict)) if x]
        if parsed:
            candidates.append((os.path.getmtime(f), parsed))
    if not candidates:
        return None
    # Most recently modified valid config wins.
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


_CATALOG: dict[str, Any] = {"data": None, "ts": 0.0}
_CATALOG_TTL = float(os.environ.get("CODEBUDDY_MODELS_TTL", "3600"))


def load_model_catalog() -> list[dict[str, Any]]:
    """Resolve + cache the active chat-model catalog."""
    now = time.time()
    if _CATALOG["data"] is not None and now - _CATALOG["ts"] < _CATALOG_TTL:
        return _CATALOG["data"]
    cat: list[dict[str, Any]] | None = None
    url = os.environ.get("CODEBUDDY_MODELS_URL")
    if url:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
            arr = j if isinstance(j, list) else (j.get("models") if isinstance(j, dict) else None)
            if isinstance(arr, list):
                parsed = [x for x in (_norm_model(x) for x in arr if isinstance(x, dict)) if x]
                if parsed:
                    cat = parsed
        except Exception as e:
            log.warning("model catalog fetch failed (%s): %s", url, e)
    if not cat:
        cat = _local_storage_models()   # account-specific, live catalog
    if not cat:
        cat = _cli_product_models()     # generic CLI default (fallback)
    if not cat:
        cat = MODELS                    # hardcoded last resort
    _CATALOG["data"] = cat
    _CATALOG["ts"] = now
    log.info("model catalog resolved: %d models", len(cat))
    return cat


def model_supports_vision(model: str | None) -> bool:
    if not model:
        return False
    return model in {m["id"] for m in load_model_catalog() if m.get("img")}


def models_json() -> dict[str, Any]:
    """OpenAI-compatible /v1/models response (live catalog)."""
    return {
        "object": "list",
        "data": [
            {"id": m["id"], "object": "model", "created": 0, "owned_by": "codebuddy"}
            for m in load_model_catalog()
        ],
    }


# ── Upstream routing headers (match CLI fingerprint) ────────────────────────
def upstream_headers(credential: str, *, mode: str = "key",
                     accept: str = "text/event-stream") -> dict[str, str]:
    """Headers sent to CodeBuddy.

    ``mode="key"``  (legacy ``ck_`` API key): sends both ``Authorization: Bearer``
    and ``X-API-Key`` (CLI parity).
    ``mode="oauth"``: sends only ``Authorization: Bearer <token>`` (no X-API-Key),
    used when the gateway drives the chat path with an OAuth session.
    """
    h: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": accept,
        "X-Requested-With": "XMLHttpRequest",
        "x-codebuddy-request": "1",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "X-IDE-Name": "CLI",
        "X-IDE-Version": "2.118.2",
        "X-Agent-Intent": "craft",
        "X-Agent-Purpose": "conversation",
        "User-Agent": "CLI/2.118.2 CodeBuddy/2.118.2 codebuddy-gateway/1.0",
    }
    h["Authorization"] = f"Bearer {credential}"
    if mode == "key":
        h["X-API-Key"] = credential
    return h


# ── Errors ──────────────────────────────────────────────────────────────────
class GatewayError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


class UpstreamError(GatewayError):
    """Non-2xx from CodeBuddy."""


# ── Small helpers ───────────────────────────────────────────────────────────
def load_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        raise GatewayError(400, f"invalid JSON body: {e}")
