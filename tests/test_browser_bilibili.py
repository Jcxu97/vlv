"""Unit tests for browser_bilibili pure helpers."""
from __future__ import annotations

import json
from pathlib import Path

from bilibili_vision.browser_bilibili import parse_srt_to_lines, storage_state_to_netscape


def test_parse_srt_basic() -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,500\nHello world\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond line\n"
    rows = parse_srt_to_lines(srt)
    assert rows == [
        ("00:00:01,000", "Hello world"),
        ("00:00:03,000", "Second line"),
    ]


def test_parse_srt_strips_html_tags() -> None:
    srt = "1\n00:00:01,000 --> 00:00:02,000\n<font color=red>Red</font> text\n"
    rows = parse_srt_to_lines(srt)
    assert rows == [("00:00:01,000", "Red text")]


def test_parse_srt_skips_empty_cue() -> None:
    # Cue body is whitespace-only → stripped to "" → row dropped.
    srt = "1\n00:00:01,000 --> 00:00:02,000\n   \n"
    assert parse_srt_to_lines(srt) == []


def test_parse_srt_multiline_cue() -> None:
    srt = "1\n00:00:01,000 --> 00:00:05,000\nLine one\nLine two\n"
    rows = parse_srt_to_lines(srt)
    assert rows == [("00:00:01,000", "Line one\nLine two")]


def test_parse_srt_empty_returns_empty() -> None:
    assert parse_srt_to_lines("") == []


def test_storage_state_filters_to_bilibili(tmp_path: Path) -> None:
    state = {
        "cookies": [
            {
                "name": "SESSDATA",
                "value": "abc",
                "domain": ".bilibili.com",
                "path": "/",
                "secure": True,
                "expires": 9999999999.0,
            },
            {
                "name": "unrelated",
                "value": "xyz",
                "domain": "example.com",
                "path": "/",
            },
        ]
    }
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state), encoding="utf-8")
    out = tmp_path / "cookies.txt"
    n = storage_state_to_netscape(sp, out)
    assert n == 1
    text = out.read_text(encoding="utf-8")
    assert "SESSDATA" in text
    assert "unrelated" not in text
    assert ".bilibili.com" in text


def test_storage_state_expires_missing_becomes_zero(tmp_path: Path) -> None:
    state = {
        "cookies": [
            {"name": "n1", "value": "v1", "domain": "bilibili.com", "path": "/"},
        ]
    }
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state), encoding="utf-8")
    out = tmp_path / "cookies.txt"
    n = storage_state_to_netscape(sp, out)
    assert n == 1
    assert "\t0\tn1\tv1" in out.read_text(encoding="utf-8")


def test_storage_state_skips_missing_name(tmp_path: Path) -> None:
    state = {
        "cookies": [
            {"value": "v", "domain": ".bilibili.com", "path": "/"},
            {"name": "good", "value": "v", "domain": "bilibili.com", "path": "/"},
        ]
    }
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state), encoding="utf-8")
    out = tmp_path / "cookies.txt"
    assert storage_state_to_netscape(sp, out) == 1


def test_storage_state_domain_specified_field(tmp_path: Path) -> None:
    state = {
        "cookies": [
            {"name": "a", "value": "v", "domain": ".bilibili.com", "path": "/"},
            {"name": "b", "value": "v", "domain": "bilibili.com", "path": "/"},
        ]
    }
    sp = tmp_path / "state.json"
    sp.write_text(json.dumps(state), encoding="utf-8")
    out = tmp_path / "cookies.txt"
    storage_state_to_netscape(sp, out)
    content = out.read_text(encoding="utf-8")
    # Dotted domain → TRUE; non-dotted → FALSE
    assert ".bilibili.com\tTRUE\t" in content
    assert "\nbilibili.com\tFALSE\t" in content
