"""
Gemma 4（HF 目录）+ bitsandbytes 4-bit 量化，本地 OpenAI 兼容 HTTP 服务。

  venv_gemma4\\Scripts\\python serve_gemma4_4bit.py

GUI：API 与模型 → 首选 OpenAI；根 URL http://127.0.0.1:18090/v1；Key 任意；模型名 gemma-4-31b-4bit

若本地目录仅有权重+tokenizer、缺少 preprocessor/processor JSON：默认从 Hugging Face「google/gemma-4-31B-it」
拉取多模态预处理（需联网或缓存）；可用 --processor-from / --no-processor-fallback 调整。

依赖：CUDA 版 PyTorch + bitsandbytes + Pillow（与显卡驱动/CUDA 版本需匹配，5090 须 cu128 版 torch 与带 libbitsandbytes_cuda128.dll 的 bitsandbytes；加载期 0xC0000005 可先 `pip install -U bitsandbytes` 并试 `--device-map single`）。

默认 **temperature=0（贪心解码）** 最稳；可选采样时仍带 top_p。
并启用 repetition_penalty、no_repeat_ngram，以及对异常「接龙/字母乱码」的 **输出截断**（流式与非流式均生效）。
可用命令行或请求 JSON 覆盖各参数；关闭截断：`--no-output-runaway-guard`。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

# Windows：尽量让本进程 stdout/stderr 走 UTF-8，避免 GUI 管道里混入系统 ANSI/GBK 导致解码线程报错
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# tqdm / Hub 进度条少用 Unicode 块字符，降低管道乱码概率
os.environ.setdefault("TQDM_ASCII", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _windows_force_console_utf8() -> None:
    """
    GUI 直接启动 python.exe（不经 cmd chcp 65001）时，控制台常为 GBK；PyTorch 等内部 subprocess 若按 UTF-8
    读管道会触发 _readerthread UnicodeDecodeError。尽早把本进程控制台改为 UTF-8（与 chcp 65001 等效）。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
    except Exception:
        pass


_windows_force_console_utf8()

from bilibili_vision.paths import PROJECT_ROOT as ROOT


class OpenAIChatCompletionsBody(BaseModel):
    """与 OpenAI /v1/chat/completions 兼容的请求体（仅用字段子集；忽略未知字段）。"""

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[dict[str, Any]]
    max_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool | None = False
    repetition_penalty: float | None = None
    no_repeat_ngram_size: int | None = None

    @field_validator("messages")
    @classmethod
    def _messages_nonempty(cls, v: list) -> list:
        if not v:
            raise ValueError("messages 须为非空数组")
        return v


_RUNAWAY_NOTE = (
    "\n\n[本段已在服务端截断：检测到异常重复或乱码拼接。"
    "未传 temperature 时本服务默认为 0（贪心）；若仍频繁出现，多为权重/量化问题，可换官方 Gemma-it 或非 abliterated 快照。]"
)


def _runaway_cut_index(text: str) -> int | None:
    """若文本出现崩溃式重复，返回应截断的起始下标；否则 None。"""
    if len(text) < 20:
        return None
    cut: int | None = None

    def consider(idx: int) -> None:
        nonlocal cut
        if idx >= 0 and (cut is None or idx < cut):
            cut = idx

    for m in re.finditer(r"([^\s\r\n\u3000])\1{9,}", text):
        consider(m.start())
    for m in re.finditer(r"一{14,}", text):
        consider(m.start())
    matches = [m.start() for m in re.finditer(re.escape("一个一个"), text)]
    if len(matches) >= 8:
        consider(matches[4])
    m = re.search(r"(?:\b[a-zA-Z]\b\s+){24,}", text)
    if m:
        consider(m.start())
    m = re.search(r"(?:\b[a-zA-Z]{1,2}\s*/\s*){16,}", text, re.I)
    if m:
        consider(m.start())
    if len(text) > 400:
        w = min(900, len(text))
        tail = text[-w:]
        if len(tail) >= 64:
            ratio = len(set(tail)) / len(tail)
            if ratio < 0.085:
                consider(len(text) - w)
    return cut


