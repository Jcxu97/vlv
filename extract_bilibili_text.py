"""
一键：B 站链接 → 弹幕/字幕；或本机音视频路径 → 直接本地转写。
- 链接：未登录常只有弹幕 XML；有 cookies.txt 可尝试官方/AI 字幕。
- 本地文件：跳过 B 站下载，用 ffmpeg（视频先抽音轨）+ faster-whisper 生成 SRT。
- 链接无 SRT/VTT 时可用 --asr-if-no-subs 下音频并转写。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Embeddable python311._pth may omit the script directory from sys.path; local modules live next to this file.
sys.path.insert(0, str(ROOT))

from transcribe_local import default_whisper_model_choice

try:
    from browser_bilibili import parse_srt_to_lines
except ImportError:
    parse_srt_to_lines = None  # type: ignore[misc,assignment]

OUT = ROOT / "out"
COOKIES = ROOT / "cookies.txt"


def _clear_previous_media_files() -> None:
    """避免 out 里残留旧视频的 srt/弹幕，合并进新报告看起来像链接无效。"""
    OUT.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.danmaku.xml", "*.srt", "*.vtt"):
        for f in OUT.glob(pattern):
            try:
                f.unlink()
            except OSError:
                pass
    for f in OUT.glob("*_audio.*"):
        try:
            f.unlink()
        except OSError:
            pass
    for f in OUT.glob("*_extracted_audio.m4a"):
        try:
            f.unlink()
        except OSError:
            pass


def run_ytdlp(url: str, no_playlist: bool, cookies: Path | None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _clear_previous_media_files()
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "all,-live_chat",
        "--sub-format",
        "srt/vtt/ass/xml/best",
        "--skip-download",
        "-o",
        str(OUT / "%(id)s_p%(playlist_index)s.%(ext)s"),
    ]
    if no_playlist:
        cmd.append("--no-playlist")
    if cookies and cookies.is_file():
        cmd.extend(["--cookies", str(cookies)])
    cmd.append(url)
    print("运行:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


DANMU_RE = re.compile(r'<d p="([^"]+)">([^<]*)</d>')


def parse_danmaku_xml(path: Path) -> list[tuple[float, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows: list[tuple[float, str]] = []
    for m in DANMU_RE.finditer(text):
        p = m.group(1).split(",")
        try:
            t = float(p[0])
        except (IndexError, ValueError):
            continue
        body = m.group(2).strip()
        if not body:
            continue
        if re.fullmatch(r"\d{1,2}", body):
            continue
        rows.append((t, body))
    rows.sort(key=lambda x: x[0])
    return rows


def is_noise_line(s: str) -> bool:
    s = s.strip()
    if not s:
        return True
    if re.fullmatch(r"\d{1,2}", s):
        return True
    return False


def merge_outputs() -> tuple[str, Path]:
    """合并弹幕与可用的 xml 字幕（若有）。"""
    chunks: list[str] = []

    danmu_files = sorted(OUT.glob("*.danmaku.xml"))
    for f in danmu_files:
        label = f.stem
        lines = parse_danmaku_xml(f)
        if not lines:
            continue
        chunks.append(f"===== 弹幕 {label} =====\n")
        for t, body in lines:
            chunks.append(f"[{t:08.2f}s] {body}\n")
        chunks.append("\n")

    for f in sorted(OUT.glob("*.srt")):
        raw = f.read_text(encoding="utf-8", errors="replace")
        chunks.append(f"===== 字幕 {f.name} =====\n")
        if parse_srt_to_lines:
            for ts, line in parse_srt_to_lines(raw):
                chunks.append(f"[{ts}] {line}\n")
        else:
            chunks.append(raw[:50000])
            if len(raw) > 50000:
                chunks.append("\n... [truncated] ...\n")
        chunks.append("\n")

    for f in sorted(OUT.glob("*.vtt")):
        chunks.append(f"===== 字幕 {f.name}（VTT 原文）=====\n")
        chunks.append(f.read_text(encoding="utf-8", errors="replace")[:80000])
        chunks.append("\n\n")

    sub_files = sorted(OUT.glob("*.xml"))
    for f in sub_files:
        if f.suffixes[:2] == [".danmaku", ".xml"]:
            continue
        if "danmaku" in f.name:
            continue
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<subtitle" in raw or "<text" in raw or "<d " in raw:
            chunks.append(f"===== 字幕文件 {f.name}（未细解析，请人工查看）=====\n")
            chunks.append(raw[:50000])
            if len(raw) > 50000:
                chunks.append("\n... [truncated] ...\n")

    merged = (
        "".join(chunks)
        if chunks
        else "(未生成任何文本：若为 B 站链接请检查弹幕/字幕或 cookies；若为本地文件请确认已成功转写。)\n"
    )
    out_path = OUT / "transcript_merged.txt"
    out_path.write_text(merged, encoding="utf-8")
    return merged, out_path


def _subs_present(out_dir: Path) -> bool:
    return any(out_dir.glob("*.srt")) or any(out_dir.glob("*.vtt"))


def _need_asr(args: argparse.Namespace, out_dir: Path) -> bool:
    if getattr(args, "asr_force", False):
        return True
    if not getattr(args, "asr_if_no_subs", False):
        return False
    return not _subs_present(out_dir)


def _normalize_source(raw: str) -> str:
    return raw.strip().strip('"')


def _try_local_media_path(raw: str) -> Path | None:
    p = Path(os.path.expanduser(_normalize_source(raw)))
    try:
        if p.is_file():
            return p.resolve()
    except OSError:
        pass
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="B 站链接或本地音视频：合并字幕/弹幕/转写为 transcript_merged.txt"
    )
    ap.add_argument(
        "source",
        metavar="URL或本地文件",
        help="B 站 https 链接，或本机音视频路径（如 .mp3 .mp4 .wav）",
    )
    ap.add_argument("--no-playlist", action="store_true", help="只处理当前 P（不分集）")
    ap.add_argument("--no-analyze", action="store_true", help="不生成 out/video_analysis.txt")
    ap.add_argument(
        "--asr-if-no-subs",
        action="store_true",
        help="无 SRT/VTT 时下载音频并用 faster-whisper 本地转写（需 pip install faster-whisper 与 ffmpeg）",
    )
    ap.add_argument(
        "--asr-force",
        action="store_true",
        help="无论是否有字幕都进行本地转写（可与官方字幕对照）",
    )
    ap.add_argument(
        "--whisper-model",
        default=default_whisper_model_choice(),
        help="faster-whisper 模型（默认优先使用 whisper-models/ 中已存在的目录；离线勿选未拷贝的模型）",
    )
    ap.add_argument(
        "--whisper-device",
        default="auto",
        choices=["auto", "cpu", "cuda"],
        help="推理设备（默认 auto：有 CUDA 则用 GPU；缺 CUDA DLL 时会自动改用 CPU）",
    )
    ap.add_argument(
        "--whisper-model-path",
        metavar="DIR",
        default=None,
        help="本地 faster-whisper 模型目录（与 Hub 上 Systran/faster-whisper-* 结构一致）；也可设环境变量 WHISPER_MODEL_PATH",
    )
    ap.add_argument(
        "--ffmpeg-location",
        metavar="PATH",
        default=None,
        help="ffmpeg 所在目录，或 ffmpeg 可执行文件路径（默认：PATH，其次 static-ffmpeg）",
    )
    args = ap.parse_args()

    src_path = _try_local_media_path(args.source)
    model_path_opt = (
        (args.whisper_model_path or os.environ.get("WHISPER_MODEL_PATH") or "").strip() or None
    )

    if src_path:
        try:
            from transcribe_local import (
                SUPPORTED_LOCAL_MEDIA_SUFFIXES,
                is_supported_local_media,
                run_asr_for_local_path,
            )
        except ImportError as e:
            print(f"错误：无法加载 transcribe_local。\n  {e}", file=sys.stderr)
            raise SystemExit(2) from e
        if not is_supported_local_media(src_path):
            print(
                f"不支持的本地文件类型：{src_path.suffix}\n"
                f"支持：{', '.join(sorted(SUPPORTED_LOCAL_MEDIA_SUFFIXES))}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        OUT.mkdir(parents=True, exist_ok=True)
        _clear_previous_media_files()
        print("本地文件模式：跳过 B 站下载，直接转写。", flush=True)
        try:
            run_asr_for_local_path(
                src_path,
                OUT,
                model_size=args.whisper_model,
                device_pref=args.whisper_device,
                ffmpeg_location=args.ffmpeg_location,
                model_path=model_path_opt,
            )
        except ImportError as e:
            print(
                "错误：需要安装 faster-whisper，且需 ffmpeg+ffprobe。\n"
                "  pip install faster-whisper static-ffmpeg",
                file=sys.stderr,
            )
            raise SystemExit(2) from e
        except (RuntimeError, FileNotFoundError, ValueError, subprocess.CalledProcessError) as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from e
    else:
        line = _normalize_source(args.source)
        if not line.lower().startswith(("http://", "https://")):
            print(
                "既不是可读的本地文件路径，也不是以 http(s) 开头的链接。\n"
                "请检查路径是否存在，或粘贴完整 B 站链接。",
                file=sys.stderr,
            )
            raise SystemExit(1)

        cookies = COOKIES if COOKIES.is_file() else None
        if cookies:
            print("检测到 cookies.txt，将尝试抓取需登录的字幕。")
        run_ytdlp(line, args.no_playlist, cookies)

        if _need_asr(args, OUT):
            try:
                from transcribe_local import run_asr_for_url
            except ImportError as e:
                print(
                    "错误：已请求本地转写但无法加载 transcribe_local。\n"
                    f"  {e}",
                    file=sys.stderr,
                )
                raise SystemExit(2) from e
            print("开始本地语音转写（口播稿）…", flush=True)
            try:
                run_asr_for_url(
                    line,
                    OUT,
                    no_playlist=args.no_playlist,
                    cookies=cookies,
                    model_size=args.whisper_model,
                    device_pref=args.whisper_device,
                    ffmpeg_location=args.ffmpeg_location,
                    model_path=model_path_opt,
                )
            except ImportError as e:
                print(
                    "错误：需要安装 faster-whisper，且需 ffmpeg+ffprobe（pip install static-ffmpeg 或系统 PATH）。\n"
                    "  pip install faster-whisper static-ffmpeg",
                    file=sys.stderr,
                )
                raise SystemExit(2) from e
            except (RuntimeError, FileNotFoundError) as e:
                print(str(e), file=sys.stderr)
                raise SystemExit(1) from e

    _, path = merge_outputs()
    print(f"完成：{path}")
    if not args.no_analyze:
        subprocess.run([sys.executable, str(ROOT / "analyze_transcript.py")], check=False)


if __name__ == "__main__":
    main()
