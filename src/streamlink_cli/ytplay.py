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


def format_selector(quality: str) -> str:
    height = _height_ceiling(quality)
    h = f"[height<={height}]"
    # M1/M2 >1080p: take vp9 webm + AAC m4a for remux mode (extract_streams
    # routes this pair to on-the-fly fMP4-HLS repackaging). Kept above the av01
    # branch so it wins; <=1080p and M3+ fall straight through to av01 sidx.
    vp9_remux = (
        f"bv*{h}[vcodec^=vp9][ext=webm]+ba[ext=m4a][protocol=https]/"
        if _remux_tier(quality) else ""
    )
    # Preference order:
    #   0.   (M1/M2 >1080p) vp9 webm + AAC -> remux mode, hw decode
    #   1/2. fMP4 pairs (av01 first: better compression + hw decode) -> sidx mode
    #   3.   any https video + m4a audio pair (some sites serve vp9-in-mp4)
    #   4.   best single format at target height (progressive file or HLS)
    #   5.   absolute best anything
    return (
        vp9_remux +
        f"bv*{h}[vcodec^=av01][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
        f"bv*{h}[vcodec^=avc1][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
        f"bv*{h}[ext=mp4][protocol=https]+ba[acodec^=mp4a][protocol=https]/"
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
               timeout: int) -> dict:
    cmd = ["yt-dlp", "--no-update", "--no-playlist", "--dump-json",
           "-f", format_selector(quality), url]
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


