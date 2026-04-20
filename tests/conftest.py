"""Shared pytest fixtures and path setup for VLV tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def tmp_json_file(tmp_path: Path) -> Path:
    return tmp_path / "prefs.json"


@pytest.fixture
def isolated_project_root(tmp_path: Path, monkeypatch) -> Path:
    """Create a minimal fake PROJECT_ROOT layout so output_session / paths can
    be exercised without touching the real `out/` directory."""
    (tmp_path / "src").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "README.md").write_text("stub", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("", encoding="utf-8")
    monkeypatch.setenv("VLV_PROJECT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def mock_ytdlp_info():
    """Minimal yt-dlp info_dict used by platform adapter tests."""
    return {
        "id": "test_id_123",
        "title": "Test Video",
        "uploader": "Test Channel",
        "duration": 120,
        "webpage_url": "https://example.com/watch?v=test_id_123",
        "subtitles": {},
        "automatic_captions": {},
        "formats": [{"format_id": "best", "url": "https://example.com/stream"}],
    }


@pytest.fixture
def mock_ytdlp_subprocess(monkeypatch):
    """Patch subprocess.run so yt-dlp calls return a fixed JSON payload."""
    import json
    import subprocess

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        info = {
            "id": "mock_id",
            "title": "Mock Video",
            "uploader": "Mock Uploader",
            "duration": 60,
            "webpage_url": cmd[-1] if isinstance(cmd, list) else "",
        }
        result.stdout = json.dumps(info)
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.fixture
def mock_llm_http(monkeypatch):
    """Patch urllib.request.urlopen used by llm_analyze / cloud clients."""
    import io
    import json
    import urllib.request

    state = {"last_request": None, "response": {"ok": True, "text": "stub response"}}

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._buf = io.BytesIO(body)
            self.status = 200

        def read(self, *a, **k):
            return self._buf.read(*a, **k)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._buf.close()

        def getcode(self):
            return 200

    def fake_urlopen(req, *args, **kwargs):
        state["last_request"] = req
        body = json.dumps(state["response"]).encode("utf-8")
        return _FakeResp(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def quiet_logging():
    """Silence VLV logging for tests that would otherwise spam stderr."""
    import logging

    level_before = logging.getLogger().level
    logging.getLogger().setLevel(logging.CRITICAL)
    yield
    logging.getLogger().setLevel(level_before)
