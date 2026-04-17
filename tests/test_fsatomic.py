"""Regression tests for Bug #1: atomic JSON writes."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bilibili_vision.fsatomic import atomic_write_text


def test_creates_new_file(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    atomic_write_text(p, '{"a":1}')
    assert p.read_text(encoding="utf-8") == '{"a":1}'


def test_overwrites_existing(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    p.write_text("OLD", encoding="utf-8")
    atomic_write_text(p, "NEW")
    assert p.read_text(encoding="utf-8") == "NEW"


def test_creates_parent_directories(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deep" / "out.json"
    atomic_write_text(p, "hi")
    assert p.read_text(encoding="utf-8") == "hi"


def test_leaves_original_if_replace_fails(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "out.json"
    p.write_text("OLD", encoding="utf-8")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "NEW")
    # Original must survive the failure.
    assert p.read_text(encoding="utf-8") == "OLD"


def test_cleans_up_temp_file_on_failure(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "out.json"
    p.write_text("OLD", encoding="utf-8")

    def boom(_src: object, _dst: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "NEW")
    # No stray .tmp- files left in directory.
    stray = [q for q in tmp_path.iterdir() if q.name.startswith(".") and ".tmp-" in q.name]
    assert stray == []


def test_cleans_up_temp_file_on_success(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    atomic_write_text(p, "ok")
    stray = [q for q in tmp_path.iterdir() if q.name.startswith(".") and ".tmp-" in q.name]
    assert stray == []


def test_round_trip_utf8(tmp_path: Path) -> None:
    p = tmp_path / "out.json"
    payload = '{"名字": "你好世界", "emoji": "🎵"}'
    atomic_write_text(p, payload)
    assert p.read_text(encoding="utf-8") == payload
