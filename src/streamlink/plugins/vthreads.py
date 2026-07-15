"""
$description Cloud video downloader that resolves VOD content from YouTube, TikTok, Bilibili, Weibo, Kuaishou, Xiaohongshu, Douyin, Twitter/X, Instagram, Facebook, Reddit, Vimeo, Dailymotion, and other yt-dlp supported sites via vthreads.top proxy.
$url youtube.com
$url youtu.be
$url tiktok.com
$url bilibili.com
$url b23.tv
$url weibo.com
$url weibo.cn
$url kuaishou.com
$url xiaohongshu.com
$url xhslink.com
$url douyin.com
$url iesdouyin.com
$url twitter.com
$url x.com
$url instagram.com
$url facebook.com
$url fb.watch
$url reddit.com
$url redd.it
$url vimeo.com
$url dailymotion.com
$url dai.ly
$type vod
$metadata title
$metadata thumbnail
$notes Uses the vthreads.top public API. The service performs a server-side yt-dlp download+merge for adaptive streams; high-quality streams block on server-side processing before playback begins. Set the ``VTHREADS_ENDPOINT`` environment variable to override the API base URL.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
import time
from urllib.parse import urljoin

from streamlink.exceptions import PluginError, StreamError
from streamlink.logger import getLogger
from streamlink.plugin import HIGH_PRIORITY, Plugin, pluginmatcher
from streamlink.plugins import _vthreads_proxy_ips as proxy_ips
from streamlink.stream.http import HTTPStream
from streamlink.stream.stream import Stream


def _canon_cloud_quality(q: str) -> str:
    """Mirror worker.js:canonQuality so local cache keys match the cloud's dedup key."""
    s = (q or "best").lower().strip()
    if s in ("best", "smallest", "audio_only"):
        return s
    if s == "worst":
        return "smallest"
    aliases = {"fhd": "1080p", "qhd": "1440p", "2k": "1440p",
               "uhd": "2160p", "4k": "2160p", "hd": "720p", "sd": "480p"}
    if s in aliases:
        return aliases[s]
    m = re.search(r"(\d{3,4})", s)
    return f"{m.group(1)}p" if m else "best"


def _random_fake_ip() -> str:
    """Random public-looking IPv4. First octet 100-219 skips the obvious
    reserved / private ranges (0/8, 10/8, 127/8, 169.254/16, 172.16/12,
    192.168/16, 224/4, 240/4). Good enough for HTTP header rotation."""
    import random as _r
    return f"{_r.randint(100, 219)}.{_r.randint(0, 255)}.{_r.randint(0, 255)}.{_r.randint(1, 253)}"


def _guess_selected_stream_hint() -> str | None:
    """Sniff sys.argv for a stream/quality token, without knowing the streams dict yet.
    Used before extract to pick the quality for the cloud call. Returns None if unclear."""
    argv = sys.argv[1:]
    known = {"best", "worst", "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p",
             "1080p60", "720p60"}
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("--"):
            if "=" not in tok:
                skip_next = True
            continue
        if tok.startswith("-") and len(tok) > 1:
            skip_next = True
            continue
        if tok in known:
            return tok
    return None


log = getLogger(__name__)


VTHREADS_BASE = os.environ.get("VTHREADS_ENDPOINT", "https://vthreads.top").rstrip("/")
VTHREADS_REFERER = f"{VTHREADS_BASE}/zh/"

# Cloud extractor (Cloudflare Worker). Preferred when reachable — bypasses vthreads
# rate limits by fronting via CF and de-duplicating with a shared cache. Falls back
# to direct vthreads.top calls if unset, unreachable, or reports no direct_url.
CLOUD_BASE = os.environ.get("VTHREADS_CLOUD_ENDPOINT", "https://extractor.bugcf.ccwu.cc").rstrip("/")
CLOUD_TOKEN = os.environ.get("VTHREADS_CLOUD_TOKEN", "test-token-2026-extractor")
CLOUD_POLL_INTERVAL = 2.0
CLOUD_POLL_TIMEOUT = 300.0

POLL_INTERVAL = 2.0
POLL_TIMEOUT = 600.0

