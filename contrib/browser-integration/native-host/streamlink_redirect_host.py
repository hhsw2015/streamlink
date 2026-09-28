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
import time
import traceback

LOG_PATH = "/tmp/streamlink-native-host.log"
CHILD_LOG = "/tmp/streamlink-redirect.log"
STREAMLINK_REDIRECT = "streamlink-redirect"  # must be on PATH when Chrome runs us
STREAMLINK_YTPLAY = "streamlink-ytplay"
DEFAULT_PLAYER = os.environ.get("STREAMLINK_PLAYER", "IINA")  # macOS app name for `open -a`
# Pre-extract: the extension can prewarm a video (extraction + bootstrap ahead of
# the click) so a later play skips the ~4s extraction wait. Warm sessions are
# tracked on disk (the host is one-shot per message, so no in-memory state).
WARM_DIR = "/tmp/ytplay-warm"
WARM_TTL = 180  # prewarm must be played within this many seconds (matches ytplay idle-timeout)
WARM_WAIT = 15  # max seconds a play will WAIT for an in-progress matching prewarm to finish
                # (attach to it instead of killing + re-extracting = double work)
PLAYER_SCHEMES = {
    "senplayer": "senplayer://x-callback-url/play?url=$edurl&name=$name",
    "iina": "iina://weblink?url=$edurl",
}


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


