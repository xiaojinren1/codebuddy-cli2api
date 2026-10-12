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


def _model_aliases() -> dict[str, str]:
    """Resolve Codex display-name aliases to CodeBuddy model ids.

    CC Switch normally rewrites these names, but requests can still arrive
    with the display name when the selected model changes inside Codex.
    """
    aliases = {
        "gpt-5.6-sol": "glm-5.3",
        "gpt-5.6-luna": "kimi-k3-2",
        "gpt-5.6-terra": "deepseek-v4-flash",
    }
    raw = os.environ.get("CODEBUDDY_MODEL_ALIASES", "").strip()
    if raw:
        for pair in raw.split(","):
            key, sep, value = pair.partition("=")
            if sep and key.strip() and value.strip():
                aliases[key.strip()] = value.strip()
    return aliases


MODEL_ALIASES = _model_aliases()


def resolve_model(model: str | None) -> str | None:
    return MODEL_ALIASES.get(model, model) if model else model

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
    {"id": "glm-5.3",           "name": "GLM-5.3",                 "img": False, "max_out": 48000, "reasoning": {"effort": "max"}},
    {"id": "glm-5.3-flash",     "name": "GLM-5.3-Flash",           "img": True,  "max_out": 48000, "reasoning": {"effort": "max"}},
    {"id": "glm-5.2",           "name": "GLM-5.2",                 "img": False, "max_out": 48000, "reasoning": {"effort": "high"}},
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
    {"id": "kimi-k3-2",         "name": "Kimi-K3-2",               "img": False, "max_out": 48000, "reasoning": True},
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
    if not isinstance(m, dict):
        return None
    mid = m.get("id") or m.get("slug") or m.get("model")
    if not mid:
        return None
    def first(*keys: str, default: Any = None) -> Any:
        for key in keys:
            value = m.get(key)
            if value is not None and value != "":
                return value
        return default

    name = first("name", "display_name", "displayName", "title", default=mid)
    description = first("description", "description_en", "descriptionEn", default="")
    description_zh = first("description_zh", "descriptionZh", "zh_description", default="")
    max_in = first("max_input_tokens", "maxInputTokens", "max_input", "maxInput", default=0)
    max_out = first("max_output_tokens", "maxOutputTokens", "max_out", "maxOut", default=32000)
    context = first("context_window", "contextWindow", "max_context_window", "maxContextWindow", default=0)
    try:
        max_in, max_out, context = int(max_in or 0), int(max_out or 0), int(context or 0)
    except (TypeError, ValueError):
        max_in, max_out, context = 0, 32000, 0

    vision = first("supports_vision", "supportsVision", "vision", "img", default=False)
    tool_call = first("supports_tool_call", "supportsToolCall", "tool_call", "toolCall", "tools", default=None)
    reasoning = first("reasoning", "supports_reasoning", "supportsReasoning", default=None)
    only_reasoning = first("onlyReasoning", "only_reasoning", default=None)
    out: dict[str, Any] = {
        "id": mid,
        "name": name,
        "img": bool(first("supportsImages", default=False) or vision),
        "max_out": max_out or 32000,
        "max_output_tokens": max_out or 32000,
    }
    if description:
        out["description"] = str(description)
    if description_zh:
        out["description_zh"] = str(description_zh)
    if max_in:
        out["max_in"] = max_in
        out["max_input_tokens"] = max_in
    out["supports_vision"] = out["img"]
    if context:
        out["context_window"] = context
        try:
            out["max_context_window"] = int(first("max_context_window", "maxContextWindow", default=context) or context)
        except (TypeError, ValueError):
            out["max_context_window"] = context
    if reasoning is not None:
        out["reasoning"] = reasoning
    if only_reasoning is not None:
        out["onlyReasoning"] = bool(only_reasoning)
    if tool_call is not None:
        out["tool_call"] = tool_call
        out["supports_tool_call"] = bool(tool_call)
    for key in ("credits", "vendor"):
        value = m.get(key)
        if value is not None:
            out[key] = value
    # Preserve explicit reasoning metadata used by CodeBuddy's product config.
    if isinstance(reasoning, dict):
        levels = reasoning.get("supported_reasoning_levels") or reasoning.get("supported_levels")
        effort = reasoning.get("effort") or reasoning.get("default_reasoning_level")
        if levels is not None:
            out["supported_reasoning_levels"] = list(levels) if isinstance(levels, (list, tuple)) else [levels]
        if effort:
            out["default_reasoning_level"] = effort
    # Also accept Codex catalog-shaped input when CODEBUDDY_MODEL_CATALOG_JSON
    # is used as an override.
    top_levels = m.get("supported_reasoning_levels") or m.get("supported_reasoning_efforts")
    if top_levels is not None:
        out["supported_reasoning_levels"] = list(top_levels) if isinstance(top_levels, (list, tuple)) else [top_levels]
    top_default = m.get("default_reasoning_level") or m.get("default_reasoning_effort")
    if top_default:
        out["default_reasoning_level"] = str(top_default).lower()
    return out


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
            with open(f, encoding="utf-8") as fh:
                raw = fh.read()
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


