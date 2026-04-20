"""Douyin (TikTok CN) platform adapter.

Relies on yt-dlp's douyin extractor. Many Douyin URLs require a cookies file to
avoid anti-bot interstitials; the adapter accepts one through the standard path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import PlatformAdapter, PlatformCapability, SubtitleCue, VideoMetadata
from ._ytdlp import info_to_base_metadata, run_ytdlp_info

_DOUYIN_DOMAINS = ("douyin.com", "iesdouyin.com", "v.douyin.com")


class DouyinAdapter:
    platform_id = "douyin"
    capabilities = PlatformCapability.COOKIES

    def detect(self, url: str) -> bool:
        u = (url or "").lower()
        if not (u.startswith("http://") or u.startswith("https://")):
            return False
        return any(d in u for d in _DOUYIN_DOMAINS)

    def fetch_metadata(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> VideoMetadata:
        info = run_ytdlp_info(url, cookies=cookies, timeout=45.0)
        base = info_to_base_metadata(info)
        return VideoMetadata(platform=self.platform_id, raw=info, **base)

    def fetch_subtitles(
        self,
        url: str,
        *,
        cookies: Optional[Path] = None,
        lang_preference: Optional[list[str]] = None,
    ) -> list[SubtitleCue]:
        return []

    def fetch_enrichment(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> dict:
        return {"has_danmaku": False, "platform": self.platform_id}
