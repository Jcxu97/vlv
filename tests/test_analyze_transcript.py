"""Unit tests for analyze_transcript pure helpers."""
from __future__ import annotations

from bilibili_vision.analyze_transcript import (
    parse_merged,
    split_three_choices_from_subs,
    top_danmu_themes,
)


def test_parse_merged_sub_and_danmu() -> None:
    text = (
        "===== 字幕 =====\n"
        "[00:00:01,000] 第一句\n"
        "[00:00:02,000] 第二句\n"
        "===== 弹幕 =====\n"
        "[00001.20s] 哈哈哈\n"
        "[00002.00s] 好看\n"
    )
    subs, danmu = parse_merged(text)
    assert subs == [("00:00:01,000", "第一句"), ("00:00:02,000", "第二句")]
    assert danmu == [("00001.20s", "哈哈哈"), ("00002.00s", "好看")]


def test_parse_merged_ignores_noise_lines() -> None:
    text = (
        "===== 字幕 =====\n"
        "random noise without bracket\n"
        "[00:00:01,000] ok\n"
    )
    subs, _ = parse_merged(text)
    assert subs == [("00:00:01,000", "ok")]


def test_parse_merged_empty_returns_empty() -> None:
    assert parse_merged("") == ([], [])


def test_parse_merged_english_danmaku_header_ignored() -> None:
    text = (
        "===== danmaku =====\n"
        "[00001.00s] content\n"
    )
    subs, danmu = parse_merged(text)
    assert subs == []
    assert danmu == []


def test_split_three_choices_fewer_than_two_hits_returns_empty() -> None:
    subs = [
        ("00:00:01,000", "选择一"),
        ("00:00:02,000", "故事内容"),
    ]
    assert split_three_choices_from_subs(subs) == {}


def test_split_three_choices_exact_label_split() -> None:
    subs = [
        ("00:00:01,000", "选择一"),
        ("00:00:02,000", "第一段正文"),
        ("00:00:03,000", "选择二："),
        ("00:00:04,000", "第二段正文"),
        ("00:00:05,000", "选择三"),
        ("00:00:06,000", "第三段正文"),
    ]
    out = split_three_choices_from_subs(subs)
    assert "选择一" in out and "选择二" in out and "选择三" in out
    assert "第一段正文" in out["选择一"]
    assert "第二段正文" in out["选择二"]
    assert "第三段正文" in out["选择三"]


def test_split_three_choices_ignores_substring_in_longer_line() -> None:
    # "选择一个团队" should NOT be treated as 选择一 header
    subs = [
        ("00:00:01,000", "选择一个团队加入"),
        ("00:00:02,000", "一些内容"),
    ]
    assert split_three_choices_from_subs(subs) == {}


def test_top_danmu_themes_empty_input() -> None:
    assert top_danmu_themes([]) == []


def test_top_danmu_themes_returns_only_chinese_ngrams() -> None:
    # Mix chinese and english; english grams must not surface
    lines = ["哈哈哈哈哈"] * 40 + ["okokokok"] * 40
    themes = top_danmu_themes(lines, top_n=5)
    for w, _ in themes:
        for ch in w:
            assert "\u4e00" <= ch <= "\u9fff"


def test_top_danmu_themes_top_n_limits() -> None:
    lines = ["好看好看好看好看"] * 30 + ["搞笑搞笑搞笑搞笑"] * 30 + ["震撼震撼震撼"] * 30
    themes = top_danmu_themes(lines, top_n=2)
    assert len(themes) <= 2


def test_top_danmu_themes_substring_dedup() -> None:
    # Dedup skips any later candidate whose text is a superset of an already-seen gram.
    # The kept 4-gram rotations of "太好看了" should be unique (no rotation contains another).
    lines = ["太好看了"] * 60
    themes = top_danmu_themes(lines, top_n=10)
    grams = [w for w, _ in themes]
    assert grams, "expected some themes from non-empty input"
    for a in grams:
        assert not any(a != b and a in b for b in grams)


def test_top_danmu_themes_min_freq_filters_low() -> None:
    # With 100 lines min_freq = max(3, 100//20) = 5; a token appearing 4 times should not surface
    lines = ["好看"] * 4 + ["普通的一行文字"] * 96
    themes = top_danmu_themes(lines, top_n=5)
    for w, c in themes:
        assert c >= 5
