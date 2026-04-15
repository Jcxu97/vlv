"""仓库根目录与包目录（数据与模型始终在仓库根，与源码分离）。"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent

_MARKER_FILES = ("README.md", "requirements.txt", "run_gui.py")


def _find_project_root() -> Path:
    """Walk up from PACKAGE_DIR looking for a repo marker file.

    Falls back to SRC_DIR.parent (original heuristic) if no marker found,
    which keeps the portable-embed layout working.
    """
    candidate = PACKAGE_DIR
    for _ in range(6):
        if any((candidate / m).is_file() for m in _MARKER_FILES):
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return SRC_DIR.parent


PROJECT_ROOT = _find_project_root()

__all__ = ["PACKAGE_DIR", "SRC_DIR", "PROJECT_ROOT", "subprocess_env"]


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """子进程 cwd 一般为 PROJECT_ROOT 时，注入 PYTHONPATH 以便 ``python -m bilibili_vision.*`` 可导入。"""
    e = dict(base or os.environ)
    src = str(SRC_DIR.resolve())
    prev = (e.get("PYTHONPATH") or "").strip()
    e["PYTHONPATH"] = src + (os.pathsep + prev if prev else "")
    return e
