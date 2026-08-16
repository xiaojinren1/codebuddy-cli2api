"""CodeBuddy upstream client + canonical event stream.

Forces ``stream: true`` (CodeBuddy rejects non-stream with 400) and yields
parsed OpenAI SSE chunk dicts. Also exposes :func:`iter_events` which turns
raw chunks into a canonical event stream consumed by the aggregator and every
format emitter.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterator

from .common import (
    DEFAULT_BASE_URL,
    DEFAULT_CHAT_PATH,
    UPSTREAM_TIMEOUT,
    GatewayError,
    UpstreamError,
    log,
    upstream_headers,
)


def stream_codebuddy(
    openai_body: dict[str, Any],
    api_key: str,
    *,
    base_url: str | None = None,
    mode: str = "key",
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

    # ── Reasoning / chain-of-thought injection ─────────────────────────────
    # CodeBuddy only emits ``reasoning_content`` when the request carries a
    # top-level ``reasoning_effort`` string ("low" | "high"). Most OpenAI-style
    # clients (incl. Hermes' OpenAI transport, which gates reasoning on a
    # host allowlist that excludes the local gateway) never send it, so the
    # thinking chain silently disappears. Inject a default unless the client
    # already asked for something or explicitly disabled reasoning.
    # Env override: CODEBUDDY_REASONING_EFFORT (off|false|"" to disable).
    _reasoning_off = {"off", "false", "none", "no", "minimal", "0", ""}
    env_effort = os.environ.get("CODEBUDDY_REASONING_EFFORT", "high").strip().lower()
    explicit_effort = body.get("reasoning_effort")
    explicit_effort_disabled = False
    if "reasoning_effort" in body and explicit_effort is None:
        raise GatewayError(400, "reasoning_effort must be a string")
    if explicit_effort is not None:
        if isinstance(explicit_effort, bool):
            explicit_effort = "high" if explicit_effort else "none"
        elif not isinstance(explicit_effort, str):
            raise GatewayError(400, "reasoning_effort must be a string")
        else:
            explicit_effort = explicit_effort.strip().lower()
        if explicit_effort not in _reasoning_off | {"low", "high"}:
            raise GatewayError(400, f"unsupported reasoning_effort: {body.get('reasoning_effort')}")
        if explicit_effort in _reasoning_off:
            explicit_effort_disabled = True
            body.pop("reasoning_effort", None)
        else:
            body["reasoning_effort"] = explicit_effort
    explicit_disable = (
        body.get("enable_thinking") is False
        or body.get("reasoning") is False
        or body.get("reasoning_effort") in (False, 0, "off", "false", "none", "no", "")
        or explicit_effort_disabled
    )
    if env_effort not in _reasoning_off | {"low", "high"}:
        raise GatewayError(400, f"unsupported CODEBUDDY_REASONING_EFFORT: {env_effort}")
    if "reasoning_effort" not in body and not explicit_disable and env_effort not in _reasoning_off:
        body["reasoning_effort"] = env_effort

    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + DEFAULT_CHAT_PATH
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = upstream_headers(api_key, mode=mode)

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
    started_tools: set[int] = set()
    tool_meta: dict[int, tuple[str, str]] = {}
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
        rc = delta.get("reasoning_content")
        if rc:
            yield ("reasoning", rc)
        if delta.get("content"):
            yield ("text", delta["content"])
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            tid = tc.get("id")
            name = fn.get("name")
            args = fn.get("arguments")
            previous = tool_meta.get(idx, ("", ""))
            if idx not in started_tools and (tid or name or args is not None):
                started_tools.add(idx)
                tool_meta[idx] = (tid or "", name or "")
                yield ("tool_start", idx, tid or "", name or "")
            elif idx in started_tools and (tid or name) and (tid != previous[0] or name != previous[1]):
                tool_meta[idx] = (tid or previous[0], name or previous[1])
                yield ("tool_start", idx, tid or "", name or "")
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
