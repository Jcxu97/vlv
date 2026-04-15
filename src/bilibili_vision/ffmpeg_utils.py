"""Unified FFmpeg binary resolution (shared by transcribe_local, vision_deep_pipeline, local_vlm_openai_client)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from bilibili_vision.paths import PROJECT_ROOT


def find_ffmpeg() -> Path | None:
    """Locate the ffmpeg executable: project bundled dir first, then PATH."""
    if sys.platform == "win32":
        cand = PROJECT_ROOT / "ffmpeg" / "ffmpeg.exe"
        if cand.is_file():
            return cand
    cand = PROJECT_ROOT / "ffmpeg" / "ffmpeg"
    if cand.is_file():
        return cand
    w = shutil.which("ffmpeg")
    return Path(w) if w else None


def find_ffprobe() -> Path | None:
    """Locate the ffprobe executable."""
    if sys.platform == "win32":
        cand = PROJECT_ROOT / "ffmpeg" / "ffprobe.exe"
        if cand.is_file():
            return cand
    cand = PROJECT_ROOT / "ffmpeg" / "ffprobe"
    if cand.is_file():
        return cand
    w = shutil.which("ffprobe")
    return Path(w) if w else None
