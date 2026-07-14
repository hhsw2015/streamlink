"""Mock-based tests for the vthreads plugin's cloud/local fallback paths.

Everything reachable *without* actually calling vthreads.top or the CF worker.
Covers:
  - local extract success (no cloud call)
  - local extract fail → cloud success
  - local extract fail → cloud also fails → PluginError bubbles
  - local extract fail + VTHREADS_SKIP_CLOUD=1 → no cloud attempt
  - cloud pending → poll → success
  - cloud pending → poll → failed
  - cloud 503 cooldown → treated as failure
  - _api_json 429 retry → then success
  - canonQuality edge cases (already covered in parity script, one extra assert here)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
import requests_mock as rm_module

from streamlink.exceptions import PluginError
from streamlink.plugins import vthreads as V
from streamlink.session import Streamlink

TEST_URL = "https://www.youtube.com/watch?v=test123XXXX"


@pytest.fixture(autouse=True)
def _isolate_cache(monkeypatch, tmp_path):
    """Point cache files into a fresh dir per-test so entries don't leak between tests.
    Also stub `time.sleep` so retry loops don't add real wall-clock delay."""
    cache_dir = tmp_path / "vthreads-cache"
    monkeypatch.setattr(V, "_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(V, "_URL_CACHE_FILE", str(cache_dir / "urls.json"))
    monkeypatch.setattr(V, "_EXTRACT_CACHE_FILE", str(cache_dir / "extract.json"))
    monkeypatch.delenv("VTHREADS_SKIP_CLOUD", raising=False)
    monkeypatch.delenv("VTHREADS_PREFETCH", raising=False)
    monkeypatch.setattr(V.time, "sleep", lambda s: None)
    yield
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)


@pytest.fixture()
def plugin():
    session = Streamlink()
    p = V.VThreads(session, TEST_URL)
    yield p


@pytest.fixture()
def mock():
    with rm_module.Mocker() as m:
        yield m


def _local_extract_response(medias):
    return {"success": True, "data": {"title": "Test Video", "medias": medias}}


def _cloud_success(direct_url="https://vthreads.top/api/get_file/xxx/xxx.mp4",
                   quality="1080p Full HD"):
    return {
        "job_id": "abc-def",
        "status": "success",
        "cached": True,
        "direct_url": direct_url,
        "filename": "test.mp4",
        "title": "Test Video Cloud",
        "quality": quality,
        "required_headers": {"Referer": "https://vthreads.top/", "User-Agent": "test"},
        "expires_at": 9999999999999,
    }


# ── local success (no cloud call) ─────────────────────────────

def test_local_extract_success_no_cloud_call(plugin, mock):
    mock.get(
        f"{V.VTHREADS_BASE}/api/extract",
        json=_local_extract_response([{"quality": "360p", "url": "/api/proxy?url=x", "format": "mp4"}]),
    )
    streams = plugin._get_streams()
    assert "360p" in streams
    cloud_requests = [r for r in mock.request_history if "extractor.bugcf" in r.url]
    assert cloud_requests == [], "cloud must not be called when local succeeds"


# ── local fail → cloud success ────────────────────────────────

def test_local_fail_cloud_success(plugin, mock):
    mock.get(f"{V.VTHREADS_BASE}/api/extract", status_code=500)
    mock.post(f"{V.CLOUD_BASE}/extract", json=_cloud_success())
    streams = plugin._get_streams()
    assert "1080p" in streams
    assert "best" in streams  # convenience alias
    assert streams["1080p"] is streams["best"]


def test_local_fail_cloud_pending_then_success(plugin, mock):
    mock.get(f"{V.VTHREADS_BASE}/api/extract", status_code=503)
    mock.post(f"{V.CLOUD_BASE}/extract", json={"job_id": "job1", "status": "pending", "created_at": 0})
    poll_iter = iter([
        {"job_id": "job1", "status": "pending", "progress": 0},
        {"job_id": "job1", "status": "polling", "progress": 50},
        {"job_id": "job1", "status": "success", "progress": 100},
    ])
    mock.get(f"{V.CLOUD_BASE}/status/job1", json=lambda req, ctx: next(poll_iter))
    mock.get(f"{V.CLOUD_BASE}/result/job1", json=_cloud_success())

    streams = plugin._get_streams()
    assert "1080p" in streams


