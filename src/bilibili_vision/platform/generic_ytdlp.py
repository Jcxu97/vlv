"""Fallback adapter for any URL yt-dlp supports."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import PlatformAdapter, PlatformCapability, SubtitleCue, VideoMetadata
from ._ytdlp import info_to_base_metadata, run_ytdlp_info


class GenericYtdlpAdapter:
    platform_id = "generic"
    capabilities = PlatformCapability.COOKIES

    def detect(self, url: str) -> bool:
        # Used as the registry's fallback; detection just rubber-stamps any URL
        # that looks vaguely like HTTP. The registry only consults this adapter
        # after all specialised adapters have declined.
        u = (url or "").lower()
        return u.startswith("http://") or u.startswith("https://")

    def fetch_metadata(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> VideoMetadata:
        info = run_ytdlp_info(url, cookies=cookies)
        base = info_to_base_metadata(info)
        platform = info.get("extractor_key") or info.get("extractor") or "generic"
        return VideoMetadata(platform=str(platform).lower(), raw=info, **base)

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