def write_cookies_file(cookies: list) -> str | None:
    """Persist extension-provided cookies as a Netscape cookies.txt for yt-dlp."""
    if not cookies:
        return None
    import tempfile
    fd, path = tempfile.mkstemp(prefix="ytplay-cookies-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            domain = str(c.get("domain", ""))
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            f.write("\t".join([
                domain,
                include_sub,
                str(c.get("path", "/")),
                "TRUE" if c.get("secure") else "FALSE",
                str(int(c.get("expirationDate", 0) or 0)),
                str(c.get("name", "")),
                str(c.get("value", "")),
            ]) + "\n")
    os.chmod(path, 0o600)
    return path


def _warm_path(url: str, quality: str, enhance: str) -> str:
    import hashlib
    key = hashlib.sha1(f"{url}|{quality}|{enhance}".encode()).hexdigest()[:16]
    return os.path.join(WARM_DIR, key + ".json")


def _read_warm(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def kill_prewarms_for_url(url: str) -> None:
    """Kill any prewarm session serving `url` (e.g. a "best" prewarm the user
    never played because they picked "1080p"). A wasted prewarm otherwise keeps
    competing with the real play for network + GPU, slowing the open. Call this
    only when there is NO matching warm session to reuse."""
    try:
        entries = os.listdir(WARM_DIR)
    except OSError:
        return
    for fn in entries:
        if not fn.endswith(".json"):
            continue
        p = os.path.join(WARM_DIR, fn)
        rec = _read_warm(p)
        if rec and rec.get("src") == url and _pid_alive(int(rec.get("pid", 0))):
            try:
                os.kill(int(rec["pid"]), 15)   # SIGTERM
                log("killed wasted prewarm pid=" + str(rec.get("pid")) + " for " + url)
            except OSError:
                pass
        try:
            os.remove(p)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _warm_alive(rec: dict | None) -> bool:
    # Trust a record only while its ytplay is still running AND still inside the
    # pre-extract window. The warm ytplay self-exits via --idle-timeout (WARM_TTL)
    # when unused, so a record older than that is dead; the ts bound also guards
    # against a recycled pid landing on an unrelated process.
    if not rec or not rec.get("ready"):
        return False
    if time.time() - float(rec.get("ts", 0)) >= WARM_TTL:
        return False
    return _pid_alive(int(rec.get("pid", 0)))


def _launch_scheme(player_or_scheme: str, url: str, title: str) -> None:
    from urllib.parse import quote as _q
    s = (player_or_scheme or "").strip()
    scheme = s if "://" in s else PLAYER_SCHEMES.get(s.lower(), PLAYER_SCHEMES["senplayer"])
    launch_url = (scheme
                  .replace("$edurl", _q(url, safe=""))
                  .replace("$durl", url)
                  .replace("$name", _q(title or "", safe="")))
    log("warm-play open: " + launch_url)
    subprocess.Popen(["open", launch_url])


def _spawn_serve(url: str, quality: str, enhance: str, cookies: list,
                 info_path: str, idle: int, tag: str) -> int:
    """Launch ytplay headless (--no-open --serve-info): extract, serve, write a
    ready record to info_path. Lazy-boots (no GPU until a player connects), so it
    is safe to run several in parallel (prewarm + the fresh racer). Returns pid."""
    os.makedirs(WARM_DIR, exist_ok=True)
    child_log = open(CHILD_LOG, "a")
    child_log.write("\n===== " + _dt.datetime.now().isoformat(timespec="seconds")
                    + " [" + tag + "] url=" + url + " quality=" + quality
                    + ((" enhance=" + enhance) if enhance else "") + " =====\n")
    child_log.flush()
    cmd = [STREAMLINK_YTPLAY, url, quality, "--no-open",
           "--idle-timeout", str(idle), "--serve-info", info_path]
    if enhance:
        cmd += ["--enhance", enhance]
    cookies_file = write_cookies_file(cookies)
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    else:
        cmd += ["--cookies-from-browser", os.environ.get("YTPLAY_COOKIES_BROWSER", "chrome")]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=child_log,
                            stderr=subprocess.STDOUT, start_new_session=True, env=_child_env())
    return proc.pid


def prewarm_ytplay(url: str, quality: str, enhance: str, cookies: list) -> int:
    """Launch ytplay in serve-only pre-extract mode (extraction + bootstrap +
    serve now, record a warm-session file), so a later play skips the extraction
    wait. No-op if a fresh warm session already exists for this (url,q,enhance)."""
    info_path = _warm_path(url, quality, enhance)
    existing = _read_warm(info_path)
    if (existing and _pid_alive(int(existing.get("pid", 0)))
            and (existing.get("ready") or existing.get("pending"))):
        return int(existing.get("pid", 0))               # already warm OR warming
    try:
        os.remove(info_path)                              # clear stale record
    except OSError:
        pass
    pid = _spawn_serve(url, quality, enhance, cookies, info_path, WARM_TTL, "prewarm")
    # "pending" marker (ytplay overwrites it with the ready record when serving):
    # lets a play that clicks before extraction finishes RACE this prewarm instead
    # of blindly waiting on it then re-extracting from scratch.
    try:
        with open(info_path, "w") as f:
            json.dump({"pending": True, "pid": pid, "ts": time.time(),
                       "src": url, "quality": quality, "enhance": enhance}, f)
    except OSError:
        pass
    return pid


def try_warm_play(url: str, quality: str, enhance: str, player_or_scheme: str) -> int | None:
    """Instant reuse if a matching prewarm is ALREADY ready: launch the player at
    its warm URL, no wait. Returns the pid, or None (caller races/extracts)."""
    rec = _read_warm(_warm_path(url, quality, enhance))
    if _warm_alive(rec):
        _launch_scheme(player_or_scheme, rec["url"], rec.get("title", ""))
        return int(rec.get("pid", 0))
    return None


def race_play(url: str, quality: str, enhance: str, player_or_scheme: str,
              cookies: list) -> int | None:
    """A matching prewarm is mid-extraction. Launch a FRESH server in parallel and
    RACE: open the player at whichever serves first, kill the loser. Beats the old
    'attach-wait WARM_WAIT then re-extract' (whose worst case was ~15s wait + a full
    fresh extraction = ~24s) - here the worst case is just one fresh extraction, and
    a nearly-done prewarm still wins instantly. Both servers are --no-open lazy, so
    the race costs only network, no GPU. Returns the winner pid, or None."""
    warm_path = _warm_path(url, quality, enhance)
    race_path = os.path.join(WARM_DIR, "race-%d.json" % os.getpid())
    try:
        os.remove(race_path)
    except OSError:
        pass
    fresh_pid = _spawn_serve(url, quality, enhance, cookies, race_path, 120, "race")
    log("race: fresh pid=" + str(fresh_pid) + " vs pending prewarm for " + url)
    deadline = time.time() + WARM_WAIT + 20   # generous: racing two, not blocking on one
    while time.time() < deadline:
        for path in (warm_path, race_path):
            rec = _read_warm(path)
            if _warm_alive(rec):
                _launch_scheme(player_or_scheme, rec["url"], rec.get("title", ""))
                winner = int(rec.get("pid", 0))
                other = race_path if path == warm_path else warm_path
                orec = _read_warm(other)
                opid = int((orec or {}).get("pid", 0))
                if opid and opid != winner and _pid_alive(opid):
                    try:
                        os.kill(opid, 15)                 # stop the loser (no wasted GPU/net)
                        log("race: killed loser pid=" + str(opid))
                    except OSError:
                        pass
                try:
                    os.remove(race_path)
                except OSError:
                    pass
                log("race: winner pid=" + str(winner)
                    + (" (prewarm)" if path == warm_path else " (fresh)"))
                return winner
        wrec = _read_warm(warm_path)
        warm_alive = bool(wrec and _pid_alive(int(wrec.get("pid", 0))))
        if not _pid_alive(fresh_pid) and not warm_alive:
            break                                          # both died: cold fallback
        time.sleep(0.25)
    if _pid_alive(fresh_pid):
        try:
            os.kill(fresh_pid, 15)
        except OSError:
            pass
    try:
        os.remove(race_path)
    except OSError:
        pass
    return None


def launch_ytplay(url: str, quality: str, scheme: str, cookies: list,
                  enhance: str = "") -> int:
    child_log = open(CHILD_LOG, "a")
    child_log.write(
        "\n===== " + _dt.datetime.now().isoformat(timespec="seconds")
        + " [ytplay] url=" + url + " quality=" + quality
        + " cookies=" + str(len(cookies))
        + ((" enhance=" + enhance) if enhance else "") + " =====\n",
    )
    child_log.flush()
    # 120s: release the server + temp files reasonably soon after the player is
    # closed, while still surviving a short pause. (vtenhance is killed promptly on
    # exit via VideoRemuxer.cleanup, so GPU work stops immediately regardless.)
    cmd = [STREAMLINK_YTPLAY, url, quality, "--idle-timeout", "120"]
    if enhance:
        cmd += ["--enhance", enhance]
    if scheme:
        cmd += ["--player", scheme]
    cookies_file = write_cookies_file(cookies)
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    else:
        # No cookies from the extension (older worker / cookie API failure):
        # let yt-dlp read the browser profile directly. Chrome first (keychain
        # prompt possible on first use), YTPLAY_COOKIES_BROWSER to override.
        cmd += ["--cookies-from-browser",
                os.environ.get("YTPLAY_COOKIES_BROWSER", "chrome")]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=child_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=_child_env(),
    )
    return proc.pid


