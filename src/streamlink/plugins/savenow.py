"""
$description YouTube video/audio downloader via the savenow.to Video Download API
             (video-download-api.com). Resolves a playable direct URL for the
             requested quality and hands it to Streamlink as an HTTPStream, so
             the player gets native Range/seek.
$url youtube.com
$url youtu.be
$type vod
$metadata title
$metadata thumbnail
$notes Requires an API key from https://video-download-api.com/dashboard.
       Provide it via ``SAVENOW_API_KEY`` (or ``VDAPI_API_KEY``); when unset,
       the plugin will attempt to auto-register a free-start account
       ($1 credit ≈ 5000 requests) using the SavenowAccountPool helper.
       Override the endpoint with ``SAVENOW_ENDPOINT`` if the account has a
       custom hostname printed in its dashboard.
"""

from __future__ import annotations

import os
import re
import time

from streamlink.exceptions import PluginError
from streamlink.logger import getLogger
from streamlink.plugin import HIGH_PRIORITY, Plugin, pluginmatcher
from streamlink.plugins import _savenow_accounts as accounts
from streamlink.stream.http import HTTPStream
from streamlink.stream.stream import Stream


log = getLogger(__name__)


SAVENOW_BASE = os.environ.get("SAVENOW_ENDPOINT", "https://p.savenow.to").rstrip("/")
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 300.0


# Streamlink stream name → savenow API format token.
# API accepts: mp3, m4a, webm, 360, 480, 720, 1080, 1440, mp44k, mp48k, 4k, 8k, ...
_QUALITY_MAP = {
    "2160p60": "mp44k",
    "2160p": "mp44k",
    "4k": "mp44k",
    "1440p60": "1440",
    "1440p": "1440",
    "2k": "1440",
    "1080p60": "1080",
    "1080p": "1080",
    "720p60": "720",
    "720p": "720",
    "480p": "480",
    "360p": "360",
    "240p": "240",
    "144p": "144",
    "audio_only": "mp3",
    "audio": "mp3",
    "best": "1080",
    "worst": "144",
}

# What we tell the caller each format resolves to. Standard streamlink weights
# apply so `best` picks the highest.
_ADVERTISED_QUALITIES = [
    ("2160p", "mp44k"),
    ("1440p", "1440"),
    ("1080p", "1080"),
    ("720p", "720"),
    ("480p", "480"),
    ("360p", "360"),
    ("144p", "144"),
    ("audio", "mp3"),
]


