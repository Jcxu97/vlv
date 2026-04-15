"""
VLV GUI 共享常量与工具函数。

从 gui.py 提取的主题色、字体工具、DPI 工具、工具提示类等，
供 gui.py 及未来拆分的子模块共用。
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
import tkinter.font as tkfont

# ── 产品显示名 ──────────────────────────────────────────
APP_TITLE = "VLV · Video Listen View"
APP_BRAND = "VLV"
APP_TAGLINE = "Video Listen View · Local & Bilibili"

# ── 间距网格 ────────────────────────────────────────────
SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 12
SPACING_LG = 16
SPACING_XL = 24

# ── 卡片 / 面板样式 ────────────────────────────────────
CARD_BORDER = "#e1e4e8"
CARD_SHADOW = "#f0f2f5"

SECTION_HEADER_FG = "#24292f"

# ── 状态色 ──────────────────────────────────────────────
STATUS_SUCCESS = "#1a7f37"
STATUS_WARNING = "#9a6700"
STATUS_ERROR = "#cf222e"
STATUS_RUNNING = "#0969da"

# ── 浅色工作台主题色 ────────────────────────────────────
T_PAGE = "#f6f8fa"
T_PANEL = "#ffffff"
T_BG = T_PAGE
T_SURFACE = T_PANEL
T_RAISED = "#f0f3f6"
T_BORDER = "#e1e4e8"
T_TEXT = "#1f2328"
T_MUTED = "#656d76"
T_ACCENT = "#2563eb"
T_ACCENT_ACTIVE = "#1d4ed8"
T_ENTRY = T_PANEL
T_SELECT = "#dbeafe"

NAV_BG = T_PAGE
NAV_SIDEBAR_BG = T_PAGE
NAV_ACTIVE_BG = T_PANEL
NAV_HOVER_BG = "#e8ecf2"
NAV_DIVIDER = T_BORDER

WORKSPACE_TAB_IDLE = "#e8ecf2"
WORKSPACE_TAB_SELECTED = T_PANEL

A_BG = T_PANEL
A_USER = "#e8f0fe"
A_CARD = "#f6f8fa"
A_CARD_BORDER = T_BORDER
A_META = T_MUTED
A_ERR = "#fef2f2"
A_COMPOSER = T_PANEL
A_TEXT = T_TEXT

COMPOSER_INPUT_BG = "#fafbfc"
COMPOSER_TOOLBAR_BG = "#f3f5f9"
COMPOSER_STOP_IDLE = "#b1bac4"

SCROLL_TROUGH = "#e8ecf2"

API_SECTION_MUTED = "#57606a"

FLOW_SUB_INFO_BG = "#ecfdf5"
FLOW_SUB_INFO_BORDER = "#6ee7b7"
FLOW_SUB_MUTED = "#047857"
FLOW_FULL_INFO_BG = "#fff7ed"
FLOW_FULL_INFO_BORDER = "#fdba74"
FLOW_FULL_MUTED = "#9a3412"

# ── DPI 工具 ────────────────────────────────────────────
_WIN_PMDPI_V2 = 2


def win_set_per_monitor_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(_WIN_PMDPI_V2)
    except (AttributeError, OSError):
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def read_gui_scale_env() -> float | None:
    raw = os.environ.get("BILIBILI_GUI_SCALE", "").strip()
    if not raw:
        return None
    try:
        return max(0.75, min(float(raw), 4.0))
    except ValueError:
        return None


def detect_tk_scale(widget: tk.Misc) -> float:
    env = read_gui_scale_env()
    if env is not None:
        return env
    try:
        px_per_inch = float(widget.winfo_fpixels("1i"))
        s = px_per_inch / 72.0
    except tk.TclError:
        s = 1.0
    return max(1.0, min(s, 3.0))


# ── 字体工具 ────────────────────────────────────────────

def pick_mono_family(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return "Consolas"
    for name in ("Cascadia Mono", "JetBrains Mono", "Consolas", "Lucida Console"):
        if name in families:
            return name
    return "Consolas"


def font_soft_factor(geom_scale: float) -> float:
    g = min(max(geom_scale, 1.0), 2.25)
    return 1.0 + (g - 1.0) * 0.28


def pick_sans_cjk(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return "Segoe UI"
    for name in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Source Han Sans SC",
        "Noto Sans CJK SC",
        "Segoe UI",
    ):
        if name in families:
            return name
    return "Segoe UI"


# ── CollapsibleCard ─────────────────────────────────────

class CollapsibleCard(tk.Frame):
    """A card with a clickable header that toggles body visibility.

    The card has a white background, 1px border, and the header row
    acts as a disclosure toggle (▶ / ▼).
    """

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        *,
        initially_open: bool = True,
        font: tuple | None = None,
        status_dot_color: str | None = None,
        pad_body: int = SPACING_MD,
    ) -> None:
        super().__init__(
            parent,
            bg=T_PANEL,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
        )
        self._open = initially_open
        self._pad_body = pad_body

        hdr = tk.Frame(self, bg=T_PANEL, cursor="hand2")
        hdr.pack(fill=tk.X)

        self._arrow = tk.Label(
            hdr,
            text="▼" if initially_open else "▶",
            bg=T_PANEL,
            fg=T_MUTED,
            font=font or ("Segoe UI", 9),
        )
        self._arrow.pack(side=tk.LEFT, padx=(SPACING_MD, SPACING_SM), pady=SPACING_SM)

        self._title_lbl = tk.Label(
            hdr,
            text=title,
            bg=T_PANEL,
            fg=SECTION_HEADER_FG,
            font=font or ("Segoe UI", 10, "bold"),
            anchor=tk.W,
        )
        self._title_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=SPACING_SM)

        self._status_dot: tk.Label | None = None
        if status_dot_color:
            self._status_dot = tk.Label(
                hdr,
                text="●",
                bg=T_PANEL,
                fg=status_dot_color,
                font=("Segoe UI", 8),
            )
            self._status_dot.pack(side=tk.RIGHT, padx=(0, SPACING_MD), pady=SPACING_SM)

        for w in (hdr, self._arrow, self._title_lbl):
            w.bind("<Button-1>", self._toggle)

        self.body = tk.Frame(self, bg=T_PANEL)
        if initially_open:
            self.body.pack(
                fill=tk.BOTH, expand=True,
                padx=SPACING_LG, pady=(0, pad_body),
            )

    def _toggle(self, _event: object = None) -> None:
        self._open = not self._open
        if self._open:
            self._arrow.configure(text="▼")
            self.body.pack(
                fill=tk.BOTH, expand=True,
                padx=SPACING_LG, pady=(0, self._pad_body),
            )
        else:
            self._arrow.configure(text="▶")
            self.body.pack_forget()

    def set_open(self, state: bool) -> None:
        if state != self._open:
            self._toggle()

    def set_status_dot(self, color: str) -> None:
        if self._status_dot is not None:
            try:
                self._status_dot.configure(fg=color)
            except tk.TclError:
                pass
