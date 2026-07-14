"""Redirect server: resolve a stream URL via Streamlink and serve HTTP 302 redirects to it.

Purpose:
    Streamlink's built-in ``--player-external-http`` reads the stream and forwards bytes
    to the connecting player. That path does not proxy HTTP Range requests, so players
    can't seek. This command instead resolves the stream to a real URL once and returns
    a 302 for every incoming request, letting the player fetch the target URL directly
    (with native Range/seek). Kept as a separate entry point so nothing about the main
    ``streamlink`` command changes.

Usage:
    streamlink-redirect <URL> [STREAM] [--port 8888] [--host 127.0.0.1]

Example:
    streamlink-redirect "https://www.youtube.com/watch?v=..." 1080p
"""

from __future__ import annotations

import argparse
import http.server
import socket
import socketserver
import sys

from streamlink import Streamlink
from streamlink.logger import basicConfig


def _notify_mac(title: str, msg: str) -> None:
    if sys.platform != "darwin":
        return
    import subprocess
    try:
        subprocess.Popen(
            ["osascript", "-e", 'display notification "' + msg.replace('"', "'")[:200]
             + '" with title "' + title + '"'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _clean_url(url: str) -> str:
    # Strip shell escapes that leak in when users quote a URL after tab-completion
    # (zsh/bash produce "\?", "\&", "\=" that survive inside quotes as literal backslashes).
    for esc in ("\\?", "\\&", "\\=", "\\#", "\\+"):
        url = url.replace(esc, esc[1])
    return url.strip()


def _resolve_url(url: str, stream_name: str) -> str:
    session = Streamlink()
    _name, plugin_class, resolved_url = session.resolve_url(url)
    plugin = plugin_class(session, resolved_url)
    streams = plugin.streams()
    if not streams:
        raise RuntimeError("no streams found for " + url)
    key = _pick_stream(streams, stream_name)
    if key is None:
        raise RuntimeError(
            "stream '" + stream_name + "' not available; got: " + ", ".join(streams.keys()),
        )
    return streams[key].to_url()


def _pick_stream(streams: dict, wanted: str) -> str | None:
    if wanted in streams:
        return wanted
    keys = list(streams.keys())
    if not keys:
        return None
    # streamlink CLI understands "best"/"worst" as aliases; plugins usually don't
    # store them as dict keys directly, so map them via stream_weight.
    from streamlink.plugin.plugin import Plugin as _Plugin
    if wanted == "best":
        return max(keys, key=lambda k: _Plugin.stream_weight(k)[0])
    if wanted == "worst":
        return min(keys, key=lambda k: _Plugin.stream_weight(k)[0])
    return None


def _lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def _make_handler(target_url: str) -> type[http.server.BaseHTTPRequestHandler]:
    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()

        def log_message(self, fmt, *args):
            sys.stderr.write("[redirect] " + (fmt % args) + "\n")

    return RedirectHandler


def main() -> int:
    parser = argparse.ArgumentParser(prog="streamlink-redirect")
    parser.add_argument("url", help="video URL to resolve via a Streamlink plugin")
    parser.add_argument("stream", nargs="?", default="best", help="stream/quality name (default: best)")
    parser.add_argument("--port", type=int, default=8888, help="listen port; 0 = OS-assigned (default: 8888)")
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the local URL with the system's default handler once ready (macOS `open`)",
    )
    parser.add_argument(
        "--open-with",
        metavar="APP",
        help="open the local URL with a specific app (e.g. --open-with IINA / --open-with 'SenPlayer'); "
             "on macOS this becomes `open -a APP URL`. Overrides --open.",
    )
    parser.add_argument(
        "--scheme",
        metavar="TEMPLATE",
        help="open a URL scheme template with the resolved video URL and exit immediately "
             "(no local 302 server). Placeholders: $durl (raw URL), $edurl (percent-encoded), "
             "$bdurl (base64), $name (video title). Example: --scheme 'iina://weblink?url=$edurl'",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="exit after the first player disconnects (pairs well with --port 0 for one-shot launches)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=120,
        help="with --once, exit after this many seconds of no active/pending connections; "
             "resets every time a player connects or disconnects (default: 120)",
    )
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind host (default: 127.0.0.1; use 0.0.0.0 to expose on LAN)")
    parser.add_argument("--quiet", action="store_true", help="suppress plugin progress logs")
    args = parser.parse_args()

    if not args.quiet:
        basicConfig(stream=sys.stderr, level="info", format="[{name}] {message}", style="{")

    args.url = _clean_url(args.url)
    _notify_mac("Streamlink Redirect", "resolving " + args.stream + " ...")
    try:
        target = _resolve_url(args.url, args.stream)
    except Exception as err:
        msg = type(err).__name__ + ": " + str(err)
        print("error: " + msg, file=sys.stderr)
        _notify_mac("Streamlink Redirect failed", msg)
        return 1
    _notify_mac("Streamlink Redirect", args.stream + " ready, launching player")

    print("resolved: " + target)

    import threading as _threading
    handler = _make_handler(target)
    connections = {
        "active": 0,
        "seen": False,
        "last_active_at": 0.0,
        "lock": _threading.Lock(),
    }
    if args.once:
        handler = _wrap_lifecycle(handler, connections)

    class _ReusableServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    port = args.port
    srv = None
    for attempt in range(20):
        try:
            srv = _ReusableServer((args.host, port), handler)
            break
        except OSError as err:
            if port == 0 or attempt == 19:
                print("error: unable to bind: " + str(err), file=sys.stderr)
                _notify_mac("Streamlink Redirect failed", "bind error: " + str(err))
                return 1
            port += 1  # try the next specific port when this one is taken
    if srv is None:
        return 1

    with srv:
        bound_port = srv.server_address[1]
        local = "http://127.0.0.1:" + str(bound_port) + "/"
        print("serving 302 redirects on:")
        print("  " + local)
        for ip in _lan_ips():
            print("  http://" + ip + ":" + str(bound_port) + "/")
        sys.stdout.flush()

        if args.scheme:
            import base64
            import subprocess
            from urllib.parse import quote as _q
            substitutions = {
                "$edurl": _q(local, safe=""),
                "$bdurl": base64.b64encode(local.encode()).decode(),
                "$durl": local,
                "$name": _q(args.url.rsplit("/", 1)[-1], safe=""),
            }
            launch_url = args.scheme
            for placeholder, value in substitutions.items():
                launch_url = launch_url.replace(placeholder, value)
            print("launching: " + launch_url)
            subprocess.Popen(["open", launch_url])
        elif args.open_with:
            import subprocess
            print("launching: open -a " + args.open_with + " " + local)
            subprocess.Popen(["open", "-a", args.open_with, local])
        elif args.open:
            import subprocess
            subprocess.Popen(["open", local])

        try:
            if args.once:
                srv.timeout = 1.0
                import time as _t
                start = _t.monotonic()
                while True:
                    srv.handle_request()
                    with connections["lock"]:
                        active = connections["active"]
                        last = connections["last_active_at"]
                        seen = connections["seen"]
                    now = _t.monotonic()
                    if active > 0:
                        continue  # someone's connected, keep serving
                    # idle: exit if idle_timeout has elapsed since the last event
                    # (last disconnect, or startup if no one ever connected).
                    reference = last if seen else start
                    if now - reference >= args.idle_timeout:
                        break
                if not connections["seen"]:
                    print("no player connected within " + str(args.idle_timeout) + "s, exiting")
                    return 2
                print("idle for " + str(args.idle_timeout) + "s after last disconnect, exiting")
            else:
                srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


def _wrap_lifecycle(base_handler, state):
    import time as _t

    class TrackingHandler(base_handler):
        def setup(self):
            with state["lock"]:
                state["active"] += 1
                state["seen"] = True
                state["last_active_at"] = _t.monotonic()
            super().setup()

        def finish(self):
            try:
                super().finish()
            finally:
                with state["lock"]:
                    state["active"] -= 1
                    state["last_active_at"] = _t.monotonic()

    return TrackingHandler


if __name__ == "__main__":
    sys.exit(main())
