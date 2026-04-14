"""
听视纪要 (ListenView)：本地音视频 + B 站，统一「内容提取与分析」页（仅字幕总结 / 多模态深度可选）+ 大模型对话。
浅色工作台界面：参考 Cursor / Codex 类工具的层次与留白，保持亮色主题（非深色）。

高 DPI：启动前启用 Windows Per-Monitor V2 感知；可用环境变量 BILIBILI_GUI_SCALE 覆盖缩放。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import shutil
import threading
import uuid
from datetime import datetime
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from bilibili_vision.paths import PROJECT_ROOT, subprocess_env

# 产品显示名（窗口标题、侧栏；与 README 一致）
APP_TITLE = "听视纪要 · ListenView"
APP_BRAND = "听视纪要"
APP_TAGLINE = "B站 · 本地 · 转写 · AI"

# 子进程 analyze_transcript / vision_deep_pipeline 输出的机器可读进度前缀
_GUI_PROGRESS_PREFIX = "__GUI_PROGRESS__ "
# 便携版：子进程继承此变量，Playwright 使用目录内浏览器
_pw_browsers = PROJECT_ROOT / "pw-browsers"
if _pw_browsers.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_pw_browsers.resolve()))
from bilibili_vision.llm_analyze import (
    MAX_LOCAL_CHAT_MERGED_SNIPPET,
    OpenAICompatibleRequestCancelled,
    SYSTEM_LOCAL_CHAT_ZH,
    _normalize_openai_v1_base,
    chat_followup,
    collect_timeline_frame_paths,
    local_openai_compatible_chat_round,
    probe_local_openai_chat_health,
    resolve_provider,
)
from bilibili_vision.transcribe_local import (
    SUPPORTED_LOCAL_MEDIA_SUFFIXES,
    WHISPER_MODEL_CHOICES,
    default_whisper_model_choice,
    is_supported_local_media,
)

# Windows：PROCESS_PER_MONITOR_DPI_AWARE_V2，须在创建任何 Tk 窗口前调用
_WIN_PMDPI_V2 = 2


def _win_set_per_monitor_dpi() -> None:
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


def _read_gui_scale_env() -> float | None:
    raw = os.environ.get("BILIBILI_GUI_SCALE", "").strip()
    if not raw:
        return None
    try:
        return max(0.75, min(float(raw), 4.0))
    except ValueError:
        return None


def _detect_tk_scale(widget: tk.Misc) -> float:
    env = _read_gui_scale_env()
    if env is not None:
        return env
    try:
        px_per_inch = float(widget.winfo_fpixels("1i"))
        s = px_per_inch / 72.0
    except tk.TclError:
        s = 1.0
    return max(1.0, min(s, 3.0))


def _pick_mono_family(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return "Consolas"
    for name in ("Cascadia Mono", "JetBrains Mono", "Consolas", "Lucida Console"):
        if name in families:
            return name
    return "Consolas"


def _font_soft_factor(geom_scale: float) -> float:
    """高 DPI 下 tk scaling 已放大界面，字号不要再按 geom 线性乘，只做轻微补偿。"""
    g = min(max(geom_scale, 1.0), 2.25)
    return 1.0 + (g - 1.0) * 0.28


def _pick_sans_cjk(root: tk.Misc) -> str:
    """正文用无衬线 + 中文优先，避免 Tk Text 默认回退成宋体发糊。"""
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


# 单次任务输出在 out/YYYY-MM-DD/HHMMSS_标题_BV…/；运行中由子进程回写 BILIBILI_VISION_OUT；prefs 只记 _active_session_out，不污染全局 env。
OUT_ROOT = PROJECT_ROOT / "out"
LOCAL_CHAT_ROOT = PROJECT_ROOT / "local_chat_data"
# 与 Gemma serve 默认端口一致（原 8090 易被其它软件占用导致 POST 422）；可被 local_llm_prefs 覆盖。
DEFAULT_LOCAL_OPENAI_BASE = "http://127.0.0.1:18090/v1"
DEFAULT_LOCAL_OPENAI_MODEL_ID = "gemma-4-31b-4bit"
# Ollama OpenAI 兼容接口仍须在请求里带 model；启动/探针时若未填则作占位（可改，需与本机 ollama pull 的模型名一致）。
DEFAULT_OLLAMA_CHAT_MODEL_ID = "llama3.2"


def _maybe_rewrite_lm_studio_local_url(url: str | None) -> str:
    """空串保持空；经典 LM Studio 默认 1234 → 本项目 Gemma 18090。"""
    u = (url if url is not None else "").strip()
    if not u:
        return ""
    low = u.rstrip("/").lower()
    if low in ("http://127.0.0.1:1234/v1", "http://localhost:1234/v1"):
        return DEFAULT_LOCAL_OPENAI_BASE
    return u


def _local_openai_base_for_ui(stored: str | None) -> str:
    """偏好/输入框：空或上述 LM 默认 → Gemma 默认。"""
    return _maybe_rewrite_lm_studio_local_url(stored) or DEFAULT_LOCAL_OPENAI_BASE


def _fix_gemma_misconfigured_port8090(base: str, model: str) -> str:
    """
    本仓库 SERVE_GEMMA4 默认监听 18090；8090 常被误填且本机无服务。
    仅当模型名含 gemma 且 URL 指向本机 8090 时改为 18090（避免误伤真在 8090 上跑的服务）。
    """
    if "gemma" not in (model or "").lower():
        return (base or "").strip()
    b = (base or "").strip().rstrip("/")
    low = b.lower()
    if "127.0.0.1:8090" in low:
        return re.sub(r"127\.0\.0\.1:8090", "127.0.0.1:18090", b, count=1, flags=re.I)
    if "localhost:8090" in low:
        return re.sub(r"localhost:8090", "localhost:18090", b, count=1, flags=re.I)
    return b


def _migrate_local_llm_prefs_json() -> None:
    try:
        if not LLM_GUI_PREF_JSON.is_file():
            return
        raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        changed = False
        for key in ("local_inf_autofill_url", "chat_local_base_url"):
            if key not in raw:
                continue
            old = raw.get(key)
            if not isinstance(old, str):
                continue
            new = _local_openai_base_for_ui(old)
            if new != old:
                raw[key] = new
                changed = True
        if changed:
            LLM_GUI_PREF_JSON.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, json.JSONDecodeError, TypeError):
        pass


LLM_PROVIDER_COMBO_VALUES = ("auto", "gemini", "openai", "groq", "anthropic", "xai")
_LLM_PROVIDER_SET = frozenset(LLM_PROVIDER_COMBO_VALUES)
LLM_GUI_PREF_JSON = PROJECT_ROOT / "local_llm_prefs.json"


def _norm_llm_provider(raw: str) -> str:
    p = (raw or "auto").strip().lower()
    return p if p in _LLM_PROVIDER_SET else "auto"


def _load_llm_gui_prefs_merged() -> dict:
    try:
        if LLM_GUI_PREF_JSON.is_file():
            raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def _load_gui_llm_provider_pref() -> str:
    try:
        data = _load_llm_gui_prefs_merged()
        return _norm_llm_provider(str(data.get("llm_provider", "")))
    except (TypeError, ValueError):
        pass
    return ""


def _save_gui_llm_provider_pref(provider: str) -> None:
    prov = _norm_llm_provider(provider)
    merged = _load_llm_gui_prefs_merged()
    merged["llm_provider"] = prov
    try:
        LLM_GUI_PREF_JSON.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _save_llm_gui_chat_prefs(
    *,
    chat_route: str,
    chat_local_base: str,
    chat_local_model: str,
    chat_local_key: str,
) -> None:
    merged = _load_llm_gui_prefs_merged()
    merged["chat_route"] = chat_route.strip()
    merged["chat_local_base_url"] = chat_local_base.strip()
    merged["chat_local_model"] = chat_local_model.strip()
    merged["chat_local_api_key"] = chat_local_key.strip()
    try:
        LLM_GUI_PREF_JSON.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

# 浅色工作台侧栏（Cursor / Codex 式层次，非深色）
T_PAGE = "#f6f8fa"
T_PANEL = "#ffffff"
T_BG = T_PAGE
T_SURFACE = T_PANEL
T_RAISED = "#eff2f6"
T_BORDER = "#d0d7de"
T_TEXT = "#1f2328"
T_MUTED = "#656d76"
T_ACCENT = "#2563eb"
T_ACCENT_ACTIVE = "#1d4ed8"
T_ENTRY = T_PANEL
T_SELECT = "#dbeafe"

NAV_BG = T_PAGE
NAV_SIDEBAR_BG = T_PAGE
NAV_ACTIVE_BG = T_PANEL
NAV_HOVER_BG = "#eef1f6"
NAV_DIVIDER = T_BORDER

# 工作区标签：中性灰底 + 选中白底（去掉便签暖色）
WORKSPACE_TAB_IDLE = "#e8ecf2"
WORKSPACE_TAB_SELECTED = T_PANEL

# 对话气泡
A_BG = T_PANEL
A_USER = "#e8f0fe"
A_CARD = "#f6f8fa"
A_CARD_BORDER = T_BORDER
A_META = T_MUTED
A_ERR = "#fef2f2"
A_COMPOSER = T_PANEL
A_TEXT = T_TEXT

# 本地对话输入区：浅色底栏（非深色）
COMPOSER_INPUT_BG = "#fafbfc"
COMPOSER_TOOLBAR_BG = "#f3f5f9"
COMPOSER_STOP_IDLE = "#b1bac4"

SCROLL_TROUGH = "#e8ecf2"

# API 设置页：次要说明文字色
API_SECTION_MUTED = "#57606a"

# 流程页：信息条配色
FLOW_SUB_INFO_BG = "#ecfdf5"
FLOW_SUB_INFO_BORDER = "#6ee7b7"
FLOW_SUB_MUTED = "#047857"
FLOW_FULL_INFO_BG = "#fff7ed"
FLOW_FULL_INFO_BORDER = "#fdba74"
FLOW_FULL_MUTED = "#9a3412"

# 运行页「?」悬停：长说明（正文只保留一行提示）
FLOW_HELP_MULTIMODAL = (
    "与快速模式共用链接与 Whisper；下方配置抽帧 / OCR / Gemma 看图。\n\n"
    "B 站任务会下载视频到 out 目录供抽帧。\n"
    "无字幕、依赖画面理解时：须先启动本地 OpenAI 兼容多模态服务（本页「本地推理服务」或 bat），"
    "且「本地对话」里的服务 URL、模型 ID 须与下方「③」一致，否则易连接被拒绝或整段无画面描述。"
)
FLOW_HELP_SUBTITLE = (
    "仅生成基础总结；需要「深度内容分析」或画面管线时请切换到「多模态」。\n\n"
    "B 站优先拉字幕；无字幕可用 Whisper。本地音视频直接转写。"
)
LOCAL_INF_FLOW_HELP = (
    "多模态看图与「本地对话」共用同一套 OpenAI 兼容地址（/v1）。\n"
    "顶部栏可显示本窗口启动的推理进程（点击可打开「推理服务」页）；换页后若状态不准请点「刷新状态」。\n"
    "探针用的「服务 URL」「模型 ID」在左侧「本地对话」页上方填写（本区块不重复显示这两项）。\n"
    "出现「探针进行中…」表示正在等本地模型返回，冷启动可能较慢；随后会显示「接口可响应」或错误原因。\n"
    "「运行中」只表示子进程已启动，不等于权重已加载完。探针为单次请求，不会写入侧栏对话历史。"
)
FLOW_MODE_HELP = (
    "快速：只拉字幕/弹幕与 Whisper，生成基础总结，不跑深度稿与画面管线。\n\n"
    "多模态：在快速流程之外可生成深度分析，并可选抽帧、OCR、Gemma 一句看图；"
    "需配置下方画面管线与本机推理服务。"
)
FLOW_VIEW_MODE_HELP = (
    "粗看：全片先按「抽帧间隔」粗采样，再均匀选约 20 帧做 OCR/VLM，分层深度段落较粗；省 API 与耗时。\n\n"
    "细看：在粗看基础上把写入配置的抽帧间隔上限收紧为 1s（若你选的大于 1s）、"
    "时间轴最多约 96 帧 VLM、分层切段更细；更接近「沿时间轴多看画面」，仍非逐帧等价，但成本高很多。"
)
FLOW_REP_TIP_CHECK = (
    "对合并文稿（transcript_merged.txt）调用大模型生成「视频内容总结」。"
    "多模态并开启深度时，深度稿也沿用此处选择的提供方（在线或本地）。"
)
FLOW_REP_TIP_API = (
    "使用「API 与模型」页配置的首选提供商；auto 时按已填 Key 选择（如 Gemini）。"
    "多模态且此处为「API 首选」时，画面逐帧 VLM 与分层深度会沿用同一在线 Key/模型"
    "（须支持图像；Groq/xAI 请在模型栏选用 vision 模型，见各平台文档）。"
)
FLOW_REP_TIP_LOCAL = (
    "总结与深度分析均走本机 OpenAI 兼容服务；须与下方「③ Gemma 看图」的服务 URL、模型 ID 一致，"
    "并先在本页或 bat 中启动 serve。"
)


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
        bg = "#fffbeb"
        bd = "#fcd34d"
        fr = tk.Frame(tw, bg=bg, highlightbackground=bd, highlightthickness=1)
        lb = tk.Label(
            fr,
            text=txt,
            bg=bg,
            fg=T_TEXT,
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
    thumb: str = "#c4c4c4",
    thumb_active: str = "#a8a8a8",
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


def sanitize_analysis_display(text: str) -> str:
    """弱化旧版 Markdown 痕迹，便于阅读（新 prompt 下模型应不再输出这些）。"""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\n"):
            core, nl = line[:-1], "\n"
        else:
            core, nl = line, ""
        core = re.sub(r"^#{1,6}\s*", "", core)
        core = re.sub(r"\*\*([^*]+)\*\*", r"\1", core)
        core = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", core)
        core = re.sub(r"^\*\s+", "· ", core)
        core = re.sub(r"^-\s+", "· ", core)
        core = re.sub(r"(?m)^\s+-\s+", "  · ", core)
        out.append(core + nl)
    return "".join(out)


_REPORT_META_LINE = re.compile(r"^（[^）\n]{0,400}）$")


def _decode_subprocess_line(raw: bytes) -> str:
    """子进程在 Windows 上可能输出 GBK；优先 UTF-8，失败则回退 GBK。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        if sys.platform == "win32":
            try:
                return raw.decode("gbk", errors="replace")
            except UnicodeDecodeError:
                pass
        return raw.decode("utf-8", errors="replace")


def _register_infer_atexit(app: "App") -> None:
    """正常点关闭会走 destroy；Ctrl+C 或未走 WM_DELETE_WINDOW 时仍尽量结束本程序拉起的本地推理子进程。"""

    def _cb() -> None:
        if getattr(app, "_app_destroy_started", False):
            return
        try:
            hub = getattr(app, "_local_chat_hub", None)
            infer = getattr(hub, "_infer", None) if hub is not None else None
            if infer is not None:
                infer.terminate_child_proc()
        except Exception:
            pass

    atexit.register(_cb)


def _terminate_process_tree(proc: subprocess.Popen | None) -> None:
    """终止子进程及其子进程（Windows 下 yt-dlp / Python 会再拉子进程，需 /T）。"""
    if proc is None:
        return
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            kw: dict = {
                "args": ["taskkill", "/PID", str(pid), "/T", "/F"],
                "capture_output": True,
                "timeout": 45,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(**kw)
        else:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
    except (OSError, subprocess.TimeoutExpired):
        pass


# 用户点击「停止当前任务」时 on_done 传入此退出码（非子进程真实码）
PIPELINE_EXIT_USER_CANCEL = -100


def report_line_style_tag(line: str) -> str | None:
    """按行选择 Tk Text 标签，与纯文本章节标题约定一致。"""
    s = line.strip()
    if not s:
        return None
    if s.startswith("【视频内容总结】") or s.startswith("【深度内容分析】"):
        return "report_title"
    if _REPORT_META_LINE.match(s):
        return "report_meta"
    if re.match(r"^[一二三四五六七八九十百千万]+、", s):
        return "report_h1"
    if re.match(r"^观看建议[：:]?", s):
        return "report_subhead"
    if re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节卷、.．]", s):
        return "report_h1"
    return None


