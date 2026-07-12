"""HTTP server: routes OpenAI/Anthropic/Gemini/Codex endpoints to CodeBuddy.

Stdlib ``ThreadingHTTPServer``. Handles per-format auth, multi-key retry on
connection errors, usage capture for cache-aware rotation, and SSE streaming.
"""

from __future__ import annotations

import json
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
from .formats import anthropic, codex, gemini, openai
from .rotation import KeyPool
from .upstream import stream_codebuddy

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

    Retries on retryable upstream errors using a different key. Raises
    UpstreamError if all keys fail or the error is non-retryable.
    """
    attempts = max(1, _pool.size)
    last_err: UpstreamError | None = None
    for _ in range(attempts):
        if not _pool.has_keys():
            raise GatewayError(500, "no CodeBuddy API keys configured (set CODEBUDDY_API_KEY)")
        key = _pool.pick(openai_body)
        try:
            gen = stream_codebuddy(openai_body, key)
            first = next(gen, None)
            return first, gen, key
        except UpstreamError as e:
            _pool.mark_error(key, e.status)
            last_err = e
            if e.status not in _RETRYABLE:
                raise
            log.info("upstream %d on key …%s, retrying another key", e.status, key[-6:])
            continue
    raise last_err or UpstreamError(502, "all keys exhausted")


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
            self._send_json(200, {"ok": True, "service": "codebuddy-gateway", "keys": _pool.size})
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

        # Connect upstream (with multi-key retry before any bytes are sent).
        try:
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
        tee = _UsageTee(chunks, key)

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


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    if not _pool.has_keys():
        log.warning("starting with no API keys - set CODEBUDDY_API_KEY / CODEBUDDY_API_KEYS")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    log.info("CodeBuddy gateway listening on http://%s:%d  (keys=%d, default model=hy3)", host, port, _pool.size)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        srv.shutdown()
