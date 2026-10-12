"""Offline tests for the prompt-fingerprint sanitizer (no upstream calls)."""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLAUDE = "You are Claude Code, Anthropic's official CLI for Claude."
ZCODE = "Main branch (you will usually use this for PRs)"


def _fresh_module(store_path: str):
    """Import sanitize with an isolated learned-pattern store."""
    os.environ["CODEBUDDY_SANITIZE_STORE"] = store_path
    os.environ.pop("CODEBUDDY_SANITIZE_PATTERNS", None)
    for mod in [m for m in list(sys.modules) if m.startswith("codebuddy_gateway")]:
        del sys.modules[mod]
    from codebuddy_gateway import sanitize  # noqa: PLC0415
    return sanitize


def test_breaks_known_literals():
    with tempfile.TemporaryDirectory() as d:
        s = _fresh_module(os.path.join(d, "store.json"))
        body = {
            "model": "glm-5.3",
            "messages": [
                {"role": "system", "content": f"prefix\n{CLAUDE}\n{ZCODE}: master"},
                {"role": "user", "content": "hi"},
            ],
        }
        out = s.neutralize_body(body)
        text = out["messages"][0]["content"]
        assert CLAUDE not in text, "verbatim Claude literal must not survive"
        assert ZCODE not in text, "verbatim git-context literal must not survive"
        # Same words, one zero-width space inserted: meaning preserved.
        assert text.replace(s.ZWSP, "") == body["messages"][0]["content"]
        assert out["messages"][1]["content"] == "hi", "clean messages untouched"


def test_handles_content_parts_and_tool_descriptions():
    with tempfile.TemporaryDirectory() as d:
        s = _fresh_module(os.path.join(d, "store.json"))
        body = {
            "messages": [{"role": "system", "content": [{"type": "text", "text": CLAUDE}]}],
            "tools": [{"type": "function",
                       "function": {"name": "Bash", "description": f"x {CLAUDE} y",
                                    "parameters": {"type": "object"}}}],
        }
        out = s.neutralize_body(body)
        assert CLAUDE not in out["messages"][0]["content"][0]["text"]
        assert CLAUDE not in out["tools"][0]["function"]["description"]
        assert out["tools"][0]["function"]["name"] == "Bash", "tool names stay byte-exact"


def test_clean_body_is_returned_unchanged():
    with tempfile.TemporaryDirectory() as d:
        s = _fresh_module(os.path.join(d, "store.json"))
        body = {"messages": [{"role": "user", "content": "hello"}]}
        assert s.neutralize_body(body) is body


def test_block_error_detection():
    with tempfile.TemporaryDirectory() as d:
        s = _fresh_module(os.path.join(d, "store.json"))
        assert s.is_block_error(400, {"code": 11128, "msg": "Illegal API invocation ..."})
        assert s.is_block_error(400, {"msg": "Illegal API invocation from an unapproved channel"})
        assert s.is_block_error(400, "…unapproved channel…")
        assert not s.is_block_error(400, {"msg": "invalid parameter value"})
        assert not s.is_block_error(429, {"code": 11128})


def test_learns_unknown_literal_by_bisect():
    with tempfile.TemporaryDirectory() as d:
        store = os.path.join(d, "store.json")
        s = _fresh_module(store)
        secret = "You are SomeOtherAgent, the official CLI of Someone Else."
        haystack = ("filler text " * 200) + secret + (" more filler" * 200)
        probes = {"n": 0}

        def probe(text: str) -> bool:
            probes["n"] += 1
            return secret in text

        body = {"model": "glm-5.3",
                "messages": [{"role": "system", "content": haystack},
                             {"role": "user", "content": "hi"}]}
        learned = s.learn_blocked_literal(body, probe)
        assert learned and secret in learned, learned
        assert probes["n"] <= 24, "probe budget respected"
        # Persisted, so the next request is fixed pre-flight without probing.
        assert secret in json.dumps(json.load(open(store, encoding="utf-8")))
        out = s.neutralize_body(body)
        assert secret not in out["messages"][0]["content"]


def test_disabled_by_env():
    with tempfile.TemporaryDirectory() as d:
        os.environ["CODEBUDDY_SANITIZE"] = "0"
        try:
            s = _fresh_module(os.path.join(d, "store.json"))
            body = {"messages": [{"role": "system", "content": CLAUDE}]}
            assert s.neutralize_body(body) is body
            assert not s.learning_enabled()
        finally:
            os.environ.pop("CODEBUDDY_SANITIZE", None)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all sanitize tests passed")
