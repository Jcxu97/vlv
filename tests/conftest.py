"""Pytest fixtures shared by tests; primarily keeps sys.path clean and isolates tmp dirs."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def tmp_json_file(tmp_path: Path) -> Path:
    return tmp_path / "prefs.json"
