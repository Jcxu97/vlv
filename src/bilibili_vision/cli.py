"""Thin command-line interface for VLV.

    vlv extract <url>           — run the extract pipeline
    vlv analyze <session_dir>   — re-run analysis on an existing session
    vlv digest <session_dir>    — compress a session into an LLM-ready digest
    vlv diagnostics             — emit a diagnostics zip
    vlv gui                     — launch the GUI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _run_module(module: str, argv: list[str]) -> int:
    """Invoke a module's argparse-based main() by swapping sys.argv.

    The pipeline/analyze mains call parse_args() with no arguments, so they read
    sys.argv directly rather than accepting an argv parameter.
    """
    import importlib

    saved = sys.argv
    sys.argv = [module, *argv]
    try:
        mod = importlib.import_module(f".{module}", __package__)
        return int(mod.main() or 0)
    except SystemExit as exc:  # argparse / explicit sys.exit inside the module
        return int(exc.code or 0)
    finally:
        sys.argv = saved


def _cmd_extract(args: argparse.Namespace) -> int:
    argv = ["extract", args.url]
    if args.no_playlist:
        argv.append("--no-playlist")
    if args.skip_video:
        argv.append("--no-download-video")
    if args.asr:
        argv.append("--asr-force")
    if args.no_analyze:
        argv.append("--no-analyze")
    return _run_module("bilibili_pipeline", argv)


def _cmd_analyze(args: argparse.Namespace) -> int:
    session = args.session_dir
    merged = session / "transcript_merged.txt" if session.is_dir() else session
    if not merged.exists():
        print(f"找不到合并文稿：{merged}", file=sys.stderr)
        return 1
    argv = ["-i", str(merged), "-o", str(merged.parent / "video_analysis.txt")]
    return _run_module("analyze_transcript", argv)


def _cmd_digest(args: argparse.Namespace) -> int:
    argv = [str(args.session_dir), "--per-para", str(args.per_para)]
    if args.output:
        argv += ["-o", str(args.output)]
    return _run_module("digest", argv)


def _cmd_diagnostics(_: argparse.Namespace) -> int:
    from .diagnostics import build_diagnostic_zip

    p = build_diagnostic_zip()
    print(p)
    return 0


def _cmd_gui(_: argparse.Namespace) -> int:
    from . import gui

    gui.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlv", description="Video Listen View")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_extract = sub.add_parser("extract", help="Run extract pipeline on a URL")
    p_extract.add_argument("url")
    p_extract.add_argument("--no-playlist", action="store_true")
    p_extract.add_argument(
        "--skip-video", action="store_true", help="只拉字幕/弹幕，不下载整片"
    )
    p_extract.add_argument(
        "--asr", action="store_true", help="强制本地 Whisper 转写（即使已有官方字幕）"
    )
    p_extract.add_argument(
        "--no-analyze", action="store_true", help="跳过 video_analysis.txt 生成"
    )
    p_extract.set_defaults(func=_cmd_extract)

    p_analyze = sub.add_parser("analyze", help="Analyze a prior session directory")
    p_analyze.add_argument("session_dir", type=Path)
    p_analyze.set_defaults(func=_cmd_analyze)

    p_digest = sub.add_parser(
        "digest", help="Compress a session into an LLM-ready digest"
    )
    p_digest.add_argument("session_dir", type=Path)
    p_digest.add_argument("-o", "--output", type=Path, default=None)
    p_digest.add_argument("--per-para", type=int, default=12)
    p_digest.set_defaults(func=_cmd_digest)

    p_diag = sub.add_parser("diagnostics", help="Export a diagnostics zip")
    p_diag.set_defaults(func=_cmd_diagnostics)

    p_gui = sub.add_parser("gui", help="Launch the GUI")
    p_gui.set_defaults(func=_cmd_gui)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