_CATALOG_EXTRAS: list[dict[str, Any]] = [
    # Live CLI catalog lags behind; keep flash visible even when local_storage
    # has not picked it up yet. Skip if the live list already has the id.
    {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "img": True, "max_out": 48000,
     "reasoning": {"effort": "max"}},
]


def _merge_catalog_extras(cat: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {m.get("id") for m in cat}
    extra = [dict(m) for m in _CATALOG_EXTRAS if m["id"] not in ids]
    return cat + extra if extra else cat



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
    cat = _merge_catalog_extras(cat or [])
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
    data = []
    for m in load_model_catalog():
        capabilities = {k: v for k, v in m.items() if k not in {"id", "name"}}
        data.append({
            "id": m["id"], "object": "model", "created": 0, "owned_by": m.get("vendor", "codebuddy"),
            "name": m.get("name", m["id"]), "description": m.get("description", ""),
            "capabilities": capabilities,
        })
    return {
        "object": "list",
        "data": data,
    }


_CODEX_REASONING_LEVELS = {
    # Codex picker labels. Wire values are mapped in formats/codex.py
    # (xhigh → max). Defaults match the deepest useful CoT for each model.
    "deepseek-v4-pro": (["low", "medium", "high", "xhigh"], "xhigh"),
    "deepseek-v4-flash": (["low", "medium", "high", "xhigh"], "xhigh"),
    "glm-5.3": (["low", "medium", "high", "xhigh"], "xhigh"),
    "glm-5.3-flash": (["low", "medium", "high", "xhigh"], "xhigh"),
    "glm-5.2": (["medium", "high", "xhigh"], "high"),
}
_CODEX_NO_REASONING_MENU = {"kimi-k3-2", "glm-5v-turbo"}


def _reasoning_levels(m: dict[str, Any]) -> tuple[list[str], str | None]:
    slug = str(m.get("_codex_source_id") or m.get("id", ""))
    if slug in _CODEX_NO_REASONING_MENU:
        return [], None
    if slug in _CODEX_REASONING_LEVELS:
        return _CODEX_REASONING_LEVELS[slug]
    raw = m.get("supported_reasoning_levels")
    if isinstance(raw, (list, tuple)) and raw:
        levels = []
        for item in raw:
            token = item.get("effort") if isinstance(item, dict) else item
            token = str(token).strip().lower().replace("-", "")
            if token in {"low", "medium", "high", "xhigh", "max", "ultra"}:
                levels.append(token)
        if levels:
            default = str(m.get("default_reasoning_level") or levels[-1]).lower()
            return levels, default if default in levels else levels[-1]
    reasoning = m.get("reasoning")
    if not reasoning:
        return [], None
    default = m.get("default_reasoning_level")
    if isinstance(reasoning, dict):
        default = reasoning.get("effort") or reasoning.get("default_reasoning_level") or default
    if not default:
        return [], None
    default = str(default or "high").lower()
    if default not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        default = "high"
    return [default], default


def model_catalog_json() -> dict[str, Any]:
    """Codex ``codex debug models`` compatible catalog with explicit abilities."""
    override = os.environ.get("CODEBUDDY_MODEL_CATALOG_JSON") or os.environ.get("CODEBUDDY_CODEX_MODEL_CATALOG_JSON")
    catalog = load_model_catalog()
    if override:
        try:
            if override.lstrip().startswith(("{", "[")):
                raw = json.loads(override)
            else:
                with open(os.path.expanduser(override), encoding="utf-8") as fh:
                    raw = json.load(fh)
            items = raw.get("models") if isinstance(raw, dict) else raw
            if isinstance(items, list):
                parsed = [x for x in (_norm_model(x) for x in items if isinstance(x, dict)) if x]
                if parsed:
                    catalog = parsed
        except (OSError, ValueError, TypeError) as exc:
            log.warning("invalid model catalog override: %s", exc)
    models = []
    catalog_by_id = {m["id"]: m for m in catalog}
    # Expose the Codex-facing aliases as first-class picker entries while
    # retaining the underlying CodeBuddy IDs for direct selection.
    aliases = {
        "gpt-5.6-sol": ("glm-5.3", "GPT-5.6-Sol"),
        "gpt-5.6-luna": ("kimi-k3-2", "GPT-5.6-Luna"),
        "gpt-5.6-terra": ("deepseek-v4-flash", "GPT-5.6-Terra"),
    }
    for alias, target in MODEL_ALIASES.items():
        existing = aliases.get(alias)
        if existing is None or existing[0] != target:
            aliases[alias] = (target, alias)
    catalog_items = list(catalog)
    for alias, (target, display) in aliases.items():
        source = catalog_by_id.get(target)
        if source and alias not in catalog_by_id:
            copy = dict(source)
            copy["id"] = alias
            copy["name"] = display
            copy["description"] = display
            copy["_codex_source_id"] = target
            catalog_items.insert(0, copy)
    for m in catalog_items:
        levels, default = _reasoning_levels(m)
        try:
            context = int(m.get("context_window") or m.get("max_context_window") or m.get("max_in") or 128000)
            max_context = int(m.get("max_context_window") or context)
            priority = int(m.get("priority", 0) or 0)
        except (TypeError, ValueError):
            context, max_context, priority = 0, 0, 0
        level_objects = [{"effort": level, "description": {
            "low": "Fast responses with lighter reasoning",
            "medium": "Balances speed and reasoning depth for everyday tasks",
            "high": "Greater reasoning depth for complex problems",
            "xhigh": "Extra high reasoning depth for complex problems",
        }.get(level, level)} for level in levels]
        models.append({
            "slug": m["id"], "display_name": m.get("name", m["id"]),
            "description": m.get("description", ""), "default_reasoning_level": default,
            "supported_reasoning_levels": level_objects, "shell_type": "shell_command",
            "visibility": "list", "supported_in_api": True, "priority": priority,
            "context_window": context, "max_context_window": max_context,
            "effective_context_window_percent": 95, "input_modalities": ["text", "image"] if m.get("img") else ["text"],
            "default_reasoning_summary": "auto" if levels else "none",
            "base_instructions": "You are Codex, a coding assistant. Complete the user's task in the current turn.",
            "supports_reasoning_summaries": bool(levels), "supports_parallel_tool_calls": bool(m.get("tool_call", True)),
            "supports_search_tool": False, "support_verbosity": False,
            "experimental_supported_tools": [], "service_tiers": [], "additional_speed_tiers": [],
            "truncation_policy": {"mode": "bytes", "limit": 10000}, "upgrade": None,
        })
    return {"models": models}


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



# ── Upstream error introspection ────────────────────────────────────────────
# CodeBuddy nests its reason differently per failure class:
#   blocklist  {"code":11128,"msg":...}
#   quota      {"error":{"data":{"code":14014,"msg":"企业总额度已全部用尽…"}}}
# so pull the message from any of the shapes instead of showing "upstream error".
_QUOTA_CODES = {14014}
_QUOTA_MARKERS = ("额度已全部用尽", "额度已用尽", "quota", "购买加量包")


def _err_nodes(body: Any) -> list[dict[str, Any]]:
    """Every dict worth inspecting in an upstream error body (outer first)."""
    nodes: list[dict[str, Any]] = []
    stack = [body]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            nodes.append(node)
            stack.extend(v for k, v in node.items() if k in ("error", "data") and isinstance(v, dict))
    return nodes


def upstream_message(body: Any, default: str = "upstream error") -> str:
    """Human-readable reason from an upstream error body, at any nesting depth."""
    for node in _err_nodes(body):
        for key in ("msg", "message", "displayMsg"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):  # displayMsg: {"en": ..., "zh": ...}
                for lang in ("zh", "zh-CN", "en"):
                    text = value.get(lang)
                    if isinstance(text, str) and text.strip():
                        return text.strip()
    if isinstance(body, str) and body.strip():
        return body.strip()[:200]
    return default


def is_quota_exhausted(body: Any) -> bool:
    """True when the account/enterprise credit pool is empty (retrying is futile)."""
    for node in _err_nodes(body):
        if node.get("code") in _QUOTA_CODES:
            return True
    text = upstream_message(body, default="")
    return any(marker in text for marker in _QUOTA_MARKERS)


# ── Small helpers ───────────────────────────────────────────────────────────
def load_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        raise GatewayError(400, f"invalid JSON body: {e}")
