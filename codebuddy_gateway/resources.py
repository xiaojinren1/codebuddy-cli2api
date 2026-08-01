"""Resource routes: OAuth-authed passthrough to WorkBuddy/CodeBuddy endpoints.

The chat path (``ck_`` keys) never enters here. Every resource route is served
with the gateway's own OAuth session (see :mod:`.auth`), so clients only need
``GATEWAY_API_KEY`` - they never see a WorkBuddy token.

Convenience aliases with a ``/v1`` prefix are provided; the raw upstream paths
are accepted as-is too (identical behavior).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Iterator

from .auth import SESSIONS, oauth_headers
from .common import CODEBUDDY_RESOURCE_BASE_URL, GatewayError, log

# upstream path -> behavior
_RESOURCES: dict[str, dict[str, Any]] = {
    # ── daily check-in (billing) ─────────────────────────────────────────
    "/v2/billing/meter/daily-checkin":            {"timeout": 30, "kind": "json"},
    "/billing/meter/checkin-status":              {"timeout": 30, "kind": "json"},
    "/v2/billing/meter/checkin-activity-status":  {"timeout": 30, "kind": "json"},
    # ── speech ───────────────────────────────────────────────────────────
    "/agenttool/v1/asr":                          {"timeout": 30, "kind": "multipart"},
    "/agenttool/v1/tts":                          {"timeout": 30, "kind": "sse"},
    # ── tools ────────────────────────────────────────────────────────────
    "/agenttool/v1/tempkey":                      {"timeout": 10, "kind": "json"},
    "/agenttool/v1/search":                       {"timeout": 20, "kind": "json"},
    "/agenttool/v1/webfetch":                     {"timeout": 30, "kind": "json"},
    # ── media ────────────────────────────────────────────────────────────
    "/v2/images/generations":                     {"timeout": 120, "kind": "json"},
    "/v2/images/edits":                           {"timeout": 120, "kind": "json"},
    "/v2/videos/generations":                     {"timeout": 60, "kind": "json"},
    "/v2/videos/tasks":                           {"timeout": 30, "kind": "json"},
}

# gateway-facing alias -> upstream path
_ALIASES: dict[str, str] = {
    "/v1/checkin":           "/v2/billing/meter/daily-checkin",
    "/v1/checkin-status":    "/billing/meter/checkin-status",
    "/v1/checkin-activity":  "/v2/billing/meter/checkin-activity-status",
    "/v1/asr":               "/agenttool/v1/asr",
    "/v1/tts":               "/agenttool/v1/tts",
    "/v1/tempkey":           "/agenttool/v1/tempkey",
    "/v1/search":            "/agenttool/v1/search",
    "/v1/web_search":        "/agenttool/v1/search",
    "/v1/webfetch":          "/agenttool/v1/webfetch",
    "/v1/images/generations": "/v2/images/generations",
    "/v1/images/edits":       "/v2/images/edits",
    "/v1/videos/generations": "/v2/videos/generations",
    "/v1/videos/tasks":       "/v2/videos/tasks",
}
# raw upstream paths are self-aliases
for _u in _RESOURCES:
    _ALIASES.setdefault(_u, _u)


def resolve_resource(path: str):
    """Map a gateway path to ``(upstream_path, kind, timeout)`` or None."""
    p = path.split("?", 1)[0].rstrip("/") or "/"
    up = _ALIASES.get(p)
    if up is None:
        return None
    spec = _RESOURCES[up]
    return up, spec["kind"], spec["timeout"]


def _error_payload(raw: bytes, status: int) -> bytes:
    """Shape an upstream error body as JSON, preserving ``code``/``msg``."""
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:
        pass
    text = raw.decode("utf-8", "replace")[:500]
    return json.dumps({"code": status, "msg": text}, ensure_ascii=False).encode("utf-8")


def _unnest_images(raw: bytes) -> bytes:
    """Normalize ``/v2/images/*`` responses for the ``/v1`` aliases.

    Upstream returns ``{code, msg, data: { data: [ {b64_json|url}, ... ] }}``;
    OpenAI-style clients expect ``data: [ ... ]`` at the top level, so the
    alias view moves the inner list up one level. Raw ``/v2/images/*`` paths
    pass through untouched.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    if not isinstance(data, dict):
        return raw
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("data"), list):
        data["data"] = inner["data"]
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    return raw


def _iter_resp(resp) -> Iterator[bytes]:
    try:
        for raw_line in resp:
            if raw_line:
                yield raw_line
    finally:
        try:
            resp.close()
        except Exception:
            pass


def forward_resource(path: str, method: str, client_headers: dict[str, str], body: bytes):
    """Forward one resource request upstream.

    Returns ``(status, payload, content_type, streaming)``; ``streaming``
    means ``payload`` is a byte iterator (SSE). Raises :class:`GatewayError`
    on unrecoverable client-side failures. A single upstream 401 triggers one
    forced session refresh + retry; a second 401 is surfaced to the client.
    """
    resolved = resolve_resource(path)
    if resolved is None:
        return None
    up_path, kind, timeout = resolved

    session = SESSIONS.get()
    attempts = 0
    while True:
        attempts += 1
        url = CODEBUDDY_RESOURCE_BASE_URL.rstrip("/") + up_path
        headers = oauth_headers(session)
        if kind == "multipart":
            # Keep the client's Content-Type verbatim so the multipart
            # boundary survives the passthrough.
            ctype = client_headers.get("Content-Type", "")
            if ctype:
                headers["Content-Type"] = ctype
        else:
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            if e.code == 401 and attempts == 1:
                log.info("resource 401 - forcing session refresh and retrying once")
                try:
                    session = SESSIONS.force_refresh()
                except GatewayError:
                    pass  # keep old session; the retry will 401 again and surface
                continue
            return e.code, _error_payload(raw, e.code), "application/json", False
        except urllib.error.URLError as e:
            raise GatewayError(502, f"resource upstream unreachable: {e}")

        status = resp.status
        if status >= 300:
            raw = resp.read()
            if status == 401 and attempts == 1:
                log.info("resource 401 - forcing session refresh and retrying once")
                try:
                    session = SESSIONS.force_refresh()
                except GatewayError:
                    pass
                continue
            return status, _error_payload(raw, status), "application/json", False

        if kind == "sse":
            return 200, _iter_resp(resp), "text/event-stream", True
        raw = resp.read()
        if up_path.startswith("/v2/images/") and not path.startswith("/v2/images/"):
            raw = _unnest_images(raw)
        return 200, raw, "application/json", False
