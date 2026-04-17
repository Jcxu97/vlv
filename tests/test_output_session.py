"""Unit tests for output_session pure helpers."""
from __future__ import annotations

from bilibili_vision.output_session import extract_bv, sanitize_component


def test_sanitize_component_control_chars() -> None:
    assert sanitize_component("ab\x00cd\x1fef") == "ab_cd_ef"


def test_sanitize_component_reserved_chars() -> None:
    assert sanitize_component('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"


def test_sanitize_component_whitespace_collapse() -> None:
    assert sanitize_component("hello world\tfoo") == "hello_world_foo"


def test_sanitize_component_empty_and_only_dots() -> None:
    assert sanitize_component("") == "untitled"
    assert sanitize_component("...") == "untitled"
    assert sanitize_component("___") == "untitled"


def test_sanitize_component_strips_leading_trailing_dot_underscore() -> None:
    assert sanitize_component("._name._") == "name"


def test_sanitize_component_truncates() -> None:
    long = "a" * 300
    assert len(sanitize_component(long, max_len=50)) == 50


def test_extract_bv_basic() -> None:
    assert extract_bv("https://www.bilibili.com/video/BV1Aa4y1E7aa") == "BV1Aa4y1E7aa"


def test_extract_bv_case_insensitive() -> None:
    got = extract_bv("bv1aa4y1e7aa")
    assert got is not None
    assert got.upper() == "BV1AA4Y1E7AA"


def test_extract_bv_missing() -> None:
    assert extract_bv("https://example.com/no-bv-here") is None


def test_extract_bv_short_id_fails() -> None:
    # BV requires 10 alnum chars after BV
    assert extract_bv("BV12345") is None
