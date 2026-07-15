"""Proxy IP pool for vthreads.top.

Fetches a community-maintained list of Cloudflare-frontable IPs from
ipdb.api.030101.xyz, screens which ones actually accept the vthreads
SNI, caches the survivors on disk, and exposes a monkey-patch that
makes `socket.getaddrinfo("vthreads.top", ...)` return one of the
proxy IPs at random. TLS SNI + Host header still use "vthreads.top",
so vthreads sees a rotating source IP instead of ours — bypassing the
per-source-IP rate limit.

Third-party maintains the IP list; we only fetch, screen, and rotate.
"""

from __future__ import annotations

import json
import os
import random
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_LIST_URL = "https://ipdb.api.030101.xyz/?type=bestproxy"
_CACHE_FILE = os.path.expanduser("~/.cache/streamlink-vthreads/proxy_ips.json")
_CACHE_TTL = 30 * 60           # 30 min — refresh list this often
_SCREEN_TIMEOUT = 1.5          # seconds — TCP connect only, no TLS
_SCREEN_WORKERS = 30           # parallel probes
_MIN_ALIVE = 2                 # fewer survivors → pool unusable
_FAST_QUORUM = 2               # return as soon as this many pass screening
_TARGET_POOL = 10              # background thread grows the pool to this size

# Optimistic bootstrap: seed the pool with known-good CF-edge IPs so `pick()`
# returns instantly on first use. If any are stale they get blacklisted and the
# background refresh replaces them. Community-vetted at snapshot time — refreshed
# every 30 min from _LIST_URL.
_HARDCODED_IPS = [
    "8.212.65.162",
    "47.242.218.87",
    "8.219.245.214",
    "8.219.236.218",
    "8.212.14.90",
    "8.219.255.49",
]

# Background prewarm: kicks off on first import so first real use has no wait
_prewarm_started = False
_prewarm_thread: threading.Thread | None = None

_lock = threading.Lock()
_orig_getaddrinfo = socket.getaddrinfo
_patched = False
_patch_refcount = 0  # nested enable() calls must all disable() before we unpatch
# IP → epoch-seconds-until-usable-again. Auto-expires so a temporarily flaky
# IP gets retried after a cooldown instead of being banned for the whole
# process lifetime.
_BLACKLIST_TTL = 300.0  # 5 min
_blacklist: dict[str, float] = {}


def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + "." + str(os.getpid()) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _CACHE_FILE)
    except OSError:
        pass