def extract_streams(url: str, quality: str, proxy: str | None, cookies: str | None) -> dict:
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
        data = _run_ytdlp(url, quality, primary, cookies, timeout=120)
        used_proxy = primary
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as err:
        if use_direct and proxy:
            log("direct failed (" + str(err)[:120] + "), retrying via proxy...")
            data = _run_ytdlp(url, quality, proxy, cookies, timeout=180)
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

    def request(self, rng: str | None) -> http.client.HTTPResponse:
        """Single request on the pooled (or fresh) connection. Retries once on
        a stale keep-alive socket; does NOT retry HTTP error statuses."""
        headers = dict(self.headers)
        if rng:
            headers["Range"] = rng
        for attempt in (1, 2):
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._connect()
                self._local.conn = conn
            try:
                conn.request("GET", self.path, headers=headers)
                return conn.getresponse()
            except (http.client.HTTPException, OSError):
                self._drop()
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def fetch_range(self, start: int, end: int) -> bytes:
        rng = f"bytes={start}-{end}"
        err: Exception | None = None
        for i in range(self.RETRIES):
            try:
                res = self.request(rng)
                body = res.read()
                if res.will_close:
                    self._local.conn = None
                if res.status < 400:
                    return body
                err = UpstreamHTTPError(res.status)
            except (http.client.HTTPException, OSError) as exc:
                err = exc
            # bad exit (403) or a dropped connection: get a fresh exit next try
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

    HEAD_BYTES = 4 << 20      # enough to cover EBML+Info+Tracks+Cues+first cluster
    WORKERS = 3               # gentle prefetch: must not starve the foreground fetch
    AHEAD = 4                 # segments to prefetch past the last requested one
    CACHE = 96               # remuxed segments kept in memory (LRU)
    FETCH_CHUNK = 2 << 20    # foreground: split byte range across rotating-proxy exits

    def __init__(self, url: str, proxy: str | None, headers: dict | None, duration: float):
        from collections import OrderedDict
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="ytplay-vremux-")
        self.up = Upstream(url, proxy, headers)
        self.total = self._total_size()
        head = self.up.fetch_range(0, self.HEAD_BYTES - 1)
        self.seg_data, self.tcs, self.cues, self.first_cluster = self._parse(head)
        if not self.cues or self.first_cluster is None:
            raise RuntimeError("webm Cues not found in head")
        self.header = head[:self.first_cluster]
        starts = [t for t, _ in self.cues]
        ends = starts[1:] + [max(duration, starts[-1] + 2.0) if duration else starts[-1] + 4.0]
        self.segments = [(starts[i], max(0.001, ends[i] - starts[i])) for i in range(len(starts))]
        # canonical init + mp4 media timescale, from remuxing the first segment
        init, m4s0 = self._remux(self._mini(0))
        self.init_bytes = init
        self.mp4_ts = self._mdhd_timescale(init)
        self.cache: "OrderedDict[int, bytes]" = OrderedDict()
        self.cache[0] = self._patch(m4s0, self.segments[0][0])
        self.lock = threading.Lock()
        self.inflight: dict[int, threading.Event] = {}
        self.pool = ThreadPoolExecutor(max_workers=self.WORKERS)

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

    def _total_size(self):
        for i in range(Upstream.RETRIES):
            try:
                res = self.up.request("bytes=0-1"); res.read()
                if getattr(res, "will_close", False): self.up._drop()
                cr = res.headers.get("Content-Range")
                if cr and "/" in cr:
                    return int(cr.rsplit("/", 1)[1])
            except (http.client.HTTPException, OSError):
                pass
            self.up._drop()
        raise RuntimeError("could not determine webm size")

    # -- remux --------------------------------------------------------------- #
    def _mini(self, i: int, parallel: bool = True) -> bytes:
        a = self.seg_data + int(self.cues[i][1])
        b = (self.seg_data + int(self.cues[i + 1][1]) - 1) if i + 1 < len(self.cues) else (self.total - 1)
        body = self._fetch_parallel(a, b) if parallel else self.up.fetch_range(a, b)
        return self.header + body

    def _fetch_parallel(self, a: int, b: int) -> bytes:
        # Foreground only: split one segment's byte range across parallel
        # connections. The rotating proxy hands each connection its own exit, so
        # chunks download over several exits at once - faster than one stream for
        # the segment the player is waiting on. Prefetch stays single-connection
        # so it never starves this foreground fetch.
        total = b - a + 1
        if total <= self.FETCH_CHUNK:
            return self.up.fetch_range(a, b)
        n = min(6, (total + self.FETCH_CHUNK - 1) // self.FETCH_CHUNK)
        step = (total + n - 1) // n
        ranges = [(a + k * step, min(b, a + (k + 1) * step - 1)) for k in range(n)]
        ranges = [(s, e) for s, e in ranges if s <= e]
        parts: list = [b""] * len(ranges)
        errs: list = [None] * len(ranges)

        def grab(k, s, e):
            try:
                parts[k] = self.up.fetch_range(s, e)
            except Exception as exc:  # noqa: BLE001 - re-raised after join
                errs[k] = exc

        ths = [threading.Thread(target=grab, args=(k, s, e)) for k, (s, e) in enumerate(ranges)]
        for t in ths: t.start()
        for t in ths: t.join()
        for exc in errs:
            if exc:
                raise exc
        return b"".join(parts)

    def _remux(self, mini: bytes):
        import tempfile
        d = tempfile.mkdtemp(prefix="seg-", dir=self._tmp)
        src = os.path.join(d, "in.webm")
        with open(src, "wb") as f:
            f.write(mini)
        # -c copy, no -copyts: timestamps reset to 0 so the init is identical for
        # every segment (shared EXT-X-MAP); we position each via a patched tfdt.
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", src,
               "-c", "copy", "-f", "hls", "-hls_time", "99999",
               "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4",
               "-hls_segment_filename", os.path.join(d, "s%03d.m4s"),
               os.path.join(d, "i.m3u8")]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        init_p = os.path.join(d, "init.mp4")
        segs = sorted(f for f in os.listdir(d) if f.endswith(".m4s"))
        init = open(init_p, "rb").read() if os.path.exists(init_p) else b""
        m4s = b"".join(open(os.path.join(d, s), "rb").read() for s in segs)
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        if not init or not m4s:
            raise RuntimeError("segment remux produced no output")
        return init, m4s

    @staticmethod
    def _mdhd_timescale(init: bytes) -> int:
        p = init.find(b"mdhd")
        if p < 0: return 16000
        ver = init[p + 4]
        return struct.unpack_from(">I", init, p + 8 + (16 if ver == 1 else 8))[0]

    def _patch(self, m4s: bytes, seconds: float) -> bytes:
        d = bytearray(m4s); val = round(seconds * self.mp4_ts); p = 0
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
    def _produce(self, i: int, parallel: bool = True) -> bytes:
        init, m4s = self._remux(self._mini(i, parallel=parallel))
        return self._patch(m4s, self.segments[i][0])

    def get_segment(self, i: int) -> bytes | None:
        if i < 0 or i >= len(self.segments):
            return None
        with self.lock:
            if i in self.cache:
                self.cache.move_to_end(i); data = self.cache[i]
                mine = None
            else:
                ev = self.inflight.get(i)
                if ev is None:
                    ev = threading.Event(); self.inflight[i] = ev; mine = True
                else:
                    mine = False
        if mine is None:              # cache hit
            self._prefetch(i)         # (outside the lock: _prefetch takes it)
            return data
        if not mine:
            ev.wait(timeout=120)
            with self.lock:
                return self.cache.get(i)
        try:
            data = self._produce(i)
            with self.lock:
                self.cache[i] = data; self.cache.move_to_end(i)
                while len(self.cache) > self.CACHE:
                    self.cache.popitem(last=False)
            self._prefetch(i)
            return data
        except (RuntimeError, http.client.HTTPException, OSError) as err:
            log(f"vremux seg {i} failed: {err!r}")
            return None
        finally:
            with self.lock:
                self.inflight.pop(i, None)
            ev.set()

    def _prefetch(self, i: int):
        for j in range(i + 1, min(i + 1 + self.AHEAD, len(self.segments))):
            with self.lock:
                if j in self.cache or j in self.inflight:
                    continue
                ev = threading.Event(); self.inflight[j] = ev
            self.pool.submit(self._bg, j, ev)

    def _bg(self, j: int, ev: threading.Event):
        try:
            data = self._produce(j, parallel=False)  # gentle: don't starve foreground
            with self.lock:
                self.cache[j] = data; self.cache.move_to_end(j)
                while len(self.cache) > self.CACHE:
                    self.cache.popitem(last=False)
        except (RuntimeError, http.client.HTTPException, OSError):
            pass
        finally:
            with self.lock:
                self.inflight.pop(j, None)
            ev.set()

    def video_playlist(self) -> bytes:
        import math
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
        shutil.rmtree(self._tmp, ignore_errors=True)



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
                 remux: "VideoRemuxer | None" = None):
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

            playlist = playlists.get(path)
            if playlist is not None:
                self._send_body(playlist.encode(), "application/vnd.apple.mpegurl")
                return

            # remux mode video rendition: shared fMP4 init + segments repackaged
            # on demand from the webm Cues (random access -> seek anywhere fast).
            if remux and path == "/vinit.mp4":
                self._send_body(remux.init_bytes, "video/mp4")
                return
            if remux and path.startswith("/vseg"):
                m = re.match(r"/vseg(\d+)\.m4s$", path)
                if m:
                    data = remux.get_segment(int(m.group(1)))
                    if data is None:
                        self.send_error(404)
                        return
                    self._send_body(data, "video/mp4")
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
        info = extract_streams(args.url, args.quality, proxy, cookies_arg)
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

    if info["mode"] == "remux":
        # vp9 webm -> fMP4 (M1/M2 >1080p): video is repackaged on demand from the
        # webm Cues index (random access -> seek anywhere in ~1 fetch); audio is a
        # separate rendition served byte-range from the m4a sidx, exactly like sidx
        # mode. Both are complete VOD playlists -> full seek bar + edge-play.
        video, audio = info["video"], info["audio"]
        log(f"mode=remux {video.get('format_id')} {video.get('width')}x{video.get('height')} "
            f"{video.get('vcodec')} + {audio.get('format_id')} -> fMP4 random-access")
        a_up = Upstream(audio["url"], proxy, audio.get("http_headers"))
        try:
            remux = VideoRemuxer(video["url"], proxy, video.get("http_headers"),
                                 duration=info.get("duration") or 0)
            a_init, a_segs = parse_sidx(a_up)
        except Exception as err:
            log("remux setup failed: " + repr(err))
            notify_mac("ytplay failed", "remux setup: " + str(err)[:80])
            if remux:
                remux.cleanup()
            return 1
        playlists = {
            "/master.m3u8": master_playlist(video, audio),
            "/v.m3u8": remux.video_playlist().decode(),
            "/a.m3u8": media_playlist("a.mp4", a_init, a_segs),
        }
        prefetchers = {"/a.mp4": SegmentPrefetcher(a_up, a_init, a_segs, workers=1, ahead=4)}
        entry = "/master.m3u8"
        quality_note = f"{video.get('height')}p {(video.get('vcodec') or 'vp9').split('.')[0]} remux"

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
                media = video
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
        # extract and fetch -> fall back to yt-dlp-managed cache download
        try:
            probe = up.request("bytes=0-0")
            probe.read()
            probe_status = probe.status
        except Exception:
            probe_status = 599
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
                           prefetchers, remux)
    srv = RelayServer(("127.0.0.1", args.port), handler)
    local_url = f"http://127.0.0.1:{srv.server_address[1]}" + entry
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

    if args.idle_timeout:
        def reaper():
            while True:
                time.sleep(5)
                if time.time() - last_activity["t"] > args.idle_timeout:
                    log("idle timeout, exiting")
                    srv.shutdown()
                    return
        threading.Thread(target=reaper, daemon=True).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if cache is not None:
            cache.cleanup()
        if remux is not None:
            remux.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
