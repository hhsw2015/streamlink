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
import socket
import subprocess
import sys
import time
import urllib.parse

LOG = "/tmp/streamlink-scheme-handler.log"
CHILD_LOG = "/tmp/streamlink-redirect.log"   # shared with the native host

ENHANCE_OK = {"", "speed", "quality", "max", "photo"}
PLAYER_OK = {"senplayer", "iina"}
INGEST_PORT = 41599   # fixed port the Telegram userscript POSTs the stream to


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
    action = (parsed.netloc or parsed.path.strip("/")).lower()
    if parsed.scheme != "ytplay" or action not in ("play", "ingest"):
        log("rejected: not ytplay://play or ytplay://ingest")
        return 1
    q = urllib.parse.parse_qs(parsed.query)

    def one(name: str, default: str = "") -> str:
        return (q.get(name) or [default])[0].strip()

    enhance = one("enhance").lower()
    if enhance not in ENHANCE_OK:
        enhance = "photo"
    player = one("player", "iina").lower()
    if player not in PLAYER_OK:
        player = "iina"

    if action == "ingest":
        # Browser-pushed stream (e.g. Telegram web): start the ingest server on a
        # fixed port; the page then POSTs the media to http://127.0.0.1:<port>/ingest.
        # A new video wants a fresh server, so kill any stale ingest first - but
        # WAIT for it to release the port before spawning, or the new bind races it
        # ("Address already in use") and the server dies, leaving the POST with
        # nothing to hit.
        port = one("port", str(INGEST_PORT))
        if not port.isdigit():
            port = str(INGEST_PORT)
        pnum = int(port)

        def _port_free() -> bool:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", pnum)); return True
            except OSError:
                return False
            finally:
                s.close()

        if not _port_free():
            subprocess.run(["pkill", "-9", "-f", "streamlink-ytplay .* --ingest"],
                           capture_output=True)
            for _ in range(40):          # up to ~4s for the old server to exit
                if _port_free():
                    break
                time.sleep(0.1)
        cmd = ["streamlink-ytplay", "x", "--ingest", "--port", port,
               "--idle-timeout", "120", "--player", player, "--enhance", enhance]
        logline = f"[ytplay-scheme] INGEST port={port} enhance={enhance} player={player}"
    else:  # play
        url = one("url")
        if not url.lower().startswith(("http://", "https://", "file://")):
            log("rejected: bad url " + url[:80])
            return 1
        quality = one("quality", "best").lower()
        if quality != "best" and not quality.rstrip("p").isdigit():
            quality = "best"
        cmd = ["streamlink-ytplay", url, quality, "--idle-timeout", "120",
               "--player", player]
        if enhance:
            cmd += ["--enhance", enhance]
        logline = (f"[ytplay-scheme] url={url} quality={quality} "
                   f"enhance={enhance} player={player}")

    child = open(CHILD_LOG, "a")
    child.write("\n===== " + datetime.datetime.now().isoformat(timespec="seconds")
                + " " + logline + " =====\n")
    child.flush()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=child,
                            stderr=subprocess.STDOUT, start_new_session=True,
                            env=child_env())
    log(f"spawned streamlink-ytplay pid={proc.pid} ({action})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
