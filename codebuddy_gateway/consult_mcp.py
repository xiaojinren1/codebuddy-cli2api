"""Minimal stdio MCP server for consulting CodeBuddy models.

The server deliberately exposes one read-only tool. It can ask a GLM,
DeepSeek, Kimi, or another CodeBuddy model for analysis, but it cannot read
the workspace or execute commands. The native Codex agent remains responsible
for all tool calls and side effects.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


GATEWAY_URL = os.environ.get("CODEBUDDY_GATEWAY_URL", "http://127.0.0.1:8787").rstrip("/")
GATEWAY_KEY = os.environ.get("CODEBUDDY_GATEWAY_KEY", "")
DEFAULT_MODEL = os.environ.get("CODEBUDDY_CONSULT_MODEL", "glm-5.3")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if GATEWAY_KEY:
        headers["Authorization"] = f"Bearer {GATEWAY_KEY}"
        headers["X-API-Key"] = GATEWAY_KEY
    return headers


def _consult(args: dict[str, Any]) -> str:
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("prompt is required")
    model = str(args.get("model") or DEFAULT_MODEL)
    messages: list[dict[str, str]] = []
    system = str(args.get("system", "")).strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    effort = args.get("reasoning_effort")
    if effort is not None:
        body["reasoning_effort"] = effort

    request = urllib.request.Request(
        GATEWAY_URL + "/v1/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=_headers(), method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"CodeBuddy gateway HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CodeBuddy gateway unreachable: {exc}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"CodeBuddy returned no choices: {json.dumps(payload)[:500]}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    result = str(content or "")
    if reasoning:
        result = f"[CodeBuddy reasoning]\n{reasoning}\n\n{result}".strip()
    return result or "(CodeBuddy returned an empty response)"


def _result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _reply(request_id: Any, result: dict[str, Any] | None = None,
           error: dict[str, Any] | None = None) -> None:
    response: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result or {}
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return
    if method == "initialize":
        params = message.get("params") or {}
        version = params.get("protocolVersion", "2024-11-05")
        _reply(request_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codebuddy-consult", "version": "1.0.0"},
        })
        return
    if method == "ping":
        _reply(request_id, {})
        return
    if method == "tools/list":
        _reply(request_id, {"tools": [{
            "name": "codebuddy_consult",
            "description": (
                "Ask a CodeBuddy model for analysis or a proposed approach. "
                "This tool is read-only: it cannot inspect files or execute commands."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Question for the auxiliary model"},
                    "model": {"type": "string", "description": "CodeBuddy model id, default glm-5.3"},
                    "system": {"type": "string", "description": "Optional analysis instructions"},
                    "reasoning_effort": {"type": "string", "enum": ["low", "high"]},
                },
                "required": ["prompt"],
            },
        }]})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != "codebuddy_consult":
            _reply(request_id, error={"code": -32602, "message": "unknown tool"})
            return
        try:
            _reply(request_id, _result(_consult(params.get("arguments") or {})))
        except (ValueError, RuntimeError) as exc:
            _reply(request_id, _result(f"[codebuddy_consult error] {exc}"))
        return
    if request_id is not None:
        _reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                _handle(message)
        except json.JSONDecodeError as exc:
            _reply(None, error={"code": -32700, "message": f"invalid JSON: {exc}"})


if __name__ == "__main__":
    main()
