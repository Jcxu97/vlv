"""Base protocol and data classes for platform adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Flag, auto
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


class PlatformCapability(Flag):
    NONE = 0
    SUBTITLES = auto()          # adapter can fetch subtitle cues
    AUTO_CAPTIONS = auto()      # adapter can fetch ASR auto-captions
    DANMAKU = auto()            # danmaku / bullet-comments
    COMMENTS = auto()           # top-level comments
    LOGIN = auto()              # adapter supports an interactive login
    COOKIES = auto()            # adapter can consume a Netscape cookies.txt


@dataclass
class SubtitleCue:
    start: float       # seconds
    end: float
    text: str


@dataclass
class VideoMetadata:
    platform: str                  # e.g. "bilibili", "youtube"
    video_id: str                  # stable id (BV..., youtube 11-char, etc.)
    url: str                       # canonical URL
    title: str = ""
    uploader: str = ""
    duration_sec: Optional[float] = None
    thumbnail_url: Optional[str] = None
    language: Optional[str] = None
    raw: dict = field(default_factory=dict)   # platform-specific info_dict


@runtime_checkable
class PlatformAdapter(Protocol):
    """Protocol every platform adapter must satisfy."""

    platform_id: str
    capabilities: PlatformCapability

    def detect(self, url: str) -> bool: ...
    def fetch_metadata(self, url: str, *, cookies: Optional[Path] = None) -> VideoMetadata: ...
    def fetch_subtitles(
        self,
        url: str,
        *,
        cookies: Optional[Path] = None,
        lang_preference: Optional[list[str]] = None,
    ) -> list[SubtitleCue]: ...
    def fetch_enrichment(
        self, url: str, *, cookies: Optional[Path] = None
    ) -> dict: ...
