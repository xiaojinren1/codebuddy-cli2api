"""CodeBuddy upstream client + canonical event stream.

Forces ``stream: true`` (CodeBuddy rejects non-stream with 400) and yields
parsed OpenAI SSE chunk dicts. Also exposes :func:`iter_events` which turns
raw chunks into a canonical event stream consumed by the aggregator and every
format emitter.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

from .common import (
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_PATH,
    UPSTREAM_TIMEOUT,
    UpstreamError,
    log,
    upstream_headers,
)


def stream_codebuddy(
    openai_body: dict[str, Any],
    api_key: str,
    *,
    base_url: str | None = None,
) -> Iterator[dict[str, Any]]:
    """POST ``openai_body`` (forced ``stream:true``) to CodeBuddy, yield chunk dicts.

    Raises :class:`UpstreamError` on non-2xx. Yields parsed ``data:`` JSON
    payloads until ``[DONE]`` or stream end.
    """
    body = dict(openai_body)
    body["stream"] = True
    # stream_options.include_usage so the final chunk carries usage (CodeBuddy
    # already sends usage on the last chunk, but be explicit).
    so = body.get("stream_options")
    if isinstance(so, dict):
        so["include_usage"] = True
    else:
        body["stream_options"] = {"include_usage": True}

    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + DEFAULT_CHAT_PATH
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = upstream_headers(api_key)

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    log.debug("-> CodeBuddy %s model=%s msgs=%d", url, body.get("model"), len(body.get("messages", [])))

    try:
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        try:
            err_body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            err_body = raw.decode("utf-8", "replace")[:500] if raw else None
        raise UpstreamError(e.code, f"CodeBuddy HTTP {e.code}", err_body)
    except urllib.error.URLError as e:
        raise UpstreamError(502, f"CodeBuddy connection error: {e}")

    status = resp.status
    if status < 200 or status >= 300:
        raw = resp.read()
        try:
            err_body = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            err_body = raw.decode("utf-8", "replace")[:500] if raw else None
        raise UpstreamError(status, f"CodeBuddy HTTP {status}", err_body)

    try:
        for raw_line in resp:
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            if line.startswith(":"):  # SSE comment / keep-alive
                continue
            if line.startswith("data:"):
                data = line[5:].lstrip()
                if data == "[DONE]":
                    break
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    log.debug("unparseable SSE data: %r", data[:120])
                    continue
            # ignore non-data lines (event:, id:, retry:)
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ── Canonical event stream ──────────────────────────────────────────────────
# A format-agnostic view of the OpenAI chunk stream. Each event is a tuple:
#   ("text", str)                       - assistant text delta
#   ("reasoning", str)                  - reasoning_content delta
#   ("tool_start", index, id, name)     - new tool call begun
#   ("tool_delta", index, args_frag)    - tool-call arguments fragment
#   ("finish", reason)                  - finish_reason (stop|tool_calls|...)
#   ("usage", dict)                     - usage object (final chunk)
#   ("end",)                            - stream terminated


def iter_events(chunks: Iterator[dict[str, Any]]) -> Iterator[tuple]:
    """Convert raw OpenAI chunks into canonical events."""
    saw_any = False
    meta_emitted = False
    for chunk in chunks:
        saw_any = True
        if not meta_emitted:
            meta_emitted = True
            yield ("meta", chunk.get("id"), chunk.get("created"), chunk.get("model"))
        usage = chunk.get("usage")
        choices = chunk.get("choices") or []
        if usage:
            yield ("usage", usage)
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            yield ("text", delta["content"])
        rc = delta.get("reasoning_content")
        if rc:
            yield ("reasoning", rc)
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            tid = tc.get("id")
            name = fn.get("name")
            if tid or name:
                yield ("tool_start", idx, tid or "", name or "")
            args = fn.get("arguments")
            if args:
                yield ("tool_delta", idx, args)
        fr = choice.get("finish_reason")
        if fr:
            yield ("finish", fr)
    if saw_any:
        yield ("end",)


def extract_usage_from_chunks(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pull the last non-null usage from a list of raw chunks (fallback)."""
    usage = None
    for c in chunks:
        if c.get("usage"):
            usage = c["usage"]
    return usage
