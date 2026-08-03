"""Account pool for savenow.to.

Behaviour:
- Persists a small pool of (email, password, api_key, balance) tuples on disk.
- `get_active_key()` returns a key that still has enough balance for a
  worst-case single-request charge; otherwise auto-registers a fresh account
  and returns its key.
- `estimated_cost(fmt, duration_s)` computes the wallet debit for a given
  format+duration per the published pricing rules.
- `retire(key)` blacklists a key that just came back as exhausted so the next
  call gets a different one.
- Nothing here is streamlink-specific — it only needs `urllib` + `re` +
  `json` so tests can exercise the account logic without hitting the plugin.

Env overrides:
- SAVENOW_ACCOUNTS_FILE   — cache path (default ~/.cache/streamlink-savenow/accounts.json)
- SAVENOW_API_KEY         — pin a specific key (skips the pool). Legacy name
                            VDAPI_API_KEY / VIDEO_DL_API_KEY also honoured.
- SAVENOW_AUTOREGISTER    — set to "0" to disable auto-signup on exhaustion.
- SAVENOW_ORIGIN          — override the video-download-api.com host (rare).
"""

from __future__ import annotations

import html as _html
import json
import os
import random
import re
import string
import secrets as _secrets
import threading
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError


ORIGIN = os.environ.get("SAVENOW_ORIGIN", "https://video-download-api.com").rstrip("/")
ACCOUNTS_FILE = os.path.expanduser(
    os.environ.get("SAVENOW_ACCOUNTS_FILE", "~/.cache/streamlink-savenow/accounts.json")
)
AUTOREGISTER = os.environ.get("SAVENOW_AUTOREGISTER", "1") not in ("0", "false", "no")

# Pricing per format (USD per request within the format's base duration).
# Mirrors the public pricing table so cost estimation stays in sync.
_BASE_PRICE = {
    # audio
    "mp3": 0.00020, "m4a": 0.00015, "webm": 0.00020,
    "aac": 0.00020, "flac": 0.00020, "opus": 0.00020,
    "ogg": 0.00020, "vorbis": 0.00020, "wav": 0.00020,
    # standard video
    "144": 0.00020, "240": 0.00020, "360": 0.00020,
    "480": 0.00020, "720": 0.00020, "1080": 0.00020,
    "1440": 0.00030,
    # 4K / 8K
    "4k": 0.00035, "8k": 0.00035, "mp44k": 0.00035, "mp48k": 0.00035,
    # MOV variants
    "MOV360": 0.00020, "MOV480": 0.00020, "MOV720": 0.00020,
    "MOV1080": 0.00020, "MOV1440": 0.00030,
}
# Base duration in minutes per format.
_BASE_DURATION_MIN = {
    "4k": 30, "8k": 30, "mp44k": 30, "mp48k": 30,
}
_DEFAULT_BASE_DURATION_MIN = 180

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

_lock = threading.Lock()
_pool_cache: list[dict] | None = None


# --------------------------------------------------------------------------- #
# Pricing / cost estimation
# --------------------------------------------------------------------------- #

def estimated_cost(fmt: str, duration_s: float | None = None) -> float:
    """USD debit for one completed request at `fmt`, with `duration_s`
    seconds of media. When duration is unknown, assume the format's base
    duration so we don't accidentally rule out a short video."""
    base = _BASE_PRICE.get(fmt, 0.00020)
    base_min = _BASE_DURATION_MIN.get(fmt, _DEFAULT_BASE_DURATION_MIN)
    if duration_s is None:
        return base
    duration_min = max(0, duration_s / 60.0)
    excess = duration_min - base_min
    if excess <= 0:
        return base
    # First 90 excess minutes: ×3. Next 90: ×5. Each further 90 block adds
    # ×2 to the running multiplier.
    multiplier = 3
    remaining = excess
    charged = 0
    # First 90 min block
    take = min(remaining, 90)
    charged += take * multiplier
    remaining -= take
    if remaining > 0:
        multiplier = 5
        take = min(remaining, 90)
        charged += take * multiplier
        remaining -= take
    while remaining > 0:
        multiplier += 2
        take = min(remaining, 90)
        charged += take * multiplier
        remaining -= take
    # `charged` is now a multiplier-minutes total scaled against the base.
    # Convert back to a single-request price by treating base_min as the unit.
    ratio = charged / base_min
    return base * (1 + ratio)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def _load_pool() -> list[dict]:
    global _pool_cache
    if _pool_cache is not None:
        return _pool_cache
    try:
        with open(ACCOUNTS_FILE) as f:
            _pool_cache = json.load(f)
    except (OSError, ValueError):
        _pool_cache = []
    if not isinstance(_pool_cache, list):
        _pool_cache = []
    return _pool_cache


def _save_pool() -> None:
    if _pool_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(ACCOUNTS_FILE), exist_ok=True)
        tmp = ACCOUNTS_FILE + "." + str(os.getpid()) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_pool_cache, f, indent=2)
        os.replace(tmp, ACCOUNTS_FILE)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# HTTP session helper (uses urllib so we don't drag in `requests` here)
# --------------------------------------------------------------------------- #

def _new_opener() -> urllib.request.OpenerDirector:
    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [("User-Agent", _UA)]
    opener._cj = cj  # type: ignore[attr-defined]
    return opener


def _get(opener, url: str, timeout: float = 30) -> tuple[int, str]:
    try:
        r = opener.open(url, timeout=timeout)
        return r.status, r.read().decode()
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _post(opener, url: str, form: dict, csrf: str, timeout: float = 30) -> tuple[int, str, str]:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": url,
            "X-XSRF-TOKEN": csrf,
            "Accept": "text/html",
        },
    )
    try:
        r = opener.open(req, timeout=timeout)
        return r.status, r.geturl(), r.read().decode()
    except HTTPError as e:
        return e.code, url, e.read().decode("utf-8", "replace")


