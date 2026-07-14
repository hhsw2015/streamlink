#!/usr/bin/env python3
"""Native messaging host for the Streamlink Redirect browser extension.

Chrome spawns this script on every native message. It reads one JSON message from
stdin ({"url": "...", "quality": "..."}), launches ``streamlink-redirect`` as a
detached subprocess (so playback keeps running after we exit), writes one JSON
reply to stdout, and quits. No daemons, no long-lived processes.

Logs to /tmp/streamlink-native-host.log so you can `tail -f` and see what happened.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import struct
import subprocess
import sys
import traceback

LOG_PATH = "/tmp/streamlink-native-host.log"
CHILD_LOG = "/tmp/streamlink-redirect.log"
STREAMLINK_REDIRECT = "streamlink-redirect"  # must be on PATH when Chrome runs us
DEFAULT_PLAYER = os.environ.get("STREAMLINK_PLAYER", "IINA")  # macOS app name for `open -a`


def log(msg: str) -> None:
    try:
        with open(LOG_PATH, "a") as f:
            f.write(_dt.datetime.now().isoformat(timespec="seconds") + " " + msg + "\n")
    except OSError:
        pass


def read_message() -> dict:
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        raise EOFError("no message on stdin")
    (msg_len,) = struct.unpack("=I", raw_len)
    raw = sys.stdin.buffer.read(msg_len)
    return json.loads(raw.decode("utf-8"))


def write_message(obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("=I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PATH", "").strip(":")
    extras = ["/opt/homebrew/bin", "/usr/local/bin"]
    parts = [p for p in existing.split(":") if p] + [p for p in extras if p not in existing.split(":")]
    env["PATH"] = ":".join(parts)
    return env


def launch(url: str, quality: str, player: str, scheme: str) -> int:
    child_log = open(CHILD_LOG, "a")
    child_log.write(
        "\n===== " + _dt.datetime.now().isoformat(timespec="seconds")
        + " url=" + url + " quality=" + quality + " scheme=" + (scheme or "") + " =====\n",
    )
    child_log.flush()
    cmd = [
        STREAMLINK_REDIRECT,
        "--port", "8888",
        "--once",
        "--idle-timeout", "120",
    ]
    if scheme:
        cmd += ["--scheme", scheme]
    elif player:
        cmd += ["--open-with", player]
    else:
        cmd += ["--open"]
    cmd += [url, quality]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=child_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # detach so it survives after we return to Chrome
        env=_child_env(),
    )
    return proc.pid


def main() -> int:
    log("host started, argv=" + str(sys.argv))
    try:
        msg = read_message()
        log("received: " + json.dumps(msg))
        url = str(msg.get("url", "")).strip()
        quality = str(msg.get("quality", "best")).strip() or "best"
        player = str(msg.get("player", DEFAULT_PLAYER)).strip() or DEFAULT_PLAYER
        scheme = str(msg.get("scheme", "")).strip()
        if not url:
            write_message({"ok": False, "error": "empty url"})
            return 1
        pid = launch(url, quality, player, scheme)
        log("launched streamlink-redirect pid=" + str(pid))
        write_message({"ok": True, "pid": pid, "log": CHILD_LOG})
        return 0
    except Exception as err:
        log("ERROR: " + type(err).__name__ + ": " + str(err))
        log(traceback.format_exc())
        try:
            write_message({"ok": False, "error": type(err).__name__ + ": " + str(err)})
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
