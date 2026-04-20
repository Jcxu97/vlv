"""Global logging configuration for VLV.

Provides:
- configure_logging(): idempotent root logger setup
- get_logger(name): convenience wrapper
- new_session_log_path(): JSONL file under out/log/ for the current run
- JsonlFileHandler: structured JSONL sink used for diagnostics export
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:
    from .paths import PROJECT_ROOT
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]


_CONFIGURED = False
_LOCK = threading.Lock()
_SESSION_PATH: Optional[Path] = None


class _JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "t": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "ms": int(record.msecs),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in ("error_code", "task_id", "platform", "url"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


class _HumanFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


def _log_dir() -> Path:
    d = PROJECT_ROOT / "out" / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_session_log_path() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return _log_dir() / f"{stamp}_session.jsonl"


def configure_logging(
    *,
    level: int = logging.INFO,
    console: bool = True,
    session_file: bool = True,
    rotating_file: bool = True,
) -> Path | None:
    """Configure root logger. Idempotent — repeated calls are a no-op.

    Returns the session JSONL file path if session_file=True, else None.
    """
    global _CONFIGURED, _SESSION_PATH
    with _LOCK:
        if _CONFIGURED:
            return _SESSION_PATH

        env_level = os.environ.get("VLV_LOG_LEVEL", "").upper()
        if env_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            level = getattr(logging, env_level)

        root = logging.getLogger()
        root.setLevel(level)
        for h in list(root.handlers):
            root.removeHandler(h)

        if console:
            sh = logging.StreamHandler(stream=sys.stderr)
            sh.setLevel(level)
            sh.setFormatter(_HumanFormatter())
            root.addHandler(sh)

        if rotating_file:
            rot_path = _log_dir() / "vlv.log"
            rfh = logging.handlers.RotatingFileHandler(
                rot_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
            )
            rfh.setLevel(level)
            rfh.setFormatter(_HumanFormatter())
            root.addHandler(rfh)

        session_path: Path | None = None
        if session_file:
            session_path = new_session_log_path()
            jh = logging.FileHandler(session_path, encoding="utf-8")
            jh.setLevel(level)
            jh.setFormatter(_JsonlFormatter())
            root.addHandler(jh)

        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("playwright").setLevel(logging.WARNING)

        _CONFIGURED = True
        _SESSION_PATH = session_path
        return session_path


def get_logger(name: str) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


def current_session_path() -> Optional[Path]:
    return _SESSION_PATH


def recent_session_logs(n: int = 5) -> list[Path]:
    d = _log_dir()
    if not d.exists():
        return []
    files = sorted(
        (p for p in d.iterdir() if p.name.endswith("_session.jsonl")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:n]