def run_pipeline(
    url: str,
    on_line,
    on_done,
    *,
    asr_if_no_subs: bool = False,
    asr_force: bool = False,
    download_bilibili_video: bool = True,
    whisper_model: str | None = None,
    llm_provider: str = "auto",
    deep_analyze: bool = False,
    vision_run_after: bool = False,
    app: App | None = None,
    pending_deep_for_vision: bool = False,
) -> None:
    env = subprocess_env(os.environ.copy())
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")

    def worker() -> None:
        code = -1

        def user_cancelled() -> bool:
            return app is not None and app._pipeline_cancel_requested.is_set()

        try:
            cmd = [
                sys.executable,
                "-u",
                "-m",
                "bilibili_vision.bilibili_pipeline",
                "extract",
                # bilibili_pipeline 默认会再跑 analyze_transcript（无参=auto，优先 Gemini）；
                # GUI 在下方用用户选的 --llm-provider 单独跑，避免重复且避免与首选不一致。
                "--no-analyze",
            ]
            if asr_if_no_subs:
                cmd.append("--asr-if-no-subs")
            if asr_force:
                cmd.append("--asr-force")
            if not download_bilibili_video:
                cmd.append("--no-download-video")
            wm = (whisper_model or default_whisper_model_choice()).strip()
            if wm not in WHISPER_MODEL_CHOICES:
                wm = default_whisper_model_choice()
            cmd.extend(["--whisper-model", wm])
            cmd.append(url.strip())
            proc = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                bufsize=0,
            )
            if app is not None:
                app._register_pipeline_subprocess(proc)
            try:
                assert proc.stdout is not None
                while True:
                    line_b = proc.stdout.readline()
                    if not line_b:
                        break
                    line = _decode_subprocess_line(line_b)
                    raw = line.rstrip("\r\n")
                    if raw.startswith("__VISION_OUTPUT_DIR__"):
                        try:
                            rest = raw.split(None, 1)[1].strip()
                            p = Path(rest).resolve()
                            env["BILIBILI_VISION_OUT"] = str(p)
                            os.environ["BILIBILI_VISION_OUT"] = str(p)
                            if app is not None:

                                def _bind(px: Path = p, d: bool = pending_deep_for_vision) -> None:
                                    app._on_session_output_dir(px, d)

                                app.after(0, _bind)
                        except (IndexError, OSError, ValueError):
                            pass
                    on_line(line)
                code = proc.wait()
            finally:
                if app is not None:
                    app._unregister_pipeline_subprocess(proc)

            if user_cancelled():
                code = PIPELINE_EXIT_USER_CANCEL
            # 确保分析报告与当前 transcript_merged.txt 一致（避免子进程缓冲/旧版管线漏跑 analyze）
            elif code == 0:
                on_line("\n--- 正在刷新分析报告（analyze_transcript.py）---\n")
                lp = (llm_provider or "auto").strip() or "auto"
                acmd = [
                    sys.executable,
                    "-u",
                    "-m",
                    "bilibili_vision.analyze_transcript",
                    "--llm-provider",
                    lp,
                ]
                if deep_analyze:
                    acmd.append("--deep")
                # 云端 deep 默认 5min；本地 OpenAI 兼容（Gemma 等）单次 HTTP 已放宽到 900s，
                # 且 deep 会连续「基础总结 + 深度分析」两次请求，整进程 300s 必被误杀。
                if lp == "local":
                    timeout_sec = 3600 if deep_analyze else 900
                else:
                    timeout_sec = 300 if deep_analyze else 120
                arcode = -1
                aproc: subprocess.Popen | None = None
                try:
                    aproc = subprocess.Popen(
                        acmd,
                        cwd=str(PROJECT_ROOT),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        env=env,
                        bufsize=0,
                    )
                    if app is not None:
                        app._register_pipeline_subprocess(aproc)
                    assert aproc.stdout is not None

                    def _pump_analyze_stdout() -> None:
                        try:
                            while True:
                                b = aproc.stdout.readline()
                                if not b:
                                    break
                                on_line(_decode_subprocess_line(b))
                        except Exception:
                            pass

                    pump_t = threading.Thread(target=_pump_analyze_stdout, daemon=True)
                    pump_t.start()
                    try:
                        arcode = aproc.wait(timeout=timeout_sec)
                    except subprocess.TimeoutExpired:
                        _terminate_process_tree(aproc)
                        try:
                            arcode = aproc.wait(timeout=15)
                        except Exception:
                            arcode = -1
                        on_line(
                            f"\n[警告] 分析报告步骤超时（>{timeout_sec}s），已终止子进程。\n"
                        )
                    pump_t.join(timeout=3.0)
                except OSError as e:
                    on_line(f"\n[警告] 无法启动 analyze_transcript：{e}\n")
                    arcode = -1
                finally:
                    if aproc is not None and app is not None:
                        app._unregister_pipeline_subprocess(aproc)
                if user_cancelled():
                    code = PIPELINE_EXIT_USER_CANCEL
                elif arcode != 0:
                    on_line(f"\n[警告] 分析报告步骤退出码 {arcode}\n")
                    code = arcode
                out_raw = env.get("BILIBILI_VISION_OUT", "").strip()
                vc_path = (
                    Path(out_raw).resolve() / "vision_run_config.json"
                    if out_raw
                    else None
                )
                if (
                    not user_cancelled()
                    and code == 0
                    and deep_analyze
                    and vision_run_after
                    and vc_path is not None
                    and vc_path.is_file()
                ):
                    try:
                        vcfg = json.loads(vc_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError, TypeError):
                        vcfg = {}
                    if isinstance(vcfg, dict) and vcfg.get("enabled"):
                        on_line("\n--- 多模态画面管线（vision_deep_pipeline.py）---\n")
                        vpcmd = [
                            sys.executable,
                            "-u",
                            "-m",
                            "bilibili_vision.vision_deep_pipeline",
                            "--config",
                            str(vc_path.resolve()),
                        ]
                        vproc: subprocess.Popen | None = None
                        try:
                            vproc = subprocess.Popen(
                                vpcmd,
                                cwd=str(PROJECT_ROOT),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL,
                                env=env,
                                bufsize=0,
                            )
                            if app is not None:
                                app._register_pipeline_subprocess(vproc)
                            assert vproc.stdout is not None
                            while True:
                                vb = vproc.stdout.readline()
                                if not vb:
                                    break
                                on_line(_decode_subprocess_line(vb))
                            vcode = vproc.wait()
                            if vcode != 0:
                                on_line(
                                    f"\n[警告] 多模态画面管线退出码 {vcode}\n"
                                )
                        except Exception as ve:
                            on_line(f"\n[警告] 多模态画面管线未执行：{ve}\n")
                        finally:
                            if vproc is not None and app is not None:
                                app._unregister_pipeline_subprocess(vproc)
                        if user_cancelled():
                            code = PIPELINE_EXIT_USER_CANCEL
        except Exception as e:
            on_line(f"\n[错误] {e}\n")
        finally:
            if user_cancelled():
                code = PIPELINE_EXIT_USER_CANCEL
            on_done(code)

    threading.Thread(target=worker, daemon=True).start()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        _migrate_local_llm_prefs_json()
        self.title(APP_TITLE)
        self.configure(bg=T_BG)

        self.update_idletasks()
        self._scale = _detect_tk_scale(self)
        try:
            self.tk.call("tk", "scaling", "-displayof", ".", str(self._scale))
        except tk.TclError:
            pass

        self._geom_scale = min(self._scale, 2.25)
        gw = int(1240 * self._geom_scale)
        gh = int(760 * self._geom_scale)
        self.geometry(f"{gw}x{gh}")
        self.minsize(
            int(880 * min(self._geom_scale, 2.0)),
            int(600 * min(self._geom_scale, 2.0)),
        )

        self._font_soft = _font_soft_factor(self._geom_scale)
        self._scrollbar_px = max(
            18, int(round(20 + 8 * (min(self._geom_scale, 2.25) - 1.0)))
        )
        self._text_wheel_lines = max(6, min(14, int(round(9 * self._font_soft))))
        self._run_button_style = self._setup_fonts_and_styles()
        self._init_llm_settings_vars()
        self.agent_session = AgentSession(self)
        inner = max(8, int(round(8 + 3 * (min(self._geom_scale, 2.0) - 1.0))))

        main = tk.Frame(self, bg=T_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        nav_w = int(216 * min(self._geom_scale, 1.35))
        self._nav = tk.Frame(main, bg=NAV_BG, width=nav_w)
        self._nav.pack(side=tk.LEFT, fill=tk.Y)
        self._nav.pack_propagate(False)

        brand = tk.Frame(self._nav, bg=NAV_BG)
        brand.pack(anchor=tk.W, fill=tk.X, padx=16, pady=(18, 6))
        tk.Label(
            brand,
            text=APP_BRAND,
            font=self._font_title,
            bg=NAV_BG,
            fg=T_TEXT,
            anchor=tk.W,
        ).pack(anchor=tk.W)
        tk.Label(
            brand,
            text=APP_TAGLINE,
            font=self._font_hint,
            bg=NAV_BG,
            fg=T_MUTED,
            anchor=tk.W,
        ).pack(anchor=tk.W, pady=(1, 0))

        tk.Label(
            self._nav,
            text="工作区",
            font=self._font_hint,
            bg=NAV_BG,
            fg=T_MUTED,
        ).pack(anchor=tk.W, padx=16, pady=(10, 4))

        self._nav_rows: dict[str, tk.Frame] = {}
        self._nav_labels: dict[str, tk.Label] = {}
        self._nav_accents: dict[str, tk.Frame] = {}
        self._nav_inners: dict[str, tk.Frame] = {}
        self._current_nav = ""

        def nav_item(key: str, title: str) -> None:
            row = tk.Frame(self._nav, bg=NAV_SIDEBAR_BG, cursor="hand2")
            row.pack(fill=tk.X, padx=(12, 10), pady=2)
            accent = tk.Frame(row, width=3, bg=NAV_SIDEBAR_BG)
            accent.pack(side=tk.LEFT, fill=tk.Y)
            accent.pack_propagate(False)
            inner = tk.Frame(row, bg=NAV_SIDEBAR_BG, cursor="hand2")
            inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            lb = tk.Label(
                inner,
                text=title,
                font=self._font_ui,
                bg=NAV_SIDEBAR_BG,
                fg=T_TEXT,
                anchor=tk.W,
                padx=12,
                pady=7,
            )
            lb.pack(fill=tk.X)
            self._nav_rows[key] = row
            self._nav_labels[key] = lb
            self._nav_accents[key] = accent
            self._nav_inners[key] = inner

            def on_enter(_e: object) -> None:
                if self._current_nav == key:
                    return
                self._paint_nav_row(key, hover=True)

            def on_leave(_e: object) -> None:
                self._sync_nav_appearance()

            def on_click(_e: object) -> None:
                self._show_view(key)

            for w in (row, inner, lb, accent):
                w.bind("<Button-1>", on_click)
                w.bind("<Enter>", on_enter)
                w.bind("<Leave>", on_leave)

        nav_item("flow", "内容提取与分析")
        nav_item("local_chat", "对话")
        nav_item("api", "API 与模型")

        tk.Frame(main, bg=NAV_DIVIDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        work = ttk.Frame(main, padding=(inner, inner, inner, inner))
        work.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._top_status_bar = tk.Frame(work, bg=T_PAGE)
        self._top_status_bar.pack(fill=tk.X, pady=(0, 10))
        bar_card = tk.Frame(
            self._top_status_bar,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        bar_card.pack(fill=tk.X)
        bar = tk.Frame(bar_card, bg=T_PANEL)
        bar.pack(fill=tk.X, padx=14, pady=10)
        self.status = tk.Label(
            bar,
            text="就绪",
            anchor=tk.W,
            bg=T_PANEL,
            fg=T_MUTED,
            font=self._font_hint,
        )
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._infer_global_lbl = tk.Label(
            bar,
            text="",
            anchor=tk.E,
            bg=T_PANEL,
            fg=T_MUTED,
            font=self._font_hint,
            cursor="hand2",
        )
        self._infer_global_lbl.pack(side=tk.RIGHT, padx=(8, 0))
        self._infer_global_lbl.bind("<Button-1>", self._on_infer_global_chip_click)
        ttk.Button(
            bar,
            text="重新载入输出",
            command=self.reload_files,
            style=self._secondary_button_style,
        ).pack(side=tk.RIGHT, padx=(12, 0))

        self._progress_row = ttk.Frame(work)
        self._llm_progress_lbl = ttk.Label(
            self._progress_row, text="", anchor=tk.W, foreground=T_MUTED
        )
        self._llm_progress_lbl.pack(side=tk.LEFT, padx=(0, 10))
        self._llm_progress = ttk.Progressbar(
            self._progress_row,
            orient=tk.HORIZONTAL,
            mode="determinate",
            maximum=100,
            length=360,
        )
        self._llm_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._progress_row_visible = False

        self._content_host = ttk.Frame(work)
        self._content_host.pack(fill=tk.BOTH, expand=True)
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)

        self.frame_flow = ttk.Frame(self._content_host, padding=0)
        self.frame_local_chat = ttk.Frame(self._content_host, padding=0)
        self.frame_api = ttk.Frame(self._content_host, padding=0)

        for f in (self.frame_flow, self.frame_local_chat, self.frame_api):
            f.grid(row=0, column=0, sticky="nsew")

        self._views = {
            "flow": self.frame_flow,
            "local_chat": self.frame_local_chat,
            "api": self.frame_api,
        }

        self._log_text_widgets: list[tk.Text] = []
        self._merged_text_widgets: list[tk.Text] = []
        self._flow_nb: ttk.Notebook | None = None
        self._flow_tab_idx: dict[str, int] = {}

        self.url_var = tk.StringVar(value="")
        self.asr_if_no_subs_var = tk.BooleanVar(value=True)
        self.asr_force_var = tk.BooleanVar(value=False)
        self.download_bilibili_video_var = tk.BooleanVar(value=True)
        self.whisper_model_var = tk.StringVar(value=default_whisper_model_choice())
        _vpre = _load_llm_gui_prefs_merged()
        self.vision_enable_var = tk.BooleanVar(value=True)
        self.vision_frame_extract_var = tk.BooleanVar(value=True)
        self.vision_scene_detect_var = tk.BooleanVar(value=False)
        self.vision_phash_var = tk.BooleanVar(value=True)
        self.vision_phash_diff_var = tk.IntVar(value=6)
        self.vision_frame_iv_str = tk.StringVar(value="30")
        self.vision_ocr_var = tk.BooleanVar(value=True)
        self.vision_ocr_full_var = tk.BooleanVar(value=False)
        self.vision_ocr_2x_var = tk.BooleanVar(value=False)
        self.vision_ocr_gpu_var = tk.BooleanVar(value=True)
        self.vision_ocr_bottom_pct_var = tk.IntVar(value=18)
        self.vision_vlm_var = tk.BooleanVar(value=True)
        self.vision_vlm_base_var = tk.StringVar(
            value=_local_openai_base_for_ui(str(_vpre.get("chat_local_base_url") or ""))
        )
        _vdm = str(_vpre.get("chat_local_model", "") or "").strip()
        if not _vdm:
            _vdm = DEFAULT_LOCAL_OPENAI_MODEL_ID
        self.vision_vlm_model_var = tk.StringVar(value=_vdm)
        self.vision_vlm_key_var = tk.StringVar(
            value=str(_vpre.get("chat_local_api_key", "") or "").strip()
        )
        self.vision_vlm_precision_var = tk.StringVar(value="BF16")
        self.vision_video_type_var = tk.StringVar(value="auto")
        self.vision_view_mode_var = tk.StringVar(value="coarse")
        self.vision_out_md_var = tk.BooleanVar(value=True)
        self.vision_out_json_var = tk.BooleanVar(value=True)
        self.vision_out_srt_var = tk.BooleanVar(value=True)
        self.chat_vision_context_var = tk.StringVar(value="auto")
        self.flow_transcript_use_llm_var = tk.BooleanVar(value=True)
        self.flow_transcript_llm_source_var = tk.StringVar(value="api_preferred")
        self._transcript_llm_source_widgets: tuple[ttk.Radiobutton, ...] = ()
        self._asr_sym_boxes: list[tk.Label] = []

        self._active_session_out: Path | None = None

        self._build_api_settings_page(inner)

        self._local_chat_hub = LocalChatHub(self.frame_local_chat, self)
        self._local_chat_hub.pack(fill=tk.BOTH, expand=True)
        self._local_chat_hub._infer.attach_global_indicator(self._update_infer_global_chip)

        self._build_flow_page(inner)

        self._pipeline_proc_lock = threading.Lock()
        self._pipeline_active_procs: list[subprocess.Popen] = []
        self._pipeline_cancel_requested = threading.Event()
        self._busy = False
        self._apply_loaded_flow_and_vision_prefs()
        self._show_view("flow")
        self.reload_files()
        self._flow_url_entry.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        _register_infer_atexit(self)

    def _shutdown_before_destroy(self) -> None:
        """关窗口时：保存偏好、停流水线、结束「推理服务」子进程（含 Windows 整棵进程树）。"""
        try:
            self._save_flow_and_vision_prefs()
        except OSError:
            pass
        if getattr(self, "_busy", False):
            self._pipeline_cancel_requested.set()
            with self._pipeline_proc_lock:
                _snap = list(self._pipeline_active_procs)
            for _p in _snap:
                _terminate_process_tree(_p)
        try:
            hub = getattr(self, "_local_chat_hub", None)
            if hub is not None:
                infer = getattr(hub, "_infer", None)
                if infer is not None:
                    try:
                        infer.save_prefs()
                    except OSError:
                        pass
                    infer.terminate_child_proc()
        except (tk.TclError, OSError, AttributeError):
            pass

    def destroy(self) -> None:
        if getattr(self, "_app_destroy_started", False):
            try:
                super().destroy()
            except tk.TclError:
                pass
            return
        self._app_destroy_started = True
        try:
            self._shutdown_before_destroy()
        except Exception:
            pass
        try:
            super().destroy()
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        self.destroy()

    def _output_dir(self) -> Path:
        if self._active_session_out is not None and self._active_session_out.is_dir():
            return self._active_session_out.resolve()
        raw = os.environ.get("BILIBILI_VISION_OUT", "").strip()
        if raw:
            rp = Path(raw).expanduser().resolve()
            if rp.is_dir():
                return rp
        try:
            rel = _load_llm_gui_prefs_merged().get("last_output_session")
            if isinstance(rel, str) and rel.strip():
                p = (PROJECT_ROOT / rel.strip()).resolve()
                if p.is_dir():
                    return p
        except (OSError, TypeError, ValueError):
            pass
        return OUT_ROOT.resolve()

    def path_merged(self) -> Path:
        return self._output_dir() / "transcript_merged.txt"

    def path_analysis(self) -> Path:
        return self._output_dir() / "video_analysis.txt"

    def path_analysis_deep(self) -> Path:
        return self._output_dir() / "video_analysis_deep.txt"

    def path_analysis_deep_md(self) -> Path:
        return self._output_dir() / "video_analysis_deep.md"

    def path_analysis_deep_json(self) -> Path:
        return self._output_dir() / "video_analysis_deep.json"

    def path_vision_timeline_srt(self) -> Path:
        return self._output_dir() / "vision_outputs" / "video_analysis.srt"

    def path_vision_run_config(self) -> Path:
        return self._output_dir() / "vision_run_config.json"

    def _on_session_output_dir(self, p: Path, deep: bool) -> None:
        self._active_session_out = p.resolve()
        os.environ["BILIBILI_VISION_OUT"] = str(self._active_session_out)
        self._write_vision_run_config(deep=deep, out_dir=self._active_session_out)

    def _apply_loaded_flow_and_vision_prefs(self) -> None:
        data = _load_llm_gui_prefs_merged()
        fm = str(data.get("flow_mode_saved", "") or "").strip().lower()
        if fm in ("subtitle", "multimodal"):
            self.flow_mode_var.set(fm)
            self._sync_flow_mode_ui()
        tu = data.get("task_url_saved")
        if isinstance(tu, str) and tu.strip():
            self.url_var.set(tu.strip())
        wm = data.get("whisper_model_saved")
        if isinstance(wm, str) and wm in WHISPER_MODEL_CHOICES:
            self.whisper_model_var.set(wm)
        if "asr_if_no_subs_saved" in data:
            self.asr_if_no_subs_var.set(bool(data["asr_if_no_subs_saved"]))
        if "asr_force_saved" in data:
            self.asr_force_var.set(bool(data["asr_force_saved"]))
        if "download_bilibili_video_saved" in data:
            self.download_bilibili_video_var.set(bool(data["download_bilibili_video_saved"]))
        if "flow_transcript_use_llm_saved" in data:
            self.flow_transcript_use_llm_var.set(bool(data["flow_transcript_use_llm_saved"]))
        fts = str(data.get("flow_transcript_llm_source_saved", "") or "").strip().lower()
        if fts in ("api_preferred", "same_as_vlm"):
            self.flow_transcript_llm_source_var.set(fts)
        vb = data.get("vision_enable_saved")
        if vb is not None:
            self.vision_enable_var.set(bool(vb))
        for key, var in (
            ("vision_frame_extract_saved", self.vision_frame_extract_var),
            ("vision_scene_detect_saved", self.vision_scene_detect_var),
            ("vision_phash_saved", self.vision_phash_var),
            ("vision_ocr_saved", self.vision_ocr_var),
            ("vision_ocr_full_saved", self.vision_ocr_full_var),
            ("vision_ocr_2x_saved", self.vision_ocr_2x_var),
            ("vision_ocr_gpu_saved", self.vision_ocr_gpu_var),
            ("vision_vlm_saved", self.vision_vlm_var),
            ("vision_out_md_saved", self.vision_out_md_var),
            ("vision_out_json_saved", self.vision_out_json_var),
            ("vision_out_srt_saved", self.vision_out_srt_var),
        ):
            if key in data:
                var.set(bool(data[key]))
        if isinstance(data.get("vision_phash_diff_saved"), int):
            self.vision_phash_diff_var.set(
                max(0, min(20, int(data["vision_phash_diff_saved"])))
            )
        if isinstance(data.get("vision_ocr_bottom_pct_saved"), int):
            self.vision_ocr_bottom_pct_var.set(
                max(5, min(50, int(data["vision_ocr_bottom_pct_saved"])))
            )
        iv = data.get("vision_frame_iv_saved")
        if isinstance(iv, str) and iv.strip():
            self.vision_frame_iv_str.set(iv.strip())
        vvm = str(data.get("vision_view_mode_saved", "") or "").strip().lower()
        if vvm in ("coarse", "fine"):
            self.vision_view_mode_var.set(vvm)
        for attr, prefk in (
            (self.vision_vlm_base_var, "vision_vlm_base_saved"),
            (self.vision_vlm_model_var, "vision_vlm_model_saved"),
            (self.vision_vlm_key_var, "vision_vlm_key_saved"),
            (self.vision_vlm_precision_var, "vision_vlm_precision_saved"),
            (self.vision_video_type_var, "vision_video_type_saved"),
            (self.chat_vision_context_var, "chat_vision_context_saved"),
        ):
            v = data.get(prefk)
            if isinstance(v, str):
                attr.set(v)
        rel = data.get("last_output_session")
        if isinstance(rel, str) and rel.strip():
            p = (PROJECT_ROOT / rel.strip()).resolve()
            if p.is_dir():
                self._active_session_out = p
                # 勿写入 os.environ：会与「新任务应新建会话目录」冲突（见 bilibili_pipeline extract 默认行为）。
        try:
            self._sync_asr_symbol()
        except (tk.TclError, AttributeError):
            pass
        try:
            self._sync_transcript_llm_source_widgets_state()
        except (tk.TclError, AttributeError):
            pass

    def _save_flow_and_vision_prefs(self) -> None:
        merged = _load_llm_gui_prefs_merged()
        merged["flow_mode_saved"] = self.flow_mode_var.get()
        merged["task_url_saved"] = self.url_var.get().strip()
        merged["whisper_model_saved"] = self.whisper_model_var.get().strip()
        merged["asr_if_no_subs_saved"] = bool(self.asr_if_no_subs_var.get())
        merged["asr_force_saved"] = bool(self.asr_force_var.get())
        merged["download_bilibili_video_saved"] = bool(
            self.download_bilibili_video_var.get()
        )
        merged["flow_transcript_use_llm_saved"] = bool(self.flow_transcript_use_llm_var.get())
        merged["flow_transcript_llm_source_saved"] = self.flow_transcript_llm_source_var.get().strip()
        merged["vision_enable_saved"] = bool(self.vision_enable_var.get())
        merged["vision_frame_extract_saved"] = bool(self.vision_frame_extract_var.get())
        merged["vision_scene_detect_saved"] = bool(self.vision_scene_detect_var.get())
        merged["vision_phash_saved"] = bool(self.vision_phash_var.get())
        merged["vision_phash_diff_saved"] = int(self.vision_phash_diff_var.get())
        merged["vision_frame_iv_saved"] = self.vision_frame_iv_str.get().strip()
        merged["vision_ocr_saved"] = bool(self.vision_ocr_var.get())
        merged["vision_ocr_full_saved"] = bool(self.vision_ocr_full_var.get())
        merged["vision_ocr_2x_saved"] = bool(self.vision_ocr_2x_var.get())
        merged["vision_ocr_gpu_saved"] = bool(self.vision_ocr_gpu_var.get())
        merged["vision_ocr_bottom_pct_saved"] = int(self.vision_ocr_bottom_pct_var.get())
        merged["vision_vlm_saved"] = bool(self.vision_vlm_var.get())
        merged["vision_vlm_base_saved"] = self.vision_vlm_base_var.get().strip()
        merged["vision_vlm_model_saved"] = self.vision_vlm_model_var.get().strip()
        merged["vision_vlm_key_saved"] = self.vision_vlm_key_var.get().strip()
        merged["vision_vlm_precision_saved"] = self.vision_vlm_precision_var.get().strip()
        merged["vision_video_type_saved"] = self.vision_video_type_var.get().strip()
        merged["vision_view_mode_saved"] = self.vision_view_mode_var.get().strip()
        merged["vision_out_md_saved"] = bool(self.vision_out_md_var.get())
        merged["vision_out_json_saved"] = bool(self.vision_out_json_var.get())
        merged["vision_out_srt_saved"] = bool(self.vision_out_srt_var.get())
        merged["chat_vision_context_saved"] = self.chat_vision_context_var.get().strip()
        if self._active_session_out is not None and self._active_session_out.is_dir():
            try:
                merged["last_output_session"] = str(
                    self._active_session_out.resolve().relative_to(PROJECT_ROOT)
                )
            except ValueError:
                merged["last_output_session"] = str(self._active_session_out.resolve())
        try:
            LLM_GUI_PREF_JSON.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _init_llm_settings_vars(self) -> None:
        g = os.environ.get
        pref = _load_gui_llm_provider_pref()
        envp = _norm_llm_provider(g("LLM_PROVIDER", ""))
        self.llm_provider_var = tk.StringVar(value=(pref or envp or "auto"))
        self._llm_gemini_key = tk.StringVar(value=g("GEMINI_API_KEY", ""))
        self._llm_openai_key = tk.StringVar(value=g("OPENAI_API_KEY", ""))
        self._llm_groq_key = tk.StringVar(value=g("GROQ_API_KEY", ""))
        self._llm_anthropic_key = tk.StringVar(value=g("ANTHROPIC_API_KEY", ""))
        self._llm_xai_key = tk.StringVar(value=g("XAI_API_KEY", ""))
        self._llm_gemini_model = tk.StringVar(value=g("GEMINI_MODEL", ""))
        self._llm_openai_model = tk.StringVar(value=g("OPENAI_MODEL", ""))
        self._llm_openai_base = tk.StringVar(
            value=_maybe_rewrite_lm_studio_local_url(g("OPENAI_BASE_URL", ""))
        )
        self._llm_groq_model = tk.StringVar(value=g("GROQ_MODEL", ""))
        self._llm_anthropic_model = tk.StringVar(value=g("ANTHROPIC_MODEL", ""))
        self._llm_xai_model = tk.StringVar(value=g("XAI_MODEL", ""))
        jp = _load_llm_gui_prefs_merged()
        cr = str(jp.get("chat_route", "follow") or "follow").strip().lower()
        if cr not in ("follow", "local"):
            cr = "follow"
        self.chat_route_var = tk.StringVar(value=cr)
        self._chat_local_base = tk.StringVar(
            value=_local_openai_base_for_ui(str(jp.get("chat_local_base_url") or ""))
        )
        _clm = str(jp.get("chat_local_model", "") or "").strip()
        if not _clm:
            _clm = DEFAULT_LOCAL_OPENAI_MODEL_ID
        self._chat_local_model = tk.StringVar(value=_clm)
        self._chat_local_key = tk.StringVar(value=str(jp.get("chat_local_api_key", "") or "").strip())
        self.report_chat_attach_vision_var = tk.BooleanVar(value=False)

    def _sync_llm_env_from_form(self) -> None:
        def push(name: str, val: str) -> None:
            v = val.strip()
            if v:
                os.environ[name] = v
            else:
                os.environ.pop(name, None)

        push("GEMINI_API_KEY", self._llm_gemini_key.get())
        push("OPENAI_API_KEY", self._llm_openai_key.get())
        push("GROQ_API_KEY", self._llm_groq_key.get())
        push("ANTHROPIC_API_KEY", self._llm_anthropic_key.get())
        push("XAI_API_KEY", self._llm_xai_key.get())
        push("GEMINI_MODEL", self._llm_gemini_model.get())
        push("OPENAI_MODEL", self._llm_openai_model.get())
        push("OPENAI_BASE_URL", self._llm_openai_base.get())
        push("GROQ_MODEL", self._llm_groq_model.get())
        push("ANTHROPIC_MODEL", self._llm_anthropic_model.get())
        push("XAI_MODEL", self._llm_xai_model.get())
        os.environ["LLM_PROVIDER"] = _norm_llm_provider(self.llm_provider_var.get())

    def _overlay_vision_vlm_as_openai_env(self) -> None:
        """将 ③ VLM 的 base/model/key 写入 OPENAI_*，供 analyze_transcript --llm-provider local。"""
        base = _normalize_openai_v1_base(self.vision_vlm_base_var.get().strip())
        model = self.vision_vlm_model_var.get().strip()
        base = _fix_gemma_misconfigured_port8090(base, model)
        key = self.vision_vlm_key_var.get().strip()
        if base and model:
            os.environ["OPENAI_BASE_URL"] = base.rstrip("/")
            os.environ["OPENAI_MODEL"] = model
            os.environ["OPENAI_API_KEY"] = key if key else "EMPTY"
        else:
            # 避免沿用「本地对话」刚写入的 OPENAI_*（常见 8090），否则 --llm-provider local 会误连错误端口。
            for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"):
                os.environ.pop(name, None)

    def _ensure_pipeline_local_openai_env(self) -> None:
        """
        流水线里 analyze_transcript --llm-provider local 时，必须指向真实本机 serve。
        优先 ③ 画面管线 URL；否则「本地对话」顶栏；再否则项目默认 18090。
        避免仅因 API 与模型页填过 8090 就误连空端口。
        """
        base = _normalize_openai_v1_base(self.vision_vlm_base_var.get().strip())
        model = (self.vision_vlm_model_var.get() or "").strip()
        base = _fix_gemma_misconfigured_port8090(base, model)
        key = (self.vision_vlm_key_var.get() or "").strip()
        if base and model:
            os.environ["OPENAI_BASE_URL"] = base.rstrip("/")
            os.environ["OPENAI_MODEL"] = model
            os.environ["OPENAI_API_KEY"] = key if key else "EMPTY"
            return
        cb = self._chat_local_base.get().strip()
        cm = (self._chat_local_model.get() or "").strip()
        ck = (self._chat_local_key.get() or "").strip()
        if cb and cm:
            cb2 = _fix_gemma_misconfigured_port8090(_normalize_openai_v1_base(cb), cm)
            os.environ["OPENAI_BASE_URL"] = cb2.rstrip("/")
            os.environ["OPENAI_MODEL"] = cm
            os.environ["OPENAI_API_KEY"] = ck if ck else "EMPTY"
            return
        os.environ["OPENAI_BASE_URL"] = DEFAULT_LOCAL_OPENAI_BASE.rstrip("/")
        os.environ["OPENAI_MODEL"] = DEFAULT_LOCAL_OPENAI_MODEL_ID
        os.environ["OPENAI_API_KEY"] = "EMPTY"

    def _restore_openai_env_from_api_form(self) -> None:
        """仅恢复「API 与模型」页中的 OpenAI 兼容三项（对话选本地时会覆盖全局 OPENAI_*）。"""
        def push(name: str, val: str) -> None:
            v = val.strip()
            if v:
                os.environ[name] = v
            else:
                os.environ.pop(name, None)

        push("OPENAI_API_KEY", self._llm_openai_key.get())
        push("OPENAI_BASE_URL", self._llm_openai_base.get())
        push("OPENAI_MODEL", self._llm_openai_model.get())

    def _sync_transcript_llm_source_widgets_state(self) -> None:
        use = bool(self.flow_transcript_use_llm_var.get())
        st = tk.NORMAL if use else tk.DISABLED
        for w in self._transcript_llm_source_widgets:
            try:
                w.configure(state=st)
            except tk.TclError:
                pass

    def prepare_chat_env_for_dialogue(self) -> None:
        """先恢复各平台环境变量，再按「对话模型」覆盖 OpenAI 兼容参数（若选本地）。"""
        self._sync_llm_env_from_form()
        if self.chat_route_var.get() == "local":
            base = self._chat_local_base.get().strip()
            model = self._chat_local_model.get().strip()
            if base and model:
                os.environ["OPENAI_BASE_URL"] = base.rstrip("/")
                key = self._chat_local_key.get().strip()
                os.environ["OPENAI_API_KEY"] = key if key else "EMPTY"
                os.environ["OPENAI_MODEL"] = model

    def prepare_env_for_hub_api_chat(self) -> None:
        """「对话」页选云端时：同步各 Key，并恢复「API 与模型」里的 OpenAI 兼容三项（不被报告页本地路由覆盖）。"""
        self._sync_llm_env_from_form()
        self._restore_openai_env_from_api_form()

    def resolve_chat_provider_for_dialogue(self):
        if self.chat_route_var.get() == "local":
            base = self._chat_local_base.get().strip()
            model = self._chat_local_model.get().strip()
            if base and model:
                return resolve_provider("openai")
            return None
        return resolve_provider(self.llm_provider_var.get().strip())

    def _save_llm_keys_to_local_file(self) -> None:
        path = PROJECT_ROOT / "local_api_keys.py"
        pairs: list[tuple[str, str]] = []
        for name, var in (
            ("GEMINI_API_KEY", self._llm_gemini_key),
            ("OPENAI_API_KEY", self._llm_openai_key),
            ("GROQ_API_KEY", self._llm_groq_key),
            ("ANTHROPIC_API_KEY", self._llm_anthropic_key),
            ("XAI_API_KEY", self._llm_xai_key),
            ("GEMINI_MODEL", self._llm_gemini_model),
            ("OPENAI_MODEL", self._llm_openai_model),
            ("OPENAI_BASE_URL", self._llm_openai_base),
            ("GROQ_MODEL", self._llm_groq_model),
            ("ANTHROPIC_MODEL", self._llm_anthropic_model),
            ("XAI_MODEL", self._llm_xai_model),
        ):
            v = var.get().strip()
            if v:
                pairs.append((name, v))
        pairs.append(("LLM_PROVIDER", _norm_llm_provider(self.llm_provider_var.get())))
        lines = [
            "# -*- coding: utf-8 -*-\n",
            "# 勿将本文件提交到公开仓库或分享 zip。\n\n",
        ]
        for name, v in pairs:
            lines.append(f"{name} = {repr(v)}\n")
        if len(lines) <= 3:
            lines.append("# （当前无已填项；可在上方 GUI 填写后保存）\n")
        path.write_text("".join(lines), encoding="utf-8")

    def _browse_media(self) -> None:
        media_glob = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_LOCAL_MEDIA_SUFFIXES))
        path = filedialog.askopenfilename(
            parent=self,
            title="选择音视频文件",
            filetypes=[
                ("音视频", media_glob),
                ("全部文件", "*.*"),
            ],
        )
        if path:
            self.url_var.set(path)

    def rebuild_report_chat_panel(self) -> None:
        """重建「报告与对话」标签内下半区的对话面板（API 变更后调用）。"""
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        old = getattr(self, "flow_agent_panel", None)
        if old is not None:
            self.agent_session.detach(old)
            try:
                old.destroy()
            except tk.TclError:
                pass
            self.flow_agent_panel = None
        par = getattr(self, "_flow_chat_parent", None)
        if par is not None:
            self.flow_agent_panel = AgentChatPanel(
                par, self, self.agent_session, compact=True
            )
            self.flow_agent_panel.pack(fill=tk.BOTH, expand=True)

    def _apply_llm_clicked(self, *, save_file: bool) -> None:
        self._sync_llm_env_from_form()
        _save_gui_llm_provider_pref(self.llm_provider_var.get())
        _save_llm_gui_chat_prefs(
            chat_route=self.chat_route_var.get(),
            chat_local_base=self._chat_local_base.get(),
            chat_local_model=self._chat_local_model.get(),
            chat_local_key=self._chat_local_key.get(),
        )
        self.agent_session.clear()
        self.prepare_chat_env_for_dialogue()
        self.agent_session._provider = self.resolve_chat_provider_for_dialogue()
        self.rebuild_report_chat_panel()
        if save_file:
            try:
                self._save_llm_keys_to_local_file()
            except OSError as e:
                messagebox.showerror("保存失败", str(e))
                return
        messagebox.showinfo(
            "已应用",
            "已写入环境变量并刷新「报告与对话」标签中的对话区。"
            + (" 已保存到 local_api_keys.py。" if save_file else ""),
        )

    def _build_api_settings_page(self, pad: int) -> None:
        outer = ttk.Frame(self.frame_api, padding=pad)
        outer.pack(fill=tk.BOTH, expand=True)

        # 底部按钮先占位，避免窗口较矮时滚到视口外；中间区域单独滚动
        btns = tk.Frame(outer, bg=T_PAGE)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        sec_style = getattr(self, "_secondary_button_style", "TButton")
        ttk.Button(
            btns,
            text="应用",
            command=lambda: self._apply_llm_clicked(save_file=False),
            style=sec_style,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(
            btns,
            text="应用并保存 local_api_keys.py",
            command=lambda: self._apply_llm_clicked(save_file=True),
            style=self._run_button_style,
        ).pack(side=tk.LEFT)
        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(
            side=tk.BOTTOM, fill=tk.X, pady=(0, 10)
        )

        scroll_host = ttk.Frame(outer)
        scroll_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scroll_host.grid_rowconfigure(0, weight=1)
        scroll_host.grid_columnconfigure(0, weight=1)

        y_inc = max(18, int(round(self._font_content[1] * 2.4)))
        api_canvas = tk.Canvas(
            scroll_host,
            highlightthickness=0,
            borderwidth=0,
            bg=T_PAGE,
            yscrollincrement=y_inc,
        )
        api_vsb = tk.Scrollbar(
            scroll_host,
            orient=tk.VERTICAL,
            command=api_canvas.yview,
            width=self._scrollbar_px,
            borderwidth=0,
            troughcolor=SCROLL_TROUGH,
            bg="#c4c4c4",
            activebackground="#a8a8a8",
            highlightthickness=0,
            relief="flat",
            jump=1,
        )
        api_canvas.configure(yscrollcommand=api_vsb.set)
        api_canvas.grid(row=0, column=0, sticky="nsew")
        api_vsb.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(api_canvas, bg=T_PAGE)
        _api_inner_win = api_canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        def _api_canvas_on_cfg(event: tk.Event) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 1:
                api_canvas.itemconfigure(_api_inner_win, width=w)

        def _api_inner_on_cfg(_event: object | None = None) -> None:
            api_canvas.configure(scrollregion=api_canvas.bbox("all"))

        api_canvas.bind("<Configure>", _api_canvas_on_cfg)
        inner.bind("<Configure>", lambda _e: _api_inner_on_cfg())

        def _api_wheel(event: tk.Event) -> str | None:
            lines = self._text_wheel_lines
            d = getattr(event, "delta", 0) or 0
            if d:
                if sys.platform == "win32":
                    steps = int(-d * lines / 120.0)
                    if steps == 0:
                        steps = -lines if d > 0 else lines
                else:
                    steps = int(-d * lines / 120.0)
                    if steps == 0:
                        steps = -lines if d > 0 else lines
                if steps:
                    api_canvas.yview_scroll(steps, "units")
                return "break"
            n = getattr(event, "num", 0)
            if n == 4:
                api_canvas.yview_scroll(-lines, "units")
                return "break"
            if n == 5:
                api_canvas.yview_scroll(lines, "units")
                return "break"
            return None

        def _api_bind_wheel(w: tk.Misc) -> None:
            w.bind("<MouseWheel>", _api_wheel, add="+")
            w.bind("<Button-4>", _api_wheel, add="+")
            w.bind("<Button-5>", _api_wheel, add="+")
            for c in w.winfo_children():
                _api_bind_wheel(c)

        hint_wrap = int(680 * self._geom_scale)
        hint_small = self._font_hint

        head = tk.Frame(inner, bg=T_PAGE)
        head.pack(fill=tk.X, pady=(0, 2))
        tk.Label(
            head,
            text="API 与模型",
            font=self._font_title,
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(anchor=tk.W)

        prov_shell = tk.Frame(inner, bg=T_PAGE)
        prov_shell.pack(fill=tk.X, pady=(0, 12))
        prov_card = tk.Frame(
            prov_shell,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        prov_card.pack(fill=tk.X)
        prov_inner = tk.Frame(prov_card, bg=T_PANEL)
        prov_inner.pack(fill=tk.X, padx=18, pady=16)
        tk.Label(
            prov_inner,
            text="首选提供商",
            font=self._font_title,
            bg=T_PANEL,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        prov_row = tk.Frame(prov_inner, bg=T_PANEL)
        prov_row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(
            prov_row,
            text="生成总结使用",
            bg=T_PANEL,
            fg=T_TEXT,
            font=self._font_ui,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Combobox(
            prov_row,
            textvariable=self.llm_provider_var,
            values=LLM_PROVIDER_COMBO_VALUES,
            state="readonly",
            width=20,
        ).pack(side=tk.LEFT)

        chat_shell = tk.Frame(inner, bg=T_PAGE)
        chat_shell.pack(fill=tk.X, pady=(0, 12))
        chat_card = tk.Frame(
            chat_shell,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        chat_card.pack(fill=tk.X)
        chat_inner = tk.Frame(chat_card, bg=T_PANEL)
        chat_inner.pack(fill=tk.X, padx=18, pady=16)
        tk.Label(
            chat_inner,
            text="对话模型",
            font=self._font_title,
            bg=T_PANEL,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        chat_mode_row = tk.Frame(chat_inner, bg=T_PANEL)
        chat_mode_row.pack(fill=tk.X, pady=(10, 8))
        ttk.Radiobutton(
            chat_mode_row,
            text="同首选提供商",
            variable=self.chat_route_var,
            value="follow",
        ).pack(side=tk.LEFT, padx=(0, 14))
        ttk.Radiobutton(
            chat_mode_row,
            text="本地 OpenAI 兼容",
            variable=self.chat_route_var,
            value="local",
        ).pack(side=tk.LEFT)
        cr1 = tk.Frame(chat_inner, bg=T_PANEL)
        cr1.pack(fill=tk.X, pady=(4, 0))
        cr1.grid_columnconfigure(1, weight=1)
        tk.Label(cr1, text="根 URL", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12), pady=(0, 6)
        )
        ttk.Entry(cr1, textvariable=self._chat_local_base, width=52).grid(
            row=0, column=1, sticky="ew", pady=(0, 6)
        )
        cr2 = tk.Frame(chat_inner, bg=T_PANEL)
        cr2.pack(fill=tk.X, pady=(10, 0))
        cr2.grid_columnconfigure(1, weight=1)
        tk.Label(cr2, text="模型 ID / 路径", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12)
        )
        ttk.Entry(cr2, textvariable=self._chat_local_model).grid(row=0, column=1, sticky="ew")
        cr3 = tk.Frame(chat_inner, bg=T_PANEL)
        cr3.pack(fill=tk.X, pady=(10, 0))
        cr3.grid_columnconfigure(1, weight=1)
        tk.Label(cr3, text="API Key（可选）", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12)
        )
        ttk.Entry(cr3, textvariable=self._chat_local_key, show="*").grid(
            row=0, column=1, sticky="ew"
        )

        sec = tk.Frame(inner, bg=T_PAGE)
        sec.pack(fill=tk.X, pady=(8, 12))
        tk.Label(
            sec,
            text="各平台凭据",
            font=self._font_title,
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(side=tk.LEFT)

        model_entry_w = 30

        def provider_block(
            parent: tk.Misc,
            title: str,
            key_var: tk.StringVar,
            model_var: tk.StringVar,
            model_hint: str,
            extra_var: tk.StringVar | None = None,
            extra_lbl: str = "",
        ) -> None:
            shell = tk.Frame(parent, bg=T_PAGE)
            shell.pack(fill=tk.X, pady=(0, 12))
            card = tk.Frame(
                shell,
                bg=T_PANEL,
                highlightbackground=T_BORDER,
                highlightthickness=1,
            )
            card.pack(fill=tk.X)
            inner = tk.Frame(card, bg=T_PANEL)
            inner.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
            tk.Label(
                inner,
                text=title,
                font=self._font_title,
                bg=T_PANEL,
                fg=T_ACCENT,
                anchor=tk.W,
            ).pack(fill=tk.X)
            row = tk.Frame(inner, bg=T_PANEL)
            row.pack(fill=tk.X, pady=(12, 0))
            row.grid_columnconfigure(1, weight=1)
            tk.Label(
                row,
                text="API Key",
                bg=T_PANEL,
                fg=T_TEXT,
                font=self._font_ui,
            ).grid(row=0, column=0, sticky=tk.W, padx=(0, 12), pady=2)
            ttk.Entry(row, textvariable=key_var, show="•").grid(
                row=0, column=1, sticky=tk.EW, padx=(0, 20), pady=2
            )
            tk.Label(
                row,
                text="模型 ID",
                bg=T_PANEL,
                fg=T_TEXT,
                font=self._font_ui,
            ).grid(row=0, column=2, sticky=tk.W, padx=(0, 10), pady=2)
            ttk.Entry(row, textvariable=model_var, width=model_entry_w).grid(
                row=0, column=3, sticky=tk.W, pady=2
            )
            if model_hint:
                tk.Label(
                    inner,
                    text=model_hint,
                    bg=T_PANEL,
                    fg=API_SECTION_MUTED,
                    font=hint_small,
                    wraplength=max(280, hint_wrap - 48),
                    justify=tk.LEFT,
                ).pack(anchor=tk.W, pady=(8, 0))
            if extra_var is not None and extra_lbl:
                tk.Label(
                    inner,
                    text=extra_lbl,
                    bg=T_PANEL,
                    fg=T_TEXT,
                    font=self._font_ui,
                ).pack(anchor=tk.W, pady=(14, 6))
                ttk.Entry(inner, textvariable=extra_var).pack(fill=tk.X)

        provider_block(
            inner,
            "Google Gemini",
            self._llm_gemini_key,
            self._llm_gemini_model,
            "",
        )
        provider_block(
            inner,
            "OpenAI（GPT）",
            self._llm_openai_key,
            self._llm_openai_model,
            "",
            self._llm_openai_base,
            "API 根 URL（可选）",
        )
        provider_block(
            inner,
            "Groq",
            self._llm_groq_key,
            self._llm_groq_model,
            "",
        )
        provider_block(
            inner,
            "Anthropic Claude",
            self._llm_anthropic_key,
            self._llm_anthropic_model,
            "",
        )
        provider_block(
            inner,
            "xAI Grok",
            self._llm_xai_key,
            self._llm_xai_model,
            "",
        )

        _api_bind_wheel(inner)
        api_canvas.bind("<MouseWheel>", _api_wheel, add="+")
        api_canvas.bind("<Button-4>", _api_wheel, add="+")
        api_canvas.bind("<Button-5>", _api_wheel, add="+")

    def _focus_workspace_tab(self, tab: str) -> None:
        """tab: run | report | merged | log | chat（后四者映射到合并后的标签页）"""
        nb = self._flow_nb
        if nb is None:
            return
        i = self._flow_tab_idx.get(tab)
        if i is None:
            return
        try:
            nb.select(i)
        except tk.TclError:
            pass

    def _start_run_from_flow_mode(self) -> None:
        deep = self.flow_mode_var.get() == "multimodal"
        self.start_run(deep_analysis=deep)

    def _flow_info_tooltip_text(self) -> str:
        if self.flow_mode_var.get() == "multimodal":
            return FLOW_HELP_MULTIMODAL
        return FLOW_HELP_SUBTITLE

    def _sync_flow_mode_ui(self, *_args: object) -> None:
        multimodal = self.flow_mode_var.get() == "multimodal"
        if multimodal:
            try:
                self._flow_multimodal_scroll.pack(
                    fill=tk.BOTH, expand=True, pady=(8, 0)
                )
            except tk.TclError:
                pass
            self._flow_run_btn.configure(
                text="开始：提取 + 深度 + 多模态（可选）"
            )
            self._flow_title_lbl.configure(text="多模态：深度 + 可选画面")
            self._flow_info_box.configure(
                bg=FLOW_FULL_INFO_BG, highlightbackground=FLOW_FULL_INFO_BORDER
            )
            self._flow_info_lbl.configure(
                text="提示：B 站会下载视频到 out；画面任务需本机服务且与「③」URL 一致。悬停 ? 看详情。",
                bg=FLOW_FULL_INFO_BG,
            )
            try:
                self._flow_info_inner.configure(bg=FLOW_FULL_INFO_BG)
                self._flow_info_tip_lbl.configure(bg=FLOW_FULL_INFO_BG, fg=FLOW_FULL_MUTED)
            except (tk.TclError, AttributeError):
                pass
        else:
            try:
                self._flow_multimodal_scroll.pack_forget()
            except tk.TclError:
                pass
            self._flow_run_btn.configure(text="开始：提取并总结")
            self._flow_title_lbl.configure(text="提取 → 总结")
            self._flow_info_box.configure(
                bg=FLOW_SUB_INFO_BG, highlightbackground=FLOW_SUB_INFO_BORDER
            )
            self._flow_info_lbl.configure(
                text="提示：仅基础总结；深度/画面请切换「多模态」。悬停 ? 看详情。",
                bg=FLOW_SUB_INFO_BG,
            )
            try:
                self._flow_info_inner.configure(bg=FLOW_SUB_INFO_BG)
                self._flow_info_tip_lbl.configure(bg=FLOW_SUB_INFO_BG, fg=FLOW_SUB_MUTED)
            except (tk.TclError, AttributeError):
                pass

    def _write_vision_run_config(
        self, *, deep: bool, out_dir: Path | None = None
    ) -> None:
        if not deep:
            return
        target = out_dir if out_dir is not None else self._output_dir()
        try:
            interval = float(self.vision_frame_iv_str.get().strip().replace(",", "."))
        except (ValueError, tk.TclError):
            interval = 1.0
        interval = max(0.25, min(120.0, interval))
        view_mode = self.vision_view_mode_var.get().strip().lower()
        if view_mode not in ("coarse", "fine"):
            view_mode = "coarse"
        if view_mode == "fine":
            interval_eff = min(interval, 1.0)
            max_tl = 96
            tl_ceiling = 120
            seg_sec = 120.0
            max_seg = 24
            tr_max = 7200
            ocr_lines = 24
            ocr_chars = 3200
            vis_lines = 18
            blk_cap = 14000
        else:
            interval_eff = interval
            max_tl = 20
            tl_ceiling = 48
            seg_sec = 180.0
            max_seg = 16
            tr_max = 5200
            ocr_lines = 14
            ocr_chars = 2400
            vis_lines = 10
            blk_cap = 11000
        try:
            phd = int(self.vision_phash_diff_var.get())
        except (ValueError, tk.TclError):
            phd = 6
        phd = max(0, min(20, phd))
        try:
            pct = int(self.vision_ocr_bottom_pct_var.get())
        except (ValueError, tk.TclError):
            pct = 18
        pct = max(5, min(50, pct))
        vt = self.vision_video_type_var.get().strip().lower()
        if vt not in ("lecture", "tutorial", "vlog", "gaming", "auto"):
            vt = "auto"
        ctx = self.chat_vision_context_var.get().strip().lower()
        if ctx not in ("auto", "manual"):
            ctx = "auto"
        _vlm_model_for_cfg = self.vision_vlm_model_var.get().strip()
        _vlm_base_for_cfg = _fix_gemma_misconfigured_port8090(
            _normalize_openai_v1_base(self.vision_vlm_base_var.get().strip()),
            _vlm_model_for_cfg,
        )
        cfg = {
            "enabled": bool(self.vision_enable_var.get()),
            "source": self.url_var.get().strip(),
            "video_type": vt,
            "view_mode": view_mode,
            "frame_extract": bool(self.vision_frame_extract_var.get()),
            "frame_interval_sec": interval_eff,
            "frame_interval_ui_sec": interval,
            "scene_detect": bool(self.vision_scene_detect_var.get()),
            "phash_dedup": bool(self.vision_phash_var.get()),
            "phash_max_diff": phd,
            "ocr_enable": bool(self.vision_ocr_var.get()),
            "ocr_full_frame": bool(self.vision_ocr_full_var.get()),
            "ocr_scale_2x": bool(self.vision_ocr_2x_var.get()),
            "ocr_use_gpu": bool(self.vision_ocr_gpu_var.get()),
            "ocr_bottom_crop_pct": pct,
            "vlm_enable": bool(self.vision_vlm_var.get()),
            "vlm_provider": "openai_compatible",
            "vlm_base_url": _vlm_base_for_cfg,
            "vlm_model": _vlm_model_for_cfg,
            "vlm_api_key": self.vision_vlm_key_var.get().strip(),
            "vlm_compute": self.vision_vlm_precision_var.get().strip(),
            "output_md": bool(self.vision_out_md_var.get()),
            "output_json": bool(self.vision_out_json_var.get()),
            "output_srt": bool(self.vision_out_srt_var.get()),
            "chat_context_mode": ctx,
            "max_timeline_frames": max_tl,
            "max_timeline_frame_ceiling": tl_ceiling,
            "hierarchical_deep": True,
            "segment_sec": seg_sec,
            "max_segments": max_seg,
            "transcript_segment_max_chars": tr_max,
            "ocr_max_lines": ocr_lines,
            "ocr_max_chars": ocr_chars,
            "vision_max_lines": vis_lines,
            "segment_block_hard_cap": blk_cap,
        }
        self._apply_online_transcript_to_vision_cfg(cfg)
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / "vision_run_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _apply_online_transcript_to_vision_cfg(self, cfg: dict) -> None:
        """
        文稿选「API 首选」且指定具体在线提供方时，画面管线 VLM 与分层深度沿用同一平台
        （覆盖「③」中的 URL/模型/Key）。auto / local / 未填 Key 时不覆盖。
        """
        if not bool(self.flow_transcript_use_llm_var.get()):
            return
        if self.flow_transcript_llm_source_var.get().strip() != "api_preferred":
            return
        prov = _norm_llm_provider(self.llm_provider_var.get())
        if prov in ("auto", "local"):
            return
        if prov == "gemini":
            k = self._llm_gemini_key.get().strip()
            if not k:
                return
            m = self._llm_gemini_model.get().strip() or "gemini-2.0-flash"
            cfg["vlm_provider"] = "gemini"
            cfg["vlm_base_url"] = ""
            cfg["vlm_model"] = m
            cfg["vlm_api_key"] = k
            return
        if prov == "anthropic":
            k = self._llm_anthropic_key.get().strip()
            if not k:
                return
            m = self._llm_anthropic_model.get().strip() or "claude-3-5-sonnet-20241022"
            cfg["vlm_provider"] = "anthropic"
            cfg["vlm_base_url"] = ""
            cfg["vlm_model"] = m
            cfg["vlm_api_key"] = k
            return
        if prov == "openai":
            k = self._llm_openai_key.get().strip()
            if not k or k.upper() == "EMPTY":
                return
            base = _normalize_openai_v1_base(self._llm_openai_base.get().strip())
            if not base:
                return
            m = self._llm_openai_model.get().strip() or "gpt-4o-mini"
            cfg["vlm_provider"] = "openai_compatible"
            cfg["vlm_base_url"] = base.rstrip("/")
            cfg["vlm_model"] = m
            cfg["vlm_api_key"] = k
            return
        if prov == "xai":
            k = self._llm_xai_key.get().strip()
            if not k:
                return
            m = self._llm_xai_model.get().strip() or "grok-2-vision-latest"
            cfg["vlm_provider"] = "openai_compatible"
            cfg["vlm_base_url"] = "https://api.x.ai/v1"
            cfg["vlm_model"] = m
            cfg["vlm_api_key"] = k
            return
        if prov == "groq":
            k = self._llm_groq_key.get().strip()
            if not k:
                return
            m = self._llm_groq_model.get().strip() or "llama-3.2-11b-vision-preview"
            cfg["vlm_provider"] = "openai_compatible"
            cfg["vlm_base_url"] = "https://api.groq.com/openai/v1"
            cfg["vlm_model"] = m
            cfg["vlm_api_key"] = k

    def _build_flow_page(self, pad: int) -> None:
        """统一流程：分析模式切换 + 流程 / 报告与对话 / 合并与日志。"""
        edge = max(14, pad + 6)
        hint_small = self._font_hint
        sec_style = getattr(self, "_secondary_button_style", "TButton")

        self.flow_mode_var = tk.StringVar(value="subtitle")

        shell = ttk.Frame(self.frame_flow, padding=0)
        shell.pack(fill=tk.BOTH, expand=True)
        nb = ttk.Notebook(shell, style="Sticky.TNotebook")
        nb.pack(fill=tk.BOTH, expand=True)
        self._flow_nb = nb
        self._flow_tab_idx = {
            "run": 0,
            "report": 1,
            "chat": 1,
            "merged": 2,
            "log": 2,
        }

        tab_run = ttk.Frame(nb, padding=0)
        tab_report_chat = ttk.Frame(nb, padding=pad)
        tab_merged_log = ttk.Frame(nb, padding=pad)
        nb.add(tab_run, text=" 运行 ")
        nb.add(tab_report_chat, text=" 报告 ")
        nb.add(tab_merged_log, text=" 日志 ")

        run_wrap = ttk.Frame(tab_run)
        run_wrap.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(run_wrap, padding=(edge, edge, edge, 0))
        top.pack(fill=tk.X)

        mode_lf = ttk.LabelFrame(top, text=" 分析模式 ", padding=(pad, pad))
        mode_lf.pack(fill=tk.X, pady=(0, 10))
        ttk.Radiobutton(
            mode_lf,
            text="快速：仅字幕 / 总结",
            variable=self.flow_mode_var,
            value="subtitle",
            command=self._sync_flow_mode_ui,
        ).pack(anchor=tk.W, pady=(0, 4))
        ttk.Radiobutton(
            mode_lf,
            text="多模态：深度 + 可选画面管线",
            variable=self.flow_mode_var,
            value="multimodal",
            command=self._sync_flow_mode_ui,
        ).pack(anchor=tk.W)

        rep_lf = ttk.LabelFrame(top, text=" 文稿总结 ", padding=(pad, pad))
        rep_lf.pack(fill=tk.X, pady=(10, 0))
        rep_chk_row = ttk.Frame(rep_lf)
        rep_chk_row.pack(fill=tk.X)
        ttk.Checkbutton(
            rep_chk_row,
            text="用大模型生成「视频内容总结」",
            variable=self.flow_transcript_use_llm_var,
            command=self._sync_transcript_llm_source_widgets_state,
        ).pack(side=tk.LEFT, anchor=tk.W)
        tq_rep = ttk.Label(rep_chk_row, text=" ?", foreground=T_MUTED, cursor="hand2")
        tq_rep.pack(side=tk.LEFT, padx=(2, 0))
        _attach_tooltip(tq_rep, lambda: FLOW_REP_TIP_CHECK, wraplength=400)
        src_fr = ttk.Frame(rep_lf)
        src_fr.pack(fill=tk.X, pady=(6, 0))
        r_api = ttk.Frame(src_fr)
        r_api.pack(fill=tk.X)
        rb_api = ttk.Radiobutton(
            r_api,
            text="提供方：在线（「API 与模型」）",
            variable=self.flow_transcript_llm_source_var,
            value="api_preferred",
        )
        rb_api.pack(side=tk.LEFT, anchor=tk.W)
        tq_api = ttk.Label(r_api, text=" ?", foreground=T_MUTED, cursor="hand2")
        tq_api.pack(side=tk.LEFT, padx=(2, 0))
        _attach_tooltip(tq_api, lambda: FLOW_REP_TIP_API, wraplength=400)
        r_loc = ttk.Frame(src_fr)
        r_loc.pack(fill=tk.X, pady=(4, 0))
        rb_local = ttk.Radiobutton(
            r_loc,
            text="提供方：本地（与③同源）",
            variable=self.flow_transcript_llm_source_var,
            value="same_as_vlm",
        )
        rb_local.pack(side=tk.LEFT, anchor=tk.W)
        tq_loc = ttk.Label(r_loc, text=" ?", foreground=T_MUTED, cursor="hand2")
        tq_loc.pack(side=tk.LEFT, padx=(2, 0))
        _attach_tooltip(tq_loc, lambda: FLOW_REP_TIP_LOCAL, wraplength=400)
        self._transcript_llm_source_widgets = (rb_api, rb_local)
        self._sync_transcript_llm_source_widgets_state()

        uhead = tk.Frame(top, bg=T_PAGE)
        uhead.pack(fill=tk.X, pady=(0, 4))
        title_row = tk.Frame(uhead, bg=T_PAGE)
        title_row.pack(fill=tk.X, anchor=tk.W)
        self._flow_title_lbl = tk.Label(
            title_row,
            text="智能提取 → 大模型总结",
            font=self._font_title,
            bg=T_PAGE,
            fg=T_TEXT,
        )
        self._flow_title_lbl.pack(side=tk.LEFT, anchor=tk.W)
        tq_mode = tk.Label(
            title_row,
            text=" ?",
            font=hint_small,
            bg=T_PAGE,
            fg=T_MUTED,
            cursor="hand2",
        )
        tq_mode.pack(side=tk.LEFT, anchor=tk.W, padx=(4, 0))
        _attach_tooltip(tq_mode, lambda: FLOW_MODE_HELP, wraplength=400)

        self._flow_info_box = tk.Frame(
            uhead,
            bg=FLOW_SUB_INFO_BG,
            highlightbackground=FLOW_SUB_INFO_BORDER,
            highlightthickness=1,
        )
        self._flow_info_box.pack(fill=tk.X, pady=(6, 0))
        self._flow_info_inner = tk.Frame(self._flow_info_box, bg=FLOW_SUB_INFO_BG)
        self._flow_info_inner.pack(fill=tk.X, padx=10, pady=6)
        self._flow_info_lbl = tk.Label(
            self._flow_info_inner,
            text="",
            bg=FLOW_SUB_INFO_BG,
            fg=T_TEXT,
            font=hint_small,
            wraplength=max(380, int(720 * self._geom_scale) - 48),
            justify=tk.LEFT,
            anchor=tk.NW,
        )
        self._flow_info_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, anchor=tk.NW)
        self._flow_info_tip_lbl = tk.Label(
            self._flow_info_inner,
            text="?",
            font=hint_small,
            bg=FLOW_SUB_INFO_BG,
            fg=FLOW_SUB_MUTED,
            cursor="hand2",
        )
        self._flow_info_tip_lbl.pack(side=tk.LEFT, anchor=tk.N, padx=(6, 0))
        _attach_tooltip(
            self._flow_info_tip_lbl, self._flow_info_tooltip_text, wraplength=440
        )

        LocalInferenceFlowBar(
            top,
            self._local_chat_hub._infer,
            self,
            pad=pad,
        ).pack(fill=tk.X, pady=(12, 0))

        url_card = ttk.LabelFrame(top, text=" 链接或本地音视频 ", padding=pad)
        url_card.pack(fill=tk.X, pady=(14, 0))
        url_row = ttk.Frame(url_card)
        url_row.pack(fill=tk.X)
        url_row.columnconfigure(0, weight=1)
        self._flow_url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self._flow_url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._flow_url_entry.bind("<Return>", lambda _e: self._start_run_from_flow_mode())
        ttk.Button(url_row, text="浏览…", command=self._browse_media, width=10).grid(
            row=0, column=1, sticky=tk.E
        )
        model_row = ttk.Frame(top)
        model_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(model_row, text="Whisper 模型").pack(side=tk.LEFT, padx=(0, 8))
        self.whisper_combo = ttk.Combobox(
            model_row,
            textvariable=self.whisper_model_var,
            values=WHISPER_MODEL_CHOICES,
            state="readonly",
            width=14,
        )
        self.whisper_combo.pack(side=tk.LEFT, padx=(0, 10))

        uopt = ttk.Frame(top)
        uopt.pack(fill=tk.X, pady=(10, 0))
        asr_row = tk.Frame(uopt, bg=T_SURFACE, cursor="hand2")
        asr_row.pack(fill=tk.X)
        sym_a = tk.Label(
            asr_row,
            text="",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_ACCENT,
            cursor="hand2",
        )
        sym_a.pack(side=tk.LEFT, padx=(0, 8))
        self._asr_sym_boxes = [sym_a]
        asr_caption = tk.Label(
            asr_row,
            text="无字幕时用 Whisper 转写",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_TEXT,
            cursor="hand2",
            anchor=tk.W,
        )
        asr_caption.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _asr_click_u(_event: object = None) -> None:
            self.asr_if_no_subs_var.set(not self.asr_if_no_subs_var.get())

        for w in (asr_row, sym_a, asr_caption):
            w.bind("<Button-1>", _asr_click_u)
        self.asr_if_no_subs_var.trace_add("write", lambda *_: self._sync_asr_symbol())
        self._sync_asr_symbol()

        vid_row = tk.Frame(uopt, bg=T_SURFACE, cursor="hand2")
        vid_row.pack(fill=tk.X, pady=(6, 0))
        sym_v = tk.Label(
            vid_row,
            text="",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_ACCENT,
            cursor="hand2",
        )
        sym_v.pack(side=tk.LEFT, padx=(0, 8))
        vid_caption = tk.Label(
            vid_row,
            text="B 站：下载整片视频（多模态抽帧；关则只要字幕）",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_TEXT,
            cursor="hand2",
            anchor=tk.W,
        )
        vid_caption.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _vid_click(_event: object = None) -> None:
            self.download_bilibili_video_var.set(
                not self.download_bilibili_video_var.get()
            )

        for w in (vid_row, sym_v, vid_caption):
            w.bind("<Button-1>", _vid_click)

        def _sync_vid_symbol(*_args: object) -> None:
            on = self.download_bilibili_video_var.get()
            sym = "✔" if on else "□"
            fg = T_ACCENT if on else T_MUTED
            try:
                sym_v.configure(text=sym, fg=fg)
            except tk.TclError:
                pass

        self.download_bilibili_video_var.trace_add("write", _sync_vid_symbol)
        _sync_vid_symbol()

        bottom = tk.Frame(run_wrap, bg=T_PAGE)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=edge, pady=(0, edge))
        act = tk.Frame(bottom, bg=T_PAGE)
        act.pack(fill=tk.X, pady=(0, 6))
        self._flow_run_btn = ttk.Button(
            act,
            text="开始：提取并生成总结",
            command=self._start_run_from_flow_mode,
            style=self._run_button_style,
        )
        self._flow_run_btn.pack(side=tk.LEFT, padx=(0, 10))
        self._flow_cancel_btn = ttk.Button(
            act,
            text="停止当前任务",
            command=self.cancel_pipeline_run,
            style=sec_style,
            state=tk.DISABLED,
        )
        self._flow_cancel_btn.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(
            act,
            text="看日志",
            command=lambda: self._focus_workspace_tab("log"),
            style=sec_style,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            act,
            text="看合并",
            command=lambda: self._focus_workspace_tab("merged"),
            style=sec_style,
        ).pack(side=tk.LEFT)
        self._flow_status = ttk.Label(bottom, text="就绪", anchor=tk.W)
        self._flow_status.pack(fill=tk.X, pady=(0, 2))

        self._flow_multimodal_scroll = ttk.Frame(run_wrap)
        mm_host = self._flow_multimodal_scroll
        mm_host.grid_rowconfigure(0, weight=1)
        mm_host.grid_columnconfigure(0, weight=1)
        y_inc = max(18, int(round(self._font_content[1] * 2.4)))
        ff_canvas = tk.Canvas(
            mm_host,
            highlightthickness=0,
            borderwidth=0,
            bg=T_PAGE,
            yscrollincrement=y_inc,
        )
        ff_vsb = tk.Scrollbar(
            mm_host,
            orient=tk.VERTICAL,
            command=ff_canvas.yview,
            width=self._scrollbar_px,
            borderwidth=0,
            troughcolor=SCROLL_TROUGH,
            bg="#c4c4c4",
            activebackground="#a8a8a8",
            highlightthickness=0,
            relief="flat",
            jump=1,
        )
        ff_canvas.configure(yscrollcommand=ff_vsb.set)
        ff_canvas.grid(row=0, column=0, sticky="nsew")
        ff_vsb.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(ff_canvas, bg=T_PAGE)
        _ff_inner_win = ff_canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        def _ff_canvas_on_cfg(event: tk.Event) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 1:
                ff_canvas.itemconfigure(_ff_inner_win, width=w)

        def _ff_inner_on_cfg(_event: object | None = None) -> None:
            ff_canvas.configure(scrollregion=ff_canvas.bbox("all"))

        ff_canvas.bind("<Configure>", _ff_canvas_on_cfg)
        inner.bind("<Configure>", lambda _e: _ff_inner_on_cfg())

        def _ff_wheel(event: tk.Event) -> str | None:
            lines = self._text_wheel_lines
            d = getattr(event, "delta", 0) or 0
            if d:
                if sys.platform == "win32":
                    steps = int(-d * lines / 120.0)
                    if steps == 0:
                        steps = -lines if d > 0 else lines
                else:
                    steps = int(-d * lines / 120.0)
                    if steps == 0:
                        steps = -lines if d > 0 else lines
                if steps:
                    ff_canvas.yview_scroll(steps, "units")
                return "break"
            n = getattr(event, "num", 0)
            if n == 4:
                ff_canvas.yview_scroll(-lines, "units")
                return "break"
            if n == 5:
                ff_canvas.yview_scroll(lines, "units")
                return "break"
            return None

        def _ff_bind_wheel(w: tk.Misc) -> None:
            w.bind("<MouseWheel>", _ff_wheel, add="+")
            w.bind("<Button-4>", _ff_wheel, add="+")
            w.bind("<Button-5>", _ff_wheel, add="+")
            for c in w.winfo_children():
                _ff_bind_wheel(c)

        mm_opt = ttk.Frame(inner)
        mm_opt.pack(fill=tk.X, padx=edge, pady=(0, edge))

        force_row = tk.Frame(mm_opt, bg=T_SURFACE, cursor="hand2")
        force_row.pack(fill=tk.X)
        self._asr_force_sym = tk.Label(
            force_row,
            text="",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_ACCENT,
            cursor="hand2",
        )
        self._asr_force_sym.pack(side=tk.LEFT, padx=(0, 8))
        force_cap = tk.Label(
            force_row,
            text="有字幕也强制 Whisper（更慢）",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_TEXT,
            cursor="hand2",
            anchor=tk.W,
        )
        force_cap.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _force_click(_event: object = None) -> None:
            self.asr_force_var.set(not self.asr_force_var.get())

        for w in (force_row, self._asr_force_sym, force_cap):
            w.bind("<Button-1>", _force_click)
        self.asr_force_var.trace_add("write", lambda *_: self._sync_asr_force_symbol())
        self._sync_asr_force_symbol()

        pipe = ttk.LabelFrame(
            inner,
            text=" 画面管线（①–⑤） ",
            padding=pad,
        )
        pipe.pack(fill=tk.X, pady=(14, 0))
        ttk.Checkbutton(
            pipe,
            text="启用画面管线",
            variable=self.vision_enable_var,
        ).pack(anchor=tk.W, pady=(0, 6))
        vm_fr = ttk.Frame(pipe)
        vm_fr.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(vm_fr, text="观看密度").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Radiobutton(
            vm_fr,
            text="粗看（省资源）",
            variable=self.vision_view_mode_var,
            value="coarse",
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(
            vm_fr,
            text="细看（时间轴更全）",
            variable=self.vision_view_mode_var,
            value="fine",
        ).pack(side=tk.LEFT, padx=(0, 6))
        tq_vm = ttk.Label(vm_fr, text=" ?", foreground=T_MUTED, cursor="hand2")
        tq_vm.pack(side=tk.LEFT, padx=(2, 0))
        _attach_tooltip(tq_vm, lambda: FLOW_VIEW_MODE_HELP, wraplength=420)

        s1 = ttk.LabelFrame(pipe, text=" ① 抽帧 / 去重 ", padding=(pad, 6))
        s1.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(
            s1,
            text="按间隔 FFmpeg 抽帧",
            variable=self.vision_frame_extract_var,
        ).pack(anchor=tk.W)
        iv_row = ttk.Frame(s1)
        iv_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(iv_row, text="抽帧间隔（秒）").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Combobox(
            iv_row,
            textvariable=self.vision_frame_iv_str,
            values=("0.5", "1.0", "2.0", "3.0", "5.0", "10.0", "30.0", "60.0"),
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(
            iv_row,
            text="（界面所选间隔；粗看约 20 帧 VLM，细看写入≤1s 且至多约 96 帧）",
            foreground=T_MUTED,
            font=self._font_hint,
        ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Checkbutton(
            s1,
            text="PySceneDetect 场景切分",
            variable=self.vision_scene_detect_var,
        ).pack(anchor=tk.W, pady=(6, 0))
        ph_row = ttk.Frame(s1)
        ph_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Checkbutton(
            ph_row,
            text="感知哈希去重",
            variable=self.vision_phash_var,
        ).pack(side=tk.LEFT)
        ttk.Label(ph_row, text="哈希差阈值（越小越严）").pack(side=tk.LEFT, padx=(16, 6))
        tk.Spinbox(
            ph_row,
            from_=0,
            to=16,
            textvariable=self.vision_phash_diff_var,
            width=5,
        ).pack(side=tk.LEFT)

        s2 = ttk.LabelFrame(pipe, text=" ② OCR（PaddleOCR） ", padding=(pad, 6))
        s2.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(s2, text="启用 OCR", variable=self.vision_ocr_var).pack(anchor=tk.W)
        ttk.Checkbutton(
            s2,
            text="全帧 OCR（关则保留下沿裁切）",
            variable=self.vision_ocr_full_var,
        ).pack(anchor=tk.W, pady=(2, 0))
        ttk.Checkbutton(
            s2,
            text="2× 放大",
            variable=self.vision_ocr_2x_var,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            s2,
            text="OCR 用 GPU",
            variable=self.vision_ocr_gpu_var,
        ).pack(anchor=tk.W)
        oc_row = ttk.Frame(s2)
        oc_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(oc_row, text="下沿裁切高度（%）").pack(side=tk.LEFT, padx=(0, 8))
        tk.Spinbox(
            oc_row,
            from_=10,
            to=40,
            textvariable=self.vision_ocr_bottom_pct_var,
            width=5,
        ).pack(side=tk.LEFT)

        s3 = ttk.LabelFrame(pipe, text=" ③ Gemma 看图（OpenAI 兼容，一句描述） ", padding=(pad, 6))
        s3.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(
            s3,
            text="稀疏关键帧：每帧一句画面描述（与下方 URL/模型一致）",
            variable=self.vision_vlm_var,
        ).pack(anchor=tk.W)
        ttk.Label(s3, text="服务 URL（/v1）", font=self._font_ui).pack(anchor=tk.W, pady=(4, 0))
        ttk.Entry(s3, textvariable=self.vision_vlm_base_var).pack(fill=tk.X, pady=(2, 0))
        ttk.Label(s3, text="模型 ID", font=self._font_ui).pack(anchor=tk.W, pady=(4, 0))
        ttk.Entry(s3, textvariable=self.vision_vlm_model_var).pack(fill=tk.X, pady=(2, 0))
        vk_row = ttk.Frame(s3)
        vk_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(vk_row, text="API Key（可空）").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(vk_row, textvariable=self.vision_vlm_key_var, show="*").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        pr_row = ttk.Frame(s3)
        pr_row.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(pr_row, text="精度（写入 JSON）").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Combobox(
            pr_row,
            textvariable=self.vision_vlm_precision_var,
            values=("BF16", "FP8", "INT4"),
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT)
        s4 = ttk.LabelFrame(pipe, text=" ④ 视频类型 ", padding=(pad, 6))
        s4.pack(fill=tk.X, pady=(0, 8))
        vt_row = ttk.Frame(s4)
        vt_row.pack(fill=tk.X)
        ttk.Label(vt_row, text="类型").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Combobox(
            vt_row,
            textvariable=self.vision_video_type_var,
            values=("auto", "lecture", "tutorial", "vlog", "gaming"),
            state="readonly",
            width=12,
        ).pack(side=tk.LEFT)
        s5 = ttk.LabelFrame(pipe, text=" ⑤ 输出 ", padding=(pad, 6))
        s5.pack(fill=tk.X, pady=(0, 8))
        out_row = ttk.Frame(s5)
        out_row.pack(fill=tk.X)
        ttk.Checkbutton(out_row, text="video_analysis_deep.md", variable=self.vision_out_md_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        ttk.Checkbutton(out_row, text="video_analysis_deep.json", variable=self.vision_out_json_var).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        ttk.Checkbutton(
            out_row,
            text="video_analysis.srt",
            variable=self.vision_out_srt_var,
        ).pack(side=tk.LEFT)
        cx_row = ttk.Frame(s5)
        cx_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(cx_row, text="对话附带画面").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Combobox(
            cx_row,
            textvariable=self.chat_vision_context_var,
            values=("auto", "manual"),
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT)
        _ff_bind_wheel(inner)
        self.after(100, lambda: _ff_bind_wheel(inner))

        rc_paned_f = ttk.Panedwindow(tab_report_chat, orient=tk.VERTICAL)
        rc_paned_f.pack(fill=tk.BOTH, expand=True)
        rc_top_f = ttk.Frame(rc_paned_f)
        rc_bot_f = ttk.Frame(rc_paned_f)
        rc_paned_f.add(rc_top_f, weight=3)
        rc_paned_f.add(rc_bot_f, weight=2)
        try:
            rc_paned_f.paneconfigure(rc_top_f, minsize=140)
            rc_paned_f.paneconfigure(rc_bot_f, minsize=180)
        except tk.TclError:
            pass

        notes_outer = ttk.LabelFrame(
            rc_top_f,
            text=" 报告与摘录 ",
            padding=pad,
        )
        notes_outer.pack(fill=tk.BOTH, expand=True)
        self._flow_notes_txt = self._build_text_view(notes_outer, mono=False)
        self._configure_report_text_tags(self._flow_notes_txt)

        self._flow_chat_parent = rc_bot_f
        self.flow_agent_panel = AgentChatPanel(
            rc_bot_f, self, self.agent_session, compact=True
        )
        self.flow_agent_panel.pack(fill=tk.BOTH, expand=True)

        ml_paned_f = ttk.Panedwindow(tab_merged_log, orient=tk.VERTICAL)
        ml_paned_f.pack(fill=tk.BOTH, expand=True)
        ml_top_f = ttk.Frame(ml_paned_f)
        ml_bot_f = ttk.Frame(ml_paned_f)
        ml_paned_f.add(ml_top_f, weight=1)
        ml_paned_f.add(ml_bot_f, weight=1)
        try:
            ml_paned_f.paneconfigure(ml_top_f, minsize=120)
            ml_paned_f.paneconfigure(ml_bot_f, minsize=120)
        except tk.TclError:
            pass

        merged_card_f = ttk.LabelFrame(
            ml_top_f,
            text=" 合并文稿 ",
            padding=pad,
        )
        merged_card_f.pack(fill=tk.BOTH, expand=True)
        self.merged_txt = self._build_text_view(merged_card_f, mono=False)
        self._merged_text_widgets.append(self.merged_txt)

        log_card_f = ttk.LabelFrame(ml_bot_f, text=" 运行日志 ", padding=pad)
        log_card_f.pack(fill=tk.BOTH, expand=True)
        self.log_txt = self._build_text_view(log_card_f, mono=True)
        self._log_text_widgets.append(self.log_txt)

        self._analysis_text_widgets = [self._flow_notes_txt]

        self._sync_flow_mode_ui()

    def _mirror_status_to_flow_pages(self) -> None:
        try:
            t = self.status.cget("text")
        except tk.TclError:
            return
        lb = getattr(self, "_flow_status", None)
        if lb is not None:
            try:
                lb.configure(text=t)
            except tk.TclError:
                pass

    def _sync_asr_force_symbol(self) -> None:
        if self.asr_force_var.get():
            self._asr_force_sym.configure(text="✔", fg=T_ACCENT)
        else:
            self._asr_force_sym.configure(text="□", fg=T_MUTED)

    def _sync_asr_symbol(self) -> None:
        """与说明同字号：未选 □、已选 ✔（不用系统小复选框，避免画成 X）。"""
        on = self.asr_if_no_subs_var.get()
        sym = "✔" if on else "□"
        fg = T_ACCENT if on else T_MUTED
        for lb in self._asr_sym_boxes:
            try:
                lb.configure(text=sym, fg=fg)
            except tk.TclError:
                pass

    def _update_infer_global_chip(self, text: str) -> None:
        try:
            if text.strip():
                self._infer_global_lbl.configure(text=text.strip(), fg=T_ACCENT)
            else:
                self._infer_global_lbl.configure(text="", fg=T_MUTED)
        except tk.TclError:
            pass

    def _on_infer_global_chip_click(self, _event: object | None = None) -> None:
        self._show_view("local_chat")
        self._local_chat_hub._show_local_inference_tab()
        self._local_chat_hub._infer.refresh_ui_state()

    def _show_view(self, key: str) -> None:
        if key not in self._views:
            return
        self._current_nav = key
        self._views[key].tkraise()
        if key == "local_chat":
            self._local_chat_hub.on_tab_visible()
        else:
            self._local_chat_hub._infer.refresh_ui_state()
        self._sync_nav_appearance()

    def _paint_nav_row(self, key: str, *, hover: bool = False) -> None:
        sel = key == self._current_nav
        if sel:
            inner_bg = NAV_ACTIVE_BG
            accent_bg = T_ACCENT
        elif hover:
            inner_bg = NAV_HOVER_BG
            accent_bg = NAV_HOVER_BG
        else:
            inner_bg = NAV_SIDEBAR_BG
            accent_bg = NAV_SIDEBAR_BG
        row = self._nav_rows[key]
        row.configure(bg=inner_bg)
        self._nav_accents[key].configure(bg=accent_bg)
        self._nav_inners[key].configure(bg=inner_bg)
        self._nav_labels[key].configure(bg=inner_bg, fg=T_TEXT)

    def _sync_nav_appearance(self) -> None:
        for k in self._nav_rows:
            self._paint_nav_row(k, hover=False)

    def _setup_fonts_and_styles(self) -> str:
        """返回主操作按钮的 ttk style 名。"""
        fs = self._font_soft
        ui_pt = max(10, min(11, int(round(10 * fs))))
        content_pt = ui_pt
        mono_pt = ui_pt

        sans_ui = _pick_sans_cjk(self)
        self._font_ui = (sans_ui, ui_pt)
        self._font_content: tuple[str, int] = (sans_ui, content_pt)
        self._font_log: tuple[str, int] = (_pick_mono_family(self), mono_pt)
        title_pt = min(ui_pt + 1, 12)
        self._font_title: tuple[str, int, str] = (sans_ui, title_pt, "bold")
        self._font_hint: tuple[str, int] = (sans_ui, ui_pt)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self._clam_bg = T_PAGE

        try:
            style.configure(".", background=T_PAGE, foreground=T_TEXT, fieldbackground=T_ENTRY)
            style.configure("TFrame", background=T_PAGE)
            style.configure("TLabel", background=T_PAGE, foreground=T_TEXT)
            style.configure(
                "TLabelframe",
                background=T_PAGE,
                foreground=T_TEXT,
                borderwidth=1,
                relief="solid",
                bordercolor=T_BORDER,
            )
            style.configure(
                "TLabelframe.Label",
                background=T_PAGE,
                foreground=T_MUTED,
            )
            style.configure("TNotebook", background=T_PAGE, borderwidth=0)
            style.configure(
                "TNotebook.Tab",
                background=WORKSPACE_TAB_IDLE,
                foreground=T_MUTED,
            )
            style.map(
                "TNotebook.Tab",
                background=[("selected", WORKSPACE_TAB_SELECTED)],
                foreground=[("selected", T_TEXT)],
            )
            style.configure("Sticky.TNotebook", background=T_PAGE, borderwidth=0)
            style.configure(
                "Sticky.TNotebook.Tab",
                background=WORKSPACE_TAB_IDLE,
                foreground=T_MUTED,
                borderwidth=0,
                relief="flat",
            )
            style.map(
                "Sticky.TNotebook.Tab",
                background=[
                    ("selected", WORKSPACE_TAB_SELECTED),
                    ("active", T_RAISED),
                ],
                foreground=[("selected", T_TEXT)],
            )
            style.configure("TEntry", fieldbackground=T_ENTRY, foreground=T_TEXT, bordercolor=T_BORDER)
            style.configure("TSeparator", background=T_BORDER)
            style.configure(
                "TButton",
                font=self._font_ui,
                background=T_RAISED,
                foreground=T_TEXT,
                borderwidth=1,
                relief="flat",
                padding=(10, 5),
            )
            style.map(
                "TButton",
                background=[("active", T_BORDER), ("disabled", T_RAISED)],
                foreground=[("disabled", T_MUTED)],
            )
        except tk.TclError:
            pass

        for name in ("TLabel", "TButton", "TNotebook.Tab", "TEntry", "TLabelframe"):
            try:
                style.configure(name, font=self._font_ui)
            except tk.TclError:
                pass
        try:
            style.configure(
                "TLabelframe.Label",
                font=(self._font_ui[0], self._font_ui[1], "bold"),
                foreground=T_TEXT,
            )
        except tk.TclError:
            pass

        tab_pad_x = max(10, int(round(10 * fs)))
        tab_pad_y = max(4, int(round(5 * fs)))
        sticky_pad_x = max(10, int(round(11 * fs)))
        sticky_pad_y = max(5, int(round(6 * fs)))
        try:
            style.configure("TNotebook.Tab", padding=[tab_pad_x, tab_pad_y])
            style.configure(
                "Sticky.TNotebook.Tab", padding=[sticky_pad_x, sticky_pad_y]
            )
        except tk.TclError:
            pass

        btn_pad_x = max(10, int(round(12 * fs)))
        btn_pad_y = max(4, int(round(5 * fs)))
        try:
            style.configure(
                "Accent.TButton",
                font=self._font_ui,
                foreground="#ffffff",
                background=T_ACCENT,
                borderwidth=0,
                focusthickness=3,
                focuscolor="none",
                padding=(btn_pad_x, btn_pad_y),
            )
            style.map(
                "Accent.TButton",
                background=[("active", T_ACCENT_ACTIVE), ("disabled", T_RAISED)],
                foreground=[("disabled", T_MUTED)],
            )
            style.configure(
                "Secondary.TButton",
                font=self._font_ui,
                background=T_PANEL,
                foreground=T_TEXT,
                borderwidth=1,
                relief="flat",
                padding=(btn_pad_x, btn_pad_y),
            )
            style.map(
                "Secondary.TButton",
                background=[("active", T_RAISED), ("disabled", T_PANEL)],
                foreground=[("disabled", T_MUTED)],
            )
            stop_pad_x = max(8, int(round(10 * fs)))
            style.configure(
                "Stop.TButton",
                font=self._font_ui,
                foreground="#cf222e",
                background=COMPOSER_TOOLBAR_BG,
                borderwidth=1,
                relief="flat",
                padding=(stop_pad_x, btn_pad_y),
            )
            style.map(
                "Stop.TButton",
                background=[
                    ("active", "#ffebe9"),
                    ("disabled", COMPOSER_TOOLBAR_BG),
                ],
                foreground=[("disabled", COMPOSER_STOP_IDLE)],
            )
            self._secondary_button_style = "Secondary.TButton"
            return "Accent.TButton"
        except tk.TclError:
            try:
                style.configure("TButton", padding=(btn_pad_x, btn_pad_y))
            except tk.TclError:
                pass
            self._secondary_button_style = "TButton"
            try:
                style.configure(
                    "Stop.TButton",
                    font=self._font_ui,
                    foreground="#cf222e",
                    background=COMPOSER_TOOLBAR_BG,
                    padding=(max(8, int(round(10 * fs))), btn_pad_y),
                )
            except tk.TclError:
                pass
            return "TButton"

    def _build_text_view(self, parent: tk.Misc, *, mono: bool) -> tk.Text:
        pad_txt = max(8, int(round(8 * self._font_soft)))
        inner = ttk.Frame(parent)
        inner.pack(fill=tk.BOTH, expand=True)

        font = self._font_log if mono else self._font_content
        if not mono:
            pad_txt = max(pad_txt, int(round(12 * self._font_soft)))
        txt = tk.Text(
            inner,
            wrap=tk.WORD,
            font=font,
            undo=False,
            padx=pad_txt,
            pady=pad_txt,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=T_BORDER,
            highlightcolor=T_ACCENT,
            bg=T_PANEL,
            fg=T_TEXT,
            insertbackground=T_TEXT,
            selectbackground=T_SELECT,
        )
        scroll = make_text_y_scrollbar(inner, txt, width_px=self._scrollbar_px)
        bind_text_mousewheel(txt, lines_per_notch=self._text_wheel_lines)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return txt

    def _configure_report_text_tags(self, txt: tk.Text) -> None:
        fam, sz = self._font_content[0], self._font_content[1]
        sz_i = int(round(sz))
        txt.tag_configure(
            "report_title",
            font=(fam, min(sz_i + 2, 15), "bold"),
            foreground=T_ACCENT,
            spacing1=2,
            spacing3=6,
        )
        txt.tag_configure(
            "report_h1",
            font=(fam, min(sz_i + 1, 14), "bold"),
            foreground="#111827",
            spacing1=8,
            spacing3=3,
        )
        txt.tag_configure(
            "report_subhead",
            font=(fam, min(sz_i + 1, 13), "bold"),
            foreground="#374151",
            spacing1=6,
            spacing3=2,
        )
        txt.tag_configure(
            "report_meta",
            font=(fam, max(8, sz_i - 1)),
            foreground=T_MUTED,
        )

    def _set_report_editor_content(self, raw: str, *, target: tk.Text | None = None) -> None:
        if target is not None:
            targets: tuple[tk.Text, ...] = (target,)
        else:
            targets = tuple(getattr(self, "_analysis_text_widgets", ()))
        content = sanitize_analysis_display(raw)
        for txt in targets:
            txt.delete("1.0", tk.END)
            for line in content.splitlines(keepends=True):
                if line.endswith("\n"):
                    core, nl = line[:-1], "\n"
                else:
                    core, nl = line, ""
                tag = report_line_style_tag(core)
                if tag:
                    txt.insert(tk.END, core + nl, (tag,))
                else:
                    txt.insert(tk.END, core + nl)

    def _pack_llm_progress_row_if_needed(self) -> None:
        if self._progress_row_visible:
            return
        self._progress_row.pack(
            fill=tk.X, pady=(0, 8), after=self._top_status_bar
        )
        self._progress_row_visible = True

    def _hide_inference_progress_ui(self) -> None:
        try:
            self._llm_progress.stop()
        except tk.TclError:
            pass
        if self._progress_row_visible:
            try:
                self._progress_row.pack_forget()
            except tk.TclError:
                pass
            self._progress_row_visible = False
        try:
            self._llm_progress.configure(mode="determinate", value=0)
            self._llm_progress_lbl.configure(text="")
        except tk.TclError:
            pass

    def _apply_inference_progress_ui(self, m: str, v: int, t: str) -> None:
        if m == "h":
            self._hide_inference_progress_ui()
            return
        self._pack_llm_progress_row_if_needed()
        try:
            self._llm_progress_lbl.configure(text=t or "处理中…")
            if m == "i":
                self._llm_progress.stop()
                self._llm_progress.configure(mode="indeterminate", maximum=100)
                self._llm_progress.start(15)
            else:
                self._llm_progress.stop()
                self._llm_progress.configure(mode="determinate", maximum=100)
                self._llm_progress["value"] = max(0, min(100, int(v)))
        except tk.TclError:
            pass

    def _register_pipeline_subprocess(self, p: subprocess.Popen) -> None:
        with self._pipeline_proc_lock:
            self._pipeline_active_procs.append(p)

    def _unregister_pipeline_subprocess(self, p: subprocess.Popen) -> None:
        with self._pipeline_proc_lock:
            try:
                self._pipeline_active_procs.remove(p)
            except ValueError:
                pass

    def cancel_pipeline_run(self) -> None:
        """终止正在进行的提取 / 分析报告 / 多模态管线，便于更换链接或本地文件。"""
        if not self._busy:
            return
        self._pipeline_cancel_requested.set()
        self._append_log("\n[gui] 已请求停止当前任务（正在终止子进程）…\n")
        with self._pipeline_proc_lock:
            snap = list(self._pipeline_active_procs)
        for proc in snap:
            _terminate_process_tree(proc)

    def _handle_pipeline_output_line(self, line: str) -> None:
        raw = line.rstrip("\r\n")
        if raw.startswith(_GUI_PROGRESS_PREFIX):
            try:
                payload = json.loads(raw[len(_GUI_PROGRESS_PREFIX) :])
                m = str(payload.get("m", "h"))
                v = int(payload.get("v", 0))
                tx = str(payload.get("t", ""))
            except (json.JSONDecodeError, ValueError, TypeError):
                return
            self._apply_inference_progress_ui(m, v, tx)
            return
        self._append_log(line)

    def start_run(self, *, deep_analysis: bool) -> None:
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not is_valid_task_source(url):
            messagebox.showwarning(
                "提示",
                "请输入 B 站 https 链接，或选择/粘贴本机支持的音视频文件路径。\n"
                "常见后缀：mp3、m4a、wav、flac、mp4、mkv、webm 等。",
            )
            return

        p_try = Path(os.path.expanduser(url.strip('"')))
        local_run = bool(p_try.is_file() and is_supported_local_media(p_try))

        self._pipeline_cancel_requested.clear()
        with self._pipeline_proc_lock:
            self._pipeline_active_procs.clear()
        self._busy = True
        self._hide_inference_progress_ui()
        btn_run = getattr(self, "_flow_run_btn", None)
        if btn_run is not None:
            btn_run.configure(state=tk.DISABLED)
        cbtn = getattr(self, "_flow_cancel_btn", None)
        if cbtn is not None:
            cbtn.configure(state=tk.NORMAL)
        if local_run:
            self.status.configure(text="运行中…")
        else:
            self.status.configure(text="运行中…（B 站可能需登录）")
        self._mirror_status_to_flow_pages()
        for w in getattr(self, "_log_text_widgets", ()):
            w.delete("1.0", tk.END)
        for w in getattr(self, "_merged_text_widgets", ()):
            w.delete("1.0", tk.END)
        for txt in getattr(self, "_analysis_text_widgets", ()):
            txt.delete("1.0", tk.END)

        def on_line(line: str) -> None:
            self.after(0, lambda l=line: self._handle_pipeline_output_line(l))

        def on_done(code: int) -> None:
            self.after(0, lambda: self._finish_run(code))

        wm = self.whisper_model_var.get().strip()
        if wm not in WHISPER_MODEL_CHOICES:
            wm = default_whisper_model_choice()
        self._sync_llm_env_from_form()
        self.prepare_chat_env_for_dialogue()
        transcript_use_llm = bool(self.flow_transcript_use_llm_var.get())
        transcript_src = self.flow_transcript_llm_source_var.get().strip()
        if transcript_use_llm and transcript_src == "same_as_vlm":
            self._overlay_vision_vlm_as_openai_env()
        elif self.chat_route_var.get() == "local":
            # 文稿将走 API 首选 / 云端时，不应沿用「本地对话」的 OPENAI_*（否则会误连对话端口如 8090）。
            self._restore_openai_env_from_api_form()
        self.agent_session._provider = self.resolve_chat_provider_for_dialogue()
        if not transcript_use_llm:
            lp = "none"
        elif transcript_src == "same_as_vlm":
            lp = "local"
        else:
            lp = self.llm_provider_var.get().strip() or "auto"
        if transcript_use_llm and lp == "local":
            self._ensure_pipeline_local_openai_env()
        asr_if_no_subs = bool(self.asr_if_no_subs_var.get())
        asr_force = bool(self.asr_force_var.get()) if deep_analysis else False
        download_vid = bool(self.download_bilibili_video_var.get())
        vision_after = bool(deep_analysis and self.vision_enable_var.get())
        run_pipeline(
            url,
            on_line,
            on_done,
            asr_if_no_subs=asr_if_no_subs,
            asr_force=asr_force,
            download_bilibili_video=download_vid,
            whisper_model=wm,
            llm_provider=lp,
            deep_analyze=deep_analysis,
            vision_run_after=vision_after,
            app=self,
            pending_deep_for_vision=deep_analysis,
        )

    def _append_log(self, line: str) -> None:
        for w in getattr(self, "_log_text_widgets", ()):
            w.insert(tk.END, line)
            w.see(tk.END)

    def _finish_run(self, code: int) -> None:
        self._hide_inference_progress_ui()
        self._busy = False
        self._pipeline_cancel_requested.clear()
        with self._pipeline_proc_lock:
            self._pipeline_active_procs.clear()
        btn_run = getattr(self, "_flow_run_btn", None)
        if btn_run is not None:
            btn_run.configure(state=tk.NORMAL)
        cbtn = getattr(self, "_flow_cancel_btn", None)
        if cbtn is not None:
            cbtn.configure(state=tk.DISABLED)
        self.reload_files()
        if code == PIPELINE_EXIT_USER_CANCEL:
            self.status.configure(text="已停止")
        elif code == 0:
            try:
                self._save_flow_and_vision_prefs()
            except OSError:
                pass
            self.status.configure(text="完成")
        else:
            self.status.configure(text=f"退出码 {code} · 见「日志」")
            messagebox.showerror(
                "未完成",
                f"命令退出码 {code}，请在「合并与日志」标签下半区查看详情。",
            )
        self._mirror_status_to_flow_pages()

    def reload_files(self) -> None:
        merged_p = self.path_merged()
        if merged_p.is_file():
            merged_body = merged_p.read_text(encoding="utf-8", errors="replace")
            for w in getattr(self, "_merged_text_widgets", ()):
                w.delete("1.0", tk.END)
                w.insert("1.0", merged_body)
        analysis_p = self.path_analysis()
        if analysis_p.is_file():
            raw_a = analysis_p.read_text(encoding="utf-8", errors="replace")
            notes = getattr(self, "_flow_notes_txt", None)
            if notes is not None:
                blob = raw_a
                deep_p = self.path_analysis_deep()
                if deep_p.is_file():
                    blob += (
                        "\n\n══════════════\n\n"
                        + deep_p.read_text(encoding="utf-8", errors="replace")
                    )
                extras: list[str] = []
                md_p = self.path_analysis_deep_md()
                json_p = self.path_analysis_deep_json()
                srt_p = self.path_vision_timeline_srt()
                if md_p.is_file():
                    extras.append(f"多模态 Markdown：{md_p}")
                if json_p.is_file():
                    extras.append(f"结构化 JSON：{json_p}")
                if srt_p.is_file():
                    extras.append(f"画面时间轴 SRT：{srt_p}")
                if extras:
                    blob += "\n\n---\n" + "\n".join(extras) + "\n"
                self._set_report_editor_content(blob, target=notes)
        self._mirror_status_to_flow_pages()


def _resolve_ollama_executable() -> str | None:
    """PATH + Windows 常见安装目录（从资源管理器启动 GUI 时 PATH 往往不含 Ollama）。"""
    for name in ("ollama", "ollama.exe"):
        w = shutil.which(name)
        if w:
            return w
    if sys.platform != "win32":
        return None
    la = os.environ.get("LOCALAPPDATA", "").strip()
    pf = os.environ.get("ProgramFiles", "").strip()
    pfx86 = os.environ.get("ProgramFiles(x86)", "").strip()
    for base in (
        Path(la) / "Programs" / "Ollama" if la else None,
        Path(pf) / "Ollama" if pf else None,
        Path(pfx86) / "Ollama" if pfx86 else None,
    ):
        if base is None:
            continue
        for exe in ("ollama.exe", "Ollama.exe"):
            cand = base / exe
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                pass
    return None


def _tcp_port_in_use(host: str, port: int, *, timeout: float = 0.45) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _windows_exit_ntstatus(code: int | None) -> int | None:
    """把 subprocess 返回码转为无符号 32 位，便于识别 NTSTATUS（如 0xC0000005）。"""
    if code is None:
        return None
    return code & 0xFFFFFFFF


def _format_subprocess_exit_status(code: int | None) -> str:
    """子进程退出文案；Windows 上偶见负数/异常码，附无符号便于对照文档。"""
    if code is None:
        return "已退出（未取到返回码）"
    if sys.platform == "win32" and code < 0:
        u = code & 0xFFFFFFFF
        return f"已退出 · 返回码 {code}（无符号 {u} / 0x{u:08X}；请对照 Gemma 控制台完整报错）"
    return f"已退出 · 返回码 {code}"


def _windows_exit_hint_lines(nt: int | None) -> list[str]:
    if nt != 0xC0000005:
        return []
    return [
        "",
        "[说明] 返回码对应 Windows 访问冲突（0xC0000005），进程在加载/推理时崩溃，不是 Python 普通异常。",
        "常见原因：显卡驱动或 CUDA 与当前 PyTorch 不匹配、显存不足、5090/新卡需较新 torch/bitsandbytes。",
        "若同时看到「Thread … _readerthread / UnicodeDecodeError」，多为日志管道编码问题，可忽略；仍以本访问冲突为准。",
        "建议：在本项目目录双击运行 SERVE_GEMMA4_4BIT.bat（会打开控制台），查看完整报错；",
        "按 requirements 说明重装 CUDA 版 torch，或暂时降低 --max-model-len、换小模型试。",
        "",
    ]


class LocalInferenceBackend:
    """本机推理子进程与偏好；「本地对话 → 推理服务」与「运行」页快捷条共用同一状态。"""

    PRESET_GEMMA4 = "gemma4"
    PRESET_OLLAMA = "ollama"
    PRESET_LMSTUDIO = "lmstudio"
    PRESET_CUSTOM = "custom"
    OLLAMA_BASE = "http://127.0.0.1:11434/v1"

    def __init__(self, app: App, chat_hub: LocalChatHub) -> None:
        self._app = app
        self._chat_hub = chat_hub
        self._proc: subprocess.Popen | None = None
        pre = self._load_prefs_dict()
        _def_pres = (
            self.PRESET_GEMMA4
            if (Path(__file__).resolve().parent / "serve_gemma4_4bit.py").is_file()
            else self.PRESET_OLLAMA
        )
        self.preset_var = tk.StringVar(
            master=app, value=str(pre.get("local_inf_preset") or _def_pres)
        )
        self.exe_var = tk.StringVar(master=app, value=str(pre.get("local_inf_exe") or ""))
        self.args_var = tk.StringVar(master=app, value=str(pre.get("local_inf_args") or ""))
        self.cwd_var = tk.StringVar(master=app, value=str(pre.get("local_inf_cwd") or ""))
        self.autofill_var = tk.BooleanVar(
            master=app, value=bool(pre.get("local_inf_autofill", True))
        )
        self.autofill_url_var = tk.StringVar(
            master=app,
            value=_local_openai_base_for_ui(str(pre.get("local_inf_autofill_url") or "")),
        )
        self._log_targets: list[Callable[[str], None]] = []
        self._status_targets: list[Callable[[str], None]] = []
        self._running_targets: list[Callable[[bool], None]] = []
        self._global_indicator_fn: Callable[[str], None] | None = None
        self._watch_after: str | None = None
        self._probe_serial = 0
        self._last_probe_mono = 0.0
        self._probe_interval_sec = 30.0

    def _local_dialog_probe_triple(self) -> tuple[str, str, str]:
        """探针用的 URL / 模型 / Key；外部监听 Gemma 端口时可回退默认。"""
        base = self._chat_hub._loc_base.get().strip()
        model = self._chat_hub._loc_model.get().strip()
        key = self._chat_hub._loc_key.get().strip()
        preset = self.preset_var.get()
        if not base and preset == self.PRESET_GEMMA4 and _tcp_port_in_use("127.0.0.1", 18090):
            base = DEFAULT_LOCAL_OPENAI_BASE
        if not model and preset == self.PRESET_GEMMA4 and _tcp_port_in_use("127.0.0.1", 18090):
            model = DEFAULT_LOCAL_OPENAI_MODEL_ID
        if not base and preset == self.PRESET_OLLAMA and _tcp_port_in_use("127.0.0.1", 11434):
            base = self.OLLAMA_BASE
        if (
            not model
            and preset == self.PRESET_OLLAMA
            and base
            and _tcp_port_in_use("127.0.0.1", 11434)
        ):
            model = DEFAULT_OLLAMA_CHAT_MODEL_ID
        return base, model, key

    def _probe_status_prefixes(self) -> tuple[str, str]:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            pid = proc.pid
            return (
                f"运行中 · PID {pid}",
                f"本地推理运行中 · PID {pid}",
            )
        preset = self.preset_var.get()
        if preset == self.PRESET_GEMMA4 and _tcp_port_in_use("127.0.0.1", 18090):
            return (
                "未由本窗口托管 · 检测到 18090 有监听",
                "18090 已监听（外部 Gemma?）",
            )
        if preset == self.PRESET_OLLAMA and _tcp_port_in_use("127.0.0.1", 11434):
            return (
                "未由本窗口托管 · 检测到 11434 有监听",
                "11434 已监听（Ollama?）",
            )
        return ("", "")

    def _maybe_probe_health(self, *, force: bool) -> None:
        import time

        proc = self._proc
        port_relevant = False
        if proc is not None and proc.poll() is None:
            port_relevant = True
        else:
            preset = self.preset_var.get()
            if preset == self.PRESET_GEMMA4 and _tcp_port_in_use("127.0.0.1", 18090):
                port_relevant = True
            elif preset == self.PRESET_OLLAMA and _tcp_port_in_use("127.0.0.1", 11434):
                port_relevant = True
        if not port_relevant:
            return

        now = time.monotonic()
        if (
            not force
            and self._last_probe_mono > 0
            and (now - self._last_probe_mono) < self._probe_interval_sec
        ):
            return
        base, model, key = self._local_dialog_probe_triple()
        prefix, global_hint = self._probe_status_prefixes()
        if not prefix:
            return
        self._last_probe_mono = now
        self._probe_serial += 1
        serial = self._probe_serial
        self._emit_status(
            prefix
            + " · 探针进行中：已发测试请求，请稍候（冷启动常需数秒至一分钟；此阶段不是报错）…"
        )
        self._emit_global_indicator(f"{global_hint} · 探针等待中…")
        threading.Thread(
            target=self._run_health_probe,
            args=(serial, base, model, key, prefix, global_hint),
            daemon=True,
        ).start()

    def _run_health_probe(
        self,
        serial: int,
        base: str,
        model: str,
        key: str,
        prefix: str,
        global_hint: str,
    ) -> None:
        try:
            ok, msg = probe_local_openai_chat_health(base, model, key, timeout_sec=28)
        except Exception as e:
            ok, msg = False, str(e).split("\n")[0][:240]

        def apply() -> None:
            if self._probe_serial != serial:
                return
            if ok:
                detail = msg if len(msg) <= 72 else msg[:72] + "…"
                self._emit_status(f"{prefix} · ✓ {detail}")
                self._emit_global_indicator(f"{global_hint} · 接口就绪")
            else:
                short = msg if len(msg) <= 96 else msg[:96] + "…"
                self._emit_status(f"{prefix} · ✗ {short}")
                self._emit_global_indicator(f"{global_hint} · 接口异常")

        self._app.after(0, apply)

    def attach_global_indicator(self, fn: Callable[[str], None] | None) -> None:
        self._global_indicator_fn = fn
        if fn is not None:
            self.refresh_ui_state()

    def _emit_global_indicator(self, text: str) -> None:
        if self._global_indicator_fn is not None:
            try:
                self._global_indicator_fn(text)
            except tk.TclError:
                pass

    def _cancel_proc_watch(self) -> None:
        if self._watch_after is not None:
            try:
                self._app.after_cancel(self._watch_after)
            except tk.TclError:
                pass
            self._watch_after = None

    def _schedule_proc_watch(self) -> None:
        self._cancel_proc_watch()
        self._watch_after = self._app.after(2500, self._tick_proc_watch)

    def _tick_proc_watch(self) -> None:
        self._watch_after = None
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            self._maybe_probe_health(force=False)
            self._watch_after = self._app.after(2500, self._tick_proc_watch)
            return
        self._finalize_proc_exit(code, log_exit=True)

    def _finalize_proc_exit(self, code: int | None, *, log_exit: bool = True) -> None:
        if self._proc is None:
            return
        self._cancel_proc_watch()
        proc = self._proc
        self._proc = None
        if code is None:
            code = proc.poll()
        self._emit_running(False)
        self._emit_global_indicator("")
        st = "未运行" if code is None else _format_subprocess_exit_status(code)
        self._emit_status(st)
        if log_exit:
            self._emit_log(f"\n[gui] 进程已结束：{st}\n")
            if code is None:
                self._emit_log(
                    "[gui] 未能读取子进程退出码（可能异常崩溃）。"
                    "请在项目目录打开 CMD 运行："
                    "venv_gemma4\\Scripts\\python.exe -u serve_gemma4_4bit.py … "
                    "或运行：venv_gemma4\\Scripts\\python.exe check_local_model.py\n"
                )
            nt = _windows_exit_ntstatus(code)
            for ln in _windows_exit_hint_lines(nt):
                self._emit_log(ln + "\n")

    def refresh_ui_state(self) -> None:
        """切换页面或手动刷新时与子进程 / 本机端口状态对齐。"""
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                self._emit_running(True)
                self._emit_status(f"运行中 · PID {proc.pid}")
                self._emit_global_indicator(f"本地推理运行中 · PID {proc.pid}")
                self._schedule_proc_watch()
                self._maybe_probe_health(force=True)
            else:
                self._finalize_proc_exit(proc.poll(), log_exit=True)
            return
        self._emit_running(False)
        preset = self.preset_var.get()
        if preset == self.PRESET_GEMMA4 and _tcp_port_in_use("127.0.0.1", 18090):
            self._emit_status("未由本窗口托管 · 检测到 18090 有监听（可能已在外部启动 Gemma）")
            self._emit_global_indicator("18090 已监听（外部 Gemma?）")
            self._maybe_probe_health(force=True)
        elif preset == self.PRESET_OLLAMA and _tcp_port_in_use("127.0.0.1", 11434):
            self._emit_status("未由本窗口托管 · 检测到 11434 有监听（可能在运行 Ollama）")
            self._emit_global_indicator("11434 已监听（Ollama?）")
            self._maybe_probe_health(force=True)
        else:
            self._emit_global_indicator("")

    def add_log_target(self, fn: Callable[[str], None]) -> None:
        if fn not in self._log_targets:
            self._log_targets.append(fn)

    def remove_log_target(self, fn: Callable[[str], None]) -> None:
        try:
            self._log_targets.remove(fn)
        except ValueError:
            pass

    def add_status_target(self, fn: Callable[[str], None]) -> None:
        if fn not in self._status_targets:
            self._status_targets.append(fn)

    def remove_status_target(self, fn: Callable[[str], None]) -> None:
        try:
            self._status_targets.remove(fn)
        except ValueError:
            pass

    def add_running_target(self, fn: Callable[[bool], None]) -> None:
        if fn not in self._running_targets:
            self._running_targets.append(fn)

    def remove_running_target(self, fn: Callable[[bool], None]) -> None:
        try:
            self._running_targets.remove(fn)
        except ValueError:
            pass

    def _emit_log(self, s: str) -> None:
        for fn in list(self._log_targets):
            try:
                fn(s)
            except tk.TclError:
                pass

    def _emit_status(self, t: str) -> None:
        for fn in list(self._status_targets):
            try:
                fn(t)
            except tk.TclError:
                pass

    def _emit_running(self, running: bool) -> None:
        for fn in list(self._running_targets):
            try:
                fn(running)
            except tk.TclError:
                pass

    def _load_prefs_dict(self) -> dict:
        try:
            if LLM_GUI_PREF_JSON.is_file():
                raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return {}

    def save_prefs(self) -> None:
        m = self._load_prefs_dict()
        m.update(
            {
                "local_inf_preset": self.preset_var.get(),
                "local_inf_exe": self.exe_var.get().strip(),
                "local_inf_args": self.args_var.get().strip(),
                "local_inf_cwd": self.cwd_var.get().strip(),
                "local_inf_autofill": self.autofill_var.get(),
                "local_inf_autofill_url": self.autofill_url_var.get().strip(),
            }
        )
        try:
            LLM_GUI_PREF_JSON.write_text(
                json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def terminate_child_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            self._proc = None
            self._cancel_proc_watch()
            self._emit_global_indicator("")
            return
        self._cancel_proc_watch()
        if sys.platform == "win32":
            # 与流水线一致：仅 terminate/kill 根进程时，PyTorch/CUDA 等子进程常残留；须 taskkill /T。
            _terminate_process_tree(proc)
            try:
                proc.wait(timeout=12)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        self._proc = None
        self._emit_running(False)
        self._emit_global_indicator("")
        self._emit_status("未运行")

    def _launch_lm_studio(self) -> None:
        cands = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LM Studio" / "LM Studio.exe",
            Path(os.environ.get("ProgramFiles", "")) / "LM Studio" / "LM Studio.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "LM Studio" / "LM Studio.exe",
        ]
        for p in cands:
            if p.is_file():
                try:
                    subprocess.Popen([str(p)], cwd=str(p.parent))
                except OSError as e:
                    messagebox.showerror("失败", str(e), parent=self._app)
                    return
                self._emit_log(
                    f"[gui] 已启动 LM Studio：{p}\n请在软件内开启 Local Server，再把地址填到「服务 URL」。\n"
                )
                self._emit_status("已打开 LM Studio")
                return
        messagebox.showinfo(
            "未找到",
            "未在常见安装路径找到 LM Studio.exe。请手动打开 LM Studio，或改用 Ollama / 自定义。",
            parent=self._app,
        )

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            messagebox.showinfo("提示", "当前已有进程在运行，请先停止。", parent=self._app)
            return
        preset = self.preset_var.get()
        cmd: list[str]
        cwd: str | None = None
        if preset == self.PRESET_OLLAMA:
            exe = _resolve_ollama_executable()
            if not exe:
                messagebox.showerror(
                    "未找到 ollama",
                    "未在系统 PATH 及常见安装目录中找到 ollama.exe。\n"
                    "若已安装 Ollama，可改用「自定义」：程序选 ollama.exe，参数填 serve。\n"
                    "安装见 https://ollama.com",
                    parent=self._app,
                )
                return
            cmd = [exe, "serve"]
        elif preset == self.PRESET_LMSTUDIO:
            self.save_prefs()
            self._launch_lm_studio()
            return
        elif preset == self.PRESET_GEMMA4:
            script = Path(__file__).resolve().parent / "serve_gemma4_4bit.py"
            if not script.is_file():
                messagebox.showerror("未找到", f"缺少脚本：{script}", parent=self._app)
                return
            if sys.platform == "win32":
                py_exe = PROJECT_ROOT / "venv_gemma4" / "Scripts" / "python.exe"
            else:
                py_exe = PROJECT_ROOT / "venv_gemma4" / "bin" / "python3"
                if not py_exe.is_file():
                    py_exe = PROJECT_ROOT / "venv_gemma4" / "bin" / "python"
            if not py_exe.is_file():
                messagebox.showerror(
                    "未找到 venv_gemma4",
                    "请先双击运行 SERVE_GEMMA4_4BIT.bat，创建虚拟环境并安装依赖后，再点「一键 Gemma」启动。",
                    parent=self._app,
                )
                return
            model_dir = PROJECT_ROOT / "models" / "Gemma-4-31B-it-abliterated"
            if not model_dir.is_dir() or not (model_dir / "config.json").is_file():
                messagebox.showerror(
                    "未找到模型",
                    f"请确认模型目录存在且含 config.json：\n{model_dir}",
                    parent=self._app,
                )
                return
            if _tcp_port_in_use("127.0.0.1", 18090):
                messagebox.showerror(
                    "无法启动：端口 18090 已被占用",
                    "127.0.0.1:18090 上已有程序在监听。\n\n"
                    "若已在其它窗口或 bat 里跑着 Gemma，无需再点「一键 Gemma」；"
                    "直接在本程序「本地对话」里使用即可。\n\n"
                    "若仍要从此处启动，请先结束占用端口的进程（PowerShell："
                    "netstat -ano | findstr :18090，再用任务管理器结束对应 PID）。\n\n"
                    "说明：在端口已占用时强行启动会白白加载整份模型，最后在绑定端口时失败（错误 10048）。",
                    parent=self._app,
                )
                return
            gemma_args = [
                str(py_exe),
                "-u",
                "-m",
                "bilibili_vision.serve_gemma4_4bit",
                "--model",
                str(model_dir),
                "--host",
                "127.0.0.1",
                "--port",
                "18090",
                "--listen-model-id",
                "gemma-4-31b-4bit",
                "--max-model-len",
                "8192",
                "--default-temperature",
                "0",
                "--default-top-p",
                "0.82",
                "--repetition-penalty",
                "1.22",
                "--no-repeat-ngram",
                "6",
            ]
            # 直接启动 venv python -m，cwd 为仓库根；PYTHONPATH 在 sub_env 中注入 src。
            cmd = gemma_args
            cwd = str(PROJECT_ROOT)
        elif preset == self.PRESET_CUSTOM:
            exe = self.exe_var.get().strip().strip('"')
            if not exe:
                messagebox.showwarning("提示", "请填写可执行文件路径。", parent=self._app)
                return
            if not Path(exe).is_file():
                messagebox.showwarning("提示", f"找不到文件：{exe}", parent=self._app)
                return
            arg_s = self.args_var.get().strip()
            try:
                parts = shlex.split(arg_s, posix=(os.name != "nt")) if arg_s else []
            except ValueError as e:
                messagebox.showerror("参数解析失败", str(e), parent=self._app)
                return
            cmd = [exe] + parts
            cw = self.cwd_var.get().strip()
            if cw:
                if not Path(cw).is_dir():
                    messagebox.showwarning("提示", f"工作目录无效：{cw}", parent=self._app)
                    return
                cwd = cw
        else:
            messagebox.showerror("内部错误", f"未知预设：{preset!r}", parent=self._app)
            return

        self.save_prefs()
        sub_env = subprocess_env(os.environ.copy())
        sub_env.setdefault("PYTHONUTF8", "1")
        sub_env.setdefault("PYTHONIOENCODING", "utf-8")
        sub_env.setdefault("TQDM_ASCII", "1")
        sub_env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        sub_env.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        kw: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": sub_env,
        }
        if cwd:
            kw["cwd"] = cwd
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(cmd, **kw)
        except OSError as e:
            messagebox.showerror("启动失败", str(e), parent=self._app)
            return

        threading.Thread(target=self._drain_stdout, daemon=True).start()
        self._emit_running(True)
        self._emit_status(f"运行中 · PID {self._proc.pid}")
        self._emit_global_indicator(f"本地推理运行中 · PID {self._proc.pid}")
        self._schedule_proc_watch()
        if isinstance(cmd, list):
            log_cmd = (
                subprocess.list2cmdline(cmd)
                if sys.platform == "win32"
                else shlex.join(cmd)
            )
        else:
            log_cmd = str(cmd)
        self._emit_log("[gui] 已启动: " + log_cmd + "\n")
        if preset == self.PRESET_GEMMA4:
            self._emit_log(
                "[gui] Gemma 首次加载权重常需 1～3 分钟，此期间请勿重复点「启动」。"
                "若状态很快变为「未运行」但未见报错，多为子进程退出码尚未同步，已自动延后探针并改进回收逻辑；"
                "仍异常请用 CMD 运行 venv_gemma4\\Scripts\\python.exe -u serve_gemma4_4bit.py … 查看完整输出。\n"
            )
        probe_ms = 8000 if preset == self.PRESET_GEMMA4 else 900
        self._app.after(probe_ms, lambda: self._maybe_probe_health(force=True))
        if self.autofill_var.get():
            if preset == self.PRESET_OLLAMA:
                url = self.OLLAMA_BASE
            elif preset == self.PRESET_GEMMA4:
                url = DEFAULT_LOCAL_OPENAI_BASE
            else:
                url = self.autofill_url_var.get().strip().rstrip("/")
            if url:

                def _apply(u: str = url) -> None:
                    self._chat_hub._loc_base.set(u)

                self._app.after(500, _apply)
            if preset == self.PRESET_OLLAMA:

                def _apply_ollama_model() -> None:
                    if not self._chat_hub._loc_model.get().strip():
                        self._chat_hub._loc_model.set(DEFAULT_OLLAMA_CHAT_MODEL_ID)

                self._app.after(550, _apply_ollama_model)

    def _drain_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                text = (
                    _decode_subprocess_line(line)
                    if isinstance(line, (bytes, bytearray))
                    else str(line)
                )

                def _append(t: str = text) -> None:
                    self._emit_log(t)

                self._app.after(0, _append)
        finally:
            self._app.after(0, self._on_proc_finished)

    def _on_proc_finished(self) -> None:
        finishing = self._proc
        if finishing is None:
            return

        def reap() -> None:
            code = finishing.poll()
            if code is None:
                try:
                    code = finishing.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    code = finishing.poll()
            self._app.after(0, lambda c=code: self._apply_proc_reap(finishing, c))

        threading.Thread(target=reap, daemon=True).start()

    def _apply_proc_reap(self, finishing: subprocess.Popen, code: int | None) -> None:
        if self._proc is not finishing:
            return
        self._finalize_proc_exit(code, log_exit=True)

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            self._emit_log(
                "[gui] 当前没有由本窗口启动的推理子进程。"
                "若 18090 仍显示「有监听」，说明 Gemma 是在外部（如 SERVE_GEMMA4_4BIT.bat）跑的，"
                "请关闭那个控制台窗口，或在任务管理器中结束对应的 python.exe。\n"
            )
            return
        if proc.poll() is not None:
            if self._proc is proc:
                self._finalize_proc_exit(proc.poll(), log_exit=True)
            return
        self._cancel_proc_watch()
        try:
            proc.terminate()
        except OSError:
            pass
        code: int | None = None
        try:
            code = proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                code = proc.wait(timeout=5)
            except OSError:
                code = proc.poll()
        if self._proc is proc:
            self._finalize_proc_exit(code, log_exit=True)


class LocalInferenceFlowBar(ttk.Frame):
    """主界面「运行」页顶部：与推理服务页共享同一后端，便于打开软件即可启动本地模型。"""

    def __init__(self, parent: tk.Misc, infer: LocalInferenceBackend, app: App, *, pad: int) -> None:
        super().__init__(parent)
        self._infer = infer
        self._app = app
        lf = ttk.LabelFrame(self, text=" 本地推理服务 ", padding=(pad, pad))
        lf.pack(fill=tk.X)
        inf_row = ttk.Frame(lf)
        inf_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            inf_row,
            text="与「本地对话」共用 /v1 地址；URL 与模型在侧栏「本地对话」填写。可先启动再跑多模态。",
            foreground=T_MUTED,
        ).pack(side=tk.LEFT, anchor=tk.W, fill=tk.X, expand=True)
        tq_inf = ttk.Label(inf_row, text=" ?", foreground=T_MUTED, cursor="hand2")
        tq_inf.pack(side=tk.LEFT, padx=(4, 0))
        _attach_tooltip(tq_inf, lambda: LOCAL_INF_FLOW_HELP, wraplength=460)
        pf = ttk.Frame(lf)
        pf.pack(fill=tk.X, pady=(0, 6))
        for val, label in (
            (infer.PRESET_GEMMA4, "Gemma 4-bit"),
            (infer.PRESET_OLLAMA, "Ollama"),
            (infer.PRESET_LMSTUDIO, "LM Studio"),
            (infer.PRESET_CUSTOM, "自定义"),
        ):
            ttk.Radiobutton(
                pf,
                text=label,
                value=val,
                variable=infer.preset_var,
                command=self._sync_custom_hint,
            ).pack(side=tk.LEFT, padx=(0, 12))
        self._custom_hint = ttk.Label(
            lf,
            text="",
            wraplength=max(300, int(660 * app._geom_scale)),
            foreground=T_MUTED,
        )
        self._custom_hint.pack(anchor=tk.W, pady=(0, 6))
        ttk.Checkbutton(
            lf,
            text="启动后写入「本地对话」服务 URL",
            variable=infer.autofill_var,
        ).pack(anchor=tk.W, pady=(0, 8))
        bar = ttk.Frame(lf)
        bar.pack(fill=tk.X)
        ttk.Button(
            bar,
            text="刷新状态",
            command=infer.refresh_ui_state,
            style=app._secondary_button_style,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self._btn_start = ttk.Button(bar, text="启动", command=infer.start)
        self._btn_start.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_stop = ttk.Button(bar, text="停止", command=infer.stop, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            bar,
            text="填写 URL/模型…",
            command=self._open_local_chat_for_url_model,
            style=app._secondary_button_style,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            bar,
            text="完整面板与日志…",
            command=self._open_full_panel,
            style=app._secondary_button_style,
        ).pack(side=tk.LEFT, padx=(0, 12))
        self._status = ttk.Label(bar, text="未运行")
        self._status.pack(side=tk.LEFT, padx=(8, 0))

        infer.add_status_target(self._on_status)
        infer.add_running_target(self._on_running)
        infer.preset_var.trace_add("write", lambda *_: self._sync_custom_hint())
        self._sync_custom_hint()
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._infer.remove_status_target(self._on_status)
        self._infer.remove_running_target(self._on_running)

    def _on_status(self, t: str) -> None:
        try:
            self._status.configure(text=t)
        except tk.TclError:
            pass

    def _on_running(self, running: bool) -> None:
        try:
            self._btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
            self._btn_start.configure(state=tk.DISABLED if running else tk.NORMAL)
        except tk.TclError:
            pass

    def _sync_custom_hint(self) -> None:
        if self._infer.preset_var.get() == self._infer.PRESET_CUSTOM:
            self._custom_hint.configure(
                text="自定义需在「本地对话 → 推理服务」中填写程序路径与参数；此处与完整面板共用同一套设置。"
            )
        else:
            self._custom_hint.configure(text="")

    def _open_local_chat_for_url_model(self) -> None:
        self._app._show_view("local_chat")
        self._infer._chat_hub._show_local_chat_tab()

    def _open_full_panel(self) -> None:
        self._app._show_view("local_chat")
        self._infer._chat_hub._show_local_inference_tab()


class LocalInferenceServerPanel(ttk.Frame):
    """在「本地对话」页内启动本机推理进程（如 Gemma serve / ollama），显示输出并可选回填服务 URL。"""

    def __init__(self, parent: tk.Misc, infer: LocalInferenceBackend) -> None:
        super().__init__(parent)
        self._infer = infer
        app = infer._app

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            outer,
            text="日志在下方；关主窗口会结束由本窗口启动的子进程。顶部状态栏右侧可全局查看是否在运行；切换页面时点「刷新状态」可对齐界面。"
            " 「服务 URL」「模型 ID」在上方「对话」子标签里填写（本页「推理服务」不显示这两项）。"
            " 点「刷新状态」后若出现「探针进行中…请稍候」，表示正在请求模型，请等待出现「✓」或「✗」，勿当作已失败。",
            wraplength=520,
            foreground=T_MUTED,
        ).pack(anchor=tk.W, pady=(0, 8))

        pf = ttk.LabelFrame(outer, text="方式", padding=(8, 6))
        pf.pack(fill=tk.X, pady=(0, 8))
        for val, label in (
            (infer.PRESET_GEMMA4, "Gemma 4-bit（本目录 serve，默认 18090）"),
            (infer.PRESET_OLLAMA, "Ollama serve"),
            (infer.PRESET_LMSTUDIO, "打开 LM Studio"),
            (infer.PRESET_CUSTOM, "自定义可执行文件"),
        ):
            ttk.Radiobutton(
                pf, text=label, value=val, variable=infer.preset_var, command=self._sync_preset_ui
            ).pack(anchor=tk.W, pady=2)

        self._custom_fr = ttk.Frame(outer)
        r1 = ttk.Frame(self._custom_fr)
        r1.pack(fill=tk.X)
        ttk.Label(r1, text="程序", width=8).pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=infer.exe_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(r1, text="浏览…", command=self._browse_exe, width=8).pack(side=tk.LEFT)
        r2 = ttk.Frame(self._custom_fr)
        r2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r2, text="参数").pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=infer.args_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        r3 = ttk.Frame(self._custom_fr)
        r3.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r3, text="工作目录").pack(side=tk.LEFT)
        ttk.Entry(r3, textvariable=infer.cwd_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        af = ttk.Frame(outer)
        af.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(
            af, text="启动后回填对话页 URL", variable=infer.autofill_var
        ).pack(anchor=tk.W)
        ttk.Label(
            af,
            text="自定义预设时的 Base URL",
            foreground=T_MUTED,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Entry(af, textvariable=infer.autofill_url_var).pack(fill=tk.X, pady=(2, 0))

        bar = ttk.Frame(outer)
        bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(bar, text="刷新状态", command=infer.refresh_ui_state).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        self._btn_start = ttk.Button(bar, text="启动", command=infer.start)
        self._btn_start.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_stop = ttk.Button(bar, text="停止", command=infer.stop, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT)
        self._status = ttk.Label(bar, text="未运行")
        self._status.pack(side=tk.LEFT, padx=(16, 0))

        log_fr = ttk.Frame(outer)
        log_fr.pack(fill=tk.BOTH, expand=True)
        ttk.Label(log_fr, text="进程输出").pack(anchor=tk.W)
        sb = ttk.Scrollbar(log_fr)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log = tk.Text(
            log_fr,
            height=14,
            wrap=tk.WORD,
            font=app._font_log,
            bg=T_PANEL,
            fg=T_TEXT,
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=T_BORDER,
        )
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.configure(command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        bind_text_mousewheel(self._log, lines_per_notch=app._text_wheel_lines)

        self._sync_preset_ui()
        infer.add_log_target(self._log_line)
        infer.add_status_target(self._set_status)
        infer.add_running_target(self._on_running)
        self.bind("<Destroy>", self._on_panel_destroy)

    def _on_panel_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._infer.remove_log_target(self._log_line)
        self._infer.remove_status_target(self._set_status)
        self._infer.remove_running_target(self._on_running)
        self._infer.save_prefs()

    def _sync_preset_ui(self) -> None:
        p = self._infer.preset_var.get()
        if p == self._infer.PRESET_CUSTOM:
            self._custom_fr.pack(fill=tk.X, pady=(0, 6))
        else:
            self._custom_fr.pack_forget()

    def _browse_exe(self) -> None:
        p = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="选择可执行文件",
            filetypes=[("可执行文件", "*.exe;*.bat;*.cmd"), ("全部", "*.*")],
        )
        if p:
            self._infer.exe_var.set(p)

    def _log_line(self, s: str) -> None:
        try:
            self._log.insert(tk.END, s)
            self._log.see(tk.END)
        except tk.TclError:
            pass

    def _set_status(self, t: str) -> None:
        try:
            self._status.configure(text=t)
        except tk.TclError:
            pass

    def _on_running(self, running: bool) -> None:
        try:
            self._btn_stop.configure(state=tk.NORMAL if running else tk.DISABLED)
            self._btn_start.configure(state=tk.DISABLED if running else tk.NORMAL)
        except tk.TclError:
            pass


class LocalChatHub(ttk.Frame):
    """对话：可选「本地 OpenAI 兼容」或「云端 API（与 API 与模型页一致）」；侧栏会话、多轮、本地模式可附图。"""

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent)
        self._app = app
        self._busy = False
        self._current_sid: str | None = None
        self._meta: dict = {}
        self._pending_names: list[str] = []
        self._lb_ids: list[str] = []
        self._typing_row: tk.Frame | None = None
        self._wrap_labels: list[tuple[tk.Misc, str]] = []
        self._title_var = tk.StringVar(value="新对话")
        self._include_merged = tk.BooleanVar(value=False)
        self._hub_route = tk.StringVar(value="local")
        _hub_saved = _load_llm_gui_prefs_merged().get("local_chat_hub_backend_saved")
        if _hub_saved in ("local", "api"):
            self._hub_route.set(str(_hub_saved))
        self._loc_base = tk.StringVar(value="")
        self._loc_model = tk.StringVar(value="")
        self._loc_key = tk.StringVar(value="")
        # 本地大模型流式 SSE 易造成界面频繁 after 与「卡住」感；默认关流式，需要时再开。
        self._loc_stream = tk.BooleanVar(value=False)
        self._stream_pending = ""
        self._stream_after_id: str | None = None
        self._stream_body: tk.Text | None = None
        self._chat_cancel = threading.Event()

        self._font_small = app._font_hint

        top_nb = ttk.Notebook(self, style="Sticky.TNotebook")
        top_nb.pack(fill=tk.BOTH, expand=True)
        tab_chat = ttk.Frame(top_nb, padding=0)
        tab_srv = ttk.Frame(top_nb, padding=0)
        top_nb.add(tab_chat, text=" 对话 ")
        top_nb.add(tab_srv, text=" 推理服务 ")
        self._local_hub_nb = top_nb
        self._infer = LocalInferenceBackend(app, self)
        LocalInferenceServerPanel(tab_srv, self._infer).pack(
            fill=tk.BOTH, expand=True, padx=0, pady=0
        )

        shell = tk.Frame(tab_chat, bg=T_PAGE)
        shell.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(shell, bg=NAV_BG, width=int(236 * min(app._geom_scale, 1.35)))
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)
        right = tk.Frame(shell, bg=T_PAGE)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right_body = tk.Frame(right, bg=T_PAGE)
        right_body.pack(fill=tk.BOTH, expand=True, padx=(10, 16), pady=(10, 12))
        pv = ttk.Panedwindow(right_body, orient=tk.VERTICAL)
        pv.pack(fill=tk.BOTH, expand=True)
        settings_pane = tk.Frame(pv, bg=T_PAGE)
        chat_stack = tk.Frame(pv, bg=T_PAGE)
        chat_stack.grid_rowconfigure(0, weight=1)
        chat_stack.grid_columnconfigure(0, weight=1)
        pv.add(settings_pane, weight=0)
        pv.add(chat_stack, weight=1)

        settings_card = tk.Frame(
            settings_pane,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        settings_card.pack(fill=tk.BOTH, expand=False, pady=(0, 10))
        settings_inner = tk.Frame(settings_card, bg=T_PANEL)
        settings_inner.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)

        tk.Label(
            left,
            text="会话",
            font=app._font_hint,
            bg=NAV_BG,
            fg=T_MUTED,
        ).pack(anchor=tk.W, padx=12, pady=(10, 4))
        nb_row = tk.Frame(left, bg=NAV_BG)
        nb_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Button(nb_row, text="＋ 新对话", command=self._new_session).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        ttk.Button(nb_row, text="删除", command=self._delete_current, width=6).pack(
            side=tk.LEFT
        )
        lb_fr = tk.Frame(left, bg=NAV_BG)
        lb_fr.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 12))
        sb_l = tk.Scrollbar(
            lb_fr,
            orient=tk.VERTICAL,
            width=app._scrollbar_px,
            troughcolor=SCROLL_TROUGH,
        )
        self._listbox = tk.Listbox(
            lb_fr,
            activestyle="dotbox",
            bg=NAV_ACTIVE_BG,
            fg=T_TEXT,
            selectbackground=T_SELECT,
            highlightthickness=0,
            borderwidth=0,
            font=app._font_ui,
        )
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_l.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.configure(yscrollcommand=sb_l.set)
        sb_l.configure(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_sidebar_select)

        head = tk.Frame(settings_inner, bg=T_PANEL)
        head.pack(fill=tk.X, pady=(0, 10))
        tk.Label(head, text="标题", bg=T_PANEL, fg=T_MUTED, font=self._font_small).pack(
            side=tk.LEFT, padx=(0, 10)
        )
        ttk.Entry(head, textvariable=self._title_var, width=36).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        self._title_var.trace_add("write", lambda *_: self._debounced_save_title())

        route_fr = ttk.Frame(settings_inner)
        route_fr.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(route_fr, text="对话后端").pack(anchor=tk.W)
        rr = ttk.Frame(route_fr)
        rr.pack(fill=tk.X, pady=(4, 0))
        ttk.Radiobutton(
            rr,
            text="本地（OpenAI 兼容，如 Gemma / Ollama）",
            value="local",
            variable=self._hub_route,
            command=self._on_hub_route_change,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            rr,
            text="云端 API（与「API 与模型」首选提供商一致）",
            value="api",
            variable=self._hub_route,
            command=self._on_hub_route_change,
        ).pack(anchor=tk.W, pady=(2, 0))
        self._hub_route_hint = tk.Label(
            route_fr,
            text="",
            bg=T_PANEL,
            fg=T_MUTED,
            font=self._font_small,
            wraplength=480,
            justify=tk.LEFT,
        )
        self._hub_route_hint.pack(anchor=tk.W, pady=(6, 0))

        api_row = ttk.Frame(settings_inner, padding=(0, 0, 0, 4))
        api_row.pack(fill=tk.X)
        row_gap = max(8, int(round(10 * app._font_soft)))
        ttk.Label(api_row, text="服务 URL").grid(row=0, column=0, sticky=tk.W)
        self._e_hub_url = ttk.Entry(api_row, textvariable=self._loc_base, width=52)
        self._e_hub_url.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        ttk.Label(api_row, text="模型 ID").grid(row=1, column=0, sticky=tk.W, pady=(row_gap, 0))
        self._e_hub_model = ttk.Entry(api_row, textvariable=self._loc_model, width=28)
        self._e_hub_model.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=(row_gap, 0))
        ttk.Label(api_row, text="API Key").grid(row=2, column=0, sticky=tk.W, pady=(row_gap, 0))
        self._e_hub_key = ttk.Entry(api_row, textvariable=self._loc_key, width=36, show="•")
        self._e_hub_key.grid(row=2, column=1, sticky="ew", padx=(10, 0), pady=(row_gap, 0))
        api_row.columnconfigure(1, weight=1)
        srv_row = ttk.Frame(settings_inner)
        srv_row.pack(fill=tk.X, pady=(max(4, row_gap // 2), 0))
        ttk.Button(
            srv_row,
            text="打开「推理服务」标签",
            command=self._show_local_inference_tab,
            style=app._secondary_button_style,
        ).pack(anchor=tk.W)
        opt_row = tk.Frame(settings_inner, bg=T_PANEL)
        opt_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Checkbutton(
            opt_row,
            text="附带合并文稿（transcript_merged.txt）",
            variable=self._include_merged,
        ).pack(anchor=tk.W)

        chat_row = tk.Frame(chat_stack, bg=T_PAGE)
        chat_row.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        chat_row.grid_rowconfigure(0, weight=1)
        chat_row.grid_columnconfigure(0, weight=1)
        y_inc = max(18, int(round(app._font_content[1] * 2.4)))
        self._chat_canvas = tk.Canvas(
            chat_row,
            bg=T_SURFACE,
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=y_inc,
        )
        sb = tk.Scrollbar(
            chat_row,
            command=self._chat_canvas.yview,
            width=app._scrollbar_px,
            troughcolor=SCROLL_TROUGH,
        )
        self._chat_canvas.configure(yscrollcommand=sb.set)
        self._msgs = tk.Frame(self._chat_canvas, bg=T_SURFACE)
        self._msg_win = self._chat_canvas.create_window((0, 0), window=self._msgs, anchor="nw")

        def _sync(_e: object | None = None) -> None:
            self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))

        def _on_cv_cfg(event: tk.Misc) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 8:
                self._chat_canvas.itemconfigure(self._msg_win, width=w)
                self._refresh_bubble_wrap(w)

        self._msgs.bind("<Configure>", lambda e: _sync())
        self._chat_canvas.bind("<Configure>", _on_cv_cfg)
        for w in (self._chat_canvas, self._msgs):
            w.bind("<MouseWheel>", self._on_chat_wheel)
            w.bind("<Button-4>", self._on_chat_wheel)
            w.bind("<Button-5>", self._on_chat_wheel)
        self._chat_canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        comp_outer = tk.Frame(chat_stack, bg=T_PAGE)
        comp_outer.grid(row=1, column=0, sticky="ew")
        self._composer = tk.Frame(
            comp_outer,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        self._composer.pack(fill=tk.X, ipadx=6, ipady=6)
        inp_host = tk.Frame(self._composer, bg=COMPOSER_INPUT_BG)
        inp_host.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        top_c = tk.Frame(inp_host, bg=COMPOSER_INPUT_BG)
        top_c.pack(fill=tk.BOTH, expand=True)
        ttk.Button(top_c, text="＋", width=3, command=self._pick_images).pack(
            side=tk.LEFT, padx=(0, 8), pady=(2, 6), anchor=tk.N
        )
        self._inp = tk.Text(
            top_c,
            height=4,
            wrap=tk.WORD,
            font=app._font_content,
            padx=12,
            pady=10,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=COMPOSER_INPUT_BG,
            fg=A_TEXT,
            insertbackground=A_TEXT,
        )
        self._inp.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bind_text_mousewheel(self._inp, lines_per_notch=app._text_wheel_lines)
        self._attach_lbl = tk.Label(
            inp_host,
            text="",
            font=self._font_small,
            bg=COMPOSER_INPUT_BG,
            fg=A_META,
            anchor=tk.W,
        )
        self._attach_lbl.pack(fill=tk.X, padx=(4, 8), pady=(0, 6))
        bar_row = tk.Frame(self._composer, bg=COMPOSER_TOOLBAR_BG)
        bar_row.pack(fill=tk.X, padx=8, pady=(4, 8))
        left_bar = tk.Frame(bar_row, bg=COMPOSER_TOOLBAR_BG)
        left_bar.pack(side=tk.LEFT, fill=tk.Y)
        self._stream_chk = ttk.Checkbutton(
            left_bar,
            text="流式 SSE（仅本地）",
            variable=self._loc_stream,
        )
        self._stream_chk.pack(side=tk.LEFT, padx=(4, 12))
        self._status = tk.Label(
            left_bar,
            text="Ctrl+Enter 发送",
            font=app._font_hint,
            bg=COMPOSER_TOOLBAR_BG,
            fg=A_META,
            anchor=tk.W,
        )
        self._status.pack(side=tk.LEFT)
        right_bar = tk.Frame(bar_row, bg=COMPOSER_TOOLBAR_BG)
        right_bar.pack(side=tk.RIGHT, fill=tk.Y)
        self._btn_stop = ttk.Button(
            right_bar,
            text="停止",
            command=self._on_cancel_chat,
            style="Stop.TButton",
            state=tk.DISABLED,
        )
        self._btn_stop.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            right_bar,
            text="发送",
            command=self._on_send,
            style=app._run_button_style,
            width=7,
        ).pack(side=tk.RIGHT)
        ttk.Button(
            right_bar,
            text="清除附件",
            command=self._clear_pending,
            style=app._secondary_button_style,
        ).pack(side=tk.RIGHT, padx=(6, 0))
        self._inp.bind("<Control-Return>", self._on_ctrl_enter)

        self._title_save_after: str | None = None
        self._sync_hub_route_ui()

    def _persist_local_chat_hub_backend(self) -> None:
        try:
            merged = _load_llm_gui_prefs_merged()
            merged["local_chat_hub_backend_saved"] = self._hub_route.get().strip()
            LLM_GUI_PREF_JSON.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _on_hub_route_change(self) -> None:
        self._persist_local_chat_hub_backend()
        self._sync_hub_route_ui()

    def _sync_hub_route_ui(self) -> None:
        api = self._hub_route.get().strip() == "api"
        st = tk.DISABLED if api else tk.NORMAL
        for w in (self._e_hub_url, self._e_hub_model, self._e_hub_key):
            try:
                w.configure(state=st)
            except tk.TclError:
                pass
        try:
            self._stream_chk.configure(state=tk.DISABLED if api else tk.NORMAL)
        except tk.TclError:
            pass
        if api:
            try:
                self._loc_stream.set(False)
            except tk.TclError:
                pass
            self._hub_route_hint.configure(
                text="将使用「API 与模型」里配置的 Key 与首选提供商；不支持本页图片附件与流式。"
                " 若未点过「应用」，请先在该页填写并应用。"
            )
        else:
            self._hub_route_hint.configure(
                text="使用下方服务 URL / 模型；可勾选流式 SSE（大模型较慢时建议关闭流式以减少界面压力）。"
            )

    def _show_local_inference_tab(self) -> None:
        try:
            self._local_hub_nb.select(1)
        except (tk.TclError, AttributeError):
            pass

    def _show_local_chat_tab(self) -> None:
        try:
            self._local_hub_nb.select(0)
        except (tk.TclError, AttributeError):
            pass

    def on_tab_visible(self) -> None:
        self._sync_api_fields_from_app()
        self._refresh_sidebar()
        self._infer.refresh_ui_state()

    def _sync_api_fields_from_app(self) -> None:
        app = self._app
        if not self._loc_base.get().strip():
            b = (
                app._chat_local_base.get().strip()
                or app.vision_vlm_base_var.get().strip()
                or app._llm_openai_base.get().strip()
            )
            self._loc_base.set(_local_openai_base_for_ui(b or None))
        else:
            cur = self._loc_base.get().strip()
            nxt = _maybe_rewrite_lm_studio_local_url(cur) or cur
            if nxt != cur:
                self._loc_base.set(nxt)
        if not self._loc_model.get().strip():
            m = (
                app._chat_local_model.get().strip()
                or app.vision_vlm_model_var.get().strip()
                or app._llm_openai_model.get().strip()
                or DEFAULT_LOCAL_OPENAI_MODEL_ID
            )
            self._loc_model.set(m)
        if not self._loc_key.get().strip():
            k = (
                app._chat_local_key.get().strip()
                or app.vision_vlm_key_var.get().strip()
                or app._llm_openai_key.get().strip()
            )
            self._loc_key.set(k)

    def _debounced_save_title(self) -> None:
        if self._title_save_after:
            try:
                self._app.after_cancel(self._title_save_after)
            except tk.TclError:
                pass
        self._title_save_after = self._app.after(
            400, lambda: self._flush_title_to_meta()
        )

    def _flush_title_to_meta(self) -> None:
        self._title_save_after = None
        if not self._current_sid:
            return
        try:
            self._meta["title"] = self._title_var.get().strip() or "对话"
            self._save_meta()
            self._refresh_sidebar()
        except (OSError, TypeError, KeyError):
            pass

    def _session_dir(self, sid: str) -> Path:
        return LOCAL_CHAT_ROOT / sid

    def _meta_path(self, sid: str) -> Path:
        return self._session_dir(sid) / "meta.json"

    def _files_dir(self, sid: str) -> Path:
        return self._session_dir(sid) / "files"

    def _list_session_ids(self) -> list[str]:
        LOCAL_CHAT_ROOT.mkdir(parents=True, exist_ok=True)
        rows: list[tuple[str, str]] = []
        for p in LOCAL_CHAT_ROOT.iterdir():
            if not p.is_dir():
                continue
            mp = p / "meta.json"
            if not mp.is_file():
                continue
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                rows.append((str(m.get("updated") or ""), p.name))
            except (OSError, json.JSONDecodeError):
                rows.append(("", p.name))
        rows.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in rows]

    def _refresh_sidebar(self) -> None:
        self._lb_ids = self._list_session_ids()
        self._listbox.delete(0, tk.END)
        for sid in self._lb_ids:
            try:
                m = json.loads(self._meta_path(sid).read_text(encoding="utf-8"))
                title = str(m.get("title") or sid)[:48]
            except (OSError, json.JSONDecodeError):
                title = sid
            self._listbox.insert(tk.END, title)
        if self._current_sid and self._current_sid in self._lb_ids:
            idx = self._lb_ids.index(self._current_sid)
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.see(idx)

    def _select_sid_in_list(self, sid: str) -> None:
        if sid in self._lb_ids:
            idx = self._lb_ids.index(sid)
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.see(idx)

    def _on_sidebar_select(self, _event: object) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        i = int(sel[0])
        if 0 <= i < len(self._lb_ids):
            self._load_session(self._lb_ids[i])

    def _new_session(self) -> None:
        sid = uuid.uuid4().hex[:12]
        d = self._session_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        self._files_dir(sid).mkdir(exist_ok=True)
        self._meta = {
            "id": sid,
            "title": "新对话",
            "updated": datetime.now().isoformat(timespec="seconds"),
            "include_merged": False,
            "turns": [],
        }
        self._meta_path(sid).write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._current_sid = sid
        self._title_var.set("新对话")
        self._include_merged.set(False)
        self._pending_names.clear()
        self._clear_chat_surface()
        self._refresh_sidebar()
        self._select_sid_in_list(sid)

    def _load_session(self, sid: str) -> None:
        try:
            self._meta = json.loads(self._meta_path(sid).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            messagebox.showerror("错误", "无法读取会话文件。")
            return
        self._current_sid = sid
        self._title_var.set(str(self._meta.get("title") or "对话"))
        self._include_merged.set(bool(self._meta.get("include_merged")))
        self._pending_names.clear()
        self._clear_chat_surface()
        for t in self._meta.get("turns") or []:
            imgs = t.get("imgs") or []
            self._add_user_bubble(str(t.get("u") or ""), [str(x) for x in imgs])
            self._add_assistant_bubble(str(t.get("a") or ""))
        self._scroll_bottom()

    def _save_meta(self) -> None:
        if not self._current_sid:
            return
        self._meta["id"] = self._current_sid
        self._meta["title"] = self._title_var.get().strip() or "对话"
        self._meta["include_merged"] = bool(self._include_merged.get())
        self._meta["updated"] = datetime.now().isoformat(timespec="seconds")
        self._meta.setdefault("turns", [])
        self._meta_path(self._current_sid).write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _ensure_session(self) -> str:
        if self._current_sid and self._meta_path(self._current_sid).is_file():
            return self._current_sid
        self._new_session()
        return self._current_sid or ""

    def _delete_current(self) -> None:
        if not self._current_sid:
            messagebox.showinfo("提示", "请先选择一个会话。")
            return
        if not messagebox.askyesno("确认", "删除当前会话及其中的附件图片？"):
            return
        shutil.rmtree(self._session_dir(self._current_sid), ignore_errors=True)
        self._current_sid = None
        self._meta = {}
        self._pending_names.clear()
        self._clear_chat_surface()
        self._title_var.set("新对话")
        self._refresh_sidebar()

    def _clear_chat_surface(self) -> None:
        self._hide_typing()
        self._wrap_labels.clear()
        for w in self._msgs.winfo_children():
            w.destroy()

    def _clear_pending(self) -> None:
        self._pending_names.clear()
        self._refresh_attach_label()

    def _refresh_attach_label(self) -> None:
        if self._pending_names:
            self._attach_lbl.configure(
                text="已选附件：" + "、".join(self._pending_names[:8])
                + (" …" if len(self._pending_names) > 8 else "")
            )
        else:
            self._attach_lbl.configure(text="")

    def _pick_images(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self._app,
            title="选择图片",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
                ("全部", "*.*"),
            ],
        )
        if not paths:
            return
        sid = self._ensure_session()
        fd = self._files_dir(sid)
        fd.mkdir(parents=True, exist_ok=True)
        for src in paths:
            sp = Path(src)
            if not sp.is_file():
                continue
            ext = sp.suffix.lower() or ".png"
            dest = fd / f"img_{uuid.uuid4().hex[:10]}{ext}"
            try:
                shutil.copy2(sp, dest)
                self._pending_names.append(dest.name)
            except OSError as e:
                messagebox.showerror("复制失败", str(e))
        self._refresh_attach_label()

    def _bubble_dims(self, canvas_w: int) -> tuple[int, int]:
        wu = max(200, int((canvas_w - 72) * 0.70))
        wa = max(240, int((canvas_w - 40) * 0.92))
        return wu, wa

    def _chars_for_wrap(self, px: int) -> int:
        f = tkfont.Font(font=self._app._font_content)
        w = max(f.measure("测"), f.measure("W"), 6)
        return max(12, int(px // w))

    def _bubble_text(
        self,
        parent: tk.Misc,
        content: str,
        *,
        bg: str,
        wrap_px: int,
    ) -> tk.Text:
        chars = self._chars_for_wrap(wrap_px)
        t = tk.Text(
            parent,
            wrap=tk.WORD,
            width=chars,
            height=1,
            font=self._app._font_content,
            bg=bg,
            fg=A_TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=4,
            cursor="xterm",
            undo=False,
            maxundo=0,
        )
        t.insert("1.0", (content.strip() or "\u00a0"))
        lines = int(t.index("end-1c").split(".")[0])
        t.configure(height=max(1, lines))

        def on_key(event: tk.Event) -> str | None:
            st = int(getattr(event, "state", 0) or 0)
            sym = (event.keysym or "").lower()
            if st & 0x4 and sym in ("a", "c"):
                return None
            return "break"

        t.bind("<Key>", on_key, add=True)
        t.bind("<<Paste>>", lambda _e: "break", add=True)
        return t

    def _refresh_bubble_wrap(self, canvas_w: int) -> None:
        wu, wa = self._bubble_dims(canvas_w)
        for w, role in self._wrap_labels:
            if isinstance(w, tk.Text):
                px = wu if role == "user" else wa
                w.configure(width=self._chars_for_wrap(px))
                try:
                    lines = int(w.index("end-1c").split(".")[0])
                    w.configure(height=max(1, lines))
                except tk.TclError:
                    pass

    def _on_chat_wheel(self, event: tk.Event) -> None:
        if _widget_under_ancestor(getattr(event, "widget", None), self._composer):
            return
        lines = self._app._text_wheel_lines
        d = getattr(event, "delta", 0) or 0
        if d:
            steps = int(-d * lines / 120.0) if sys.platform == "win32" else int(-d * lines / 120.0)
            if steps == 0:
                steps = -lines if d > 0 else lines
            if steps:
                self._chat_canvas.yview_scroll(steps, "units")
            return
        n = getattr(event, "num", 0)
        if n == 4:
            self._chat_canvas.yview_scroll(-lines, "units")
        elif n == 5:
            self._chat_canvas.yview_scroll(lines, "units")

    def _scroll_bottom(self) -> None:
        self.update_idletasks()
        self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))
        self._chat_canvas.yview_moveto(1.0)

    def _add_user_bubble(self, text: str, img_names: list[str]) -> None:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        ts = datetime.now().strftime("%H:%M")
        meta = tk.Frame(row, bg=z)
        meta.pack(fill=tk.X, padx=(0, 12))
        tk.Label(
            meta,
            text=f"You  ·  {ts}",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(side=tk.RIGHT)
        bubble = tk.Frame(row, bg=A_USER)
        bubble.pack(anchor=tk.E, padx=(40, 8), pady=(2, 4))
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        wu, _ = self._bubble_dims(cw)
        body = self._bubble_text(bubble, text, bg=A_USER, wrap_px=wu)
        body.pack(padx=12, pady=10, anchor=tk.W)
        self._wrap_labels.append((body, "user"))
        if img_names:
            tk.Label(
                bubble,
                text="📎 " + "、".join(img_names[:6])
                + (" …" if len(img_names) > 6 else ""),
                font=self._font_small,
                bg=A_USER,
                fg=A_META,
                anchor=tk.W,
            ).pack(anchor=tk.W, padx=12, pady=(0, 8))
        self._scroll_bottom()

    def _add_assistant_bubble(self, text: str, *, error: bool = False) -> None:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        tk.Label(
            row,
            text="Assistant",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(8, 0))
        bg = A_ERR if error else A_CARD
        border = "#fecaca" if error else A_CARD_BORDER
        card = tk.Frame(row, bg=bg, highlightbackground=border, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(8, 12), pady=(4, 6), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_dims(cw)
        body = self._bubble_text(card, text, bg=bg, wrap_px=wa)
        body.pack(padx=14, pady=12, anchor=tk.W, fill=tk.X)
        self._wrap_labels.append((body, "assistant"))
        self._scroll_bottom()

    def _show_typing(self) -> None:
        if self._typing_row is not None:
            return
        z = T_SURFACE
        self._typing_row = tk.Frame(self._msgs, bg=z)
        self._typing_row.pack(fill=tk.X, pady=(4, 8))
        tk.Label(
            self._typing_row,
            text="正在回复…",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(12, 0))
        self._scroll_bottom()

    def _hide_typing(self) -> None:
        if self._typing_row is not None:
            self._typing_row.destroy()
            self._typing_row = None

    def _reset_stream_coalesce(self) -> None:
        self._stream_pending = ""
        self._stream_body = None
        aid = self._stream_after_id
        self._stream_after_id = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _enqueue_stream_delta(self, body: tk.Text, chunk: str) -> None:
        self._stream_body = body
        self._stream_pending += chunk
        if self._stream_after_id is None:
            self._stream_after_id = self.after(
                100, lambda b=body: self._flush_stream_pending(b)
            )

    def _flush_stream_pending(self, body: tk.Text) -> None:
        self._stream_after_id = None
        chunk = self._stream_pending
        self._stream_pending = ""
        if not chunk:
            return
        try:
            cur = body.get("1.0", "end-1c")
            if cur.strip() in ("", "\u00a0"):
                body.delete("1.0", tk.END)
            body.insert(tk.END, chunk)
            lines = int(body.index("end-1c").split(".")[0])
            body.configure(height=max(1, lines))
            self._scroll_bottom()
        except tk.TclError:
            pass

    def _flush_stream_delta_buffer(self) -> None:
        b = self._stream_body
        if b is not None and self._stream_pending:
            self._flush_stream_pending(b)
        self._stream_pending = ""
        aid = self._stream_after_id
        self._stream_after_id = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _add_assistant_stream_shell(self) -> tk.Text:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        tk.Label(
            row,
            text="Assistant",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(8, 0))
        bg = A_CARD
        card = tk.Frame(row, bg=bg, highlightbackground=A_CARD_BORDER, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(8, 12), pady=(4, 6), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_dims(cw)
        body = self._bubble_text(card, "\u00a0", bg=bg, wrap_px=wa)
        body.pack(padx=14, pady=12, anchor=tk.W, fill=tk.X)
        self._wrap_labels.append((body, "assistant"))
        self._scroll_bottom()
        return body

    def _on_ctrl_enter(self, _event: object) -> str:
        self._on_send()
        return "break"

    def _on_cancel_chat(self) -> None:
        if not self._busy:
            return
        self._chat_cancel.set()
        try:
            self._status.configure(text="正在中止…")
        except tk.TclError:
            pass

    def _on_send(self) -> None:
        if self._busy:
            return
        text = self._inp.get("1.0", tk.END).strip()
        if not text and not self._pending_names:
            return

        use_api_hub = self._hub_route.get().strip() == "api"
        if use_api_hub and self._pending_names:
            messagebox.showwarning(
                "提示",
                "云端 API 模式不支持本页图片附件。请清除附件后重试，或切换到「本地」再发图。",
            )
            return

        if use_api_hub:
            self._app.prepare_env_for_hub_api_chat()
            prov = resolve_provider(self._app.llm_provider_var.get().strip())
            if not prov:
                messagebox.showwarning(
                    "无法发送",
                    "请先在「API 与模型」填写对应平台 Key 并点击「应用」，或将首选提供商改为「自动」。",
                )
                return
        else:
            base = self._loc_base.get().strip().rstrip("/")
            model = self._loc_model.get().strip()
            if not base:
                messagebox.showwarning(
                    "提示", f"请填写服务根 URL（例如 {DEFAULT_LOCAL_OPENAI_BASE}）。"
                )
                return
            if not model:
                messagebox.showwarning("提示", "请填写模型 ID（须与本地 serve 一致）。")
                return

        sid = self._ensure_session()
        fd = self._files_dir(sid)
        pending_paths = [fd / n for n in self._pending_names]
        prior: list[tuple[str, str, list[Path]]] = []
        for t in self._meta.get("turns") or []:
            names = [str(x) for x in (t.get("imgs") or []) if x]
            imgs = [fd / n for n in names]
            prior.append((str(t.get("u") or ""), str(t.get("a") or ""), imgs))
        sys_p = SYSTEM_LOCAL_CHAT_ZH
        merged_p = self._app.path_merged()
        if self._include_merged.get() and merged_p.is_file():
            body = merged_p.read_text(encoding="utf-8", errors="replace")
            if len(body) > MAX_LOCAL_CHAT_MERGED_SNIPPET:
                body = body[: MAX_LOCAL_CHAT_MERGED_SNIPPET] + "\n…（已截断）"
            sys_p += "\n\n【背景：视频合并文稿（可选参考）】\n" + body

        self._chat_cancel.clear()
        self._busy = True
        self._add_user_bubble(text, list(self._pending_names))
        self._inp.delete("1.0", tk.END)
        self._pending_names.clear()
        self._refresh_attach_label()
        self._status.configure(text="请求中…")
        try:
            self._btn_stop.configure(state=tk.NORMAL)
        except tk.TclError:
            pass
        for w in (self._inp,):
            try:
                w.configure(state=tk.DISABLED)
            except tk.TclError:
                pass

        if use_api_hub:
            self._show_typing()
            merged_full = ""
            if self._include_merged.get() and merged_p.is_file():
                merged_full = merged_p.read_text(encoding="utf-8", errors="replace")
            prior_api = [(u, a, bool(imgs)) for u, a, imgs in prior]
            threading.Thread(
                target=self._worker_send_api,
                args=(text, sid, prov, merged_full, prior_api),
                daemon=True,
            ).start()
            return

        base = self._loc_base.get().strip().rstrip("/")
        model = self._loc_model.get().strip()
        key = self._loc_key.get().strip() or "EMPTY"
        use_stream = bool(self._loc_stream.get())
        stream_body: tk.Text | None = None
        if use_stream:
            stream_body = self._add_assistant_stream_shell()
        else:
            self._show_typing()
        threading.Thread(
            target=self._worker_send,
            args=(text, pending_paths, prior, base, key, model, sys_p, sid, use_stream, stream_body),
            daemon=True,
        ).start()

    def _worker_send(
        self,
        text: str,
        pending_paths: list[Path],
        prior: list[tuple[str, str, list[Path]]],
        base: str,
        key: str,
        model: str,
        sys_p: str,
        sid: str,
        use_stream: bool,
        stream_body: tk.Text | None,
    ) -> None:
        try:
            names = [p.name for p in pending_paths if p.is_file()]
            if use_stream and stream_body is not None:
                def push(c: str) -> None:
                    self._app.after(
                        0, lambda c=c: self._enqueue_stream_delta(stream_body, c)
                    )

                reply, used = local_openai_compatible_chat_round(
                    base_url=base,
                    api_key=key,
                    model=model,
                    system_prompt=sys_p,
                    prior=prior,
                    user_text=text,
                    user_images=pending_paths,
                    stream=True,
                    on_delta=push,
                    cancel_event=self._chat_cancel,
                )
                self._app.after(0, self._flush_stream_delta_buffer)
                self._app.after(
                    0,
                    lambda: self._finish_ok(
                        text, reply, used, names, sid, stream_bubble=stream_body
                    ),
                )
            else:
                reply, used = local_openai_compatible_chat_round(
                    base_url=base,
                    api_key=key,
                    model=model,
                    system_prompt=sys_p,
                    prior=prior,
                    user_text=text,
                    user_images=pending_paths,
                    cancel_event=self._chat_cancel,
                )
                self._app.after(
                    0,
                    lambda: self._finish_ok(text, reply, used, names, sid),
                )
        except OpenAICompatibleRequestCancelled:
            self._app.after(
                0,
                lambda sb=stream_body: self._finish_cancelled(stream_bubble=sb),
            )
        except Exception as e:
            self._app.after(
                0,
                lambda err=str(e), sb=stream_body: self._finish_err(err, stream_bubble=sb),
            )

    def _worker_send_api(
        self,
        user_text: str,
        sid: str,
        provider: str,
        merged_text: str,
        prior_api: list[tuple[str, str, bool]],
    ) -> None:
        try:
            app = self._app
            analysis = ""
            ap = app.path_analysis()
            if ap.is_file():
                analysis = ap.read_text(encoding="utf-8", errors="replace")
            dp = app.path_analysis_deep()
            if dp.is_file():
                deep_raw = dp.read_text(encoding="utf-8", errors="replace")
                cap = 14_000
                if len(deep_raw) > cap:
                    deep_raw = deep_raw[:cap] + "\n…（深度分析节选已截断）\n"
                analysis = (
                    (analysis + "\n\n【深度内容分析节选】\n" + deep_raw).strip()
                    if analysis
                    else "【深度内容分析节选】\n" + deep_raw
                )
            reply, tag, _payload = chat_followup(
                provider,
                merged_text,
                user_text,
                analysis_excerpt=analysis or None,
                prior_turns=prior_api,
                attach_vision_current=False,
                vision_frame_paths=None,
            )
            self._app.after(
                0,
                lambda: self._finish_ok(user_text, reply, tag, [], sid),
            )
        except Exception as e:
            self._app.after(0, lambda err=str(e): self._finish_err(err))

    def _finish_ok(
        self,
        user_text: str,
        reply: str,
        used_model: str,
        img_names: list[str],
        sid: str,
        *,
        stream_bubble: tk.Text | None = None,
    ) -> None:
        self._reset_stream_coalesce()
        self._chat_cancel.clear()
        self._busy = False
        try:
            self._btn_stop.configure(state=tk.DISABLED)
        except tk.TclError:
            pass
        for w in (self._inp,):
            try:
                w.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        self._hide_typing()
        mp = self._meta_path(sid)
        try:
            disk = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            disk = {"id": sid, "title": "对话", "turns": []}
        disk.setdefault("turns", []).append(
            {"u": user_text, "a": reply, "imgs": img_names}
        )
        disk["updated"] = datetime.now().isoformat(timespec="seconds")
        turns = disk["turns"]
        if len(turns) == 1 and str(disk.get("title") or "") in ("", "新对话"):
            short = user_text.replace("\n", " ").strip()[:40]
            if short:
                disk["title"] = short + ("…" if len(user_text) > 40 else "")
        try:
            mp.write_text(
                json.dumps(disk, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

        if self._current_sid == sid:
            if stream_bubble is None:
                self._add_assistant_bubble(reply)
            self._meta = disk
            self._title_var.set(str(disk.get("title") or "对话"))
            self._save_meta()
            self._status.configure(text=f"就绪 · {used_model}")
        else:
            self._status.configure(text=f"就绪 · {used_model}（已写入其它会话）")
        self._refresh_sidebar()

    def _finish_err(self, err: str, *, stream_bubble: tk.Text | None = None) -> None:
        self._reset_stream_coalesce()
        self._chat_cancel.clear()
        self._busy = False
        try:
            self._btn_stop.configure(state=tk.DISABLED)
        except tk.TclError:
            pass
        for w in (self._inp,):
            try:
                w.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        self._hide_typing()
        if stream_bubble is not None:
            try:
                stream_bubble.master.master.destroy()
            except tk.TclError:
                pass
        self._add_assistant_bubble(err, error=True)
        self._status.configure(text="出错 · 见气泡内详情")

    def _finish_cancelled(self, *, stream_bubble: tk.Text | None = None) -> None:
        self._flush_stream_delta_buffer()
        self._reset_stream_coalesce()
        self._chat_cancel.clear()
        self._busy = False
        try:
            self._btn_stop.configure(state=tk.DISABLED)
        except tk.TclError:
            pass
        for w in (self._inp,):
            try:
                w.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        self._hide_typing()
        if stream_bubble is not None:
            try:
                stream_bubble.insert(tk.END, "\n\n[已中止]")
                lines = int(stream_bubble.index("end-1c").split(".")[0])
                stream_bubble.configure(height=max(1, lines))
            except tk.TclError:
                pass
            self._scroll_bottom()
        self._status.configure(text="已中止（未保存本轮）")


class AgentSession:
    """主界面「报告与对话」标签：共享会话与 API 轮次。"""

    def __init__(self, app: App) -> None:
        self._app = app
        app.prepare_chat_env_for_dialogue()
        self._provider = app.resolve_chat_provider_for_dialogue()
        self._turns: list[tuple[str, str, bool]] = []
        self._panels: list[AgentChatPanel] = []
        self._busy = False

    def attach(self, panel: AgentChatPanel) -> None:
        if panel not in self._panels:
            self._panels.append(panel)

    def detach(self, panel: AgentChatPanel) -> None:
        try:
            self._panels.remove(panel)
        except ValueError:
            pass

    def clear(self) -> None:
        self._turns.clear()
        self._busy = False
        for p in list(self._panels):
            try:
                p._reset_chat_ui()
            except tk.TclError:
                self.detach(p)

    def submit(self, user_visible: str) -> None:
        self._app.prepare_chat_env_for_dialogue()
        self._provider = self._app.resolve_chat_provider_for_dialogue()
        if self._provider is None or self._busy:
            if self._provider is None and not self._busy:
                if self._app.chat_route_var.get() == "local":
                    messagebox.showwarning(
                        "无法发送",
                        "已选择「本地 OpenAI 兼容」对话：请填写服务根 URL（须含 /v1）与模型 ID，"
                        "并点击「应用」。",
                    )
                else:
                    messagebox.showwarning(
                        "无法发送",
                        "请先在「API 与模型」填写对应平台的 Key 并点击「应用」，"
                        "或将「首选提供商」改为「自动」并至少填写一个平台的 Key。",
                    )
            return
        mp = self._app.path_merged()
        if not mp.is_file():
            messagebox.showwarning(
                "无法发送",
                "尚未生成合并文稿（当前输出目录下的 transcript_merged.txt）。\n"
                "请先在「内容提取与分析」→「流程」标签点击「开始：…」。",
            )
            return
        u = user_visible.strip()
        if not u:
            return
        self._busy = True
        for p in self._panels:
            p._inp.delete("1.0", tk.END)
        for p in self._panels:
            p._add_user_bubble(u)
        for p in self._panels:
            p._show_typing()
        for p in self._panels:
            p._send_btn.configure(state=tk.DISABLED)
            p._clear_btn.configure(state=tk.DISABLED)
            p._status.configure(text="请求中…")
        threading.Thread(target=self._worker, args=(u,), daemon=True).start()

    def _worker(self, user_visible: str) -> None:
        try:
            merged = self._app.path_merged().read_text(encoding="utf-8", errors="replace")
            analysis = ""
            ap = self._app.path_analysis()
            if ap.is_file():
                analysis = ap.read_text(encoding="utf-8", errors="replace")
            dp = self._app.path_analysis_deep()
            if dp.is_file():
                deep_raw = dp.read_text(encoding="utf-8", errors="replace")
                cap = 14_000
                if len(deep_raw) > cap:
                    deep_raw = deep_raw[:cap] + "\n…（深度分析节选已截断）\n"
                analysis = (
                    (analysis + "\n\n【深度内容分析节选】\n" + deep_raw).strip()
                    if analysis
                    else "【深度内容分析节选】\n" + deep_raw
                )
            try:
                ctx_mode = self._app.chat_vision_context_var.get().strip().lower()
            except (tk.TclError, AttributeError):
                ctx_mode = "auto"
            jp = self._app.path_analysis_deep_json()
            vision_paths = collect_timeline_frame_paths(jp)
            attach_vis = bool(self._app.report_chat_attach_vision_var.get())
            if ctx_mode == "auto" and jp.is_file():
                try:
                    jd = json.loads(jp.read_text(encoding="utf-8", errors="replace"))
                except (OSError, json.JSONDecodeError, TypeError):
                    jd = None
                if isinstance(jd, dict):
                    tl = jd.get("timeline")
                    if isinstance(tl, list) and tl:
                        parts: list[str] = []
                        for ev in tl[:24]:
                            if not isinstance(ev, dict):
                                continue
                            t = ev.get("t", 0)
                            o = str(ev.get("ocr", "") or "").strip()
                            v = str(ev.get("vlm", "") or "").strip()
                            if o or v:
                                parts.append(
                                    f"[{t}s] "
                                    + ("OCR:" + o if o else "")
                                    + (" " if o and v else "")
                                    + ("VLM:" + v if v else "")
                                )
                        if parts:
                            vis_blob = "\n".join(parts)
                            vcap = 8000
                            if len(vis_blob) > vcap:
                                vis_blob = vis_blob[:vcap] + "\n…（画面摘录已截断）\n"
                            analysis = (
                                (analysis + "\n\n【画面时间轴摘录】\n" + vis_blob).strip()
                                if analysis
                                else "【画面时间轴摘录】\n" + vis_blob
                            )
            reply, tag, payload = chat_followup(
                self._provider,
                merged,
                user_visible,
                analysis_excerpt=analysis or None,
                prior_turns=self._turns,
                attach_vision_current=attach_vis,
                vision_frame_paths=vision_paths,
            )
            self._turns.append((payload, reply, attach_vis))
            self._app.after(0, lambda r=reply, t=tag: self._finish_ok(r, t))
        except Exception as e:
            self._app.after(0, lambda err=str(e): self._finish_err(err))

    def _finish_ok(self, reply: str, tag: str) -> None:
        self._busy = False
        for p in self._panels:
            p._hide_typing()
            p._send_btn.configure(state=tk.NORMAL)
            p._clear_btn.configure(state=tk.NORMAL)
            p._status.configure(text=f"就绪 · {tag}")
            p._add_assistant_bubble(reply, f"助手 · {tag}")

    def _finish_err(self, err: str) -> None:
        self._busy = False
        for p in self._panels:
            p._hide_typing()
            p._send_btn.configure(state=tk.NORMAL)
            p._clear_btn.configure(state=tk.NORMAL)
            p._status.configure(text="出错 · 请重试")
            p._add_assistant_bubble(err, "Error", error=True)


class AgentChatPanel(tk.Frame):
    """「报告与对话」标签下半区（聊天记录 + 输入框）。"""

    def __init__(
        self,
        parent: tk.Misc,
        app: App,
        session: AgentSession,
        *,
        compact: bool = True,
    ) -> None:
        super().__init__(parent, bg=T_BG if compact else A_BG)
        self._app = app
        self._session = session
        self._compact = compact
        self._provider = session._provider
        self._wrap_labels: list[tuple[tk.Label, str]] = []
        self._typing_row: tk.Frame | None = None

        self._font_body = app._font_content
        self._font_small = app._font_hint

        pad_x = 0 if compact else 10
        pad_y = (0, 2) if compact else (8, 6)
        top = tk.Frame(self, bg=T_BG if compact else A_BG)
        top.pack(fill=tk.BOTH, expand=True, padx=pad_x, pady=pad_y)

        if self._provider is None:
            ttk.Label(
                top,
                text="未配置 API：请到「API 与模型」填写并应用。",
                wraplength=320,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, padx=4)
            return

        chat_bg = T_SURFACE if compact else A_BG
        self._chat_zone_bg = chat_bg
        chat_row = tk.Frame(top, bg=chat_bg)

        inp_h = 3
        szf = float(self._font_body[1])
        line_h = max(18, int(round(szf * 1.55)))
        composer_minsize = max(168, 18 + inp_h * line_h + 50)
        top.grid_rowconfigure(0, weight=1, minsize=40)
        top.grid_rowconfigure(1, weight=0, minsize=composer_minsize)
        top.grid_columnconfigure(0, weight=1)
        chat_row.grid(row=0, column=0, sticky="nsew")

        y_inc = max(18, int(round(app._font_content[1] * 2.4)))
        self._chat_canvas = tk.Canvas(
            chat_row,
            bg=chat_bg,
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=y_inc,
        )
        sb = tk.Scrollbar(
            chat_row,
            command=self._chat_canvas.yview,
            width=app._scrollbar_px,
            borderwidth=0,
            troughcolor=SCROLL_TROUGH,
            bg="#c4c4c4",
            activebackground="#a8a8a8",
            highlightthickness=0,
            relief="flat",
            jump=1,
        )
        self._chat_canvas.configure(yscrollcommand=sb.set)
        self._msgs = tk.Frame(self._chat_canvas, bg=chat_bg)
        self._msg_win = self._chat_canvas.create_window((0, 0), window=self._msgs, anchor="nw")

        def _sync_scroll(_event: object | None = None) -> None:
            self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))

        def _on_canvas_cfg(event: tk.Misc) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 8:
                self._chat_canvas.itemconfigure(self._msg_win, width=w)
                self._refresh_bubble_wrap(w)

        self._msgs.bind("<Configure>", lambda e: _sync_scroll())
        self._chat_canvas.bind("<Configure>", _on_canvas_cfg)

        self._attach_chat_wheel(self._chat_canvas)
        self._attach_chat_wheel(self._msgs)

        self._chat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        composer_outer = tk.Frame(top, bg=T_BG if compact else A_BG)
        composer_outer.grid(row=1, column=0, sticky="ew", pady=(6, 2))
        self._composer = tk.Frame(
            composer_outer,
            bg=A_COMPOSER,
            highlightbackground=A_CARD_BORDER,
            highlightthickness=1,
        )
        self._composer.pack(fill=tk.X, ipadx=1, ipady=1)
        self._inp = tk.Text(
            self._composer,
            height=inp_h,
            wrap=tk.WORD,
            font=self._font_body,
            padx=10,
            pady=6 if compact else 8,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=A_COMPOSER,
            fg=A_TEXT,
            insertbackground=A_TEXT,
        )
        self._inp.pack(fill=tk.X, padx=4, pady=(4, 2))
        bind_text_mousewheel(self._inp, lines_per_notch=app._text_wheel_lines)

        bottom_bar = tk.Frame(self._composer, bg=A_COMPOSER)
        bottom_bar.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._status = tk.Label(
            bottom_bar,
            text="Ctrl+Enter 发送",
            font=self._font_small,
            bg=A_COMPOSER,
            fg=A_META,
        )
        self._status.pack(side=tk.LEFT)
        ttk.Checkbutton(
            bottom_bar,
            text="本轮附带画面帧（不勾选则模型只看文字）",
            variable=app.report_chat_attach_vision_var,
        ).pack(side=tk.LEFT, padx=(10, 4))
        self._send_btn = ttk.Button(
            bottom_bar,
            text="发送",
            command=self._on_send,
            style=app._run_button_style,
            width=8,
        )
        self._send_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._clear_btn = ttk.Button(
            bottom_bar,
            text="清空",
            command=self._clear_turns,
            width=6,
        )
        self._clear_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._inp.bind("<Control-Return>", self._on_ctrl_enter)

        self.bind("<Destroy>", self._on_panel_destroy)
        self._session.attach(self)

    def _bubble_wraps(self, canvas_w: int) -> tuple[int, int]:
        wu = max(200, int((canvas_w - 72) * 0.70))
        wa = max(240, int((canvas_w - 40) * 0.92))
        return wu, wa

    def _chars_for_wrap_px(self, px: int) -> int:
        f = tkfont.Font(font=self._font_body)
        w = max(f.measure("测"), f.measure("W"), 6)
        return max(12, int(px // w))

    def _bind_bubble_readonly_text(self, w: tk.Text) -> None:
        """可选中、Ctrl+C 复制；禁止编辑与粘贴。"""

        def on_key(event: tk.Event) -> str | None:
            st = int(getattr(event, "state", 0) or 0)
            sym = (event.keysym or "").lower()
            if st & 0x4 and sym in ("a", "c"):
                return None
            return "break"

        w.bind("<Key>", on_key, add=True)
        w.bind("<<Paste>>", lambda _e: "break", add=True)
        w.bind("<<Cut>>", lambda _e: "break", add=True)

    def _make_bubble_text(
        self,
        parent: tk.Misc,
        content: str,
        *,
        bg: str,
        fg: str,
        wrap_px: int,
    ) -> tk.Text:
        chars = self._chars_for_wrap_px(wrap_px)
        t = tk.Text(
            parent,
            wrap=tk.WORD,
            width=chars,
            height=1,
            font=self._font_body,
            bg=bg,
            fg=fg,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=4,
            cursor="xterm",
            undo=False,
            maxundo=0,
        )
        t.insert("1.0", (content.strip() or "\u00a0"))
        lines = int(t.index("end-1c").split(".")[0])
        t.configure(height=max(1, lines))
        self._bind_bubble_readonly_text(t)
        return t

    def _refresh_bubble_wrap(self, canvas_w: int) -> None:
        wu, wa = self._bubble_wraps(canvas_w)
        for w, role in self._wrap_labels:
            if isinstance(w, tk.Text):
                px = wu if role == "user" else wa
                chars = self._chars_for_wrap_px(px)
                w.configure(width=chars)
                lines = int(w.index("end-1c").split(".")[0])
                w.configure(height=max(1, lines))
            else:
                w.configure(wraplength=wu if role == "user" else wa)

    def _attach_chat_wheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_chat_wheel)
        widget.bind("<Button-4>", self._on_chat_wheel)
        widget.bind("<Button-5>", self._on_chat_wheel)

    def _wire_bubble_wheel(self, root: tk.Misc) -> None:
        self._attach_chat_wheel(root)
        for c in root.winfo_children():
            self._wire_bubble_wheel(c)

    def _on_chat_wheel(self, event: tk.Event) -> None:
        if _widget_under_ancestor(getattr(event, "widget", None), self._composer):
            return
        lines = self._app._text_wheel_lines
        d = getattr(event, "delta", 0) or 0
        if d:
            if sys.platform == "win32":
                steps = int(-d * lines / 120.0)
                if steps == 0:
                    steps = -lines if d > 0 else lines
            else:
                steps = int(-d * lines / 120.0)
                if steps == 0:
                    steps = -lines if d > 0 else lines
            if steps:
                self._chat_canvas.yview_scroll(steps, "units")
            return
        n = getattr(event, "num", 0)
        if n == 4:
            self._chat_canvas.yview_scroll(-lines, "units")
        elif n == 5:
            self._chat_canvas.yview_scroll(lines, "units")

    def _scroll_chat_bottom(self) -> None:
        self.update_idletasks()
        self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))
        self._chat_canvas.yview_moveto(1.0)

    def _add_user_bubble(self, text: str) -> None:
        z = self._chat_zone_bg
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        meta = tk.Frame(row, bg=z)
        meta.pack(fill=tk.X, padx=(0, 12))
        ts = datetime.now().strftime("%H:%M")
        tk.Label(
            meta,
            text=f"You  ·  {ts}",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(side=tk.RIGHT)

        bubble = tk.Frame(row, bg=A_USER)
        bubble.pack(anchor=tk.E, padx=(40, 8), pady=(2, 4))
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        wu, _ = self._bubble_wraps(cw)
        body = self._make_bubble_text(
            bubble, text, bg=A_USER, fg=A_TEXT, wrap_px=wu
        )
        body.pack(padx=12, pady=10, anchor=tk.W)
        self._wrap_labels.append((body, "user"))
        self._wire_bubble_wheel(row)
        self._scroll_chat_bottom()

    def _add_assistant_bubble(
        self,
        text: str,
        subtitle: str,
        *,
        muted: bool = False,
        error: bool = False,
    ) -> None:
        z = self._chat_zone_bg
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        head = tk.Label(
            row,
            text=subtitle,
            font=self._font_small,
            bg=z,
            fg=A_META if muted else "#374151",
        )
        head.pack(anchor=tk.W, padx=(8, 0))
        bg = A_ERR if error else A_CARD
        border = "#fecaca" if error else A_CARD_BORDER
        card = tk.Frame(row, bg=bg, highlightbackground=border, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(8, 12), pady=(4, 6), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_wraps(cw)
        body = self._make_bubble_text(
            card, text, bg=bg, fg=A_TEXT, wrap_px=wa
        )
        body.pack(padx=14, pady=12, anchor=tk.W, fill=tk.X)
        self._wrap_labels.append((body, "assistant"))
        self._wire_bubble_wheel(row)
        self._scroll_chat_bottom()

    def _show_typing(self) -> None:
        if self._typing_row is not None:
            return
        z = self._chat_zone_bg
        self._typing_row = tk.Frame(self._msgs, bg=z)
        self._typing_row.pack(fill=tk.X, pady=(4, 8))
        tk.Label(
            self._typing_row,
            text="正在回复…",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(12, 0))
        self._wire_bubble_wheel(self._typing_row)
        self._scroll_chat_bottom()

    def _hide_typing(self) -> None:
        if self._typing_row is not None:
            self._typing_row.destroy()
            self._typing_row = None

    def _on_ctrl_enter(self, _event: object) -> str:
        self._on_send()
        return "break"

    def _on_panel_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._session.detach(self)

    def _reset_chat_ui(self) -> None:
        if self._provider is None:
            return
        self._wrap_labels.clear()
        self._hide_typing()
        for w in self._msgs.winfo_children():
            w.destroy()
        self._status.configure(text="Ctrl+Enter 发送")
        self._send_btn.configure(state=tk.NORMAL)
        self._clear_btn.configure(state=tk.NORMAL)

    def _clear_turns(self) -> None:
        self._session.clear()

    def _on_send(self) -> None:
        if self._provider is None:
            return
        text = self._inp.get("1.0", tk.END).strip()
        self._session.submit(text)


def main() -> None:
    _win_set_per_monitor_dpi()
    app = App()
    app.reload_files()
    app.mainloop()


if __name__ == "__main__":
    main()
