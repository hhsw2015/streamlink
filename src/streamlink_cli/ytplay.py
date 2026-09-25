"""ytplay: play any yt-dlp-supported video in an external player, max quality, full seek.

Three zero-transcode strategies, picked automatically per source:

  sidx        fMP4 video+audio pair (YouTube AV1/H.264 ladder up to 8K):
              parse each stream's sidx box, emit an HLS VOD playlist with
              EXT-X-BYTERANGE segments pointing into the original files.
  hls         source is already an HLS manifest (live or VOD): proxy it,
              rewriting every URI (variants, segments, keys, maps) through
              the local relay so players that can't use the system proxy
              still reach the CDN.
  progressive single muxed file: plain Range-passthrough relay; the player
              seeks natively via Range.

The relay only forwards bytes (threaded, keep-alive upstream pools); all
decode happens in the player on GPU (VideoToolbox). Nothing is re-encoded,
so "max quality" is whatever the site serves.

Usage:
    streamlink-ytplay <URL> [QUALITY] [--player senplayer] [--cookies FILE]

    QUALITY: best (default) | 2160p | 1440p | 1080p | 720p | ...
"""

from __future__ import annotations

import argparse
import base64
import re
import http.client
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

PLAYER_SCHEMES = {
    "senplayer": "senplayer://x-callback-url/play?url=$edurl&name=$name",
    "iina": "iina://weblink?url=$edurl",
}


def log(msg: str) -> None:
    sys.stderr.write("[ytplay] " + msg + "\n")
    sys.stderr.flush()


