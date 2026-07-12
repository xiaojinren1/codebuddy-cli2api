"""Shared config, model catalog, and constants for the CodeBuddy gateway.

Pure stdlib. No external dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_BASE_URL = os.environ.get("CODEBUDDY_BASE_URL", "https://www.codebuddy.cn")
DEFAULT_CHAT_PATH = os.environ.get("CODEBUDDY_API_PATH", "/v2/chat/completions")
DEFAULT_HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
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


# ── Model catalog ───────────────────────────────────────────────────────────
# Source: product.cloudhosted.json "models" array bundled in the
# @tencent-ai/codebuddy-code npm package. There is NO REST catalog on the
# server (/v2/models, /v2/config, /v2/*models* all return 404). This list is
# the authoritative set of chat models; image-gen models (hunyuan-image-v3.0)
# are excluded. `img` = accepts image input (vision).
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


def model_supports_vision(model: str | None) -> bool:
    if not model:
        return False
    return model in VISION_MODELS


def models_json() -> dict[str, Any]:
    """OpenAI-compatible /v1/models response."""
    return {
        "object": "list",
        "data": [
            {"id": m["id"], "object": "model", "created": 0, "owned_by": "codebuddy"}
            for m in MODELS
        ],
    }


# ── Upstream routing headers (match CLI fingerprint) ────────────────────────
def upstream_headers(api_key: str, *, accept: str = "text/event-stream") -> dict[str, str]:
    """Headers sent to CodeBuddy. Auth via both Bearer and X-API-Key (CLI parity)."""
    return {
        "Authorization": f"Bearer {api_key}",
        "X-API-Key": api_key,
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
