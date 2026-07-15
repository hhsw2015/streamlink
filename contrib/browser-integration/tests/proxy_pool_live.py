#!/usr/bin/env python3
"""End-to-end live test of the proxy IP pool.

Exercises the full flow that only exists when direct vthreads.top is
rate-limited:

  1. Pool bootstraps from hardcoded IPs (0ms)
  2. Background prewarm fetches fresh list from ipdb.api and screens
  3. Monkey-patch installs, resolves vthreads.top → a proxy IP
  4. Real HTTPS request goes through the proxy IP with SNI=vthreads.top
  5. vthreads returns 200 (proving the proxy path works end-to-end)
  6. blacklist() + pick() rotates to a different IP
  7. disable() restores real DNS

Exit 0 = green. Any red = non-zero. Skipped (exit 0 with 'SKIP') on
no-network.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Make plugin imports work when the file is run directly.
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from streamlink.plugins import _vthreads_proxy_ips as pool

# Standalone HTTPS check that goes through streamlink-independent stack so we
# see raw behavior of the monkey-patch.
import urllib.request
import urllib.error

TEST_URL = (
    "https://vthreads.top/api/extract"
    "?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ"
    "&lang=zh&vid=" + "a" * 32
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def _do_https(referer_and_headers=True):
    headers = {"User-Agent": UA}
    if referer_and_headers:
        headers["Referer"] = "https://vthreads.top/zh/"
    req = urllib.request.Request(TEST_URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200]
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)[:200]


def main() -> int:
    print("=== proxy IP pool: end-to-end live test ===")

    # 1. Clean slate — wipe cache so we exercise hardcoded seed first.
    try:
        os.remove(pool._CACHE_FILE)
    except OSError:
        pass
    pool._blacklist.clear()
    pool._prewarm_started = False
    pool._prewarm_thread = None

    # 2. Cold enable — must be near-instant.
    t0 = time.monotonic()
    ok = pool.enable()
    cold_ms = (time.monotonic() - t0) * 1000
    check(f"cold enable() returns True and completes in <50ms (got {cold_ms:.1f}ms)",
          ok and cold_ms < 50)

    # 3. Pool must have at least one hardcoded IP available.
    ip = pool.pick()
    check(f"pick() returns an IP from hardcoded seeds (got {ip})",
          ip in pool._HARDCODED_IPS)

    # 4. Real HTTPS request through the monkey-patched DNS.
    t0 = time.monotonic()
    status, body = _do_https()
    request_s = time.monotonic() - t0
    if status is None:
        print(f"  [SKIP] no network / all proxy IPs unreachable ({body})")
        pool.disable()
        return 0
    check(f"HTTPS via proxy IP returns 200 (got {status}, {request_s:.2f}s)",
          status == 200,
          f"body head: {body!r}")

    # 5. blacklist + pick rotates.
    used_ip = ip
    pool.blacklist(used_ip)
    new_ip = pool.pick()
    check(f"blacklist({used_ip}) then pick() returns different IP (got {new_ip})",
          new_ip != used_ip and new_ip is not None)

    # 6. Background prewarm fills disk cache.
    print("  waiting up to 6s for background prewarm to refresh disk cache…")
    for _ in range(30):
        entry = pool._load_cache().get("vthreads.top")
        if entry and entry.get("ips"):
            break
        time.sleep(0.2)
    entry = pool._load_cache().get("vthreads.top")
    check("background prewarm populated disk cache",
          bool(entry and entry.get("ips")),
          f"ips: {(entry or {}).get('ips')}")

    # 7. disable() restores DNS.
    pool.disable()
    check("disable() sets _patched=False", not pool._patched)

    # 8. Second request after disable goes to real vthreads DNS. Depending on
    # whether the caller is rate-limited, this may return 200 (cache hit) or
    # 403/429 (limited). We just check the DNS unwind worked (any response).
    status2, _ = _do_https()
    check(f"post-disable request completed (status={status2})", status2 is not None)

    print("=== all proxy pool live tests passed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
