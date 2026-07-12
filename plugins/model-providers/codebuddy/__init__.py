"""Tencent CodeBuddy provider profile for Hermes Agent.

Routes Hermes through the local ``codebuddy_gateway`` adapter, which exposes
CodeBuddy's stream-only CLI inference API as a fully OpenAI-compatible
endpoint (aggregating non-stream calls that CodeBuddy would otherwise reject).

The gateway also speaks Anthropic / Gemini / Codex(Responses) formats, supports
multi-key cache-aware rotation, and inlines images - but Hermes only needs the
OpenAI Chat Completions path, so this profile points at ``/v1``.

Setup:
  1. Start the gateway:  ``CODEBUDDY_API_KEY=ck_... python -m codebuddy_gateway``
  2. Set ``CODEBUDDY_API_KEY`` in ``~/.hermes/.env`` (used by the gateway).
  3. In ``~/.hermes/config.yaml``:  ``model.provider: codebuddy``, ``model.default: hy3``.

Override ``model.base_url`` in config.yaml to use a different gateway host/port,
or to bypass the gateway and hit CodeBuddy directly in stream-only mode
(``https://www.codebuddy.cn/v2``) - note: direct mode breaks Hermes' non-stream
subagent/compression/health-check calls.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class CodeBuddyProfile(ProviderProfile):
    """CodeBuddy via the local cli2api gateway."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Live model list from the gateway's /v1/models catalog."""
        effective_base = (base_url or self.base_url or "").rstrip("/")
        if not effective_base:
            return None
        url = effective_base + "/models"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("data", []) if isinstance(data, dict) else data
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception:
            return None


codebuddy = CodeBuddyProfile(
    name="codebuddy",
    aliases=("tencent-codebuddy", "codebuddy-code", "cb"),
    api_mode="chat_completions",
    display_name="CodeBuddy (Tencent)",
    description="Tencent CodeBuddy Code - GLM/DeepSeek/Kimi/Hunyuan via the cli2api gateway",
    signup_url="https://www.codebuddy.cn/",
    env_vars=("CODEBUDDY_API_KEY",),
    base_url="http://127.0.0.1:8787/v1",
    auth_type="api_key",
    supports_health_check=True,   # gateway serves /v1/models
    supports_vision=True,         # glm-4.6v, glm-5v-turbo, deepseek-v4-pro, hy3, ...
    fallback_models=(
        "hy3",
        "hy3-preview-agent",
        "glm-5.1",
        "glm-5.0",
        "glm-5.0-turbo",
        "glm-4.7",
        "glm-4.6",
        "glm-4.6v",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v3-2-volc",
        "kimi-k2.6",
        "kimi-k2.5",
        "minimax-m2.5",
        "hunyuan-2.0-instruct",
    ),
    default_max_tokens=64000,
    default_aux_model="glm-4.6",  # cheap model for compression / title-gen
    default_headers={
        "x-codebuddy-request": "1",
        "X-Product": "SaaS",
        "X-IDE-Type": "CLI",
        "User-Agent": "codebuddy-gateway/1.0",
    },
)

register_provider(codebuddy)
