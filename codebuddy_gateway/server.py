"""HTTP server: routes OpenAI/Anthropic/Gemini/Codex endpoints to CodeBuddy.

Stdlib ``ThreadingHTTPServer``. Handles per-format auth, multi-key retry on
connection errors, usage capture for cache-aware rotation, and SSE streaming.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain
from typing import Any, Iterator
from urllib.parse import urlparse, parse_qs

from .common import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    GATEWAY_API_KEY,
    GatewayError,
    UpstreamError,
    log,
    models_json,
)
from . import augment
from .formats import anthropic, codex, gemini, openai
from .resources import forward_resource, resolve_resource
from .rotation import KeyPool
from .upstream import stream_codebuddy, extract_usage_from_chunks
from .auth import SESSIONS

# Methods on the pool that are retryable upstream statuses.
_RETRYABLE = {401, 403, 408, 409, 429, 500, 502, 503, 504}

_pool = KeyPool()


# ── format routing ──────────────────────────────────────────────────────────
def _route(method: str, path: str):
    """Return (format_module, client_stream_hint, kind) or None."""
    p = path.split("?", 1)[0].rstrip("/")
    if method == "GET":
        if p in ("/v1/models", "/v1beta/models", "/models"):
            return ("catalog", None, "get-models")
        if p in ("/health", "/"):
            return (None, None, "health")
        if p == "/debug/keys":
            return (None, None, "debug-keys")
        return None
    if method == "POST":
        if p in ("/v1/chat/completions", "/chat/completions"):
            return (openai, None, "openai")
        if p in ("/v1/messages", "/messages"):
            return (anthropic, None, "anthropic")
        if p.endswith(":generateContent") or p.endswith(":streamGenerateContent"):
            return (gemini, ":streamGenerateContent" in p, "gemini")
        if p in ("/v1/responses", "/responses"):
            return (codex, None, "codex")
    return None


def _format_name(fmt) -> str:
    return {openai: "openai", anthropic: "anthropic", gemini: "gemini", codex: "codex"}.get(fmt, "openai")


# ── error shaping per format ────────────────────────────────────────────────
def _error_body(status: int, message: str, fmt_name: str) -> tuple[bytes, str]:
    if fmt_name == "anthropic":
        etype = "authentication_error" if status in (401, 403) else (
            "rate_limit_error" if status == 429 else "api_error")
        return json.dumps({"type": "error", "error": {"type": etype, "message": message}},
                          ensure_ascii=False).encode("utf-8"), "application/json"
    if fmt_name == "gemini":
        return json.dumps({"error": {"code": status, "message": message, "status": "INVALID_ARGUMENT"}},
                          ensure_ascii=False).encode("utf-8"), "application/json"
    # openai + codex share the OpenAI error shape
    etype = "invalid_request_error"
    if status in (401, 403):
        etype = "authentication_error"
    elif status == 429:
        etype = "rate_limit_error"
    elif status >= 500:
        etype = "api_error"
    return json.dumps({"error": {"message": message, "type": etype, "code": status}},
                      ensure_ascii=False).encode("utf-8"), "application/json"


# ── usage tee ───────────────────────────────────────────────────────────────
class _UsageTee:
    """Wrap a chunk iterator, capture the last ``usage`` seen, record on close."""

    def __init__(self, chunks: Iterator[dict[str, Any]], key: str):
        self._it = chunks
        self._key = key
        self.usage: dict[str, Any] | None = None
        self._recorded = False

    def __iter__(self):
        try:
            for c in self._it:
                if isinstance(c, dict) and c.get("usage"):
                    self.usage = c["usage"]
                yield c
        finally:
            self._record()

    def _record(self):
        if self._recorded:
            return
        self._recorded = True
        try:
            _pool.record(self._key, self.usage)
        except Exception as e:  # noqa: BLE001
            log.debug("usage record failed: %s", e)


# ── connection + retry ──────────────────────────────────────────────────────
def _connect(openai_body: dict[str, Any], fmt_name: str):
    """Open the upstream stream, returning (first_chunk_or_None, rest_iter, key).

    Retries on retryable upstream errors using a different key. When the pool
    is empty or every key failed retryably (401/403/429/5xx), falls back to the
    account's OAuth session (mode="oauth") so chat keeps working without a
    valid ck_ key. Non-retryable request errors (400 etc.) are never retried
    via OAuth - the request itself is bad.
    """
    attempts = max(1, _pool.size)
    last_err: UpstreamError | None = None
    retryable_only = True
    for _ in range(attempts):
        if not _pool.has_keys():
            break
        key = _pool.pick(openai_body)
        try:
            gen = stream_codebuddy(openai_body, key)
            first = next(gen, None)
            return first, gen, key
        except UpstreamError as e:
            _pool.mark_error(key, e.status)
            last_err = e
            if e.status not in _RETRYABLE:
                retryable_only = False
                break
            log.info("upstream %d on key …%s, retrying another key", e.status, key[-6:])
            continue

    if retryable_only:
        try:
            from .auth import SESSIONS
            session = SESSIONS.get()
            gen = stream_codebuddy(openai_body, session.access_token, mode="oauth")
            first = next(gen, None)
            log.info("chat via OAuth session (no usable ck_ key)")
            return first, gen, "oauth"
        except UpstreamError:
            raise
        except GatewayError as e:
            raise last_err or e
    raise last_err or UpstreamError(502, "all keys exhausted")


# ── capability augmentation (vision + web tools) ──────────────────────────
def _apply_augmentation(openai_body: dict[str, Any], key: str) -> dict[str, Any]:
    """Apply vision-description + web-tool injection when enabled.

    Vision uses the *original* key credential (any ck_ key works for the
    describer call). Web tool injection is pure body rewriting.
    """
    if not augment.AUGMENT_ENABLED:
        return openai_body
    body = openai_body
    try:
        body = augment.inject_vision_descriptions(body, key)
    except GatewayError as e:
        # Vision describer failure must not kill the whole request: fall back
        # to the unaugmented body (the main model will just not see images).
        log.warning("augment: vision describe failed, passing through: %s", e.message)
    try:
        body = augment.inject_web_tools(body)
    except Exception as e:  # noqa: BLE001
        log.warning("augment: web tool injection failed: %s", e)
    return body


def _first_tool_call(first_chunk: dict[str, Any] | None, rest: Iterator[dict[str, Any]]):
    """Consume chunks, return (events_list, tool_calls, finish_reason, usage).

    Aggregates the full response so the virtual-tool loop can inspect complete
    tool call arguments before deciding whether to execute + replay.
    """
    from .upstream import iter_events
    chunks = [c for c in chain([first_chunk] if first_chunk is not None else [], rest) if c]
    tool_calls: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = None
    usage = extract_usage_from_chunks(chunks)
    chunk_id = None
    created = None
    model = None
    for ev in iter_events(iter(chunks)):
        kind = ev[0]
        if kind == "meta":
            _, chunk_id, created, model = ev
        elif kind == "text":
            text_parts.append(ev[1])
        elif kind == "reasoning":
            reasoning_parts.append(ev[1])
        elif kind == "tool_start":
            idx, tid, name = ev[1], ev[2], ev[3]
            if idx not in tool_calls:
                tool_calls[idx] = {"id": tid, "type": "function", "function": {"name": name, "arguments": ""}}
                order.append(idx)
            else:
                if tid:
                    tool_calls[idx]["id"] = tid
                if name:
                    tool_calls[idx]["function"]["name"] = name
        elif kind == "tool_delta":
            idx, frag = ev[1], ev[2]
            if idx not in tool_calls:
                tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                order.append(idx)
            tool_calls[idx]["function"]["arguments"] += frag
        elif kind == "finish":
            finish_reason = ev[1]
    calls = [tool_calls[i] for i in order]
    return {
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": calls,
        "finish_reason": finish_reason,
        "usage": usage,
        "id": chunk_id,
        "created": created,
        "model": model,
    }


def _replay_with_virtual_tools(
    openai_body: dict[str, Any], key: str, fmt_name: str
) -> tuple[dict[str, Any] | None, Iterator[dict[str, Any]]]:
    """Run the virtual-tool loop. Returns (aggregated_result, final_chunk_iter).

    When the model calls a virtual tool (web_search/web_fetch), execute it,
    append the assistant tool_calls + tool result to the messages, and re-send
    upstream — up to augment.MAX_LOOPS. The final iteration's chunk iterator
    is returned for normal streaming/aggregation downstream. If the model
    never calls a virtual tool, returns (None, original_iterator).
    """
    body = openai_body
    for loop in range(augment.MAX_LOOPS):
        first, rest, k = _connect(body, fmt_name)
        agg = _first_tool_call(first, rest)
        virtual = [tc for tc in agg["tool_calls"]
                   if augment.is_virtual_tool((tc["function"] or {}).get("name", ""))]
        real = [tc for tc in agg["tool_calls"] if tc not in virtual]
        if not virtual:
            # Nothing to execute: rebuild a chunk iterator from the aggregate
            # and hand it downstream untouched.
            return None, _agg_to_chunks(agg)

        log.info("augment: loop %d, executing %d virtual tool call(s)", loop + 1, len(virtual))

        # Append the assistant message with ALL tool_calls (virtual + real) so
        # the model sees its own reasoning context, then a tool result message
        # per call. Virtual results are real; real tools get a deferral notice
        # (the client will execute them after this stream ends).
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": agg["text"] or None}
        if agg["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"] or f"call_v{loop}_{i}", "type": "function",
                 "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"] or "{}"}}
                for i, tc in enumerate(agg["tool_calls"])
            ]
        messages = list(body.get("messages", [])) + [assistant_msg]
        for i, tc in enumerate(agg["tool_calls"]):
            name = (tc["function"] or {}).get("name", "")
            try:
                arguments = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if augment.is_virtual_tool(name):
                result_text = augment.execute_virtual_tool(name, arguments, key)
            else:
                result_text = (
                    "(该工具由客户端执行，本轮不可用；请基于已有信息继续，"
                    "或先使用 web_search/web_fetch 获取信息)"
                )
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"] or f"call_v{loop}_{i}",
                "content": result_text,
            })
        body = dict(body)
        body["messages"] = messages

    # Loops exhausted: one last upstream call, results as-is.
    first, rest, k = _connect(body, fmt_name)
    return None, chain([first] if first is not None else [], rest)


def _agg_to_chunks(agg: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild raw OpenAI chunks from an aggregate (for passthrough)."""
    chunks: list[dict[str, Any]] = []
    if agg.get("id") is not None or True:
        chunks.append({"id": agg.get("id"), "created": agg.get("created"), "model": agg.get("model"),
                       "choices": [{"index": 0, "delta": {}, "finish_reason": None}]})
    if agg.get("reasoning"):
        chunks.append({"choices": [{"index": 0, "delta": {"reasoning_content": agg["reasoning"]}, "finish_reason": None}]})
    if agg.get("text"):
        chunks.append({"choices": [{"index": 0, "delta": {"content": agg["text"]}, "finish_reason": None}]})
    for i, tc in enumerate(agg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        chunks.append({"choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": i, "id": tc.get("id") or "", "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments", "")},
        }]}, "finish_reason": None}]})
    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": agg.get("finish_reason") or "stop"}]})
    if agg.get("usage"):
        chunks.append({"choices": [], "usage": agg["usage"]})
    return chunks