def notify_mac(title: str, msg: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            ["osascript", "-e", 'display notification "' + msg.replace('"', "'")[:200]
             + '" with title "' + title + '"'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Proxy detection
# --------------------------------------------------------------------------- #

def detect_proxy() -> str | None:
    """Env proxy first, then macOS system proxy (scutil)."""
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        val = os.environ.get(var)
        if val:
            return val
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            info[key.strip()] = val.strip()
    if info.get("HTTPEnable") == "1" and info.get("HTTPProxy"):
        return "http://" + info["HTTPProxy"] + ":" + info.get("HTTPPort", "8080")
    return None


# --------------------------------------------------------------------------- #
# yt-dlp extraction
# --------------------------------------------------------------------------- #

_HW_TIER: str | None = None


def _hw_decode_tier() -> str:
    """Hardware video decode capability of this machine.

    "av1": AV1 + VP9 hw decode (Apple M3+); "vp9": VP9 only (Apple M1/M2 -
    AV1 must be soft-decoded, which drops frames above 1080p); "": unknown.
    """
    global _HW_TIER
    if _HW_TIER is None:
        tier = ""
        if sys.platform == "darwin":
            try:
                brand = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                m = re.search(r"\bApple M(\d+)", brand)
                if m:
                    tier = "av1" if int(m.group(1)) >= 3 else "vp9"
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        _HW_TIER = tier
    return _HW_TIER


def _height_ceiling(quality: str) -> int:
    try:
        return int(str(quality).lower().rstrip("p"))
    except ValueError:
        return 8640  # best/max/anything unparseable -> no ceiling


def _remux_tier(quality: str) -> bool:
    """True when we must repackage vp9 to serve >1080p smoothly.

    M1/M2 have no AV1 hw decoder, so >1080p AV1 soft-decodes into a slideshow.
    The only hw-decodable >1080p codec YouTube gives an authenticated (cookie'd)
    client is VP9-in-webm, which HLS can't carry - so we remux it to fMP4 HLS.
    """
    return _height_ceiling(quality) > 1080 and _hw_decode_tier() == "vp9"


# --------------------------------------------------------------------------- #
# GPU enhance planning (AI upscale via libplacebo shaders)
# --------------------------------------------------------------------------- #

# Sustained CuNNy throughput on this class of GPU, measured on M2 Pro (19-core):
# 1080p30 -> 4K held 1.44x realtime = ~358 Mpx/s; use a conservative figure so
# the auto-planner keeps a safety margin and never lets the enhancer fall behind
# playback (which would stutter). Tune per machine if needed.
ENHANCE_MPX_PER_S = 300_000_000
ENHANCE_SHADER = "CuNNy-4x12-DS.glsl"      # quality-tier CNN luma upscaler

# Native tiers (vtenhance): VT decode -> GPU enhance -> VT HEVC encode, all
# zero-copy IOSurface. Full-chain measured on M2 Pro, 1080p60 -> 4K:
#   speed   MetalFX Spatial   88.5 fps (encoder-bound)
#   quality CuNNy CNN 2x      89.6 fps (encoder-bound - beats old ffmpeg est.)
#   max     ArtCNN C4F16 2x   61.4 fps (GPU-bound, 1.02x realtime@60)
# CuNNy/ArtCNN are exact-2x luma CNNs (no arbitrary scaling).
ENHANCE_MPX_METALFX = 550_000_000
NATIVE_TIERS = {"speed": [], "quality": ["--cunny"], "max": ["--artcnn"]}


def _vtenhance_path() -> str | None:
    """The native enhance binary (~/Dev/metalenhance), if built/installed."""
    for c in (os.environ.get("YTPLAY_VTENHANCE"),
              os.path.expanduser("~/Dev/metalenhance/.build/release/vtenhance"),
              "/usr/local/bin/vtenhance", "/opt/homebrew/bin/vtenhance"):
        if c and os.path.exists(c):
            return c
    return None


def _shader_path(name: str) -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(here, "..", "..", "contrib", "enhance-shaders", name),
              os.path.expanduser("~/.config/mpv/shaders/" + name)):
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def enhance_plan(width: int, height: int, fps: float, mode: str,
                 native: bool = False) -> tuple[int, int] | None:
    """Pick the largest output that keeps the CNN enhancer ahead of playback.

    "auto"/"cunny": scale toward 2x, but cap output pixels to the per-frame
    budget (throughput / fps) so a 60fps source lands at ~3K instead of a 4K it
    can't sustain, while a 24-30fps source gets full 4K. Returns (w, h) rounded
    to even, or None to pass through unenhanced (source already high, or the
    budget only allows a trivial <1.2x upscale that isn't worth a transcode).
    """
    if mode in ("", "off") or not width or not height:
        return None
    if height >= 1440:                     # already high; enhancing it blows the budget
        return None
    if native:
        # Native CNN tiers are exact-2x luma by construction; full-chain
        # throughput (measured 61-90fps at 4K) sustains 2x for any <=1080p60
        # source, so no budget cap is needed.
        return (width * 2, height * 2)
    fps = fps or 30.0
    src_px = width * height
    mpx = ENHANCE_MPX_METALFX if mode == "speed" else ENHANCE_MPX_PER_S
    budget_px = mpx / max(fps, 1.0)
    scale = min(2.0, (budget_px / src_px) ** 0.5)
    if scale < 1.2:
        return None
    return (int(width * scale) // 2 * 2, int(height * scale) // 2 * 2)


def format_selector(quality: str, enhance: bool = False) -> str:
    height = _height_ceiling(quality)
    h = f"[height<={height}]"
    # M1/M2 >1080p: take vp9 webm + AAC m4a for remux mode (extract_streams
    # routes this pair to on-the-fly fMP4-HLS repackaging). Kept above the av01
    # branch so it wins; <=1080p and M3+ fall straight through to av01 sidx.
    # Enhance also prefers webm at any height: the native per-segment enhance
    # tier needs the webm Cues index for random access.
    vp9_remux = (
        f"bv*{h}[vcodec^=vp9][ext=webm]+ba[ext=m4a][protocol=https]/"
        if (_remux_tier(quality) or enhance) else ""
    )
    # Enhance: prefer a muxed HLS single format over a progressive file -
    # muxed HLS goes through the native tier (HlsRemuxer, per-segment enhance,
    # fast seek); progressive would fall back to the slow sequential pipe.
    hls_muxed = f"b{h}[protocol^=m3u8]/" if enhance else ""
    # Preference order:
    #   0.   (M1/M2 >1080p or enhance) vp9 webm + AAC -> remux mode, hw decode
    #   1/2. fMP4 pairs (av01 first: better compression + hw decode) -> sidx mode
    #   3.   any https video + m4a audio pair (some sites serve vp9-in-mp4)
    #   4.   (enhance) muxed HLS single format -> native HlsRemuxer
    #   5.   best single format at target height (progressive file or HLS)
    #   6.   absolute best anything
    return (
        vp9_remux +
        f"bv*{h}[vcodec^=av01][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
        f"bv*{h}[vcodec^=avc1][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
        f"bv*{h}[ext=mp4][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
        + hls_muxed +
        f"b{h}/b"
    )


# Video-only / audio-only selectors for cache mode's dual-file DASH download.
# fMP4 only (needs sidx): av01 preferred, then avc1; no VP9 (WebM has no sidx).
def _video_selector(quality: str) -> str:
    try:
        height = int(str(quality).lower().rstrip("p"))
    except ValueError:
        height = 8640
    h = f"[height<={height}]"
    return (
        f"bv*{h}[vcodec^=av01][protocol=https]/"
        f"bv*{h}[vcodec^=avc1][protocol=https]/"
        f"bv*{h}[ext=mp4][protocol=https]"
    )


_AUDIO_SELECTOR = "ba[acodec^=mp4a][protocol=https]/ba[ext=m4a][protocol=https]/ba"


def _cookie_args(cookies: str | None) -> list[str]:
    """cookies is either a cookies.txt path or 'browser:<name>' (yt-dlp
    --cookies-from-browser). Empty -> no cookie args."""
    if not cookies:
        return []
    if cookies.startswith("browser:"):
        return ["--cookies-from-browser", cookies.split(":", 1)[1]]
    return ["--cookies", cookies]


def _run_ytdlp(url: str, quality: str, proxy: str | None, cookies: str | None,
               timeout: int, fmt: str | None = None, enhance: bool = False) -> dict:
    cmd = ["yt-dlp", "--no-update", "--no-playlist", "--dump-json",
           "-f", fmt or format_selector(quality, enhance), url]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd += _cookie_args(cookies)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        tail = (res.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise RuntimeError("yt-dlp failed: " + tail[0])
    return json.loads(res.stdout)


def _direct_reachable(url: str, timeout: float = 2.5) -> bool:
    """Quick direct TLS probe of the site host (GFW blocks fail here fast)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        import ssl
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                return True
    except OSError:
        return False


def extract_streams(url: str, quality: str, proxy: str | None, cookies: str | None,
                    enhance: bool = False) -> dict:
    """Extract streams, keeping extract IP == relay IP.

    Many CDNs (phncdn, ...) sign the requesting IP into media URLs, so the IP
    that runs yt-dlp MUST be the IP that later fetches media. Rotating-exit
    proxies break that even for proxy-extracted URLs on such sites, so:
    direct when the site is directly reachable (5s TLS probe), proxy only
    when it isn't - and whichever path extracted also relays (info["proxy"]).
    """
    use_direct = _direct_reachable(url) if proxy else True
    primary = None if use_direct else proxy
    log("extracting streams (" + ("direct" if use_direct else "proxy") + ")...")
    try:
        data = _run_ytdlp(url, quality, primary, cookies, timeout=120, enhance=enhance)
        used_proxy = primary
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as err:
        if use_direct and proxy:
            log("direct failed (" + str(err)[:120] + "), retrying via proxy...")
            data = _run_ytdlp(url, quality, proxy, cookies, timeout=180, enhance=enhance)
            used_proxy = proxy
        else:
            raise

    fmts = data.get("requested_formats")
    info = {"title": data.get("title") or "video", "duration": data.get("duration") or 0,
            "proxy": used_proxy}
    if fmts:
        video = next((f for f in fmts if f.get("vcodec") not in (None, "none")), None)
        # audio-only = no video codec (acodec itself is often None on HLS
        # renditions, where the codec isn't declared per-format)
        audio = next((f for f in fmts if f.get("vcodec") in (None, "none") and f is not video), None)
        if not video or not audio:
            raise RuntimeError("unexpected format pair from yt-dlp")
        vproto = video.get("protocol") or ""
        vcodec = video.get("vcodec") or ""
        if video.get("ext") == "webm" or vcodec.startswith("vp9"):
            # vp9 webm (M1/M2 >1080p): HLS can't carry webm, so repackage the
            # webm video + m4a audio into fMP4 HLS on the fly (lossless -c copy).
            mode = "remux"
        elif "m3u8" in vproto:
            # already-HLS fMP4 pair (e.g. vp09 mp4 HLS): proxy the playlists.
            mode = "hlspair"
        else:
            mode = "sidx"
        info.update(mode=mode, video=video, audio=audio)
        return info

    proto = data.get("protocol") or ""
    single = {k: data.get(k) for k in
              ("url", "format_id", "vcodec", "acodec", "width", "height", "tbr", "ext",
               "http_headers")}
    if "m3u8" in proto:
        info.update(mode="hls", media=single)
    else:
        info.update(mode="progressive", media=single)
    return info


# --------------------------------------------------------------------------- #
# Upstream connection pool
# --------------------------------------------------------------------------- #

class UpstreamHTTPError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"upstream HTTP {status}")
        self.status = status

class Upstream:
    """Keep-alive connection pool to one upstream URL through an HTTP proxy.

    One pooled connection per relay thread: skips a fresh TCP+TLS handshake
    per segment (~1s through a proxy), the dominant latency source.
    """

    def __init__(self, url: str, proxy: str | None, headers: dict | None = None):
        self.url = url
        parsed = urllib.parse.urlsplit(url)
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.path = parsed.path + ("?" + parsed.query if parsed.query else "")
        self.tls = parsed.scheme == "https"
        self.proxy = urllib.parse.urlsplit(proxy) if proxy else None
        # per-site headers from yt-dlp (Referer/Cookie - some CDNs 4xx without them)
        self.headers = {"User-Agent": UA, **(headers or {})}
        self._local = threading.local()

    # googlevideo signs the extractor's exit IP into the URL, but a rotating
    # proxy hands out a different exit per connection - empirically most exits
    # still pass (~1 in 10 gets a 403/timeout). So on a bad response we drop the
    # connection (forcing a fresh exit) and retry; a good keep-alive connection
    # then sticks to a working exit for subsequent ranges.
    RETRIES = 8

    def _connect(self) -> http.client.HTTPConnection:
        if self.proxy:
            cls = http.client.HTTPSConnection if self.tls else http.client.HTTPConnection
            conn = cls(self.proxy.hostname, self.proxy.port, timeout=30)
            if self.tls:
                conn.set_tunnel(self.host, self.port)
        elif self.tls:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=30)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        return conn

    def _drop(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
            self._local.conn = None

    def _retarget(self, location: str):
        """Follow a redirect: repoint this Upstream (all threads) at the new URL.

        googlevideo 302s across cache nodes; the move is durable, so updating
        the shared target is right - other threads' pooled connections to the
        old host get redirected too on their next request and land here.
        """
        new = urllib.parse.urljoin(self.url, location)
        parsed = urllib.parse.urlsplit(new)
        self.url = new
        self.host = parsed.hostname or self.host
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.path = parsed.path + ("?" + parsed.query if parsed.query else "")
        self.tls = parsed.scheme == "https"
        self._drop()  # this thread reconnects to the new host
        log(f"upstream redirect -> {self.host}")

    def request(self, rng: str | None) -> http.client.HTTPResponse:
        """Single request on the pooled (or fresh) connection. Retries once on
        a stale keep-alive socket and follows redirects (302 body is empty -
        treating it as success is how segments turn into 0-byte reads);
        does NOT retry HTTP error statuses."""
        headers = dict(self.headers)
        if rng:
            headers["Range"] = rng
        stale = 0
        redirects = 0
        while True:
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._connect()
                self._local.conn = conn
            try:
                conn.request("GET", self.path, headers=headers)
                res = conn.getresponse()
            except (http.client.HTTPException, OSError):
                self._drop()
                stale += 1
                if stale > 1:
                    raise
                continue
            if res.status in (301, 302, 303, 307, 308) and redirects < 3:
                loc = res.headers.get("Location")
                if loc:
                    try:
                        res.read()  # drain before switching targets
                    except (http.client.HTTPException, OSError):
                        pass
                    self._retarget(loc)
                    redirects += 1
                    continue
            return res

    def fetch_range(self, start: int, end: int) -> bytes:
        rng = f"bytes={start}-{end}"
        want = end - start + 1
        err: Exception | None = None
        for i in range(self.RETRIES):
            try:
                res = self.request(rng)
                body = res.read()
                if res.will_close:
                    self._local.conn = None
                if res.status < 400:
                    # Rotating-proxy exits sometimes return a truncated or empty
                    # body with a clean 2xx: verify against what the server said
                    # it was sending (Content-Range end may be EOF-clamped below
                    # our request - that clamped length is the correct one).
                    cr = res.headers.get("Content-Range", "")
                    m = re.match(r"bytes (\d+)-(\d+)/", cr)
                    expect = (int(m.group(2)) - int(m.group(1)) + 1) if m else want
                    if len(body) == expect:
                        return body
                    err = RuntimeError(
                        f"short read {len(body)}/{expect} (HTTP {res.status}, CR={cr!r})")
                else:
                    err = UpstreamHTTPError(res.status)
            except (http.client.HTTPException, OSError) as exc:
                err = exc
            # bad exit (403/short read) or dropped connection: fresh exit next try
            self._drop()
            time.sleep(min(0.4, 0.1 * (i + 1)))
        raise err or RuntimeError("fetch failed")


# --------------------------------------------------------------------------- #
# sidx parsing (fMP4 seek index)
# --------------------------------------------------------------------------- #

SIDX_HEAD_BYTES = 262144       # first fetch: covers ftyp+moov+sidx on most streams
SIDX_HEAD_MAX = 8 << 20        # give up if no sidx within the first 8 MB


def parse_sidx_blob(head: bytes) -> tuple[int, list[tuple[int, int, float]]]:
    """Parse the first sidx box out of `head`.

    Returns (data_anchor, [(abs_offset, size, duration_s), ...]) where
    data_anchor is the byte offset of the first media segment; everything
    before it is the init section (ftyp+moov+sidx).
    """
    pos = 0
    sidx_off = None
    while pos + 8 <= len(head):
        size, box = struct.unpack_from(">I4s", head, pos)
        if size == 1:
            size = struct.unpack_from(">Q", head, pos + 8)[0]
        if box == b"sidx":
            sidx_off = pos
            break
        if size < 8:
            break
        pos += size
    if sidx_off is None:
        raise ValueError("no sidx box found")

    p = sidx_off + 8
    version = head[p]
    p += 4 + 4  # version/flags + reference_ID
    timescale = struct.unpack_from(">I", head, p)[0]
    p += 4
    if version == 0:
        _ept, first_offset = struct.unpack_from(">II", head, p)
        p += 8
    else:
        _ept, first_offset = struct.unpack_from(">QQ", head, p)
        p += 16
    p += 2
    ref_count = struct.unpack_from(">H", head, p)[0]
    p += 2

    sidx_size = struct.unpack_from(">I", head, sidx_off)[0]
    offset = sidx_off + sidx_size + first_offset
    anchor = offset
    segments = []
    for _ in range(ref_count):
        word, dur = struct.unpack_from(">II", head, p)
        p += 12
        seg_size = word & 0x7FFFFFFF
        segments.append((offset, seg_size, dur / timescale))
        offset += seg_size
    return anchor, segments


def parse_sidx(upstream: Upstream) -> tuple[int, list[tuple[int, int, float]]]:
    """Fetch stream head, growing the window until sidx is complete.

    Some streams put a large moov before sidx; double the window until the
    parse succeeds or SIDX_HEAD_MAX is reached. A truncated sidx (window ends
    mid-box) surfaces as struct.error - retry bigger for that too.
    """
    size = SIDX_HEAD_BYTES
    head = upstream.fetch_range(0, size - 1)
    while True:
        try:
            return parse_sidx_blob(head)
        except (ValueError, struct.error):
            if size >= SIDX_HEAD_MAX or len(head) < size:
                raise  # full file was smaller than window, or cap reached
            head += upstream.fetch_range(size, size * 2 - 1)
            size *= 2


# --------------------------------------------------------------------------- #
# Segment prefetcher (sidx mode)
# --------------------------------------------------------------------------- #

class SegmentPrefetcher:
    """Read-ahead cache keyed by segment index.

    Two effects: parallel workers multiply throughput through proxies that
    cap per-connection speed (each worker thread owns its own upstream
    connection), and read-ahead hides per-segment round-trip latency, so the
    player finds the next segment already in RAM instead of buffering.
    """

    def __init__(self, upstream: Upstream, init_end: int, segments: list,
                 workers: int = 3, ahead: int = 8, cap_bytes: int = 192 << 20):
        from collections import OrderedDict
        self.up = upstream
        self.init_end = init_end
        self.segs = segments
        self.total = segments[-1][0] + segments[-1][1] if segments else init_end
        self.by_offset = {off: i for i, (off, _size, _dur) in enumerate(segments)}
        self.cache: dict = OrderedDict()  # seg index -> bytes; -1 = init section
        self.cached_bytes = 0
        self.cap = cap_bytes
        self.ahead = ahead
        self.lock = threading.Lock()
        self.inflight: dict[int, threading.Event] = {}
        self.pool = ThreadPoolExecutor(max_workers=workers)

    def _range_of(self, idx: int) -> tuple[int, int]:
        if idx == -1:
            return 0, self.init_end - 1
        off, size, _dur = self.segs[idx]
        return off, off + size - 1

    def _fetch(self, idx: int) -> bytes | None:
        with self.lock:
            if idx in self.cache:
                self.cache.move_to_end(idx)
                return self.cache[idx]
            event = self.inflight.get(idx)
            if event is None:
                event = threading.Event()
                self.inflight[idx] = event
                fetch_here = True
            else:
                fetch_here = False
        if not fetch_here:
            event.wait(timeout=90)
            with self.lock:
                return self.cache.get(idx)
        start, end = self._range_of(idx)
        try:
            data = self.up.fetch_range(start, end)
        except Exception as err:
            log("prefetch error seg " + str(idx) + ": " + repr(err))
            data = None
        with self.lock:
            if data is not None:
                self.cache[idx] = data
                self.cached_bytes += len(data)
                while self.cached_bytes > self.cap and len(self.cache) > 1:
                    _idx, old = self.cache.popitem(last=False)
                    self.cached_bytes -= len(old)
            self.inflight.pop(idx, None)
        event.set()
        return data

    def _schedule(self, idx: int) -> None:
        if 0 <= idx < len(self.segs):
            with self.lock:
                if idx in self.cache or idx in self.inflight:
                    return
            self.pool.submit(self._fetch, idx)

    def warm_at(self, seconds: float, count: int = 3) -> None:
        """Pre-fetch the segments covering `seconds` (video seek warms audio).

        Runs on the pool's persistent worker (warm keep-alive connection), so
        by the time the player asks for audio here it's already in RAM.
        """
        t = 0.0
        for i, (_off, _size, dur) in enumerate(self.segs):
            if t + dur > seconds:
                for j in range(i, min(i + count, len(self.segs))):
                    self._schedule(j)
                return
            t += dur

    def get(self, start: int, end: int) -> bytes | None:
        """Serve [start, end] if it is exactly the init section or one whole
        segment (the only ranges our generated playlists produce); None means
        the caller should fall back to plain passthrough."""
        if start == 0 and end == self.init_end - 1:
            idx = -1
        else:
            idx = self.by_offset.get(start)
            if idx is None:
                return None
            off, size, _dur = self.segs[idx]
            if end != off + size - 1:
                return None
        data = self._fetch(idx)
        if data is not None and idx >= 0:
            for nxt in range(idx + 1, min(idx + 1 + self.ahead, len(self.segs))):
                self._schedule(nxt)
        return data


# --------------------------------------------------------------------------- #
# HLS playlist generation (sidx mode)
# --------------------------------------------------------------------------- #

def media_playlist(path: str, init_end: int, segments: list) -> str:
    target = max(int(s[2]) + 1 for s in segments)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f'#EXT-X-MAP:URI="{path}",BYTERANGE="{init_end}@0"',
    ]
    for off, size, dur in segments:
        lines.append(f"#EXTINF:{dur:.5f},")
        lines.append(f"#EXT-X-BYTERANGE:{size}@{off}")
        lines.append(path)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def master_playlist(video: dict, audio: dict) -> str:
    vcodec = video.get("vcodec") or "avc1"
    bandwidth = int(((video.get("tbr") or 2000) + (audio.get("tbr") or 128)) * 1000)
    return "\n".join([
        "#EXTM3U",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="a.m3u8"',
        f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
        f'CODECS="{vcodec},mp4a.40.2",'
        f'RESOLUTION={video.get("width")}x{video.get("height")},AUDIO="a"',
        "v.m3u8",
    ]) + "\n"


# --------------------------------------------------------------------------- #
# HLS proxy mode (m3u8 URI rewriting)
# --------------------------------------------------------------------------- #

def _b64(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def _unb64(token: str) -> str:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()


def rewrite_m3u8(text: str, base_url: str) -> str:
    """Rewrite every URI in an HLS playlist to route through the local relay."""
    def local(url: str, playlist: bool) -> str:
        absolute = urllib.parse.urljoin(base_url, url)
        return ("/hls.m3u8?u=" if playlist else "/seg?u=") + _b64(absolute)

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            # rewrite URI="..." attributes (EXT-X-MEDIA, EXT-X-MAP, EXT-X-KEY, ...)
            if 'URI="' in stripped:
                head, _, rest = stripped.partition('URI="')
                uri, _, tail = rest.partition('"')
                is_playlist = stripped.startswith("#EXT-X-MEDIA") or stripped.startswith("#EXT-X-I-FRAME")
                stripped = head + 'URI="' + local(uri, is_playlist) + '"' + tail
            out.append(stripped)
        else:
            # bare URI line: variant (master) or segment (media). Match on the
            # path SUFFIX: googlevideo segment paths embed ".../index.m3u8/..."
            # mid-path (e.g. .../playlist/index.m3u8/govp/.../file/seg.ts).
            is_playlist = stripped.split("?", 1)[0].endswith(".m3u8")
            out.append(local(stripped, is_playlist))
    return "\n".join(out) + "\n"


class HlsFetcher:
    """Fetch playlists/segments for HLS proxy mode via one shared opener."""

    def __init__(self, proxy: str | None, headers: dict | None = None):
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = urllib.request.build_opener(*handlers)
        self.headers = {"User-Agent": UA, **(headers or {})}

    def open(self, url: str, rng: str | None = None):
        headers = dict(self.headers)
        if rng:
            headers["Range"] = rng
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=30)


# --------------------------------------------------------------------------- #
# Cache mode (blocked + IP-signed sites, e.g. phncdn behind rotating proxy)
# --------------------------------------------------------------------------- #

class CacheDownload:
    """yt-dlp downloads video-only + audio-only DASH streams to local files;
    we serve byte-range HLS from the growing files.

    Used when the media URL can't be relayed directly (IP-signed URL + rotating
    proxy exit -> 403). yt-dlp fetches through its own process (it re-extracts
    on 403), writing each fragmented-mp4 DASH stream verbatim to disk, sidx box
    intact at the head. We parse that sidx, build a byte-range HLS VOD playlist
    (full seek bar), and serve ranges from the growing files - blocking on any
    range not downloaded yet. AV1/4K preserved (no transcode), edge-play, seek.
    """

    def __init__(self, url: str, quality: str, proxy: str | None,
                 cookies: str | None, vfmt: str | None = None, afmt: str | None = None):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="ytplay-dash-")
        self.vpath = os.path.join(self.dir, "video.mp4")
        self.apath = os.path.join(self.dir, "audio.m4a")
        self.vdone = False
        self.adone = False
        # Reuse the exact format ids from the first extraction when available:
        # skips format re-selection and guarantees we download the same streams
        # we built the master playlist from. Both processes run concurrently, so
        # their extractions overlap (parallel, not serial).
        vsel = vfmt or _video_selector(quality)
        asel = afmt or _AUDIO_SELECTOR
        self.vproc = self._spawn(url, vsel, proxy, cookies, self.vpath)
        self.aproc = self._spawn(url, asel, proxy, cookies, self.apath)
        threading.Thread(target=self._wait, args=("v",), daemon=True).start()
        threading.Thread(target=self._wait, args=("a",), daemon=True).start()

    @staticmethod
    def _spawn(url, fmt, proxy, cookies, out) -> subprocess.Popen:
        # --fixup never: keep the raw fragmented DASH stream. yt-dlp's default
        # fixup remuxes DASH audio into a progressive m4a (moov+mdat, no sidx),
        # which breaks byte-range segmentation; "never" preserves the sidx.
        # --no-part: write directly to the final path (not <name>.part), so we
        # can parse the sidx and serve byte ranges while it's still growing.
        cmd = ["yt-dlp", "--no-update", "--no-playlist", "--quiet",
               "--fixup", "never", "--no-part",
               "-f", fmt, "-o", out, url]
        if proxy:
            cmd += ["--proxy", proxy]
        cmd += _cookie_args(cookies)
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wait(self, which: str):
        proc = self.vproc if which == "v" else self.aproc
        proc.wait()
        if which == "v":
            self.vdone = True
        else:
            self.adone = True
        log(f"cache dash {which} download done (rc={proc.returncode})")

    def _is_done(self, which: str) -> bool:
        return self.vdone if which == "v" else self.adone

    def path(self, which: str) -> str:
        return self.vpath if which == "v" else self.apath

    def parse_sidx(self, which: str, timeout: float = 120.0):
        """Wait for the stream head, then parse its sidx. Returns
        (init_end, segments) or raises on failure."""
        path = self.path(which)
        deadline = time.time() + timeout
        window = SIDX_HEAD_BYTES
        while True:
            try:
                have = os.path.getsize(path)
            except OSError:
                have = 0
            if have >= window or self._is_done(which):
                with open(path, "rb") as f:
                    head = f.read(max(window, SIDX_HEAD_BYTES))
                try:
                    return parse_sidx_blob(head)
                except (ValueError, struct.error):
                    if window >= SIDX_HEAD_MAX or (self._is_done(which) and have < window):
                        raise
                    window *= 2
            if time.time() > deadline:
                raise TimeoutError(f"sidx head for {which} not available")
            time.sleep(0.2)

    def cleanup(self):
        import shutil
        for p in (self.vproc, self.aproc):
            if p.poll() is None:
                p.terminate()
        shutil.rmtree(self.dir, ignore_errors=True)


class VideoRemuxer:
    """Random-access vp9(webm) -> fMP4 repackaging for smooth >1080p on M1/M2.

    YouTube gives a cookie'd client hw-decodable >1080p only as vp9-in-webm,
    which HLS can't carry. We repackage on demand: the webm Cues index (at the
    file head) gives byte offset + time for every DASH segment, so ANY segment
    can be fetched by HTTP Range and remuxed alone - random access, so seeking
    anywhere costs one segment fetch, not a wait for sequential download. Every
    segment shares one canonical fMP4 init; per-segment timing is written into a
    patched tfdt so the shared-init timeline stays continuous and SenPlayer seeks
    cleanly. Audio is a separate rendition served byte-range from the m4a sidx.
    """

    HEAD_BYTES = 512 << 10    # Cues sit at the file head (~7KB); grown if needed
    HEAD_MAX = 8 << 20
    WORKERS = 4               # background remux workers (fetch runs on chunk_pool)
    AHEAD = 14                # deep lookahead: playback downloads ~3x realtime, so
                              # the spare bandwidth builds a ~1min cushion - small
                              # and medium seeks land inside it and cost nothing
    CACHE_BYTES = 256 << 20  # remuxed segments kept in memory (LRU, byte-capped)
    FETCH_CHUNK = 3 << 20    # split byte ranges across rotating-proxy exits
    SRC_NAME = "in.webm"     # temp input name for _remux (subclass: "in.mp4")

    def __init__(self, url: str, proxy: str | None, headers: dict | None, duration: float,
                 enhance: tuple[int, int, str] | None = None):
        from collections import OrderedDict
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="ytplay-vremux-")
        # enhance = (out_w, out_h, shader_path): each segment is GPU-upscaled
        # (libplacebo) instead of -c copy remuxed. Random access + prefetch +
        # seek all come free from this class, so enhanced playback seeks like
        # raw does (per-segment on demand) instead of a sequential transcode.
        self.enhance = enhance
        # One GPU job at a time: concurrent vtenhance/ffmpeg runs would split
        # the shared hardware encoder and all finish late. The GPU always goes
        # to the waiter CLOSEST TO THE PLAYHEAD (lowest segment index, fg_want
        # first): without this, prefetch jobs grab the GPU in download-completion
        # order and the segment the player actually needs waits behind them.
        self.gpu_cv = threading.Condition()
        self.gpu_busy = False
        self.gpu_holder = -1          # segment the current GPU job serves
        self.gpu_proc = None          # its subprocess, for fg preemption
        self.gpu_waiters: set[int] = set()
        self.fg_want: set[int] = set()
        self.playhead = 0             # last foreground-requested segment
        # Sub-segment early publish: while vtenhance splits a segment into ~2s
        # sub-segments (--seg-interval), finished subs land here so a seeking
        # player starts after the FIRST sub instead of the whole segment.
        self.partial: dict[int, list[bytes]] = {}
        self.pcond = threading.Condition()
        self.up = Upstream(url, proxy, headers)
        # Source-specific index -> self.segments [(start_s, dur_s)]. Base builds
        # it from webm Cues; subclasses (sidx fMP4) override. Everything after
        # this line (enhance, sub-segments, GPU scheduling, seek, serving) is
        # source-agnostic, so any source that provides fMP4 segments gets the
        # full native path - "use native whenever the format allows".
        self._build_index(duration)
        self.cache: "OrderedDict[int, bytes]" = OrderedDict()
        self.cached_bytes = 0
        self.lock = threading.Lock()
        self.inflight: dict[int, threading.Event] = {}
        self.started: set[int] = set()
        self.gen = 0              # bumped on every foreground miss (seek signal);
                                  # queued prefetch from an older gen bails unstarted
        self.pool = ThreadPoolExecutor(max_workers=self.WORKERS)
        # Persistent chunk pool: Upstream connections are thread-local, so only
        # long-lived threads keep warm keep-alive connections across seeks
        # (fresh threads would pay the ~1s proxy TLS handshake per chunk). FIFO
        # order doubles as priority: the awaited segment's chunks enqueue first.
        # 12 workers ~ 3 segments x 4 chunks each downloading truly concurrently.
        self.chunk_pool = ThreadPoolExecutor(max_workers=12)
        # canonical init + mp4 timescale come from remuxing segment 0; run in the
        # background so server startup overlaps the player launching
        self.init_bytes = b""
        self.mp4_ts = 16000
        self._init_ready = threading.Event()
        boot_ev = threading.Event()
        self.inflight[0] = boot_ev
        self.started.add(0)
        threading.Thread(target=self._bootstrap, args=(boot_ev,), daemon=True).start()

    def _build_index(self, duration: float):
        """Webm Cues -> self.segments. Also sets self.total/header/seg_data/cues."""
        head, self.total = self._fetch_head()  # one request: head bytes + file size
        self.seg_data, self.tcs, self.cues, self.first_cluster = self._parse(head)
        while (not self.cues or self.first_cluster is None) and len(head) < min(self.HEAD_MAX, self.total):
            more = self.up.fetch_range(len(head), min(len(head) * 2, self.HEAD_MAX) - 1)
            if not more:
                break  # EOF/upstream stall: fall through to the error below
            head += more
            self.seg_data, self.tcs, self.cues, self.first_cluster = self._parse(head)
        if not self.cues or self.first_cluster is None:
            raise RuntimeError("webm Cues not found in head")
        self.header = head[:self.first_cluster]
        starts = [t for t, _ in self.cues]
        ends = starts[1:] + [max(duration, starts[-1] + 2.0) if duration else starts[-1] + 4.0]
        self.segments = [(starts[i], max(0.001, ends[i] - starts[i])) for i in range(len(starts))]

    def _bootstrap(self, ev: threading.Event):
        try:
            init, subs0 = self._remux(self._mini(0), seg_idx=0)
            self.init_bytes = init
            self.mp4_ts = self._mdhd_timescale(init)
            self._init_ready.set()
            with self.lock:
                self._store(0, self._patch_subs(subs0, self.segments[0][0]))
        except Exception as err:  # noqa: BLE001 - background thread boundary
            log("vremux bootstrap failed: " + repr(err))
        finally:
            self._init_ready.set()
            with self.lock:
                if self.inflight.get(0) is ev:
                    self.inflight.pop(0)
                self.started.discard(0)
            with self.pcond:
                self.partial.pop(0, None)
                self.pcond.notify_all()
            ev.set()

    def get_init(self) -> bytes | None:
        self._init_ready.wait(timeout=90)
        if not self.init_bytes:
            try:  # bootstrap failed: one synchronous retry
                init, _subs0 = self._remux(self._mini(0), seg_idx=0)
                self.init_bytes = init
                self.mp4_ts = self._mdhd_timescale(init)
            except Exception as err:  # noqa: BLE001 - reported to the player as 404
                log("vremux init retry failed: " + repr(err))
                return None
        return self.init_bytes

    def _store(self, i: int, subs: dict) -> None:
        """Insert into the LRU under self.lock, evicting past the byte cap.

        Values are {advertised_sub_index: bytes}; a partial production (seek
        landed mid-segment) stores only the tail it produced.
        """
        old = self.cache.pop(i, None)
        if old is not None:
            self.cached_bytes -= sum(map(len, old.values()))
        self.cache[i] = subs
        self.cached_bytes += sum(map(len, subs.values()))
        while self.cached_bytes > self.CACHE_BYTES and len(self.cache) > 1:
            _k, v = self.cache.popitem(last=False)
            self.cached_bytes -= sum(map(len, v.values()))

    # -- webm parsing -------------------------------------------------------- #
    @staticmethod
    def _rid(b, p):
        f = b[p]; m = 0x80; l = 1
        while l <= 4 and not (f & m): m >>= 1; l += 1
        return int.from_bytes(b[p:p + l], "big"), l

    @staticmethod
    def _rvint(b, p):
        f = b[p]; m = 0x80; l = 1
        while l <= 8 and not (f & m): m >>= 1; l += 1
        v = f & (m - 1)
        for i in range(1, l): v = (v << 8) | b[p + i]
        return v, l

    def _parse(self, d: bytes):
        rid, rv = self._rid, self._rvint
        eid, il = rid(d, 0); sz, sl = rv(d, il); p = il + sl + sz      # skip EBML header
        eid, il = rid(d, p); sz, sl = rv(d, p + il); seg_data = p + il + sl  # Segment
        tcs = 1000000; cues: list = []; first_cluster = None
        q = seg_data
        while q < len(d) - 4:
            xid, xil = rid(d, q)
            try: xsz, xsl = rv(d, q + xil)
            except Exception: break
            inner = q + xil + xsl
            if xid == 0x1549A966:                                     # Info
                r = inner
                while r < inner + xsz:
                    yid, yil = rid(d, r); ysz, ysl = rv(d, r + yil)
                    if yid == 0x2AD7B1:
                        tcs = int.from_bytes(d[r + yil + ysl:r + yil + ysl + ysz], "big")
                    r = r + yil + ysl + ysz
            elif xid == 0x1C53BB6B:                                   # Cues
                r = inner
                while r < inner + xsz:
                    cid, cil = rid(d, r); csz, csl = rv(d, r + cil); ci = r + cil + csl
                    if cid == 0xBB:                                   # CuePoint
                        ct = cpos = None; s = ci
                        while s < ci + csz:
                            zid, zil = rid(d, s); zsz, zsl = rv(d, s + zil)
                            if zid == 0xB3:
                                ct = int.from_bytes(d[s + zil + zsl:s + zil + zsl + zsz], "big")
                            elif zid == 0xB7:                         # CueTrackPositions
                                t2 = s + zil + zsl
                                while t2 < s + zil + zsl + zsz:
                                    wid, wil = rid(d, t2); wsz, wsl = rv(d, t2 + wil)
                                    if wid == 0xF1:
                                        cpos = int.from_bytes(d[t2 + wil + wsl:t2 + wil + wsl + wsz], "big")
                                    t2 = t2 + wil + wsl + wsz
                            s = s + zil + zsl + zsz
                        if ct is not None and cpos is not None:
                            cues.append((ct * tcs / 1e9, cpos))
                    r = ci + csz
            elif xid == 0x1F43B675:                                   # Cluster
                first_cluster = q; break
            q = inner + xsz
        return seg_data, tcs, cues, first_cluster

    def _fetch_head(self) -> tuple[bytes, int]:
        """Fetch the file head; its Content-Range also carries the total size."""
        err: Exception | None = None
        for i in range(Upstream.RETRIES):
            try:
                res = self.up.request(f"bytes=0-{self.HEAD_BYTES - 1}")
                body = res.read()
                if getattr(res, "will_close", False):
                    self.up._drop()
                if res.status < 400:
                    cr = res.headers.get("Content-Range")
                    if cr and "/" in cr:
                        return body, int(cr.rsplit("/", 1)[1])
                    err = RuntimeError("no Content-Range on head response")
                else:
                    err = UpstreamHTTPError(res.status)
            except (http.client.HTTPException, OSError) as exc:
                err = exc
            self.up._drop()
            time.sleep(min(0.4, 0.1 * (i + 1)))
        raise err or RuntimeError("could not fetch webm head")

    # -- remux --------------------------------------------------------------- #
    def _mini(self, i: int, parallel: bool = True) -> bytes:
        a = self.seg_data + int(self.cues[i][1])
        b = (self.seg_data + int(self.cues[i + 1][1]) - 1) if i + 1 < len(self.cues) else (self.total - 1)
        body = self._fetch_parallel(a, b) if parallel else self.up.fetch_range(a, b)
        return self.header + body

    HEDGE_S = 1.5             # chunk slower than this -> duplicate it to a new exit

    def _fetch_parallel(self, a: int, b: int) -> bytes:
        # Split one segment's byte range across parallel chunks on the shared
        # persistent pool: each worker thread keeps a warm keep-alive connection
        # to its own rotating-proxy exit, so chunks download over several exits
        # at once and no seek pays a fresh TLS handshake.
        from concurrent.futures import FIRST_COMPLETED, TimeoutError as FutTimeout, wait
        total = b - a + 1
        if total <= self.FETCH_CHUNK:
            return self.up.fetch_range(a, b)
        n = min(6, (total + self.FETCH_CHUNK - 1) // self.FETCH_CHUNK)
        step = (total + n - 1) // n
        ranges = [(a + k * step, min(b, a + (k + 1) * step - 1)) for k in range(n)]
        ranges = [(s, e) for s, e in ranges if s <= e]
        futures = [self.chunk_pool.submit(self.up.fetch_range, s, e) for s, e in ranges]
        parts = []
        for k, f in enumerate(futures):
            try:
                parts.append(f.result(timeout=self.HEDGE_S))
                continue
            except FutTimeout:
                pass
            # straggler: most exits finish a chunk well under HEDGE_S, so this
            # one likely landed on a slow exit. Race a duplicate on a fresh
            # connection (= fresh exit); first completed result wins. The loser
            # is left to finish quietly (byte ranges are idempotent).
            g = self.chunk_pool.submit(self.up.fetch_range, *ranges[k])
            deadline = time.time() + 60
            winner = None
            while winner is None:
                done, _ = wait({f, g}, timeout=5, return_when=FIRST_COMPLETED)
                for h in done:
                    if h.exception() is None:
                        winner = h
                        break
                else:
                    if len(done) == 2:  # both failed
                        raise done.pop().exception() or RuntimeError("chunk fetch failed")
                    if time.time() > deadline:
                        raise TimeoutError("chunk fetch timed out (both attempts)")
            parts.append(winner.result())
        return b"".join(parts)

    def _gpu_acquire(self, seg_idx: int):
        """Take the GPU in playback order.

        Among waiters, fg_want members (player is blocked on them) win; ties
        and the rest go to the lowest segment index = nearest the playhead.
        Bootstrap passes -1 and therefore always goes first.
        """
        with self.gpu_cv:
            self.gpu_waiters.add(seg_idx)
            while True:
                if not self.gpu_busy and seg_idx == self._gpu_next():
                    self.gpu_waiters.discard(seg_idx)
                    self.gpu_busy = True
                    self.gpu_holder = seg_idx
                    return
                # Seek preemption: the player is waiting on THIS segment while
                # the GPU grinds a background prefetch - kill it. The bg job
                # fails cleanly and its segment gets re-produced on demand.
                if (seg_idx >= 0 and seg_idx not in self.fg_want
                        and abs(seg_idx - self.playhead) > 8):
                    self.gpu_waiters.discard(seg_idx)
                    raise RuntimeError("stale prefetch (seeked away)")
                if (self.gpu_busy and seg_idx in self.fg_want
                        and self.gpu_holder not in self.fg_want
                        and self.gpu_holder >= 0 and self.gpu_proc is not None):
                    try:
                        self.gpu_proc.terminate()
                    except OSError:
                        pass
                    self.gpu_proc = None  # only one kill per job
                self.gpu_cv.wait(0.1)

    def _gpu_next(self) -> int:
        # racy read of fg_want/playhead is fine: worst case one stale round.
        # Priority: fg_want (player blocked), then distance from playhead -
        # NOT absolute index, or prefetch left over from an earlier seek
        # position would starve the new position (observed: seg34 beating
        # seg101 after a seek to 100).
        fg = self.gpu_waiters & self.fg_want
        if fg:
            return min(fg)
        ph = self.playhead
        return min(self.gpu_waiters, key=lambda j: (abs(j - ph), j)) \
            if self.gpu_waiters else -1

    def _gpu_release(self):
        with self.gpu_cv:
            self.gpu_busy = False
            self.gpu_holder = -1
            self.gpu_proc = None
            self.gpu_cv.notify_all()

    def _publish_partials(self, i: int, outd: str, stop: threading.Event,
                          sub_from: int = 0):
        """Poll vtenhance's output dir; publish finished sub-segments early.

        Runs while vtenhance enhances segment i. Each completed ~2s sub-segment
        is tfdt-patched and appended to self.partial[i] so get_sub() can serve
        it immediately - a seeking player resumes after the first sub instead
        of waiting for the whole segment.
        """
        seen = 0
        ts: int | None = None
        seg_start = self.segments[i][0]
        while True:
            stopped = stop.is_set()
            try:
                files = sorted(f for f in os.listdir(outd) if f.endswith(".m4s"))
            except OSError:
                files = []
            if ts is None:
                ip = os.path.join(outd, "init.mp4")
                if os.path.exists(ip):
                    try:
                        ts = self._mdhd_timescale(open(ip, "rb").read())
                    except OSError:
                        pass
            for f in files[seen:]:
                try:
                    sub = open(os.path.join(outd, f), "rb").read()
                except OSError:
                    break
                if not sub:
                    break
                local = self._read_tfdt(sub) / (ts or 90000)
                base_t = seg_start + sub_from * self.SUB_S
                patched = self._patch(sub, base_t + local, ts=ts)
                with self.pcond:
                    self.partial.setdefault(i, {})[sub_from + seen] = patched
                    self.pcond.notify_all()
                seen += 1
            if stopped:
                return
            stop.wait(0.05)

    # Native-enhance sub-segment length (s). Smaller = finer seek pipelining
    # (player starts filling its buffer after ~1s of GPU work instead of 2s);
    # GPU cost per second of video is unchanged. Keyframes every SUB_S.
    SUB_S = 1.0

    def subcount(self, i: int) -> int:
        """Advertised sub-segments for segment i (native enhance playlists).

        floor(dur/SUB_S) never exceeds what vtenhance actually emits (encoder
        keyframes are forced to SUB_S, so every boundary produces a split);
        the last advertised sub absorbs any extra actual subs.
        """
        return max(1, int(self.segments[i][1] // self.SUB_S))

    def get_sub(self, i: int, m: int) -> bytes | None:
        """Sub-segment m of segment i; serves early from partials during enhance.

        A cold request with m > 0 (seek landed mid-segment) starts production
        AT sub m (vtenhance --start): the player's first sub costs ~1 sub of
        GPU work instead of enhancing the whole head of the segment first.
        """
        if i < 0 or i >= len(self.segments):
            return None
        n = self.subcount(i)
        if m < 0 or m >= n:
            return None
        self.playhead = i
        with self.lock:
            cached = self.cache.get(i)
            producing = i in self.inflight
        if cached is None and not producing:
            threading.Thread(target=self._produce_from, args=(i, m), daemon=True).start()
        elif cached is not None:
            self._prefetch(i)
        deadline = time.time() + 120
        while time.time() < deadline:
            with self.lock:
                subs = self.cache.get(i)
                if subs is not None:
                    self.cache.move_to_end(i)
                    have_all_tail = all(k in subs for k in range(m, n))
            if subs is not None:
                if m < n - 1 and m in subs:
                    return subs[m]
                if m == n - 1 and have_all_tail:
                    # last advertised sub absorbs any extra actual subs
                    return b"".join(subs[k] for k in sorted(subs) if k >= m)
                # cached but this sub missing (partial store from a mid-segment
                # production): produce the missing head, then loop re-checks
                with self.lock:
                    refill = i not in self.inflight
                    if refill:
                        rev = threading.Event()
                        self.inflight[i] = rev
                        self.started.add(i)
                if refill:
                    threading.Thread(target=self._refill, args=(i, rev),
                                     daemon=True).start()
                time.sleep(0.3)
            else:
                with self.pcond:
                    part = self.partial.get(i)
                    if part and m in part and m < n - 1:
                        return part[m]
                    self.pcond.wait(0.5)
        return None

    def _produce_from(self, i: int, m: int):
        """Foreground production starting at sub m (claims like get_segment)."""
        with self.lock:
            if i in self.cache or i in self.started:
                return
            ev = self.inflight.get(i)
            if ev is None:
                ev = threading.Event()
                self.inflight[i] = ev
            self.started.add(i)
            self.gen += 1
            self.fg_want.add(i)
        try:
            data = self._produce(i, chain=True, sub_from=m)
            with self.lock:
                existing = self.cache.get(i)
                if existing:
                    existing.update(data)
                    data = existing
                self._store(i, data)
        except (RuntimeError, http.client.HTTPException, OSError) as err:
            log(f"vremux seg {i}+{m} failed: {err!r}")
        finally:
            with self.lock:
                self.fg_want.discard(i)
                if self.inflight.get(i) is ev:
                    self.inflight.pop(i)
                self.started.discard(i)
            with self.pcond:
                self.partial.pop(i, None)
                self.pcond.notify_all()
            ev.set()

    def _refill(self, i: int, ev: threading.Event):
        """Produce the whole segment to fill head subs missing from a partial
        (mid-segment) production; merges over what's cached."""
        try:
            data = self._produce(i, parallel=False)
            with self.lock:
                existing = self.cache.get(i) or {}
                existing.update(data)
                self._store(i, existing)
        except (RuntimeError, http.client.HTTPException, OSError) as err:
            log(f"vremux refill {i} failed: {err!r}")
        finally:
            with self.lock:
                if self.inflight.get(i) is ev:
                    self.inflight.pop(i)
                self.started.discard(i)
            with self.pcond:
                self.pcond.notify_all()
            ev.set()

    def _remux(self, mini: bytes, seg_idx: int = -1, sub_from: int = 0):
        import tempfile
        d = tempfile.mkdtemp(prefix="seg-", dir=self._tmp)
        src = os.path.join(d, self.SRC_NAME)
        with open(src, "wb") as f:
            f.write(mini)
        # No -copyts: timestamps reset to 0 so the init is identical for every
        # segment (shared EXT-X-MAP); we position each via a patched tfdt.
        if self.enhance and self.enhance[2].startswith("native"):
            # Native zero-copy enhance (vtenhance): VT decode -> MetalFX Spatial
            # AI upscale -> VT HEVC encode, every frame on IOSurface. Fast
            # enough (~80fps at 4K) that per-segment enhance keeps up with
            # playback AND random seeks - the tier ffmpeg could never reach.
            ow, oh, _ = self.enhance
            mbit = 25 if oh >= 2000 else 16 if oh >= 1400 else 10
            frag = os.path.join(d, "in.mp4")
            fcmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                    "-i", src, "-c", "copy"]
            movflags = "frag_keyframe+empty_moov+default_base_moof"
            if getattr(self, "muxed", False):
                # TS sources carry ADTS AAC; mp4 needs it converted to ASC.
                # Harmless for sources already in ASC (filter passes through).
                # delay_moov: with empty_moov the moov is written before the
                # first AAC packet, leaving esds without DecoderSpecificInfo -
                # AVFoundation then reports NO audio track (silent drop).
                fcmd += ["-bsf:a", "aac_adtstoasc"]
                movflags = "frag_keyframe+delay_moov+default_base_moof"
            fcmd += ["-f", "mp4", "-movflags", movflags, frag]
            fres = subprocess.run(fcmd, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True)
            frag_ok = fres.returncode == 0 and os.path.exists(frag) and os.path.getsize(frag) > 0
            if not frag_ok:
                tail = (fres.stderr or "").strip().splitlines()[-1:] or ["?"]
                log(f"enhance frag step failed (rc={fres.returncode}): {tail[0][:200]}")
            outd = os.path.join(d, "out")
            os.makedirs(outd, exist_ok=True)
            cmd = [_vtenhance_path() or "vtenhance", frag, outd, "--hls",
                   "--seg-interval", str(self.SUB_S), "--scale", f"{ow}x{oh}",
                   "--bitrate", str(mbit)]
            parts = self.enhance[2].split(":")   # "native:<tier>[:fps2x]"
            tier = parts[1] if len(parts) > 1 else "speed"
            cmd += NATIVE_TIERS.get(tier, [])
            if "fps2x" in parts:
                cmd += ["--fps2x"]               # ML interpolate 30 -> 60fps
            # (muxed sources serve audio as a SEPARATE rendition - SenPlayer
            # only plays EXT-X-MEDIA audio, not audio muxed in the variant - so
            # the enhance pass stays video-only here.)
            if sub_from > 0:
                # Seek landed mid-segment: enhance only from that sub onward.
                cmd += ["--start", str(sub_from * self.SUB_S)]
        elif self.enhance:
            # GPU upscale this segment (VideoToolbox decode -> Metal shader ->
            # VideoToolbox HEVC encode). Per-segment, so seeking anywhere only
            # enhances the segments actually requested.
            ow, oh, shader = self.enhance
            mbit = 25 if oh >= 2000 else 16 if oh >= 1400 else 10
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                   "-hwaccel", "videotoolbox", "-i", src,
                   "-vf", f"libplacebo=w={ow}:h={oh}:upscaler=ewa_lanczos:custom_shader_path={shader}",
                   "-c:v", "hevc_videotoolbox", "-b:v", f"{mbit}M", "-realtime", "1",
                   "-tag:v", "hvc1", "-an",
                   "-f", "hls", "-hls_time", "99999", "-hls_segment_type", "fmp4",
                   "-hls_fmp4_init_filename", "init.mp4",
                   "-hls_segment_filename", os.path.join(d, "s%03d.m4s"),
                   os.path.join(d, "i.m3u8")]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", src,
                   "-c", "copy", "-f", "hls", "-hls_time", "99999",
                   "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
                   "-hls_segment_filename", os.path.join(d, "s%03d.m4s"),
                   os.path.join(d, "i.m3u8")]
        native = self.enhance is not None and self.enhance[2].startswith("native")
        if self.enhance:
            self._gpu_acquire(seg_idx)
            stop_pub = threading.Event()
            pub = None
            if native and seg_idx >= 0:
                pub = threading.Thread(target=self._publish_partials,
                                       args=(seg_idx, outd, stop_pub, sub_from),
                                       daemon=True)
                pub.start()
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, text=True)
                with self.gpu_cv:
                    self.gpu_proc = proc
                _, perr = proc.communicate()
                res = subprocess.CompletedProcess(cmd, proc.returncode, None, perr)
            finally:
                self._gpu_release()
                stop_pub.set()
                if pub:
                    pub.join(timeout=5)
            if res.returncode != 0:
                if res.returncode == -15:  # SIGTERM = seek preempted us
                    log(f"enhance seg{seg_idx} preempted by seek")
                    raise RuntimeError("enhance preempted")
                tail = (res.stderr or "").strip().splitlines()[-1:] or ["?"]
                log(f"enhance segment failed (rc={res.returncode}): {tail[0][:200]}")
                if os.environ.get("YTPLAY_ENHANCE_DEBUG"):
                    import shutil as _sh
                    keep = f"/tmp/enh-fail-{int(time.time())}"
                    _sh.copytree(d, keep, dirs_exist_ok=True)
                    log(f"enhance debug: inputs kept at {keep}")
        else:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        srcdir = outd if (native and self.enhance) else d
        init_p = os.path.join(srcdir, "init.mp4")
        segs = sorted(f for f in os.listdir(srcdir) if f.endswith(".m4s"))
        init = open(init_p, "rb").read() if os.path.exists(init_p) else b""
        subs = [open(os.path.join(srcdir, f), "rb").read() for f in segs]
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        if not init or not subs or not all(subs):
            raise RuntimeError("segment remux produced no output")
        if not native:
            subs = [b"".join(subs)]  # ffmpeg paths emit one segment
        return init, subs

    @staticmethod
    def _mdhd_timescale(init: bytes) -> int:
        p = init.find(b"mdhd")
        if p < 0: return 16000
        ver = init[p + 4]
        return struct.unpack_from(">I", init, p + 8 + (16 if ver == 1 else 8))[0]

    def _patch(self, m4s: bytes, seconds: float, ts: int | None = None) -> bytes:
        d = bytearray(m4s); val = round(seconds * (ts or self.mp4_ts)); p = 0
        while p + 8 <= len(d):
            sz = struct.unpack_from(">I", d, p)[0]
            if d[p + 4:p + 8] == b"moof":
                q = p + 8
                while q + 8 <= p + sz:
                    s2 = struct.unpack_from(">I", d, q)[0]
                    if d[q + 4:q + 8] == b"traf":
                        r = q + 8
                        while r + 8 <= q + s2:
                            s3 = struct.unpack_from(">I", d, r)[0]
                            if d[r + 4:r + 8] == b"tfdt":
                                if d[r + 8] == 1: struct.pack_into(">Q", d, r + 12, val)
                                else: struct.pack_into(">I", d, r + 12, val)
                            r += s3
                    q += s2
            if sz < 8: break
            p += sz
        return bytes(d)

    # -- public -------------------------------------------------------------- #
    def _produce(self, i: int, parallel: bool = True, chain: bool = False,
                 sub_from: int = 0) -> dict:
        t0 = time.time()
        mini = self._mini(i, parallel=parallel)
        t_dl = time.time() - t0
        if chain:
            # Download of the NEXT segments overlaps our GPU time: by the time
            # this segment finishes enhancing, the next is ready to enhance.
            # Only foreground requests chain (window slides with playback),
            # so prefetch can't cascade unbounded.
            self._prefetch(i)
        t1 = time.time()
        init, subs = self._remux(mini, seg_idx=i, sub_from=sub_from)
        if self.enhance:
            log(f"seg{i}{'+' + str(sub_from) if sub_from else ''}: dl {t_dl:.1f}s"
                f" gpu(wait+run) {time.time() - t1:.1f}s"
                f" ({len(mini) >> 20}MB, {'fg' if parallel else 'bg'})")
        return self._patch_subs(subs, self.segments[i][0], sub_from)

    def _patch_subs(self, subs: list, seg_start: float, sub_from: int = 0) -> dict:
        """tfdt-position sub-segments; returns {advertised_index: bytes}.

        Sub boundaries come from the subs' own baseMediaDecodeTime deltas
        (vtenhance encodes from 0, or from sub_from*SUB_S with --start),
        so audio-video sync survives splitting.
        """
        base_t = seg_start + sub_from * self.SUB_S
        out = {}
        for k, sub in enumerate(subs):
            local = self._read_tfdt(sub) / self.mp4_ts  # seconds inside production
            out[sub_from + k] = self._patch(sub, base_t + local)
        return out

    @staticmethod
    def _read_tfdt(m4s: bytes) -> int:
        p = m4s.find(b"tfdt")
        if p < 0:
            return 0
        ver = m4s[p + 4]
        return struct.unpack_from(">Q" if ver == 1 else ">I", m4s, p + 8)[0]

    def get_segment(self, i: int) -> bytes | None:
        if i < 0 or i >= len(self.segments):
            return None
        with self.lock:
            if i in self.cache:
                self.cache.move_to_end(i); data = self.cache[i]
                mine = None
            else:
                # foreground miss = the player jumped here: bump the generation
                # so queued prefetch for the OLD position bails instead of
                # competing with this fetch for proxy bandwidth
                self.gen += 1
                self.playhead = i
                ev = self.inflight.get(i)
                if ev is None:
                    ev = threading.Event(); self.inflight[i] = ev
                    self.started.add(i); mine = True
                elif i not in self.started:
                    # queued prefetch that hasn't begun: claim it for the
                    # foreground (parallel fetch) instead of waiting on a slot
                    self.started.add(i); mine = True
                else:
                    mine = False
        if mine is None:              # cache hit
            self._prefetch(i)         # (outside the lock: _prefetch takes it)
            return b"".join(data[k] for k in sorted(data))
        # The player is now blocked on segment i: give whichever job produces
        # it (this thread or a background one already running) GPU priority.
        with self.lock:
            self.fg_want.add(i)
        try:
            if not mine:              # actively producing in the background
                ev.wait(timeout=120)
                with self.lock:
                    got = self.cache.get(i)
                    return (b"".join(got[k] for k in sorted(got))
                            if got is not None else None)
            try:
                data = self._produce(i, chain=True)
                with self.lock:
                    self._store(i, data)
                return b"".join(data[k] for k in sorted(data))
            except (RuntimeError, http.client.HTTPException, OSError) as err:
                log(f"vremux seg {i} failed: {err!r}")
                return None
            finally:
                with self.lock:
                    if self.inflight.get(i) is ev:
                        self.inflight.pop(i)
                    self.started.discard(i)
                with self.pcond:
                    self.partial.pop(i, None)
                    self.pcond.notify_all()
                ev.set()
        finally:
            with self.lock:
                self.fg_want.discard(i)

    def _prefetch(self, i: int, hot: int = 2):
        # After a seek the player buffers ~3 segments before resuming, so the
        # first `hot` lookahead segments matter as much as the awaited one:
        # fetch them parallel too (their chunks queue behind the foreground's
        # on the shared FIFO pool). The rest stay gentle single-connection.
        with self.lock:
            gen = self.gen
        if self.enhance:
            # fps2x sustains ~1.28x realtime, so the GPU CAN stay ahead - but it
            # must be kept continuously busy building a lead, not idle between
            # foreground requests. Prefetch a steady multi-segment lead
            # (strict playhead-order scheduling means far-ahead jobs never
            # starve the segment the player is waiting on). Downloads are ~2x
            # realtime so hot=2 keeps the GPU fed.
            ahead = 3
            hot = 2
        else:
            ahead = self.AHEAD
        for j in range(i + 1, min(i + 1 + ahead, len(self.segments))):
            with self.lock:
                if j in self.cache or j in self.inflight:
                    continue
                ev = threading.Event(); self.inflight[j] = ev
            self.pool.submit(self._bg, j, ev, gen, j - i <= hot)

    def _bg(self, j: int, ev: threading.Event, gen: int, parallel: bool = False):
        with self.lock:
            if j in self.cache or j in self.started:
                return  # foreground claimed it (or done): the owner cleans up
            if gen != self.gen:
                # stale queue entry (player seeked away): nobody will produce
                # this - release the slot so a future request starts fresh
                if self.inflight.get(j) is ev:
                    self.inflight.pop(j)
                ev.set()
                return
            self.started.add(j)
        try:
            data = self._produce(j, parallel=parallel)
            with self.lock:
                self._store(j, data)
        except (RuntimeError, http.client.HTTPException, OSError):
            pass
        finally:
            with self.lock:
                if self.inflight.get(j) is ev:
                    self.inflight.pop(j)
                self.started.discard(j)
            with self.pcond:
                self.partial.pop(j, None)
                self.pcond.notify_all()
            ev.set()

    def video_playlist(self) -> bytes:
        import math
        native = self.enhance is not None and self.enhance[2].startswith("native")
        if native:
            # Advertise ~2s sub-segments (vsegI_M.m4s): a seek needs only the
            # first sub of the target segment, not the whole enhanced segment.
            lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:3",
                     "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                     '#EXT-X-MAP:URI="vinit.mp4"']
            for i, (_, dur) in enumerate(self.segments):
                n = self.subcount(i)
                per = dur / n
                for m in range(n):
                    lines.append(f"#EXTINF:{per:.3f},")
                    lines.append(f"vseg{i}_{m}.m4s")
            lines.append("#EXT-X-ENDLIST")
            return ("\n".join(lines) + "\n").encode()
        maxdur = max((d for _, d in self.segments), default=2.0)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7",
                 f"#EXT-X-TARGETDURATION:{int(math.ceil(maxdur))}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                 '#EXT-X-MAP:URI="vinit.mp4"']
        for i, (_, dur) in enumerate(self.segments):
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"vseg{i}.m4s")
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode()

    def cleanup(self):
        import shutil
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.chunk_pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self._tmp, ignore_errors=True)