def launch(url: str, quality: str, player: str, scheme: str, skip_cloud: bool) -> int:
    child_log = open(CHILD_LOG, "a")
    child_log.write(
        "\n===== " + _dt.datetime.now().isoformat(timespec="seconds")
        + " url=" + url + " quality=" + quality + " scheme=" + (scheme or "")
        + " skip_cloud=" + str(skip_cloud) + " =====\n",
    )
    child_log.flush()
    env = _child_env()
    if skip_cloud:
        # Legacy name kept for any users who still pin an old plugin; the
        # current savenow plugin doesn't read this and doesn't need to
        # (there's no cloud step in-plugin, the extension already decided).
        env["SAVENOW_SKIP_CLOUD"] = "1"
        env["VTHREADS_SKIP_CLOUD"] = "1"
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
        env=env,
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
        # `skip_cloud` = extension already tried the cloud extractor itself and failed;
        # skip cloud in the plugin so we do NOT double-hit the same failing service.
        skip_cloud = bool(msg.get("skip_cloud", False))
        # `prefetched` = url is already a direct playable URL from the cloud extractor;
        # skip streamlink entirely and just hand it to the player's app via `open -a`.
        prefetched = bool(msg.get("prefetched", False))
        enhance = str(msg.get("enhance", "") or "")
        action = str(msg.get("action", "") or "").strip()
        if not url:
            write_message({"ok": False, "error": "empty url"})
            return 1
        # Pre-extract: extension asks us to warm this video ahead of the click.
        if action == "prewarm":
            pid = prewarm_ytplay(url, quality, enhance, msg.get("cookies") or [])
            log("prewarm ytplay pid=" + str(pid))
            write_message({"ok": True, "pid": pid, "warming": True})
            return 0
        # ytplay (yt-dlp pipeline: any site, max quality, full seek) is the only
        # resolve path now — savenow stopped granting credit, so the legacy
        # streamlink-redirect chain dead-ends. Old extension payloads (no
        # `engine` field, scheme template like "senplayer://...url=$edurl")
        # are migrated here so stale service workers keep working.
        if not prefetched:
            if scheme and "://" not in scheme:
                player_arg = scheme          # new payload: bare name (senplayer/iina)
            elif scheme:
                low = scheme.lower()         # legacy payload: full scheme template
                if "senplayer" in low:
                    player_arg = "senplayer"
                elif "iina" in low:
                    player_arg = "iina"
                else:
                    player_arg = scheme      # raw template - ytplay substitutes $edurl etc.
            else:
                player_arg = "iina" if player.lower() == "iina" else "senplayer"
            # Pre-extract fast path: if this video was prewarmed AND already ready,
            # launch the player straight at the warm session (no extraction wait).
            warm_pid = try_warm_play(url, quality, enhance, player_arg)
            if warm_pid is not None:
                log("warm-play reused pid=" + str(warm_pid) + " player=" + player_arg)
                write_message({"ok": True, "pid": warm_pid, "warm": True, "log": CHILD_LOG})
                return 0
            # A matching prewarm is still extracting? Race a fresh server against it
            # (whichever serves first wins) instead of blindly waiting then
            # re-extracting - the old attach-wait's ~24s worst case.
            wrec = _read_warm(_warm_path(url, quality, enhance))
            if wrec and wrec.get("pending") and _pid_alive(int(wrec.get("pid", 0))):
                race_pid = race_play(url, quality, enhance, player_arg,
                                     msg.get("cookies") or [])
                if race_pid is not None:
                    log("race-play winner pid=" + str(race_pid) + " player=" + player_arg)
                    write_message({"ok": True, "pid": race_pid, "warm": True, "log": CHILD_LOG})
                    return 0
            # No warm / race failed: clean cold launch (ytplay opens the player).
            kill_prewarms_for_url(url)
            pid = launch_ytplay(url, quality, player_arg, msg.get("cookies") or [],
                                enhance=enhance)
            log("launched streamlink-ytplay pid=" + str(pid) + " player=" + player_arg)
            write_message({"ok": True, "pid": pid, "log": CHILD_LOG})
            return 0
        if prefetched:
            if scheme:
                from urllib.parse import quote as _q
                import base64 as _b64
                launch_url = (
                    scheme
                    .replace("$edurl", _q(url, safe=""))
                    .replace("$bdurl", _b64.b64encode(url.encode()).decode())
                    .replace("$durl", url)
                    .replace("$name", "")
                )
                log(f"prefetched launch: open {launch_url}")
                subprocess.Popen(["open", launch_url])
            else:
                log(f"prefetched launch: open -a {player} {url}")
                subprocess.Popen(["open", "-a", player, url])
            write_message({"ok": True, "pid": 0, "log": CHILD_LOG})
            return 0
        pid = launch(url, quality, player, scheme, skip_cloud)
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