# ── local fail + cloud fail → PluginError ─────────────────────

def test_local_fail_cloud_fail_raises(plugin, mock):
    mock.get(f"{V.VTHREADS_BASE}/api/extract", status_code=500)
    mock.post(f"{V.CLOUD_BASE}/extract", status_code=503)
    with pytest.raises(PluginError):
        plugin._get_streams()


def test_local_fail_cloud_job_failed_raises(plugin, mock):
    mock.get(f"{V.VTHREADS_BASE}/api/extract", status_code=500)
    mock.post(f"{V.CLOUD_BASE}/extract", json={"job_id": "j2", "status": "pending", "created_at": 0})
    mock.get(f"{V.CLOUD_BASE}/status/j2", json={"job_id": "j2", "status": "failed", "error": "upstream 429"})
    with pytest.raises(PluginError):
        plugin._get_streams()


# ── skip_cloud env var ─────────────────────────────────────────

def test_skip_cloud_env_prevents_fallback(plugin, mock, monkeypatch):
    monkeypatch.setenv("VTHREADS_SKIP_CLOUD", "1")
    mock.get(f"{V.VTHREADS_BASE}/api/extract", status_code=500)
    with pytest.raises(PluginError):
        plugin._get_streams()
    cloud_requests = [r for r in mock.request_history if "extractor.bugcf" in r.url]
    assert cloud_requests == [], "skip_cloud must suppress every cloud call"


# ── retry on 429 ───────────────────────────────────────────────

def test_api_json_retries_on_429_then_succeeds(plugin, mock):
    responses = [
        {"status_code": 429},
        {"status_code": 429},
        {"json": _local_extract_response([{"quality": "480p", "url": "/api/proxy?url=x", "format": "mp4"}])},
    ]
    mock.get(f"{V.VTHREADS_BASE}/api/extract", responses)
    data = plugin._extract()
    assert data["medias"][0]["quality"] == "480p"


# ── cache ──────────────────────────────────────────────────────

def test_extract_cache_hit_avoids_network(plugin, mock):
    mock.get(f"{V.VTHREADS_BASE}/api/extract",
             json=_local_extract_response([{"quality": "720p", "url": "/api/proxy?url=x", "format": "mp4"}]))
    plugin._extract()
    assert len(mock.request_history) == 1
    plugin._extract()
    assert len(mock.request_history) == 1, "second call must hit cache, not network"


def test_cloud_cache_ignored_when_expired(plugin, mock, monkeypatch):
    # seed cache with an already-expired entry
    V._cache_put(V._URL_CACHE_FILE, "cloud:" + TEST_URL + "|q=1080p",
                 {"direct_url": "https://stale/x.mp4", "expires_at": 0})
    mock.get(f"{V.VTHREADS_BASE}/api/extract", status_code=500)
    mock.post(f"{V.CLOUD_BASE}/extract", json=_cloud_success())
    streams = plugin._get_streams()
    # stale cache ignored → cloud actually called → direct_url is the fresh one
    assert streams["1080p"].args["url"] != "https://stale/x.mp4"


# ── streams_from_cloud shape ──────────────────────────────────

def test_streams_from_cloud_includes_required_headers(plugin):
    result = _cloud_success(quality="1080p Full HD")
    streams = plugin._streams_from_cloud(result)
    assert "1080p" in streams and "best" in streams
    hdrs = streams["1080p"].args.get("headers") or {}
    assert hdrs.get("Referer") == "https://vthreads.top/"
    assert hdrs.get("User-Agent") == "test"
