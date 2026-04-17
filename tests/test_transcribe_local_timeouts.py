"""Regression tests for Bug #2: yt-dlp / ffmpeg subprocess timeouts."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bilibili_vision import transcribe_local


def test_download_audio_passes_timeout(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(transcribe_local.subprocess, "run", fake_run)
    monkeypatch.setattr(transcribe_local, "resolve_ffmpeg_bin_dir", lambda _loc: str(tmp_path))
    monkeypatch.setattr(transcribe_local, "_prepend_path", lambda _p: None)

    transcribe_local.download_audio(
        "https://www.bilibili.com/video/BV1abcdefghi",
        tmp_path,
        no_playlist=True,
        cookies=None,
    )
    assert captured["kwargs"].get("timeout", 0) > 0


def test_download_audio_raises_on_timeout(monkeypatch, tmp_path: Path) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(transcribe_local.subprocess, "run", fake_run)
    monkeypatch.setattr(transcribe_local, "resolve_ffmpeg_bin_dir", lambda _loc: str(tmp_path))
    monkeypatch.setattr(transcribe_local, "_prepend_path", lambda _p: None)

    url = "https://www.bilibili.com/video/BV1abcdefghi"
    with pytest.raises(RuntimeError) as excinfo:
        transcribe_local.download_audio(url, tmp_path, no_playlist=True, cookies=None)
    assert url in str(excinfo.value)


def test_extract_audio_from_video_passes_timeout(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(transcribe_local.subprocess, "run", fake_run)
    monkeypatch.setattr(transcribe_local, "_ffmpeg_executable", lambda _d: "ffmpeg")

    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"fake")
    transcribe_local.extract_audio_from_video(vid, tmp_path, str(tmp_path))
    assert captured["kwargs"].get("timeout", 0) > 0


def test_extract_audio_from_video_raises_on_timeout(monkeypatch, tmp_path: Path) -> None:
    def fake_run(cmd, **kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(transcribe_local.subprocess, "run", fake_run)
    monkeypatch.setattr(transcribe_local, "_ffmpeg_executable", lambda _d: "ffmpeg")

    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"fake")
    with pytest.raises(RuntimeError) as excinfo:
        transcribe_local.extract_audio_from_video(vid, tmp_path, str(tmp_path))
    assert "clip.mp4" in str(excinfo.value)
