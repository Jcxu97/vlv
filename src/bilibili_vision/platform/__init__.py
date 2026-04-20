"""Platform abstraction layer for VLV.

This package decouples the pipeline from any single video source. Each
`PlatformAdapter` encapsulates the URL detection, optional login flow,
metadata fetch, subtitle fetch, and platform-specific enrichment (e.g.
Bilibili danmaku, YouTube auto-captions).

Public API:
    detect_adapter(url)       -> PlatformAdapter
    list_adapters()           -> list[PlatformAdapter]
    BilibiliAdapter, YouTubeAdapter, DouyinAdapter, GenericYtdlpAdapter

See base.py for the protocol and dataclasses.
"""
from .base import (
    PlatformAdapter,
    VideoMetadata,
    SubtitleCue,
    PlatformCapability,
)
from .registry import detect_adapter, list_adapters, register_adapter
from .bilibili import BilibiliAdapter
from .youtube import YouTubeAdapter
from .douyin import DouyinAdapter
from .generic_ytdlp import GenericYtdlpAdapter

__all__ = [
    "PlatformAdapter",
    "VideoMetadata",
    "SubtitleCue",
    "PlatformCapability",
    "detect_adapter",
    "list_adapters",
    "register_adapter",
    "BilibiliAdapter",
    "YouTubeAdapter",
    "DouyinAdapter",
    "GenericYtdlpAdapter",
]
