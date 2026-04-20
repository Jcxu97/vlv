"""
按日期子目录 + 时间戳 + 标题（网页）或文件名（本地）组织 out/，避免覆盖。
环境变量 BILIBILI_VISION_OUT 指向当前任务目录，供 analyze / vision 子进程使用。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from bilibili_vision.paths import PROJECT_ROOT

ENV_OUT = "BILIBILI_VISION_OUT"
_CRED_DIR = PROJECT_ROOT / ".credentials"
COOKIES = _CRED_DIR / "cookies.txt"


def sanitize_component(name: str, max_len: int = 72) -> str:
    s = re.sub(r'[\x00-\x1f\\/*?:"<>|]', "_", (name or "").strip())
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._")
    if not s:
        return "untitled"
    return s[:max_len]


def extract_bv(url: str) -> str | None:
    m = re.search(r"BV[a-zA-Z0-9]{10}", url, re.I)
    return m.group(0) if m else None


_YT_ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|youtu\.be/)([A-Za-z0-9_-]{11})")


def extract_platform_id(url: str) -> tuple[str, str | None]:
    """Return (platform_slug, id_or_None) from a URL without importing the full
    registry — kept lightweight for use inside build_session_path where we
    only need the pieces for a directory name."""
    u = (url or "").lower()
    bv = extract_bv(url)
    if bv:
        return ("bilibili", bv)
    if any(d in u for d in ("youtube.com", "youtu.be")):
        m = _YT_ID_RE.search(url)
        return ("youtube", m.group(1) if m else None)
    if "douyin.com" in u or "iesdouyin.com" in u:
        return ("douyin", None)
    return ("generic", None)


def fetch_ytdlp_title(
    url: str, *, cookies: Path | None, no_playlist: bool
) -> str:
    cmd = [
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
            timeout=15,
            cwd=str(PROJECT_ROOT),
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if r.returncode != 0:
        return ""
    line = (r.stdout or "").strip().split("\n", 1)[0]
    if not line:
        return ""
    try:
        data = json.loads(line)
        t = data.get("title")
        if isinstance(t, str) and t.strip():
            return t.strip()
    except json.JSONDecodeError:
        pass
    return ""


def build_session_path(
    *,
    source_for_name: str,
    is_local: bool,
    local_path: Path | None,
    no_playlist: bool,
    cookies: Path | None,
) -> Path:
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    tp = now.strftime("%H%M%S")
    if is_local and local_path:
        slug = sanitize_component(local_path.stem)
        name = f"{tp}_{slug}"
    else:
        platform, pid = extract_platform_id(source_for_name)
        title = fetch_ytdlp_title(
            source_for_name, cookies=cookies, no_playlist=no_playlist
        )
        slug = sanitize_component(title) if title else platform
        suffix = f"_{platform}"
        if pid:
            suffix += f"_{pid}"
        name = f"{tp}_{slug}{suffix}"

    base = PROJECT_ROOT / "out" / day / name
    cand = base
    for n in range(100):
        try:
            cand.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            cand = base.parent / f"{base.name}_{n + 1}"
    else:
        cand.mkdir(parents=True, exist_ok=True)
    print(f"[输出目录] {cand}", flush=True)
    return cand.resolve()


def prepare_output_directory(
    *,
    source: str,
    is_local: bool,
    local_path: Path | None,
    no_playlist: bool,
) -> Path:
    cookies = COOKIES if COOKIES.is_file() else None
    p = build_session_path(
        source_for_name=source,
        is_local=is_local,
        local_path=local_path,
        no_playlist=no_playlist,
        cookies=cookies,
    )
    os.environ[ENV_OUT] = str(p)
    return p
