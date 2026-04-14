"""
使用免费在线大模型分析合并文稿（urllib，无额外依赖）。

在系统或用户环境变量中配置其一即可：
  GEMINI_API_KEY   — Google AI Studio 免费申请：https://aistudio.google.com/apikey
  GEMINI_MODEL     — 可选，固定模型 ID（否则按列表自动尝试 gemini-2.5-flash 等）
  GROQ_API_KEY     — GroqCloud 免费申请：https://console.groq.com/keys

也可在本目录放置 local_api_keys.py（已加入 .gitignore），内写各平台 API Key 与可选模型名；
若环境变量未设置则自动读取（环境变量优先）。

支持：Gemini、OpenAI（GPT）、Groq、Anthropic（Claude）、xAI（Grok）。均为 HTTPS + 标准库 urllib。

说明：免费额度与限速以各平台为准；勿把含密钥的仓库或 zip 公开分享。

大模型输出约定为纯文本（无 Markdown）；GUI「分析报告」页会对标题与元信息分层加粗并调整字号。
主窗口「分析报告」页底部可与同一 API 多轮问答（合并文稿与报告节选作为上下文；跟进对话可结合常识与推理，不限于逐字复述文稿）。
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from bilibili_vision.paths import PROJECT_ROOT

# 控制单次请求体大小，避免超出免费档常见限制
MAX_MERGED_CHARS = 100_000


class OpenAICompatibleRequestCancelled(Exception):
    """GUI 或调用方主动中止本机 OpenAI 兼容请求（如用户点击「停止」）。"""


def _is_cancelled(ev: threading.Event | None) -> bool:
    return ev is not None and ev.is_set()
# 仅用于底部多轮对话（非首屏分析报告）；略提高温度，回答更自然
CHAT_TEMPERATURE = 0.65

# 「报告与对话」附带 video_analysis_deep.json 中截取的代表帧：默认开启；设 CHAT_ATTACH_VISION_FRAMES=0 关闭
# CHAT_VISION_MAX_FRAMES 默认 20（上限 32）


def _env_bool_chat_attach_vision(default: bool = True) -> bool:
    raw = os.environ.get("CHAT_ATTACH_VISION_FRAMES", "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def collect_timeline_frame_paths(json_path: Path) -> list[Path]:
    """
    从 video_analysis_deep.json 的 timeline 收集代表帧磁盘路径（去重、按时间轴顺序）。
    用于「报告与对话」向多模态模型附带真实 JPEG，而非仅 OCR/VLM 文本摘要。
    """
    if not _env_bool_chat_attach_vision():
        return []
    if not json_path.is_file():
        return []
    raw_max = os.environ.get("CHAT_VISION_MAX_FRAMES", "").strip()
    try:
        max_frames = int(raw_max) if raw_max.isdigit() else 20
    except ValueError:
        max_frames = 20
    max_frames = max(1, min(max_frames, 32))
    max_bytes = 12 * 1024 * 1024
    try:
        jd = json.loads(json_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(jd, dict):
        return []
    tl = jd.get("timeline")
    if not isinstance(tl, list):
        return []
    seen: set[str] = set()
    out: list[Path] = []
    for ev in tl:
        if not isinstance(ev, dict):
            continue
        ps = ev.get("path")
        if not isinstance(ps, str) or not ps.strip():
            continue
        p = Path(ps).expanduser()
        try:
            p = p.resolve()
        except OSError:
            continue
        if not p.is_file():
            continue
        try:
            if p.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= max_frames:
            break
    return out


def normalize_chat_prior_turns(
    prior_turns: Sequence[tuple[str, str] | tuple[str, str, bool]],
) -> list[tuple[str, str, bool]]:
    """统一为 (用户原文, 助手回复, 该轮是否附带画面帧)。"""
    out: list[tuple[str, str, bool]] = []
    for t in prior_turns:
        if len(t) == 3:
            out.append((t[0], t[1], bool(t[2])))
        else:
            out.append((t[0], t[1], False))
    return out


def _load_local_api_keys() -> None:
    """从同目录 local_api_keys.py 注入环境变量（仅当尚未设置时）。"""
    p = PROJECT_ROOT / "local_api_keys.py"
    if not p.is_file():
        return
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_bilibili_local_api_keys", p)
        if spec is None or spec.loader is None:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for key in (
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "XAI_API_KEY",
            "GEMINI_MODEL",
            "GROQ_MODEL",
            "OPENAI_MODEL",
            "OPENAI_BASE_URL",
            "ANTHROPIC_MODEL",
            "XAI_MODEL",
        ):
            if hasattr(mod, key):
                val = getattr(mod, key)
                if isinstance(val, str) and val.strip():
                    os.environ.setdefault(key, val.strip())
        if hasattr(mod, "LLM_PROVIDER"):
            lp = getattr(mod, "LLM_PROVIDER", "")
            if isinstance(lp, str):
                lv = lp.strip().lower()
                if lv in ("auto", "gemini", "openai", "groq", "anthropic", "xai"):
                    os.environ.setdefault("LLM_PROVIDER", lv)
    except Exception:
        pass


_load_local_api_keys()

Provider = Literal["gemini", "groq", "openai", "anthropic", "xai", "local"]


def _local_openai_env_triple() -> tuple[str, str, str]:
    """(api_key, base_url, model)，供本地 OpenAI 兼容（Gemma serve 等）。"""
    base = _normalize_openai_v1_base((os.environ.get("OPENAI_BASE_URL") or "").strip())
    model = (os.environ.get("OPENAI_MODEL") or "").strip()
    key = os.environ.get("OPENAI_API_KEY", "").strip() or "EMPTY"
    return key, base, model


def local_openai_provider_ready() -> bool:
    """环境变量是否已指向本机且含模型名（用于 resolve_provider(\"local\")）。"""
    _, base, model = _local_openai_env_triple()
    if not base or not model:
        return False
    return _url_is_loopback(base)

SYSTEM_ZH = (
    "你是专业的视频文稿分析师。你只根据用户提供的「合并文稿」作答；"
    "信息不足时明确写「文本中未体现」。禁止编造合并文稿里不存在的情节或设定；"
    "禁止使用与本文稿无关的固定模板（例如未出现「庇护所对比」时不要硬套）。\n"
    "【版式硬性要求】全文必须是纯文本、纯中文排版，禁止使用 Markdown 或类似符号："
    "禁止半角井号标题（不要用 #、##、###）；"
    "禁止星号列表或星号加粗（不要用单独的 * 或 ** 包裹文字）；"
    "不要用半角减号 - 充当列表符号。"
    "章节标题单独占一行，用「一、」「二、」「三、」开头；"
    "分条说明用「（1）」「（2）」或「1.」「2.」起行，必要时条目之间空一行；"
    "需要强调的词语用中文引号「」括起即可，不要用星号。"
)


def _truncate(merged: str) -> tuple[str, bool]:
    if len(merged) <= MAX_MERGED_CHARS:
        return merged, False
    return merged[:MAX_MERGED_CHARS], True


def _user_prompt(merged: str, truncated: bool) -> str:
    note = ""
    if truncated:
        note = f"\n（文稿已截断至前 {MAX_MERGED_CHARS} 字符，若分析不完整属正常。）\n"
    return (
        f"{note}"
        "以下是某 B 站视频的「合并文稿」，通常包含「弹幕」时间轴块与「字幕」口播/ASR 块。\n\n"
        "--- 合并文稿开始 ---\n"
        f"{merged}\n"
        "--- 合并文稿结束 ---\n\n"
        "请使用简体中文撰写，且遵守系统说明中的纯文本版式要求（禁止 # 与 * 等）。\n"
        "按下述三节输出；每节先写标题行（单独一行），空一行，再写正文：\n"
        "一、视频在讲什么（严格依据字幕/口播归纳）\n"
        "\n"
        "二、观众弹幕在讨论什么（归纳观点与梗，勿臆测画面）\n"
        "\n"
        "三、简要结论与观看建议（若文本过短则说明依据有限）\n"
        "\n"
        "第三节若含「观看建议」，可将「观看建议」单独起一行作为小标题，再接建议条目。\n"
    )


def _openai_compatible_timeout_sec(base_url: str) -> int:
    """本地大模型首包慢，对 127.0.0.1/localhost 默认放宽；可用 LLM_HTTP_TIMEOUT_SEC 覆盖。"""
    raw = os.environ.get("LLM_HTTP_TIMEOUT_SEC", "").strip()
    if raw:
        try:
            t = int(raw)
            return max(30, min(t, 7200))
        except ValueError:
            pass
    low = base_url.lower()
    if "127.0.0.1" in low or "localhost" in low:
        return 900
    return 120


def _friendly_urlopen_err(url: str, err: BaseException) -> str:
    """常见本机连接失败（如未启动本地 serve）时给出可操作说明。"""
    r = getattr(err, "reason", err)
    win = getattr(r, "winerror", None)
    errno = getattr(r, "errno", None)
    if win == 10061 or errno in (10061, 111):
        port_hint = ""
        if ":1234" in url:
            port_hint = (
                "\n\n（当前 URL 为 1234 端口，多为 LM Studio；若你启动的是本项目 Gemma，"
                "请改为 http://127.0.0.1:18090/v1 并运行 SERVE_GEMMA4_4BIT.bat。）"
            )
        return (
            "无法连接该地址：本机拒绝连接（没有程序在监听该端口，WinError 10061）。\n"
            "请先在本机启动 OpenAI 兼容服务，并把「本地对话」里的服务 URL 改成正在监听的地址（须含 /v1）。\n"
            "· 本项目 Gemma 4-bit：运行 SERVE_GEMMA4_4BIT.bat，"
            "URL 填 http://127.0.0.1:18090/v1 ，模型 gemma-4-31b-4bit。\n"
            "· 其它：Ollama 多为 http://127.0.0.1:11434/v1 ；LM Studio 常见 http://127.0.0.1:1234/v1（以软件显示为准）。\n"
            f"本次请求：{url}"
            + port_hint
        )
    low = str(err).lower()
    if "timed out" in low or "timeout" in low:
        return f"连接超时：{url}"
    return f"{err}\n请求：{url}"


def _http_post_json(
    url: str,
    payload: dict,
    headers: dict | None,
    timeout: int = 120,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    ctx = ssl.create_default_context()
    try:
        with _urlopen_request(req, timeout=timeout, context=ctx) as resp:
            if cancel_event is None:
                return json.loads(resp.read().decode("utf-8"))
            buf = b""
            while True:
                if _is_cancelled(cancel_event):
                    try:
                        resp.close()
                    except Exception:
                        pass
                    raise OpenAICompatibleRequestCancelled()
                try:
                    chunk = resp.read(4096)
                except Exception as e:
                    if _is_cancelled(cancel_event):
                        raise OpenAICompatibleRequestCancelled() from e
                    raise
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8"))
    except urllib.error.HTTPError as e:
        # HTTPError 是 URLError 子类，须先处理，否则读不到响应体（如 422 detail）
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        snippet = body or str(e)
        if len(snippet) > 2400:
            snippet = snippet[:2400] + "…"
        extra = ""
        if (
            e.code == 422
            and _url_is_loopback(url)
            and '"query"' in snippet
            and '"request"' in snippet
        ):
            extra = (
                "\n\n[说明] 该 422 表示当前 URL 对应的 /v1/chat/completions 不是「JSON body」式 OpenAI 接口，"
                "而是要求 URL 查询参数 request 的其它服务（与浏览器里 /health 看到的内容可能来自不同程序）。\n"
                "请 netstat -ano | findstr :<你的端口> 查 PID，结束误占端口的进程；"
                "或改用本项目新版默认端口：SERVE_GEMMA4_4BIT.bat（默认 18090），"
                "并把「本地对话」服务 URL 改为 http://127.0.0.1:18090/v1 。"
            )
        raise RuntimeError(f"HTTP {e.code} {e.reason}\n{snippet}\n请求：{url}{extra}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(_friendly_urlopen_err(url, e)) from e


def _http_post_openai_chat_sse_deltas(
    url: str,
    payload: dict,
    headers: dict[str, str] | None,
    timeout: int = 240,
    *,
    on_delta: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """POST chat/completions（stream:true），聚合正文并可选回调每个文本增量。"""
    pl = dict(payload)
    pl["stream"] = True
    data = json.dumps(pl, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    ctx = ssl.create_default_context()
    buf = b""
    parts: list[str] = []
    try:
        with _urlopen_request(req, timeout=timeout, context=ctx) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                body = resp.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"HTTP {status}\n{body[:2400]}\n请求：{url}")
            while True:
                if _is_cancelled(cancel_event):
                    try:
                        resp.close()
                    except Exception:
                        pass
                    raise OpenAICompatibleRequestCancelled()
                try:
                    raw = resp.read(4096)
                except Exception as e:
                    if _is_cancelled(cancel_event):
                        raise OpenAICompatibleRequestCancelled() from e
                    raise
                if not raw:
                    break
                buf += raw
                while True:
                    pos = buf.find(b"\n")
                    if pos < 0:
                        break
                    line = buf[:pos]
                    buf = buf[pos + 1 :]
                    s = line.decode("utf-8", errors="replace").strip()
                    if not s or s.startswith(":"):
                        continue
                    if not s.startswith("data:"):
                        continue
                    data_s = s[5:].strip()
                    if data_s == "[DONE]":
                        return "".join(parts).strip()
                    try:
                        obj = json.loads(data_s)
                    except json.JSONDecodeError:
                        continue
                    err = obj.get("error")
                    if isinstance(err, dict) and err.get("message"):
                        raise RuntimeError(str(err.get("message")))
                    for ch in obj.get("choices") or []:
                        if not isinstance(ch, dict):
                            continue
                        delta = ch.get("delta") or {}
                        if not isinstance(delta, dict):
                            continue
                        c = delta.get("content")
                        if c:
                            t = str(c)
                            parts.append(t)
                            if on_delta is not None:
                                on_delta(t)
    except OpenAICompatibleRequestCancelled:
        raise
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        snippet = body or str(e)
        if len(snippet) > 2400:
            snippet = snippet[:2400] + "…"
        raise RuntimeError(f"HTTP {e.code} {e.reason}\n{snippet}\n请求：{url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(_friendly_urlopen_err(url, e)) from e
    return "".join(parts).strip()


def _gemini_default_models() -> tuple[str, ...]:
    """按优先级尝试；旧 ID 会从 AI Studio 下线，见 https://ai.google.dev/gemini-api/docs/models"""
    override = os.environ.get("GEMINI_MODEL", "").strip()
    fallback = (
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    )
    if override:
        return (override,) + tuple(m for m in fallback if m != override)
    return fallback


SYSTEM_CHAT_ZH = (
    "你是视频文稿分析助手。每轮请求里会附上「合并文稿」与（如有）「分析报告节选」作为背景，帮助你理解视频在讲什么；"
    "这是上下文，不是答题范围限制。\n"
    "作答原则：若问题针对视频里说了什么、弹幕讨论什么，请优先依据背景文稿；文稿里没有的细节可以直说「文稿里没提到」，"
    "并在此基础上用你的常识、推理或建议把问题答完整，不要只用一句「文本中未体现」敷衍。\n"
    "若用户问策略、假设、延伸观点、创作或一般知识等与文稿弱相关的问题，请正常发挥模型能力；"
    "若用到文稿外的推断，用简短话标明「以下为推测/补充观点」即可。\n"
    "禁止把文稿里明显不存在的内容说成「视频里说过」；禁止假装引用不存在的原话。\n"
    "【版式】回复使用纯中文纯文本：禁止 Markdown（不要用 #、##、*、**、以半角减号开头的列表）；"
    "分条用「（1）」「（2）」或「1.」「2.」；强调用「」。"
)


def _gemini_generate(api_key: str, system: str, user: str) -> tuple[str, str]:
    """返回 (正文, 模型名)。"""
    models_try = _gemini_default_models()
    last_err: str | None = None
    gen_cfg = {
        "temperature": 0.35,
        "maxOutputTokens": 8192,
    }
    for api_ver in ("v1beta", "v1"):
        for model in models_try:
            url = (
                f"https://generativelanguage.googleapis.com/{api_ver}/models/"
                f"{model}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
            )
            # v1 generateContent 不接受 systemInstruction，需并入首条 user（见 Google API 报错 Unknown name systemInstruction）
            if api_ver == "v1beta":
                payload: dict = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": gen_cfg,
                }
            else:
                merged = "【系统设定】\n" + system + "\n\n【待分析内容】\n" + user
                payload = {
                    "contents": [{"role": "user", "parts": [{"text": merged}]}],
                    "generationConfig": gen_cfg,
                }
            try:
                out = _http_post_json(url, payload, None, timeout=120)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = str(e)
                last_err = f"{api_ver}/{model}: HTTP {e.code} {body[:800]}"
                continue
            except Exception as e:
                last_err = f"{api_ver}/{model}: {e}"
                continue
            try:
                parts = out["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError, TypeError) as e:
                last_err = f"{api_ver}/{model}: 解析响应失败 {e!s} raw={str(out)[:500]}"
                continue
            if text:
                return text, f"{model} ({api_ver})"
            last_err = f"{api_ver}/{model}: 空回复"
    raise RuntimeError("Gemini 调用失败：" + (last_err or "unknown"))


def _gemini_inline_user_parts(text: str, images: list[Path]) -> list[dict]:
    parts: list[dict] = []
    t = (text or "").strip()
    if t:
        parts.append({"text": t})
    for p in images:
        try:
            if not p.is_file():
                continue
            raw, mime = _read_image_bytes_for_vision(p)
            b64 = base64.standard_b64encode(raw).decode("ascii")
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        except OSError:
            continue
        except RuntimeError:
            raise
    if not parts:
        parts.append({"text": (text or "").strip() or "."})
    return parts


def _gemini_multiturn(
    api_key: str,
    system: str,
    prior_turns: list[tuple[str, str, bool]],
    user_payload: str,
    *,
    attach_vision_current: bool,
    vision_paths: list[Path] | None = None,
) -> tuple[str, str]:
    """prior 每项 (user, model, 该轮是否附图)；仅当对应标志为真时把 vision_paths 附在该轮 user 上。"""
    imgs0 = list(vision_paths or [])
    contents: list[dict] = []
    for u, a, att in prior_turns:
        u_imgs = imgs0 if att else []
        contents.append({"role": "user", "parts": _gemini_inline_user_parts(u, u_imgs)})
        contents.append({"role": "model", "parts": [{"text": a}]})
    last_imgs = imgs0 if attach_vision_current else []
    contents.append({"role": "user", "parts": _gemini_inline_user_parts(user_payload, last_imgs)})

    models_try = _gemini_default_models()
    last_err: str | None = None
    need_slow = bool(imgs0) and (attach_vision_current or any(t[2] for t in prior_turns))
    chat_gen_cfg = {
        "temperature": CHAT_TEMPERATURE,
        "maxOutputTokens": 8192,
    }
    for api_ver in ("v1beta", "v1"):
        for model in models_try:
            url = (
                f"https://generativelanguage.googleapis.com/{api_ver}/models/"
                f"{model}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
            )
            if api_ver == "v1beta":
                payload = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": contents,
                    "generationConfig": chat_gen_cfg,
                }
            else:
                primed = [
                    {"role": "user", "parts": [{"text": "【系统设定】\n" + system}]},
                    {"role": "model", "parts": [{"text": "好的，我会按上述设定协助你。"}]},
                ]
                payload = {
                    "contents": primed + contents,
                    "generationConfig": chat_gen_cfg,
                }
            try:
                to = 300 if need_slow else 120
                out = _http_post_json(url, payload, None, timeout=to)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = str(e)
                last_err = f"{api_ver}/{model}: HTTP {e.code} {body[:800]}"
                continue
            except Exception as e:
                last_err = f"{api_ver}/{model}: {e}"
                continue
            try:
                parts = out["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError, TypeError) as e:
                last_err = f"{api_ver}/{model}: 解析响应失败 {e!s} raw={str(out)[:500]}"
                continue
            if text:
                return text, f"{model} ({api_ver})"
            last_err = f"{api_ver}/{model}: 空回复"
    raise RuntimeError("Gemini 调用失败：" + (last_err or "unknown"))


def _groq_chat(api_key: str, system: str, user: str) -> tuple[str, str]:
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.35,
        "max_tokens": 8192,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        out = _http_post_json(url, payload, headers, timeout=120)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Groq HTTP {e.code}: {body[:1200]}") from e
    try:
        text = out["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Groq 解析失败: {e!s}") from e
    if not text:
        raise RuntimeError("Groq 返回空正文")
    return text, model


def _groq_multiturn(
    api_key: str,
    system: str,
    prior_turns: list[tuple[str, str, bool]],
    user_payload: str,
    *,
    attach_vision_current: bool,
    vision_paths: list[Path] | None = None,
) -> tuple[str, str]:
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    return _openai_compatible_multiturn(
        api_key,
        "https://api.groq.com/openai/v1",
        model,
        system,
        prior_turns,
        user_payload,
        attach_vision_current=attach_vision_current,
        vision_paths=vision_paths,
    )


def _openai_compatible_chat(
    api_key: str,
    base_url: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[str, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    to = _openai_compatible_timeout_sec(base_url)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.35 if temperature is None else float(temperature),
        "max_tokens": 8192 if max_tokens is None else int(max_tokens),
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        out = _http_post_json(url, payload, headers, timeout=to)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compatible HTTP {e.code}: {body[:1200]}") from e
    try:
        text = out["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"OpenAI-compatible 解析失败: {e!s}") from e
    if not text:
        raise RuntimeError("OpenAI-compatible 返回空正文")
    return text, model


def _openai_compatible_multiturn(
    api_key: str,
    base_url: str,
    model: str,
    system: str,
    prior_turns: list[tuple[str, str, bool]],
    user_payload: str,
    *,
    attach_vision_current: bool,
    vision_paths: list[Path] | None = None,
) -> tuple[str, str]:
    """OpenAI 兼容多轮；仅在 prior 各轮或当前轮标志为真时附 vision_paths。"""
    url = base_url.rstrip("/") + "/chat/completions"
    imgs0 = list(vision_paths or [])
    messages: list[dict] = [{"role": "system", "content": system}]
    for u, a, att in prior_turns:
        uimgs = imgs0 if att else []
        try:
            ucontent = _openai_user_content_parts(u, uimgs)
        except ValueError:
            ucontent = (u or "").strip() or "."
        messages.append({"role": "user", "content": ucontent})
        messages.append({"role": "assistant", "content": a})
    last_imgs = imgs0 if attach_vision_current else []
    try:
        ucontent = _openai_user_content_parts(user_payload, last_imgs)
    except ValueError:
        ucontent = (user_payload or "").strip() or "."
    messages.append({"role": "user", "content": ucontent})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": CHAT_TEMPERATURE,
        "max_tokens": 8192,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    to = _openai_compatible_timeout_sec(base_url)
    if imgs0 and (attach_vision_current or any(t[2] for t in prior_turns)):
        to = max(to, 300)
    try:
        out = _http_post_json(url, payload, headers, timeout=to)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compatible HTTP {e.code}: {body[:1200]}") from e
    try:
        raw = out["choices"][0]["message"]["content"]
        text = _parse_assistant_openai_content(raw)
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"OpenAI-compatible 解析失败: {e!s}") from e
    if not text:
        raise RuntimeError("OpenAI-compatible 返回空正文")
    return text, model


SYSTEM_LOCAL_CHAT_ZH = (
    "你是乐于助人的助手，默认用简体中文、自然口语化回答。"
    "需要时分条说明即可，不必使用 Markdown 标题或星号加粗。"
    "若用户上传证件、卡片类照片，只根据画面上真实可见的内容描述版式或字段含义；看不清或无法确认的字符勿编造。"
)

MAX_LOCAL_CHAT_MERGED_SNIPPET = 48_000

# 各 API 内联 base64 图片常见上限约 20MB；略保守避免网关拒绝
VISION_INLINE_IMAGE_MAX_BYTES = 15 * 1024 * 1024


def _guess_image_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "image/png"


def _read_image_bytes_for_vision(
    path: Path, *, max_bytes: int = VISION_INLINE_IMAGE_MAX_BYTES
) -> tuple[bytes, str]:
    """
    读取图片并保证编码后不超过 max_bytes。
    超限则用 Pillow 缩小尺寸并以 JPEG 重编码；需 pip install Pillow。
    """
    raw = path.read_bytes()
    if len(raw) <= max_bytes:
        return raw, _guess_image_mime(path)
    try:
        from PIL import Image
    except ImportError:
        mb = max(1, len(raw) // 1024 // 1024)
        raise RuntimeError(
            f"单张图片过大（约 {mb}MB），超过常见 API 上限 {max_bytes // 1024 // 1024}MB，且未安装 Pillow 无法自动压缩。"
            f"请执行 pip install Pillow，或手动缩小后重试：{path.name}"
        ) from None
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise RuntimeError(f"无法解码图片 {path.name}：{e}") from e
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode == "P":
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    w0, h0 = img.size
    scale = 1.0
    qualities = (90, 82, 74, 66, 58, 50, 42, 35)
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.LANCZOS  # type: ignore[attr-defined]
    while scale >= 0.1:
        w = max(32, int(w0 * scale))
        h = max(32, int(h0 * scale))
        imr = img.resize((w, h), resample) if (w, h) != (w0, h0) else img
        for q in qualities:
            buf = io.BytesIO()
            imr.save(buf, format="JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return data, "image/jpeg"
        scale *= 0.72
    raise RuntimeError(
        f"图片已尽力压缩仍超过 {max_bytes // 1024 // 1024}MB，请换更小的源文件：{path.name}"
    )


def _openai_user_content_parts(text: str, image_paths: list[Path]) -> str | list:
    """构造 OpenAI 兼容的 user content（纯文或 text+image_url 数组）。"""
    parts: list[dict] = []
    t = (text or "").strip()
    if t:
        parts.append({"type": "text", "text": t})
    for p in image_paths:
        try:
            if not p.is_file():
                continue
            raw, mime = _read_image_bytes_for_vision(p)
            b64 = base64.standard_b64encode(raw).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                }
            )
        except OSError as e:
            raise RuntimeError(f"无法读取图片 {p.name}：{e}") from e
    if not parts:
        raise ValueError("请输入文字或添加至少一张图片。")
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    if not t:
        parts.insert(0, {"type": "text", "text": "请描述或分析这些图片。"})
    return parts


def _parse_assistant_openai_content(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
        return "".join(chunks).strip()
    return (str(content) if content is not None else "").strip()


def _host_is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    h = hostname.strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


def _url_is_loopback(url: str) -> bool:
    try:
        pu = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return _host_is_loopback(pu.hostname)


def _urlopen_request(
    req: urllib.request.Request, *, timeout: int, context: ssl.SSLContext | None = None
):
    """对本机地址强制不走 HTTP(S)_PROXY，避免「浏览器 /health 正常、GUI POST 打到代理或其它服务」。"""
    full = req.full_url
    if _url_is_loopback(full):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    ctx = context if context is not None else ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _http_get_json(url: str, *, timeout: int = 4) -> dict | None:
    try:
        req = urllib.request.Request(url, method="GET")
        ctx = ssl.create_default_context()
        with _urlopen_request(req, timeout=timeout, context=ctx) as resp:
            out = json.loads(resp.read().decode("utf-8"))
            return out if isinstance(out, dict) else None
    except Exception:
        return None


def _normalize_openai_v1_base(base_url: str) -> str:
    """本机 OpenAI 兼容根 URL 须以 /v1 结尾；若只填到端口则自动补 /v1（避免 POST 到错误路径）。"""
    b = (base_url or "").strip().rstrip("/")
    if not b:
        return b
    low = b.lower()
    if ("127.0.0.1" in low or "localhost" in low) and not b.endswith("/v1"):
        b = b + "/v1"
    return b


_GEMMA_LOCAL_HEALTH_SERVICE = "serve_gemma4_4bit"


def _health_json_looks_like_serve_gemma(h: dict) -> bool:
    """serve_gemma4_4bit 的 /health 为 {\"status\":\"ok\",\"model\":str,...}；用于区分 Ollama 等无此形态的本地服务。"""
    if str(h.get("status", "")).lower() != "ok":
        return False
    m = h.get("model")
    return isinstance(m, str)


def _openapi_post_requires_query_request(spec: dict) -> bool:
    """非 OpenAI 兼容的 FastAPI 常见在 POST /v1/chat/completions 上声明 query.request，导致 422。"""
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return False
    for path_key in ("/v1/chat/completions", "/v1/chat/completions/"):
        node = paths.get(path_key)
        if not isinstance(node, dict):
            continue
        post = node.get("post")
        if not isinstance(post, dict):
            continue
        params = post.get("parameters")
        if not isinstance(params, list):
            continue
        for p in params:
            if isinstance(p, dict) and p.get("in") == "query" and str(p.get("name", "")) == "request":
                return True
    return False


def _assert_loopback_serve_gemma_preflight(
    chat_completions_url: str, *, client_model: str = ""
) -> None:
    """
    仅当本机 /health 呈 serve_gemma4_4bit 形态时做校验（不误伤 Ollama 等）。
    不绑定固定端口号：8090 常被其它软件占用，浏览器看到的 /health 可能与 POST 实际打到的程序不一致。
    """
    try:
        pu = urllib.parse.urlparse(chat_completions_url)
    except ValueError:
        return
    if not _host_is_loopback(pu.hostname):
        return
    if not (pu.path or "").rstrip("/").endswith("/v1/chat/completions"):
        return
    root = f"{pu.scheme}://{pu.netloc}"
    port_hint = str(pu.port) if pu.port else "(默认端口)"
    health_url = root + "/health"
    h = _http_get_json(health_url, timeout=3)
    if h is None or not _health_json_looks_like_serve_gemma(h):
        return
    if str(h.get("status", "")).lower() != "ok":
        raise RuntimeError(
            "本地 Gemma 自检：GET "
            + health_url
            + ' 的 JSON 中 status 须为 "ok"。\n摘要：'
            + json.dumps(h, ensure_ascii=False)[:800]
        )
    want = (client_model or "").strip()
    got = str(h.get("model", "")).strip()
    if h.get("service") == _GEMMA_LOCAL_HEALTH_SERVICE:
        pass
    elif want and got == want:
        pass
    else:
        raise RuntimeError(
            "本地 Gemma 自检：GET "
            + health_url
            + " 未带 "
            + f'"{_GEMMA_LOCAL_HEALTH_SERVICE}" 的 service 指纹（建议重启为新版 serve），'
            + "且 health.model 与「本地对话」中的模型 ID 不一致。\n"
            f'当前 health.model={got!r}，界面 model={want!r}。\n'
            "若 POST 仍出现 422（query、request），多半是端口 "
            + port_hint
            + " 上混入了其它 HTTP 服务；请 netstat 查 PID，"
            "或改用本项目默认端口 18090 启动 SERVE_GEMMA4_4BIT.bat。\n摘要："
            + json.dumps(h, ensure_ascii=False)[:800]
        )
    spec_url = root.rstrip("/") + "/openapi.json"
    spec = _http_get_json(spec_url, timeout=5)
    if isinstance(spec, dict) and _openapi_post_requires_query_request(spec):
        raise RuntimeError(
            "根据 "
            + spec_url
            + "：当前 "
            + root
            + " 上的 POST /v1/chat/completions 在 OpenAPI 中要求 URL 查询参数 request，"
            "不是本仓库 serve_gemma4_4bit（应使用 JSON body 的 messages）。\n"
            "因此会出现 HTTP 422（detail 含 query、request）。\n\n"
            "说明：浏览器里 /health 若仍像 Gemma，多半是同一端口上另有程序或代理把路径「嫁接」了；\n"
            "请 netstat -ano | findstr :"
            + port_hint
            + " 结束错误 PID，并只启动本项目的 SERVE_GEMMA4_4BIT.bat（新版默认监听 18090）。\n"
            "然后把「本地对话」URL 改成 http://127.0.0.1:18090/v1 再试。"
        )


def probe_local_openai_chat_health(
    base_url: str,
    model: str,
    api_key: str = "",
    *,
    timeout_sec: int = 25,
) -> tuple[bool, str]:
    """
    GUI 探活：向 /v1/chat/completions 发一条最小请求。
    不经过「本地对话」面板；OpenAI 兼容接口为无状态，服务端不会因此积累会话历史。
    返回 (是否成功, 简短说明)。
    """
    b = _normalize_openai_v1_base((base_url or "").strip())
    m = (model or "").strip()
    if not b or not m:
        return (
            False,
            "请到主导航「对话」页上方填写「服务 URL」（须含 /v1）与「模型 ID」",
        )
    url = b.rstrip("/") + "/chat/completions"
    try:
        _assert_loopback_serve_gemma_preflight(url, client_model=m)
    except Exception as e:
        line = str(e).strip().split("\n", 1)[0]
        return False, (line[:280] + ("…" if len(line) > 280 else "")) if line else "Gemma 预检失败"
    k = (api_key or "").strip()
    headers: dict[str, str] | None = None
    if k and k.upper() != "EMPTY":
        headers = {"Authorization": f"Bearer {k}"}
    payload = {
        "model": m,
        "messages": [
            {
                "role": "user",
                "content": (
                    "You are a health check endpoint. Reply with exactly the word PONG and nothing else."
                ),
            }
        ],
        "temperature": 0,
        "max_tokens": 16,
    }
    to = max(8, min(int(timeout_sec), 120))
    to = max(to, _openai_compatible_timeout_sec(b.rstrip("/")))
    try:
        out = _http_post_json(url, payload, headers, timeout=to)
        raw = out["choices"][0]["message"]["content"]
        text = _parse_assistant_openai_content(raw)
    except Exception as e:
        full = str(e).strip()
        line = full.split("\n", 1)[0]
        short = (line[:320] + ("…" if len(line) > 320 else "")) if line else "请求失败"
        low = full.lower()
        if "http 500" in low or "500 internal" in low:
            body_hint = ""
            for ln in full.split("\n"):
                s = ln.strip()
                if s and not s.upper().startswith("HTTP"):
                    body_hint = s[:140]
                    break
            short = (
                "HTTP 500：多为显存不足、模型未就绪或服务端内部错误，请查看 Gemma（或当前 serve）控制台"
                + (f" · {body_hint}" if body_hint else "")
            )
        return False, short[:400]
    if not text:
        return False, "模型返回空正文"
    return True, "接口可响应（单次探针，未写入本地对话）"


def local_openai_compatible_chat_round(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    prior: list[tuple[str, str, list[Path]]],
    user_text: str,
    user_images: list[Path],
    temperature: float = 0.0,
    top_p: float = 0.82,
    timeout_sec: int = 240,
    stream: bool = False,
    on_delta: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str]:
    """
    面向本地 / OpenAI 兼容服务的多轮对话；支持每轮用户侧附带多张图片（vision）。
    prior 每项为 (用户文本, 助手回复, 该轮用户消息中的图片路径列表)。
    stream=True 时使用 SSE；on_delta 在收到增量时调用（通常在后台线程，由调用方自行切回 UI 线程）。
    cancel_event：可选 threading.Event，置位后尽快结束请求并抛出 OpenAICompatibleRequestCancelled
    （用于 GUI「停止」；流式在两次 read 之间响应，非流式按块读取以便中止）。
    """
    b = _normalize_openai_v1_base(base_url)
    url = b.rstrip("/") + "/chat/completions"
    _assert_loopback_serve_gemma_preflight(url, client_model=model)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for ut, at, imgs in prior:
        try:
            ucontent = _openai_user_content_parts(ut, imgs)
        except ValueError:
            ucontent = (ut or "").strip() or "."
        messages.append({"role": "user", "content": ucontent})
        messages.append({"role": "assistant", "content": at})
    tail = _openai_user_content_parts(user_text, user_images)
    messages.append({"role": "user", "content": tail})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": 8192,
    }
    _k = (api_key or "").strip()
    headers: dict[str, str] | None = None
    if _k and _k.upper() != "EMPTY":
        headers = {"Authorization": f"Bearer {_k}"}
    to = max(timeout_sec, _openai_compatible_timeout_sec(b.rstrip("/")))
    if stream:
        text = _http_post_openai_chat_sse_deltas(
            url,
            payload,
            headers,
            timeout=to,
            on_delta=on_delta,
            cancel_event=cancel_event,
        )
        if not text:
            raise RuntimeError("OpenAI-compatible 流式返回空正文")
        return text, model
    out = _http_post_json(url, payload, headers, timeout=to, cancel_event=cancel_event)
    try:
        raw = out["choices"][0]["message"]["content"]
        text = _parse_assistant_openai_content(raw)
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"OpenAI-compatible 解析失败: {e!s}") from e
    if not text:
        raise RuntimeError("OpenAI-compatible 返回空正文")
    return text, model


def _anthropic_text_from_message_payload(out: dict) -> str:
    parts: list[str] = []
    for block in out.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _anthropic_generate(api_key: str, model: str, system: str, user: str) -> tuple[str, str]:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 8192,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        out = _http_post_json(url, payload, headers, timeout=120)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic HTTP {e.code}: {body[:1200]}") from e
    text = _anthropic_text_from_message_payload(out)
    if not text:
        raise RuntimeError("Anthropic 返回空正文")
    return text, model


def _anthropic_user_blocks(text: str, images: list[Path]) -> str | list:
    if not images:
        return text
    blocks: list[dict] = []
    t = (text or "").strip()
    if t:
        blocks.append({"type": "text", "text": t})
    for p in images:
        try:
            if not p.is_file():
                continue
            raw, mime = _read_image_bytes_for_vision(p)
            if not str(mime).startswith("image/"):
                mime = "image/jpeg"
            b64 = base64.standard_b64encode(raw).decode("ascii")
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64},
                }
            )
        except OSError:
            continue
        except RuntimeError:
            raise
    if not blocks:
        return (text or "").strip() or "."
    return blocks


def gemini_vision_caption_frame(
    api_key: str,
    model: str,
    image_path: Path,
    user_prompt: str,
    *,
    system: str = "你是视频画面助理。只依据图像作答，简短客观；不要编造屏幕外内容。",
) -> tuple[str, str]:
    """单帧画面说明（在线 Gemini）。返回 (正文, 模型标签)。"""
    if not (api_key or "").strip():
        raise RuntimeError("未设置 GEMINI_API_KEY")
    if not image_path.is_file():
        raise RuntimeError(f"图片不存在：{image_path}")
    from .frame_vision_gemma import FRAME_VISION_MAX_TOKENS

    contents = [{"role": "user", "parts": _gemini_inline_user_parts(user_prompt, [image_path])}]
    um = (model or "").strip()
    models_try = (
        (um,) + tuple(x for x in _gemini_default_models() if x != um) if um else _gemini_default_models()
    )
    last_err: str | None = None
    gen_cfg = {
        "temperature": 0.2,
        "maxOutputTokens": max(160, int(FRAME_VISION_MAX_TOKENS) + 64),
    }
    for api_ver in ("v1beta", "v1"):
        for mod in models_try:
            url = (
                f"https://generativelanguage.googleapis.com/{api_ver}/models/"
                f"{mod}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
            )
            if api_ver == "v1beta":
                payload: dict[str, Any] = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": contents,
                    "generationConfig": gen_cfg,
                }
            else:
                primed = [
                    {"role": "user", "parts": [{"text": "【系统设定】\n" + system}]},
                    {"role": "model", "parts": [{"text": "好的。"}]},
                ]
                payload = {
                    "contents": primed + contents,
                    "generationConfig": gen_cfg,
                }
            try:
                out = _http_post_json(url, payload, None, timeout=240)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = str(e)
                last_err = f"{api_ver}/{mod}: HTTP {e.code} {body[:800]}"
                continue
            except Exception as e:
                last_err = f"{api_ver}/{mod}: {e}"
                continue
            try:
                parts = out["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError, TypeError) as e:
                last_err = f"{api_ver}/{mod}: 解析响应失败 {e!s} raw={str(out)[:500]}"
                continue
            if text:
                return text, f"{mod} ({api_ver})"
            last_err = f"{api_ver}/{mod}: 空回复"
    raise RuntimeError("Gemini 视觉调用失败：" + (last_err or "unknown"))


def anthropic_vision_caption_frame(
    api_key: str,
    model: str,
    image_path: Path,
    user_prompt: str,
    *,
    system: str = "你是视频画面助理。只依据图像作答，简短客观；不要编造屏幕外内容。",
) -> tuple[str, str]:
    """单帧画面说明（在线 Claude）。返回 (正文, 模型名)。"""
    if not (api_key or "").strip():
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    if not image_path.is_file():
        raise RuntimeError(f"图片不存在：{image_path}")
    content = _anthropic_user_blocks(user_prompt, [image_path])
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": (model or "").strip(),
        "max_tokens": 512,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    try:
        out = _http_post_json(url, payload, headers, timeout=180)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic HTTP {e.code}: {body[:1200]}") from e
    text = _anthropic_text_from_message_payload(out)
    if not text:
        raise RuntimeError("Anthropic 返回空正文")
    return text, model


def _anthropic_multiturn(
    api_key: str,
    model: str,
    system: str,
    prior_turns: list[tuple[str, str, bool]],
    user_payload: str,
    *,
    attach_vision_current: bool,
    vision_paths: list[Path] | None = None,
) -> tuple[str, str]:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    imgs0 = list(vision_paths or [])
    messages: list[dict] = []
    for u, a, att in prior_turns:
        uimgs = imgs0 if att else []
        messages.append({"role": "user", "content": _anthropic_user_blocks(u, uimgs)})
        messages.append({"role": "assistant", "content": a})
    last_imgs = imgs0 if attach_vision_current else []
    messages.append({"role": "user", "content": _anthropic_user_blocks(user_payload, last_imgs)})
    payload = {
        "model": model,
        "max_tokens": 8192,
        "temperature": CHAT_TEMPERATURE,
        "system": system,
        "messages": messages,
    }
    to = (
        300
        if imgs0 and (attach_vision_current or any(t[2] for t in prior_turns))
        else 120
    )
    try:
        out = _http_post_json(url, payload, headers, timeout=to)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic HTTP {e.code}: {body[:1200]}") from e
    text = _anthropic_text_from_message_payload(out)
    if not text:
        raise RuntimeError("Anthropic 返回空正文")
    return text, model


def _chat_system_with_vision(base: str, vision_paths: list[Path]) -> str:
    if not vision_paths:
        return base
    return (
        base
        + "\n\n【多模态·硬性要求】本请求的用户消息在 API 中与文字一并包含多张 JPEG（视频代表帧，来自画面管线）。"
        "你必须根据这些图像中的像素内容回答颜色、物体、界面、字幕条等可见信息；"
        "禁止声称「无法访问图片」「仅有合并文稿/报告文本」「没有视频画面权限」——除非某一细节在图上确实看不清，再如实说明。\n"
        "图像与文字节选矛盾时，以图像为准并简短点明。"
    )


def _vision_user_preamble() -> str:
    return (
        "【给助手】本条 HTTP 请求里，本条用户消息除下列文字外还包含多张 JPEG 图像（视频截帧）。"
        "请直接依据图像回答（含颜色、形状、界面文字）；不要说你只能看合并文稿或看不到图。\n\n"
    )


def _build_chat_intro(merged_text: str, analysis_excerpt: str | None) -> str:
    body, was_trunc = _truncate(merged_text)
    note = f"（已截断至前 {MAX_MERGED_CHARS} 字）" if was_trunc else ""
    parts = [
        f"【合并文稿·供全程参考{note}】\n",
        "---\n",
        body,
        "\n---\n",
    ]
    if analysis_excerpt and analysis_excerpt.strip():
        ae = analysis_excerpt.strip()
        cap = 16_000
        if len(ae) > cap:
            ae = ae[:cap] + "\n…（报告节选已截断）\n"
        parts.extend(["\n【当前分析报告节选·仅供参考】\n", "---\n", ae, "\n---\n"])
    return "".join(parts)


def chat_followup(
    provider: Provider,
    merged_transcript: str,
    user_message: str,
    *,
    analysis_excerpt: str | None = None,
    prior_turns: Sequence[tuple[str, str] | tuple[str, str, bool]],
    attach_vision_current: bool = False,
    vision_frame_paths: Sequence[Path] | None = None,
) -> tuple[str, str, str]:
    """
    多轮对话的单次请求。prior_turns 每项为 (用户原文, 助手回复) 或 (用户原文, 助手回复, 该轮是否附带画面帧)。
    attach_vision_current：为 True 时在本轮用户消息上附带 vision_frame_paths（须模型支持视觉）。
    返回 (助手正文, 模型标签, 本轮实际提交给 API 的用户文本)。
    """
    q = user_message.strip()
    if not q:
        raise ValueError("请输入问题后再发送。")
    intro = _build_chat_intro(merged_transcript, analysis_excerpt)
    prior = normalize_chat_prior_turns(prior_turns)
    if not prior:
        user_payload = intro + "\n【用户问题】\n" + q
    else:
        user_payload = q

    vpaths = list(vision_frame_paths) if vision_frame_paths else []
    if attach_vision_current and vpaths:
        user_payload = _vision_user_preamble() + user_payload
    using_vision = bool(vpaths) and (attach_vision_current or any(t[2] for t in prior))
    sys_chat = (
        _chat_system_with_vision(SYSTEM_CHAT_ZH, vpaths) if using_vision else SYSTEM_CHAT_ZH
    )

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GEMINI_API_KEY")
        text, model = _gemini_multiturn(
            key,
            sys_chat,
            prior,
            user_payload,
            attach_vision_current=attach_vision_current,
            vision_paths=vpaths or None,
        )
        return text, f"Gemini / {model}", user_payload
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GROQ_API_KEY")
        text, model = _groq_multiturn(
            key,
            sys_chat,
            prior,
            user_payload,
            attach_vision_current=attach_vision_current,
            vision_paths=vpaths or None,
        )
        return text, f"Groq / {model}", user_payload
    if provider in ("openai", "local"):
        key, base, model = _local_openai_env_triple() if provider == "local" else (
            os.environ.get("OPENAI_API_KEY", "").strip(),
            (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip(),
            (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip(),
        )
        if provider == "openai":
            _require_real_openai_key_for_cloud()
        if provider == "local":
            if not local_openai_provider_ready():
                raise RuntimeError(
                    "local 提供商需要本机 OPENAI_BASE_URL（如 http://127.0.0.1:18090/v1）与 OPENAI_MODEL"
                )
        text, used = _openai_compatible_multiturn(
            key,
            base,
            model,
            sys_chat,
            prior,
            user_payload,
            attach_vision_current=attach_vision_current,
            vision_paths=vpaths or None,
        )
        tag = f"本地 OpenAI 兼容 / {used}" if provider == "local" else f"OpenAI / {used}"
        return text, tag, user_payload
    if provider == "xai":
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 XAI_API_KEY（xAI Grok）")
        base = "https://api.x.ai/v1"
        model = (os.environ.get("XAI_MODEL") or "grok-2-latest").strip()
        text, used = _openai_compatible_multiturn(
            key,
            base,
            model,
            sys_chat,
            prior,
            user_payload,
            attach_vision_current=attach_vision_current,
            vision_paths=vpaths or None,
        )
        return text, f"xAI Grok / {used}", user_payload
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 ANTHROPIC_API_KEY")
        model = (os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022").strip()
        text, used = _anthropic_multiturn(
            key,
            model,
            sys_chat,
            prior,
            user_payload,
            attach_vision_current=attach_vision_current,
            vision_paths=vpaths or None,
        )
        return text, f"Anthropic / {used}", user_payload
    raise RuntimeError(f"不支持的 provider：{provider!r}")


def build_llm_report(
    merged_text: str,
    input_path: Path,
    provider: Provider,
) -> str:
    """生成完整报告文本（含与本地规则风格接近的抬头）。"""
    body_trunc, was_trunc = _truncate(merged_text)
    user = _user_prompt(body_trunc, was_trunc)

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GEMINI_API_KEY")
        body, model = _gemini_generate(key, SYSTEM_ZH, user)
        tag = f"Gemini / {model}"
    elif provider == "groq":
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GROQ_API_KEY")
        body, model = _groq_chat(key, SYSTEM_ZH, user)
        tag = f"Groq / {model}"
    elif provider in ("openai", "local"):
        key, base, model = _local_openai_env_triple() if provider == "local" else (
            os.environ.get("OPENAI_API_KEY", "").strip(),
            (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip(),
            (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip(),
        )
        if provider == "openai":
            _require_real_openai_key_for_cloud()
        if provider == "local" and not local_openai_provider_ready():
            raise RuntimeError(
                "local 提供商需要本机 OPENAI_BASE_URL 与 OPENAI_MODEL（可与 GUI「③ VLM」同源）"
            )
        body, used = _openai_compatible_chat(key, base, model, SYSTEM_ZH, user)
        tag = f"本地 OpenAI 兼容 / {used}" if provider == "local" else f"OpenAI / {used}"
    elif provider == "xai":
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 XAI_API_KEY")
        base = "https://api.x.ai/v1"
        model = (os.environ.get("XAI_MODEL") or "grok-2-latest").strip()
        body, used = _openai_compatible_chat(key, base, model, SYSTEM_ZH, user)
        tag = f"xAI Grok / {used}"
    elif provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 ANTHROPIC_API_KEY")
        model = (os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022").strip()
        body, used = _anthropic_generate(key, model, SYSTEM_ZH, user)
        tag = f"Anthropic / {used}"
    else:
        raise RuntimeError(f"不支持的 provider：{provider!r}")

    from .analyze_transcript import _report_header  # noqa: PLC0415 — 运行时导入，避免循环依赖

    head = _report_header(merged_text, input_path)
    meta = [f"（由模型生成：{tag}）"]
    if was_trunc:
        meta.append("（文稿已截断，仅分析前段。）")
    meta.append("（API Key 与模型说明见 llm_analyze.py 与 GUI「API 与模型」页）")
    return "【视频内容总结】\n" + "\n".join(meta) + "\n" + head + body + "\n"


SYSTEM_DEEP_ZH = (
    "你是资深视频与叙事分析顾问。用户已有一份「基础总结」与完整「合并文稿」。"
    "请在**不重复基础总结字面堆砌**的前提下，做第二层解读：梳理结构、推断论证方式、受众与表达策略，并标明哪些是文稿直接支持、哪些是合理推断。\n"
    "硬性要求：信息不足就写「文本中未体现」；禁止编造合并文稿里没有的情节、数据或引用。"
    "全文简体中文、纯文本：禁止 Markdown（不要用 #、##、*、**、以半角减号开头的列表）；"
    "章节标题单独成行，用「一、」「二、」开头；分条用「（1）」「（2）」或「1.」「2.」；强调用「」。"
)


def _user_prompt_deep(
    merged: str,
    truncated: bool,
    basic_report: str,
) -> str:
    note = ""
    if truncated:
        note = f"\n（合并文稿已截断至前 {MAX_MERGED_CHARS} 字符。）\n"
    cap_basic = 24_000
    br = basic_report.strip()
    if len(br) > cap_basic:
        br = br[:cap_basic] + "\n…（基础总结已截断）\n"
    return (
        f"{note}"
        "下列【基础总结】由前一步模型或规则生成，可能有不完整之处，请与之对照但勿机械重复。\n\n"
        "--- 基础总结开始 ---\n"
        f"{br}\n"
        "--- 基础总结结束 ---\n\n"
        "下列为「合并文稿」（弹幕块 + 字幕/口播块）。\n\n"
        "--- 合并文稿开始 ---\n"
        f"{merged}\n"
        "--- 合并文稿结束 ---\n\n"
        "请输出「深度内容分析」一篇，必须包含以下四节（每节标题单独一行，空一行再写正文）：\n"
        "一、叙事与信息结构（时间线、段落功能、重点转移；依据文稿）\n"
        "\n"
        "二、观点、论据与潜在立场（哪些是明确陈述、哪些是暗示；勿臆造原话）\n"
        "\n"
        "三、表达风格与受众假设（用语、节奏、互动方式如弹幕呼应等）\n"
        "\n"
        "四、补充视角与审慎提醒（可与基础总结对照的差异、遗漏或需线下核实之点）\n"
    )


def build_llm_deep_report(
    merged_text: str,
    basic_report: str,
    input_path: Path,
    provider: Provider,
) -> str:
    """在基础报告之上生成第二篇深度分析（写入独立文件）。"""
    body_trunc, was_trunc = _truncate(merged_text)
    user = _user_prompt_deep(body_trunc, was_trunc, basic_report)

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GEMINI_API_KEY")
        body, model = _gemini_generate(key, SYSTEM_DEEP_ZH, user)
        tag = f"Gemini / {model}"
    elif provider == "groq":
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GROQ_API_KEY")
        body, model = _groq_chat(key, SYSTEM_DEEP_ZH, user)
        tag = f"Groq / {model}"
    elif provider in ("openai", "local"):
        key, base, model = _local_openai_env_triple() if provider == "local" else (
            os.environ.get("OPENAI_API_KEY", "").strip(),
            (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip(),
            (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip(),
        )
        if provider == "openai":
            _require_real_openai_key_for_cloud()
        if provider == "local" and not local_openai_provider_ready():
            raise RuntimeError(
                "local 提供商需要本机 OPENAI_BASE_URL 与 OPENAI_MODEL（可与 GUI「③ VLM」同源）"
            )
        body, used = _openai_compatible_chat(key, base, model, SYSTEM_DEEP_ZH, user)
        tag = f"本地 OpenAI 兼容 / {used}" if provider == "local" else f"OpenAI / {used}"
    elif provider == "xai":
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 XAI_API_KEY")
        base = "https://api.x.ai/v1"
        model = (os.environ.get("XAI_MODEL") or "grok-2-latest").strip()
        body, used = _openai_compatible_chat(key, base, model, SYSTEM_DEEP_ZH, user)
        tag = f"xAI Grok / {used}"
    elif provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 ANTHROPIC_API_KEY")
        model = (os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022").strip()
        body, used = _anthropic_generate(key, model, SYSTEM_DEEP_ZH, user)
        tag = f"Anthropic / {used}"
    else:
        raise RuntimeError(f"不支持的 provider：{provider!r}")

    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = f"（深度分析生成于 {ts}；依据 {input_path.name}；模型 {tag}）\n"
    if was_trunc:
        head += "（合并文稿已截断，深度分析可能不完整。）\n"
    return "【深度内容分析】\n" + head + "\n" + body.strip() + "\n"


SYSTEM_HIERARCHICAL_SEGMENT_ZH = (
    "你是专业中文视听内容助理。严格依据用户材料归纳；不得编造事实。"
    "用户消息末尾规定了固定的小节标题输出格式，请严格遵守，便于机器后续汇总。"
)


def hierarchical_multimodal_deep_report(
    merged_text: str,
    basic_report: str,
    timeline: list[dict[str, Any]],
    *,
    base_url: str,
    model: str,
    api_key: str = "",
    backend: str = "openai_compatible",
    segment_sec: float = 180.0,
    max_segments: int = 16,
    transcript_max_chars: int = 5200,
    ocr_max_lines: int = 14,
    ocr_max_chars: int = 2400,
    vision_max_lines: int = 10,
    segment_block_hard_cap: int = 11_000,
    input_path: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> str | None:
    """
    在画面管线产出 OCR + 稀疏画面描述后，按时间段分层调用大模型（OpenAI 兼容 / Gemini / Claude），
    再生成终稿「深度内容分析」，覆盖仅文本的深度稿。
    """
    from .video_context_builder import (
        build_final_deep_user_prompt,
        build_segment_context_block,
        build_single_shot_deep_user_prompt,
        build_time_segments,
        format_timestamp,
        infer_duration_sec,
        merge_segments_to_limit,
        parse_subtitle_lines_from_merged,
        prepare_segment_inputs,
        subs_text_in_range,
        timeline_in_range,
        truncate_subs_for_excerpt,
    )

    def _emit(msg: str) -> None:
        if log:
            log(msg)

    be = (backend or "openai_compatible").strip().lower()
    if be not in ("openai_compatible", "gemini", "anthropic"):
        be = "openai_compatible"
    m = (model or "").strip()
    k = (api_key or "").strip() or "EMPTY"
    b = _normalize_openai_v1_base((base_url or "").strip()) if be == "openai_compatible" else ""
    if be == "openai_compatible":
        if not b or not m:
            _emit("[分层深度] 未配置 base_url 或 model，跳过。\n")
            return None
    elif be == "gemini":
        if not m or not k:
            _emit("[分层深度] Gemini 未配置 model 或 api_key，跳过。\n")
            return None
    else:
        if not m or not k:
            _emit("[分层深度] Anthropic 未配置 model 或 api_key，跳过。\n")
            return None

    def _segment_llm(
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float | None = None,
    ) -> tuple[str, str]:
        if be == "openai_compatible":
            return _openai_compatible_chat(
                k, b, m, system, user, max_tokens=max_tokens, temperature=temperature
            )
        if be == "gemini":
            return _gemini_generate(k, system, user)
        return _anthropic_generate(k, m, system, user)

    subs = parse_subtitle_lines_from_merged(merged_text)
    dur = infer_duration_sec(subs, timeline)
    segs = build_time_segments(dur, max(60.0, float(segment_sec)))
    segs = merge_segments_to_limit(segs, max(1, int(max_segments)))

    def _header_note(tag: str, used: str) -> str:
        from datetime import datetime

        try:
            from zoneinfo import ZoneInfo

            ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ip = input_path.name if input_path else "transcript_merged.txt"
        return (
            f"（深度分析生成于 {ts}；依据 {ip}；{tag} {used}；"
            f"已融合字幕 + OCR + 稀疏画面描述，分段约 {int(segment_sec)}s，"
            f"分段调用上限 {int(max_segments)}）\n"
        )

    def _one_shot(t0: float, t1: float) -> tuple[str, str]:
        ocr_l, vlm_l = timeline_in_range(timeline, t0, t1)
        tr = subs_text_in_range(subs, t0, t1)
        tr, ocr_l, vlm_l = prepare_segment_inputs(
            tr,
            ocr_l,
            vlm_l,
            transcript_max_chars=transcript_max_chars,
            ocr_max_lines=ocr_max_lines,
            ocr_max_chars=ocr_max_chars,
            vision_max_lines=vision_max_lines,
        )
        ctx = build_segment_context_block(
            transcript_chunk=tr,
            ocr_lines=ocr_l,
            vision_lines=vlm_l,
            t0=t0,
            t1=t1,
            structured_segment=False,
        )
        excerpt = truncate_subs_for_excerpt(subs, 14_000)
        user = build_single_shot_deep_user_prompt(
            basic_report=basic_report,
            structured_context=ctx,
            merged_excerpt=excerpt,
        )
        body, used = _segment_llm(SYSTEM_DEEP_ZH, user, max_tokens=8192)
        return body, used

    if len(segs) <= 1:
        t0, t1 = segs[0]
        try:
            body, used = _one_shot(t0, t1)
        except Exception as e:
            _emit(f"[分层深度] 单次融合失败：{e}\n")
            return None
        head = _header_note("模型", used)
        _emit("[分层深度] 已用「单段融合」生成深度稿。\n")
        return "【深度内容分析】\n" + head + "\n" + body.strip() + "\n"

    summaries: list[str] = []
    for t0, t1 in segs:
        ocr_l, vlm_l = timeline_in_range(timeline, t0, t1)
        tr = subs_text_in_range(subs, t0, t1)
        if len(tr) < 12 and not ocr_l and not vlm_l:
            continue
        tr, ocr_l, vlm_l = prepare_segment_inputs(
            tr,
            ocr_l,
            vlm_l,
            transcript_max_chars=transcript_max_chars,
            ocr_max_lines=ocr_max_lines,
            ocr_max_chars=ocr_max_chars,
            vision_max_lines=vision_max_lines,
        )
        block = build_segment_context_block(
            transcript_chunk=tr,
            ocr_lines=ocr_l,
            vision_lines=vlm_l,
            t0=t0,
            t1=t1,
            structured_segment=True,
        )
        cap = int(segment_block_hard_cap)
        if len(block) > cap:
            block = block[:cap] + "\n…（本段上下文已超预算截断）\n"
        try:
            seg_body, _ = _segment_llm(
                SYSTEM_HIERARCHICAL_SEGMENT_ZH,
                block,
                max_tokens=768,
                temperature=0.2,
            )
        except Exception as e:
            _emit(f"[分层深度] 分段 {format_timestamp(t0)}–{format_timestamp(t1)} 失败：{e}\n")
            continue
        summaries.append(
            f"【{format_timestamp(t0)} – {format_timestamp(t1)}】\n{seg_body.strip()}"
        )

    if not summaries:
        t0, t1 = 0.0, dur
        ocr_l, vlm_l = timeline_in_range(timeline, t0, t1)
        tr = subs_text_in_range(subs, t0, t1)
        tr, ocr_l, vlm_l = prepare_segment_inputs(
            tr,
            ocr_l,
            vlm_l,
            transcript_max_chars=transcript_max_chars,
            ocr_max_lines=ocr_max_lines,
            ocr_max_chars=ocr_max_chars,
            vision_max_lines=vision_max_lines,
        )
        if len(tr) < 8 and not ocr_l and not vlm_l:
            _emit("[分层深度] 无可用字幕与画面条目，跳过。\n")
            return None
        try:
            body, used = _one_shot(t0, t1)
        except Exception as e:
            _emit(f"[分层深度] 回退单段失败：{e}\n")
            return None
        head = _header_note("模型", used)
        return "【深度内容分析】\n" + head + "\n" + body.strip() + "\n"

    excerpt = truncate_subs_for_excerpt(subs, 12_000)
    user = build_final_deep_user_prompt(
        basic_report=basic_report,
        segment_summaries=summaries,
        merged_excerpt=excerpt,
    )
    try:
        body, used = _segment_llm(SYSTEM_DEEP_ZH, user, max_tokens=8192)
    except Exception as e:
        _emit(f"[分层深度] 终稿汇总失败：{e}\n")
        return None
    head = _header_note("模型", used)
    _emit(f"[分层深度] 已完成 {len(summaries)} 段摘要 + 终稿。\n")
    return "【深度内容分析】\n" + head + "\n" + body.strip() + "\n"


def _openai_api_key_is_real(raw: str | None) -> bool:
    """占位符 EMPTY（本地对话兼容）不得算作已配置 Openai，否则 auto 会误走 OPENAI_BASE_URL（如 8090）。"""
    k = (raw or "").strip()
    return bool(k) and k.upper() != "EMPTY"


def _require_real_openai_key_for_cloud() -> None:
    raw = os.environ.get("OPENAI_API_KEY")
    if not (raw or "").strip():
        raise RuntimeError("未设置环境变量 OPENAI_API_KEY")
    if not _openai_api_key_is_real(raw):
        raise RuntimeError(
            "OPENAI_API_KEY 为占位 EMPTY，不能按 OpenAI 云路径调用。"
            "请在「API 与模型」填写真实 Key，或将「运行」页文稿改为「与 ③ VLM 同源」并启动本地兼容服务。"
        )


def provider_has_configured_key(p: Provider) -> bool:
    if p == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if p == "groq":
        return bool(os.environ.get("GROQ_API_KEY", "").strip())
    if p == "local":
        return local_openai_provider_ready()
    if p == "openai":
        return _openai_api_key_is_real(os.environ.get("OPENAI_API_KEY"))
    if p == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if p == "xai":
        return bool(os.environ.get("XAI_API_KEY", "").strip())
    return False


def resolve_provider(explicit: str) -> Provider | None:
    e = explicit.lower().strip()
    if e == "none":
        return None
    if e == "auto":
        for p in ("gemini", "openai", "groq", "anthropic", "xai"):
            if p == "openai":
                raw_b = (os.environ.get("OPENAI_BASE_URL") or "").strip()
                if raw_b and _url_is_loopback(_normalize_openai_v1_base(raw_b)):
                    # 环回地址应走 --llm-provider local / 与 VLM 同源；避免误把「本地对话」里的 8090 当 OpenAI 云
                    continue
            if provider_has_configured_key(p):  # type: ignore[arg-type]
                return p  # type: ignore[return-value]
        return None
    if e in ("gemini", "groq", "openai", "anthropic", "xai", "local"):
        return e if provider_has_configured_key(e) else None  # type: ignore[arg-type]
    return None
