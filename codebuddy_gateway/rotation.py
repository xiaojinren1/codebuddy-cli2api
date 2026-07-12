"""Multi-key, cache-aware rotation for CodeBuddy API keys.

Routing policy (matches the requested behaviour):

  * **Prefix-affinity** - the cacheable prefix (system prompt + first user
    message text) is hashed. Requests with the same prefix stick to the same
    key so CodeBuddy's prompt cache (per-account) stays warm. Verified:
    the second request with the same prefix returns ``prompt_cache_hit_tokens``
    instead of misses.

  * **New-prefix assignment** - when no key owns the prefix yet, pick the
    healthy key with the **highest cache-hit rate** ("按照缓存命中最多的账号"),
    tie-broken by **fewest total requests** ("均匀分配prompt"), then LRU.

  * **Error handling** - 401 disables a key (bad/expired); 429 backs it off
    for a window; 5xx is retried on another key. Prefixes owned by a disabled
    key are released for reassignment.

Thread-safe. Stats exposed via :meth:`KeyPool.stats` (``/debug/keys``).
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Any

from .common import log


def _load_keys() -> list[str]:
    keys: list[str] = []
    multi = os.environ.get("CODEBUDDY_API_KEYS", "").strip()
    if multi:
        for k in multi.replace("\n", ",").split(","):
            k = k.strip()
            if k:
                keys.append(k)
    keyfile = os.environ.get("CODEBUDDY_KEYS_FILE", "").strip()
    if keyfile:
        try:
            with open(keyfile, "r", encoding="utf-8") as f:
                for line in f:
                    k = line.strip()
                    if k and not k.startswith("#"):
                        keys.append(k)
        except OSError as e:
            log.warning("cannot read CODEBUDDY_KEYS_FILE %s: %s", keyfile, e)
    single = os.environ.get("CODEBUDDY_API_KEY", "").strip()
    if single and single not in keys:
        keys.insert(0, single)
    # dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _prefix_hash(openai_body: dict[str, Any]) -> str:
    """Hash the cacheable prefix: system prompt + first user text (≤2KB each)."""
    msgs = openai_body.get("messages") or []
    parts: list[str] = []
    for m in msgs[:3]:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        content = m.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            chunks = []
            for p in content:
                if isinstance(p, dict):
                    if p.get("type") == "text" and p.get("text"):
                        chunks.append(str(p["text"]))
                    elif p.get("text"):
                        chunks.append(str(p["text"]))
            text = " ".join(chunks)
        parts.append(role + ":" + text[:2000])
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


class _KeyState:
    __slots__ = ("key", "requests", "cache_hit", "cache_miss", "errors",
                 "last_used", "disabled", "disabled_until", "prefixes")

    def __init__(self, key: str):
        self.key = key
        self.requests = 0
        self.cache_hit = 0
        self.cache_miss = 0
        self.errors = 0
        self.last_used = 0.0
        self.disabled = False          # permanently (401)
        self.disabled_until = 0.0      # temporary backoff (429)
        self.prefixes: set[str] = set()

    @property
    def hit_rate(self) -> float:
        total = self.cache_hit + self.cache_miss
        if total == 0:
            return 0.0
        return self.cache_hit / total

    def available(self, now: float) -> bool:
        if self.disabled:
            return False
        if self.disabled_until and now < self.disabled_until:
            return False
        return True


class KeyPool:
    def __init__(self, keys: list[str] | None = None):
        self._lock = threading.Lock()
        self._keys: list[_KeyState] = [_KeyState(k) for k in (keys if keys is not None else _load_keys())]
        self._prefix_owner: dict[str, str] = {}
        if not self._keys:
            log.warning("no CodeBuddy API keys configured (set CODEBUDDY_API_KEY or CODEBUDDY_API_KEYS)")

    @property
    def size(self) -> int:
        return len(self._keys)

    def has_keys(self) -> bool:
        return bool(self._keys)

    def pick(self, openai_body: dict[str, Any]) -> str:
        """Choose an API key for this request (cache-affinity + hit-rate biased)."""
        with self._lock:
            if not self._keys:
                raise RuntimeError("no CodeBuddy API keys configured")
            now = time.time()
            ph = _prefix_hash(openai_body)

            # 1. Prefix affinity: if a healthy key owns this prefix, reuse it.
            owner = self._prefix_owner.get(ph)
            if owner:
                for ks in self._keys:
                    if ks.key == owner and ks.available(now):
                        ks.requests += 1
                        ks.last_used = now
                        return ks.key

            # 2. New prefix: pick healthy key with highest hit-rate,
            #    tiebreak fewest requests (even distribution), then LRU.
            candidates = [ks for ks in self._keys if ks.available(now)]
            if not candidates:
                # All temporarily backed off or disabled - force the one
                # recovering soonest so the request still goes out.
                candidates = sorted(self._keys, key=lambda k: k.disabled_until)[:1]
            best = sorted(
                candidates,
                key=lambda k: (-k.hit_rate, k.requests, k.last_used),
            )[0]
            best.requests += 1
            best.last_used = now
            best.prefixes.add(ph)
            self._prefix_owner[ph] = best.key
            return best.key

    def record(self, api_key: str, usage: dict[str, Any] | None) -> None:
        """Update cache stats from the response ``usage`` object."""
        if not usage:
            return
        with self._lock:
            for ks in self._keys:
                if ks.key == api_key:
                    ks.cache_hit += int(usage.get("prompt_cache_hit_tokens", 0) or 0)
                    ks.cache_miss += int(usage.get("prompt_cache_miss_tokens", 0) or 0)
                    return

    def mark_error(self, api_key: str, status: int) -> None:
        with self._lock:
            for ks in self._keys:
                if ks.key != api_key:
                    continue
                ks.errors += 1
                if status == 401:
                    ks.disabled = True
                    self._release_prefixes(ks)
                    log.warning("key …%s disabled (401)", api_key[-6:])
                elif status == 429:
                    ks.disabled_until = time.time() + 30.0
                    log.info("key …%s backed off 30s (429)", api_key[-6:])
                return

    def _release_prefixes(self, ks: _KeyState) -> None:
        for ph in list(ks.prefixes):
            if self._prefix_owner.get(ph) == ks.key:
                del self._prefix_owner[ph]
        ks.prefixes.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            return {
                "keys": [
                    {
                        "key_tail": "…" + ks.key[-6:] if ks.key else "",
                        "requests": ks.requests,
                        "cache_hit_tokens": ks.cache_hit,
                        "cache_miss_tokens": ks.cache_miss,
                        "hit_rate": round(ks.hit_rate, 4),
                        "errors": ks.errors,
                        "disabled": ks.disabled,
                        "backoff_seconds_left": max(0.0, ks.disabled_until - now) if ks.disabled_until else 0.0,
                        "prefixes": len(ks.prefixes),
                        "last_used": int(ks.last_used),
                    }
                    for ks in self._keys
                ],
                "total_keys": len(self._keys),
                "active_keys": sum(1 for ks in self._keys if ks.available(now)),
            }
