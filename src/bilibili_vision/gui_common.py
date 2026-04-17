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

from bilibili_vision.paths import PROJECT_ROOT

# ── 产品显示名 ──────────────────────────────────────────
APP_TITLE = "VLV · Video Listen View"
APP_BRAND = "VLV"
APP_TAGLINE = "Video Listen View · Local & Bilibili"

# ── LLM 本地默认 / Prefs 路径 / UI 文案 ────────────────
LLM_GUI_PREF_JSON = PROJECT_ROOT / "local_llm_prefs.json"
# 与 Gemma serve 默认端口一致（原 8090 易被其它软件占用导致 POST 422）；可被 local_llm_prefs 覆盖。
DEFAULT_LOCAL_OPENAI_BASE = "http://127.0.0.1:18090/v1"
DEFAULT_LOCAL_OPENAI_MODEL_ID = "gemma-4-31b-4bit"
# Ollama OpenAI 兼容接口仍须在请求里带 model；启动/探针时若未填则作占位（可改，需与本机 ollama pull 的模型名一致）。
DEFAULT_OLLAMA_CHAT_MODEL_ID = "llama3.2"
LOCAL_INF_FLOW_HELP = (
    "多模态看图与「本地对话」共用同一套 OpenAI 兼容地址（/v1）。\n"
    "顶部栏可显示本窗口启动的推理进程（点击可打开「推理服务」页）；换页后若状态不准请点「刷新状态」。\n"
    "探针用的「服务 URL」「模型 ID」在左侧「本地对话」页上方填写（本区块不重复显示这两项）。\n"
    "出现「探针进行中…」表示正在等本地模型返回，冷启动可能较慢；随后会显示「接口可响应」或错误原因。\n"
    "「运行中」只表示子进程已启动，不等于权重已加载完。探针为单次请求，不会写入侧栏对话历史。"
)

# ── 间距网格 ────────────────────────────────────────────
SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 12
SPACING_LG = 16
SPACING_XL = 24

# ── 卡片 / 面板样式 ────────────────────────────────────
# Codex 风格:近白底 + 冷灰层级 + 1px 细边 + 近黑字 + OpenAI 绿点缀。
CARD_BORDER = "#e5e5e5"
CARD_SHADOW = "#f0f0f0"

SECTION_HEADER_FG = "#0d0d0d"

# ── 状态色 ────────────────────────────────────────────
STATUS_SUCCESS = "#10a37f"       # OpenAI 绿
STATUS_WARNING = "#d97706"       # 琥珀
STATUS_ERROR = "#dc2626"         # 砖红
STATUS_RUNNING = "#525252"       # 中性灰

# ── Codex 工作台主题色 ────────────────────────────────
T_PAGE = "#ffffff"               # 纯白页底
T_PANEL = "#ffffff"              # 面板同页底,用边框分层
T_BG = T_PAGE
T_SURFACE = T_PANEL
T_RAISED = "#f7f7f8"             # 悬浮/次级底(ChatGPT 侧栏灰)
T_BORDER = "#e5e5e5"             # 主边框
T_TEXT = "#0d0d0d"               # 近黑正文
T_MUTED = "#737373"              # 二级文字
T_ACCENT = "#10a37f"             # OpenAI 绿(主操作)
T_ACCENT_ACTIVE = "#0d8a6a"
T_ENTRY = "#ffffff"
T_SELECT = "#dbeafe"             # 淡蓝选中

NAV_BG = "#f7f7f8"               # 侧栏浅灰
NAV_SIDEBAR_BG = "#f7f7f8"
NAV_ACTIVE_BG = "#ececec"
NAV_HOVER_BG = "#ececec"
NAV_DIVIDER = "#e5e5e5"

WORKSPACE_TAB_IDLE = "#f7f7f8"
WORKSPACE_TAB_SELECTED = "#ffffff"

A_BG = "#ffffff"
A_USER = "#f4f4f4"               # 用户气泡:ChatGPT 淡灰
A_CARD = "#ffffff"               # 助手气泡:白底 + 细边
A_CARD_BORDER = "#e5e5e5"
A_META = "#737373"
A_ERR = "#fef2f2"
A_COMPOSER = "#ffffff"
A_TEXT = "#0d0d0d"

COMPOSER_INPUT_BG = "#ffffff"
COMPOSER_TOOLBAR_BG = "#f7f7f8"
COMPOSER_STOP_IDLE = "#a3a3a3"

SCROLL_TROUGH = "#ffffff"
SCROLL_THUMB = "#d4d4d4"
SCROLL_THUMB_ACTIVE = "#a3a3a3"

# 气泡 / 提示专用边框
BUBBLE_USER_BORDER = "#e5e5e5"
BUBBLE_ERROR_BORDER = "#fecaca"
BUBBLE_ASSIST_HEAD = "#404040"
STOP_BTN_HOVER_BG = "#fee2e2"
REPORT_H1_FG = "#0d0d0d"
REPORT_SUBHEAD_FG = "#404040"
TOOLTIP_BG = "#0d0d0d"           # 深色气泡提示(ChatGPT 风)
TOOLTIP_BORDER = "#0d0d0d"
TOOLTIP_FG = "#ffffff"

API_SECTION_MUTED = "#737373"

FLOW_SUB_INFO_BG = "#ecfdf5"     # 子字幕:淡绿
FLOW_SUB_INFO_BORDER = "#a7f3d0"
FLOW_SUB_MUTED = "#047857"
FLOW_FULL_INFO_BG = "#eff6ff"    # 多模态深:淡蓝
FLOW_FULL_INFO_BORDER = "#bfdbfe"
FLOW_FULL_MUTED = "#1d4ed8"

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
