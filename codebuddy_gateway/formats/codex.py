"""OpenAI Responses API format (``POST /v1/responses``) - the "Codex" wire shape.

Parses Responses requests (``input``/``instructions``/``tools``) into OpenAI
chat-completions bodies, and emits CodeBuddy's stream as Responses SSE events
(``response.created`` / ``response.output_text.delta`` /
``response.function_call_arguments.delta`` / ``response.completed``) or a single
``response`` object.
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from ..aggregate import aggregate
from ..common import DEFAULT_MODEL
from ..images import to_openai_image_url
from ..upstream import iter_events


def _resp_id() -> str:
    return f"resp_codebuddy_{int(time.time() * 1000)}"


def _input_to_messages(input_val: Any, instructions: str | None) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(input_val, str):
        messages.append({"role": "user", "content": input_val})
        return messages
    if not isinstance(input_val, list):
        return messages

    for item in input_val:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        role = item.get("role", "user")

        if itype == "function_call":
            messages.append({"role": "assistant", "tool_calls": [{
                "id": item.get("call_id", ""), "type": "function",
                "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "")},
            }]})
        elif itype == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                             "content": item.get("output", "")})
        elif itype == "message" or "content" in item:
            content = item.get("content")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                parts: list[dict[str, Any]] = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": c.get("text", "")})
                    elif ct == "input_image":
                        img = to_openai_image_url(c)
                        if img:
                            parts.append(img)
                messages.append({"role": role, "content": parts if parts else ""})
        elif "text" in item:
            messages.append({"role": role, "content": item["text"]})
    return messages


def parse(body: dict[str, Any], headers: dict[str, str], path: str) -> dict[str, Any]:
    out: dict[str, Any] = {"model": body.get("model") or DEFAULT_MODEL}
    out["messages"] = _input_to_messages(body.get("input"), body.get("instructions"))

    tools: list[dict[str, Any]] = []
    for t in body.get("tools", []):
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            # Responses tools are flat: {name, description, parameters}
            fn = t.get("function") or t  # accept both shapes
            tools.append({"type": "function", "function": {
                "name": fn.get("name", ""), "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            }})
    if tools:
        out["tools"] = tools

    if body.get("max_output_tokens"):
        out["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p"):
        if k in body:
            out[k] = body[k]
    out["stream"] = bool(body.get("stream", False))
    return out


# ── emit ────────────────────────────────────────────────────────────────────
def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _usage_to_resp(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def emit_stream(chunks: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    resp_id = _resp_id()
    model = "codebuddy"
    msg_item_id = "msg_item"
    text_started = False
    tool_items: dict[int, str] = {}
    tool_args: dict[int, str] = {}
    output_index = 0
    finish_reason = "stop"
    usage: dict[str, Any] | None = None
    full_text: list[str] = []

    def response_skeleton(status: str) -> dict[str, Any]:
        return {"id": resp_id, "object": "response", "created_at": int(time.time()),
                "model": model, "status": status, "output": []}

    yield _sse("response.created", {"type": "response.created", "response": response_skeleton("in_progress")})

    for ev in iter_events(chunks):
        kind = ev[0]
        if kind == "meta":
            _, _, _, cmodel = ev
            if cmodel:
                model = cmodel
            continue
        if kind == "text":
            if not text_started:
                yield _sse("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": {"id": msg_item_id, "type": "message", "role": "assistant",
                             "status": "in_progress", "content": []},
                })
                yield _sse("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": msg_item_id,
                    "output_index": output_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                })
                text_started = True
            full_text.append(ev[1])
            yield _sse("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": msg_item_id,
                "output_index": output_index, "content_index": 0, "delta": ev[1],
            })
        elif kind == "tool_start":
            idx, tid, name = ev[1], ev[2], ev[3]
            if text_started:
                yield _sse("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": msg_item_id,
                    "output_index": output_index, "content_index": 0, "text": "".join(full_text),
                })
                yield _sse("response.content_part.done", {
                    "type": "response.content_part.done", "item_id": msg_item_id,
                    "output_index": output_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "".join(full_text), "annotations": []},
                })
                yield _sse("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": output_index,
                    "item": {"id": msg_item_id, "type": "message", "role": "assistant", "status": "completed",
                             "content": [{"type": "output_text", "text": "".join(full_text), "annotations": []}]},
                })
                output_index += 1
                text_started = False
            fc_id = f"fc_{idx}"
            tool_items[idx] = fc_id
            tool_args[idx] = ""
            yield _sse("response.output_item.added", {
                "type": "response.output_item.added", "output_index": output_index,
                "item": {"id": fc_id, "type": "function_call", "status": "in_progress",
                         "call_id": tid, "name": name, "arguments": ""},
            })
        elif kind == "tool_delta":
            idx, frag = ev[1], ev[2]
            if idx in tool_items:
                tool_args[idx] += frag
                yield _sse("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": tool_items[idx], "output_index": output_index, "delta": frag,
                })
        elif kind == "finish":
            finish_reason = ev[1]
        elif kind == "usage":
            usage = ev[1]

    # Close any open tool items
    for idx, fc_id in tool_items.items():
        yield _sse("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done", "item_id": fc_id,
            "output_index": output_index, "arguments": tool_args[idx],
        })
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done", "output_index": output_index,
            "item": {"id": fc_id, "type": "function_call", "status": "completed",
                     "call_id": "", "name": "", "arguments": tool_args[idx]},
        })

    completed = response_skeleton("completed")
    completed["usage"] = _usage_to_resp(usage)
    yield _sse("response.completed", {"type": "response.completed", "response": completed})


def emit_nonstream(chunks: Iterator[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    agg = aggregate(chunks, model=model)
    choice = agg["choices"][0]
    msg = choice["message"]
    output: list[dict[str, Any]] = []
    if msg.get("content"):
        output.append({"id": "msg_item", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
    for i, tc in enumerate(msg.get("tool_calls") or []):
        output.append({"id": f"fc_{i}", "type": "function_call", "status": "completed",
                       "call_id": tc.get("id", ""), "name": tc["function"]["name"],
                       "arguments": tc["function"]["arguments"]})
    return {
        "id": agg["id"],
        "object": "response",
        "created_at": int(time.time()),
        "model": agg.get("model", model or "codebuddy"),
        "status": "completed",
        "output": output,
        "usage": _usage_to_resp(agg.get("usage")),
    }
