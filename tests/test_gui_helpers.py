"""Unit tests for bilibili_vision.gui_helpers pure URL/source validators."""
from __future__ import annotations

from pathlib import Path

from bilibili_vision import gui_helpers
from bilibili_vision.gui_helpers import is_valid_task_source, looks_like_bilibili_url


def test_looks_like_bilibili_www() -> None:
    assert looks_like_bilibili_url("https://www.bilibili.com/video/BV1abcdefghij")


def test_looks_like_bilibili_b23() -> None:
    assert looks_like_bilibili_url("https://b23.tv/abcd")


def test_looks_like_bilibili_bv_no_host() -> None:
    # BV id alone is not enough; needs http/https scheme
    assert not looks_like_bilibili_url("BV1abcdefghij")


def test_looks_like_bilibili_bv_in_arbitrary_url() -> None:
    # BV id (BV1 + exactly 9 alnum) inside any http URL counts per current heuristic
    assert looks_like_bilibili_url("https://example.com/path/BV1abcdefghi")


def test_looks_like_bilibili_rejects_non_http() -> None:
    assert not looks_like_bilibili_url("ftp://bilibili.com/video/BV1abcdefghij")


def test_looks_like_bilibili_rejects_unrelated() -> None:
    assert not looks_like_bilibili_url("https://example.com/video")


def test_is_valid_task_source_url() -> None:
    assert is_valid_task_source("https://www.bilibili.com/video/BV1abcdefghij")


def test_is_valid_task_source_local_supported(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake")
    monkeypatch.setattr(gui_helpers, "is_supported_local_media", lambda p: True)
    assert is_valid_task_source(str(f))


def test_is_valid_task_source_local_unsupported(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(gui_helpers, "is_supported_local_media", lambda p: False)
    assert not is_valid_task_source(str(f))


def test_is_valid_task_source_missing_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gui_helpers, "is_supported_local_media", lambda p: True)
    assert not is_valid_task_source(str(tmp_path / "does-not-exist.mp4"))
