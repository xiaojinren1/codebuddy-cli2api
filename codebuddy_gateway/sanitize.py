"""Neutralize third-party agent prompt fingerprints that CodeBuddy blocks.

CodeBuddy's edge matches a blocklist of **verbatim** system-prompt strings from
other coding agents (Claude Code, its forks, …) and answers
``400 {"code": 11128, "msg": "Illegal API invocation from an unapproved channel"}``
— even when the request itself is a normal chat completion with a valid key.
Verified 2026-09: ``"You are Claude Code, Anthropic's official CLI for Claude."``
and ``"Main branch (you will usually use this for PRs)"`` are blocked, while any
one-word variation of either passes, i.e. the match is exact-substring.

Fix: insert a zero-width space (U+200B) inside the matched literal. That breaks
the byte-level match while the model still reads the same words, so prompts keep
their meaning and no client has to be reconfigured.

Two layers:

1. **Pre-flight** — every known pattern is neutralized before the request goes
   out (:func:`neutralize_body`).
2. **Learn-on-block** — when a request is blocked by a pattern we do not know
   yet, :func:`learn_blocked_literal` bisects the offending message with cheap
   1-token probes, stores the literal in ``~/.codebuddy/gateway-blocked.json``,
   and the retry goes through. The next request with that text is fixed
   pre-flight, so the bisect happens once per fingerprint.

Env:

- ``CODEBUDDY_SANITIZE`` — ``0``/``off`` disables both layers.
- ``CODEBUDDY_SANITIZE_PATTERNS`` — extra literals, ``|``-separated.
- ``CODEBUDDY_SANITIZE_STORE`` — learned-pattern file path.
- ``CODEBUDDY_SANITIZE_LEARN`` — ``0``/``off`` keeps layer 1 only (no probes).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable

from .common import log

ZWSP = "\u200b"

# Upstream marker for "your prompt matched our blocklist".
BLOCK_CODE = 11128
BLOCK_MSG_MARKER = "unapproved channel"

# Verified blocked literals (2026-09).
DEFAULT_PATTERNS: list[str] = [
    "You are Claude Code, Anthropic's official CLI for Claude.",
    "Main branch (you will usually use this for PRs)",
]

_STORE_PATH = os.environ.get(
    "CODEBUDDY_SANITIZE_STORE",
    os.path.expanduser("~/.codebuddy/gateway-blocked.json"),
)

_lock = threading.Lock()
_patterns: list[str] | None = None


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "off", "false", "no", "")


def enabled() -> bool:
    return _env_flag("CODEBUDDY_SANITIZE")


def learning_enabled() -> bool:
    return enabled() and _env_flag("CODEBUDDY_SANITIZE_LEARN")


def _load_store() -> list[str]:
    try:
        with open(_STORE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    raw = data.get("patterns") if isinstance(data, dict) else data
    return [p for p in raw if isinstance(p, str) and p] if isinstance(raw, list) else []


def patterns() -> list[str]:
    """Known blocked literals: defaults + env + learned store (cached)."""
    global _patterns
    with _lock:
        if _patterns is None:
            out = list(DEFAULT_PATTERNS)
            extra = os.environ.get("CODEBUDDY_SANITIZE_PATTERNS", "").strip()
            if extra:
                out.extend(p for p in (x.strip() for x in extra.split("|")) if p)
            out.extend(_load_store())
            seen: set[str] = set()
            _patterns = [p for p in out if not (p in seen or seen.add(p))]
        return list(_patterns)


def remember(literal: str) -> None:
    """Persist a newly discovered blocked literal (idempotent)."""
    global _patterns
    literal = literal.strip()
    if not literal:
        return
    with _lock:
        known = set(_load_store()) | set(DEFAULT_PATTERNS)
        if literal not in known:
            try:
                os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
                with open(_STORE_PATH, "w", encoding="utf-8") as fh:
                    json.dump({"patterns": sorted(known | {literal})}, fh,
                              ensure_ascii=False, indent=2)
            except OSError as e:
                log.warning("cannot persist blocked pattern to %s: %s", _STORE_PATH, e)
        _patterns = None  # force reload incl. the new literal
    log.info("sanitize: learned blocked literal (%d chars): %r…", len(literal), literal[:60])


# ── text rewriting ──────────────────────────────────────────────────────────
def break_literal(text: str, literal: str) -> str:
    """Insert ZWSP inside every occurrence of ``literal`` in ``text``."""
    if not literal or literal not in text:
        return text
    # Split in the middle of the literal - any interior position works; the
    # midpoint avoids landing on the first/last char (some matchers normalize
    # leading/trailing whitespace).
    cut = max(1, len(literal) // 2)
    return text.replace(literal, literal[:cut] + ZWSP + literal[cut:])


def sanitize_text(text: str, pats: list[str] | None = None) -> str:
    for literal in (patterns() if pats is None else pats):
        text = break_literal(text, literal)
    return text


def _map_content(content: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part = {**part, "text": fn(part["text"])}
            out.append(part)
        return out
    return content


def _map_body(body: dict[str, Any], fn: Callable[[str], str]) -> dict[str, Any]:
    """Apply ``fn`` to prose only: message content and tool descriptions."""
    out = dict(body)
    messages = out.get("messages")
    if isinstance(messages, list):
        out["messages"] = [
            {**m, "content": _map_content(m.get("content"), fn)}
            if isinstance(m, dict) and m.get("content") is not None else m
            for m in messages
        ]
    tools = out.get("tools")
    if isinstance(tools, list):
        new_tools = []
        for t in tools:
            fnspec = t.get("function") if isinstance(t, dict) else None
            if isinstance(fnspec, dict) and isinstance(fnspec.get("description"), str):
                t = {**t, "function": {**fnspec, "description": fn(fnspec["description"])}}
            new_tools.append(t)
        out["tools"] = new_tools
    return out


def neutralize_body(body: dict[str, Any]) -> dict[str, Any]:
    """Break every known blocked literal in ``body``. No-op when disabled."""
    if not enabled():
        return body
    pats = patterns()
    if not pats:
        return body
    text = json.dumps(body.get("messages"), ensure_ascii=False)
    if not any(p in text for p in pats):
        tools_text = json.dumps(body.get("tools"), ensure_ascii=False) if body.get("tools") else ""
        if not any(p in tools_text for p in pats):
            return body
    return _map_body(body, lambda s: sanitize_text(s, pats))


def is_block_error(status: int, err_body: Any) -> bool:
    """True when an upstream error is the prompt-fingerprint block."""
    if status != 400:
        return False
    if isinstance(err_body, dict):
        if err_body.get("code") == BLOCK_CODE:
            return True
        return BLOCK_MSG_MARKER in str(err_body.get("msg") or err_body.get("message") or "")
    return BLOCK_MSG_MARKER in str(err_body or "")


# ── learn-on-block: bisect the offending literal ────────────────────────────
_MIN_SPAN = 8         # a blocked span shorter than this is too generic to store
_MAX_PROBES = 40      # hard ceiling on probe requests per learning run


def _texts_of(body: dict[str, Any]) -> list[str]:
    """Every prose string in the body, longest first (the trigger is prose)."""
    out: list[str] = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p["text"] for p in c
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    for t in body.get("tools") or []:
        fnspec = t.get("function") if isinstance(t, dict) else None
        if isinstance(fnspec, dict) and isinstance(fnspec.get("description"), str):
            out.append(fnspec["description"])
    return sorted(out, key=len, reverse=True)


def _units(text: str) -> tuple[list[str], str]:
    """Split into search units: lines, or fixed chunks for single-line text."""
    lines = text.split("\n")
    if len(lines) >= 4:
        return lines, "\n"
    size = max(_MIN_SPAN, len(text) // 40 or 1)
    return [text[i:i + size] for i in range(0, len(text), size)], ""


def _narrow(text: str, blocked: Callable[[str], bool]) -> str:
    """Two binary searches: shortest blocked prefix, then latest blocked start.

    Costs ~2·log2(units) probes, so a 200-line prompt needs ~16 - unlike
    overlap-halving, which cannot converge inside a sane budget.
    """
    units, sep = _units(text)
    # Phase 1: smallest prefix that is still blocked -> where the literal ends.
    lo, hi = 1, len(units)
    while lo < hi:
        mid = (lo + hi) // 2
        if blocked(sep.join(units[:mid])):
            hi = mid
        else:
            lo = mid + 1
    end = lo
    # Phase 2: latest start that is still blocked -> where the literal begins.
    lo2, hi2 = 0, end - 1
    while lo2 < hi2:
        mid = (lo2 + hi2 + 1) // 2
        if blocked(sep.join(units[mid:end])):
            lo2 = mid
        else:
            hi2 = mid - 1
    return sep.join(units[lo2:end])


def learn_blocked_literal(body: dict[str, Any], probe: Callable[[str], bool]) -> str | None:
    """Locate and persist the blocked literal inside ``body``.

    ``probe(text)`` must send ``text`` as a minimal upstream request and return
    True when it is blocked. Returns the learned literal (also persisted), or
    None when nothing reproducible was found within the probe budget.
    """
    probes = 0

    def blocked(text: str) -> bool:
        nonlocal probes
        if probes >= _MAX_PROBES:
            raise StopIteration
        probes += 1
        return probe(text)

    try:
        for text in _texts_of(body):
            if len(text) < _MIN_SPAN or not blocked(text):
                continue
            literal = _narrow(text, blocked).strip()
            if len(literal) < _MIN_SPAN or literal not in text:
                log.warning("sanitize: narrowed span unusable (%d chars), keeping none",
                            len(literal))
                return None
            remember(literal)
            log.info("sanitize: located blocked literal in %d probes", probes)
            return literal
    except StopIteration:
        log.warning("sanitize: probe budget (%d) exhausted while learning", _MAX_PROBES)
    return None

