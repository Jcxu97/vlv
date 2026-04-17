"""Unit tests for bilibili_vision.paths."""
from __future__ import annotations

import os

from bilibili_vision.paths import PROJECT_ROOT, SRC_DIR, subprocess_env


def test_project_root_contains_marker() -> None:
    markers = ("README.md", "requirements.txt", "run_gui.py")
    assert any((PROJECT_ROOT / m).is_file() for m in markers)


def test_subprocess_env_empty_prev() -> None:
    e = subprocess_env({"PYTHONPATH": ""})
    assert e["PYTHONPATH"] == str(SRC_DIR.resolve())


def test_subprocess_env_prepends_and_preserves_prev() -> None:
    prev = "/some/other/path"
    e = subprocess_env({"PYTHONPATH": prev})
    parts = e["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(SRC_DIR.resolve())
    assert prev in parts


def test_subprocess_env_does_not_mutate_input() -> None:
    base = {"PYTHONPATH": "/original", "FOO": "bar"}
    snapshot = dict(base)
    subprocess_env(base)
    assert base == snapshot


def test_subprocess_env_propagates_other_vars() -> None:
    e = subprocess_env({"PYTHONPATH": "", "OTHER": "xyz"})
    assert e["OTHER"] == "xyz"


def test_subprocess_env_strips_whitespace_only_prev() -> None:
    e = subprocess_env({"PYTHONPATH": "   "})
    assert e["PYTHONPATH"] == str(SRC_DIR.resolve())
