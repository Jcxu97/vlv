"""Diagnostics export — produce a zip with logs + environment info for support.

Invoked from GUI "Help → Export diagnostics" or via CLI:
    python -m bilibili_vision.diagnostics > diagnostics.zip
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from .log_config import recent_session_logs
from .paths import PROJECT_ROOT


def collect_environment() -> dict:
    env = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "platform": sys.platform,
        },
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
    }
    try:
        from . import __version__  # type: ignore[attr-defined]

        env["vlv_version"] = __version__
    except Exception:
        env["vlv_version"] = "unknown"

    # Optional hardware probe — never errors out.
    try:
        from .gpu_watchdog import probe_gpu

        g = probe_gpu()
        env["gpu"] = {
            "available": g.available,
            "name": g.name,
            "total_gib": round(g.total_gib, 2),
            "free_gib": round(g.free_gib, 2),
        }
    except Exception:
        env["gpu"] = {"available": False, "error": "probe failed"}

    # ffmpeg presence.
    try:
        import shutil

        env["ffmpeg_path"] = shutil.which("ffmpeg") or "<not found>"
    except Exception:
        env["ffmpeg_path"] = "<error>"

    return env


def redact_config_hash(path: Path) -> Optional[str]:
    """Return a SHA-256 digest (first 16 chars) of a config file's bytes.
    Never includes its contents in the bundle."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


def build_diagnostic_zip(
    *, out_path: Optional[Path] = None, n_logs: int = 5
) -> Path:
    """Write a diagnostics zip and return the path."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    if out_path is None:
        out_dir = PROJECT_ROOT / "out" / "diagnostics"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"vlv_diagnostics_{stamp}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        env = collect_environment()
        zf.writestr("environment.json", json.dumps(env, indent=2, ensure_ascii=False))

        # Recent session logs.
        for p in recent_session_logs(n_logs):
            try:
                zf.writestr(f"logs/{p.name}", p.read_bytes())
            except OSError:
                continue

        # Rotating log.
        rot = PROJECT_ROOT / "out" / "log" / "vlv.log"
        if rot.is_file():
            try:
                zf.writestr("logs/vlv.log", rot.read_bytes())
            except OSError:
                pass

        # Redacted config hashes (content is NOT included).
        cred_dir = PROJECT_ROOT / ".credentials"
        hashes: dict[str, Optional[str]] = {}
        if cred_dir.is_dir():
            for f in cred_dir.iterdir():
                if f.is_file():
                    hashes[f.name] = redact_config_hash(f)
        zf.writestr("config_hashes.json", json.dumps(hashes, indent=2))

        # README for the recipient.
        zf.writestr(
            "README.txt",
            "VLV diagnostics bundle\n"
            "======================\n"
            "Contents:\n"
            "  environment.json   — Python / OS / GPU / ffmpeg info\n"
            "  logs/              — most recent structured session logs\n"
            "  config_hashes.json — SHA-256 fingerprints only; no secrets\n"
            "\n"
            "Share this zip with the VLV maintainers when reporting an issue.\n",
        )

    out_path.write_bytes(buf.getvalue())
    return out_path


def _cli() -> None:
    p = build_diagnostic_zip()
    print(str(p))


if __name__ == "__main__":
    _cli()
