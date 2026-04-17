"""
VLV GUI 无状态工具:延迟提示、URL 验证、Text 滚轮、滚动条工厂。

从 gui.py 抽出,供各 GUI 子模块共用;不依赖 App,不依赖 tk 根窗口之外的全局。
"""
from __future__ import annotations

import os
import re
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path

from bilibili_vision.gui_common import (
    SCROLL_THUMB,
    SCROLL_THUMB_ACTIVE,
    SCROLL_TROUGH,
    TOOLTIP_BG,
    TOOLTIP_BORDER,
    TOOLTIP_FG,
)
from bilibili_vision.transcribe_local import is_supported_local_media


class _DelayedTooltip:
    """小「?」标签悬停若干毫秒后显示黄底说明（不占用版面）。"""

    __slots__ = ("_after", "_delay_ms", "_get_text", "_tw", "_wraplength", "widget")

    def __init__(
        self,
        widget: tk.Misc,
        get_text: Callable[[], str],
        *,
        delay_ms: int = 450,
        wraplength: int = 420,
    ) -> None:
        self.widget = widget
        self._get_text = get_text
        self._delay_ms = delay_ms
        self._wraplength = wraplength
        self._after: str | None = None
        self._tw: tk.Toplevel | None = None
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)
        widget.bind("<ButtonPress>", self._on_leave)

    def _cancel_sched(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _on_enter(self, _event: object | None = None) -> None:
        self._cancel_sched()
        self._after = self.widget.after(self._delay_ms, self._open)

    def _close_tw(self) -> None:
        if self._tw is not None:
            try:
                self._tw.destroy()
            except tk.TclError:
                pass
            self._tw = None

    def _on_leave(self, _event: object | None = None) -> None:
        self._cancel_sched()
        self._close_tw()

    def _open(self) -> None:
        self._after = None
        txt = (self._get_text() or "").strip()
        if not txt:
            return
        self._close_tw()
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        bg = TOOLTIP_BG
        bd = TOOLTIP_BORDER
        fr = tk.Frame(tw, bg=bg, highlightbackground=bd, highlightthickness=1)
        lb = tk.Label(
            fr,
            text=txt,
            bg=bg,
            fg=TOOLTIP_FG,
            justify=tk.LEFT,
            anchor=tk.NW,
            wraplength=self._wraplength,
        )
        lb.pack(padx=10, pady=8)
        fr.pack()
        tw.update_idletasks()
        w = max(tw.winfo_reqwidth(), 120)
        h = max(tw.winfo_reqheight(), 40)
        sw = self.widget.winfo_screenwidth()
        sh = self.widget.winfo_screenheight()
        if x + w > sw - 8:
            x = max(8, sw - w - 8)
        if y + h > sh - 8:
            y = max(8, self.widget.winfo_rooty() - h - 4)
        tw.geometry(f"+{x}+{y}")
        self._tw = tw


def _attach_tooltip(
    widget: tk.Misc,
    get_text: Callable[[], str],
    *,
    delay_ms: int = 450,
    wraplength: int = 420,
) -> _DelayedTooltip:
    return _DelayedTooltip(widget, get_text, delay_ms=delay_ms, wraplength=wraplength)


# 常见 B 站域名与短链（分享链接不一定是 www.bilibili.com）
URL_HINTS = (
    "bilibili.com",
    "bilibili.tv",
    "b23.tv",
    "bili2233.cn",
    "bilivideo.com",
)


def looks_like_bilibili_url(url: str) -> bool:
    u = url.strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    return any(h in u for h in URL_HINTS) or bool(re.search(r"\bBV1[\w]{9}\b", url, re.I))


def is_valid_task_source(text: str) -> bool:
    s = text.strip()
    if looks_like_bilibili_url(s):
        return True
    p = Path(os.path.expanduser(s.strip('"')))
    try:
        return bool(p.is_file() and is_supported_local_media(p))
    except OSError:
        return False


def bind_text_mousewheel(text: tk.Text, *, lines_per_notch: int) -> None:
    """加快 Text 内滚轮翻动：每「格」滚动多行（系统默认约 3 行偏慢）。"""

    def on_wheel(event: tk.Event) -> None:
        d = getattr(event, "delta", 0) or 0
        if d:
            if sys.platform == "win32":
                steps = int(-d * lines_per_notch / 120.0)
                if steps == 0:
                    steps = -lines_per_notch if d > 0 else lines_per_notch
            else:
                steps = int(-d * lines_per_notch / 120.0)
                if steps == 0:
                    steps = -lines_per_notch if d > 0 else lines_per_notch
            if steps:
                text.yview_scroll(steps, "units")
            return
        n = getattr(event, "num", 0)
        if n == 4:
            text.yview_scroll(-lines_per_notch, "units")
        elif n == 5:
            text.yview_scroll(lines_per_notch, "units")

    text.bind("<MouseWheel>", on_wheel)
    text.bind("<Button-4>", on_wheel)
    text.bind("<Button-5>", on_wheel)


def _widget_under_ancestor(w: tk.Misc | None, ancestor: tk.Misc) -> bool:
    cur: tk.Misc | None = w
    while cur is not None:
        if cur == ancestor:
            return True
        cur = cur.master  # type: ignore[assignment]
    return False


def make_text_y_scrollbar(
    parent: tk.Misc,
    text: tk.Text,
    *,
    width_px: int,
    trough: str = SCROLL_TROUGH,
    thumb: str = SCROLL_THUMB,
    thumb_active: str = SCROLL_THUMB_ACTIVE,
) -> tk.Scrollbar:
    """粗竖条（浅色主题默认）。"""
    sb = tk.Scrollbar(
        parent,
        command=text.yview,
        width=width_px,
        borderwidth=0,
        troughcolor=trough,
        bg=thumb,
        activebackground=thumb_active,
        highlightthickness=0,
        relief="flat",
        jump=1,
    )
    text.configure(yscrollcommand=sb.set)
    return sb
