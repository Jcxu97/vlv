"""Bilibili platform adapter.

Wraps the existing `browser_bilibili` login flow and yt-dlp info fetch. The
full extract pipeline (download + danmaku + subtitle priority) still lives in
`extract_bilibili_text.py`; this adapter is the routing/metadata entry point
used by the registry. A later phase can fold that pipeline into this class.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .base import PlatformAdapter, PlatformCapability, SubtitleCue, VideoMetadata
from ._ytdlp import info_to_base_metadata, run_ytdlp_info

_BILIBILI_DOMAINS = (
    "bilibili.com",
    "bilibili.tv",
    "b23.tv",
    "bili2233.cn",
    "bilivideo.com",
    "bilivideo.cn",
)

_BV_RE = re.compile(r"BV[a-zA-Z0-9]{10}")


class BilibiliAdapter:
    platform_id = "bilibili"
    capabilities = (
        PlatformCapability.SUBTITLES
        | PlatformCapability.AUTO_CAPTIONS
        | PlatformCapability.DANMAKU
        | PlatformCapability.LOGIN
        | PlatformCapability.COOKIES
    )

    def detect(self, url: str) -> bool:
        u = (url or "").lower()
        if not (u.startswith("http://") or u.startswith("https://")):
            return False
        return any(d in u for d in _BILIBILI_DOMAINS)

    def fetch_metadata(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> VideoMetadata:
        info = run_ytdlp_info(url, cookies=cookies)
        base = info_to_base_metadata(info)
        bv = _BV_RE.search(url)
        if bv and not base["video_id"]:
            base["video_id"] = bv.group(0)
        return VideoMetadata(platform=self.platform_id, raw=info, **base)

    def fetch_subtitles(
        self,
        url: str,
        *,
        cookies: Optional[Path] = None,
        lang_preference: Optional[list[str]] = None,
    ) -> list[SubtitleCue]:
        """Bilibili subtitle extraction currently goes through the full pipeline
        in extract_bilibili_text; returning [] here signals the caller to use
        that code path. Phase 2 will move parse-only logic into this method."""
        return []

    def fetch_enrichment(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> dict:
        """Returns metadata about whether danmaku are available. The actual
        danmaku parse still lives in extract_bilibili_text.parse_danmaku_xml;
        a follow-up moves that into this adapter."""
        return {"has_danmaku": True, "platform": self.platform_id}
