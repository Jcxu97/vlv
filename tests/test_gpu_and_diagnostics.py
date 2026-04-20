"""Tests for gpu_watchdog preflight + diagnostics zip export."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest


def test_probe_gpu_never_raises():
    from bilibili_vision.gpu_watchdog import probe_gpu

    s = probe_gpu()
    # Whether we have CUDA or not, probe_gpu must not raise.
    assert isinstance(s.available, bool)


def test_assert_gpu_headroom_raises_without_cuda(monkeypatch):
    from bilibili_vision import gpu_watchdog
    from bilibili_vision.errors import GPUError

    monkeypatch.setattr(gpu_watchdog, "probe_gpu", lambda: gpu_watchdog.GPUStatus(available=False))
    with pytest.raises(GPUError):
        gpu_watchdog.assert_gpu_headroom(min_free_gib=4.0, model_label="test")


def test_assert_gpu_headroom_raises_on_low_vram(monkeypatch):
    from bilibili_vision import gpu_watchdog
    from bilibili_vision.errors import GPUMemoryError

    fake = gpu_watchdog.GPUStatus(
        available=True, name="FakeGPU", free_bytes=2 * (1024 ** 3), total_bytes=8 * (1024 ** 3)
    )
    monkeypatch.setattr(gpu_watchdog, "probe_gpu", lambda: fake)
    with pytest.raises(GPUMemoryError):
        gpu_watchdog.assert_gpu_headroom(min_free_gib=4.0)


def test_crash_counter_trips():
    from bilibili_vision.gpu_watchdog import CrashCounter

    c = CrashCounter(threshold=3, window_sec=60.0)
    c.record()
    c.record()
    assert not c.tripped
    c.record()
    assert c.tripped
    c.reset()
    assert not c.tripped


def test_diagnostic_zip_shape(tmp_path, monkeypatch):
    from bilibili_vision import diagnostics, log_config

    monkeypatch.setattr(diagnostics, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(log_config, "PROJECT_ROOT", tmp_path)

    log_dir = tmp_path / "out" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "20260101_000000_session.jsonl").write_text(
        '{"t":"2026-01-01T00:00:00","level":"INFO","msg":"hello"}', encoding="utf-8"
    )
    (log_dir / "vlv.log").write_text("rotating log entry\n", encoding="utf-8")

    cred = tmp_path / ".credentials"
    cred.mkdir()
    (cred / "cookies.txt").write_text("# stub", encoding="utf-8")

    out = diagnostics.build_diagnostic_zip()
    assert out.is_file()
    with zipfile.ZipFile(out, "r") as zf:
        names = zf.namelist()
        assert "environment.json" in names
        assert "config_hashes.json" in names
        assert "README.txt" in names
        assert any(n.startswith("logs/") for n in names)

        env = json.loads(zf.read("environment.json"))
        assert "python" in env and "os" in env

        hashes = json.loads(zf.read("config_hashes.json"))
        assert "cookies.txt" in hashes
        # Hash must be 16 chars of hex, not the cookie content.
        assert len(hashes["cookies.txt"]) == 16
        assert "# stub" not in hashes["cookies.txt"]
