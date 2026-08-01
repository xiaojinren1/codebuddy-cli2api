"""OAuth session manager for WorkBuddy/CodeBuddy resource endpoints.

The gateway's chat path uses ``ck_`` keys (KeyPool, mode="key") and is
untouched. The free/paid resource endpoints (search, speech, images, videos,
daily check-in) require the account's OAuth ``accessToken``, which this module
owns on behalf of every client.

The session is *reused across machines*: a single gateway host holds the
session file (WorkBuddy desktop or CodeBuddy CLI login) and refreshes it
automatically; remote clients only ever present ``GATEWAY_API_KEY``.

Session file schema (same for WorkBuddy desktop and CodeBuddy CLI):

    { "auth":    { "accessToken", "refreshToken", "tokenType": "Bearer",
                    "expiresAt" (ms, 0 = non-expiring), "domain", ... },
      "account": { "uid", "enterpriseId", "nickname", ... },
      "accounts": [...] }
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .common import DEFAULT_BASE_URL, GatewayError, log, upstream_headers

# Priority list of session files. CODEBUDDY_AUTH_FILE overrides everything.
_DEFAULT_AUTH_FILES = [
    os.path.expanduser(
        "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
    ),
    os.path.expanduser("~/.codebuddy/auth/Tencent-Cloud.coding-copilot.info"),
]

REFRESH_WINDOW_S = 60  # refresh when closer than this to expiry
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
REFRESH_TIMEOUT = 30


@dataclass
class Session:
    access_token: str
    refresh_token: str = ""
    expires_at_ms: int = 0  # 0 = non-expiring token
    uid: str = ""
    enterprise_id: str = ""
    domain: str = ""
    source: str = ""
    auth: dict[str, Any] = field(default_factory=dict)
    account: dict[str, Any] = field(default_factory=dict)

    def needs_refresh(self) -> bool:
        if self.expires_at_ms <= 0:
            return False
        return time.time() * 1000 + REFRESH_WINDOW_S * 1000 >= self.expires_at_ms


# ── discovery ───────────────────────────────────────────────────────────────
def auth_file() -> str | None:
    """Locate a session file, or None."""
    env = os.environ.get("CODEBUDDY_AUTH_FILE", "").strip()
    if env:
        return env if os.path.isfile(env) else None
    for p in _DEFAULT_AUTH_FILES:
        if os.path.isfile(p):
            return p
    return None


def session_available() -> bool:
    """True when a session file with an access token exists (no network)."""
    p = auth_file()
    if not p:
        return False
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool((data.get("auth") or {}).get("accessToken"))
    except Exception:
        return False


def load_session(path: str) -> Session:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise GatewayError(401, f"cannot read auth file {path}: {e}")
    auth = data.get("auth") or {}
    account = data.get("account") or {}
    token = auth.get("accessToken") or ""
    if not token:
        raise GatewayError(
            401, f"no accessToken in {path} - please login to CodeBuddy/WorkBuddy first"
        )
    return Session(
        access_token=token,
        refresh_token=auth.get("refreshToken") or "",
        expires_at_ms=int(auth.get("expiresAt") or 0),
        uid=str(account.get("uid") or ""),
        enterprise_id=str(account.get("enterpriseId") or ""),
        domain=str(auth.get("domain") or ""),
        source=path,
        auth=auth,
        account=account,
    )


# ── refresh ─────────────────────────────────────────────────────────────────
def _auth_endpoint(s: Session) -> str:
    domain = (s.domain or DEFAULT_BASE_URL).strip()
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    return domain.rstrip("/")


def _write_back(s: Session, changed: dict[str, Any]) -> None:
    """Persist refreshed token fields into the session file (preserving the rest)."""
    if not s.source:
        return
    try:
        with open(s.source, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    (data.setdefault("auth", {})).update({k: v for k, v in changed.items() if v is not None})
    try:
        with open(s.source, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("failed to persist refreshed session: %s", e)


def refresh_session(s: Session) -> Session:
    """Exchange refreshToken for a fresh accessToken; write back on success.

    The refresh request carries BOTH the old Bearer token and
    ``X-Refresh-Token`` (observed in the CLI/desktop client code). If the
    refresh response contains no new token (some setups return a non-2xx with
    an empty body), the old token is kept so the caller can try it once.
    """
    if not s.refresh_token:
        raise GatewayError(
            401, f"session {s.source} has no refreshToken - please re-login"
        )
    url = _auth_endpoint(s) + REFRESH_PATH
    headers = {
        "Authorization": f"Bearer {s.access_token}",
        "X-Refresh-Token": s.refresh_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT)
        raw = resp.read()
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            raise GatewayError(
                401,
                f"workbuddy session refresh failed (HTTP {e.code}) - "
                f"please re-login (delete/refresh {s.source})",
            )
        raise GatewayError(502, f"session refresh upstream error: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise GatewayError(502, f"session refresh network error: {e}")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        data = {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    new_token = data.get("accessToken") or inner.get("accessToken") or ""
    if not new_token:
        log.warning("refresh returned no accessToken; falling back to cached token")
        return s

    s.access_token = new_token
    if data.get("refreshToken") or inner.get("refreshToken"):
        s.refresh_token = data.get("refreshToken") or inner.get("refreshToken")
    expires = data.get("expiresAt") or inner.get("expiresAt")
    if expires:
        s.expires_at_ms = int(expires)
    elif data.get("expiresIn") or inner.get("expiresIn"):
        s.expires_at_ms = int(time.time() * 1000) + int(
            data.get("expiresIn") or inner.get("expiresIn")
        ) * 1000
    if data.get("domain") or inner.get("domain"):
        s.domain = data.get("domain") or inner.get("domain")

    changed = {
        "accessToken": s.access_token,
        "refreshToken": s.refresh_token,
        "expiresAt": s.expires_at_ms,
        "lastRefreshTime": int(time.time() * 1000),
    }
    if data.get("expiresIn") or inner.get("expiresIn"):
        changed["expiresIn"] = int(data.get("expiresIn") or inner.get("expiresIn"))
    _write_back(s, changed)
    log.info("refreshed workbuddy session (expiresAt=%d)", s.expires_at_ms)
    return s


# ── manager ─────────────────────────────────────────────────────────────────
class SessionManager:
    """Cached OAuth session with lazy, once-per-window refresh."""

    def __init__(self) -> None:
        self._session: Session | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return session_available()

    def get(self) -> Session:
        with self._lock:
            if self._session is not None and not self._session.needs_refresh():
                return self._session
            if self._session is None:
                p = auth_file()
                if not p:
                    raise GatewayError(
                        401,
                        "no WorkBuddy/CodeBuddy session found - set CODEBUDDY_AUTH_FILE "
                        "or run `python3 -m codebuddy_gateway --login`",
                    )
                self._session = load_session(p)
            if self._session.needs_refresh():
                try:
                    self._session = refresh_session(self._session)
                except GatewayError as e:
                    if not self._session.access_token:
                        raise
                    log.warning("session refresh failed (%s); using cached token", e.message)
            return self._session

    def force_refresh(self) -> Session:
        """Refresh unconditionally (used after an upstream 401)."""
        with self._lock:
            if self._session is None:
                return self.get()
            self._session = refresh_session(self._session)
            return self._session


SESSIONS = SessionManager()


# ── resource headers ────────────────────────────────────────────────────────
def oauth_headers(session: Session, accept: str = "application/json") -> dict[str, str]:
    """Headers for resource calls: product fingerprint + enterprise trio.

    Mirrors upstream_headers(mode="oauth") plus the headers the desktop client
    attaches via its authentication interceptor (X-User-Id / X-Enterprise-Id /
    X-Tenant-Id / X-Domain, each only when the account has a value).
    """
    h = upstream_headers(session.access_token, mode="oauth", accept=accept)
    h["X-Product-Version"] = "2.118.2"
    h["X-Request-From"] = "cli"
    if session.uid:
        h["X-User-Id"] = session.uid
    if session.enterprise_id:
        h["X-Enterprise-Id"] = session.enterprise_id
        h["X-Tenant-Id"] = session.enterprise_id
    if session.domain:
        h["X-Domain"] = session.domain
    return h


# ── interactive login fallback ──────────────────────────────────────────────
def login_interactive(save_path: str | None = None, endpoint: str | None = None) -> Session:
    """External-link login flow; prints authUrl, polls until authorized.

    Used only when no session file exists (first deployment on a fresh host).
    """
    base = (endpoint or DEFAULT_BASE_URL).rstrip("/")
    state_url = base + "/v2/plugin/auth/state?platform=CLI"
    req = urllib.request.Request(state_url, data=b"{}", method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise GatewayError(502, f"login state request failed: {e}")
    state = data.get("state") or (data.get("data") or {}).get("state")
    auth_url = data.get("authUrl") or (data.get("data") or {}).get("authUrl")
    if not state or not auth_url:
        raise GatewayError(502, f"login state response missing state/authUrl: {data}")

    print("=" * 72)
    print("Open this URL in a browser to authorize the gateway:")
    print(f"  {auth_url}")
    print("Waiting for authorization... (polling; Ctrl-C to abort)")
    print("=" * 72)

    deadline = time.time() + 600
    token_payload: dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(5)
        try:
            tok_url = f"{base}/v2/plugin/auth/token?state={state}"
            r = urllib.request.urlopen(tok_url, timeout=30)
            token_payload = json.loads(r.read().decode("utf-8"))
        except Exception:
            continue
        if token_payload.get("accessToken") or (token_payload.get("data") or {}).get("accessToken"):
            break
    else:
        raise GatewayError(408, "login timed out after 600s")

    account_payload: dict[str, Any] = {}
    try:
        acct_url = f"{base}/v2/plugin/login/account?state={state}"
        r = urllib.request.urlopen(acct_url, timeout=30)
        account_payload = json.loads(r.read().decode("utf-8"))
    except Exception:
        pass

    token = token_payload.get("accessToken") or (token_payload.get("data") or {}).get("accessToken")
    account = account_payload.get("account") or account_payload.get("data") or {}
    auth = {
        "accessToken": token,
        "refreshToken": token_payload.get("refreshToken") or "",
        "tokenType": "Bearer",
        "expiresAt": int(token_payload.get("expiresAt") or 0),
        "domain": token_payload.get("domain") or "",
    }
    data_out = {"account": account, "auth": auth, "accounts": [account] if account else []}
    path = save_path or auth_file() or _DEFAULT_AUTH_FILES[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data_out, f, ensure_ascii=False, indent=2)
    log.info("login complete; session saved to %s", path)
    return load_session(path)