def _fetch_list(timeout: float = 5.0) -> list[str]:
    import urllib.request
    req = urllib.request.Request(
        _LIST_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = r.read().decode()
    return [line.strip() for line in text.splitlines() if line.strip() and "." in line]


def _screen_one(ip: str, host: str = "vthreads.top") -> bool:
    """Return True if we can TLS-handshake to `ip:443` using `host` as SNI.
    A TCP-only probe would let dead-path IPs through (network reaches the
    edge but TLS terminates on some other cert / GFW rewrites SNI); the
    full handshake weeds those out at ~500-1500ms per IP."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # we only care that TLS completes, not cert CN match
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((ip, 443), timeout=_SCREEN_TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                return tls.version() is not None
    except (OSError, ssl.SSLError, TimeoutError):
        return False


def _screen(ips: list[str], target: int) -> list[str]:
    """Return as soon as `target` IPs pass a TCP screen. Does NOT wait for
    in-flight probes to finish once the quorum is reached — the pool is
    shut down with wait=False so the caller unblocks in ~200-500ms even
    if some probes still have 1.5s of timeout left to burn."""
    good: list[str] = []
    ex = ThreadPoolExecutor(max_workers=_SCREEN_WORKERS)
    try:
        futures = {ex.submit(_screen_one, ip): ip for ip in ips}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    good.append(ip)
                    if len(good) >= target:
                        break
            except Exception:
                pass
    finally:
        # wait=False on Python 3.9+; older versions still return promptly
        # because the daemon threads holding sockets don't block interpreter exit.
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)
    return good


def _screen_first(ips: list[str], quorum: int = _FAST_QUORUM) -> list[str]:
    return _screen(ips, quorum)


def _screen_full(ips: list[str], keep: int = _TARGET_POOL) -> list[str]:
    return _screen(ips, keep)


def _topup_in_background(remaining_ips: list[str], initial_count: int) -> None:
    """Screen the rest and merge into cache so the pool grows without blocking
    the caller. `initial_count` tells us how many good IPs are already stored
    so we only screen enough to hit _TARGET_POOL."""
    try:
        need = max(1, _TARGET_POOL - initial_count)
        extras = _screen_full(remaining_ips, keep=need)
        if not extras:
            return
        with _lock:
            cache = _load_cache()
            entry = cache.get("vthreads.top") or {}
            merged = list(dict.fromkeys(list(entry.get("ips") or []) + extras))
            entry["ips"] = merged[:_TARGET_POOL]
            entry["fetched_at"] = time.time()
            cache["vthreads.top"] = entry
            _save_cache(cache)
    except Exception:
        pass


def _prescreen_hardcoded_locked() -> list[str]:
    """Blacklist hardcoded IPs that fail a TLS handshake right now. Returns
    the survivors, so the caller can trust `_HARDCODED_IPS` after filtering.
    Runs in parallel — only ~500ms in the common case."""
    now = time.monotonic()
    candidates = [ip for ip in _HARDCODED_IPS if not _is_blacklisted(ip, now)]
    if not candidates:
        return []
    good = _screen(candidates, target=len(candidates))
    dead = set(candidates) - set(good)
    for ip in dead:
        _blacklist[ip] = time.monotonic() + _BLACKLIST_TTL
    return good


def refresh(force: bool = False, timeout: float = 5.0) -> list[str]:
    """Return screened proxy IPs. First call blocks only until FAST_QUORUM IPs
    pass screening (~1-3s), then spawns a background thread to fill the pool
    up to TARGET_POOL. Subsequent calls hit the on-disk cache for TTL. Never
    raises — returns [] on total failure."""
    now = time.time()
    with _lock:
        cache = _load_cache()
        entry = cache.get("vthreads.top")
        if not force and entry and now - entry.get("fetched_at", 0) < _CACHE_TTL:
            return list(entry.get("ips") or [])
    try:
        raw_ips = _fetch_list(timeout=timeout)
    except Exception:
        return list((entry or {}).get("ips") or [])
    if not raw_ips:
        return list((entry or {}).get("ips") or [])
    fast = _screen_first(raw_ips, quorum=_FAST_QUORUM)
    if len(fast) < _MIN_ALIVE:
        return list((entry or {}).get("ips") or [])
    with _lock:
        cache["vthreads.top"] = {"fetched_at": now, "ips": fast}
        _save_cache(cache)
    # top up the pool in the background so future picks have more choices
    remaining = [ip for ip in raw_ips if ip not in fast]
    if remaining and len(fast) < _TARGET_POOL:
        t = threading.Thread(
            target=_topup_in_background,
            args=(remaining, len(fast)),
            daemon=True,
            name="vthreads-proxy-topup",
        )
        t.start()
    return fast


def blacklist(ip: str) -> None:
    """Mark this IP as unusable for _BLACKLIST_TTL seconds. Auto-expires so
    an IP that was only briefly flaky can be tried again later. Uses
    monotonic time so clock jumps (NTP, sleep/wake) don't corrupt the
    cooldown window."""
    with _lock:
        _blacklist[ip] = time.monotonic() + _BLACKLIST_TTL


def _is_blacklisted(ip: str, now: float) -> bool:
    return _blacklist.get(ip, 0) > now


def _live_blacklist_snapshot() -> set[str]:
    """Return {ip} for IPs still in cooldown right now. Cleans out expired
    entries as a side effect. Caller must hold `_lock`."""
    now = time.monotonic()
    expired = [ip for ip, until in _blacklist.items() if until <= now]
    for ip in expired:
        del _blacklist[ip]
    return set(_blacklist.keys())


def pick() -> str | None:
    """Return a proxy IP with zero blocking on the happy path.
    Preference order:
      1. Disk cache (populated by prewarm/refresh) — hot fast path
      2. Hardcoded bootstrap IPs — used until cache is warm
      3. Live refresh — only if the above two produced nothing"""
    ips: list[str] = []
    with _lock:
        entry = _load_cache().get("vthreads.top")
        cooling = _live_blacklist_snapshot()
        if entry:
            ips = [ip for ip in (entry.get("ips") or []) if ip not in cooling]
        if not ips:
            ips = [ip for ip in _HARDCODED_IPS if ip not in cooling]
    if not ips:
        # Everything blacklisted — synchronous refresh is the last resort.
        fresh = refresh(force=True, timeout=3.0)
        with _lock:
            cooling = _live_blacklist_snapshot()
        ips = [ip for ip in fresh if ip not in cooling]
    return random.choice(ips) if ips else None


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host == "vthreads.top":
        ip = pick()
        if ip:
            resolved_port = port if isinstance(port, int) else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, resolved_port))]
    return _orig_getaddrinfo(host, port, *args, **kwargs)


def enable() -> bool:
    """Install the DNS override. Never blocks on the network — hardcoded IPs
    seed the pool immediately, background refresh replaces them within seconds.
    Returns True as long as some IP is theoretically available. Ref-counted:
    concurrent callers each get their own enable/disable pair without racing
    each other into a premature unpatch."""
    global _patched, _patch_refcount
    with _lock:
        _patch_refcount += 1
        if not _patched:
            socket.getaddrinfo = _patched_getaddrinfo
            _patched = True
        available = _has_available_ip_locked()
    prewarm()
    return available


def _has_available_ip_locked() -> bool:
    """Whether pick() could produce something without a synchronous network
    call. Caller must already hold `_lock`."""
    now = time.monotonic()
    entry = _load_cache().get("vthreads.top")
    if entry and any(not _is_blacklisted(ip, now) for ip in (entry.get("ips") or [])):
        return True
    return any(not _is_blacklisted(ip, now) for ip in _HARDCODED_IPS)


def disable() -> None:
    """Release one reference to the DNS patch. When the refcount reaches zero
    (all callers have paired their enable/disable), restore the real
    getaddrinfo. Never unpatches while another thread is still inside its
    proxy attempt."""
    global _patched, _patch_refcount
    with _lock:
        if _patch_refcount > 0:
            _patch_refcount -= 1
        if _patch_refcount == 0 and _patched:
            socket.getaddrinfo = _orig_getaddrinfo
            _patched = False


def prewarm() -> None:
    """Kick off refresh() on a background daemon thread so the first real request
    that needs a proxy IP has an already-primed pool. Safe to call many times —
    only the first call actually starts the thread. Callable from module init or
    from the plugin at construction time."""
    global _prewarm_started, _prewarm_thread
    if os.environ.get("VTHREADS_USE_PROXY_IPS", "1") not in ("1", "true", "yes"):
        return
    with _lock:
        if _prewarm_started:
            return
        # If cache is still fresh, there is nothing to prewarm.
        entry = _load_cache().get("vthreads.top")
        if entry and time.time() - entry.get("fetched_at", 0) < _CACHE_TTL and entry.get("ips"):
            _prewarm_started = True
            return
        _prewarm_started = True

    def _run():
        try:
            # First blacklist any hardcoded seeds that are already unreachable so
            # pick() doesn't hand out dead IPs while the fresh refresh is
            # in flight.
            with _lock:
                _prescreen_hardcoded_locked()
            refresh()
        except Exception:
            pass

    _prewarm_thread = threading.Thread(target=_run, daemon=True, name="vthreads-proxy-prewarm")
    _prewarm_thread.start()


def wait_for_prewarm(timeout: float = 2.0) -> None:
    """Block up to `timeout` seconds for the prewarm thread. If prewarm was never
    started or has already finished, returns immediately."""
    t = _prewarm_thread
    if t is not None:
        t.join(timeout=timeout)
