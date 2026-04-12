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

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

# 控制单次请求体大小，避免超出免费档常见限制
MAX_MERGED_CHARS = 100_000
# 仅用于底部多轮对话（非首屏分析报告）；略提高温度，回答更自然
CHAT_TEMPERATURE = 0.65


def _load_local_api_keys() -> None:
    """从同目录 local_api_keys.py 注入环境变量（仅当尚未设置时）。"""
    p = Path(__file__).resolve().parent / "local_api_keys.py"
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

Provider = Literal["gemini", "groq", "openai", "anthropic", "xai"]

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


def _http_post_json(url: str, payload: dict, headers: dict | None, timeout: int = 120) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    for api_ver in ("v1beta", "v1"):
        for model in models_try:
            url = (
                f"https://generativelanguage.googleapis.com/{api_ver}/models/"
                f"{model}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
            )
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": 0.35,
                    "maxOutputTokens": 8192,
                },
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


def _gemini_multiturn(
    api_key: str,
    system: str,
    prior_turns: list[tuple[str, str]],
    user_payload: str,
) -> tuple[str, str]:
    """prior_turns: (user_text, model_text) 按时间顺序；user_payload 为本轮用户输入。"""
    contents: list[dict] = []
    for u, a in prior_turns:
        contents.append({"role": "user", "parts": [{"text": u}]})
        contents.append({"role": "model", "parts": [{"text": a}]})
    contents.append({"role": "user", "parts": [{"text": user_payload}]})

    models_try = _gemini_default_models()
    last_err: str | None = None
    for api_ver in ("v1beta", "v1"):
        for model in models_try:
            url = (
                f"https://generativelanguage.googleapis.com/{api_ver}/models/"
                f"{model}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
            )
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {
                    "temperature": CHAT_TEMPERATURE,
                    "maxOutputTokens": 8192,
                },
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
    prior_turns: list[tuple[str, str]],
    user_payload: str,
) -> tuple[str, str]:
    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages: list[dict] = [{"role": "system", "content": system}]
    for u, a in prior_turns:
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": user_payload})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": CHAT_TEMPERATURE,
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


def _openai_compatible_chat(
    api_key: str, base_url: str, model: str, system: str, user: str
) -> tuple[str, str]:
    url = base_url.rstrip("/") + "/chat/completions"
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
    prior_turns: list[tuple[str, str]],
    user_payload: str,
) -> tuple[str, str]:
    url = base_url.rstrip("/") + "/chat/completions"
    messages: list[dict] = [{"role": "system", "content": system}]
    for u, a in prior_turns:
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": user_payload})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": CHAT_TEMPERATURE,
        "max_tokens": 8192,
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        out = _http_post_json(url, payload, headers, timeout=120)
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


def _anthropic_multiturn(
    api_key: str,
    model: str,
    system: str,
    prior_turns: list[tuple[str, str]],
    user_payload: str,
) -> tuple[str, str]:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    messages: list[dict] = []
    for u, a in prior_turns:
        messages.append({"role": "user", "content": u})
        messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": user_payload})
    payload = {
        "model": model,
        "max_tokens": 8192,
        "temperature": CHAT_TEMPERATURE,
        "system": system,
        "messages": messages,
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
    prior_turns: list[tuple[str, str]],
) -> tuple[str, str, str]:
    """
    多轮对话的单次请求。prior_turns 为已完成的 (发给 API 的用户原文, 助手回复) 列表。
    返回 (助手正文, 模型标签, 本轮实际提交给 API 的用户文本)。
    """
    q = user_message.strip()
    if not q:
        raise ValueError("请输入问题后再发送。")
    intro = _build_chat_intro(merged_transcript, analysis_excerpt)
    prior = list(prior_turns)
    if not prior:
        user_payload = intro + "\n【用户问题】\n" + q
    else:
        user_payload = q

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GEMINI_API_KEY")
        text, model = _gemini_multiturn(key, SYSTEM_CHAT_ZH, prior, user_payload)
        return text, f"Gemini / {model}", user_payload
    if provider == "groq":
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 GROQ_API_KEY")
        text, model = _groq_multiturn(key, SYSTEM_CHAT_ZH, prior, user_payload)
        return text, f"Groq / {model}", user_payload
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 OPENAI_API_KEY")
        base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
        model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
        text, used = _openai_compatible_multiturn(
            key, base, model, SYSTEM_CHAT_ZH, prior, user_payload
        )
        return text, f"OpenAI / {used}", user_payload
    if provider == "xai":
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 XAI_API_KEY（xAI Grok）")
        base = "https://api.x.ai/v1"
        model = (os.environ.get("XAI_MODEL") or "grok-2-latest").strip()
        text, used = _openai_compatible_multiturn(
            key, base, model, SYSTEM_CHAT_ZH, prior, user_payload
        )
        return text, f"xAI Grok / {used}", user_payload
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 ANTHROPIC_API_KEY")
        model = (os.environ.get("ANTHROPIC_MODEL") or "claude-3-5-haiku-20241022").strip()
        text, used = _anthropic_multiturn(
            key, model, SYSTEM_CHAT_ZH, prior, user_payload
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
    elif provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("未设置环境变量 OPENAI_API_KEY")
        base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").strip()
        model = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
        body, used = _openai_compatible_chat(key, base, model, SYSTEM_ZH, user)
        tag = f"OpenAI / {used}"
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

    from analyze_transcript import _report_header  # noqa: PLC0415 — 运行时导入，避免循环依赖

    head = _report_header(merged_text, input_path)
    meta = [f"（由在线大模型生成：{tag}）"]
    if was_trunc:
        meta.append("（文稿已截断，仅分析前段。）")
    meta.append("（API Key 与模型说明见 llm_analyze.py 与 GUI「API 与模型」页）")
    return "【视频内容总结】\n" + "\n".join(meta) + "\n" + head + body + "\n"


def provider_has_configured_key(p: Provider) -> bool:
    if p == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if p == "groq":
        return bool(os.environ.get("GROQ_API_KEY", "").strip())
    if p == "openai":
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())
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
            if provider_has_configured_key(p):  # type: ignore[arg-type]
                return p  # type: ignore[return-value]
        return None
    if e in ("gemini", "groq", "openai", "anthropic", "xai"):
        return e if provider_has_configured_key(e) else None  # type: ignore[arg-type]
    return None
