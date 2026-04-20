"""Thin command-line interface for VLV.

    vlv extract <url>           — run the extract pipeline
    vlv analyze <session_dir>   — re-run analysis on an existing session
    vlv diagnostics             — emit a diagnostics zip
    vlv gui                     — launch the GUI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _cmd_extract(args: argparse.Namespace) -> int:
    from .bilibili_pipeline import main as pipeline_main  # lazy import

    argv = ["extract", args.url]
    if args.no_playlist:
        argv.append("--no-playlist")
    if args.skip_video:
        argv.append("--skip-video")
    return int(pipeline_main(argv) or 0)


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .analyze_transcript import main as analyze_main  # lazy import

    return int(analyze_main([str(args.session_dir)]) or 0)


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
    p_extract.add_argument("--skip-video", action="store_true")
    p_extract.set_defaults(func=_cmd_extract)

    p_analyze = sub.add_parser("analyze", help="Analyze a prior session directory")
    p_analyze.add_argument("session_dir", type=Path)
    p_analyze.set_defaults(func=_cmd_analyze)

    p_diag = sub.add_parser("diagnostics", help="Export a diagnostics zip")
    p_diag.set_defaults(func=_cmd_diagnostics)

    p_gui = sub.add_parser("gui", help="Launch the GUI")
    p_gui.set_defaults(func=_cmd_gui)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
