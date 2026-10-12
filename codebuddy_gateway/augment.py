"""Capability augmentation for models that lack vision or web access.

Two transparent upgrades, applied when the requested model lacks them:

1. **Vision** — if the incoming messages contain images and the target model
   has no vision (``img=False`` in the catalog), every image is described by
   a vision model (default ``kimi-k3-2``) and the description text is injected
   in place of the image. ``glm-5.3`` is excluded: it is text-only; use
   ``glm-5.3-flash`` for native multimodal.

2. **Web access** — two virtual tools (``web_search`` / ``web_fetch``) are
   appended to the request's ``tools``. When the main model calls them, the
   gateway executes the call against CodeBuddy's free ``/agenttool/v1/*``
   endpoints (verified 2026-08: plain ``ck_`` keys are accepted with
   ``X-API-Key``) and replays the conversation with a ``tool`` role message
   containing the result. The client never sees the virtual tools: they are
   stripped from the outgoing stream, so tool lists round-trip unchanged.

Configuration (env):

- ``CODEBUDDY_AUGMENT``            — "0"/"off" disables everything (default on)
- ``CODEBUDDY_VISION_MODEL``       — vision describer model (default kimi-k3-2)
- ``CODEBUDDY_AUGMENT_MAX_LOOPS``  — max virtual-tool loops per request (default 4)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Any

from .common import (
    CODEBUDDY_RESOURCE_BASE_URL,
    DEFAULT_BASE_URL,
    UPSTREAM_TIMEOUT,
    GatewayError,
    log,
    models_json,
    upstream_headers,
)

# ── configuration ───────────────────────────────────────────────────────────

def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "off", "false", "no", "")

AUGMENT_ENABLED = _env_flag("CODEBUDDY_AUGMENT")
VISION_MODEL = os.environ.get("CODEBUDDY_VISION_MODEL", "kimi-k3-2")
MAX_LOOPS = int(os.environ.get("CODEBUDDY_AUGMENT_MAX_LOOPS", "4"))

# Virtual tools injected for web access. Descriptions follow the model's
# language so glm (Chinese-first) picks them up naturally.
VIRTUAL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索最新信息。当你需要当前日期之后的事件、最新版本号、实时数据、"
                "或任何你可能不知道的信息时调用。返回搜索结果列表（标题、URL、摘要）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "抓取指定 URL 的网页内容并转为 Markdown。用于阅读搜索结果中的页面全文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的网页 URL"},
                },
                "required": ["url"],
            },
        },
    },
]

VIRTUAL_TOOL_NAMES = {t["function"]["name"] for t in VIRTUAL_TOOLS}


# ── catalog helpers ─────────────────────────────────────────────────────────

_model_capabilities: dict[str, dict[str, Any]] | None = None


def _capabilities() -> dict[str, dict[str, Any]]:
    """id -> capabilities dict from the (cached) model catalog."""
    global _model_capabilities
    if _model_capabilities is None:
        caps: dict[str, dict[str, Any]] = {}
        try:
            for m in models_json().get("data", []):
                caps[m.get("id", "")] = m.get("capabilities") or {}
        except Exception as e:  # noqa: BLE001
            log.debug("augment: catalog unavailable: %s", e)
        _model_capabilities = caps
    return _model_capabilities


_NO_VISION_PROXY = {"glm-5.3"}  # text-only; native vision lives on glm-5.3-flash


def model_has_vision(model: str) -> bool:
    """True when the model id reports vision support (unknown -> True)."""
    caps = _capabilities().get(model)
    if caps is None:
        return True  # unknown model: do not touch it
    return bool(caps.get("img") or caps.get("supports_vision"))


# ── vision: describe images with the vision model ──────────────────────────

_IMAGE_RE = re.compile(r"^data:image/([^;]+);base64,(.*)$", re.S)
# Keep descriptions bounded; the main model needs signal, not pixels.
_DESCRIBE_PROMPT = (
    "Describe this image precisely and completely for someone who cannot see it. "
    "Cover: what is shown (UI screenshot? photo? diagram? code? error message?), all "
    "visible text verbatim, layout, colors, and any notable details. Be thorough but "
    "not verbose. Reply in the language the text in the image uses."
)


def _describe_image(data_uri: str, api_key: str) -> str:
    """Ask the vision model to describe one data-URI image. Returns text."""
    body = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _DESCRIBE_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": "low",  # describing needs speed, not depth
    }
    url = DEFAULT_BASE_URL.rstrip("/") + "/v2/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=upstream_headers(api_key),
        method="POST",
    )
    text_parts: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].lstrip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for ch in chunk.get("choices") or []:
                    delta = (ch.get("delta") or {}).get("content")
                    if delta:
                        text_parts.append(delta)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise GatewayError(502, f"vision describe failed (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise GatewayError(502, f"vision describe unreachable: {e}") from e
    desc = "".join(text_parts).strip()
    if not desc:
        raise GatewayError(502, "vision describe returned empty content")
    return desc


def _content_images(content: Any) -> list[str]:
    """Extract data-URI image URLs from one message content (str or parts)."""
    if isinstance(content, str) or content is None:
        return []
    images: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            u = ((part.get("image_url") or {}).get("url")) or ""
            if u.startswith("data:image/"):
                images.append(u)
    return images


def inject_vision_descriptions(
    openai_body: dict[str, Any], api_key: str
) -> dict[str, Any]:
    """Replace images with vision-model descriptions for non-vision targets.

    Returns the body unchanged when the model sees images itself or no images
    are present. Cache-aware: identical data URIs are described once per
    process (LRU bounded).
    """
    model = openai_body.get("model", "")
    if model in _NO_VISION_PROXY or model_has_vision(model):
        return openai_body
    messages = openai_body.get("messages")
    if not isinstance(messages, list):
        return openai_body

    # Collect every image first (fail fast on key errors before any rewrite).
    all_images: list[str] = []
    for msg in messages:
        all_images.extend(_content_images(msg.get("content") if isinstance(msg, dict) else None))
    if not all_images:
        return openai_body

    cache = _get_desc_cache()
    descriptions: dict[str, str] = {}
    for uri in all_images:
        if uri in cache:
            descriptions[uri] = cache[uri]
            continue
        log.info("augment: describing image (%d bytes b64) via %s", len(uri), VISION_MODEL)
        descriptions[uri] = _describe_image(uri, api_key)
        cache[uri] = descriptions[uri]

    # Rewrite message contents: image part -> text part with the description.
    new_messages: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict) or not _content_images(msg.get("content")):
            new_messages.append(msg)
            continue
        content = msg["content"]
        parts_out: list[dict[str, Any]] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
            ):
                uri = ((part.get("image_url") or {}).get("url")) or ""
                parts_out.append({
                    "type": "text",
                    "text": (
                        f"[图片内容（由 {VISION_MODEL} 视觉模型描述，你本身无法直接看到图片）]\n"
                        f"{descriptions.get(uri, '(描述生成失败)')}"
                    ),
                })
            else:
                parts_out.append(part)
        new_messages.append({**msg, "content": parts_out})

    out = dict(openai_body)
    out["messages"] = new_messages
    return out


# Bounded LRU cache for image descriptions (identical screenshots recur).
_DESC_CACHE: OrderedDict[str, str] = OrderedDict()
_DESC_CACHE_MAX = 32


def _get_desc_cache() -> "OrderedDict[str, str]":
    while len(_DESC_CACHE) > _DESC_CACHE_MAX:
        _DESC_CACHE.popitem(last=False)
    return _DESC_CACHE


# ── web: virtual tools executed against free CodeBuddy endpoints ───────────

def _resource_call(path: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """POST to a free agenttool endpoint using the plain ck_ key."""
    url = CODEBUDDY_RESOURCE_BASE_URL.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=upstream_headers(api_key, accept="application/json"),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise GatewayError(502, f"{path} HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        # copilot.tencent.com refused? try the public www endpoint as fallback
        if "copilot.tencent.com" in url:
            url2 = DEFAULT_BASE_URL.rstrip("/") + path
            req2 = urllib.request.Request(
                url2,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=upstream_headers(api_key, accept="application/json"),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req2, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e2:  # noqa: BLE001
                raise GatewayError(502, f"{path} unreachable: {e} / {e2}") from e2
        raise GatewayError(502, f"{path} unreachable: {e}") from e


def web_search(query: str, api_key: str, max_results: int = 5) -> list[dict[str, Any]]:
    payload = {
        "query": query,
        "type": "text2text",
        "max_results": max_results,
    }
    data = _resource_call("/agenttool/v1/search", payload, api_key)
    results = data.get("results") or []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", ""),
        }
        for r in results
        if isinstance(r, dict)
    ]


def web_fetch(url: str, api_key: str, max_length: int = 100_000) -> str:
    payload = {
        "url": url,
        "format": "markdown",
        "timeout": 20,
        "max_length": max_length,
    }
    data = _resource_call("/agenttool/v1/webfetch", payload, api_key)
    return str(data.get("content") or "")


def format_search_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(没有搜索到结果)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   摘要: {r['snippet']}")
    return "\n".join(lines)


def inject_web_tools(openai_body: dict[str, Any]) -> dict[str, Any]:
    """Append virtual web tools unless the client already sent them."""
    tools = openai_body.get("tools")
    if not isinstance(tools, list):
        tools = []
    existing = {
        (t.get("function") or {}).get("name")
        for t in tools
        if isinstance(t, dict)
    }
    add = [t for t in VIRTUAL_TOOLS if t["function"]["name"] not in existing]
    if not add:
        return openai_body
    out = dict(openai_body)
    out["tools"] = tools + add
    return out


def is_virtual_tool(name: str) -> bool:
    return name in VIRTUAL_TOOL_NAMES


def execute_virtual_tool(name: str, arguments: dict[str, Any], api_key: str) -> str:
    """Run one virtual tool call, return the text result for the model."""
    try:
        if name == "web_search":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return "错误: query 不能为空"
            results = web_search(query, api_key)
            log.info("augment: web_search(%r) -> %d results", query, len(results))
            return format_search_results(results)
        if name == "web_fetch":
            url = str(arguments.get("url", "")).strip()
            if not url:
                return "错误: url 不能为空"
            content = web_fetch(url, api_key)
            log.info("augment: web_fetch(%r) -> %d chars", url, len(content))
            if not content:
                return "(网页内容为空)"
            return content
        return f"错误: 未知工具 {name}"
    except GatewayError as e:
        return f"工具调用失败: {e.message}"
    except Exception as e:  # noqa: BLE001
        return f"工具调用异常: {e}"