class SidxRemuxer(VideoRemuxer):
    """Native enhance for DASH fMP4 (sidx) sources - any site serving fMP4.

    The segments are already fMP4, so there is no webm->mp4 transcode: each
    init+segment concat is a valid fragmented mp4 that feeds vtenhance directly.
    Everything else (per-segment enhance, sub-segments, GPU scheduling, seek,
    audio warm) is inherited unchanged - this is the "use native whenever the
    format allows" seam.
    """
    SRC_NAME = "insrc.mp4"    # distinct from _remux's frag output "in.mp4"

    def _build_index(self, duration: float):
        self.data_anchor, segs = parse_sidx(self.up)
        if not segs:
            raise RuntimeError("sidx has no segments")
        self.sidx_init = self.up.fetch_range(0, self.data_anchor - 1)
        self.seg_ranges = [(off, off + size - 1) for off, size, _ in segs]
        starts, t = [], 0.0
        for _off, _size, d in segs:
            starts.append(t)
            t += d
        self.segments = [(starts[i], max(0.001, segs[i][2])) for i in range(len(segs))]
        self.total = self.seg_ranges[-1][1] + 1

    def _mini(self, i: int, parallel: bool = True) -> bytes:
        a, b = self.seg_ranges[i]
        body = self._fetch_parallel(a, b) if parallel else self.up.fetch_range(a, b)
        return self.sidx_init + body