def _extract_csrf(html: str) -> str | None:
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


def _extract_data_page(html: str) -> dict | None:
    """Inertia embeds the page state in <div data-page="...">. Returns parsed JSON."""
    m = re.search(r'data-page="([^"]+)"', html)
    if not m:
        return None
    try:
        return json.loads(_html.unescape(m.group(1)))
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Registration + balance
# --------------------------------------------------------------------------- #

def _random_email() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=10)) + "@outlook.com"


def _random_name() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=8)).title()


def register_new(email: str | None = None, password: str | None = None) -> dict | None:
    """Register a fresh account. Returns account dict on success, None on fail."""
    email = email or _random_email()
    password = password or _secrets.token_urlsafe(16)
    name = _random_name()
    opener = _new_opener()
    for attempt in range(3):
        try:
            status, html = _get(opener, f"{ORIGIN}/register")
            token = _extract_csrf(html)
            if not token:
                time.sleep(1.5)
                continue
            code, landing, body = _post(
                opener, f"{ORIGIN}/register",
                {"_token": token, "name": name, "email": email,
                 "password": password, "password_confirmation": password},
                token,
            )
            if not landing.endswith("/dashboard"):
                # Look for validation errors in the returned Inertia page.
                page = _extract_data_page(body) or {}
                errs = (page.get("props") or {}).get("errors") or {}
                if errs:
                    return None
                time.sleep(1.5)
                continue
            page = _extract_data_page(body) or {}
            api_key_block = (page.get("props") or {}).get("apiKey") or {}
            api_key = api_key_block.get("key")
            balance_micro = api_key_block.get("balanceMicro") or 0
            if not api_key:
                return None
            return {
                "email": email,
                "password": password,
                "name": name,
                "api_key": api_key,
                "balance_micro": int(balance_micro),
                "retired": False,
                "created_at": time.time(),
            }
        except (URLError, HTTPError, TimeoutError, OSError):
            time.sleep(2)
    return None


def refresh_balance(account: dict) -> int | None:
    """Log in and query current balanceMicro via Inertia JSON. Returns new
    balance_micro or None on failure. Mutates the account in place."""
    opener = _new_opener()
    try:
        status, html = _get(opener, f"{ORIGIN}/login")
        token = _extract_csrf(html)
        if not token:
            return None
        code, landing, body = _post(
            opener, f"{ORIGIN}/login",
            {"_token": token, "email": account["email"], "password": account["password"]},
            token,
        )
        if not landing.endswith("/dashboard"):
            return None
        # Same page we just landed on already has apiKey.balanceMicro.
        page = _extract_data_page(body) or {}
        block = (page.get("props") or {}).get("apiKey") or {}
        micro = block.get("balanceMicro")
        if micro is None:
            return None
        account["balance_micro"] = int(micro)
        return int(micro)
    except (URLError, HTTPError, TimeoutError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def _env_key() -> str | None:
    for var in ("SAVENOW_API_KEY", "VDAPI_API_KEY", "VIDEO_DL_API_KEY"):
        v = os.environ.get(var)
        if v:
            return v.strip()
    return None


def get_active_key(min_cost_usd: float = 0.0004) -> str | None:
    """Return an API key with enough balance for at least `min_cost_usd`. The
    default 0.0004 covers a single ≤180min MP4 1080p request (0.0002) with a
    safety margin. Auto-registers a fresh account when the pool is exhausted
    and SAVENOW_AUTOREGISTER is on."""
    # 1. Explicit env override — used regardless of balance.
    pinned = _env_key()
    if pinned:
        return pinned

    min_micro = int(min_cost_usd * 1_000_000)
    with _lock:
        pool = _load_pool()
        # Prefer accounts with headroom.
        for acc in pool:
            if acc.get("retired"):
                continue
            if acc.get("balance_micro", 0) >= min_micro:
                return acc["api_key"]

        if not AUTOREGISTER:
            return None

        # 2. Try to top up: refresh live balance of each non-retired account
        # before spending a new signup slot.
        for acc in pool:
            if acc.get("retired"):
                continue
            fresh = refresh_balance(acc)
            if fresh is not None and fresh >= min_micro:
                _save_pool()
                return acc["api_key"]
            if fresh is not None and fresh < min_micro:
                acc["retired"] = True

        # 3. Register a new account.
        new_acc = register_new()
        if not new_acc:
            _save_pool()
            return None
        pool.append(new_acc)
        _save_pool()
        return new_acc["api_key"]


def has_headroom(cost_usd: float) -> bool:
    """Cheap check: is there a key that can afford `cost_usd` right now?"""
    key = get_active_key(min_cost_usd=cost_usd)
    return key is not None


def mark_used(api_key: str, cost_usd: float = 0.0002) -> None:
    """Debit the cached balance so we know when to rotate before hitting a
    real 402/insufficient-funds response."""
    with _lock:
        pool = _load_pool()
        for acc in pool:
            if acc.get("api_key") == api_key:
                acc["balance_micro"] = max(0, acc.get("balance_micro", 0) - int(cost_usd * 1_000_000))
                break
        _save_pool()


def retire(api_key: str) -> None:
    """Mark a key as unusable (exhausted / banned). Next get_active_key call
    will skip it and register a new account if needed."""
    with _lock:
        pool = _load_pool()
        for acc in pool:
            if acc.get("api_key") == api_key:
                acc["retired"] = True
                acc["balance_micro"] = 0
                break
        _save_pool()


def list_accounts() -> list[dict]:
    """Read-only view for dashboards / tests."""
    with _lock:
        return list(_load_pool())
