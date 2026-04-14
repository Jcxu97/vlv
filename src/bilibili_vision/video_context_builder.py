"""
将字幕时间轴 + 画面管线 timeline（OCR / Gemma 一句描述）拼成结构化文本，供分层深度总结使用。
"""
from __future__ import annotations

import re
from typing import Any

HEADER = re.compile(r"^===== (.+?) =====\s*$")
LINE_SUB = re.compile(r"^\[([^\]]+)\]\s*(.+?)\s*$")


def ts_to_seconds(raw: str) -> float | None:
    """解析合并稿中的时间戳为秒。支持 [00:01:02,500] 与 [12.34s] 等。"""
    s = (raw or "").strip()
    if not s:
        return None
    if s.endswith("s"):
        try:
            return float(s[:-1].strip())
        except ValueError:
            return None
    s2 = s.replace(",", ".")
    parts = s2.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = parts
            return int(h) * 3600 + int(m) * 60 + float(sec)
        if len(parts) == 2:
            m, sec = parts
            return int(m) * 60 + float(sec)
        return float(s2)
    except ValueError:
        return None


def format_timestamp(sec: float) -> str:
    sec = max(0.0, float(sec))
    h, r = divmod(int(round(sec)), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_subtitle_lines_from_merged(merged_text: str) -> list[tuple[float, str]]:
    """只收集「字幕」块下的行，返回 (秒, 正文)。"""
    out: list[tuple[float, str]] = []
    mode: str | None = None
    for raw in merged_text.splitlines():
        line = raw.strip()
        hm = HEADER.match(line)
        if hm:
            title = hm.group(1)
            if "字幕" in title and "danmaku" not in title.lower():
                mode = "sub"
            elif "弹幕" in title:
                mode = "danmu"
            else:
                mode = None
            continue
        m = LINE_SUB.match(line)
        if not m or mode != "sub":
            continue
        ts_raw, body = m.group(1), m.group(2).strip()
        t = ts_to_seconds(ts_raw)
        if t is None:
            continue
        if body:
            out.append((t, body))
    out.sort(key=lambda x: x[0])
    return out


def timeline_in_range(
    timeline: list[dict[str, Any]], t0: float, t1: float
) -> tuple[list[str], list[str]]:
    """返回 (ocr 行, vlm 行)，仅含落在 [t0, t1] 的帧（含端点）。"""
    ocr_lines: list[str] = []
    vlm_lines: list[str] = []
    for ev in timeline:
        try:
            t = float(ev.get("t", -1))
        except (TypeError, ValueError):
            continue
        if t < t0 or t > t1:
            continue
        if ev.get("ocr"):
            ocr_lines.append(str(ev["ocr"]).strip())
        if ev.get("vlm"):
            vlm_lines.append(str(ev["vlm"]).strip())
    return ocr_lines, vlm_lines


def subs_text_in_range(subs: list[tuple[float, str]], t0: float, t1: float) -> str:
    parts = [b for tt, b in subs if t0 <= tt <= t1 and b.strip()]
    return "\n".join(parts).strip()


def format_ocr_timeline_line(t_sec: float, text: str, *, max_chars: int = 320) -> str:
    """单帧 OCR 单行结构化（避免整段糊在一起）。"""
    stamp = format_timestamp(t_sec)
    t = (text or "").strip().replace("\n", " ")
    if len(t) > max_chars:
        t = t[: max_chars - 1] + "…"
    return f"{stamp} - 画面文字：{t}"


def format_vlm_timeline_line(t_sec: float, sentence: str) -> str:
    stamp = format_timestamp(t_sec)
    s = (sentence or "").strip().replace("\n", " ")
    return f"{stamp} - 画面描述：{s}"


def build_time_segments(
    duration_sec: float, segment_sec: float
) -> list[tuple[float, float]]:
    """半开区间 [t0, t1] 闭合为包含端点；这里用闭区间 [t0, t1] 与字幕时间对齐。"""
    dur = max(1.0, float(duration_sec))
    step = max(60.0, float(segment_sec))
    segs: list[tuple[float, float]] = []
    t = 0.0
    while t < dur + 0.01:
        t1 = min(t + step, dur)
        segs.append((t, t1))
        t = t1
        if t >= dur:
            break
    return segs


def infer_duration_sec(
    subs: list[tuple[float, str]], timeline: list[dict[str, Any]]
) -> float:
    mx = 0.0
    for tt, _ in subs:
        mx = max(mx, float(tt))
    for ev in timeline:
        try:
            mx = max(mx, float(ev.get("t", 0)))
        except (TypeError, ValueError):
            pass
    return max(mx, 1.0)


def build_segment_context_block(
    *,
    transcript_chunk: str,
    ocr_lines: list[str],
    vision_lines: list[str],
    t0: float,
    t1: float,
    structured_segment: bool = False,
) -> str:
    ocr_block = "\n".join(ocr_lines) if ocr_lines else "（本段无 OCR 命中）"
    vis_block = "\n".join(vision_lines) if vision_lines else "（本段无画面描述）"
    tr = (transcript_chunk or "").strip() or "（本段无字幕文本）"
    head = (
        f"【时间段】{format_timestamp(t0)} – {format_timestamp(t1)}\n\n"
        f"【视频字幕】\n{tr}\n\n"
        f"【画面文字（OCR）】\n{ocr_block}\n\n"
        f"【关键画面描述】\n{vis_block}\n\n"
    )
    if structured_segment:
        return head + SEGMENT_STRUCTURED_TAIL_ZH.strip()
    return (
        head
        + "【任务】请用简体中文简要归纳本段：核心信息、关键术语或步骤（不超过 800 字）；"
        "勿编造合并稿中不存在的事实；无信息则写「本段信息不足」。"
    )


def build_final_deep_user_prompt(
    *,
    basic_report: str,
    segment_summaries: list[str],
    merged_excerpt: str,
) -> str:
    br = (basic_report or "").strip()
    if len(br) > 24_000:
        br = br[:24_000] + "\n…（基础总结已截断）\n"
    joined = "\n\n".join(segment_summaries)
    if len(joined) > 22_000:
        joined = joined[:22_000] + "\n…（分段摘要已截断）\n"
    ex = (merged_excerpt or "").strip()
    if len(ex) > 12_000:
        ex = ex[:12_000] + "\n…（合并文稿摘录已截断）\n"
    return (
        "下列【基础总结】由前一步生成，请对照但勿机械重复。\n\n"
        "--- 基础总结开始 ---\n"
        f"{br}\n"
        "--- 基础总结结束 ---\n\n"
        "下列为各时间段「分段摘要」（已融合字幕与画面侧文本）。\n\n"
        "--- 分段摘要开始 ---\n"
        f"{joined}\n"
        "--- 分段摘要结束 ---\n\n"
        "下列为合并文稿摘录（字幕为主），供核对细节。\n\n"
        "--- 合并文稿摘录开始 ---\n"
        f"{ex}\n"
        "--- 合并文稿摘录结束 ---\n\n"
        "请输出「深度内容分析」一篇，必须包含以下四节（每节标题单独一行，空一行再写正文）：\n"
        "一、叙事与信息结构（时间线、段落功能、重点转移；可结合画面文字与描述）\n"
        "\n"
        "二、观点、论据与潜在立场（勿臆造原话）\n"
        "\n"
        "三、表达风格与受众假设\n"
        "\n"
        "四、补充视角与审慎提醒（与基础总结对照的差异或需核实之点）\n"
        + COVERAGE_TAIL_FINAL_ZH.strip()
    )


def build_single_shot_deep_user_prompt(
    *,
    basic_report: str,
    structured_context: str,
    merged_excerpt: str,
) -> str:
    br = (basic_report or "").strip()
    if len(br) > 24_000:
        br = br[:24_000] + "\n…（基础总结已截断）\n"
    ctx = (structured_context or "").strip()
    if len(ctx) > 20_000:
        ctx = ctx[:20_000] + "\n…（结构化上下文已截断）\n"
    ex = (merged_excerpt or "").strip()
    if len(ex) > 14_000:
        ex = ex[:14_000] + "\n…（合并文稿摘录已截断）\n"
    return (
        "--- 基础总结开始 ---\n"
        f"{br}\n"
        "--- 基础总结结束 ---\n\n"
        "--- 结构化多模态上下文（字幕 + OCR + 画面一句描述）开始 ---\n"
        f"{ctx}\n"
        "--- 结构化多模态上下文结束 ---\n\n"
        "--- 合并文稿摘录开始 ---\n"
        f"{ex}\n"
        "--- 合并文稿摘录结束 ---\n\n"
        "请输出「深度内容分析」一篇，必须包含以下四节（每节标题单独一行，空一行再写正文）：\n"
        "一、叙事与信息结构（时间线、段落功能、重点转移；可结合画面文字与描述）\n"
        "\n"
        "二、观点、论据与潜在立场（勿臆造原话）\n"
        "\n"
        "三、表达风格与受众假设\n"
        "\n"
        "四、补充视角与审慎提醒（与基础总结对照的差异或需核实之点）\n"
        + COVERAGE_TAIL_FINAL_ZH.strip()
    )


def _ocr_line_payload(line: str) -> str:
    if "画面文字：" in line:
        return line.split("画面文字：", 1)[-1].strip()
    return (line or "").strip()


def compress_ocr_lines(
    lines: list[str],
    *,
    max_lines: int = 14,
    max_total_chars: int = 2600,
    min_payload_len: int = 3,
) -> list[str]:
    """去重 + 限量 + 总字符上限，降低 OCR 噪声进上下文。"""
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        payload = _ocr_line_payload(ln)
        if len(payload) < min_payload_len:
            continue
        key = re.sub(r"\s+", "", payload.casefold())[:96]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(ln.strip())
        if len(out) >= max_lines:
            break
    while out:
        total = sum(len(x) for x in out)
        if total <= max_total_chars:
            break
        out.pop()
    return out


def compress_transcript_chunk(text: str, *, max_chars: int = 5500) -> str:
    """单段字幕预算，优先在换行处截断。"""
    t = (text or "").strip()
    if len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    last_nl = cut.rfind("\n")
    if last_nl > int(max_chars * 0.55):
        return cut[:last_nl].rstrip() + "\n…（本段字幕截断）"
    return cut.rstrip() + "…（本段字幕截断）"


def compress_vision_lines(lines: list[str], *, max_lines: int = 10) -> list[str]:
    return [x.strip() for x in lines if x.strip()][:max_lines]


def prepare_segment_inputs(
    transcript_chunk: str,
    ocr_lines: list[str],
    vision_lines: list[str],
    *,
    transcript_max_chars: int = 5200,
    ocr_max_lines: int = 14,
    ocr_max_chars: int = 2400,
    vision_max_lines: int = 10,
) -> tuple[str, list[str], list[str]]:
    tr = compress_transcript_chunk(transcript_chunk, max_chars=transcript_max_chars)
    ocr = compress_ocr_lines(
        ocr_lines, max_lines=ocr_max_lines, max_total_chars=ocr_max_chars
    )
    vis = compress_vision_lines(vision_lines, max_lines=vision_max_lines)
    return tr, ocr, vis


def merge_segments_to_limit(
    segs: list[tuple[float, float]], max_segments: int
) -> list[tuple[float, float]]:
    """长视频合并相邻时间段，控制 LLM 分段调用次数。"""
    if max_segments <= 0 or len(segs) <= max_segments:
        return segs
    n = len(segs)
    bs = max(1, (n + max_segments - 1) // max_segments)
    out: list[tuple[float, float]] = []
    i = 0
    while i < n:
        j = min(n, i + bs)
        block = segs[i:j]
        out.append((block[0][0], block[-1][1]))
        i = j
    return out[:max_segments]


SEGMENT_STRUCTURED_TAIL_ZH = (
    "\n【输出格式】请严格使用下列小节标题（不要 JSON），每节内简练中文：\n"
    "主题：（一行）\n"
    "要点：（1）…（2）… 至多 5 条，每条不超过 55 字\n"
    "实体与术语：（逗号分隔；无则写 无）\n"
    "动作或步骤：（无则写 无；有则编号 1. 2. …）\n"
    "演示或代码：（写 有 或 无；若有则依据材料写一句）\n"
    "若本段材料不足，在「主题」后注明「本段信息不足」并其余小节写 无。"
)


COVERAGE_TAIL_FINAL_ZH = (
    "\n【覆盖要求】请在四节正文中尽量体现（无则明确写「未涉及」）："
    "讲了什么主题；是否出现演示或代码；是否有清晰步骤；是否有明确结论或主张。\n"
)


def truncate_subs_for_excerpt(subs: list[tuple[float, str]], max_chars: int) -> str:
    lines: list[str] = []
    n = 0
    for tt, b in subs:
        line = f"[{format_timestamp(tt)}] {b}"
        if n + len(line) > max_chars:
            break
        lines.append(line)
        n += len(line) + 1
    return "\n".join(lines)
