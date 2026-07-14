from streamlink.plugins.vthreads import VThreads, _normalize_quality
from tests.plugins import PluginCanHandleUrl


class TestPluginCanHandleUrlVThreads(PluginCanHandleUrl):
    __plugin__ = VThreads

    should_match = [
        "https://www.youtube.com/watch?v=5DD_PIe7bAw",
        "https://youtu.be/5DD_PIe7bAw",
        "https://www.youtube.com/shorts/abc",
        "https://www.tiktok.com/@user/video/123",
        "https://vm.tiktok.com/ZM123/",
        "https://www.bilibili.com/video/BV1xx",
        "https://b23.tv/abc",
        "https://weibo.com/tv/show/1",
        "https://www.kuaishou.com/short-video/1",
        "https://www.xiaohongshu.com/explore/1",
        "https://www.douyin.com/video/1",
        "https://twitter.com/user/status/1",
        "https://x.com/user/status/1",
        "https://www.instagram.com/reel/xyz/",
        "https://www.facebook.com/watch?v=1",
        "https://fb.watch/abc/",
        "https://www.reddit.com/r/sub/comments/1/x/",
        "https://redd.it/abc",
        "https://vimeo.com/123",
        "https://www.dailymotion.com/video/x1",
        "https://dai.ly/x1",
    ]

    should_not_match = [
        "https://vthreads.top/",
        "https://example.com/watch?v=x",
    ]


def test_normalize_quality():
    assert _normalize_quality("4K Ultra HD") == "2160p"
    assert _normalize_quality("2K Quad HD") == "1440p"
    assert _normalize_quality("1080p Full HD") == "1080p"
    assert _normalize_quality("720p HD") == "720p"
    assert _normalize_quality("360p") == "360p"
    assert _normalize_quality("1080p60") == "1080p60"
    assert _normalize_quality("weird label") == "weird_label"
