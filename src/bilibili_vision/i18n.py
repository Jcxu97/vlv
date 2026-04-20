"""Lightweight i18n wrapper around gettext.

Usage:
    from bilibili_vision.i18n import gettext as _
    label = _("Extract")

At startup, GUI calls `set_language("en")` or `set_language("zh_CN")`.
Falls back to the source-language string if the catalog or key is missing.
"""
from __future__ import annotations

import gettext as _gettext
import os
from pathlib import Path
from typing import Optional

from .paths import PROJECT_ROOT

_DOMAIN = "vlv"
_LOCALE_DIR = PROJECT_ROOT / "src" / "bilibili_vision" / "locales"
_current: Optional[_gettext.NullTranslations] = None
_current_lang: str = ""


def available_languages() -> list[str]:
    if not _LOCALE_DIR.is_dir():
        return []
    return sorted(
        p.name for p in _LOCALE_DIR.iterdir()
        if p.is_dir() and (p / "LC_MESSAGES" / f"{_DOMAIN}.po").is_file()
    )


def set_language(lang: str) -> None:
    """Switch the active translation catalog. Safe to call repeatedly."""
    global _current, _current_lang
    if not lang:
        _current = _gettext.NullTranslations()
        _current_lang = ""
        return
    try:
        _current = _gettext.translation(
            _DOMAIN, localedir=str(_LOCALE_DIR), languages=[lang], fallback=True
        )
        _current_lang = lang
    except Exception:
        _current = _gettext.NullTranslations()
        _current_lang = ""


def current_language() -> str:
    return _current_lang


def gettext(message: str) -> str:
    """Translate a source string. Returns the original if no catalog is loaded."""
    if _current is None:
        set_language(_default_lang())
    assert _current is not None
    return _current.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    if _current is None:
        set_language(_default_lang())
    assert _current is not None
    return _current.ngettext(singular, plural, n)


def _default_lang() -> str:
    env = os.environ.get("VLV_LANG", "").strip()
    if env:
        return env
    try:
        import locale as _locale

        loc = _locale.getlocale()[0] or ""
        if loc:
            return loc
    except Exception:
        pass
    return ""


# Alias so modules can `from .i18n import _`.
_ = gettext