# ── request handler ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "codebuddy-gateway/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # stderr logging
        log.info("%s - %s", self.address_string(), fmt % args)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _check_client_auth(self) -> bool:
        if not GATEWAY_API_KEY:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == GATEWAY_API_KEY:
            return True
        for h in ("x-api-key", "x-goog-api-key", "x-codebuddy-key"):
            if self.headers.get(h) == GATEWAY_API_KEY:
                return True
        qs = parse_qs(urlparse(self.path).query)
        if qs.get("key", [None])[0] == GATEWAY_API_KEY:
            return True
        return False

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise GatewayError(400, f"invalid JSON body: {e}")

    def _send_json(self, status: int, body: Any, content_type: str = "application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, message: str, fmt_name: str = "openai"):
        body, ctype = _error_body(status, message, fmt_name)
        self._send_json(status, body, ctype)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # SSE has no Content-Length; signal end-of-response via connection close
        # so clients (curl -N, OpenAI SDK) see EOF after [DONE].
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        self.close_connection = True

    # ── dispatch ────────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        route = _route("GET", self.path)
        if route is None:
            self._send_error(404, f"not found: {self.path}", "openai")
            return
        _, _, kind = route
        if kind == "health":
            self._send_json(200, {
                "ok": True,
                "service": "codebuddy-gateway",
                "keys": _pool.size,
                "oauth": SESSIONS.available(),
            })
        elif kind == "get-models":
            self._send_json(200, models_json())
        elif kind == "debug-keys":
            if not self._check_client_auth():
                self._send_error(401, "unauthorized", "openai")
                return
            self._send_json(200, _pool.stats())
        else:
            self._send_error(404, "not found", "openai")

    def do_POST(self):
        if not self._check_client_auth():
            self._send_error(401, "invalid gateway api key", "openai")
            return
        route = _route("POST", self.path)
        if route is None:
            if self._handle_resource():
                return
            self._send_error(404, f"not found: {self.path}", "openai")
            return
        fmt, stream_hint, kind = route
        fmt_name = _format_name(fmt)

        try:
            body = self._read_json()
            openai_body = fmt.parse(body, dict(self.headers), self.path)
        except GatewayError as e:
            self._send_error(e.status, e.message, fmt_name)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("parse error")
            self._send_error(400, f"parse error: {e}", fmt_name)
            return

        client_stream = bool(openai_body.get("stream", stream_hint))

        # ── Capability augmentation (vision descriptions + web tools) ──
        augmented = False
        if augment.AUGMENT_ENABLED:
            try:
                probe_key = _pool.peek() or ""
            except Exception:  # noqa: BLE001
                probe_key = ""
            if probe_key:
                openai_body = _apply_augmentation(openai_body, probe_key)
                augmented = True

        # Connect upstream (with multi-key retry before any bytes are sent).
        try:
            replayed = None
            if augmented and openai_body.get("tools"):
                replayed, chunk_iter = _replay_with_virtual_tools(openai_body, _pool.peek() or "", fmt_name)
                first = None
                rest = chunk_iter
                key = "augmented"
            else:
                first, rest, key = _connect(openai_body, fmt_name)
        except UpstreamError as e:
            msg = "upstream error"
            if isinstance(e.body, dict):
                msg = str(e.body.get("msg") or e.body.get("message") or msg)
            self._send_error(e.status, f"CodeBuddy: {msg} (HTTP {e.status})", fmt_name)
            return
        except GatewayError as e:
            self._send_error(e.status, e.message, fmt_name)
            return

        chunks = chain([first], rest) if first is not None else rest
        tee = _UsageTee(chunks, key if isinstance(key, str) else "augment")

        if client_stream:
            try:
                self._start_sse()
                for chunk_bytes in fmt.emit_stream(tee):
                    self.wfile.write(chunk_bytes)
                    self.wfile.flush()
            except UpstreamError as e:
                # Mid-stream upstream failure - best-effort error event in-format.
                log.warning("mid-stream upstream error: %s", e)
                try:
                    err = _error_body(e.status, f"CodeBuddy stream error (HTTP {e.status})", fmt_name)
                    self.wfile.write(b"data: " + err + b"\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
            except (BrokenPipeError, ConnectionResetError):
                log.info("client disconnected (stream)")
            except Exception as e:  # noqa: BLE001
                log.exception("emit_stream failed")
        else:
            try:
                result = fmt.emit_nonstream(tee, model=openai_body.get("model"))
                self._send_json(200, result)
            except UpstreamError as e:
                msg = "upstream error"
                if isinstance(e.body, dict):
                    msg = str(e.body.get("msg") or e.body.get("message") or msg)
                self._send_error(e.status, f"CodeBuddy: {msg} (HTTP {e.status})", fmt_name)
            except Exception as e:  # noqa: BLE001
                log.exception("emit_nonstream failed")
                self._send_error(502, f"gateway error: {e}", fmt_name)


    # ── resource passthrough (OAuth session, never touches the ck_ pool) ──
    def _handle_resource(self) -> bool:
        """Serve a resource route; return False when the path is not a resource."""
        if resolve_resource(self.path) is None:
            return False
        raw = self._read_body()
        try:
            result = forward_resource(self.path, "POST", dict(self.headers), raw)
        except GatewayError as e:
            self._send_error(e.status, e.message, "openai")
            return True
        status, payload, ctype, streaming = result
        if streaming:
            self._start_sse()
            try:
                for chunk in payload:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.info("client disconnected (resource sse)")
            return True
        self._send_json(status, payload, ctype)
        return True


# ── daily check-in loop ─────────────────────────────────────────────────────
def _start_auto_checkin() -> threading.Thread | None:
    """Claim the daily check-in credit once per UTC day, client-independent.

    Runs inside the gateway (daemon thread), so the daily 100 credits are
    claimed as long as the gateway process is up - no OMP session, no exposed
    port, no cron needed. The endpoint is idempotent ("already checked in" is
    a normal response), so restarts are harmless. Retries hourly on failure.
    Disable with CODEBUDDY_AUTO_CHECKIN=0.
    """
    if os.environ.get("CODEBUDDY_AUTO_CHECKIN", "1").strip().lower() in ("0", "off", "false", "no"):
        log.info("auto check-in disabled (CODEBUDDY_AUTO_CHECKIN=0)")
        return None

    def loop():
        import time
        claimed_day = ""
        while True:
            try:
                today = time.strftime("%Y-%m-%d", time.gmtime())
                if today != claimed_day:
                    try:
                        status, payload, _, _ = forward_resource("/v1/checkin", "POST", {}, b"{}")
                        claimed_day = today  # any upstream answer is conclusive
                        try:
                            data = json.loads(payload.decode("utf-8"))
                            code = data.get("code")
                            msg = data.get("msg") or payload[:120]
                        except Exception:
                            code, msg = None, payload[:120]
                        # code 0 = claimed; 10001 = already claimed today (also success)
                        if status == 200 or code in (0, 10001):
                            log.info("daily check-in (%s): HTTP %d code=%s %s", today, status, code, msg)
                        else:
                            log.warning("daily check-in (%s): HTTP %d code=%s %s", today, status, code, msg)
                    except GatewayError as e:
                        log.warning("daily check-in (%s) failed: %s - retrying in 1h", today, e.message)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("auto check-in loop error")
            time.sleep(3600)

    t = threading.Thread(target=loop, name="daily-checkin", daemon=True)
    t.start()
    return t


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    # Remote binding without a client secret is a footgun: every machine on
    # the network could burn the owner's paid quota. Fail at startup.
    _LOCAL_BINDS = {"127.0.0.1", "localhost", "::1", ""}
    if host not in _LOCAL_BINDS and not GATEWAY_API_KEY:
        raise RuntimeError(
            f"refusing to bind {host!r} without GATEWAY_API_KEY: set GATEWAY_API_KEY "
            "(shared with clients) or keep GATEWAY_HOST=127.0.0.1"
        )
    if not _pool.has_keys():
        log.warning("starting with no API keys - set CODEBUDDY_API_KEY / CODEBUDDY_API_KEYS")
    _start_auto_checkin()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log.info("CodeBuddy gateway listening on http://%s:%d  (keys=%d, default model=hy3)", host, port, _pool.size)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        srv.shutdown()
