#!/usr/bin/env python3
"""ytplay:// URL scheme handler.

Invoked by the YtplayScheme.app applet with the full URL as argv[1].
Parses  ytplay://play?url=<enc>&quality=best&enhance=quality&player=iina
and spawns `streamlink-ytplay` detached (same contract as the native host).

Validation: only whitelisted param values reach the CLI - a web page can
craft this URL, so nothing from it may become an arbitrary argument.
"""
import datetime
import os
import subprocess
import sys
import urllib.parse

LOG = "/tmp/streamlink-scheme-handler.log"
CHILD_LOG = "/tmp/streamlink-redirect.log"   # shared with the native host

ENHANCE_OK = {"", "speed", "quality", "max", "photo"}
PLAYER_OK = {"senplayer", "iina"}


def log(msg: str) -> None:
    with open(LOG, "a") as f:
        f.write(datetime.datetime.now().isoformat(timespec="seconds") + " " + msg + "\n")


def child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PATH", "").strip(":")
    extras = ["/opt/homebrew/bin", "/usr/local/bin"]
    parts = [p for p in existing.split(":") if p]
    parts += [p for p in extras if p not in parts]
    env["PATH"] = ":".join(parts)
    return env


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    log("received: " + raw)
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "ytplay" or (parsed.netloc or parsed.path.strip("/")) != "play":
        log("rejected: not ytplay://play")
        return 1
    q = urllib.parse.parse_qs(parsed.query)

    def one(name: str, default: str = "") -> str:
        return (q.get(name) or [default])[0].strip()

    url = one("url")
    if not url.lower().startswith(("http://", "https://", "file://")):
        log("rejected: bad url " + url[:80])
        return 1
    quality = one("quality", "best").lower()
    if quality != "best" and not quality.rstrip("p").isdigit():
        quality = "best"
    enhance = one("enhance").lower()
    if enhance not in ENHANCE_OK:
        enhance = "quality"
    player = one("player", "iina").lower()
    if player not in PLAYER_OK:
        player = "iina"

    cmd = ["streamlink-ytplay", url, quality, "--idle-timeout", "120",
           "--player", player]
    if enhance:
        cmd += ["--enhance", enhance]
    child = open(CHILD_LOG, "a")
    child.write("\n===== " + datetime.datetime.now().isoformat(timespec="seconds")
                + " [ytplay-scheme] url=" + url + " quality=" + quality
                + " enhance=" + enhance + " player=" + player + " =====\n")
    child.flush()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=child,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            env=child_env())
    log(f"spawned streamlink-ytplay pid={proc.pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