# Cross-process cache: same URL clicked twice in a row reuses the resolved mp4
# without hitting vthreads again. Keeps browser-integration snappy and avoids
# tripping upstream rate limits.
_URL_CACHE_TTL = 600.0
_EXTRACT_CACHE_TTL = 300.0
_CACHE_DIR = os.path.expanduser("~/.cache/streamlink-vthreads")
_URL_CACHE_FILE = os.path.join(_CACHE_DIR, "urls.json")
_EXTRACT_CACHE_FILE = os.path.join(_CACHE_DIR, "extract.json")


def _cache_load(path: str) -> dict:
    try:
        with open(path, "r") as f:
            import json as _json
            return _json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_save(path: str, data: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        import json as _json
        tmp = path + "." + str(os.getpid()) + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(data, f)
        os.replace(tmp, path)  # atomic on POSIX
    except OSError:
        pass


def _cache_get(path: str, key: str, ttl: float):
    data = _cache_load(path)
    entry = data.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > ttl:
        return None
    return value


def _cache_delete(path: str, key: str) -> None:
    data = _cache_load(path)
    if data.pop(key, None) is not None:
        _cache_save(path, data)


def _cache_put(path: str, key: str, value) -> None:
    data = _cache_load(path)
    data[key] = [time.time(), value]
    # prune expired keys opportunistically
    ttl = _URL_CACHE_TTL if path == _URL_CACHE_FILE else _EXTRACT_CACHE_TTL
    now = time.time()
    data = {k: v for k, v in data.items() if now - v[0] <= ttl}
    _cache_save(path, data)

# vthreads quality label -> streamlink standard label (drives stream_weight ordering)
QUALITY_MAP = {
    "4K Ultra HD": "2160p",
    "2K Quad HD": "1440p",
    "1080p Full HD": "1080p",
    "720p HD": "720p",
    "480p": "480p",
    "360p": "360p",
    "240p": "240p",
    "144p": "144p",
}


def _guess_selected_stream(streams: dict) -> str | None:
    """Scan sys.argv for a token that matches a stream key or the standard aliases."""
    aliases = {"best", "worst"}
    keys = set(streams.keys())
    argv = sys.argv[1:]
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("--"):
            # long flags: --key=val is self-contained; --key val consumes the next token
            if "=" not in tok:
                skip_next = True
            continue
        if tok.startswith("-") and len(tok) > 1:
            # short flags (single dash) — most streamlink short flags take a value
            skip_next = True
            continue
        if tok in keys or tok in aliases or "," in tok:
            # comma list: streamlink accepts "1080p,720p,best"
            for pick in tok.split(","):
                pick = pick.strip()
                if pick in keys or pick in aliases:
                    return pick
            continue
    return None


def _resolve_stream_key(streams: dict, wanted: str) -> str | None:
    if wanted in streams:
        return wanted
    keys = list(streams.keys())
    if not keys:
        return None
    if wanted == "best":
        return max(keys, key=lambda k: Plugin.stream_weight(k)[0])
    if wanted == "worst":
        return min(keys, key=lambda k: Plugin.stream_weight(k)[0])
    return None


def _normalize_quality(label: str) -> str:
    if label in QUALITY_MAP:
        return QUALITY_MAP[label]
    m = re.search(r"(\d+)p(?:60)?", label)
    if m:
        suffix = "60" if "60" in label else ""
        return f"{m.group(1)}p{suffix}"
    return label.lower().replace(" ", "_")


@pluginmatcher(
    # Whitelist — extending is a matter of adding hosts here (not "wants to be a yt-dlp gateway
    # for all URLs", which would slam vthreads with 20-30s probes for every navigation).
    priority=HIGH_PRIORITY,
    pattern=re.compile(
        r"""https?://
        (?:[\w-]+\.)*
        (?:
            youtube\.com/(?:watch|v/|shorts/|embed/)
            | youtu\.be/
            | tiktok\.com/
            | vm\.tiktok\.com/
            | bilibili\.com/
            | b23\.tv/
            | weibo\.(?:com|cn)/
            | kuaishou\.com/
            | xiaohongshu\.com/
            | xhslink\.com/
            | douyin\.com/
            | iesdouyin\.com/
            | twitter\.com/
            | x\.com/
            | instagram\.com/
            | facebook\.com/
            | fb\.watch/
            | reddit\.com/
            | redd\.it/
            | vimeo\.com/
            | dailymotion\.com/
            | dai\.ly/
            | pornhub\.com/
            | xvideos\.com/
            | xhamster\.com/
            | redtube\.com/
            | youporn\.com/
            | spankbang\.com/
            | twitch\.tv/(?:videos/|clip/)
            | soundcloud\.com/
            | ok\.ru/
            | rumble\.com/
            | odysee\.com/
            | bitchute\.com/
            | streamable\.com/
            | v\.qq\.com/
            | iqiyi\.com/
            | mgtv\.com/
            | youku\.com/
            | ixigua\.com/
            | ted\.com/
            | archive\.org/
        )
        """,
        re.VERBOSE | re.IGNORECASE,
    ),
)
class VThreads(Plugin):
    # Direct-path timeout: extract POST/GET on the happy path. Long enough for
    # the vthreads probe (yt-dlp on their side takes 5-20s) but short enough
    # that a hung upstream doesn't burn a minute before we fall back.
    # _try_with_proxy_ips overrides this to 8s per proxy attempt.
    _api_timeout = 15
    # In the direct path we do one quick transient retry so a single network
    # blip doesn't force a fallback. In the proxy-pool path we set this to
    # False — any error should surface immediately so the current IP gets
    # blacklisted and the outer loop rotates to the next one.
    _api_retry_transient = True

    def __init__(self, session, url):
        super().__init__(session, url)
        # Fire-and-forget prewarm of the proxy IP pool so the first fallback
        # attempt (if direct fails) doesn't pay setup latency. Safe to call every
        # time — proxy_ips.prewarm() self-guards against duplicate work.
        proxy_ips.prewarm()

    def _api_headers(self) -> dict:
        # Rotate a fake client IP across every request. vthreads' L2 daily quota
        # (30 requests/IP/day) reads the client IP from HTTP headers, not the
        # socket peer, so a fresh fake IP each call presents as a new "user"
        # and sidesteps the quota. Verified upstream: XFF=1.1.1.1 succeeded on a
        # 4th attempt where the same socket source had already been 429'd.
        fake = _random_fake_ip()
        return {
            "Accept": "*/*",
            "Referer": VTHREADS_REFERER,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            "X-Forwarded-For": fake,
            "X-Real-IP": fake,
            "CF-Connecting-IP": fake,
            "True-Client-IP": fake,
            "X-Client-IP": fake,
        }

    def _cloud_headers(self) -> dict:
        # Cloudflare's bot-fight rejects the default python-requests / streamlink UA
        # with error 1010. Send a real Chrome UA — same string used for vthreads calls.
        return {
            "X-Auth": CLOUD_TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        }

    def _cloud_extract(self, quality: str) -> dict | None:
        cloud_quality = _canon_cloud_quality(quality)
        cache_key = self.url + "|q=" + cloud_quality
        cached = _cache_get(_URL_CACHE_FILE, "cloud:" + cache_key, _URL_CACHE_TTL)
        if cached and cached.get("direct_url"):
            expires = cached.get("expires_at") or 0
            # Cloud reports expires_at in ms since epoch. Refuse entries that would
            # expire before the player has time to start (5 min buffer, same as cloud
            # cache logic in worker.js).
            if expires and expires > int(time.time() * 1000) + 300_000:
                log.info("vthreads: cloud cache hit for " + cloud_quality)
                return cached
        submit_url = CLOUD_BASE + "/extract"
        log.info("vthreads: asking cloud extractor for " + cloud_quality)
        try:
            res = self.session.http.post(
                submit_url,
                headers=self._cloud_headers(),
                json={"source_url": self.url, "quality": cloud_quality},
                timeout=30,
                retries=0,
                raise_for_status=False,
            )
        except Exception as err:
            log.info("vthreads: cloud submit error: " + type(err).__name__)
            return None
        if res.status_code == 503:
            log.info("vthreads: cloud reports all upstreams cooling down (503)")
            return None
        if res.status_code >= 400:
            log.info("vthreads: cloud submit HTTP " + str(res.status_code))
            return None
        try:
            payload = res.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("status") == "success" and payload.get("direct_url"):
            _cache_put(_URL_CACHE_FILE, "cloud:" + cache_key, payload)
            return payload
        job_id = payload.get("job_id")
        if not job_id:
            return None
        return self._cloud_poll(job_id, cache_key)

    def _cloud_poll(self, job_id: str, cache_key: str) -> dict | None:
        status_url = CLOUD_BASE + "/status/" + job_id
        result_url = CLOUD_BASE + "/result/" + job_id
        deadline = time.monotonic() + CLOUD_POLL_TIMEOUT
        last_status = ""
        last_progress = -1.0
        while time.monotonic() < deadline:
            try:
                res = self.session.http.get(status_url, headers=self._cloud_headers(),
                                            timeout=30, retries=0, raise_for_status=False)
                if res.status_code >= 500 or res.status_code == 429:
                    time.sleep(3)
                    continue
                if res.status_code >= 400:
                    return None
                info = res.json()
            except Exception:
                time.sleep(CLOUD_POLL_INTERVAL)
                continue
            if not isinstance(info, dict):
                return None
            status = str(info.get("status", ""))
            progress = float(info.get("progress") or 0)
            if status != last_status or progress - last_progress >= 10:
                log.info("vthreads: cloud " + status + " " + str(int(progress)) + "%")
                last_status = status
                last_progress = progress
            if status == "success":
                try:
                    r = self.session.http.get(result_url, headers=self._cloud_headers(),
                                              timeout=30, retries=0, raise_for_status=False)
                    if r.status_code >= 400:
                        return None
                    result = r.json()
                except Exception:
                    return None
                if isinstance(result, dict) and result.get("direct_url"):
                    _cache_put(_URL_CACHE_FILE, "cloud:" + cache_key, result)
                    return result
                return None
            if status == "failed":
                log.info("vthreads: cloud job failed: " + str(info.get("error") or ""))
                return None
            time.sleep(CLOUD_POLL_INTERVAL)
        log.info("vthreads: cloud poll timed out")
        return None

    def _streams_from_cloud(self, result: dict) -> dict:
        direct_url = result["direct_url"]
        # Default to a real Chrome UA + Referer so vthreads (behind Cloudflare bot-fight)
        # doesn't 1010-reject the request. Cloud-provided required_headers override these.
        headers = {
            "Referer": VTHREADS_REFERER,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        }
        required = result.get("required_headers") or {}
        headers.update(required)
        # Cloud returns one quality per call, so map to the label the caller asked for
        # plus a "best" alias for convenience.
        quality_label = result.get("quality") or "best"
        key = _normalize_quality(quality_label)
        stream = HTTPStream(self.session, direct_url, headers=headers)
        log.info("vthreads: cloud resolved " + key + " -> " + direct_url)
        return {key: stream, "best": stream}

    _RATE_LIMIT_BACKOFF = (0.2, 0.4, 0.8, 1.2, 1.6)  # per 429 attempt

    def _api_json(self, url: str, params: dict | None = None):
        # On 429/403/1015 we rotate fake IP headers (see _api_headers) and
        # retry with short exponential backoff — the client IP the server
        # trusts changes each iteration, so most rate limits clear within a
        # couple of tries. Transient network errors get one retry regardless.
        # Anything else (real 4xx/5xx after retries exhausted) bubbles up so
        # the outer fallback (proxy pool → cloud) can take over quickly.
        max_rate_limit_retries = len(self._RATE_LIMIT_BACKOFF) + 1
        rate_limit_attempts = 0
        transient_attempts = 0
        while True:
            try:
                res = self.session.http.get(
                    url,
                    params=params,
                    headers=self._api_headers(),
                    timeout=self._api_timeout,
                    raise_for_status=False,
                    retries=0,
                )
                if res.status_code in (429, 403, 1015):
                    if rate_limit_attempts < max_rate_limit_retries:
                        wait = self._RATE_LIMIT_BACKOFF[min(rate_limit_attempts, len(self._RATE_LIMIT_BACKOFF) - 1)]
                        log.info(
                            "vthreads: HTTP " + str(res.status_code)
                            + ", rotating fake IP and retrying in " + str(wait) + "s "
                            + "(attempt " + str(rate_limit_attempts + 1) + "/"
                            + str(max_rate_limit_retries) + ")"
                        )
                        rate_limit_attempts += 1
                        time.sleep(wait)
                        continue
                    raise PluginError("vthreads: rate limited after retries (HTTP " + str(res.status_code) + ")")
                if res.status_code >= 500 or res.status_code == 408:
                    raise PluginError("vthreads: server error (HTTP " + str(res.status_code) + ")")
                if res.status_code >= 400:
                    raise PluginError("vthreads: HTTP " + str(res.status_code))
                return res.json()
            except ValueError as err:
                raise PluginError("vthreads: non-JSON response") from err
            except PluginError as err:
                msg = str(err)
                is_transient = any(k in msg for k in ("SSL", "Max retries", "timed out", "Connection"))
                if transient_attempts == 0 and is_transient and self._api_retry_transient:
                    log.info("vthreads: transient network error, retrying once: " + msg[:200])
                    transient_attempts += 1
                    time.sleep(1)
                    continue
                raise

    def _try_with_proxy_ips(self, max_attempts: int = 4) -> dict | None:
        """Route the extract call through a rotating proxy-IP DNS override.
        Returns extract data on success, None on total failure. Always leaves
        DNS restored to the original state — later calls (playback / merge)
        make their own decision about which path to use."""
        if not proxy_ips.enable():
            log.info("vthreads: proxy IP pool unavailable, skipping")
            return None
        # Fail fast per attempt: a dead proxy IP shouldn't burn the full 30s
        # HTTP timeout — we'd rather burn 8s × 4 attempts and move to cloud.
        # Also skip the transient-retry in _api_json so a network error on a
        # dead IP surfaces immediately and blacklists it, instead of wasting
        # another 8s retrying the same dead IP.
        original_timeout = self._api_timeout
        original_retry = self._api_retry_transient
        self._api_timeout = 8
        self._api_retry_transient = False
        last_err: Exception | None = None
        try:
            for attempt in range(max_attempts):
                ip = proxy_ips.pick()
                if not ip:
                    break
                log.info("vthreads: attempt " + str(attempt + 1) + " via proxy IP " + ip)
                try:
                    return self._extract()
                except Exception as err:
                    last_err = err
                    log.info("vthreads: proxy IP " + ip + " failed (" + type(err).__name__ + "), blacklisting")
                    proxy_ips.blacklist(ip)
                    _cache_delete(_EXTRACT_CACHE_FILE, self.url)
            if last_err:
                log.info("vthreads: all proxy attempts failed, falling through")
            return None
        finally:
            self._api_timeout = original_timeout
            self._api_retry_transient = original_retry
            proxy_ips.disable()

    def _extract(self) -> dict:
        cached = _cache_get(_EXTRACT_CACHE_FILE, self.url, _EXTRACT_CACHE_TTL)
        if cached:
            log.info("vthreads: using cached extract for " + self.url)
            return cached
        vid = secrets.token_hex(16)
        url = f"{VTHREADS_BASE}/api/extract"
        params = {"url": self.url, "lang": "zh", "vid": vid}
        log.info("vthreads: probing " + self.url + " (server-side yt-dlp, usually 20-40s)")
        payload = self._api_json(url, params=params)
        if not isinstance(payload, dict) or not payload.get("success"):
            msg = (payload or {}).get("message") if isinstance(payload, dict) else "invalid response"
            raise PluginError("vthreads: " + str(msg or "extract failed"))
        data = payload.get("data") or {}
        medias = data.get("medias") or []
        if not medias:
            raise PluginError("vthreads: no medias returned")
        _cache_put(_EXTRACT_CACHE_FILE, self.url, data)
        return data

    def _get_streams(self):
        # Order (fastest first, fall through to slower/less-reliable paths):
        #   1. direct vthreads.top — normal path, 0 extra latency
        #   2. proxy-IP pool — rotates CF-edge IPs to bypass per-source-IP limits
        #      when direct returns 429/1015. First activation adds 1-3s.
        #   3. cloud worker — our CF Worker, shared cache saves repeat probes.
        # VTHREADS_USE_PROXY_IPS=0 skips step 2. VTHREADS_SKIP_CLOUD=1 skips step 3.
        wanted_quality = _guess_selected_stream_hint() or "best"
        skip_cloud = os.environ.get("VTHREADS_SKIP_CLOUD") in ("1", "true", "yes")
        use_proxy = os.environ.get("VTHREADS_USE_PROXY_IPS", "1") in ("1", "true", "yes")

        try:
            data = self._extract()
        except PluginError as direct_err:
            data = None
            if use_proxy:
                log.info("vthreads: direct failed (" + str(direct_err) + "), trying proxy IPs")
                data = self._try_with_proxy_ips()
            if data is None:
                if skip_cloud or not CLOUD_TOKEN:
                    raise direct_err
                log.info("vthreads: proxy also failed, trying cloud")
                cloud_result = self._cloud_extract(wanted_quality)
                if cloud_result and cloud_result.get("direct_url"):
                    self.title = cloud_result.get("title") or self.title
                    return self._streams_from_cloud(cloud_result)
                raise direct_err
        self.title = data.get("title")

        streams: dict[str, Stream] = {}
        for media in data["medias"]:
            key = _normalize_quality(media["quality"])
            fmt = media.get("format", "")
            path = media["url"]
            full_url = urljoin(VTHREADS_BASE + "/", path.lstrip("/"))

            if fmt == "merge" or "/api/download_merge" in path:
                streams[key] = VThreadsMergeStream(self.session, full_url, quality=key)
            else:
                # vthreads.top sits behind Cloudflare; direct proxy fetches need a
                # real UA + Referer or CF returns 1010 (browser_signature_banned).
                streams[key] = HTTPStream(
                    self.session,
                    full_url,
                    headers={
                        "Referer": VTHREADS_REFERER,
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
                        ),
                    },
                )
            log.debug("vthreads: media " + repr(media["quality"]) + " -> " + key + " (" + (fmt or "http") + ")")

        # streamlink's Plugin.get_streams accepts a `sorting_excludes` kwarg but not the
        # user's selection, so read it from sys.argv. This lets the plugin prefetch just
        # the merge task the user is actually going to play, without a separate env var.
        wanted = os.environ.get("VTHREADS_PREFETCH") or _guess_selected_stream(streams)
        if wanted:
            target = _resolve_stream_key(streams, wanted)
            if target and isinstance(streams.get(target), VThreadsMergeStream):
                log.info("vthreads: prefetching " + target)
                try:
                    streams[target]._resolve()
                except StreamError as merge_err:
                    if skip_cloud or not CLOUD_TOKEN:
                        raise
                    log.info("vthreads: merge failed (" + str(merge_err) + "), trying cloud")
                    cloud_result = self._cloud_extract(target)
                    if cloud_result and cloud_result.get("direct_url"):
                        streams.update(self._streams_from_cloud(cloud_result))
                    else:
                        raise

        return streams


