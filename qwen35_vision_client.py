"""
Call a local OpenAI-compatible server (transformers serve / vLLM) with image or one video frame.

  set OPENAI_BASE_URL=http://127.0.0.1:8000/v1
  venv_qwen35\Scripts\python.exe qwen35_vision_client.py --image photo.jpg --prompt "描述画面中的文字与内容"

  venv_qwen35\Scripts\python.exe qwen35_vision_client.py --video clip.mp4 --at 5 --prompt "这张画面里有什么？"
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _ffmpeg_exe() -> Path | None:
    d = ROOT / "ffmpeg"
    ex = d / "ffmpeg.exe" if sys.platform == "win32" else d / "ffmpeg"
    return ex if ex.is_file() else None


def extract_video_frame(video: Path, t_sec: float, out_png: Path) -> None:
    ff = _ffmpeg_exe()
    if not ff:
        raise FileNotFoundError(
            "未找到项目内 ffmpeg/ffmpeg.exe；请安装 FFmpeg 或放入便携目录 ffmpeg/。"
        )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ff),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(t_sec),
        "-i",
        str(video.resolve()),
        "-frames:v",
        "1",
        str(out_png),
    ]
    subprocess.run(cmd, check=True)


def image_to_data_url(path: Path) -> str:
    data = path.read_bytes()
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def main() -> None:
    p = argparse.ArgumentParser(description="Qwen3.5 local VLM smoke test (OpenAI-compatible server).")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"))
    p.add_argument(
        "--model",
        default=os.environ.get("QWEN35_MODEL", "Qwen/Qwen3.5-27B-GPTQ-Int4"),
        help="Must match the model id passed to: transformers serve <model>",
    )
    p.add_argument("--image", type=Path, help="Image file (jpg/png/webp)")
    p.add_argument("--video", type=Path, help="Video file; grabs one frame at --at seconds")
    p.add_argument("--at", type=float, default=1.0, dest="at_sec", help="Seek time for --video (seconds)")
    p.add_argument(
        "--prompt",
        default="请用中文简要描述画面中的文字、界面元素和正在展示的内容。",
        help="User text alongside the image",
    )
    p.add_argument("--max-tokens", type=int, default=2048)
    args = p.parse_args()

    if bool(args.image) == bool(args.video):
        p.error("Specify exactly one of --image or --video")

    img_path: Path
    tmp: tempfile.TemporaryDirectory | None = None
    try:
        if args.image:
            img_path = args.image.expanduser().resolve()
            if not img_path.is_file():
                raise FileNotFoundError(str(img_path))
        else:
            vid = args.video.expanduser().resolve()
            if not vid.is_file():
                raise FileNotFoundError(str(vid))
            tmp = tempfile.TemporaryDirectory(prefix="bto_vision_")
            img_path = Path(tmp.name) / "frame.png"
            extract_video_frame(vid, args.at_sec, img_path)

        try:
            from openai import OpenAI
        except ImportError as e:
            raise SystemExit("请安装 openai:  venv_qwen35\\Scripts\\pip install openai") from e

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"), base_url=args.base_url.rstrip("/"))
        url = image_to_data_url(img_path)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": args.prompt},
                ],
            }
        ]
        print(f"POST {args.base_url}  model={args.model}", flush=True)
        resp = client.chat.completions.create(
            model=args.model,
            messages=messages,
            max_tokens=args.max_tokens,
            temperature=0.7,
            top_p=0.8,
        )
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        print(text if text else "(empty response)")
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    main()
