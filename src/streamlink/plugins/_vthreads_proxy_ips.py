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

# Background prewarm: kicks off on first import so first real use has no wait
_prewarm_started = False
_prewarm_thread: threading.Thread | None = None

_lock = threading.Lock()
_orig_getaddrinfo = socket.getaddrinfo
_patched = False
_blacklist: set[str] = set()


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


def _fetch_list() -> list[str]:
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
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode()
    return [line.strip() for line in text.splitlines() if line.strip() and "." in line]


def _screen_one(ip: str, host: str = "vthreads.top") -> bool:
    """Return True if the IP accepts a TCP connection on 443.
    We deliberately skip TLS here for speed (~200-500ms vs ~1-2s); real requests
    that later fail via TLS/HTTP get the IP blacklisted at the call site."""
    try:
        with socket.create_connection((ip, 443), timeout=_SCREEN_TIMEOUT):
            return True
    except (OSError, TimeoutError):
        return False


def _screen_first(ips: list[str], quorum: int = _FAST_QUORUM) -> list[str]:
    """Return as soon as `quorum` IPs pass screening. Blocks up to a few seconds."""
    good: list[str] = []
    with ThreadPoolExecutor(max_workers=_SCREEN_WORKERS) as ex:
        futures = {ex.submit(_screen_one, ip): ip for ip in ips}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    good.append(ip)
                    if len(good) >= quorum:
                        for f in futures:
                            f.cancel()
                        break
            except Exception:
                pass
    return good


def _screen_full(ips: list[str], keep: int = _TARGET_POOL) -> list[str]:
    """Screen every IP until `keep` survive. Used by the background top-up thread."""
    good: list[str] = []
    with ThreadPoolExecutor(max_workers=_SCREEN_WORKERS) as ex:
        futures = {ex.submit(_screen_one, ip): ip for ip in ips}
        for fut in as_completed(futures):
            ip = futures[fut]
            try:
                if fut.result():
                    good.append(ip)
                    if len(good) >= keep:
                        for f in futures:
                            f.cancel()
                        break
            except Exception:
                pass
    return good


def _topup_in_background(remaining_ips: list[str], initial: list[str]) -> None:
    """Screen the rest and merge into cache so the pool grows without blocking the caller."""
    try:
        extras = _screen_full(remaining_ips, keep=_TARGET_POOL - len(initial))
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


def refresh(force: bool = False) -> list[str]:
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
        raw_ips = _fetch_list()
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
            args=(remaining, fast),
            daemon=True,
            name="vthreads-proxy-topup",
        )
        t.start()
    return fast


def blacklist(ip: str) -> None:
    with _lock:
        _blacklist.add(ip)


def pick() -> str | None:
    ips = [ip for ip in refresh() if ip not in _blacklist]
    return random.choice(ips) if ips else None


def _patched_getaddrinfo(host, port, *args, **kwargs):
    if host == "vthreads.top":
        ip = pick()
        if ip:
            resolved_port = port if isinstance(port, int) else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, resolved_port))]
    return _orig_getaddrinfo(host, port, *args, **kwargs)


def enable() -> bool:
    """Install the DNS override. Returns True if a working IP pool is available."""
    global _patched
    with _lock:
        if _patched:
            return bool(refresh())
    # prime + screen outside the lock
    ips = refresh()
    if not ips:
        return False
    with _lock:
        if not _patched:
            socket.getaddrinfo = _patched_getaddrinfo
            _patched = True
    return True


def disable() -> None:
    global _patched
    with _lock:
        if _patched:
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
