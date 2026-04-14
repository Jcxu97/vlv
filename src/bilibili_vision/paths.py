"""仓库根目录与包目录（数据与模型始终在仓库根，与源码分离）。"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
SRC_DIR = PACKAGE_DIR.parent
PROJECT_ROOT = SRC_DIR.parent

__all__ = ["PACKAGE_DIR", "SRC_DIR", "PROJECT_ROOT", "subprocess_env"]


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """子进程 cwd 一般为 PROJECT_ROOT 时，注入 PYTHONPATH 以便 ``python -m bilibili_vision.*`` 可导入。"""
    e = dict(base or os.environ)
    src = str(SRC_DIR.resolve())
    prev = (e.get("PYTHONPATH") or "").strip()
    e["PYTHONPATH"] = src + (os.pathsep + prev if prev else "")
    return e
