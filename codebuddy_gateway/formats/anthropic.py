"""Anthropic Messages API format (``POST /v1/messages``).

Parses Anthropic requests into OpenAI chat-completions bodies, and emits
CodeBuddy's OpenAI stream back as Anthropic ``message_start`` /
``content_block_*`` / ``message_delta`` / ``message_stop`` events (stream) or a
single ``message`` object (non-stream).
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from ..aggregate import aggregate
from ..common import DEFAULT_MODEL
from ..images import to_openai_image_url
from ..upstream import iter_events

_STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens",
             "stop_sequence": "stop_sequence"}


# ── parse ───────────────────────────────────────────────────────────────────
def _conv_content(content: Any) -> Any:
    """Anthropic content (str|blocks) -> OpenAI content (str|parts)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        bt = block.get("type")
        if bt == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif bt == "image":
            img = to_openai_image_url(block)
            if img:
                parts.append(img)
        elif bt == "tool_use":
            # assistant tool_use -> OpenAI tool_calls (handled at message level)
            parts.append({"__tool_use__": block})
        elif bt == "tool_result":
            parts.append({"__tool_result__": block})
        else:
            parts.append(block)
    return parts


def _flatten_tool_calls(content: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a message's content into plain parts + tool_calls (assistant) or a
    tool message (user with tool_result)."""
    parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for p in content:
        if "__tool_use__" in p:
            tu = p["__tool_use__"]
            tool_calls.append({
                "id": tu.get("id", ""),
                "type": "function",
                "function": {"name": tu.get("name", ""), "arguments": json.dumps(tu.get("input", {}), ensure_ascii=False)},
            })
        elif "__tool_result__" in p:
            tr = p["__tool_result__"]
            tc_content = tr.get("content", "")
            if isinstance(tc_content, list):
                tc_text = " ".join(b.get("text", "") for b in tc_content if isinstance(b, dict) and b.get("type") == "text")
            else:
                tc_text = str(tc_content)
            tool_results.append({"tool_call_id": tr.get("tool_use_id", ""), "content": tc_text})
        else:
            parts.append(p)
    return parts, tool_calls + tool_results


def parse(body: dict[str, Any], headers: dict[str, str], path: str) -> dict[str, Any]:
    out: dict[str, Any] = {"model": body.get("model") or DEFAULT_MODEL}
    messages: list[dict[str, Any]] = []

    # system (str | list of text blocks) -> system message
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        sys_text = " ".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for m in body.get("messages", []):
        role = m.get("role", "user")
        content = _conv_content(m.get("content"))
        if isinstance(content, list):
            parts, tools = _flatten_tool_calls(content)
            if role == "assistant" and tools:
                msg: dict[str, Any] = {"role": "assistant"}
                if parts:
                    msg["content"] = parts
                msg["tool_calls"] = tools
                messages.append(msg)
            elif tools:
                # user tool_result -> OpenAI tool messages
                for tr in tools:
                    messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]})
            else:
                messages.append({"role": role, "content": parts})
        else:
            messages.append({"role": role, "content": content})

    out["messages"] = messages

    # tools: input_schema -> parameters
    if body.get("tools"):
        out["tools"] = [
            {"type": "function",
             "function": {"name": t.get("name", ""), "description": t.get("description", ""),
                          "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in body["tools"] if isinstance(t, dict)
        ]
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    for k in ("temperature", "top_p", "stop"):
        if k in body:
            out[k] = body[k]
    out["stream"] = bool(body.get("stream", False))
    return out


# ── emit ────────────────────────────────────────────────────────────────────
def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _usage_to_anthropic(usage: dict[str, Any] | None) -> tuple[int, int]:
    if not usage:
        return 0, 0
    return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)


def emit_stream(chunks: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    msg_id = "msg_codebuddy"
    model = "codebuddy"
    block_index = -1
    block_open_type: str | None = None  # "text" | "tool"
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"
    started = False

    for ev in iter_events(chunks):
        kind = ev[0]
        if kind == "meta":
            _, cid, _, cmodel = ev
            if cid:
                msg_id = cid
            if cmodel:
                model = cmodel
            yield _sse("message_start", {
                "type": "message_start",
                "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [],
                            "model": model, "stop_reason": None, "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0}},
            })
            started = True
            continue
        if kind == "text":
            if block_open_type != "text":
                if block_open_type is not None:
                    yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
                block_index += 1
                block_open_type = "text"
                yield _sse("content_block_start", {"type": "content_block_start", "index": block_index,
                                                    "content_block": {"type": "text", "text": ""}})
            yield _sse("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                               "delta": {"type": "text_delta", "text": ev[1]}})
        elif kind == "tool_start":
            idx, tid, name = ev[1], ev[2], ev[3]
            if block_open_type is not None:
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
            block_index += 1
            block_open_type = "tool"
            yield _sse("content_block_start", {"type": "content_block_start", "index": block_index,
                                                "content_block": {"type": "tool_use", "id": tid, "name": name, "input": {}}})
        elif kind == "tool_delta":
            if block_open_type == "tool":
                yield _sse("content_block_delta", {"type": "content_block_delta", "index": block_index,
                                                    "delta": {"type": "input_json_delta", "partial_json": ev[2]}})
        elif kind == "finish":
            stop_reason = _STOP_MAP.get(ev[1], "end_turn")
        elif kind == "usage":
            input_tokens, output_tokens = _usage_to_anthropic(ev[1])

    if not started:
        yield _sse("message_start", {
            "type": "message_start",
            "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [],
                        "model": model, "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        })
    if block_open_type is not None:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_index})
    yield _sse("message_delta", {"type": "message_delta",
                                 "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                 "usage": {"output_tokens": output_tokens}})
    yield _sse("message_stop", {"type": "message_stop"})


def emit_nonstream(chunks: Iterator[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    agg = aggregate(chunks, model=model)
    choice = agg["choices"][0]
    msg = choice["message"]
    content: list[dict[str, Any]] = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        try:
            inp = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
        except json.JSONDecodeError:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id", ""), "name": tc["function"]["name"], "input": inp})
    usage = agg.get("usage") or {}
    return {
        "id": agg["id"],
        "type": "message",
        "role": "assistant",
        "model": agg.get("model", model or "codebuddy"),
        "content": content,
        "stop_reason": _STOP_MAP.get(choice["finish_reason"], "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    }
