"""Shared yt-dlp helpers used across adapters."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..errors import ExtractionError, NetworkError


def run_ytdlp_info(
    url: str,
    *,
    cookies: Optional[Path] = None,
    no_playlist: bool = True,
    timeout: float = 30.0,
    cwd: Optional[Path] = None,
) -> dict:
    """Invoke `yt-dlp -j` for a URL and return the parsed info_dict."""
    cmd: list[str] = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-j",
        "--skip-download",
        "--no-download",
    ]
    if no_playlist:
        cmd.append("--no-playlist")
    if cookies and cookies.is_file():
        cmd.extend(["--cookies", str(cookies)])
    cmd.append(url)
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as e:
        raise NetworkError(f"yt-dlp timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise ExtractionError(f"yt-dlp launch failed: {e}") from e
    if r.returncode != 0:
        raise ExtractionError(
            f"yt-dlp failed (code={r.returncode}): {(r.stderr or '').strip()[:400]}"
        )
    line = (r.stdout or "").strip().split("\n", 1)[0]
    if not line:
        raise ExtractionError("yt-dlp returned no JSON output")
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise ExtractionError(f"yt-dlp info dict was not valid JSON: {e}") from e


def info_to_base_metadata(info: dict) -> dict:
    """Extract the fields common to all adapters from a yt-dlp info_dict."""
    return {
        "video_id": info.get("id") or "",
        "title": (info.get("title") or "").strip(),
        "uploader": (info.get("uploader") or info.get("channel") or "").strip(),
        "duration_sec": info.get("duration"),
        "thumbnail_url": info.get("thumbnail"),
        "language": info.get("language"),
        "url": info.get("webpage_url") or "",
    }