class HlsRemuxer(VideoRemuxer):
    """Native enhance for MUXED HLS sources (segments carry A+V in one file).

    Segments come from an HLS media playlist (URLs, not byte ranges); each is
    fed to vtenhance with --audio so the enhanced output keeps its sound. A
    fMP4 playlist's EXT-X-MAP init is prepended; TS segments are self-contained.
    """
    SRC_NAME = "insrc.mp4"
    muxed = True              # _remux passes --audio; served as one A+V variant

    def _build_index(self, duration: float):
        proxy = self.up.proxy.geturl() if self.up.proxy else None
        hdrs = {k: v for k, v in self.up.headers.items() if k != "User-Agent"}
        self._hls = HlsFetcher(proxy, hdrs)
        base = self.up.url
        text = self._fetch_url(base).decode("utf-8", "replace")
        init_url = None
        seg_urls: list[str] = []
        durs: list[float] = []
        pending: float | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-MAP:"):
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    init_url = urllib.parse.urljoin(base, m.group(1))
            elif line.startswith("#EXTINF:"):
                try:
                    pending = float(line[8:].split(",")[0])
                except ValueError:
                    pending = 2.0
            elif line and not line.startswith("#"):
                seg_urls.append(urllib.parse.urljoin(base, line))
                durs.append(pending or 2.0)
                pending = None
        if not seg_urls:
            raise RuntimeError("HLS media playlist has no segments")
        self._seg_urls = seg_urls
        self._src_init = self._fetch_url(init_url) if init_url else b""
        starts, t = [], 0.0
        for d in durs:
            starts.append(t)
            t += d
        self.segments = [(starts[i], max(0.001, durs[i])) for i in range(len(seg_urls))]
        self.total = 0

    def _fetch_url(self, url: str) -> bytes:
        # Rotating-proxy resilience: each retry rides a fresh connection (and
        # therefore usually a fresh exit); some sites 4xx per-exit (PH 410/474).
        err: Exception | None = None
        for k in range(6):
            try:
                return self._hls.open(url).read()
            except Exception as exc:  # noqa: BLE001 - urllib raises broadly
                err = exc
                time.sleep(min(0.5, 0.15 * (k + 1)))
        raise err or RuntimeError("hls fetch failed")

    def _mini(self, i: int, parallel: bool = True) -> bytes:
        return self._src_init + self._fetch_url(self._seg_urls[i])

    # --- separate audio rendition (muxed source -> its own EXT-X-MEDIA group) --
    # SenPlayer only plays audio declared as a separate rendition, so we extract
    # the source audio per segment (ffmpeg copy, no GPU) with a canonical init
    # and per-segment tfdt, exactly like the video side.
    def _audio_extract(self, i: int):
        import tempfile, shutil
        d = tempfile.mkdtemp(prefix="aud-", dir=self._tmp)
        src = os.path.join(d, self.SRC_NAME)
        with open(src, "wb") as f:
            f.write(self._mini(i))
        outd = os.path.join(d, "out"); os.makedirs(outd)
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", src,
               "-map", "0:a:0", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
               "-f", "hls", "-hls_time", "99999", "-hls_segment_type", "fmp4",
               "-hls_flags", "delete_segments",
               "-hls_fmp4_init_filename", "init.mp4",
               "-hls_segment_filename", os.path.join(outd, "a%03d.m4s"),
               os.path.join(outd, "a.m3u8")]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ip = os.path.join(outd, "init.mp4")
        segs = sorted(f for f in os.listdir(outd) if f.endswith(".m4s"))
        init = open(ip, "rb").read() if os.path.exists(ip) else b""
        m4s = b"".join(open(os.path.join(outd, s), "rb").read() for s in segs)
        shutil.rmtree(d, ignore_errors=True)
        return init, m4s

    def audio_init(self) -> bytes:
        if getattr(self, "_a_init", None) is None:
            init, _ = self._audio_extract(0)
            self._a_init = init
            self._a_ts = self._mdhd_timescale(init)
        return self._a_init

    def audio_segment(self, i: int) -> bytes | None:
        if i < 0 or i >= len(self.segments):
            return None
        if not hasattr(self, "_a_cache"):
            from collections import OrderedDict
            self._a_cache: "OrderedDict[int, bytes]" = OrderedDict()
        with self.lock:
            if i in self._a_cache:
                self._a_cache.move_to_end(i)
                return self._a_cache[i]
        self.audio_init()  # ensure timescale known
        try:
            _init, m4s = self._audio_extract(i)
        except (RuntimeError, OSError) as err:
            log(f"audio seg {i} failed: {err!r}")
            return None
        if not m4s:
            return None
        data = self._patch(m4s, self.segments[i][0], ts=self._a_ts)
        with self.lock:
            self._a_cache[i] = data
            while len(self._a_cache) > 24:
                self._a_cache.popitem(last=False)
        return data

    def audio_playlist(self) -> bytes:
        import math
        maxdur = max((d for _, d in self.segments), default=8.0)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7",
                 f"#EXT-X-TARGETDURATION:{int(math.ceil(maxdur))}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                 '#EXT-X-MAP:URI="ainit.mp4"']
        for i, (_, dur) in enumerate(self.segments):
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"aseg{i}.m4s")
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode()