def _apply_runaway_truncate(text: str) -> tuple[str, bool]:
    idx = _runaway_cut_index(text)
    if idx is None:
        return text, False
    frag = text[:idx].rstrip()
    if not frag:
        return _RUNAWAY_NOTE.strip(), True
    return frag + _RUNAWAY_NOTE, True


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gemma 4 HF + 4-bit BNBF + OpenAI-compatible server")
    p.add_argument(
        "--model",
        type=Path,
        default=ROOT / "models" / "Gemma-4-31B-it-abliterated",
        help="本地 HF 模型目录",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--port",
        type=int,
        default=18090,
        help="默认 18090（8090 易被其它软件占用导致 POST 422）；可用 --port 8090 显式指定",
    )
    p.add_argument(
        "--listen-model-id",
        default="gemma-4-31b-4bit",
        help="客户端 /v1/chat/completions 的 model 字段须与此一致",
    )
    p.add_argument(
        "--max-model-len",
        type=int,
        default=8192,
        help="最大上下文 token（含输入；过大易 OOM，5090 建议 4k～16k 试）",
    )
    p.add_argument(
        "--processor-from",
        default="google/gemma-4-31B-it",
        metavar="HF_ID",
        help=(
            "仅权重+tokenizer 的快照缺少 preprocessor/processor 时：从此 HF 模型加载图像/视频/音频预处理；"
            "分词器仍用 --model 目录（需联网或缓存已存在）"
        ),
    )
    p.add_argument(
        "--no-processor-fallback",
        action="store_true",
        help="禁用从 Hub 补全处理器；本地目录不完整则直接失败",
    )
    p.add_argument(
        "--default-temperature",
        type=float,
        default=0.0,
        help="请求未带 temperature 时的默认温度（0=贪心解码，最抑重复；>0 为采样）",
    )
    p.add_argument(
        "--default-top-p",
        type=float,
        default=0.82,
        help="采样时默认 nucleus top_p（仅 temperature>0 时生效）",
    )
    p.add_argument(
        "--repetition-penalty",
        type=float,
        default=1.22,
        help="抑制「一个词接龙」式重复；请求体 repetition_penalty 可覆盖",
    )
    p.add_argument(
        "--no-repeat-ngram",
        type=int,
        default=6,
        metavar="N",
        help="禁止最近 N-gram 重复；0 表示关闭；请求体 no_repeat_ngram_size 可覆盖",
    )
    p.add_argument(
        "--no-output-runaway-guard",
        action="store_true",
        help="关闭对异常重复/乱码输出的自动截断（调试用）",
    )
    p.add_argument(
        "--device-map",
        choices=("auto", "single"),
        default="single",
        help=(
            "权重放置策略：single（默认）整模上 cuda:0，RTX 5090 + bitsandbytes 在 auto 下易 0xC0000005；"
            "多卡时再选 auto"
        ),
    )
    p.add_argument(
        "--bnb-double-quant",
        action="store_true",
        help=(
            "启用 bitsandbytes 双重量化（略省显存；RTX 50 / 部分 Windows 环境加载期易原生崩溃，默认关闭）"
        ),
    )
    p.add_argument(
        "--attn-sdpa",
        action="store_true",
        help="使用 PyTorch SDPA 注意力（默认 eager，略慢但更稳，避免部分卡加载/推理异常）",
    )
    p.add_argument(
        "--low-cpu-mem-usage",
        action="store_true",
        help="from_pretrained 低 CPU 内存模式（默认关闭；Windows 上 mmap 路径偶发与量化加载不兼容）",
    )
    return p.parse_args()


