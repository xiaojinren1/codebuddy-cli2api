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
from . import sanitize


def _read_error_body(reader) -> Any:
    raw = b""
    try:
        raw = reader.read()
    except Exception:
        pass
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw.decode("utf-8", "replace")[:500]


def _open_upstream(url: str, body: dict[str, Any], api_key: str, mode: str):
    """POST ``body`` to ``url``; return the open response or raise UpstreamError."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = upstream_headers(api_key, mode=mode)
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    log.debug("-> CodeBuddy %s model=%s msgs=%d", url, body.get("model"), len(body.get("messages", [])))
    try:
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, f"CodeBuddy HTTP {e.code}", _read_error_body(e))
    except urllib.error.URLError as e:
        raise UpstreamError(502, f"CodeBuddy connection error: {e}")
    status = resp.status
    if status < 200 or status >= 300:
        raise UpstreamError(status, f"CodeBuddy HTTP {status}", _read_error_body(resp))
    return resp


def _probe_blocked(url: str, text: str, model: str | None, api_key: str, mode: str) -> bool:
    """Send ``text`` as a 1-token probe; True when upstream blocks it."""
    probe = {
        "model": model or "glm-5.3",
        "messages": [{"role": "system", "content": text}, {"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 1,
    }
    try:
        resp = _open_upstream(url, probe, api_key, mode)
    except UpstreamError as e:
        return sanitize.is_block_error(e.status, e.body)
    try:
        resp.close()
    except Exception:
        pass
    return False


_REASONING_OFF = {"off", "false", "none", "no", "minimal", "0", ""}
_REASONING_ON = {"low", "medium", "high", "max", "ultra", "xhigh"}


def _canonical_effort(token: str) -> str:
    """Map a known effort token onto the value we send upstream."""
    if token in _REASONING_OFF:
        return "low"
    if token in ("xhigh", "ultra"):
        return "max"
    return token


def apply_reasoning_effort(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize ``reasoning_effort`` in place. Returns the same dict.

    Client omit  → ``max``. glm-5.3 thinks deeply with the field absent, but
                   deepseek-v4-* emit *no* CoT unless an effort is present, so a
                   value must be injected. ``max`` is accepted by every model we
                   probed, including non-reasoning ones (hy3 / hunyuan / auto).
    off/none/low → ``low`` (only way to disable CoT).
    medium/high  → passthrough (light CoT).
    max/xhigh    → ``max`` (deep CoT).
    Env ``CODEBUDDY_REASONING_EFFORT`` overrides the injected default; set it to
    an empty string to send no field at all.

    """
    explicit = body.get("reasoning_effort")
    if "reasoning_effort" in body and explicit is None:
        raise GatewayError(400, "reasoning_effort must be a string")

    token: str | None = None
    if explicit is not None:
        if isinstance(explicit, bool):
            token = "high" if explicit else "none"
        elif not isinstance(explicit, str):
            raise GatewayError(400, "reasoning_effort must be a string")
        else:
            token = explicit.strip().lower()
        if token not in _REASONING_OFF | _REASONING_ON:
            raise GatewayError(400, f"unsupported reasoning_effort: {body.get('reasoning_effort')}")
    elif body.get("enable_thinking") is False or body.get("reasoning") is False:
        token = "none"
    else:
        env_effort = os.environ.get("CODEBUDDY_REASONING_EFFORT", "max").strip().lower()
        if env_effort:
            if env_effort not in _REASONING_OFF | _REASONING_ON:
                raise GatewayError(400, f"unsupported CODEBUDDY_REASONING_EFFORT: {env_effort}")
            token = env_effort

    if token is None:
        body.pop("reasoning_effort", None)
    else:
        body["reasoning_effort"] = _canonical_effort(token)
    return body




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

    # ── Reasoning / chain-of-thought ───────────────────────────────────────
    # Measured against the live upstream 2026-09-08 — the two families disagree,
    # so a single "just omit it" rule cannot work:
    #   glm-5.3 / glm-5.3-flash : omit or max/xhigh → deep, medium/high → light, low → off
    #   deepseek-v4-pro/flash   : omit → NO CoT at all, any effort value → CoT
    # Injecting ``max`` when the client sends nothing satisfies both, and every
    # non-reasoning model we probed (hy3 / hunyuan / kimi / auto) accepts it.
    # Passthrough known values, alias xhigh/ultra → max, map disables to ``low``.
    # CODEBUDDY_REASONING_EFFORT overrides the default ("" = send no field).
    apply_reasoning_effort(body)


    url = (base_url or DEFAULT_BASE_URL).rstrip("/") + DEFAULT_CHAT_PATH

    # ── Prompt-fingerprint block (400 code 11128) ───────────────────────────
    # CodeBuddy blocks verbatim system prompts of other coding agents. Break
    # known literals up front; on an unknown one, learn it (bisect probes) and
    # retry once. See sanitize.py.
    body = sanitize.neutralize_body(body)
    try:
        resp = _open_upstream(url, body, api_key, mode)
    except UpstreamError as e:
        if not (sanitize.learning_enabled() and sanitize.is_block_error(e.status, e.body)):
            raise
        learned = sanitize.learn_blocked_literal(
            body, lambda text: _probe_blocked(url, text, body.get("model"), api_key, mode))
        if not learned:
            raise
        retry_body = sanitize.neutralize_body(body)
        if retry_body is body:
            raise
        log.info("sanitize: retrying after neutralizing the blocked literal")
        resp = _open_upstream(url, retry_body, api_key, mode)


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