class EnhancePipe:
    """GPU AI-upscale stage in front of the player.

    yt-dlp streams the source video + audio through FIFOs into ffmpeg (yt-dlp
    handles proxy/cookies/403 for any site); ffmpeg runs a CNN luma upscaler on
    the GPU via libplacebo (VideoToolbox decode -> Metal shader -> VideoToolbox
    HEVC encode, all hardware) and emits a fresh fMP4-HLS the player consumes.
    Self-contained (does not read our byte-range relay, whose custom ranges
    ffmpeg's HLS client can't follow). Transcode is sequential: playback and
    edge-play stay smooth (it runs faster than realtime by design - see
    enhance_plan), but a large forward seek waits for the transcode to reach it.
    """

    SEG = 2.0  # forced-keyframe interval = HLS segment seconds (predictable)

    def __init__(self, url: str, quality: str, proxy: str | None, cookies: str | None,
                 out_w: int, out_h: int, shader: str, duration: float = 0):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="ytplay-enh-")
        self.m3u8 = os.path.join(self.dir, "index.m3u8")  # ffmpeg's own (unused)
        self.done = False
        self.playlist = self._build_playlist(duration)
        # Two FIFOs stream video + audio LIVE into ffmpeg. (Merging to a single
        # stdout via yt-dlp does NOT stream - it downloads both fully then muxes,
        # starving ffmpeg.) FIFOs carry an extension so ffmpeg picks the demuxer.
        # Prefer webm video: it streams through a pipe (mp4 needs to seek for its
        # moov) and vp9 hardware-decodes; av01 would software-decode and stall.
        self.vfifo = os.path.join(self.dir, "v.webm")
        self.afifo = os.path.join(self.dir, "a.m4a")
        os.mkfifo(self.vfifo)
        os.mkfifo(self.afifo)
        h = _height_ceiling(quality)
        vsel = f"bv*[height<={h}][ext=webm]/bv*[height<={h}][vcodec^=vp9]/bv*[height<={h}]"
        asel = "ba[ext=m4a]/ba[acodec^=mp4a]/ba"
        self.yv = self._ytdlp(url, vsel, proxy, cookies, self.vfifo)
        self.ya = self._ytdlp(url, asel, proxy, cookies, self.afifo)
        mbit = 25 if out_h >= 2000 else 16 if out_h >= 1400 else 10
        vf = f"libplacebo=w={out_w}:h={out_h}:upscaler=ewa_lanczos:custom_shader_path={shader}"
        fcmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                "-hwaccel", "videotoolbox", "-i", self.vfifo, "-i", self.afifo,
                "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
                "-c:v", "hevc_videotoolbox", "-b:v", f"{mbit}M", "-realtime", "1",
                # exact SEG-second GOP so segments are uniform & keyframe-aligned:
                # lets us publish a complete VOD playlist upfront (seek bar) and
                # keeps every seek landing clean (re-encode, so keyframes are real)
                "-force_key_frames", f"expr:gte(t,n_forced*{self.SEG})",
                "-tag:v", "hvc1", "-c:a", "aac", "-b:a", "192k",
                "-f", "hls", "-hls_time", str(self.SEG), "-hls_playlist_type", "vod",
                "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", os.path.join(self.dir, "seg%05d.m4s"),
                self.m3u8]
        self.proc = subprocess.Popen(fcmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._wait, daemon=True).start()

    def _build_playlist(self, duration: float) -> bytes:
        import math
        n = max(1, math.ceil(duration / self.SEG)) if duration > 0 else 1
        lines = ["#EXTM3U", "#EXT-X-VERSION:7",
                 f"#EXT-X-TARGETDURATION:{int(self.SEG) + 1}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD",
                 '#EXT-X-MAP:URI="init.mp4"']
        remaining = duration or self.SEG
        for i in range(n):
            dur = self.SEG if remaining >= self.SEG or i < n - 1 else max(0.001, remaining)
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"seg{i:05d}.m4s")
            remaining -= self.SEG
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode()

    @staticmethod
    def _ytdlp(url, fmt, proxy, cookies, out) -> subprocess.Popen:
        cmd = ["yt-dlp", "--no-update", "--no-playlist", "--quiet", "--no-part",
               "-f", fmt, "-o", out, url]
        if proxy:
            cmd += ["--proxy", proxy]
        cmd += _cookie_args(cookies)
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wait(self):
        self.proc.wait()
        self.done = True
        for p in (self.yv, self.ya):
            if p.poll() is None:
                p.terminate()
        log(f"enhance done (rc={self.proc.returncode})")

    def wait_ready(self, timeout: float = 90.0) -> bool:
        # -hls_playlist_type vod defers ffmpeg's own index.m3u8 to the end, but
        # segments + init stream out as they encode - wait on those (we serve a
        # synthetic VOD playlist, not ffmpeg's).
        deadline = time.time() + timeout
        init = os.path.join(self.dir, "init.mp4")
        seg0 = os.path.join(self.dir, "seg00000.m4s")
        while time.time() < deadline:
            if os.path.exists(init) and os.path.exists(seg0):
                return True
            if self.done:
                return os.path.exists(seg0)
            time.sleep(0.2)
        return False

    def read(self, name: str, last_activity: dict | None = None,
             timeout: float = 600.0) -> bytes | None:
        # Complete VOD playlist served upfront (full seek bar); init + segments
        # served from disk, blocking on a not-yet-transcoded segment (a forward
        # seek ahead of the sequential transcode) until it lands or ffmpeg ends.
        if name.endswith(".m3u8"):
            return self.playlist
        path = os.path.join(self.dir, os.path.basename(name))
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(self.dir):
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        return f.read()
                except OSError:
                    return None
            if self.done:
                return None
            if last_activity is not None:
                last_activity["t"] = time.time()
            time.sleep(0.2)
        return None

    def cleanup(self):
        import shutil
        for p in (getattr(self, "yv", None), getattr(self, "ya", None), self.proc):
            if p is not None and p.poll() is None:
                p.terminate()
        shutil.rmtree(self.dir, ignore_errors=True)


