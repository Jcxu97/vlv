"""原子文本写入：写到同目录 .<name>.tmp-<pid>,然后 os.replace 到目标。

进程中途崩溃时目标文件要么是旧内容、要么是完整新内容,不会出现半写损坏的 JSON。
在 Windows NTFS 上 os.replace 是原子的。临时文件不论成功失败都会尝试清理。
"""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
