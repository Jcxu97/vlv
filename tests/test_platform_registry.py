"""Unit tests for the platform adapter registry."""
from __future__ import annotations

import pytest


def test_bilibili_adapter_detects():
    from bilibili_vision.platform import BilibiliAdapter

    a = BilibiliAdapter()
    assert a.detect("https://www.bilibili.com/video/BV1xxx")
    assert a.detect("https://b23.tv/abc")
    assert not a.detect("https://youtube.com/watch?v=abc")
    assert not a.detect("not a url")


def test_youtube_adapter_detects():
    from bilibili_vision.platform import YouTubeAdapter

    a = YouTubeAdapter()
    assert a.detect("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert a.detect("https://youtu.be/dQw4w9WgXcQ")
    assert a.detect("https://youtube.com/shorts/abc")
    assert not a.detect("https://bilibili.com/video/BV1xxx")


def test_douyin_adapter_detects():
    from bilibili_vision.platform import DouyinAdapter

    a = DouyinAdapter()
    assert a.detect("https://www.douyin.com/video/123456")
    assert a.detect("https://v.douyin.com/abc/")
    assert not a.detect("https://youtube.com/watch?v=abc")


def test_registry_routes_correct_adapter():
    from bilibili_vision.platform import detect_adapter

    bili = detect_adapter("https://www.bilibili.com/video/BV1xxx")
    yt = detect_adapter("https://youtu.be/dQw4w9WgXcQ")
    dy = detect_adapter("https://www.douyin.com/video/1")
    assert bili.platform_id == "bilibili"
    assert yt.platform_id == "youtube"
    assert dy.platform_id == "douyin"


def test_registry_falls_back_to_generic():
    from bilibili_vision.platform import detect_adapter

    a = detect_adapter("https://www.vimeo.com/12345")
    assert a.platform_id == "generic"


def test_registry_rejects_non_url():
    from bilibili_vision.platform import detect_adapter
    from bilibili_vision.errors import UnsupportedURLError

    with pytest.raises(UnsupportedURLError):
        detect_adapter("not a url at all")


def test_capabilities_flags():
    from bilibili_vision.platform import (
        BilibiliAdapter,
        YouTubeAdapter,
        PlatformCapability,
    )

    b = BilibiliAdapter()
    assert PlatformCapability.DANMAKU in b.capabilities
    assert PlatformCapability.LOGIN in b.capabilities

    y = YouTubeAdapter()
    assert PlatformCapability.DANMAKU not in y.capabilities
    assert PlatformCapability.AUTO_CAPTIONS in y.capabilities


def test_extract_platform_id_routes():
    from bilibili_vision.output_session import extract_platform_id

    assert extract_platform_id("https://bilibili.com/video/BV1abc2defgh/") == (
        "bilibili",
        "BV1abc2defgh",
    )
    plat, pid = extract_platform_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert plat == "youtube"
    assert pid == "dQw4w9WgXcQ"
    plat, pid = extract_platform_id("https://www.douyin.com/video/7000")
    assert plat == "douyin"


def test_gui_helpers_multi_platform():
    from bilibili_vision import gui_helpers

    assert gui_helpers.looks_like_supported_url("https://www.bilibili.com/video/BV1xxx")
    assert gui_helpers.looks_like_supported_url("https://youtu.be/abc")
    assert gui_helpers.looks_like_supported_url("https://www.douyin.com/video/1")
    assert not gui_helpers.looks_like_supported_url("random string")
    assert gui_helpers.detect_platform_badge("https://youtu.be/abc") == "YouTube"
    assert gui_helpers.detect_platform_badge("https://b23.tv/x") == "Bilibili"
    assert gui_helpers.detect_platform_badge("https://www.douyin.com/v/1") == "Douyin"