def _load_gemma4_processor(
    model_path: Path,
    processor_from: str | None,
    *,
    trust_remote_code: bool = True,
):
    """
    优先从本地目录加载 Gemma4 AutoProcessor；若缺少 preprocessor/processor 配置（常见「仅权重」快照），
    则从 processor_from 拉取多模态子处理器并与本地 AutoTokenizer 合并。
    """
    from transformers import AutoProcessor, AutoTokenizer

    try:
        proc = AutoProcessor.from_pretrained(str(model_path), trust_remote_code=trust_remote_code)
        return proc, proc.tokenizer
    except OSError as e:
        msg = str(e).lower()
        recoverable = (
            "preprocessor_config.json" in msg
            or "processor_config.json" in msg
            or "feature extractor" in msg
        )
        if not recoverable or not processor_from:
            if recoverable and not processor_from:
                print(
                    "错误：本地模型目录缺少完整处理器配置，且已指定 --no-processor-fallback（或未提供 --processor-from）。\n"
                    "请从官方仓库复制 preprocessor_config.json / processor_config.json，或去掉 --no-processor-fallback。",
                    file=sys.stderr,
                )
                raise SystemExit(2) from e
            raise
    try:
        from transformers.models.gemma4.processing_gemma4 import Gemma4Processor
    except ImportError as e:
        print(
            "当前 transformers 无法导入 Gemma4Processor，请升级：pip install -U \"transformers>=4.51\"",
            file=sys.stderr,
        )
        raise SystemExit(2) from e

    print(
        f"本地目录缺少多模态处理器 JSON，从 Hugging Face 加载预处理：{processor_from!r}（权重仍用 {model_path}）",
        flush=True,
    )
    hub = AutoProcessor.from_pretrained(processor_from, trust_remote_code=trust_remote_code)
    if type(hub).__name__ != "Gemma4Processor":
        print(
            f"错误：{processor_from!r} 的处理器为 {type(hub).__name__}，需要 Gemma4Processor。请换 --processor-from。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    local_tok = AutoTokenizer.from_pretrained(str(model_path), trust_remote_code=trust_remote_code)
    chat_tmpl = getattr(local_tok, "chat_template", None) or getattr(hub, "chat_template", None)
    proc = Gemma4Processor(
        feature_extractor=hub.feature_extractor,
        image_processor=hub.image_processor,
        tokenizer=local_tok,
        video_processor=hub.video_processor,
        chat_template=chat_tmpl,
        image_seq_length=getattr(hub, "image_seq_length", 280),
        audio_seq_length=getattr(hub, "audio_seq_length", 750),
        audio_ms_per_token=getattr(hub, "audio_ms_per_token", 40),
    )
    return proc, local_tok


def main() -> None:
    args = _parse_args()
    # 管道/无控制台时保证 UTF-8 输出，避免 GUI 日志里中文变乱码
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass
    warnings.filterwarnings(
        "ignore",
        message=r".*_check_is_size.*",
        category=FutureWarning,
    )
    logging.getLogger("torch.utils.flop_counter").setLevel(logging.ERROR)

    model_path = args.model.expanduser().resolve()
    if not model_path.is_dir():
        print(f"错误：模型目录不存在：{model_path}", file=sys.stderr)
        raise SystemExit(2)

    try:
        import torch
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import StreamingResponse
        import uvicorn
        from transformers import AutoModelForImageTextToText, BitsAndBytesConfig
    except ImportError as e:
        print(
            "缺少依赖。请先安装：pip install -r requirements-gemma4-4bit.txt\n"
            f"详情：{e}",
            file=sys.stderr,
        )
        raise SystemExit(2) from e

    print(f"加载模型（4-bit）… {model_path}", flush=True)
    try:
        print(
            f"PyTorch {torch.__version__}，CUDA 可用：{torch.cuda.is_available()}",
            flush=True,
        )
        if torch.cuda.is_available():
            try:
                print(f"GPU0：{torch.cuda.get_device_name(0)}", flush=True)
            except Exception:
                pass
        use_dq = bool(getattr(args, "bnb_double_quant", False))
        quant = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=use_dq,
        )
        if not use_dq:
            print("bitsandbytes：已关闭双重量化（5090 等默认更稳；需省显存可加 --bnb-double-quant）", flush=True)
        proc_src = None if args.no_processor_fallback else (args.processor_from or None)
        print(
            "正在加载处理器与 tokenizer（若从 Hub 拉 processor 可能需联网）…",
            flush=True,
        )
        processor, tokenizer = _load_gemma4_processor(
            model_path, proc_src, trust_remote_code=True
        )
        print(
            "正在以 4-bit 加载权重（大模型可能占用 15GB+ 显存，需数分钟；"
            "RTX 50 系请确认已装匹配的 CUDA 版 PyTorch 与 bitsandbytes）…",
            flush=True,
        )
        dmap: str | dict[str, int] = (
            {"": 0} if getattr(args, "device_map", "single") == "single" else "auto"
        )
        if dmap != "auto":
            print(f"device_map={dmap!r}（默认 single，多卡请 --device-map auto）", flush=True)
        else:
            print("device_map=auto（Accelerate 自动分配）", flush=True)
        low_cpu = bool(getattr(args, "low_cpu_mem_usage", False))
        if not low_cpu:
            print("from_pretrained：low_cpu_mem_usage=False（Windows+量化加载更稳）", flush=True)
        attn_impl: str | None = None if getattr(args, "attn_sdpa", False) else "eager"
        if attn_impl:
            print("注意力：eager（更稳；可加 --attn-sdpa 换 SDPA）", flush=True)
        fp_kw: dict[str, Any] = {
            "pretrained_model_name_or_path": str(model_path),
            "quantization_config": quant,
            "device_map": dmap,
            "trust_remote_code": True,
            "low_cpu_mem_usage": low_cpu,
        }
        if attn_impl:
            fp_kw["attn_implementation"] = attn_impl
        model = AutoModelForImageTextToText.from_pretrained(**fp_kw)
        model.eval()
    except Exception:
        print(
            "\n======== Gemma 服务：模型加载失败（请把下面整段复制排查）========\n",
            file=sys.stderr,
            flush=True,
        )
        import traceback

        traceback.print_exc(file=sys.stderr)
        print(
            "\n常见原因：① 显存不足（OOM）② bitsandbytes / PyTorch 与显卡驱动不匹配 "
            "③ 模型目录不完整 ④ RTX 5090 若仍见 0xC0000005：勿与旧版同时使用「双重量化 + device_map=auto」；"
            "当前默认已为 single + 无双重量化 + eager 注意力。\n"
            "建议：在项目目录运行 `python check_local_model.py`；"
            "或在 CMD 中直接运行 venv_gemma4\\Scripts\\python.exe -u serve_gemma4_4bit.py ... 查看完整输出。\n"
            "================================================================\n",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1) from None

    dev = next(model.parameters()).device
    print(f"设备：{dev}，监听 http://{args.host}:{args.port}/v1", flush=True)

    from PIL import Image

    _MAX_INLINE_IMAGE_BYTES = 15 * 1024 * 1024

    def _fetch_http_image(url: str) -> Image.Image:
        pu = urllib.parse.urlparse(url)
        if pu.scheme not in ("http", "https"):
            raise ValueError("仅支持 http(s) 图片 URL")
        req = urllib.request.Request(
            url, headers={"User-Agent": "serve_gemma4_4bit/1.0"}, method="GET"
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read(_MAX_INLINE_IMAGE_BYTES + 1)
        except urllib.error.URLError as e:
            raise ValueError(f"无法下载图片：{e}") from e
        if len(raw) > _MAX_INLINE_IMAGE_BYTES:
            raise ValueError("图片过大（>15MB）")
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def _data_uri_to_pil(data_uri: str) -> Image.Image:
        if "," not in data_uri:
            raise ValueError("无效的 data URI")
        head, b64part = data_uri.split(",", 1)
        raw = base64.standard_b64decode(b64part)
        if len(raw) > _MAX_INLINE_IMAGE_BYTES:
            raise ValueError("内联图片过大（>15MB）")
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def _openai_image_url_part_to_pil(part: dict[str, Any]) -> Image.Image | None:
        iu = part.get("image_url")
        if isinstance(iu, str):
            url = iu.strip()
        elif isinstance(iu, dict):
            url = str(iu.get("url") or "").strip()
        else:
            return None
        if not url:
            return None
        if url.startswith("data:"):
            try:
                return _data_uri_to_pil(url)
            except Exception:
                return None
        if url.startswith(("http://", "https://")):
            try:
                return _fetch_http_image(url)
            except Exception:
                return None
        return None

    def _normalize_openai_messages(msgs: list[Any]) -> list[dict[str, Any]]:
        """
        - 字符串 content → [{"type":"text","text":...}]（避免按字符迭代）
        - OpenAI 多模态块：text / image_url（data URI 或 http(s)）→ Gemma 处理器常用的 image 块
        """
        out: list[dict[str, Any]] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            row = dict(m)
            c = row.get("content")
            parts: list[dict[str, Any]] = []
            if isinstance(c, str):
                parts.append({"type": "text", "text": c})
            elif isinstance(c, list):
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get("type")
                    if pt == "text":
                        parts.append({"type": "text", "text": str(p.get("text") or "")})
                    elif pt == "image_url":
                        pil = _openai_image_url_part_to_pil(p)
                        if pil is not None:
                            parts.append({"type": "image", "image": pil})
                    elif pt == "image" and p.get("image") is not None:
                        im = p["image"]
                        if isinstance(im, Image.Image):
                            parts.append({"type": "image", "image": im.convert("RGB")})
            else:
                parts.append({"type": "text", "text": str(c) if c is not None else ""})
            has_text = any(
                x.get("type") == "text" and str(x.get("text") or "").strip() for x in parts
            )
            has_image = any(x.get("type") == "image" for x in parts)
            if has_image and not has_text:
                parts.insert(0, {"type": "text", "text": "请描述或分析这些图片。"})
            row["content"] = parts
            out.append(row)
        return out

    app = FastAPI(title="gemma4-4bit-local")

    @app.get("/v1/models")
    def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": args.listen_model_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "local",
                }
            ],
        }

    # 勿在签名里使用 Request：FastAPI+Pydantic 2.12 会把 Request 变成 ForwardRef，OpenAPI 失败且 POST 被误判为 query
    @app.post("/v1/chat/completions")
    async def chat_completions(body: OpenAIChatCompletionsBody):
        from threading import Thread

        from transformers.generation.streamers import TextIteratorStreamer

        messages = body.messages

        req_model = body.model
        if req_model is not None and str(req_model).strip():
            if str(req_model).strip() != args.listen_model_id:
                raise HTTPException(
                    400,
                    detail=(
                        f"model 须与启动参数 --listen-model-id 一致：{args.listen_model_id!r}，"
                        f"收到 {req_model!r}"
                    ),
                )

        max_tokens = int(body.max_tokens or 1024)
        max_tokens = max(1, min(max_tokens, 8192))
        temperature = float(
            body.temperature
            if body.temperature is not None
            else args.default_temperature
        )
        temperature = max(0.0, min(temperature, 2.0))

        rep_pen = float(
            body.repetition_penalty
            if body.repetition_penalty is not None
            else args.repetition_penalty
        )
        rep_pen = max(1.0, min(rep_pen, 1.55))
        ngram = int(
            body.no_repeat_ngram_size
            if body.no_repeat_ngram_size is not None
            else args.no_repeat_ngram
        )
        ngram = max(0, min(ngram, 10))
        top_p_req = body.top_p
        top_p_eff = (
            float(top_p_req)
            if top_p_req is not None
            else float(args.default_top_p)
        )
        top_p_eff = max(0.05, min(top_p_eff, 0.999))

        max_ctx = max(512, args.max_model_len - max_tokens)
        messages_n = _normalize_openai_messages(messages)
        try:
            inputs_m = processor.apply_chat_template(
                messages_n,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
                add_generation_prompt=True,
                processor_kwargs={"truncation": True, "max_length": max_ctx},
            )
        except Exception as e:
            raise HTTPException(400, f"apply_chat_template 失败：{e}") from e

        enc: dict[str, Any] = {}
        for k, v in dict(inputs_m).items():
            if v is None:
                continue
            if isinstance(v, torch.Tensor):
                enc[k] = v.to(dev)
            else:
                enc[k] = v
        enc.pop("offset_mapping", None)

        in_len = int(enc["input_ids"].shape[1])
        max_new = min(max_tokens, max(1, args.max_model_len - in_len))

        gen_kw: dict[str, Any] = {
            "max_new_tokens": max_new,
            "do_sample": temperature > 0.001,
            "pad_token_id": tokenizer.pad_token_id or tokenizer.eos_token_id,
            "repetition_penalty": rep_pen,
        }
        if ngram > 0:
            gen_kw["no_repeat_ngram_size"] = ngram
        if gen_kw["do_sample"]:
            gen_kw["temperature"] = temperature
            gen_kw["top_p"] = top_p_eff

        stream_on = bool(body.stream)
        if stream_on:
            streamer = TextIteratorStreamer(
                tokenizer, skip_prompt=True, skip_special_tokens=True
            )
            gkw = dict(gen_kw)
            gkw["streamer"] = streamer
            enc_gen = dict(enc)

            def _run_generate() -> None:
                try:
                    with torch.inference_mode():
                        model.generate(**enc_gen, **gkw)
                except Exception as e:
                    logging.exception("stream generate 失败：%s", e)

            worker = Thread(target=_run_generate, daemon=True)
            cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())
            mid = args.listen_model_id

            def _sse() -> Any:
                worker.start()
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": mid,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"role": "assistant"},
                                    "finish_reason": None,
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                n_out = 0
                emitted_len = 0
                acc = ""
                guard = not args.no_output_runaway_guard
                for piece in streamer:
                    if piece:
                        acc += piece
                    if guard and acc:
                        win = acc[-1600:] if len(acc) > 1600 else acc
                        li = _runaway_cut_index(win)
                        if li is not None:
                            gc = len(acc) - len(win) + li
                            safe_core = acc[:gc].rstrip()
                            if emitted_len >= len(safe_core):
                                delta = _RUNAWAY_NOTE
                            else:
                                delta = safe_core[emitted_len:] + _RUNAWAY_NOTE
                            if delta:
                                n_out += len(delta)
                                yield (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "id": cid,
                                            "object": "chat.completion.chunk",
                                            "created": created,
                                            "model": mid,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {"content": delta},
                                                    "finish_reason": None,
                                                }
                                            ],
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                )
                            print(
                                "[runaway guard] truncated stream completion",
                                flush=True,
                            )
                            break
                    if piece:
                        delta_out = acc[emitted_len:]
                        if delta_out:
                            n_out += len(delta_out)
                            yield (
                                "data: "
                                + json.dumps(
                                    {
                                        "id": cid,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": mid,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": delta_out},
                                                "finish_reason": None,
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            )
                            emitted_len = len(acc)
                worker.join(timeout=7200)
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "id": cid,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": mid,
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"
                print(
                    f"[generate stream] {in_len}+~{n_out} chars in (see tokenizer for tok)",
                    flush=True,
                )

            return StreamingResponse(_sse(), media_type="text/event-stream")

        t0 = time.perf_counter()
        with torch.inference_mode():
            out = model.generate(**enc, **gen_kw)
        dt = time.perf_counter() - t0

        full_ids = out[0]
        new_ids = full_ids[in_len:]
        text = tokenizer.decode(new_ids, skip_special_tokens=True).strip()
        if not args.no_output_runaway_guard:
            text, tr = _apply_runaway_truncate(text)
            if tr:
                print("[runaway guard] truncated non-stream completion", flush=True)

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        resp = {
            "id": cid,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": args.listen_model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": in_len,
                "completion_tokens": int(new_ids.shape[0]),
                "total_tokens": int(full_ids.shape[0]),
            },
        }
        print(f"[generate] {in_len}+{new_ids.shape[0]} tok in {dt:.2f}s", flush=True)
        return resp

    @app.get("/health")
    def health() -> dict[str, str]:
        # service：供 GUI 自检，区分「同端口上其它 FastAPI / 代理」避免误判为 Gemma
        return {
            "status": "ok",
            "model": args.listen_model_id,
            "service": "serve_gemma4_4bit",
        }

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
