"""
Pre-download faster-whisper CTranslate2 models into whisper-models/<name>/ for portable / offline use.
Uses the same Hub IDs as faster_whisper.utils.download_model.

  set PYTHONPATH=src
  python_embed\\python.exe -m bilibili_vision.download_whisper_models large-v3 small
"""
from __future__ import annotations

import argparse
import sys

from bilibili_vision.paths import PROJECT_ROOT


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Systran faster-whisper models into ./whisper-models/"
    )
    parser.add_argument(
        "models",
        nargs="*",
        default=["large-v3", "small"],
        help="Model id(s) (default: large-v3 small); e.g. medium large-v2",
    )
    args = parser.parse_args()
    from faster_whisper.utils import download_model

    base = PROJECT_ROOT / "whisper-models"
    base.mkdir(parents=True, exist_ok=True)
    for name in args.models:
        dest = base / name
        if dest.is_dir() and (dest / "model.bin").is_file():
            print(f"[skip] {name} already at {dest}", flush=True)
            continue
        print(f"[download] {name} -> {dest} (may take a while) ...", flush=True)
        download_model(name, output_dir=str(dest))
        print(f"[ok] {name}", flush=True)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()
