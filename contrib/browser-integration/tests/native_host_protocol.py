#!/usr/bin/env python3
"""Speak Chrome's native-messaging protocol to the host script and assert its reply.

Runs without Chrome, without extension, without a real player: we only verify that
the host script correctly reads a JSON message, spawns something, and writes back
a valid JSON reply. `prefetched=true` is exercised so no real streamlink child is
started (the host just runs `open` on a fake URL).
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST = HERE.parent / "native-host" / "streamlink_redirect_host.py"

EXIT_OK = 0
EXIT_FAIL = 1


def talk(msg: dict) -> dict:
    """Round-trip one message through the host and return the parsed reply."""
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
    proc = subprocess.Popen(
        [sys.executable, str(HOST)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    encoded = json.dumps(msg).encode()
    proc.stdin.write(struct.pack("=I", len(encoded)))
    proc.stdin.write(encoded)
    proc.stdin.close()
    out = proc.stdout.read()
    err = proc.stderr.read().decode(errors="replace")
    proc.wait(timeout=10)
    if len(out) < 4:
        raise AssertionError(f"host wrote nothing (stderr: {err!r})")
    (reply_len,) = struct.unpack("=I", out[:4])
    return json.loads(out[4:4 + reply_len].decode())


def check(label: str, ok: bool, detail: str = "") -> None:
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        raise SystemExit(EXIT_FAIL)


def main() -> int:
    print("native-host protocol tests")

    # 1. Prefetched scheme launch — host resolves the placeholder and calls `open`.
    reply = talk({
        "url": "https://example.com/fake.mp4",
        "quality": "best",
        "scheme": "iina://weblink?url=$edurl",
        "prefetched": True,
    })
    check("prefetched+scheme replies ok=true", reply.get("ok") is True, str(reply))
    check("prefetched+scheme includes log path", "log" in reply, str(reply))

    # 2. Prefetched app launch (SenPlayer, macOS `open -a`).
    reply = talk({
        "url": "https://example.com/fake.mp4",
        "player": "SenPlayer",
        "prefetched": True,
    })
    check("prefetched+app replies ok=true", reply.get("ok") is True, str(reply))

    # 3. Empty URL rejected with structured error.
    reply = talk({"url": "", "quality": "best"})
    check("empty url rejected", reply.get("ok") is False, str(reply))
    check("empty url has error string", "error" in reply, str(reply))

    print("all native-host protocol tests passed")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
