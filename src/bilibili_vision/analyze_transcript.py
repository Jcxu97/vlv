"""
读取 out/transcript_merged.txt，生成结构化中文总结 out/video_analysis.txt。
优先使用「字幕」行作为视频内容；「弹幕」单独归纳观众讨论。

在线大模型（可选）：设置环境变量 GEMINI_API_KEY 或 GROQ_API_KEY 后，默认优先调用
Gemini 再尝试 Groq；失败或未配置时自动回退为本地规则摘要。详见 llm_analyze.py 顶部说明。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bilibili_vision.paths import PROJECT_ROOT

DEFAULT_MERGED = PROJECT_ROOT / "out" / "transcript_merged.txt"
DEFAULT_OUT = PROJECT_ROOT / "out" / "video_analysis.txt"
DEFAULT_DEEP_OUT = PROJECT_ROOT / "out" / "video_analysis_deep.txt"


def _active_out_base() -> Path:
    raw = os.environ.get("BILIBILI_VISION_OUT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return PROJECT_ROOT / "out"


def _maybe_repoint_defaults(args: argparse.Namespace) -> None:
    base = _active_out_base()
    try:
        if args.input.resolve() == DEFAULT_MERGED.resolve():
            args.input = base / "transcript_merged.txt"
    except OSError:
        pass
    try:
        if args.output.resolve() == DEFAULT_OUT.resolve():
            args.output = base / "video_analysis.txt"
    except OSError:
        pass
    try:
        if args.deep_output.resolve() == DEFAULT_DEEP_OUT.resolve():
            args.deep_output = base / "video_analysis_deep.txt"
    except OSError:
        pass

# 字幕多为 [00:00:00,040]；弹幕多为 [00002.56s]
LINE_SUB = re.compile(r"^\[([^\]]+)\]\s*(.+?)\s*$")
HEADER = re.compile(r"^===== (.+?) =====\s*$")


def _gui_progress(m: str, v: int = 0, t: str = "") -> None:
    """供 GUI 解析的一行进度（勿改前缀）；m: i=不确定进度 d=确定 h=隐藏。"""
    print(
        "__GUI_PROGRESS__ " + json.dumps({"m": m, "v": int(v), "t": t}, ensure_ascii=False),
        flush=True,
    )


def parse_merged(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """返回 (字幕行列表, 弹幕行列表)，元素为 (时间戳或空, 文本)。"""
    subs: list[tuple[str, str]] = []
    danmu: list[tuple[str, str]] = []
    mode: str | None = None
    for raw in text.splitlines():
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
        if not m or not mode:
            continue
        ts, body = m.group(1), m.group(2).strip()
        if mode == "sub":
            subs.append((ts, body))
        else:
            danmu.append((ts, body))
    return subs, danmu


def join_subtitles(subs: list[tuple[str, str]]) -> str:
    return "".join(t for _, t in subs)


def split_three_choices_from_subs(subs: list[tuple[str, str]]) -> dict[str, str]:
    """
    按字幕**整行**是否为「选择一/二/三」标题来分段。
    避免在拼接全文里用子串查找：会把「选择一个团队」误当成「选择一」开头，
    进而错误套用「三处庇护所」之类的固定结论文案。
    """
    labels = ["选择一", "选择二", "选择三"]
    hits: list[tuple[int, str]] = []
    for idx, (_, body) in enumerate(subs):
        b = body.strip()
        for lab in labels:
            if b == lab or b.startswith((lab + "：", lab + ":", lab + "、")):
                hits.append((idx, lab))
                break
    if len(hits) < 2:
        return {}
    parts: dict[str, str] = {}
    for j, (idx, lab) in enumerate(hits):
        end_idx = hits[j + 1][0] if j + 1 < len(hits) else len(subs)
        text = "".join(t for _, t in subs[idx:end_idx])
        parts[lab] = text.strip()
    return parts


def top_danmu_themes(lines: list[str], top_n: int = 12) -> list[tuple[str, int]]:
    """极轻量主题词统计（2～4 字片段 + 部分关键词）。"""
    blob = " ".join(lines)
    keywords = [
        "死亡岛",
        "生化",
        "酒店",
        "潮湿",
        "溶洞",
        "森林",
        "营地",
        "太阳能",
        "围墙",
        "尸潮",
        "往日不再",
        "行尸",
        "物资",
        "食物",
        "通风",
        "安全",
    ]
    ctr: Counter[str] = Counter()
    for kw in keywords:
        c = blob.count(kw)
        if c:
            ctr[kw] += c
    return ctr.most_common(top_n)


def _report_header(merged_text: str, input_path: Path) -> str:
    try:
        tz = ZoneInfo("Asia/Shanghai")
        ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n = len(merged_text)
    return (
        f"（自动生成于 {ts}；依据 {input_path.name}，约 {n} 字符）\n"
        "若内容与当前视频不符，请在 GUI 再点一次「开始提取与分析」或运行：python analyze_transcript.py\n\n"
    )


def build_report(
    subs: list[tuple[str, str]],
    danmu: list[tuple[str, str]],
    merged_text: str,
    input_path: Path,
) -> str:
    script_full = join_subtitles(subs)
    danmu_texts = [b for _, b in danmu]

    lines: list[str] = []
    lines.append("【视频内容总结】\n")
    lines.append(_report_header(merged_text, input_path))

    chunks: dict[str, str] = {}
    if script_full:
        lines.append("一、旁白/字幕概要（视频实际在讲什么）\n")
        chunks = split_three_choices_from_subs(subs)
        if chunks:
            lines.append(
                "检测到独立字幕行「选择一/二/三」标题：下列为各段旁白压缩摘录（完整见合并文稿）。\n"
            )
        else:
            lines.append(
                "以下为字幕内容的自动摘录（口播常为连续叙述，未出现独立「选择一」行标题时按前段截取）。\n"
            )
        order = ["选择一", "选择二", "选择三"]
        for lab in order:
            if lab not in chunks:
                continue
            body = chunks[lab]
            if len(body) > 420:
                body = body[:420] + "…"
            lines.append(f"\n{lab}\n{body}\n")

        if not chunks:
            short = script_full[:800] + ("…" if len(script_full) > 800 else "")
            lines.append(f"\n（摘录前段旁白）\n{short}\n")
    else:
        lines.append("一、旁白/字幕\n未在合并文件中发现字幕块；可能未登录或该 P 无 CC。以下为弹幕推断，仅供参考。\n")

    lines.append("\n二、观众弹幕在讨论什么\n")
    if danmu_texts:
        blob = "".join(danmu_texts)
        lines.append(
            "以下为弹幕文本的粗略关键词统计（不同视频差异会体现在频次与词项上；细节请查看合并文稿中的「弹幕」段）。\n"
        )
        if any(k in blob for k in ("死亡岛", "生化", "庇护", "丧尸", "末日", "酒店")):
            lines.append(
                "本稿弹幕中可见较多与丧尸/末日题材游戏或影视的联想与玩梗。\n"
            )
        themes = top_danmu_themes(danmu_texts)
        if themes:
            lines.append("\n关键词粗略频次（跨弹幕文本统计）：\n")
            for w, n in themes:
                lines.append(f"  - {w}：约 {n} 次\n")
    else:
        lines.append("本集弹幕很少或缺失。\n")

    lines.append("\n三、一句话结论（供快速决策）\n")
    shelter_keys = ("溶洞", "露营地", "度假酒店", "庇护所", "房车", "渔获")
    looks_like_shelter_compare = any(k in script_full for k in shelter_keys)
    if chunks and looks_like_shelter_compare:
        lines.append(
            "若旁白结构为「三处庇护所对比」：度假酒店偏物资与舒适但难守；露营地偏水与渔获与房车机动；"
            "溶洞偏水路屏障与纵深，但要重点评估潮湿、采光、存粮与通风。\n"
        )
    elif script_full:
        lines.append(
            "本条以口播/规则说明为主（如多阵营、生存设定等）；具体以第一节摘要与合并文稿为准，勿套用其他视频的固定模板。\n"
        )
    else:
        lines.append("稿件类型不固定，此处不强行给单一结论；请结合第一节摘要与合并文稿判断。\n")
    return "".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="从 transcript_merged.txt 生成 video_analysis.txt")
    ap.add_argument("-i", "--input", type=Path, default=DEFAULT_MERGED, help="合并文本路径")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT, help="分析输出路径")
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="不调在线大模型，仅使用本地规则",
    )
    ap.add_argument(
        "--llm-provider",
        choices=("auto", "none", "local", "gemini", "groq", "openai", "anthropic", "xai"),
        default="auto",
        help="auto：按 Gemini→… 使用首个已配置 Key；local：OPENAI_BASE_URL 指向本机 + OPENAI_MODEL（Gemma serve 等）；none 同 --no-llm",
    )
    ap.add_argument(
        "--deep",
        action="store_true",
        help="在首份总结之后，再调用大模型生成「深度内容分析」（写入 --deep-output）",
    )
    ap.add_argument(
        "--deep-output",
        type=Path,
        default=DEFAULT_DEEP_OUT,
        help="深度分析输出路径（默认 out/video_analysis_deep.txt）",
    )
    args = ap.parse_args()
    _maybe_repoint_defaults(args)

    try:
        _main_inner(args)
    finally:
        _gui_progress("h", 0, "")


def _main_inner(args: argparse.Namespace) -> None:
    if not args.input.is_file():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            f"未找到合并文件：{args.input}\n请先运行 extract_bilibili_text.py 或 bilibili_pipeline.py extract。\n",
            encoding="utf-8",
        )
        print(f"已写入提示：{args.output}", flush=True)
        return

    text = args.input.read_text(encoding="utf-8", errors="replace")
    subs, danmu = parse_merged(text)
    use_llm = not args.no_llm and args.llm_provider != "none"
    report: str | None = None

    if use_llm:
        from .llm_analyze import build_llm_report, resolve_provider

        prov = resolve_provider(args.llm_provider)
        if prov:
            try:
                _gui_progress("i", 0, f"大模型：基础总结（{prov}）…")
                report = build_llm_report(text, args.input.resolve(), prov)
                print(f"已使用大模型（{prov}）生成分析。", flush=True)
            except Exception as e:
                print(f"[警告] 大模型调用失败，已改用本地规则：{e}", flush=True)
        elif args.llm_provider == "local":
            print(
                "[警告] 已指定 --llm-provider local，但环境未就绪："
                "需 OPENAI_BASE_URL 指向本机（如 http://127.0.0.1:18090/v1）且设置 OPENAI_MODEL；已改用本地规则。",
                flush=True,
            )
        elif args.llm_provider in ("gemini", "groq", "openai", "anthropic", "xai"):
            print(
                f"[警告] 已指定 --llm-provider {args.llm_provider} 但未配置对应 API Key，改用本地规则。",
                flush=True,
            )
        elif args.llm_provider == "auto":
            from .llm_analyze import provider_has_configured_key

            if not any(
                provider_has_configured_key(p)  # type: ignore[arg-type]
                for p in ("gemini", "openai", "groq", "anthropic", "xai")
            ):
                print(
                    "提示：未配置任何在线模型 API Key，使用本地规则。"
                    " 在 GUI「API 与模型」或环境变量中配置后重跑即可（见 llm_analyze.py）。",
                    flush=True,
                )

    if report is None:
        report = build_report(subs, danmu, text, args.input.resolve())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"分析已写入：{args.output}", flush=True)

    if getattr(args, "deep", False) and use_llm:
        from .llm_analyze import build_llm_deep_report, resolve_provider

        deep_prov = resolve_provider(args.llm_provider)
        if deep_prov:
            try:
                _gui_progress("i", 0, f"大模型：深度分析（{deep_prov}）…")
                deep_text = build_llm_deep_report(text, report, args.input.resolve(), deep_prov)
                args.deep_output.parent.mkdir(parents=True, exist_ok=True)
                args.deep_output.write_text(deep_text, encoding="utf-8")
                print(f"深度分析已写入：{args.deep_output}", flush=True)
            except Exception as e:
                print(f"[警告] 深度分析生成失败：{e}", flush=True)
        else:
            if args.llm_provider == "local":
                print(
                    "[警告] 已指定 --deep 且 --llm-provider local，但本地 OpenAI 环境未就绪，已跳过深度分析。",
                    flush=True,
                )
            else:
                print(
                    "[警告] 已指定 --deep 但未配置可用的大模型 Key，已跳过深度分析。",
                    flush=True,
                )


if __name__ == "__main__":
    main()
