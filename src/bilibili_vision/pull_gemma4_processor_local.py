"""
把官方 Gemma 4 的 processor_config.json 写入本地模型目录，之后 serve 可直接
AutoProcessor.from_pretrained(本地)，无需每次从 Hugging Face 拉预处理。

来源：google/gemma-4-31B-it（仅 JSON，无权重；与 abliterated 权重共用同一架构处理器）。

用法（在 bilibili-transcript-oneclick-vision 目录下）：
  venv_gemma4\\Scripts\\python pull_gemma4_processor_local.py
  venv_gemma4\\Scripts\\python pull_gemma4_processor_local.py --model D:\\path\\to\\your\\Gemma-dir
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from bilibili_vision.paths import PROJECT_ROOT

DEFAULT_MODEL_DIR = PROJECT_ROOT / "models" / "Gemma-4-31B-it-abliterated"
RESOLVE_URL = (
    "https://huggingface.co/google/gemma-4-31B-it/resolve/main/processor_config.json"
)


def main() -> int:
    p = argparse.ArgumentParser(description="下载 processor_config.json 到本地模型目录")
    p.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL_DIR,
        help="本地 HF 模型目录（须已有 tokenizer 与 config.json）",
    )
    p.add_argument(
        "--url",
        default=RESOLVE_URL,
        help="官方 processor_config.json 地址（可换其它 gemma-4-*-it 变体）",
    )
    args = p.parse_args()
    model_dir = args.model.expanduser().resolve()
    if not model_dir.is_dir():
        print(f"错误：目录不存在：{model_dir}", file=sys.stderr)
        return 2
    dest = model_dir / "processor_config.json"
    if dest.is_file():
        print(f"已存在，跳过下载：{dest}")
        return 0
    print(f"正在下载：{args.url}\n写入：{dest}", flush=True)
    req = urllib.request.Request(
        args.url,
        headers={"User-Agent": "pull_gemma4_processor_local/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        print(f"下载失败：{e}", file=sys.stderr)
        return 2
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        print(f"不是合法 JSON：{e}", file=sys.stderr)
        return 2
    if not isinstance(data, dict) or data.get("processor_class") != "Gemma4Processor":
        print("警告：JSON 形态与预期 Gemma4Processor 不符，仍写入文件。", file=sys.stderr)
    dest.write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    print("完成。请重新启动 serve；不应再出现「从 Hugging Face 加载预处理」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
