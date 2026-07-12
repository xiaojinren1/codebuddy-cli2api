"""Format handlers. Each module exposes:

  parse(body: dict, headers: dict, path: str) -> dict   # -> OpenAI chat body
  emit_stream(chunks: Iterator[dict]) -> Iterator[bytes] # SSE bytes for the format
  emit_nonstream(chunks: Iterator[dict], *, model) -> dict  # single response object
"""

from . import anthropic, codex, gemini, openai

__all__ = ["openai", "anthropic", "gemini", "codex"]
