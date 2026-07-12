"""Image normalization for the gateway.

CodeBuddy accepts OpenAI ``image_url`` parts with **data URIs only** - http(s)
image URLs are rejected with 400 "invalid parameter value". So every image
from every input format is normalized to an OpenAI ``image_url`` part whose
``url`` is a ``data:<mime>;base64,...`` string. Remote http(s) images are
fetched (size-bounded) and inlined.

Input shapes handled (each format parser converts its native image blocks to
OpenAI ``image_url`` parts first, then this module inlines any http URLs):

  * OpenAI:     {"type":"image_url","image_url":{"url":"data:..." | "https://..."}}
  * Anthropic:  {"type":"image","source":{"type":"base64","media_type":..,"data":..}}
                {"type":"image","source":{"type":"url","url":"https://..."}}
  * Gemini:     {"inlineData":{"mimeType":..,"data":"<b64>"}}
                {"fileData":{"fileUri":"https://...","mimeType":..}}
  * Codex/Resp: {"type":"input_image","image_url":"data:..."|"https://..."}
"""

from __future__ import annotations

import base64
import urllib.request
from typing import Any

from .common import IMAGE_FETCH_MAX_BYTES, log

_DATA_PREFIXES = ("data:", "data:image/")


def _ext_to_mime(url: str) -> str:
    low = url.lower().split("?", 1)[0]
    for ext, mime in (
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
        (".bmp", "image/bmp"),
    ):
        if low.endswith(ext):
            return mime
    return "image/png"


def fetch_inline(url: str) -> str:
    """Download a remote image and return a ``data:<mime>;base64,...`` URI."""
    req = urllib.request.Request(url, headers={"User-Agent": "codebuddy-gateway/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        mime = resp.headers.get("Content-Type", "").split(";")[0].strip() or _ext_to_mime(url)
        if not mime.startswith("image/"):
            mime = _ext_to_mime(url)
        data = resp.read(IMAGE_FETCH_MAX_BYTES + 1)
        if len(data) > IMAGE_FETCH_MAX_BYTES:
            raise ValueError(f"image exceeds {IMAGE_FETCH_MAX_BYTES} bytes: {url}")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def to_openai_image_url(part: dict[str, Any]) -> dict[str, Any] | None:
    """Convert any native image part to an OpenAI ``image_url`` part (data URI)."""
    if not isinstance(part, dict):
        return None

    # Already OpenAI image_url
    if part.get("type") == "image_url":
        iu = part.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else iu
        if isinstance(url, str) and url:
            if url.startswith("data:"):
                return {"type": "image_url", "image_url": {"url": url}}
            if url.startswith(("http://", "https://")):
                return {"type": "image_url", "image_url": {"url": fetch_inline(url)}}
        return None

    # Codex/Responses input_image
    if part.get("type") == "input_image":
        url = part.get("image_url") or part.get("url")
        if isinstance(url, str) and url:
            if url.startswith("data:"):
                return {"type": "image_url", "image_url": {"url": url}}
            if url.startswith(("http://", "https://")):
                return {"type": "image_url", "image_url": {"url": fetch_inline(url)}}
        return None

    # Anthropic image block
    if part.get("type") == "image":
        src = part.get("source") or {}
        if src.get("type") == "base64":
            mime = src.get("media_type") or "image/png"
            data = src.get("data", "")
            return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
        if src.get("type") == "url":
            return {"type": "image_url", "image_url": {"url": fetch_inline(src["url"])}}
        return None

    # Gemini inlineData / fileData (also accepted if a parser passes them through)
    if "inlineData" in part:
        idata = part["inlineData"] or {}
        mime = idata.get("mimeType") or "image/png"
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{idata.get('data','')}"}}
    if "fileData" in part:
        fdata = part["fileData"] or {}
        return {"type": "image_url", "image_url": {"url": fetch_inline(fdata.get("fileUri", ""))}}

    return None


def normalize_message_content(content: Any) -> Any:
    """If ``content`` is a list of parts, inline any image parts (data URI).

    Text parts are left as OpenAI text parts. Non-image, non-text parts are
    passed through unchanged. Returns ``content`` unchanged if it's a string.
    """
    if not isinstance(content, list):
        return content
    out: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            out.append(part)
            continue
        ptype = part.get("type")
        if ptype == "text" or "text" in part and ptype in (None, "text"):
            out.append(part)
            continue
        img = to_openai_image_url(part)
        if img is not None:
            out.append(img)
            continue
        # Anthropic tool_use / tool_result / Gemini functionCall etc. handled by parsers
        out.append(part)
    return out


def normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inline images in every message's ``content`` list (OpenAI shape)."""
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            m["content"] = normalize_message_content(m["content"])
    return messages


__all__ = [
    "fetch_inline",
    "to_openai_image_url",
    "normalize_message_content",
    "normalize_messages",
]
