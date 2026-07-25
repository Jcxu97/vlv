"""Compress a session's merged transcript into an LLM-ready digest.

`transcript_merged.txt` inlines every subtitle track yt-dlp returned (often five
machine-translated languages next to the Chinese one) plus every danmaku line.
For a 30-minute video that is ~9.5k lines, most of it redundant. This module
picks one narration track, folds it into timestamped paragraphs, and replaces the
danmaku wall with a frequency table.

Output: `transcript_digest.md` in the session directory, typically 3-5% the size
of the merged file while keeping the full narration content.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from .browser_bilibili import parse_srt_to_lines

# Track preference: local Whisper beats the platform's AI subtitles (better
# punctuation), and any Chinese track beats a machine-translated one. Matched as
# substrings against the filename, so ".zh" also covers ".zh-CN"/".zh-Hans".
_TRACK_RANK = ("_local_asr", ".ai-zh", ".zh", ".ai-en", ".en")


def _cue_count(path: Path) -> int:
    try:
        return len(parse_srt_to_lines(path.read_text(encoding="utf-8", errors="ignore")))
    except OSError:
        return 0

_DANMU_RE = re.compile(r'<d p="([^"]*)"[^>]*>(.*?)</d>', re.S)


def pick_narration_srt(session: Path) -> Path | None:
    """Highest-ranked track that actually has cues.

    Whisper writes a 0-cue SRT when it cannot detect speech (music videos, for
    instance), so ranking on filename alone would discard every usable subtitle.
    """
    usable = sorted(p for p in session.glob("*.srt") if _cue_count(p) > 0)
    if not usable:
        return None
    for token in _TRACK_RANK:
        for p in usable:
            if token in p.name:
                return p
    return usable[0]


def fold_segments(
    rows: list[tuple[str, str]], per_para: int = 12
) -> list[tuple[str, str]]:
    """Group consecutive cues into paragraphs stamped with the first cue's time."""
    out: list[tuple[str, str]] = []
    for i in range(0, len(rows), per_para):
        chunk = rows[i : i + per_para]
        stamp = chunk[0][0].split(" --> ")[0].split(",")[0][:8]
        out.append((stamp, "".join(text for _, text in chunk)))
    return out


def danmaku_table(session: Path, top: int = 30) -> tuple[int, list[tuple[str, int]]]:
    texts: list[str] = []
    for xml in session.glob("*.danmaku.xml"):
        raw = xml.read_text(encoding="utf-8", errors="ignore")
        texts += [t.strip() for _, t in _DANMU_RE.findall(raw) if t.strip()]
    counter = Counter(texts)
    return len(texts), counter.most_common(top)


def build_digest(session: Path, per_para: int = 12, top_danmu: int = 30) -> str:
    lines = [f"# {session.name}", ""]

    srt = pick_narration_srt(session)
    if srt is None:
        lines += ["(没有找到任何 .srt 字幕轨)", ""]
        paras: list[tuple[str, str]] = []
    else:
        rows = parse_srt_to_lines(srt.read_text(encoding="utf-8", errors="ignore"))
        paras = fold_segments(rows, per_para)
        duration = rows[-1][0].split(" --> ")[-1][:8] if rows else "?"
        lines += [
            f"- 口播轨：`{srt.name}`（{len(rows)} 段，时长 {duration}）",
            f"- 折叠为 {len(paras)} 段落（每 {per_para} 句合一）",
        ]

    total, top = danmaku_table(session, top_danmu)
    if total:
        lines.append(f"- 弹幕：{total} 条")
    lines += ["", "## 口播稿", ""]
    lines += [f"[{stamp}] {text}" for stamp, text in paras]

    if top:
        lines += ["", f"## 高频弹幕 top{len(top)}", ""]
        lines += [f"{count:4d}  {text}" for text, count in top]

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="把 session 目录压成 LLM 可直接读的 transcript_digest.md"
    )
    ap.add_argument("session_dir", type=Path, help="extract 产出的会话目录")
    ap.add_argument("-o", "--output", type=Path, default=None, help="输出路径")
    ap.add_argument("--per-para", type=int, default=12, help="每段合并多少句字幕")
    ap.add_argument("--top-danmu", type=int, default=30, help="保留多少条高频弹幕")
    args = ap.parse_args()

    session = args.session_dir
    if not session.is_dir():
        print(f"不是目录：{session}", file=sys.stderr)
        raise SystemExit(1)

    text = build_digest(session, args.per_para, args.top_danmu)
    out = args.output or session / "transcript_digest.md"
    out.write_text(text, encoding="utf-8")
    print(f"简报已写入：{out}（{len(text)} 字符）")


if __name__ == "__main__":
    main()
