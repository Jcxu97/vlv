"""Routes a URL to the appropriate PlatformAdapter."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..errors import UnsupportedURLError

if TYPE_CHECKING:
    from .base import PlatformAdapter


_REGISTRY: list["PlatformAdapter"] = []
_FALLBACK: "PlatformAdapter | None" = None


def register_adapter(adapter: "PlatformAdapter", *, fallback: bool = False) -> None:
    """Register an adapter. `fallback=True` marks it as the last-resort handler."""
    global _FALLBACK
    if fallback:
        _FALLBACK = adapter
    else:
        _REGISTRY.append(adapter)


def list_adapters() -> list["PlatformAdapter"]:
    out = list(_REGISTRY)
    if _FALLBACK is not None:
        out.append(_FALLBACK)
    return out


def detect_adapter(url: str) -> "PlatformAdapter":
    """Return the first adapter that claims this URL; falls back to generic yt-dlp."""
    _ensure_defaults()
    for a in _REGISTRY:
        try:
            if a.detect(url):
                return a
        except Exception:
            continue
    if _FALLBACK is not None and _FALLBACK.detect(url):
        return _FALLBACK
    raise UnsupportedURLError(f"No adapter can handle URL: {url}")


def _ensure_defaults() -> None:
    """Lazy-import the default adapter set on first use."""
    if _REGISTRY or _FALLBACK:
        return
    from .bilibili import BilibiliAdapter
    from .youtube import YouTubeAdapter
    from .douyin import DouyinAdapter
    from .generic_ytdlp import GenericYtdlpAdapter

    register_adapter(BilibiliAdapter())
    register_adapter(YouTubeAdapter())
    register_adapter(DouyinAdapter())
    register_adapter(GenericYtdlpAdapter(), fallback=True)
