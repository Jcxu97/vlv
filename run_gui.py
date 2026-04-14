#!/usr/bin/env python3
"""从仓库根目录启动图形工作台（双击 START.bat / 启动.bat 与此等价）。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bilibili_vision.gui import main

if __name__ == "__main__":
    main()
