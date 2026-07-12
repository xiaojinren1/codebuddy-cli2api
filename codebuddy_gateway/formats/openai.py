"""OpenAI Chat Completions format (passthrough - CodeBuddy speaks this natively)."""

from __future__ import annotations

import json
from typing import Any, Iterator

from ..aggregate import aggregate
from ..common import DEFAULT_MODEL
from ..images import normalize_messages
from ..upstream import stream_codebuddy  # noqa: F401  (re-exported for server)


def parse(body: dict[str, Any], headers: dict[str, str], path: str) -> dict[str, Any]:
    """OpenAI request -> OpenAI request (normalize model + inline images)."""
    out = dict(body)
    if not out.get("model"):
        out["model"] = DEFAULT_MODEL
    if isinstance(out.get("messages"), list):
        out["messages"] = normalize_messages(out["messages"])
    return out


def emit_stream(chunks: Iterator[dict[str, Any]]) -> Iterator[bytes]:
    """Relay OpenAI SSE chunks verbatim, terminate with ``[DONE]``."""
    for chunk in chunks:
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def emit_nonstream(chunks: Iterator[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    return aggregate(chunks, model=model)
