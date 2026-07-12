"""Aggregate a CodeBuddy OpenAI SSE stream into a complete ``chat.completion``.

Used for every non-stream client request (CodeBuddy is stream-only, so the
gateway always streams upstream and aggregates when the client asked for a
single object).
"""

from __future__ import annotations

from typing import Any, Iterator

from .upstream import iter_events


def aggregate(chunks: Iterator[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    """Consume raw chunks, return a complete OpenAI ``chat.completion`` object."""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    tool_order: list[int] = []
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    chunk_id: str | None = None
    created: int | None = None
    chunk_model = model

    for ev in iter_events(chunks):
        kind = ev[0]
        if kind == "meta":
            _, chunk_id, created, chunk_model = ev
            if model:
                chunk_model = model
            continue
        if kind == "text":
            content_parts.append(ev[1])
        elif kind == "reasoning":
            reasoning_parts.append(ev[1])
        elif kind == "tool_start":
            idx, tid, name = ev[1], ev[2], ev[3]
            if idx not in tool_calls:
                tool_calls[idx] = {"id": tid, "type": "function", "function": {"name": name, "arguments": ""}}
                tool_order.append(idx)
            else:
                if tid:
                    tool_calls[idx]["id"] = tid
                if name:
                    tool_calls[idx]["function"]["name"] = name
        elif kind == "tool_delta":
            idx, frag = ev[1], ev[2]
            if idx not in tool_calls:
                tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                tool_order.append(idx)
            tool_calls[idx]["function"]["arguments"] += frag
        elif kind == "finish":
            finish_reason = ev[1]
        elif kind == "usage":
            usage = ev[1]

    content = "".join(content_parts)
    message: dict[str, Any] = {"role": "assistant", "content": content if content else None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_order:
        message["tool_calls"] = [tool_calls[i] for i in tool_order]

    if finish_reason is None:
        finish_reason = "tool_calls" if tool_order else "stop"

    return {
        "id": chunk_id or "chatcmpl-codebuddy",
        "object": "chat.completion",
        "created": created or 0,
        "model": chunk_model or "codebuddy",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
        "usage": usage or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }
