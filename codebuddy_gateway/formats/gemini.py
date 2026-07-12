"""Google Gemini API format (``/v1beta/models/{m}:generateContent`` and
``:streamGenerateContent``).
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from ..aggregate import aggregate
from ..common import DEFAULT_MODEL
from ..images import to_openai_image_url
from ..upstream import iter_events

_FINISH_MAP = {"stop": "STOP", "tool_calls": "STOP", "length": "MAX_TOKENS",
               "stop_sequence": "STOP"}
_MODEL_RE = re.compile(r"/models/([^/:]+):(generateContent|streamGenerateContent)")


def _extract_model(path: str) -> str:
    m = _MODEL_RE.search(path)
    return m.group(1) if m else DEFAULT_MODEL


def _parts_to_content(parts: list[dict[str, Any]]) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Gemini parts -> (openai_content_parts, tool_calls, tool_results)."""
    out: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        if p.get("text") is not None:
            out.append({"type": "text", "text": p["text"]})
        elif "inlineData" in p or "fileData" in p:
            img = to_openai_image_url(p)
            if img:
                out.append(img)
        elif "functionCall" in p:
            fc = p["functionCall"] or {}
            tool_calls.append({
                "id": f"call_{fc.get('name','x')}",
                "type": "function",
                "function": {"name": fc.get("name", ""),
                             "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False)},
            })
        elif "functionResponse" in p:
            fr = p["functionResponse"] or {}
            tool_results.append({
                "tool_call_id": f"call_{fr.get('name','x')}",
                "content": json.dumps(fr.get("response", {}), ensure_ascii=False),
            })
    return out, tool_calls, tool_results


def parse(body: dict[str, Any], headers: dict[str, str], path: str) -> dict[str, Any]:
    model = _extract_model(path)
    out: dict[str, Any] = {"model": model}
    messages: list[dict[str, Any]] = []

    # systemInstruction
    si = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(si, dict) and si.get("parts"):
        sys_text = " ".join(p.get("text", "") for p in si["parts"] if isinstance(p, dict) and p.get("text") is not None)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for c in body.get("contents", []):
        role = "assistant" if c.get("role") == "model" else c.get("role", "user")
        parts, tcs, trs = _parts_to_content(c.get("parts", []))
        if tcs:
            msg: dict[str, Any] = {"role": "assistant"}
            if parts:
                msg["content"] = parts
            msg["tool_calls"] = tcs
            messages.append(msg)
        elif trs:
            for tr in trs:
                messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]})
        else:
            messages.append({"role": role, "content": parts if parts else ""})

    out["messages"] = messages

    # tools
    tools: list[dict[str, Any]] = []
    for t in body.get("tools", []):
        for fd in (t or {}).get("functionDeclarations", []):
            tools.append({"type": "function",
                          "function": {"name": fd.get("name", ""), "description": fd.get("description", ""),
                                       "parameters": fd.get("parameters", {"type": "object", "properties": {}})}})
    if tools:
        out["tools"] = tools

    gc = body.get("generationConfig") or body.get("generation_config") or {}
    if gc.get("maxOutputTokens") or gc.get("max_output_tokens"):
        out["max_tokens"] = gc.get("maxOutputTokens") or gc.get("max_output_tokens")
    for src, dst in (("temperature", "temperature"), ("topP", "top_p"), ("top_p", "top_p")):
        if gc.get(src) is not None:
            out[dst] = gc[src]
    if gc.get("stopSequences") or gc.get("stop_sequences"):
        out["stop"] = gc.get("stopSequences") or gc.get("stop_sequences")

    out["stream"] = ":streamGenerateContent" in path
    return out


def _usage_to_gemini(usage: dict[str, Any] | None) -> dict[str, int]:
    if not usage:
        return {}
    return {
        "promptTokenCount": int(usage.get("prompt_tokens", 0) or 0),
        "candidatesTokenCount": int(usage.get("completion_tokens", 0) or 0),
        "totalTokenCount": int(usage.get("total_tokens", 0) or 0),
    }


def _chunk(parts: list[dict[str, Any]], *, finish: str | None = None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    cand: dict[str, Any] = {"content": {"parts": parts, "role": "model"}, "index": 0}
    if finish:
        cand["finishReason"] = finish
    out: dict[str, Any] = {"candidates": [cand]}
    if usage:
        out["usageMetadata"] = _usage_to_gemini(usage)
    return out


def emit_stream(chunks: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    tool_buf: dict[int, dict[str, Any]] = {}
    tool_order: list[int] = []
    pending_text = False
    finish_reason = "STOP"
    usage: dict[str, Any] | None = None

    for ev in iter_events(chunks):
        kind = ev[0]
        if kind == "text":
            yield f"data: {json.dumps(_chunk([{'text': ev[1]}]), ensure_ascii=False)}\n\n".encode("utf-8")
            pending_text = True
        elif kind == "tool_start":
            idx, tid, name = ev[1], ev[2], ev[3]
            tool_buf[idx] = {"name": name, "args": ""}
            tool_order.append(idx)
        elif kind == "tool_delta":
            idx, frag = ev[1], ev[2]
            if idx in tool_buf:
                tool_buf[idx]["args"] += frag
        elif kind == "finish":
            finish_reason = _FINISH_MAP.get(ev[1], "STOP")
        elif kind == "usage":
            usage = ev[1]

    # Emit accumulated tool calls (Gemini sends complete functionCall objects)
    parts: list[dict[str, Any]] = []
    for idx in tool_order:
        tb = tool_buf[idx]
        try:
            args = json.loads(tb["args"]) if tb["args"] else {}
        except json.JSONDecodeError:
            args = {"_raw": tb["args"]}
        parts.append({"functionCall": {"name": tb["name"], "args": args}})

    yield f"data: {json.dumps(_chunk(parts or [{'text': ''}], finish=finish_reason, usage=usage), ensure_ascii=False)}\n\n".encode("utf-8")


def emit_nonstream(chunks: Iterator[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    agg = aggregate(chunks, model=model)
    choice = agg["choices"][0]
    msg = choice["message"]
    parts: list[dict[str, Any]] = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"]["arguments"]) if tc["function"]["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        parts.append({"functionCall": {"name": tc["function"]["name"], "args": args}})
    if not parts:
        parts.append({"text": ""})
    return _chunk(parts, finish=_FINISH_MAP.get(choice["finish_reason"], "STOP"), usage=agg.get("usage"))
