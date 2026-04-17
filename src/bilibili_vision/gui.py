"""
VLV（Video Listen View）：本地音视频 + B 站，统一「内容提取与分析」页（仅字幕总结 / 多模态深度可选）+ 大模型对话。
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

from bilibili_vision.fsatomic import atomic_write_text
from bilibili_vision.paths import PROJECT_ROOT, subprocess_env
from bilibili_vision.gui_common import (  # noqa: F401 — re-export for backward compat
    APP_BRAND,
    APP_TAGLINE,
    APP_TITLE,
    A_BG,
    A_CARD,
    A_CARD_BORDER,
    A_COMPOSER,
    A_ERR,
    A_META,
    A_TEXT,
    A_USER,
    API_SECTION_MUTED,
    CARD_BORDER,
    CARD_SHADOW,
    COMPOSER_INPUT_BG,
    COMPOSER_STOP_IDLE,
    COMPOSER_TOOLBAR_BG,
    CollapsibleCard,
    DEFAULT_LOCAL_OPENAI_BASE,
    DEFAULT_LOCAL_OPENAI_MODEL_ID,
    DEFAULT_OLLAMA_CHAT_MODEL_ID,
    FLOW_FULL_INFO_BG,
    FLOW_FULL_INFO_BORDER,
    FLOW_FULL_MUTED,
    FLOW_SUB_INFO_BG,
    FLOW_SUB_INFO_BORDER,
    FLOW_SUB_MUTED,
    LLM_GUI_PREF_JSON,
    LOCAL_INF_FLOW_HELP,
    NAV_ACTIVE_BG,
    NAV_BG,
    NAV_DIVIDER,
    NAV_HOVER_BG,
    NAV_SIDEBAR_BG,
    REPORT_H1_FG,
    REPORT_SUBHEAD_FG,
    SCROLL_THUMB,
    SCROLL_THUMB_ACTIVE,
    SCROLL_TROUGH,
    SECTION_HEADER_FG,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XL,
    SPACING_XS,
    STATUS_ERROR,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_WARNING,
    STOP_BTN_HOVER_BG,
    T_ACCENT,
    T_ACCENT_ACTIVE,
    T_BG,
    T_BORDER,
    T_ENTRY,
    T_MUTED,
    T_PAGE,
    T_PANEL,
    T_RAISED,
    T_SELECT,
    T_SURFACE,
    T_TEXT,
    WORKSPACE_TAB_IDLE,
    WORKSPACE_TAB_SELECTED,
    detect_tk_scale,
    font_soft_factor,
    pick_mono_family,
    pick_sans_cjk,
    read_gui_scale_env,
    win_set_per_monitor_dpi,
)

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
from bilibili_vision.gui_helpers import (  # noqa: F401 — re-export for backward compat
    URL_HINTS,
    _DelayedTooltip,
    _attach_tooltip,
    _widget_under_ancestor,
    bind_text_mousewheel,
    is_valid_task_source,
    looks_like_bilibili_url,
    make_text_y_scrollbar,
)

_win_set_per_monitor_dpi = win_set_per_monitor_dpi
_read_gui_scale_env = read_gui_scale_env
_detect_tk_scale = detect_tk_scale
_pick_mono_family = pick_mono_family
_font_soft_factor = font_soft_factor
_pick_sans_cjk = pick_sans_cjk


# 单次任务输出在 out/YYYY-MM-DD/HHMMSS_标题_BV…/；运行中由子进程回写 BILIBILI_VISION_OUT；prefs 只记 _active_session_out，不污染全局 env。
OUT_ROOT = PROJECT_ROOT / "out"


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
            atomic_write_text(
                LLM_GUI_PREF_JSON,
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            )
    except (OSError, json.JSONDecodeError, TypeError):
        pass


LLM_PROVIDER_COMBO_VALUES = ("auto", "gemini", "openai", "groq", "anthropic", "xai")
_LLM_PROVIDER_SET = frozenset(LLM_PROVIDER_COMBO_VALUES)


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
        atomic_write_text(
            LLM_GUI_PREF_JSON,
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
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
        atomic_write_text(
            LLM_GUI_PREF_JSON,
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        )
    except OSError:
        pass


# Theme colors, DPI/font helpers are imported from gui_common above.

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


from bilibili_vision.gui_pipeline import (  # noqa: F401 — re-export for backward compat
    PIPELINE_EXIT_USER_CANCEL,
    _decode_subprocess_line,
    _register_infer_atexit,
    _terminate_process_tree,
    report_line_style_tag,
    run_pipeline,
    sanitize_analysis_display,
)


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

        _nav_first = True

        def nav_item(key: str, title: str) -> None:
            nonlocal _nav_first
            if not _nav_first:
                tk.Frame(self._nav, bg=CARD_BORDER, height=1).pack(
                    fill=tk.X, padx=(SPACING_LG, SPACING_MD),
                )
            _nav_first = False
            row = tk.Frame(self._nav, bg=NAV_SIDEBAR_BG, cursor="hand2")
            row.pack(fill=tk.X, padx=(SPACING_MD, SPACING_SM), pady=1)
            accent = tk.Frame(row, width=3, bg=NAV_SIDEBAR_BG)
            accent.pack(side=tk.LEFT, fill=tk.Y)
            accent.pack_propagate(False)
            inner = tk.Frame(row, bg=NAV_SIDEBAR_BG, cursor="hand2")
            inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            lb = tk.Label(
                inner, text=title, font=self._font_ui,
                bg=NAV_SIDEBAR_BG, fg=T_TEXT, anchor=tk.W,
                padx=SPACING_MD, pady=SPACING_SM,
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

        nav_spacer = tk.Frame(self._nav, bg=NAV_BG)
        nav_spacer.pack(fill=tk.BOTH, expand=True)
        tk.Frame(self._nav, bg=CARD_BORDER, height=1).pack(fill=tk.X, padx=SPACING_LG)
        gear_row = tk.Frame(self._nav, bg=NAV_BG)
        gear_row.pack(fill=tk.X, padx=SPACING_LG, pady=SPACING_SM)
        tk.Label(
            gear_row, text="\u2699  设置", font=self._font_hint,
            bg=NAV_BG, fg=T_MUTED, anchor=tk.W, cursor="hand2",
        ).pack(anchor=tk.W)

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
            atomic_write_text(
                LLM_GUI_PREF_JSON,
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
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
        atomic_write_text(path, "".join(lines))

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

    def _refresh_api_status_dots(self) -> None:
        for card, key_var in getattr(self, "_api_provider_cards", []):
            color = STATUS_SUCCESS if key_var.get().strip() else T_MUTED
            card.set_status_dot(color)

    def _apply_llm_clicked(self, *, save_file: bool) -> None:
        self._sync_llm_env_from_form()
        _save_gui_llm_provider_pref(self.llm_provider_var.get())
        _save_llm_gui_chat_prefs(
            chat_route=self.chat_route_var.get(),
            chat_local_base=self._chat_local_base.get(),
            chat_local_model=self._chat_local_model.get(),
            chat_local_key=self._chat_local_key.get(),
        )
        self._refresh_api_status_dots()
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
            bg=SCROLL_THUMB,
            activebackground=SCROLL_THUMB_ACTIVE,
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

        tk.Label(
            inner, text="各平台凭据", font=self._font_title,
            bg=T_PAGE, fg=T_TEXT,
        ).pack(anchor=tk.W, pady=(SPACING_SM, SPACING_MD))

        model_entry_w = 30
        self._api_provider_cards: list[tuple[CollapsibleCard, tk.StringVar]] = []

        def provider_block(
            parent: tk.Misc,
            title: str,
            key_var: tk.StringVar,
            model_var: tk.StringVar,
            extra_var: tk.StringVar | None = None,
            extra_lbl: str = "",
        ) -> None:
            dot_color = STATUS_SUCCESS if key_var.get().strip() else T_MUTED
            card = CollapsibleCard(
                parent, title, initially_open=False,
                font=(self._font_title[0], self._font_title[1], "bold"),
                status_dot_color=dot_color,
            )
            card.pack(fill=tk.X, pady=(0, SPACING_SM))
            self._api_provider_cards.append((card, key_var))
            body = card.body
            row = tk.Frame(body, bg=T_PANEL)
            row.pack(fill=tk.X, pady=(0, SPACING_XS))
            row.grid_columnconfigure(1, weight=1)
            tk.Label(row, text="API Key", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
                row=0, column=0, sticky=tk.W, padx=(0, SPACING_MD), pady=2,
            )
            ttk.Entry(row, textvariable=key_var, show="\u2022").grid(
                row=0, column=1, sticky=tk.EW, padx=(0, SPACING_LG), pady=2,
            )
            tk.Label(row, text="模型 ID", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
                row=0, column=2, sticky=tk.W, padx=(0, SPACING_SM), pady=2,
            )
            ttk.Entry(row, textvariable=model_var, width=model_entry_w).grid(
                row=0, column=3, sticky=tk.W, pady=2,
            )
            if extra_var is not None and extra_lbl:
                tk.Label(body, text=extra_lbl, bg=T_PANEL, fg=T_TEXT, font=self._font_ui).pack(
                    anchor=tk.W, pady=(SPACING_SM, SPACING_XS),
                )
                ttk.Entry(body, textvariable=extra_var).pack(fill=tk.X)

        provider_block(inner, "Google Gemini", self._llm_gemini_key, self._llm_gemini_model)
        provider_block(
            inner, "OpenAI（GPT）", self._llm_openai_key, self._llm_openai_model,
            self._llm_openai_base, "API 根 URL（可选）",
        )
        provider_block(inner, "Groq", self._llm_groq_key, self._llm_groq_model)
        provider_block(inner, "Anthropic Claude", self._llm_anthropic_key, self._llm_anthropic_model)
        provider_block(inner, "xAI Grok", self._llm_xai_key, self._llm_xai_model)

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

    def _set_flow_mode(self, mode: str) -> None:
        self.flow_mode_var.set(mode)
        self._sync_seg_highlight()
        self._sync_flow_mode_ui()

    def _sync_seg_highlight(self) -> None:
        cur = self.flow_mode_var.get()
        for val, btn in getattr(self, "_seg_btns", {}).items():
            if val == cur:
                btn.configure(bg=T_ACCENT, fg="#ffffff")
            else:
                btn.configure(bg=T_RAISED, fg=T_MUTED)

    def _sync_flow_mode_ui(self, *_args: object) -> None:
        multimodal = self.flow_mode_var.get() == "multimodal"
        self._sync_seg_highlight()
        card_pipe = getattr(self, "_card_pipeline", None)
        if multimodal:
            if card_pipe is not None:
                try:
                    card_pipe.pack(fill=tk.X, pady=(0, SPACING_SM))
                except tk.TclError:
                    pass
            self._flow_run_btn.configure(
                text="开始：提取 + 深度 + 多模态"
            )
            self._flow_info_box.configure(
                bg=FLOW_FULL_INFO_BG, highlightbackground=FLOW_FULL_INFO_BORDER
            )
            self._flow_info_lbl.configure(
                text="B 站会下载视频；画面任务需本机服务且与③ URL 一致。",
                bg=FLOW_FULL_INFO_BG,
            )
            try:
                self._flow_info_inner.configure(bg=FLOW_FULL_INFO_BG)
                self._flow_info_tip_lbl.configure(bg=FLOW_FULL_INFO_BG, fg=FLOW_FULL_MUTED)
            except (tk.TclError, AttributeError):
                pass
        else:
            if card_pipe is not None:
                try:
                    card_pipe.pack_forget()
                except tk.TclError:
                    pass
            self._flow_run_btn.configure(text="开始：提取并总结")
            self._flow_info_box.configure(
                bg=FLOW_SUB_INFO_BG, highlightbackground=FLOW_SUB_INFO_BORDER
            )
            self._flow_info_lbl.configure(
                text="仅基础总结；深度/画面请切换「多模态」。",
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
            atomic_write_text(
                target / "vision_run_config.json",
                json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
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

        run_scroll_host = ttk.Frame(run_wrap)
        run_scroll_host.pack(fill=tk.BOTH, expand=True)
        run_scroll_host.grid_rowconfigure(0, weight=1)
        run_scroll_host.grid_columnconfigure(0, weight=1)
        y_inc_run = max(18, int(round(self._font_content[1] * 2.4)))
        run_canvas = tk.Canvas(
            run_scroll_host, highlightthickness=0, borderwidth=0,
            bg=T_PAGE, yscrollincrement=y_inc_run,
        )
        run_vsb = tk.Scrollbar(
            run_scroll_host, orient=tk.VERTICAL, command=run_canvas.yview,
            width=self._scrollbar_px, borderwidth=0, troughcolor=SCROLL_TROUGH,
            bg=SCROLL_THUMB, activebackground=SCROLL_THUMB_ACTIVE, highlightthickness=0,
            relief="flat", jump=1,
        )
        run_canvas.configure(yscrollcommand=run_vsb.set)
        run_canvas.grid(row=0, column=0, sticky="nsew")
        run_vsb.grid(row=0, column=1, sticky="ns")

        run_inner = tk.Frame(run_canvas, bg=T_PAGE)
        _run_inner_win = run_canvas.create_window((0, 0), window=run_inner, anchor=tk.NW)

        def _run_canvas_on_cfg(event: tk.Event) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 1:
                run_canvas.itemconfigure(_run_inner_win, width=w)

        def _run_inner_on_cfg(_event: object = None) -> None:
            run_canvas.configure(scrollregion=run_canvas.bbox("all"))

        run_canvas.bind("<Configure>", _run_canvas_on_cfg)
        run_inner.bind("<Configure>", lambda _e: _run_inner_on_cfg())

        def _run_wheel(event: tk.Event) -> str | None:
            lines = self._text_wheel_lines
            d = getattr(event, "delta", 0) or 0
            if d:
                steps = int(-d * lines / 120.0)
                if steps == 0:
                    steps = -lines if d > 0 else lines
                if steps:
                    run_canvas.yview_scroll(steps, "units")
                return "break"
            n = getattr(event, "num", 0)
            if n == 4:
                run_canvas.yview_scroll(-lines, "units")
                return "break"
            if n == 5:
                run_canvas.yview_scroll(lines, "units")
                return "break"
            return None

        def _run_bind_wheel(w: tk.Misc) -> None:
            w.bind("<MouseWheel>", _run_wheel, add="+")
            w.bind("<Button-4>", _run_wheel, add="+")
            w.bind("<Button-5>", _run_wheel, add="+")
            for c in w.winfo_children():
                _run_bind_wheel(c)

        top = tk.Frame(run_inner, bg=T_PAGE)
        top.pack(fill=tk.X, padx=edge, pady=(edge, 0))

        # ── Card 1: 输入源 ──
        card_input = CollapsibleCard(
            top, "输入源", initially_open=True,
            font=(self._font_title[0], self._font_title[1], "bold"),
        )
        card_input.pack(fill=tk.X, pady=(0, SPACING_SM))
        url_row = ttk.Frame(card_input.body)
        url_row.pack(fill=tk.X, pady=(0, SPACING_SM))
        url_row.columnconfigure(0, weight=1)
        self._flow_url_entry = ttk.Entry(url_row, textvariable=self.url_var)
        self._flow_url_entry.grid(row=0, column=0, sticky="ew", padx=(0, SPACING_SM))
        self._flow_url_entry.bind("<Return>", lambda _e: self._start_run_from_flow_mode())
        ttk.Button(url_row, text="浏览…", command=self._browse_media, width=10).grid(
            row=0, column=1, sticky=tk.E,
        )
        whisper_row = ttk.Frame(card_input.body)
        whisper_row.pack(fill=tk.X, pady=(SPACING_XS, 0))
        ttk.Label(whisper_row, text="Whisper 模型").pack(side=tk.LEFT, padx=(0, SPACING_SM))
        self.whisper_combo = ttk.Combobox(
            whisper_row, textvariable=self.whisper_model_var,
            values=WHISPER_MODEL_CHOICES, state="readonly", width=14,
        )
        self.whisper_combo.pack(side=tk.LEFT, padx=(0, SPACING_MD))

        asr_opts = tk.Frame(card_input.body, bg=T_PANEL)
        asr_opts.pack(fill=tk.X, pady=(SPACING_SM, 0))
        asr_row = tk.Frame(asr_opts, bg=T_PANEL, cursor="hand2")
        asr_row.pack(fill=tk.X)
        sym_a = tk.Label(asr_row, text="", font=self._font_ui, bg=T_PANEL, fg=T_ACCENT, cursor="hand2")
        sym_a.pack(side=tk.LEFT, padx=(0, SPACING_SM))
        self._asr_sym_boxes = [sym_a]
        asr_caption = tk.Label(
            asr_row, text="无字幕时用 Whisper 转写", font=self._font_ui,
            bg=T_PANEL, fg=T_TEXT, cursor="hand2", anchor=tk.W,
        )
        asr_caption.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _asr_click_u(_event: object = None) -> None:
            self.asr_if_no_subs_var.set(not self.asr_if_no_subs_var.get())
        for w in (asr_row, sym_a, asr_caption):
            w.bind("<Button-1>", _asr_click_u)
        self.asr_if_no_subs_var.trace_add("write", lambda *_: self._sync_asr_symbol())
        self._sync_asr_symbol()

        vid_row = tk.Frame(asr_opts, bg=T_PANEL, cursor="hand2")
        vid_row.pack(fill=tk.X, pady=(SPACING_XS, 0))
        sym_v = tk.Label(vid_row, text="", font=self._font_ui, bg=T_PANEL, fg=T_ACCENT, cursor="hand2")
        sym_v.pack(side=tk.LEFT, padx=(0, SPACING_SM))
        vid_caption = tk.Label(
            vid_row, text="B 站：下载整片视频（多模态抽帧；关则只要字幕）",
            font=self._font_ui, bg=T_PANEL, fg=T_TEXT, cursor="hand2", anchor=tk.W,
        )
        vid_caption.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _vid_click(_event: object = None) -> None:
            self.download_bilibili_video_var.set(not self.download_bilibili_video_var.get())
        for w in (vid_row, sym_v, vid_caption):
            w.bind("<Button-1>", _vid_click)

        def _sync_vid_symbol(*_args: object) -> None:
            on = self.download_bilibili_video_var.get()
            sym = "\u2714" if on else "\u25a1"
            fg = T_ACCENT if on else T_MUTED
            try:
                sym_v.configure(text=sym, fg=fg)
            except tk.TclError:
                pass
        self.download_bilibili_video_var.trace_add("write", _sync_vid_symbol)
        _sync_vid_symbol()

        # ── Card 2: 分析配置 ──
        card_config = CollapsibleCard(
            top, "分析配置", initially_open=True,
            font=(self._font_title[0], self._font_title[1], "bold"),
        )
        card_config.pack(fill=tk.X, pady=(0, SPACING_SM))

        seg_row = tk.Frame(card_config.body, bg=T_PANEL)
        seg_row.pack(fill=tk.X, pady=(0, SPACING_SM))
        self._seg_btns: dict[str, tk.Label] = {}
        for val, label in (("subtitle", " 快速 "), ("multimodal", " 多模态 ")):
            btn = tk.Label(
                seg_row, text=label, font=self._font_ui,
                bg=T_RAISED, fg=T_MUTED, cursor="hand2",
                padx=16, pady=5, relief="flat",
            )
            btn.pack(side=tk.LEFT, padx=(0, 2))
            self._seg_btns[val] = btn
            btn.bind("<Button-1>", lambda _e, v=val: self._set_flow_mode(v))
        self._sync_seg_highlight()

        rep_chk_row = ttk.Frame(card_config.body)
        rep_chk_row.pack(fill=tk.X)
        ttk.Checkbutton(
            rep_chk_row,
            text="用大模型生成总结",
            variable=self.flow_transcript_use_llm_var,
            command=self._sync_transcript_llm_source_widgets_state,
        ).pack(side=tk.LEFT, anchor=tk.W)
        tq_rep = ttk.Label(rep_chk_row, text=" ?", foreground=T_MUTED, cursor="hand2")
        tq_rep.pack(side=tk.LEFT, padx=(2, 0))
        _attach_tooltip(tq_rep, lambda: FLOW_REP_TIP_CHECK, wraplength=400)
        src_fr = ttk.Frame(card_config.body)
        src_fr.pack(fill=tk.X, pady=(SPACING_XS, 0))
        rb_api = ttk.Radiobutton(
            src_fr, text="在线 API", variable=self.flow_transcript_llm_source_var,
            value="api_preferred",
        )
        rb_api.pack(side=tk.LEFT, padx=(0, SPACING_MD))
        rb_local = ttk.Radiobutton(
            src_fr, text="本地（与③同源）", variable=self.flow_transcript_llm_source_var,
            value="same_as_vlm",
        )
        rb_local.pack(side=tk.LEFT)
        self._transcript_llm_source_widgets = (rb_api, rb_local)
        self._sync_transcript_llm_source_widgets_state()

        self._flow_info_box = tk.Frame(
            card_config.body,
            bg=FLOW_SUB_INFO_BG,
            highlightbackground=FLOW_SUB_INFO_BORDER,
            highlightthickness=1,
        )
        self._flow_info_box.pack(fill=tk.X, pady=(SPACING_SM, 0))
        self._flow_info_inner = tk.Frame(self._flow_info_box, bg=FLOW_SUB_INFO_BG)
        self._flow_info_inner.pack(fill=tk.X, padx=SPACING_SM, pady=SPACING_XS)
        self._flow_info_lbl = tk.Label(
            self._flow_info_inner, text="", bg=FLOW_SUB_INFO_BG, fg=T_TEXT,
            font=hint_small, wraplength=max(380, int(720 * self._geom_scale) - 48),
            justify=tk.LEFT, anchor=tk.NW,
        )
        self._flow_info_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, anchor=tk.NW)
        self._flow_info_tip_lbl = tk.Label(
            self._flow_info_inner, text="?", font=hint_small,
            bg=FLOW_SUB_INFO_BG, fg=FLOW_SUB_MUTED, cursor="hand2",
        )
        self._flow_info_tip_lbl.pack(side=tk.LEFT, anchor=tk.N, padx=(6, 0))
        _attach_tooltip(self._flow_info_tip_lbl, self._flow_info_tooltip_text, wraplength=440)
        self._flow_title_lbl = tk.Label(card_config.body)

        # ── Card 3: 本地推理服务 ──
        card_infer = CollapsibleCard(
            top, "本地推理服务", initially_open=False,
            font=(self._font_title[0], self._font_title[1], "bold"),
        )
        card_infer.pack(fill=tk.X, pady=(0, SPACING_SM))
        LocalInferenceFlowBar(
            card_infer.body, self._local_chat_hub._infer, self, pad=pad,
        ).pack(fill=tk.X)

        # ── Card 4: 画面管线详细设置 (collapsed, multimodal only) ──
        self._card_pipeline = CollapsibleCard(
            top, "画面管线详细设置", initially_open=False,
            font=(self._font_title[0], self._font_title[1], "bold"),
        )
        self._card_pipeline.pack(fill=tk.X, pady=(0, SPACING_SM))
        pipe = self._card_pipeline.body

        force_row = tk.Frame(pipe, bg=T_PANEL, cursor="hand2")
        force_row.pack(fill=tk.X, pady=(0, SPACING_SM))
        self._asr_force_sym = tk.Label(
            force_row, text="", font=self._font_ui,
            bg=T_PANEL, fg=T_ACCENT, cursor="hand2",
        )
        self._asr_force_sym.pack(side=tk.LEFT, padx=(0, SPACING_SM))
        force_cap = tk.Label(
            force_row, text="有字幕也强制 Whisper（更慢）",
            font=self._font_ui, bg=T_PANEL, fg=T_TEXT,
            cursor="hand2", anchor=tk.W,
        )
        force_cap.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _force_click(_event: object = None) -> None:
            self.asr_force_var.set(not self.asr_force_var.get())
        for w in (force_row, self._asr_force_sym, force_cap):
            w.bind("<Button-1>", _force_click)
        self.asr_force_var.trace_add("write", lambda *_: self._sync_asr_force_symbol())
        self._sync_asr_force_symbol()

        ttk.Checkbutton(
            pipe, text="启用画面管线", variable=self.vision_enable_var,
        ).pack(anchor=tk.W, pady=(0, SPACING_SM))
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

        s1 = ttk.LabelFrame(pipe, text=" ① 抽帧 / 去重 ", padding=(pad, SPACING_XS))
        s1.pack(fill=tk.X, pady=(0, SPACING_SM))
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

        s2 = ttk.LabelFrame(pipe, text=" ② OCR（PaddleOCR） ", padding=(pad, SPACING_XS))
        s2.pack(fill=tk.X, pady=(0, SPACING_SM))
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

        s3 = ttk.LabelFrame(pipe, text=" ③ Gemma 看图（OpenAI 兼容） ", padding=(pad, SPACING_XS))
        s3.pack(fill=tk.X, pady=(0, SPACING_SM))
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
        s4 = ttk.LabelFrame(pipe, text=" ④ 视频类型 ", padding=(pad, SPACING_XS))
        s4.pack(fill=tk.X, pady=(0, SPACING_SM))
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
        s5 = ttk.LabelFrame(pipe, text=" ⑤ 输出 ", padding=(pad, SPACING_XS))
        s5.pack(fill=tk.X, pady=(0, SPACING_SM))
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
        _run_bind_wheel(run_inner)
        self.after(100, lambda: _run_bind_wheel(run_inner))

        # ── Sticky bottom bar ──
        bottom = tk.Frame(run_wrap, bg=T_PAGE)
        bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=edge, pady=(0, edge))
        ttk.Separator(bottom, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, SPACING_SM))
        act = tk.Frame(bottom, bg=T_PAGE)
        act.pack(fill=tk.X, pady=(0, SPACING_XS))
        self._flow_run_btn = ttk.Button(
            act, text="开始：提取并生成总结",
            command=self._start_run_from_flow_mode,
            style=self._run_button_style,
        )
        self._flow_run_btn.pack(side=tk.LEFT, padx=(0, SPACING_SM))
        self._flow_cancel_btn = ttk.Button(
            act, text="停止", command=self.cancel_pipeline_run,
            style=sec_style, state=tk.DISABLED,
        )
        self._flow_cancel_btn.pack(side=tk.LEFT, padx=(0, SPACING_SM))
        ttk.Button(
            act, text="看日志",
            command=lambda: self._focus_workspace_tab("log"),
            style=sec_style,
        ).pack(side=tk.LEFT, padx=(0, SPACING_SM))
        ttk.Button(
            act, text="看合并",
            command=lambda: self._focus_workspace_tab("merged"),
            style=sec_style,
        ).pack(side=tk.LEFT)
        self._flow_status = ttk.Label(bottom, text="就绪", anchor=tk.W)
        self._flow_status.pack(fill=tk.X, pady=(0, 2))

        self._flow_multimodal_scroll = self._card_pipeline

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
                bordercolor=CARD_BORDER,
                padding=(SPACING_MD, SPACING_SM),
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
            style.configure("TEntry", fieldbackground=T_ENTRY, foreground=T_TEXT, bordercolor=CARD_BORDER)
            style.configure("TSeparator", background=T_BORDER)
            style.configure(
                "TButton",
                font=self._font_ui,
                background=T_RAISED,
                foreground=T_TEXT,
                borderwidth=1,
                relief="flat",
                focuscolor=T_ACCENT,
                padding=(14, 6),
            )
            style.map(
                "TButton",
                background=[("active", T_BORDER), ("disabled", T_RAISED)],
                foreground=[("disabled", T_MUTED)],
            )
            style.configure(
                "Card.TFrame",
                background=T_PANEL,
                borderwidth=1,
                relief="solid",
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

        tab_pad_x = max(14, int(round(14 * fs)))
        tab_pad_y = max(5, int(round(6 * fs)))
        sticky_pad_x = max(14, int(round(14 * fs)))
        sticky_pad_y = max(6, int(round(7 * fs)))
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
                focuscolor=T_ACCENT,
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
                foreground=STATUS_ERROR,
                background=COMPOSER_TOOLBAR_BG,
                borderwidth=1,
                relief="flat",
                padding=(stop_pad_x, btn_pad_y),
            )
            style.map(
                "Stop.TButton",
                background=[
                    ("active", STOP_BTN_HOVER_BG),
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
                    foreground=STATUS_ERROR,
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
            foreground=REPORT_H1_FG,
            spacing1=8,
            spacing3=3,
        )
        txt.tag_configure(
            "report_subhead",
            font=(fam, min(sz_i + 1, 13), "bold"),
            foreground=REPORT_SUBHEAD_FG,
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


from bilibili_vision.gui_inference import (  # noqa: F401 — re-export for backward compat
    LocalInferenceBackend,
    LocalInferenceFlowBar,
    LocalInferenceServerPanel,
    _format_subprocess_exit_status,
    _resolve_ollama_executable,
    _tcp_port_in_use,
    _windows_exit_hint_lines,
    _windows_exit_ntstatus,
)


from bilibili_vision.gui_chat import (  # noqa: F401 — re-export for backward compat
    LOCAL_CHAT_ROOT,
    LocalChatHub,
)



from bilibili_vision.gui_dialogue import (  # noqa: F401 — re-export for backward compat
    AgentChatPanel,
    AgentSession,
)



def main() -> None:
    _win_set_per_monitor_dpi()
    app = App()
    app.reload_files()
    app.mainloop()


if __name__ == "__main__":
    main()