class LocalGrowingFile:
    """Serve byte ranges from a file yt-dlp is still writing. Same get()/total
    surface as SegmentPrefetcher so the relay handler treats both alike; a
    range past the current size blocks until the download reaches it (or the
    download ends, in which case a still-missing range returns None)."""

    def __init__(self, path: str, total: int, done: "callable"):
        self.path = path
        self.total = total
        self._done = done

    def get(self, start: int, end: int, timeout: float = 120.0) -> bytes | None:
        need = end + 1
        deadline = time.time() + timeout
        while True:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                size = 0
            if size >= need:
                break
            if self._done():
                if size >= need:
                    break
                return None  # download ended and the range never arrived
            if time.time() > deadline:
                return None
            time.sleep(0.2)
        with open(self.path, "rb") as f:
            f.seek(start)
            return f.read(end - start + 1)


# --------------------------------------------------------------------------- #
# Relay server
# --------------------------------------------------------------------------- #

def make_handler(playlists: dict[str, str], upstreams: dict[str, Upstream],
                 hls: HlsFetcher | None, last_activity: dict,
                 cache: CacheDownload | None = None,
                 prefetchers: dict[str, "SegmentPrefetcher"] | None = None,
                 remux: "VideoRemuxer | None" = None,
                 enh_holder: dict | None = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # players hammer the request log

        def _send_body(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _stream_response(self, res):
            self.send_response(getattr(res, "status", 200))
            for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                val = res.headers.get(key)
                if val:
                    self.send_header(key, val)
            if not res.headers.get("Content-Length"):
                # upstream is chunked (googlevideo HLS segments): without a
                # length the client can't find the body end on a keep-alive
                # connection and hangs until timeout - close to delimit.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = res.read(262144)
                if not chunk:
                    break
                self.wfile.write(chunk)

        def do_GET(self):
            last_activity["t"] = time.time()
            path, _, query = self.path.partition("?")
            params = urllib.parse.parse_qs(query)
            if os.environ.get("YTPLAY_REQLOG"):
                t0 = time.time()
                try:
                    return self._do_get(path, params)
                finally:
                    log(f"req {path} {time.time() - t0:.2f}s")
            return self._do_get(path, params)

        def _do_get(self, path, params):

            playlist = playlists.get(path)
            if playlist is not None:
                self._send_body(playlist.encode(), "application/vnd.apple.mpegurl")
                return

            # enhance mode: serve ffmpeg's growing enhanced HLS from its temp dir.
            enh = (enh_holder or {}).get("pipe")
            if enh and path.startswith("/enh/"):
                body = enh.read(os.path.basename(path), last_activity)
                if body is None:
                    self.send_error(404)
                    return
                ctype = ("application/vnd.apple.mpegurl" if path.endswith(".m3u8")
                         else "video/mp4")
                try:
                    self._send_body(body, ctype)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            # remux mode video rendition: shared fMP4 init + segments repackaged
            # on demand from the webm Cues (random access -> seek anywhere fast).
            if remux and path == "/vinit.mp4":
                init = remux.get_init()
                if init is None:
                    self.send_error(502)
                    return
                try:
                    self._send_body(init, "video/mp4")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            # Separate audio rendition for muxed-HLS native (HlsRemuxer).
            if remux and path == "/ainit.mp4" and hasattr(remux, "audio_init"):
                try:
                    self._send_body(remux.audio_init(), "video/mp4")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if remux and path.startswith("/aseg") and hasattr(remux, "audio_segment"):
                m = re.match(r"/aseg(\d+)\.m4s$", path)
                if m:
                    data = remux.audio_segment(int(m.group(1)))
                    if data is None:
                        self.send_error(404)
                        return
                    try:
                        self._send_body(data, "video/mp4")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
            if remux and path.startswith("/vseg"):
                m = re.match(r"/vseg(\d+)_(\d+)\.m4s$", path)
                if m:
                    si, sm = int(m.group(1)), int(m.group(2))
                    ap = (prefetchers or {}).get("/a.mp4")
                    if ap and si < len(remux.segments):
                        # warm the audio for this position on its own pool -
                        # otherwise a seek pays a cold proxy fetch for audio
                        # AFTER the video buffer is already filled
                        ap.warm_at(remux.segments[si][0] + sm * remux.SUB_S)
                    data = remux.get_sub(si, sm)
                    if data is None:
                        self.send_error(404)
                        return
                    try:
                        self._send_body(data, "video/mp4")
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                m = re.match(r"/vseg(\d+)\.m4s$", path)
                if m:
                    data = remux.get_segment(int(m.group(1)))
                    if data is None:
                        self.send_error(404)
                        return
                    try:
                        self._send_body(data, "video/mp4")
                    except (BrokenPipeError, ConnectionResetError):
                        pass  # player cancelled (normal during seeks)
                    return

            if path == "/hls.m3u8" and hls and "u" in params:
                url = _unb64(params["u"][0])
                try:
                    with hls.open(url) as res:
                        body = res.read().decode("utf-8", "replace")
                except Exception as err:
                    log("hls playlist error: " + repr(err))
                    self.send_error(502)
                    return
                self._send_body(rewrite_m3u8(body, url).encode(),
                                "application/vnd.apple.mpegurl")
                return

            if path == "/seg" and hls and "u" in params:
                try:
                    res = hls.open(_unb64(params["u"][0]), self.headers.get("Range"))
                except Exception as err:
                    log("segment error: " + repr(err))
                    self.send_error(502)
                    return
                try:
                    with res:
                        self._stream_response(res)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

            # sidx-mode media: serve whole segments from the prefetch cache
            prefetcher = (prefetchers or {}).get(path)
            rng = self.headers.get("Range")
            if prefetcher and rng and rng.startswith("bytes="):
                spec = rng[6:].split(",")[0]
                s, _, e = spec.partition("-")
                if s and e:
                    data = prefetcher.get(int(s), int(e))
                    if data is not None:
                        self.send_response(206)
                        self.send_header("Content-Type", "video/mp4")
                        self.send_header("Content-Range",
                                         f"bytes {s}-{e}/{prefetcher.total}")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Accept-Ranges", "bytes")
                        self.end_headers()
                        if self.command != "HEAD":
                            try:
                                self.wfile.write(data)
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                        return

            upstream = upstreams.get(path)
            if upstream is None:
                self.send_error(404)
                return
            # retry the initial response on a bad proxy exit (403), same as
            # fetch_range; once streaming starts we can't restart, but the 403
            # shows up on the first response so retrying here is enough
            res = None
            for _ in range(Upstream.RETRIES):
                try:
                    res = upstream.request(self.headers.get("Range"))
                    if res.status < 400:
                        break
                    res.read()
                    res = None
                except Exception as err:
                    log("upstream error: " + repr(err))
                    res = None
                upstream._drop()
                time.sleep(0.2)
            if res is None:
                try:
                    self.send_error(502)
                except OSError:
                    pass
                return
            try:
                self._stream_response(res)
            except (BrokenPipeError, ConnectionResetError):
                try:
                    res.close()
                except OSError:
                    pass

        do_HEAD = do_GET

    return Handler


class RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def launch_player(player: str, local_url: str, title: str) -> None:
    scheme = PLAYER_SCHEMES.get(player, player)  # raw templates allowed
    launch = (scheme
              .replace("$edurl", urllib.parse.quote(local_url, safe=""))
              .replace("$durl", local_url)
              .replace("$name", urllib.parse.quote(title, safe="")))
    log("launching: " + launch)
    subprocess.Popen(["open", launch])


def main() -> int:
    parser = argparse.ArgumentParser(prog="streamlink-ytplay")
    parser.add_argument("url", help="video URL (any yt-dlp-supported site)")
    parser.add_argument("quality", nargs="?", default="best",
                        help="best | 2160p | 1440p | 1080p | ... (default: best)")
    parser.add_argument("--player", default="senplayer",
                        help="senplayer | iina | raw scheme template with $edurl/$durl/$name")
    parser.add_argument("--no-open", action="store_true", help="don't launch a player, just serve")
    parser.add_argument("--port", type=int, default=0, help="listen port (default: OS-assigned)")
    parser.add_argument("--proxy", default=None,
                        help="HTTP proxy for the source site (default: auto-detect env/system)")
    parser.add_argument("--cookies", default=None, help="Netscape cookies.txt passed to yt-dlp")
    parser.add_argument("--cookies-from-browser", default=None, metavar="BROWSER",
                        help="let yt-dlp read cookies from a browser profile (chrome/safari/...)")
    parser.add_argument("--idle-timeout", type=int, default=0,
                        help="exit N seconds after the last request (0 = run until killed)")
    parser.add_argument("--enhance", default="", metavar="MODE",
                        help="GPU AI upscale: speed (MetalFX) | quality (CuNNy) | "
                             "max (ArtCNN). Empty = off.")
    args = parser.parse_args()

    proxy = args.proxy if args.proxy else detect_proxy()
    if proxy:
        log("proxy: " + proxy)

    notify_mac("ytplay", "resolving " + args.quality + " ...")
    t0 = time.time()
    cookies_arg = args.cookies
    if not cookies_arg and args.cookies_from_browser:
        cookies_arg = "browser:" + args.cookies_from_browser
    try:
        info = extract_streams(args.url, args.quality, proxy, cookies_arg,
                               enhance=(args.enhance in ("speed", "quality", "max")
                                        and _vtenhance_path() is not None))
    except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as err:
        log("error: " + str(err))
        notify_mac("ytplay failed", str(err))
        return 1

    proxy = info["proxy"]  # relay through the same path that extracted (IP-signed URLs)
    log("relay path: " + (proxy or "direct"))

    playlists: dict[str, str] = {}
    upstreams: dict[str, Upstream] = {}
    hls_fetcher: HlsFetcher | None = None
    cache: CacheDownload | None = None
    remux: VideoRemuxer | None = None
    prefetchers: dict[str, SegmentPrefetcher] = {}
    last_activity = {"t": time.time()}
    enh_holder: dict = {}

    # Enhance mode: per-segment GPU upscale with the SAME random-access engine as
    # remux (VideoRemuxer + enhance filter), so seeking anywhere only enhances the
    # requested segments - no sequential-transcode buffering. Video is webm/vp9
    # (Cues give random access); audio is a separate m4a-sidx rendition. Falls
    # back to the sequential EnhancePipe when the source has no webm/Cues.
    vsrc = info.get("video") or info.get("media") or {}
    enh_shader = _shader_path(ENHANCE_SHADER) if args.enhance else None
    enh_native_bin = (_vtenhance_path()
                      if args.enhance in ("speed", "quality", "max") else None)
    _native_ok = bool(enh_native_bin and info.get("mode") == "remux")
    enh_plan = (enhance_plan(int(vsrc.get("width") or 0), int(vsrc.get("height") or 0),
                             float(vsrc.get("fps") or 30), args.enhance,
                             native=_native_ok)
                if (enh_shader or enh_native_bin) else None)
    if args.enhance and enh_plan is None:
        log(f"enhance: source {vsrc.get('width')}x{vsrc.get('height')} not upscaled "
            f"({'no engine' if not (enh_shader or enh_native_bin) else 'already high / not worth it'}); raw mode")

    # Native tiers: when the source is a vp9 webm with Cues (remux mode) and
    # vtenhance is built, enhance per segment inside VideoRemuxer (zero-copy,
    # random access). speed=MetalFX 88fps, quality=CuNNy 89fps, max=ArtCNN
    # 61fps - all >=1x realtime@60 at 4K on M2 Pro. Sources without webm fall
    # back to the sequential ffmpeg CuNNy pipe.
    # Native whenever the format allows: remux (webm Cues) and sidx (fMP4) both
    # give per-segment random access, so both take the zero-copy native path.
    # Other modes (progressive/hls) still fall back to the sequential pipe.
    enh_native = bool(enh_plan and enh_native_bin
                      and info.get("mode") in ("remux", "sidx"))
    # Muxed HLS (single A+V rendition): native via HlsRemuxer, segments keep
    # their own audio (vtenhance --audio), served as one variant.
    enh_native_muxed = bool(enh_plan and enh_native_bin and info.get("mode") == "hls")

    if enh_plan is not None and not enh_native and not enh_native_muxed:
        ow, oh = enh_plan
        fpsv = vsrc.get("fps") or 30
        log(f"mode=enhance {vsrc.get('width')}x{vsrc.get('height')}@{fpsv} -> {ow}x{oh} CuNNy")
        # Sequential transcode: smooth playback (~1.8x realtime), forward seek
        # past the transcode waits. Per-segment random-access enhance was tried
        # and rejected - ffmpeg's per-spawn Vulkan init makes it too slow to keep
        # playback fed. Smooth + seekable + enhanced needs the native Metal tier.
        pipe = EnhancePipe(args.url, args.quality, proxy, cookies_arg, ow, oh, enh_shader,
                           duration=info.get("duration") or 0)
        if pipe.wait_ready():
            enh_holder["pipe"] = pipe
            entry = "/enh/index.m3u8"
            quality_note = f"{oh}p enhanced (CuNNy)"
        else:
            log("enhance: ffmpeg produced no output -> raw dispatch")
            pipe.cleanup()
            enh_plan = None

    if enh_plan is not None and not enh_native and not enh_native_muxed:
        pass  # enhance path set entry above; skip source-mode dispatch
    elif info["mode"] == "remux" or (enh_native and info["mode"] == "sidx"):
        # Native random-access enhance path, shared by two source shapes:
        #   remux = vp9 webm (YouTube >1080p on M1/M2): repackaged from Cues.
        #   sidx  = fMP4 pair (any site, av01/avc1/vp09-in-mp4): segments are
        #           already fMP4, fed to vtenhance with no transcode.
        # Video is repackaged/enhanced on demand per segment (seek anywhere in
        # ~1 fetch); audio is a separate rendition served byte-range from its
        # sidx. Both are complete VOD playlists -> full seek bar + edge-play.
        RemuxCls = VideoRemuxer if info["mode"] == "remux" else SidxRemuxer
        video, audio = info["video"], info["audio"]
        vr_enhance = None
        if enh_native:
            ow, oh = enh_plan
            # Auto frame interpolation: a <35fps source is doubled to ~60 so
            # every tier targets 4K60 (measured: MetalFX/CuNNy 1.28x realtime,
            # ArtCNN 46fps output). A source already >=50fps is left alone.
            src_fps = float(vsrc.get("fps") or 30)
            fps2x = src_fps < 35
            mode_str = "native:" + args.enhance + (":fps2x" if fps2x else "")
            vr_enhance = (ow, oh, mode_str)
            engine = {"speed": "MetalFX", "quality": "CuNNy", "max": "ArtCNN"}[args.enhance]
            log(f"mode={info['mode']}+native-enhance {video.get('width')}x{video.get('height')}"
                f"@{src_fps:g} -> {ow}x{oh}"
                f"{'@60(interp)' if fps2x else ''} {engine} zero-copy")
        else:
            log(f"mode=remux {video.get('format_id')} {video.get('width')}x{video.get('height')} "
                f"{video.get('vcodec')} + {audio.get('format_id')} -> fMP4 random-access")
        a_up = Upstream(audio["url"], proxy, audio.get("http_headers"))
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:  # video head ∥ audio sidx
                r_future = pool.submit(RemuxCls, video["url"], proxy,
                                       video.get("http_headers"),
                                       info.get("duration") or 0, vr_enhance)
                a_future = pool.submit(parse_sidx, a_up)
                remux = r_future.result(timeout=90)
                a_init, a_segs = a_future.result(timeout=90)
        except Exception as err:
            log("remux setup failed: " + repr(err))
            notify_mac("ytplay failed", "remux setup: " + str(err)[:80])
            if remux:
                remux.cleanup()
            return 1
        if enh_native:
            # The playlist's CODECS must describe the ENHANCED stream (hvc1),
            # not the vp9 source, or players reject the variant.
            ow, oh = enh_plan
            video = dict(video, vcodec="hvc1.2.4.L153.B0", width=ow, height=oh)
        playlists = {
            "/master.m3u8": master_playlist(video, audio),
            "/v.m3u8": remux.video_playlist().decode(),
            "/a.m3u8": media_playlist("a.mp4", a_init, a_segs),
        }
        # 2 workers: seek-warm audio fetches must not queue behind the
        # sequential read-ahead on a single connection
        prefetchers = {"/a.mp4": SegmentPrefetcher(a_up, a_init, a_segs, workers=2, ahead=4)}
        entry = "/master.m3u8"
        quality_note = (f"{enh_plan[1]}p enhanced (native)" if enh_native else
                        f"{video.get('height')}p {(video.get('vcodec') or 'vp9').split('.')[0]} remux")

    elif info["mode"] == "sidx":
        video, audio = info["video"], info["audio"]
        log(f"mode=sidx {video.get('format_id')} {video.get('width')}x{video.get('height')} "
            f"{video.get('vcodec')} + {audio.get('format_id')}")
        v_up = Upstream(video["url"], proxy, video.get("http_headers"))
        a_up = Upstream(audio["url"], proxy, audio.get("http_headers"))
        with ThreadPoolExecutor(max_workers=2) as pool:  # parallel head fetches
            v_future = pool.submit(parse_sidx, v_up)
            a_future = pool.submit(parse_sidx, a_up)
            try:
                v_init, v_segs = v_future.result(timeout=60)
                a_init, a_segs = a_future.result(timeout=60)
            except Exception as err:
                # 4xx = IP-signed URL + rotated proxy exit (googlevideo does
                # strict ip= checks in bot-flagged sessions). Same dead end as
                # Pornhub -> cache mode: yt-dlp re-extracts and downloads in
                # one process, retried whole, while we serve the growing file.
                log(f"sidx parse failed ({time.time() - t0:.1f}s): "
                    + repr(err) + " -> cache mode")
                info["mode"] = "cache-fallback"
        if info["mode"] == "cache-fallback":
            # yt-dlp downloads both DASH streams to local files; serve byte-range
            # HLS from the growing files (full seek + edge play, no transcode).
            cache = CacheDownload(args.url, args.quality, proxy, cookies_arg,
                                  vfmt=video.get("format_id"), afmt=audio.get("format_id"))
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    vf = pool.submit(cache.parse_sidx, "v")
                    af = pool.submit(cache.parse_sidx, "a")
                    v_init, v_segs = vf.result()
                    a_init, a_segs = af.result()
            except Exception as err:
                log("cache sidx parse failed: " + repr(err))
                notify_mac("ytplay failed", "cache index parse error")
                return 1
            v_total = v_segs[-1][0] + v_segs[-1][1]
            a_total = a_segs[-1][0] + a_segs[-1][1]
            playlists = {
                "/master.m3u8": master_playlist(video, audio),
                "/v.m3u8": media_playlist("v.mp4", v_init, v_segs),
                "/a.m3u8": media_playlist("a.mp4", a_init, a_segs),
            }
            prefetchers = {
                "/v.mp4": LocalGrowingFile(cache.vpath, v_total, lambda: cache.vdone),
                "/a.mp4": LocalGrowingFile(cache.apath, a_total, lambda: cache.adone),
            }
            entry = "/master.m3u8"
            quality_note = f"{video.get('height') or '?'}p cached"
        else:
            playlists = {
                "/master.m3u8": master_playlist(video, audio),
                "/v.m3u8": media_playlist("v.mp4", v_init, v_segs),
                "/a.m3u8": media_playlist("a.mp4", a_init, a_segs),
            }
            upstreams = {"/v.mp4": v_up, "/a.mp4": a_up}
            # video gets parallel read-ahead (throughput-bound); audio segments
            # are tiny so a single worker with a short horizon is plenty
            prefetchers = {
                "/v.mp4": SegmentPrefetcher(v_up, v_init, v_segs, workers=3, ahead=8),
                "/a.mp4": SegmentPrefetcher(a_up, a_init, a_segs, workers=1, ahead=4),
            }
            entry = "/master.m3u8"
            quality_note = f"{video.get('height')}p {video.get('vcodec') or ''}"

    elif info["mode"] == "hlspair":
        # vp09 HLS video + m3u8 audio (M1/M2 >1080p hw-decode path): serve a
        # local master that pairs the two proxied media playlists.
        video, audio = info["video"], info["audio"]
        log(f"mode=hlspair {video.get('format_id')} {video.get('width')}x{video.get('height')} "
            f"{video.get('vcodec')} + {audio.get('format_id')}")
        hls_fetcher = HlsFetcher(proxy, video.get("http_headers"))
        vcodec = video.get("vcodec") or "vp09"
        bandwidth = int(((video.get("tbr") or 8000) + (audio.get("tbr") or 128)) * 1000)
        playlists = {
            "/master.m3u8": "\n".join([
                "#EXTM3U",
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="audio",DEFAULT=YES,'
                'AUTOSELECT=YES,URI="/hls.m3u8?u=' + _b64(audio["url"]) + '"',
                f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
                f'CODECS="{vcodec},mp4a.40.2",'
                f'RESOLUTION={video.get("width")}x{video.get("height")},AUDIO="a"',
                "/hls.m3u8?u=" + _b64(video["url"]),
            ]) + "\n",
        }
        entry = "/master.m3u8"
        quality_note = f"{video.get('height')}p {vcodec.split('.')[0]} HLS"

    elif enh_native_muxed and info["mode"] == "hls":
        # Muxed HLS + native enhance: each segment (A+V) runs through vtenhance
        # --audio; served as one variant (no separate audio rendition).
        media = info["media"]
        ow, oh = enh_plan
        src_fps = float(media.get("fps") or 30)
        fps2x = src_fps < 35
        mode_str = "native:" + args.enhance + (":fps2x" if fps2x else "")
        engine = {"speed": "MetalFX", "quality": "CuNNy", "max": "ArtCNN"}[args.enhance]
        log(f"mode=hls+native-enhance {media.get('width')}x{media.get('height')}"
            f"@{src_fps:g} -> {ow}x{oh}{'@60(interp)' if fps2x else ''} {engine} muxed")
        try:
            remux = HlsRemuxer(media["url"], proxy, media.get("http_headers"),
                               info.get("duration") or 0, (ow, oh, mode_str))
        except Exception as err:
            log("hls native setup failed: " + repr(err))
            notify_mac("ytplay failed", "hls native: " + str(err)[:80])
            return 1
        bandwidth = int((media.get("tbr") or 6000) * 1000)
        playlists = {
            "/master.m3u8": "\n".join([
                "#EXTM3U",
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="audio",DEFAULT=YES,'
                'AUTOSELECT=YES,URI="a.m3u8"',
                f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
                f'CODECS="hvc1.2.4.L153.B0,mp4a.40.2",'
                f'RESOLUTION={ow}x{oh},AUDIO="a"',
                "v.m3u8",
            ]) + "\n",
            "/v.m3u8": remux.video_playlist().decode(),
            "/a.m3u8": remux.audio_playlist().decode(),
        }
        entry = "/master.m3u8"
        quality_note = f"{oh}p enhanced (native muxed)"

    elif info["mode"] == "hls":
        media = info["media"]
        log(f"mode=hls {media.get('format_id')} {media.get('width')}x{media.get('height')}")
        hls_fetcher = HlsFetcher(proxy, media.get("http_headers"))
        entry = "/hls.m3u8?u=" + _b64(media["url"])
        quality_note = f"{media.get('height') or '?'}p HLS"

    else:  # progressive
        media = info["media"]
        log(f"mode=progressive {media.get('format_id')} "
            f"{media.get('width')}x{media.get('height')} .{media.get('ext')}")
        up = Upstream(media["url"], proxy, media.get("http_headers"))
        # probe: IP-signed URLs die when the proxy exit rotates between
        # extract and fetch -> fall back to yt-dlp-managed cache download.
        # Retried: a single 403 may just be one bad rotating exit (~1 in 10),
        # and cache mode is expensive (full download), so don't fall in early.
        probe_status = 599
        for _ in range(3):
            try:
                probe = up.request("bytes=0-0")
                probe.read()
                probe_status = probe.status
            except Exception:
                probe_status = 599
            if probe_status < 400:
                break
            up._drop()
            time.sleep(0.2)
        if probe_status < 400:
            upstreams = {"/media": up}
            entry = "/media"
            quality_note = f"{media.get('height') or '?'}p file"
        else:
            log(f"relay probe got {probe_status} (IP-signed URL?), using cache mode")
            # dual-DASH cache: yt-dlp downloads video+audio to local files,
            # serve byte-range HLS from the growing files (seek + edge play)
            cache = CacheDownload(args.url, args.quality, proxy, cookies_arg)
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    vf = pool.submit(cache.parse_sidx, "v")
                    af = pool.submit(cache.parse_sidx, "a")
                    v_init, v_segs = vf.result()
                    a_init, a_segs = af.result()
            except Exception as err:
                log("cache sidx parse failed: " + repr(err))
                notify_mac("ytplay failed", "cache index parse error")
                return 1
            v_total = v_segs[-1][0] + v_segs[-1][1]
            a_total = a_segs[-1][0] + a_segs[-1][1]
            playlists = {
                "/master.m3u8": master_playlist(media, media),
                "/v.m3u8": media_playlist("v.mp4", v_init, v_segs),
                "/a.m3u8": media_playlist("a.mp4", a_init, a_segs),
            }
            prefetchers = {
                "/v.mp4": LocalGrowingFile(cache.vpath, v_total, lambda: cache.vdone),
                "/a.mp4": LocalGrowingFile(cache.apath, a_total, lambda: cache.adone),
            }
            entry = "/master.m3u8"
            quality_note = f"{media.get('height') or '?'}p cached"

    log(f"ready in {time.time() - t0:.1f}s")

    handler = make_handler(playlists, upstreams, hls_fetcher, last_activity, cache,
                           prefetchers, remux, enh_holder)
    srv = RelayServer(("127.0.0.1", args.port), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    local_url = f"http://127.0.0.1:{port}" + entry
    log("serving " + local_url)
    notify_mac("ytplay", quality_note + " ready, launching player")

    if not args.no_open:
        # Extension hint: players (SenPlayer) use the name's suffix to pick a
        # demuxer. HLS entries carry .m3u8 in the URL; the raw /media file mode
        # needs the hint in the name or SenPlayer can hang probing the container.
        title = info["title"]
        if entry == "/media" and info["media"].get("ext"):
            title += "." + info["media"]["ext"]
        launch_player(args.player, local_url, title)

    stop = threading.Event()
    if args.idle_timeout:
        def reaper():
            while not stop.is_set():
                time.sleep(5)
                if time.time() - last_activity["t"] > args.idle_timeout:
                    log("idle timeout, exiting")
                    stop.set()
                    return
        threading.Thread(target=reaper, daemon=True).start()

    # serve_forever runs on a daemon thread (started at bind); park here.
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        if cache is not None:
            cache.cleanup()
        if remux is not None:
            remux.cleanup()
        pipe = enh_holder.get("pipe")
        if pipe is not None:
            pipe.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
