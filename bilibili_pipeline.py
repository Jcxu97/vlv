"""
本地音视频 + B 站：浏览器会话、yt-dlp、可选本地转写。

  python bilibili_pipeline.py extract "https://..."
      B 站链接：无会话时打开 Chromium 登录后继续拉字幕/弹幕。

  python bilibili_pipeline.py extract "D:\\\\media\\\\talk.mp3"
      本地路径：跳过登录与下载，直接 faster-whisper 转写（需 ffmpeg）。

  python bilibili_pipeline.py extract --asr-if-no-subs "https://..."
      无 SRT/VTT 时下载音频并转写。

  python bilibili_pipeline.py login
      仅刷新 browser_state.json（可选）。

登录检测：轮询 B 站 nav 接口，无需在终端按 Enter。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from browser_bilibili import (  # noqa: E402
    COOKIES_PATH,
    STATE_PATH,
    ensure_cookies_from_state,
    save_login_state,
)
from transcribe_local import default_whisper_model_choice  # noqa: E402


def run(cmd: list[str]) -> None:
    print("运行:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def cmd_login(args: argparse.Namespace) -> None:
    save_login_state(state_path=STATE_PATH, timeout_sec=args.timeout_sec)
    ensure_cookies_from_state(STATE_PATH)


def _normalize_source(raw: str) -> str:
    return raw.strip().strip('"')


def _try_resolved_file(raw: str) -> Path | None:
    p = Path(os.path.expanduser(_normalize_source(raw)))
    try:
        if p.is_file():
            return p.resolve()
    except OSError:
        pass
    return None


def cmd_extract(args: argparse.Namespace) -> None:
    from transcribe_local import is_supported_local_media

    src_raw = _normalize_source(args.source)
    local_path = _try_resolved_file(args.source)
    is_local = bool(local_path and is_supported_local_media(local_path))

    if is_local:
        print(f"本地文件：{local_path}（跳过浏览器与 B 站下载）", flush=True)
    else:
        if args.relogin or not STATE_PATH.is_file():
            print("需要登录会话：将打开浏览器窗口…", flush=True)
            save_login_state(state_path=STATE_PATH, timeout_sec=args.timeout_sec)
        ensure_cookies_from_state(STATE_PATH)

    extras: list[str] = []
    if args.no_playlist:
        extras.append("--no-playlist")
    if getattr(args, "asr_if_no_subs", False):
        extras.append("--asr-if-no-subs")
    if getattr(args, "asr_force", False):
        extras.append("--asr-force")
    need_whisper = is_local or getattr(args, "asr_if_no_subs", False) or getattr(
        args, "asr_force", False
    )
    if need_whisper:
        extras.extend(
            [
                "--whisper-model",
                getattr(args, "whisper_model", default_whisper_model_choice()),
                "--whisper-device",
                getattr(args, "whisper_device", "auto"),
            ]
        )
    wmp = getattr(args, "whisper_model_path", None)
    if wmp:
        extras.extend(["--whisper-model-path", wmp])
    ff_loc = getattr(args, "ffmpeg_location", None)
    if ff_loc:
        extras.extend(["--ffmpeg-location", ff_loc])
    argv_source = str(local_path) if is_local else src_raw
    cmd = [
        sys.executable,
        str(ROOT / "extract_bilibili_text.py"),
        "--no-analyze",
        *extras,
        argv_source,
    ]
    run(cmd)
    if not getattr(args, "no_analyze", False):
        run([sys.executable, str(ROOT / "analyze_transcript.py")])


def main() -> None:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    ap = argparse.ArgumentParser(description="本地音视频或 B 站：提取/转写 + 可选分析")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="打开 Chromium 登录 B 站并保存 browser_state.json")
    p_login.add_argument(
        "--timeout-sec",
        type=float,
        default=900.0,
        help="最长等待登录的秒数（默认 900）",
    )
    p_login.set_defaults(func=cmd_login)

    p_ex = sub.add_parser(
        "extract",
        help="B 站链接拉字幕/弹幕，或本地音视频路径直接转写（本地模式跳过登录）",
    )
    p_ex.add_argument(
        "source",
        metavar="URL或本地文件",
        help="https B 站链接，或本机音视频路径（如 .mp3 .mp4）",
    )
    p_ex.add_argument("--no-playlist", action="store_true", help="只抓当前 P")
    p_ex.add_argument(
        "--relogin",
        action="store_true",
        help="忽略已有会话，强制重新登录",
    )
    p_ex.add_argument(
        "--timeout-sec",
        type=float,
        default=900.0,
        help="自动登录时最长等待秒数（默认 900）",
    )
    p_ex.add_argument(
        "--no-analyze",
        action="store_true",
        help="跳过生成 out/video_analysis.txt",
    )
    p_ex.add_argument(
        "--asr-if-no-subs",
        action="store_true",
        help="无官方字幕时下载音频并用 faster-whisper 本地转写",
    )
    p_ex.add_argument(
        "--asr-force",
        action="store_true",
        help="无论是否有字幕都进行本地转写",
    )
    p_ex.add_argument(
        "--whisper-model",
        default=default_whisper_model_choice(),
        help="faster-whisper 模型（默认优先使用 whisper-models/ 中已存在的目录，如仅 small 则默认 small）",
    )
    p_ex.add_argument(
        "--whisper-device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="转写设备（默认 auto）",
    )
    p_ex.add_argument(
        "--ffmpeg-location",
        metavar="PATH",
        default=None,
        help="ffmpeg/ffprobe 所在目录（可选；不设则自动用 PATH 或 static-ffmpeg）",
    )
    p_ex.add_argument(
        "--whisper-model-path",
        metavar="DIR",
        default=None,
        help="本地 faster-whisper 模型目录（可选；也可设 WHISPER_MODEL_PATH）",
    )
    p_ex.set_defaults(func=cmd_extract)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
