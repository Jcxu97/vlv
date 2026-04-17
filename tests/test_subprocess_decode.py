"""Regression tests for Bug #3: subprocess line decoding is tolerant of non-UTF-8 bytes."""
from __future__ import annotations

import re
from pathlib import Path

from bilibili_vision.gui_pipeline import _decode_subprocess_line


def test_decodes_utf8() -> None:
    assert _decode_subprocess_line("你好".encode("utf-8") + b"\n") == "你好\n"


def test_decodes_gbk_fallback() -> None:
    # "你好" in GBK is not valid UTF-8; helper must not crash.
    raw = "你好".encode("gbk") + b"\n"
    out = _decode_subprocess_line(raw)
    assert isinstance(out, str) and out.endswith("\n")


def test_decodes_random_bytes_without_crash() -> None:
    # Arbitrary bytes that are valid in neither UTF-8 nor GBK must still decode to str.
    out = _decode_subprocess_line(b"\xff\xfe\x80mixed\n")
    assert isinstance(out, str)


def test_empty_bytes() -> None:
    assert _decode_subprocess_line(b"") == ""


_BAD_DECODE = re.compile(r"\.decode\s*\(\s*['\"]utf-8['\"]\s*\)")


def test_no_naked_utf8_decode_in_gui_inference() -> None:
    # Guard against reintroducing `.decode("utf-8")` without errors= in modules that
    # read subprocess byte streams. The helper `_decode_subprocess_line` itself lives
    # in gui_pipeline.py and its use of .decode("utf-8") is by design (wrapped in try).
    src = Path(__file__).resolve().parent.parent / "src" / "bilibili_vision" / "gui_inference.py"
    assert not _BAD_DECODE.search(src.read_text(encoding="utf-8"))
