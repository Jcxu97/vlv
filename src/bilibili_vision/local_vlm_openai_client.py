"""
本机 OpenAI 兼容视觉接口（单图 / 视频抽一帧）→ HTTP chat + image_url。

默认搭配本项目的 serve_gemma4_4bit（Gemma 4 4-bit）；其它实现只要支持相同 API 也可用。

默认用标准库 urllib 发 POST，不依赖 openai SDK（避免其 jiter 等原生扩展在 Windows
「智能应用控制」下被拦截导致 DLL load failed）。

若需走官方 SDK：加参数 --use-openai-sdk，或环境变量 LOCAL_VLM_USE_OPENAI_SDK=1（可能触发 jiter）。

  set OPENAI_BASE_URL=http://127.0.0.1:18090/v1
  venv_gemma4\\Scripts\\python.exe local_vlm_openai_client.py --model gemma-4-31b-4bit --image photo.jpg
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bilibili_vision.paths import PROJECT_ROOT as ROOT


def _ffmpeg_exe() -> Path | None:
    d = ROOT / "ffmpeg"
    ex = d / "ffmpeg.exe" if sys.platform == "win32" else d / "ffmpeg"
    if ex.is_file():
        return ex
    which = shutil.which("ffmpeg")
    return Path(which) if which else None


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


def _parse_assistant_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
        return "".join(chunks).strip()
    return (str(content) if content is not None else "").strip()


def _urlopen_maybe_no_proxy(req: urllib.request.Request, *, timeout: float):
    """本机地址不走系统代理，与 llm_analyze 行为一致。"""
    try:
        pu = urllib.parse.urlparse(req.full_url)
    except ValueError:
        return urllib.request.urlopen(req, timeout=timeout)
    h = (pu.hostname or "").strip().lower()
    if h in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def chat_completions_urllib(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list,
    max_tokens: int,
    temperature: float,
    top_p: float,
    timeout_sec: float = 600.0,
) -> str:
    root = base_url.strip().rstrip("/")
    if not root:
        raise ValueError("base_url 为空")
    url = root + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with _urlopen_maybe_no_proxy(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body[:4000]}") from e
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"响应非 JSON：{raw[:800]!r}") from e
    if not isinstance(obj, dict):
        raise RuntimeError(f"意外响应类型：{type(obj)}")
    err = obj.get("error")
    if isinstance(err, dict) and err.get("message"):
        raise RuntimeError(str(err.get("message")))
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"无 choices：{raw[:1200]!r}")
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        raise RuntimeError(f"无 message：{raw[:1200]!r}")
    return _parse_assistant_content(msg.get("content"))


def main() -> None:
    p = argparse.ArgumentParser(description="Local VLM via OpenAI-compatible HTTP (image in chat).")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:18090/v1"))
    p.add_argument(
        "--model",
        default=os.environ.get("VLM_MODEL", "gemma-4-31b-4bit"),
        help="须与 serve 的 --listen-model-id / 模型名一致",
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
    p.add_argument(
        "--quiet",
        action="store_true",
        help="仅将模型回复打印到 stdout（供 GUI 子进程解析）",
    )
    p.add_argument(
        "--use-openai-sdk",
        action="store_true",
        help="使用 openai 官方包（会加载 jiter 等原生扩展；默认用 urllib，避免智能应用控制拦 DLL）",
    )
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
        if not args.quiet:
            print(f"POST {args.base_url}  model={args.model}", flush=True)
        env_sdk = os.environ.get("LOCAL_VLM_USE_OPENAI_SDK", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        use_sdk = bool(args.use_openai_sdk) or env_sdk
        try:
            if use_sdk:
                try:
                    from openai import OpenAI
                except ImportError as e:
                    raise SystemExit(
                        "已设置 LOCAL_VLM_USE_OPENAI_SDK=1，但未安装 openai："
                        "venv_gemma4\\Scripts\\pip install openai"
                    ) from e

                client = OpenAI(
                    api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
                    base_url=args.base_url.rstrip("/"),
                )
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=messages,
                    max_tokens=args.max_tokens,
                    temperature=0.7,
                    top_p=0.8,
                )
                choice = resp.choices[0]
                text = _parse_assistant_content(getattr(choice.message, "content", None))
            else:
                text = chat_completions_urllib(
                    base_url=args.base_url,
                    api_key=os.environ.get("OPENAI_API_KEY", "") or "EMPTY",
                    model=args.model,
                    messages=messages,
                    max_tokens=args.max_tokens,
                    temperature=0.7,
                    top_p=0.8,
                )
        except Exception as e:
            import traceback

            tag = "[local_vlm_openai]"
            print(f"{tag} {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            body = getattr(e, "body", None)
            if body is not None:
                print(f"{tag} body: {body!s}"[:4000], file=sys.stderr, flush=True)
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None and hasattr(resp_obj, "text"):
                try:
                    tx = (resp_obj.text or "")[:4000]
                    if tx.strip():
                        print(f"{tag} HTTP: {tx}", file=sys.stderr, flush=True)
                except Exception:
                    pass
            traceback.print_exc(limit=12, file=sys.stderr)
            raise SystemExit(1) from e
        print(text if text else "(empty response)")
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    main()