@pluginmatcher(
    # YouTube only — savenow explicitly supports YouTube; other platforms
    # (TikTok/Bilibili/Twitter/Vimeo/…) come back as "Failed" from the API.
    priority=HIGH_PRIORITY,
    pattern=re.compile(
        r"""https?://
        (?:[\w-]+\.)*
        (?:
            youtube\.com/(?:watch|v/|shorts/|embed/)
            | youtu\.be/
        )
        """,
        re.VERBOSE | re.IGNORECASE,
    ),
)
class Savenow(Plugin):
    def _headers(self) -> dict:
        return {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
        }

    def _submit(self, api_key: str, api_format: str) -> str:
        params = {
            "url": self.url,
            "format": api_format,
            "apikey": api_key,
            "add_info": "1",
            "allow_extended_duration": "1",
        }
        log.info("savenow: submitting %s job (format=%s)", self.url, api_format)
        try:
            res = self.session.http.get(
                f"{SAVENOW_BASE}/api/v2/download",
                params=params,
                headers=self._headers(),
                timeout=30,
                retries=0,
                raise_for_status=False,
            )
        except Exception as err:
            raise PluginError("savenow: submit failed: " + type(err).__name__) from err
        if res.status_code >= 400:
            raise PluginError("savenow: submit HTTP " + str(res.status_code))
        try:
            payload = res.json()
        except ValueError as err:
            raise PluginError("savenow: submit returned non-JSON") from err
        if not isinstance(payload, dict) or not payload.get("success"):
            msg = (payload or {}).get("text") or (payload or {}).get("error") or "no success flag"
            raise PluginError("savenow: submit rejected: " + str(msg))
        # Some responses ship the info block immediately — capture title.
        info = payload.get("info") or {}
        if info.get("title"):
            self.title = info["title"]
        job_id = payload.get("id")
        if not job_id:
            raise PluginError("savenow: submit missing id")
        return job_id

    def _wait(self, job_id: str) -> str:
        """Poll /api/progress until the job publishes a download_url. Returns
        the resolved direct URL. Raises PluginError on failure / timeout."""
        url = f"{SAVENOW_BASE}/api/progress"
        deadline = time.monotonic() + POLL_TIMEOUT
        last_progress = -1
        consecutive_errors = 0
        while time.monotonic() < deadline:
            try:
                res = self.session.http.get(
                    url,
                    params={"id": job_id},
                    headers=self._headers(),
                    timeout=30,
                    retries=0,
                    raise_for_status=False,
                )
                if res.status_code >= 500:
                    raise PluginError("HTTP " + str(res.status_code))
                if res.status_code >= 400:
                    raise PluginError("savenow: progress HTTP " + str(res.status_code))
                info = res.json()
                consecutive_errors = 0
            except PluginError:
                raise
            except Exception as err:
                consecutive_errors += 1
                if consecutive_errors > 5:
                    raise PluginError("savenow: progress polling failed: " + type(err).__name__) from err
                time.sleep(POLL_INTERVAL)
                continue
            if not isinstance(info, dict):
                raise PluginError("savenow: progress returned non-dict")
            direct = info.get("download_url")
            text = (info.get("text") or "").strip()
            progress = int(info.get("progress") or 0)
            if progress != last_progress and progress % 100 == 0:
                log.info("savenow: %s %d%%", text or "processing", progress // 10)
                last_progress = progress
            if direct:
                # Prefer the info block's title if present.
                info_block = info.get("info") or {}
                if info_block.get("title") and not self.title:
                    self.title = info_block["title"]
                return direct
            if text.lower() == "failed" or "error" in text.lower():
                raise PluginError("savenow: job failed: " + text)
            time.sleep(POLL_INTERVAL)
        raise PluginError("savenow: job " + job_id + " timed out")

    def _resolve_one(self, api_format: str) -> str | None:
        """Submit + poll for one format. Rotate/register keys on exhaustion.
        Returns None if every key attempt failed."""
        est_cost = accounts.estimated_cost(api_format, duration_s=None)
        for attempt in range(3):
            api_key = accounts.get_active_key(min_cost_usd=est_cost)
            if not api_key:
                log.info("savenow: no funded API key available (need $%.5f)", est_cost)
                return None
            log.info("savenow: using key ...%s (est cost $%.5f)", api_key[-6:], est_cost)
            try:
                job_id = self._submit(api_key, api_format)
                direct_url = self._wait(job_id)
                accounts.mark_used(api_key, cost_usd=est_cost)
                return direct_url
            except PluginError as err:
                msg = str(err)
                if any(t in msg.lower() for t in (
                    "insufficient", "wallet", "balance", "quota", "credit", "401", "403",
                )):
                    log.info("savenow: key ...%s exhausted (%s); rotating", api_key[-6:], msg[:80])
                    accounts.retire(api_key)
                    continue
                raise
        return None

    def _get_streams(self):
        streams: dict[str, Stream] = {}

        wanted = _wanted_quality_from_argv()
        # Only resolve the one quality the caller will actually play.
        # Every resolve = one billable request; enumerating every quality
        # would charge $0.0002 × N for a single click. If we can't infer a
        # target (no stream arg — e.g. `streamlink URL` to list qualities),
        # fall back to 1080p as a sensible default.
        target_label = wanted or "1080p"
        api_format = _QUALITY_MAP.get(target_label, _QUALITY_MAP["1080p"])
        direct_url = self._resolve_one(api_format)
        if not direct_url:
            return streams
        stream = HTTPStream(self.session, direct_url, headers=self._headers())
        # Expose the resolved stream under the target label plus `best`/`worst`
        # aliases so `streamlink URL best` / `streamlink URL worst` work.
        streams[target_label] = stream
        streams["best"] = stream
        streams["worst"] = stream
        return streams


def _wanted_quality_from_argv() -> str | None:
    """Peek at sys.argv for a stream label so we only pay to resolve the one
    the caller will actually play. Same trick the old vthreads plugin used."""
    import sys as _sys
    known = {label for label, _ in _ADVERTISED_QUALITIES} | {"best", "worst"}
    argv = _sys.argv[1:]
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
            # Map 'best' → 1080p (highest we serve without paying 1440p+ premium).
            # Map 'worst' → 144p.
            if tok == "best":
                return "1080p"
            if tok == "worst":
                return "144p"
            return tok
    return None


__plugin__ = Savenow
