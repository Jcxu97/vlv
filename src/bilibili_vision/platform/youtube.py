"""YouTube platform adapter (public videos; cookies optional for age-gated)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .base import PlatformAdapter, PlatformCapability, SubtitleCue, VideoMetadata
from ._ytdlp import info_to_base_metadata, run_ytdlp_info

_YT_DOMAINS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "m.youtube.com")
_YT_ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})")


class YouTubeAdapter:
    platform_id = "youtube"
    capabilities = (
        PlatformCapability.SUBTITLES
        | PlatformCapability.AUTO_CAPTIONS
        | PlatformCapability.COMMENTS
        | PlatformCapability.COOKIES
    )

    def detect(self, url: str) -> bool:
        u = (url or "").lower()
        if not (u.startswith("http://") or u.startswith("https://")):
            return False
        return any(d in u for d in _YT_DOMAINS)

    def fetch_metadata(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> VideoMetadata:
        info = run_ytdlp_info(url, cookies=cookies)
        base = info_to_base_metadata(info)
        if not base["video_id"]:
            m = _YT_ID_RE.search(url)
            if m:
                base["video_id"] = m.group(1)
        return VideoMetadata(platform=self.platform_id, raw=info, **base)

    def fetch_subtitles(
        self,
        url: str,
        *,
        cookies: Optional[Path] = None,
        lang_preference: Optional[list[str]] = None,
    ) -> list[SubtitleCue]:
        # Left empty in Phase 1; the existing run_ytdlp download path captures
        # .srt/.vtt files directly. Phase 2 can parse them into SubtitleCue.
        return []

    def fetch_enrichment(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> dict:
        return {"has_danmaku": False, "platform": self.platform_id}
