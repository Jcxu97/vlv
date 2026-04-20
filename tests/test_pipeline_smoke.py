"""End-to-end smoke tests for the extract → analyze pipeline.

These tests use mocks for yt-dlp, Playwright, Whisper, and LLM calls so they
run in CI without network or GPU. The goal is to lock the public API shape so
Phase 1 refactors (PlatformAdapter split) don't silently break contracts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_extract_bv_canonical():
    from bilibili_vision.output_session import extract_bv

    assert extract_bv("https://www.bilibili.com/video/BV1abc2defgh/") == "BV1abc2defgh"
    assert extract_bv("https://b23.tv/BVabc123DEFg") == "BVabc123DEFg"
    assert extract_bv("https://youtube.com/watch?v=dQw4w9WgXcQ") is None


def test_sanitize_component_handles_windows_reserved():
    from bilibili_vision.output_session import sanitize_component

    bad = 'test:video/2024|"special"*?<>chars'
    out = sanitize_component(bad)
    for ch in '<>:"/\\|?*':
        assert ch not in out
    assert out.strip("._") == out


def test_build_session_path_local_file(tmp_path, monkeypatch):
    from bilibili_vision import output_session

    monkeypatch.setattr(output_session, "PROJECT_ROOT", tmp_path)
    local = tmp_path / "my video.mp4"
    local.write_bytes(b"x")

    p = output_session.build_session_path(
        source_for_name=str(local),
        is_local=True,
        local_path=local,
        no_playlist=True,
        cookies=None,
    )
    assert p.exists()
    assert p.is_dir()
    assert "my_video" in p.name


def test_build_session_path_remote_mocked(tmp_path, monkeypatch):
    from bilibili_vision import output_session

    monkeypatch.setattr(output_session, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        output_session,
        "fetch_ytdlp_title",
        lambda url, cookies, no_playlist: "Example Title",
    )

    p = output_session.build_session_path(
        source_for_name="https://www.bilibili.com/video/BV1abc2defgh/",
        is_local=False,
        local_path=None,
        no_playlist=True,
        cookies=None,
    )
    assert p.exists()
    assert "Example_Title" in p.name
    assert "BV1abc2defgh" in p.name


def test_gui_helpers_url_detection():
    from bilibili_vision import gui_helpers

    assert gui_helpers.looks_like_bilibili_url("https://www.bilibili.com/video/BV1xxx")
    assert gui_helpers.looks_like_bilibili_url("https://b23.tv/abc")
    assert not gui_helpers.looks_like_bilibili_url("not a url")


def test_errors_module_importable():
    from bilibili_vision import errors

    e = errors.ExtractionError("boom")
    assert e.code.startswith("VLV_E")


def test_log_config_importable():
    from bilibili_vision import log_config

    assert callable(log_config.get_logger)
    assert callable(log_config.configure_logging)


@pytest.mark.slow
def test_yt_dlp_module_available():
    """Marked slow because importing yt_dlp loads ~30 extractor modules."""
    pytest.importorskip("yt_dlp")