class _VThreadsTaskExpired(Exception):
    """Server GC'd our async task (~5-min TTL). Caller should re-submit."""


def _submit(session, submit_url: str, quality: str) -> str:
    log.info("vthreads: submitting " + quality + " merge task")
    fake = _random_fake_ip()
    headers = {
        "Accept": "*/*",
        "Referer": VTHREADS_REFERER,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "X-Forwarded-For": fake,
        "X-Real-IP": fake,
        "CF-Connecting-IP": fake,
        "True-Client-IP": fake,
        "X-Client-IP": fake,
    }
    try:
        res = session.http.get(submit_url, headers=headers, timeout=60, retries=0, raise_for_status=False)
        if res.status_code >= 400:
            raise StreamError("vthreads: submit HTTP " + str(res.status_code))
        payload = res.json()
    except StreamError:
        raise
    except Exception as err:
        raise StreamError("vthreads: submit failed: " + type(err).__name__) from err
    task_id = payload.get("task_id") if isinstance(payload, dict) else None
    if not task_id:
        raise StreamError("vthreads: submit returned no task_id")
    log.debug("vthreads: task_id=" + str(task_id))
    return task_id


def _wait(session, task_id: str) -> dict:
    status_url = VTHREADS_BASE + "/api/check_status/" + task_id
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

    def _hdrs():
        fake = _random_fake_ip()
        return {
            "Accept": "*/*",
            "Referer": VTHREADS_REFERER,
            "User-Agent": ua,
            "X-Forwarded-For": fake,
            "X-Real-IP": fake,
            "CF-Connecting-IP": fake,
            "True-Client-IP": fake,
            "X-Client-IP": fake,
        }

    deadline = time.monotonic() + POLL_TIMEOUT
    last_status = ""
    last_progress = -1.0
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            res = session.http.get(status_url, headers=_hdrs(), timeout=30, retries=0, raise_for_status=False)
            # L3: task GC'd (5-min TTL on the vthreads backend). Signal to the
            # caller via a distinct exception so it can re-submit and poll a
            # fresh task_id instead of giving up.
            if res.status_code == 404:
                raise _VThreadsTaskExpired("task " + task_id + " gone from server (404)")
            if res.status_code >= 500 or res.status_code == 429:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    raise StreamError("vthreads: repeated poll failure (HTTP " + str(res.status_code) + ")")
                time.sleep(POLL_INTERVAL * 2)
                continue
            info = res.json()
            # Also treat "任务不存在" / task-not-found error bodies as expired.
            if isinstance(info, dict):
                err_msg = str(info.get("error") or info.get("message") or "")
                if "任务不存在" in err_msg or "not found" in err_msg.lower() or "expired" in err_msg.lower():
                    raise _VThreadsTaskExpired("task " + task_id + " expired: " + err_msg)
            consecutive_errors = 0
        except _VThreadsTaskExpired:
            raise
        except StreamError:
            raise
        except Exception as err:
            consecutive_errors += 1
            log.info("vthreads: poll error, retry " + str(consecutive_errors) + "/5: " + type(err).__name__)
            if consecutive_errors > 5:
                raise StreamError("vthreads: status poll failed: " + type(err).__name__) from err
            time.sleep(POLL_INTERVAL * 2)
            continue
        if not isinstance(info, dict) or "status" not in info:
            raise StreamError("vthreads: invalid status response")
        status = str(info["status"])
        progress = float(info.get("progress") or 0)

        if status != last_status or progress - last_progress >= 5:
            log.info("vthreads: " + status + " " + str(int(progress)) + "%")
            last_status = status
            last_progress = progress

        if status == "SUCCESS":
            if not info.get("download_url"):
                raise StreamError("vthreads: SUCCESS without download_url")
            return info
        if status in ("FAILURE", "FAILED", "ERROR"):
            err = info.get("error") or info.get("message") or status
            raise StreamError("vthreads: task " + status + ": " + str(err))

        time.sleep(POLL_INTERVAL)

    raise StreamError("vthreads: task " + task_id + " timed out after " + str(int(POLL_TIMEOUT)) + "s")


