"""Unit tests for the digest builder."""
from __future__ import annotations

from pathlib import Path

from bilibili_vision.digest import (
    build_digest,
    danmaku_table,
    fold_segments,
    pick_narration_srt,
)

SRT = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:03,000 --> 00:00:05,000
第二句
"""

DANMAKU = (
    '<?xml version="1.0"?><i>'
    '<d p="1.5,1,25,16777215,0,0,0,0">落子无悔</d>'
    '<d p="9.0,1,25,16777215,0,0,0,0">落子无悔</d>'
    '<d p="12.0,1,25,16777215,0,0,0,0">关灯吃面</d>'
    '<d p="20.0,1,25,16777215,0,0,0,0">   </d>'
    "</i>"
)


def test_pick_narration_prefers_local_asr(tmp_path: Path) -> None:
    (tmp_path / "x.ai-zh.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / "x.ai-en.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / "x_local_asr.srt").write_text(SRT, encoding="utf-8")
    assert pick_narration_srt(tmp_path).name == "x_local_asr.srt"


def test_pick_narration_falls_back_to_ai_zh(tmp_path: Path) -> None:
    (tmp_path / "x.ai-ja.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / "x.ai-zh.srt").write_text(SRT, encoding="utf-8")
    assert pick_narration_srt(tmp_path).name == "x.ai-zh.srt"


def test_pick_narration_none_when_empty(tmp_path: Path) -> None:
    assert pick_narration_srt(tmp_path) is None


def test_pick_narration_skips_zero_cue_asr(tmp_path: Path) -> None:
    """Whisper writes an empty SRT for music videos; fall through to real subs."""
    (tmp_path / "x_local_asr.srt").write_text("", encoding="utf-8")
    (tmp_path / "x.zh-CN.srt").write_text(SRT, encoding="utf-8")
    assert pick_narration_srt(tmp_path).name == "x.zh-CN.srt"


def test_pick_narration_none_when_all_tracks_empty(tmp_path: Path) -> None:
    (tmp_path / "x_local_asr.srt").write_text("", encoding="utf-8")
    (tmp_path / "y.ai-zh.srt").write_text("\n\n", encoding="utf-8")
    assert pick_narration_srt(tmp_path) is None


def test_fold_segments_groups_and_stamps() -> None:
    rows = [("00:00:01,000 --> 00:00:03,000", "a"), ("00:00:03,000 --> 00:00:05,000", "b")]
    folded = fold_segments(rows, per_para=2)
    assert folded == [("00:00:01", "ab")]


def test_fold_segments_handles_ragged_tail() -> None:
    rows = [(f"00:00:0{i},000 --> 00:00:0{i + 1},000", str(i)) for i in range(5)]
    folded = fold_segments(rows, per_para=2)
    assert len(folded) == 3
    assert folded[-1][1] == "4"


def test_danmaku_table_counts_and_drops_blank(tmp_path: Path) -> None:
    (tmp_path / "x.danmaku.xml").write_text(DANMAKU, encoding="utf-8")
    total, top = danmaku_table(tmp_path, top=10)
    assert total == 3  # whitespace-only entry dropped
    assert top[0] == ("落子无悔", 2)


def test_build_digest_end_to_end(tmp_path: Path) -> None:
    (tmp_path / "x_local_asr.srt").write_text(SRT, encoding="utf-8")
    (tmp_path / "x.danmaku.xml").write_text(DANMAKU, encoding="utf-8")
    text = build_digest(tmp_path, per_para=12, top_danmu=5)
    assert "## 口播稿" in text
    assert "第一句第二句" in text  # both cues folded into one paragraph
    assert "落子无悔" in text
    assert "弹幕：3 条" in text


def test_build_digest_without_subtitles(tmp_path: Path) -> None:
    (tmp_path / "x.danmaku.xml").write_text(DANMAKU, encoding="utf-8")
    text = build_digest(tmp_path)
    assert "没有找到任何 .srt 字幕轨" in text
    assert "落子无悔" in text


def test_build_digest_without_danmaku(tmp_path: Path) -> None:
    (tmp_path / "x_local_asr.srt").write_text(SRT, encoding="utf-8")
    text = build_digest(tmp_path)
    assert "第一句第二句" in text
    assert "高频弹幕" not in text
