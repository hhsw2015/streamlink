from streamlink.plugins.savenow import Savenow, _wanted_quality_from_argv
from streamlink.plugins._savenow_accounts import estimated_cost
from tests.plugins import PluginCanHandleUrl


class TestPluginCanHandleUrlSavenow(PluginCanHandleUrl):
    __plugin__ = Savenow

    should_match = [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/abc",
        "https://www.youtube.com/embed/abc",
        "https://m.youtube.com/watch?v=abc",
    ]
    should_not_match = [
        "https://www.tiktok.com/@user/video/1",
        "https://www.bilibili.com/video/BV1",
        "https://x.com/user/status/1",
        "https://vimeo.com/1",
        "https://example.com/watch?v=x",
    ]


def test_estimated_cost_base_video():
    # ≤180min for 1080p is the flat base price.
    assert estimated_cost("1080") == 0.00020
    assert estimated_cost("720", duration_s=60) == 0.00020
    assert estimated_cost("720", duration_s=179 * 60) == 0.00020


def test_estimated_cost_4k_base_is_30min():
    # 4K/8K base duration is 30 min.
    assert estimated_cost("mp44k", duration_s=30 * 60) == 0.00035
    over = estimated_cost("mp44k", duration_s=45 * 60)
    assert over > 0.00035


def test_estimated_cost_extended_duration_multiplier():
    # 1080p at 4h → base is 180min, first 60 excess min charged at 3x.
    base = estimated_cost("1080")
    over = estimated_cost("1080", duration_s=240 * 60)
    assert over > base


def test_argv_quality_hint(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["streamlink", "url", "1080p"])
    assert _wanted_quality_from_argv() == "1080p"
    monkeypatch.setattr(sys, "argv", ["streamlink", "--player", "mpv", "url", "best"])
    assert _wanted_quality_from_argv() == "1080p"
    monkeypatch.setattr(sys, "argv", ["streamlink", "url"])
    assert _wanted_quality_from_argv() is None