class VThreadsMergeStream(HTTPStream):
    """Lazily submits the vthreads merge task and resolves to the final mp4 URL on first access.

    Uses shortname='http' so --player-passthrough=http lets the player fetch the resolved URL directly,
    avoiding streamlink acting as an in-flight HTTP proxy (which some players mishandle).
    """

    __shortname__ = "http"

    def __init__(self, session, submit_url: str, quality: str = ""):
        super().__init__(session, submit_url, headers={
            "Referer": VTHREADS_REFERER,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        })
        self._submit_url = submit_url
        self._quality = quality
        self._resolved = False

    def _resolve(self) -> None:
        if self._resolved:
            return
        cached = _cache_get(_URL_CACHE_FILE, self._submit_url, _URL_CACHE_TTL)
        if cached:
            log.info("vthreads: reusing cached url " + cached)
            self.args["url"] = cached
            self._resolved = True
            return
        # Server-side task lifetime is ~5min; if it gets GC'd mid-poll we
        # transparently re-submit (up to _MAX_TASK_RESUBMITS times).
        max_resubmits = 2
        info = None
        for _ in range(max_resubmits + 1):
            task_id = _submit(self.session, self._submit_url, self._quality)
            try:
                info = _wait(self.session, task_id)
                break
            except _VThreadsTaskExpired as err:
                log.info("vthreads: " + str(err) + " — re-submitting")
                continue
        if info is None:
            raise StreamError("vthreads: task kept expiring, giving up")
        file_url = urljoin(VTHREADS_BASE + "/", info["download_url"].lstrip("/"))
        log.info(
            "vthreads: file ready " + str(info.get("filename", "")) + " (" + str(info.get("file_size", 0)) + " bytes)",
        )
        log.info("vthreads: resolved url " + file_url)
        _cache_put(_URL_CACHE_FILE, self._submit_url, file_url)
        self.args["url"] = file_url
        self._resolved = True

    @property
    def url(self) -> str:
        self._resolve()
        return super().url

    def to_url(self) -> str:
        self._resolve()
        return super().to_url()

    def open(self):
        self._resolve()
        return super().open()


__plugin__ = VThreads
