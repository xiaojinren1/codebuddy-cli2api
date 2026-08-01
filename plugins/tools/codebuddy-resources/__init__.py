"""Hermes tool plugin: CodeBuddy free resources via the local gateway.

Exposes four tools backed by ``codebuddy-gateway`` (http://127.0.0.1:8787 by
default). The gateway owns the OAuth session and proxies CodeBuddy's
agenttool / images / videos endpoints, so Hermes itself never touches a token.

Registration mirrors the stock ``web-search-plus`` plugin contract:
    ctx.register_tool(name=, toolset=, schema=, handler=, check_fn=, ...)
where ``handler`` receives the full input dict as its first positional arg.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

_TOOLSET_NAME = "codebuddy-resources"

GATEWAY_URL = os.environ.get("CODEBUDDY_GATEWAY_URL", "http://127.0.0.1:8787").rstrip("/")
GATEWAY_KEY = os.environ.get("CODEBUDDY_GATEWAY_KEY", "")


def _gateway_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if GATEWAY_KEY:
        h["Authorization"] = f"Bearer {GATEWAY_KEY}"
        h["X-API-Key"] = GATEWAY_KEY
    return h


def _post(path: str, payload: dict) -> Any:
    url = GATEWAY_URL + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_gateway_headers(), method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"gateway {path} -> HTTP {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"gateway {path} unreachable: {e}")


# ── web search ─────────────────────────────────────────────────────────────
def _web_search(args: dict) -> str:
    payload = {"query": args.get("query", "")}
    for k in ("topic", "max_results", "query_keyword_groups", "allowed_domains", "blocked_domains"):
        if args.get(k) is not None:
            payload[k] = args[k]
    try:
        data = _post("/v1/web_search", payload)
    except RuntimeError as e:
        return f"[codebuddy_web_search error] {e}"
    results = (data.get("results") or data.get("data", {}).get("results") or [])
    if not results:
        return "(no results)"
    out = [f"# Web search: {payload['query']}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(untitled)"
        url = r.get("url") or ""
        snippet = r.get("snippet") or r.get("content") or ""
        out.append(f"{i}. **{title}**\n   {url}\n   {snippet}\n")
    return "\n".join(out)


# ── image generation ───────────────────────────────────────────────────────
def _image_generate(args: dict) -> str:
    payload = {"prompt": args.get("prompt", "")}
    for k in ("model", "n", "size", "quality", "response_format"):
        if args.get(k) is not None:
            payload[k] = args[k]
    try:
        data = _post("/v1/images/generations", payload)
    except RuntimeError as e:
        return f"[codebuddy_image_generate error] {e}"
    items = (data.get("data") or [{}])
    lines = []
    for it in items:
        if it.get("url"):
            lines.append(f"image: {it['url']}")
        elif it.get("b64_json"):
            lines.append(f"image: <base64, {len(it['b64_json'])} chars>")
        else:
            lines.append(str(it))
    return "\n".join(lines) or str(data)


# ── image edit ────────────────────────────────────────────────────────────
def _image_edit(args: dict) -> str:
    payload = {"prompt": args.get("prompt", "")}
    if args.get("image") is not None:
        payload["image"] = args["image"]
    if args.get("mask") is not None:
        payload["mask"] = args["mask"]
    for k in ("model", "n", "size", "response_format"):
        if args.get(k) is not None:
            payload[k] = args[k]
    try:
        data = _post("/v1/images/edits", payload)
    except RuntimeError as e:
        return f"[codebuddy_image_edit error] {e}"
    items = (data.get("data") or [{}])
    lines = [f"image: {it.get('url') or ('<base64>' if it.get('b64_json') else str(it))}" for it in items]
    return "\n".join(lines) or str(data)


# ── video generation (submit + poll) ───────────────────────────────────────
def _video_generate(args: dict) -> str:
    payload = {"prompt": args.get("prompt", "")}
    for k in ("model", "seconds", "negative_prompt"):
        if args.get(k) is not None:
            payload[k] = args[k]
    try:
        data = _post("/v1/videos/generations", payload)
    except RuntimeError as e:
        return f"[codebuddy_video_generate error] {e}"
    task_id = (data.get("id") or data.get("data", {}).get("id")
               or data.get("task_id") or (data.get("data") or {}).get("task_id"))
    if not task_id:
        return f"video submitted; unexpected response: {json.dumps(data, ensure_ascii=False)[:300]}"
    # Poll the tasks endpoint until complete (gateway proxies /v2/videos/tasks).
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            res = _post("/v1/videos/tasks", {"id": task_id})
        except RuntimeError as e:
            return f"[codebuddy_video_generate poll error] {e}"
        status = (res.get("status") or (res.get("data") or {}).get("status") or "").lower()
        if status in ("completed", "succeeded", "success"):
            videos = (res.get("data") or res.get("videos") or res.get("results") or [])
            if isinstance(videos, list) and videos:
                urls = [v.get("url") for v in videos if isinstance(v, dict) and v.get("url")]
                if urls:
                    return "video: " + "\nvideo: ".join(urls)
            return f"video ready: {json.dumps(res, ensure_ascii=False)[:300]}"
        if status in ("failed", "error"):
            return f"video generation failed: {json.dumps(res, ensure_ascii=False)[:300]}"
        time.sleep(5)
    return f"video generation timed out (task {task_id} still pending after 600s)"


# ── registration ───────────────────────────────────────────────────────────
def register(ctx: Any) -> None:
    ctx.register_tool(
        name="codebuddy_web_search",
        toolset=_TOOLSET_NAME,
        schema={
            "name": "codebuddy_web_search",
            "description": (
                "Search the web via CodeBuddy's agenttool. Returns a list of "
                "results with title, URL and snippet. Use for current events, "
                "documentation lookups, and grounding factual claims."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "topic": {"type": "string",
                              "enum": ["general", "news", "programming", "documentation",
                                       "academic", "finance", "technology", "legal", "medical"],
                              "description": "Search vertical. Optional."},
                    "max_results": {"type": "integer", "description": "Max results (default 5)", "default": 5},
                    "allowed_domains": {"type": "array", "items": {"type": "string"},
                                        "description": "Restrict to these domains. Optional."},
                    "blocked_domains": {"type": "array", "items": {"type": "string"},
                                        "description": "Exclude these domains. Optional."},
                    "query_keyword_groups": {"type": "array", "items": {"type": "string"},
                                             "description": "Alternate keyword phrasings. Optional."},
                },
                "required": ["query"],
            },
        },
        handler=lambda a, **k: _web_search(a if isinstance(a, dict) else {"query": a}),
        check_fn=lambda: True,
        requires_env=[],
        description="Web search via CodeBuddy agenttool",
        emoji="🔍",
    )

    ctx.register_tool(
        name="codebuddy_image_generate",
        toolset=_TOOLSET_NAME,
        schema={
            "name": "codebuddy_image_generate",
            "description": "Generate an image from a text prompt via CodeBuddy. Returns image URL(s) or base64.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description"},
                    "model": {"type": "string", "description": "Model id override. Optional."},
                    "n": {"type": "integer", "description": "Number of images (default 1)", "default": 1},
                    "size": {"type": "string", "description": "e.g. 1024x1024. Optional."},
                    "quality": {"type": "string", "description": "Optional."},
                    "response_format": {"type": "string", "description": "url or b64_json. Optional."},
                },
                "required": ["prompt"],
            },
        },
        handler=lambda a, **k: _image_generate(a if isinstance(a, dict) else {"prompt": a}),
        check_fn=lambda: True,
        requires_env=[],
        description="Text-to-image via CodeBuddy",
        emoji="🎨",
    )

    ctx.register_tool(
        name="codebuddy_image_edit",
        toolset=_TOOLSET_NAME,
        schema={
            "name": "codebuddy_image_edit",
            "description": "Edit an existing image via CodeBuddy (inpainting/restyle). Returns image URL(s) or base64.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Edit instruction"},
                    "image": {"type": "string", "description": "Source image as data URI or URL"},
                    "mask": {"type": "string", "description": "Mask as data URI (optional)"},
                    "model": {"type": "string", "description": "Model id override. Optional."},
                    "size": {"type": "string", "description": "e.g. 1024x1024. Optional."},
                },
                "required": ["prompt", "image"],
            },
        },
        handler=lambda a, **k: _image_edit(a if isinstance(a, dict) else a),
        check_fn=lambda: True,
        requires_env=[],
        description="Image editing via CodeBuddy",
        emoji="✏️",
    )

    ctx.register_tool(
        name="codebuddy_video_generate",
        toolset=_TOOLSET_NAME,
        schema={
            "name": "codebuddy_video_generate",
            "description": (
                "Generate a short video from a text prompt via CodeBuddy. Submits the "
                "job and polls until the MP4 URL is ready (up to ~10 min). Returns the video URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Video description"},
                    "seconds": {"type": "integer", "description": "Duration in seconds (default 5)", "default": 5},
                    "negative_prompt": {"type": "string", "description": "Things to avoid. Optional."},
                    "model": {"type": "string", "description": "Model id override. Optional."},
                },
                "required": ["prompt"],
            },
        },
        handler=lambda a, **k: _video_generate(a if isinstance(a, dict) else {"prompt": a}),
        check_fn=lambda: True,
        requires_env=[],
        description="Text-to-video via CodeBuddy",
        emoji="🎬",
    )
