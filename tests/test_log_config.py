"""Tests for log_config module."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest


def test_configure_logging_idempotent(tmp_path, monkeypatch):
    from bilibili_vision import log_config

    monkeypatch.setattr(log_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(log_config, "_CONFIGURED", False)
    monkeypatch.setattr(log_config, "_SESSION_PATH", None)

    p1 = log_config.configure_logging(console=False, rotating_file=False)
    p2 = log_config.configure_logging(console=False, rotating_file=False)
    assert p1 == p2
    assert p1 is not None
    assert p1.exists()


def test_session_jsonl_format(tmp_path, monkeypatch):
    from bilibili_vision import log_config

    monkeypatch.setattr(log_config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(log_config, "_CONFIGURED", False)
    monkeypatch.setattr(log_config, "_SESSION_PATH", None)

    session = log_config.configure_logging(console=False, rotating_file=False)
    logger = log_config.get_logger("vlv.test")
    logger.info("hello", extra={"platform": "bilibili", "error_code": "VLV_E000"})

    for h in logging.getLogger().handlers:
        h.flush()

    text = Path(session).read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines, "session log should have at least one line"
    last = json.loads(lines[-1])
    assert last["msg"] == "hello"
    assert last["platform"] == "bilibili"
    assert last["error_code"] == "VLV_E000"


def test_recent_session_logs(tmp_path, monkeypatch):
    from bilibili_vision import log_config

    monkeypatch.setattr(log_config, "PROJECT_ROOT", tmp_path)
    d = tmp_path / "out" / "log"
    d.mkdir(parents=True)
    for i in range(7):
        (d / f"2026010{i}_000000_session.jsonl").write_text("{}", encoding="utf-8")

    files = log_config.recent_session_logs(n=3)
    assert len(files) == 3
    assert all(p.name.endswith("_session.jsonl") for p in files)
