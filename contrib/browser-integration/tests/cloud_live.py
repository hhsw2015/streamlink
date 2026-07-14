#!/usr/bin/env python3
"""Live smoke test against the Cloudflare Worker extractor.

Verifies:
- /extract returns success (cache-hit inline) or a pending job_id
- If pending, poll /status until success (bounded), then GET /result
- direct_url is a plain https URL with a plausible file

Skipped (exit 0 with 'SKIP') when there's no network. Failures exit non-zero.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "https://extractor.bugcf.ccwu.cc"
TOKEN = "test-token-2026-extractor"
TEST_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
QUALITY = "1080p"
POLL_TIMEOUT = 180  # seconds


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    # Cloudflare's bot-fight blocks the default urllib UA; use a real Chrome string.
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    )
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={
            "X-Auth": TOKEN,
            "Accept": "application/json",
            "User-Agent": ua,
        },
    )
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = {"error": str(e)}
        return e.code, payload


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main() -> int:
    print("cloud extractor live tests")

    try:
        code, submit = _request("POST", "/extract", {"source_url": TEST_URL, "quality": QUALITY})
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  [SKIP] network unavailable ({type(e).__name__}: {e})")
        return 0

    check("submit HTTP 200", code == 200, str(submit))

    if submit.get("status") == "success":
        result = submit
    else:
        job_id = submit.get("job_id")
        check("submit gives job_id or inline success", bool(job_id), str(submit))
        deadline = time.monotonic() + POLL_TIMEOUT
        result = None
        while time.monotonic() < deadline:
            time.sleep(3)
            code, st = _request("GET", f"/status/{job_id}")
            if code != 200:
                continue
            if st.get("status") == "success":
                code, result = _request("GET", f"/result/{job_id}")
                check("result HTTP 200", code == 200, str(result))
                break
            if st.get("status") == "failed":
                sys.exit("job failed: " + str(st.get("error")))
        check("job reached success within timeout", result is not None)

    check("result has direct_url", bool(result.get("direct_url")), str(result)[:200])
    check("direct_url is https", result["direct_url"].startswith("https://"), result["direct_url"])
    check("required_headers present", isinstance(result.get("required_headers"), dict), str(result.get("required_headers")))

    print(f"  → direct_url: {result['direct_url'][:80]}...")
    print(f"  → filename:   {result.get('filename', '?')[:80]}")
    print("all cloud live tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
