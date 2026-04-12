"""
本地音视频 + B 站：转写、合并文稿与分析；浅色 Cursor 风界面（统一页底色 + 白底编辑区），左侧导航 + 右侧内容区（分析报告内含对话）。

高 DPI：启动前启用 Windows Per-Monitor V2 感知；可用环境变量 BILIBILI_GUI_SCALE 覆盖缩放。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
# 便携版：子进程继承此变量，Playwright 使用目录内浏览器
_pw_browsers = ROOT / "pw-browsers"
if _pw_browsers.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_pw_browsers.resolve()))
sys.path.insert(0, str(ROOT))
from llm_analyze import chat_followup, resolve_provider
from transcribe_local import (
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


OUT = ROOT / "out"
MERGED = OUT / "transcript_merged.txt"
ANALYSIS = OUT / "video_analysis.txt"

LLM_PROVIDER_COMBO_VALUES = ("auto", "gemini", "openai", "groq", "anthropic", "xai")
_LLM_PROVIDER_SET = frozenset(LLM_PROVIDER_COMBO_VALUES)
LLM_GUI_PREF_JSON = ROOT / "local_llm_prefs.json"


def _norm_llm_provider(raw: str) -> str:
    p = (raw or "auto").strip().lower()
    return p if p in _LLM_PROVIDER_SET else "auto"


def _load_gui_llm_provider_pref() -> str:
    try:
        if not LLM_GUI_PREF_JSON.is_file():
            return ""
        data = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _norm_llm_provider(str(data.get("llm_provider", "")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def _save_gui_llm_provider_pref(provider: str) -> None:
    prov = _norm_llm_provider(provider)
    merged: dict = {}
    try:
        if LLM_GUI_PREF_JSON.is_file():
            raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = raw
    except (OSError, json.JSONDecodeError, TypeError):
        merged = {}
    merged["llm_provider"] = prov
    try:
        LLM_GUI_PREF_JSON.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

# Cursor 风浅色：统一「页」底色（侧栏+主区同色），白底仅用于输入/正文区，避免灰条与纯白硬切
T_PAGE = "#f3f4f6"
T_PANEL = "#ffffff"
T_BG = T_PAGE
T_SURFACE = T_PAGE
T_RAISED = "#e8eaef"
T_BORDER = "#d8dce3"
T_TEXT = "#1f2937"
T_MUTED = "#6b7280"
T_ACCENT = "#0969da"
T_ACCENT_ACTIVE = "#0550ae"
T_ENTRY = T_PANEL
T_SELECT = "#cce5ff"

NAV_BG = T_PAGE
NAV_ACTIVE = T_PANEL
NAV_DIVIDER = "#d0d4dc"

# 对话气泡（与页面区分仍靠圆角卡片色，不用大块灰底）
A_BG = T_PANEL
A_USER = "#e8eeff"
A_CARD = "#f0f2f5"
A_CARD_BORDER = "#d8dce3"
A_META = "#6b7280"
A_ERR = "#fef2f2"
A_COMPOSER = T_PANEL
A_TEXT = "#1f2937"

SCROLL_TROUGH = "#dfe3e8"

# API 设置页：信息条与卡片（略提亮，减少「一片灰」）
API_INFO_BG = "#eef4ff"
API_INFO_BORDER = "#c9daf8"
API_SECTION_MUTED = "#5c6570"

# 画面理解页：浅绿系，与「API 与模型」蓝区区分
VISION_INFO_BG = "#ecfdf5"
VISION_INFO_BORDER = "#6ee7b7"
VISION_MUTED = "#047857"
VISION_CARD_TITLE = "#0f766e"
VISION_PREF_JSON = ROOT / "local_vision_prefs.json"
VISION_DEFAULT_PROMPT = (
    "请用中文简要描述画面中的文字、界面元素和正在展示的主要内容。"
)


def _default_qwen_vl_model() -> str:
    local_dir = ROOT / "models" / "Qwen2-VL-2B-Instruct"
    try:
        if (local_dir / "config.json").is_file():
            return str(local_dir.resolve())
    except OSError:
        pass
    return "Qwen/Qwen2-VL-2B-Instruct"


def _load_vision_prefs() -> dict:
    try:
        if VISION_PREF_JSON.is_file():
            raw = json.loads(VISION_PREF_JSON.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def _save_vision_prefs_file(data: dict) -> None:
    VISION_PREF_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def report_line_style_tag(line: str) -> str | None:
    """按行选择 Tk Text 标签，与纯文本章节标题约定一致。"""
    s = line.strip()
    if not s:
        return None
    if s.startswith("【视频内容总结】"):
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
    whisper_model: str | None = None,
    llm_provider: str = "auto",
) -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")

    def worker() -> None:
        code = -1
        try:
            cmd = [
                sys.executable,
                "-u",
                str(ROOT / "bilibili_pipeline.py"),
                "extract",
            ]
            if asr_if_no_subs:
                cmd.append("--asr-if-no-subs")
            wm = (whisper_model or default_whisper_model_choice()).strip()
            if wm not in WHISPER_MODEL_CHOICES:
                wm = default_whisper_model_choice()
            cmd.extend(["--whisper-model", wm])
            cmd.append(url.strip())
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                on_line(line)
            code = proc.wait()
            # 确保分析报告与当前 transcript_merged.txt 一致（避免子进程缓冲/旧版管线漏跑 analyze）
            if code == 0:
                on_line("\n--- 正在刷新分析报告（analyze_transcript.py）---\n")
                lp = (llm_provider or "auto").strip() or "auto"
                r = subprocess.run(
                    [
                        sys.executable,
                        "-u",
                        str(ROOT / "analyze_transcript.py"),
                        "--llm-provider",
                        lp,
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=120,
                )
                if r.stdout:
                    on_line(r.stdout)
                if r.stderr:
                    on_line(r.stderr)
                if r.returncode != 0:
                    on_line(f"\n[警告] 分析报告步骤退出码 {r.returncode}\n")
                    code = r.returncode
        except Exception as e:
            on_line(f"\n[错误] {e}\n")
        finally:
            on_done(code)

    threading.Thread(target=worker, daemon=True).start()


def _analysis_report_bottom_pane_minsize(
    font_content: tuple,
    report_edge: int,
    *,
    font_soft: float,
) -> int:
    """分析报告页纵向分割：下半栏最小高度，避免缩放窗口时输入框与按钮被裁掉。"""
    sz = float(font_content[1])
    inp_lines = 3
    line_h = max(18, int(round(sz * 1.55)))
    composer = max(168, 18 + inp_lines * line_h + 50)
    # 分隔线 + 内边距 + 输入条 + 至少一条聊天可视区
    return max(292, int(round(10 + 8 + report_edge + composer + 72 * font_soft)))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("本地与 B 站 · 转写与分析 · Vision")
        self.configure(bg=T_BG)

        self.update_idletasks()
        self._scale = _detect_tk_scale(self)
        try:
            self.tk.call("tk", "scaling", "-displayof", ".", str(self._scale))
        except tk.TclError:
            pass

        self._geom_scale = min(self._scale, 2.25)
        gw = int(1180 * self._geom_scale)
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

        nav_w = int(200 * min(self._geom_scale, 1.35))
        self._nav = tk.Frame(main, bg=NAV_BG, width=nav_w)
        self._nav.pack(side=tk.LEFT, fill=tk.Y)
        self._nav.pack_propagate(False)

        tk.Label(
            self._nav,
            text="功能",
            font=(self._font_ui[0], max(8, int(round(9 * self._font_soft))), "bold"),
            bg=NAV_BG,
            fg=T_MUTED,
        ).pack(anchor=tk.W, padx=14, pady=(16, 8))

        self._nav_rows: dict[str, tk.Frame] = {}
        self._nav_labels: dict[str, tk.Label] = {}
        self._current_nav = ""

        def nav_item(key: str, title: str) -> None:
            row = tk.Frame(self._nav, bg=NAV_BG, cursor="hand2")
            row.pack(fill=tk.X, padx=8, pady=2)
            lb = tk.Label(
                row,
                text=title,
                font=self._font_ui,
                bg=NAV_BG,
                fg=T_TEXT,
                anchor=tk.W,
                padx=10,
                pady=8,
            )
            lb.pack(fill=tk.X)
            self._nav_rows[key] = row
            self._nav_labels[key] = lb

            def on_enter(_e: object) -> None:
                if self._current_nav == key:
                    return
                row.configure(bg=NAV_ACTIVE)
                lb.configure(bg=NAV_ACTIVE)

            def on_leave(_e: object) -> None:
                if self._current_nav == key:
                    row.configure(bg=NAV_ACTIVE)
                    lb.configure(bg=NAV_ACTIVE)
                else:
                    row.configure(bg=NAV_BG)
                    lb.configure(bg=NAV_BG)

            def on_click(_e: object) -> None:
                self._show_view(key)

            for w in (row, lb):
                w.bind("<Button-1>", on_click)
                w.bind("<Enter>", on_enter)
                w.bind("<Leave>", on_leave)

        nav_item("task", "提取与日志")
        nav_item("merged", "合并文稿")
        nav_item("report", "分析报告")
        nav_item("vision", "画面理解")
        nav_item("api", "API 与模型")

        tk.Frame(main, bg=NAV_DIVIDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        work = ttk.Frame(main, padding=(inner, inner, inner, inner))
        work.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._content_host = ttk.Frame(work)
        self._content_host.pack(fill=tk.BOTH, expand=True)
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)

        self.frame_task = ttk.Frame(self._content_host, padding=0)
        self.frame_merged = ttk.Frame(self._content_host, padding=0)
        self.frame_report = ttk.Frame(self._content_host, padding=0)
        self.frame_vision = ttk.Frame(self._content_host, padding=0)
        self.frame_api = ttk.Frame(self._content_host, padding=0)

        for f in (
            self.frame_task,
            self.frame_merged,
            self.frame_report,
            self.frame_vision,
            self.frame_api,
        ):
            f.grid(row=0, column=0, sticky="nsew")

        self._views = {
            "task": self.frame_task,
            "merged": self.frame_merged,
            "report": self.frame_report,
            "vision": self.frame_vision,
            "api": self.frame_api,
        }

        card = ttk.LabelFrame(self.frame_task, text=" 提取任务 ", padding=inner)
        card.pack(fill=tk.X, pady=(0, inner))

        ttk.Label(card, text="链接或本地文件").pack(anchor=tk.W, pady=(0, 4))
        self.url_var = tk.StringVar(value="")
        row = ttk.Frame(card)
        row.pack(fill=tk.X, pady=(0, 6))
        entry = ttk.Entry(row, textvariable=self.url_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda e: self.start_run())

        media_glob = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_LOCAL_MEDIA_SUFFIXES))

        def browse_file() -> None:
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

        ttk.Button(row, text="浏览…", command=browse_file, width=10).pack(side=tk.RIGHT)

        hint_pt = max(8, min(10, int(round(8 * self._font_soft))))
        hint_font = (self._font_ui[0], hint_pt)
        ttk.Label(
            card,
            text="B 站：以 https:// 开头的链接（www / m 站、b23.tv、含 BV 号均可）。"
            " 本地：点「浏览」或粘贴 mp3、m4a、wav、mp4、mkv 等路径。",
            wraplength=int(720 * self._geom_scale),
            font=hint_font,
            foreground=T_MUTED,
        ).pack(anchor=tk.W, pady=(0, inner))

        ttk.Separator(card, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, inner))

        model_row = ttk.Frame(card)
        model_row.pack(fill=tk.X, pady=(0, inner))
        ttk.Label(model_row, text="本地 Whisper 模型").pack(side=tk.LEFT, padx=(0, 8))
        self.whisper_model_var = tk.StringVar(value=default_whisper_model_choice())
        self.whisper_combo = ttk.Combobox(
            model_row,
            textvariable=self.whisper_model_var,
            values=WHISPER_MODEL_CHOICES,
            state="readonly",
            width=14,
        )
        self.whisper_combo.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            model_row,
            text="large-v3 更准确；small 更快、占用更小。用于本地音视频转写，或勾选「无字幕转写」时的口播识别。",
            wraplength=int(560 * self._geom_scale),
            font=hint_font,
            foreground=T_MUTED,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        actions = ttk.Frame(card)
        actions.pack(fill=tk.X)

        self.asr_if_no_subs_var = tk.BooleanVar(value=True)
        asr_row = tk.Frame(actions, bg=T_SURFACE, cursor="hand2")
        asr_row.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._asr_sym = tk.Label(
            asr_row,
            text="",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_ACCENT,
            cursor="hand2",
            padx=0,
            pady=0,
        )
        self._asr_sym.pack(side=tk.LEFT, padx=(0, 8), anchor=tk.CENTER)

        asr_caption = tk.Label(
            asr_row,
            text="无字幕时使用本地语音转写（较慢；模型见上一行下拉框）",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_TEXT,
            cursor="hand2",
            anchor=tk.W,
        )
        asr_caption.pack(side=tk.LEFT, anchor=tk.CENTER)

        def _asr_click(_event: object = None) -> None:
            self.asr_if_no_subs_var.set(not self.asr_if_no_subs_var.get())

        for w in (asr_row, self._asr_sym, asr_caption):
            w.bind("<Button-1>", _asr_click)

        self.asr_if_no_subs_var.trace_add("write", lambda *_: self._sync_asr_symbol())
        self._sync_asr_symbol()

        btn_row = ttk.Frame(actions)
        btn_row.pack(side=tk.RIGHT)
        self.run_btn = ttk.Button(
            btn_row,
            text="开始提取与分析",
            command=self.start_run,
            style=self._run_button_style,
        )
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="重新载入输出文件", command=self.reload_files).pack(
            side=tk.LEFT
        )

        status_outer = ttk.Frame(self.frame_task, padding=(0, inner, 0, inner))
        status_outer.pack(fill=tk.X)
        self.status = ttk.Label(
            status_outer,
            text="就绪（首次运行可能需要浏览器登录 B 站）",
            anchor=tk.W,
        )
        self.status.pack(fill=tk.X)

        log_card = ttk.LabelFrame(self.frame_task, text=" 运行日志 ", padding=inner)
        log_card.pack(fill=tk.BOTH, expand=True, pady=(inner, 0))
        self.log_txt = self._build_text_view(log_card, mono=True)
        self.merged_txt = self._build_text_view(self.frame_merged, mono=False)

        report_edge = max(14, inner + 6)
        rep_paned = ttk.Panedwindow(self.frame_report, orient=tk.VERTICAL)
        rep_paned.pack(fill=tk.BOTH, expand=True)
        rep_top = ttk.Frame(
            rep_paned,
            padding=(report_edge, report_edge, report_edge, 14),
        )
        rep_paned.add(rep_top, weight=14)
        rep_bot = ttk.Frame(rep_paned, padding=0)
        rep_paned.add(rep_bot, weight=4)
        self.report_txt = self._build_text_view(rep_top, mono=False)
        self._configure_report_text_tags(self.report_txt)
        ttk.Separator(rep_bot, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 8))
        rep_bot_inner = ttk.Frame(
            rep_bot,
            padding=(report_edge, 0, report_edge, report_edge),
        )
        rep_bot_inner.pack(fill=tk.BOTH, expand=True)
        self._report_chat_parent = rep_bot_inner
        self.report_agent_panel = AgentChatPanel(
            rep_bot_inner, self, self.agent_session, compact=True
        )
        self.report_agent_panel.pack(fill=tk.BOTH, expand=True)
        try:
            rep_paned.paneconfigure(rep_top, minsize=160)
            rep_paned.paneconfigure(
                rep_bot,
                minsize=_analysis_report_bottom_pane_minsize(
                    self._font_content,
                    report_edge,
                    font_soft=self._font_soft,
                ),
            )
        except tk.TclError:
            pass

        self._build_vision_page(inner)
        self._build_api_settings_page(inner)

        self._busy = False
        self._vision_busy = False
        self._show_view("task")
        entry.focus_set()

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
        self._llm_openai_base = tk.StringVar(value=g("OPENAI_BASE_URL", ""))
        self._llm_groq_model = tk.StringVar(value=g("GROQ_MODEL", ""))
        self._llm_anthropic_model = tk.StringVar(value=g("ANTHROPIC_MODEL", ""))
        self._llm_xai_model = tk.StringVar(value=g("XAI_MODEL", ""))

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

    def _save_llm_keys_to_local_file(self) -> None:
        path = ROOT / "local_api_keys.py"
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

    def rebuild_report_chat_panel(self) -> None:
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        if getattr(self, "report_agent_panel", None) is not None:
            old = self.report_agent_panel
            self.agent_session.detach(old)
            try:
                old.destroy()
            except tk.TclError:
                pass
        self.report_agent_panel = AgentChatPanel(
            self._report_chat_parent, self, self.agent_session, compact=True
        )
        self.report_agent_panel.pack(fill=tk.BOTH, expand=True)

    def _apply_llm_clicked(self, *, save_file: bool) -> None:
        self._sync_llm_env_from_form()
        _save_gui_llm_provider_pref(self.llm_provider_var.get())
        self.agent_session.clear()
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        self.rebuild_report_chat_panel()
        if save_file:
            try:
                self._save_llm_keys_to_local_file()
            except OSError as e:
                messagebox.showerror("保存失败", str(e))
                return
        messagebox.showinfo(
            "已应用",
            "已写入环境变量并刷新「分析报告」页底部对话区。"
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
            text="应用到当前会话（刷新对话区）",
            command=lambda: self._apply_llm_clicked(save_file=False),
            style=sec_style,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(
            btns,
            text="应用并保存到 local_api_keys.py",
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
        hint_small_pt = max(8, min(10, int(round(8 * self._font_soft))))
        hint_small: tuple[str, int] = (self._font_ui[0], hint_small_pt)
        title_pt = max(12, min(15, int(self._font_ui[1]) + 2))

        head = tk.Frame(inner, bg=T_PAGE)
        head.pack(fill=tk.X, pady=(0, 2))
        tk.Label(
            head,
            text="API 与模型",
            font=(self._font_ui[0], title_pt, "bold"),
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        tk.Label(
            head,
            text="配置密钥与接口后，「提取与分析」与「分析报告」中的对话将按下方首选平台调用。",
            font=hint_small,
            bg=T_PAGE,
            fg=API_SECTION_MUTED,
            wraplength=hint_wrap,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        info_outer = tk.Frame(inner, bg=T_PAGE)
        info_outer.pack(fill=tk.X, pady=(14, 16))
        info = tk.Frame(
            info_outer,
            bg=API_INFO_BG,
            highlightbackground=API_INFO_BORDER,
            highlightthickness=1,
        )
        info.pack(fill=tk.X)
        tk.Label(
            info,
            text=(
                "· API Key 以密文显示。\n"
                "· 选「自动」时按 Gemini → OpenAI → Groq → Anthropic → xAI 顺序，使用第一个已填 Key 的平台。\n"
                "· 「应用」会记住首选平台（local_llm_prefs.json）；「应用并保存」同时写入 local_api_keys.py。\n"
                "· 模型 ID 可留空则用各平台默认；OpenAI 兼容网关请填写「API 根 URL」。"
            ),
            bg=API_INFO_BG,
            fg=T_TEXT,
            font=hint_small,
            wraplength=max(320, hint_wrap - 32),
            justify=tk.LEFT,
            anchor=tk.NW,
        ).pack(anchor=tk.W, padx=16, pady=14)

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
            font=(self._font_ui[0], self._font_ui[1], "bold"),
            bg=T_PANEL,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        prov_row = tk.Frame(prov_inner, bg=T_PANEL)
        prov_row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(
            prov_row,
            text="分析 / 对话使用",
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
        tk.Label(
            prov_row,
            text="auto=自动；指定平台时请确保该平台 Key 已填写。",
            bg=T_PANEL,
            fg=API_SECTION_MUTED,
            font=hint_small,
        ).pack(side=tk.LEFT, padx=(16, 0))

        sec = tk.Frame(inner, bg=T_PAGE)
        sec.pack(fill=tk.X, pady=(8, 12))
        tk.Label(
            sec,
            text="各平台凭据",
            font=(self._font_ui[0], self._font_ui[1], "bold"),
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(side=tk.LEFT)
        tk.Label(
            sec,
            text="（按需填写）",
            bg=T_PAGE,
            fg=API_SECTION_MUTED,
            font=hint_small,
        ).pack(side=tk.LEFT, padx=(10, 0))

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
                font=(self._font_ui[0], self._font_ui[1], "bold"),
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
            tk.Label(
                inner,
                text=model_hint,
                bg=T_PANEL,
                fg=API_SECTION_MUTED,
                font=hint_small,
                wraplength=max(280, hint_wrap - 48),
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(10, 0))
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
            "留空则自动尝试 gemini-2.5-flash 等",
        )
        provider_block(
            inner,
            "OpenAI（GPT）",
            self._llm_openai_key,
            self._llm_openai_model,
            "默认 gpt-4o-mini",
            self._llm_openai_base,
            "API 根 URL（可选，默认 https://api.openai.com/v1）",
        )
        provider_block(
            inner,
            "Groq",
            self._llm_groq_key,
            self._llm_groq_model,
            "默认 llama-3.3-70b-versatile",
        )
        provider_block(
            inner,
            "Anthropic Claude",
            self._llm_anthropic_key,
            self._llm_anthropic_model,
            "默认 claude-3-5-haiku-20241022",
        )
        provider_block(
            inner,
            "xAI Grok",
            self._llm_xai_key,
            self._llm_xai_model,
            "默认 grok-2-latest；API 为 https://api.x.ai/v1",
        )

        _api_bind_wheel(inner)
        api_canvas.bind("<MouseWheel>", _api_wheel, add="+")
        api_canvas.bind("<Button-4>", _api_wheel, add="+")
        api_canvas.bind("<Button-5>", _api_wheel, add="+")

    def _build_vision_page(self, pad: int) -> None:
        """本地 Qwen2-VL / OpenAI 兼容服务：图或视频单帧 → 文字描述。"""
        outer = ttk.Frame(self.frame_vision, padding=pad)
        outer.pack(fill=tk.BOTH, expand=True)

        sec_style = getattr(self, "_secondary_button_style", "TButton")
        btns = tk.Frame(outer, bg=T_PAGE)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        ttk.Button(
            btns,
            text="保存本页偏好",
            command=self._vision_save_prefs,
            style=sec_style,
        ).pack(side=tk.LEFT, padx=(0, 10))
        self._vision_run_btn = ttk.Button(
            btns,
            text="开始画面理解",
            command=self._vision_run_clicked,
            style=self._run_button_style,
        )
        self._vision_run_btn.pack(side=tk.LEFT)
        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(
            side=tk.BOTTOM, fill=tk.X, pady=(0, 10)
        )

        scroll_host = ttk.Frame(outer)
        scroll_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scroll_host.grid_rowconfigure(0, weight=1)
        scroll_host.grid_columnconfigure(0, weight=1)

        y_inc = max(18, int(round(self._font_content[1] * 2.4)))
        vc = tk.Canvas(
            scroll_host,
            highlightthickness=0,
            borderwidth=0,
            bg=T_PAGE,
            yscrollincrement=y_inc,
        )
        vsb = tk.Scrollbar(
            scroll_host,
            orient=tk.VERTICAL,
            command=vc.yview,
            width=self._scrollbar_px,
            borderwidth=0,
            troughcolor=SCROLL_TROUGH,
            bg="#c4c4c4",
            activebackground="#a8a8a8",
            highlightthickness=0,
            relief="flat",
            jump=1,
        )
        vc.configure(yscrollcommand=vsb.set)
        vc.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(vc, bg=T_PAGE)
        _vc_win = vc.create_window((0, 0), window=inner, anchor=tk.NW)

        def _vc_on_cfg(event: tk.Event) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 1:
                vc.itemconfigure(_vc_win, width=w)

        def _vc_inner_cfg(_e: object | None = None) -> None:
            vc.configure(scrollregion=vc.bbox("all"))

        vc.bind("<Configure>", _vc_on_cfg)
        inner.bind("<Configure>", lambda _e: _vc_inner_cfg())

        def _vc_wheel(event: tk.Event) -> str | None:
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
                    vc.yview_scroll(steps, "units")
                return "break"
            n = getattr(event, "num", 0)
            if n == 4:
                vc.yview_scroll(-lines, "units")
                return "break"
            if n == 5:
                vc.yview_scroll(lines, "units")
                return "break"
            return None

        def _vc_bind_wheel(w: tk.Misc) -> None:
            w.bind("<MouseWheel>", _vc_wheel, add="+")
            w.bind("<Button-4>", _vc_wheel, add="+")
            w.bind("<Button-5>", _vc_wheel, add="+")
            for c in w.winfo_children():
                _vc_bind_wheel(c)

        prefs = _load_vision_prefs()
        hint_wrap = int(680 * self._geom_scale)
        hint_small_pt = max(8, min(10, int(round(8 * self._font_soft))))
        hint_small: tuple[str, int] = (self._font_ui[0], hint_small_pt)
        title_pt = max(12, min(15, int(self._font_ui[1]) + 2))

        head = tk.Frame(inner, bg=T_PAGE)
        head.pack(fill=tk.X, pady=(0, 2))
        tk.Label(
            head,
            text="画面理解",
            font=(self._font_ui[0], title_pt, "bold"),
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        tk.Label(
            head,
            text="通过本机 OpenAI 兼容接口调用多模态模型（如 Qwen2-VL），识别截图、幻灯片或视频某一帧中的文字与内容。",
            font=hint_small,
            bg=T_PAGE,
            fg=VISION_MUTED,
            wraplength=hint_wrap,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        info_outer = tk.Frame(inner, bg=T_PAGE)
        info_outer.pack(fill=tk.X, pady=(14, 16))
        info = tk.Frame(
            info_outer,
            bg=VISION_INFO_BG,
            highlightbackground=VISION_INFO_BORDER,
            highlightthickness=1,
        )
        info.pack(fill=tk.X)
        tk.Label(
            info,
            text=(
                "· 请先在另一窗口运行 SERVE_QWEN35.bat（或 transformers serve <模型路径>），默认地址 http://127.0.0.1:8000/v1\n"
                "· 推荐模型目录：models\\Qwen2-VL-2B-Instruct（已下载时可填该路径，无需联网）\n"
                "· 视频模式会调用项目内 ffmpeg\\ffmpeg.exe 抽取指定时刻的一帧再送模型\n"
                "· 与「API 与模型」页的云端 Key 无关；此处仅连你本机或局域网上的兼容服务"
            ),
            bg=VISION_INFO_BG,
            fg=T_TEXT,
            font=hint_small,
            wraplength=max(320, hint_wrap - 32),
            justify=tk.LEFT,
            anchor=tk.NW,
        ).pack(anchor=tk.W, padx=16, pady=14)

        self._vision_base_url = tk.StringVar(
            value=str(prefs.get("base_url", "http://127.0.0.1:8000/v1")).strip()
        )
        self._vision_model = tk.StringVar(
            value=str(prefs.get("model", _default_qwen_vl_model())).strip()
        )
        self._vision_api_key = tk.StringVar(value=str(prefs.get("api_key", "")).strip())
        self._vision_path = tk.StringVar(value=str(prefs.get("media_path", "")).strip())
        self._vision_mode = tk.StringVar(value=str(prefs.get("mode", "image")).strip())
        self._vision_at_sec = tk.StringVar(
            value=str(prefs.get("video_at", 1.0))
        )
        self._vision_max_tokens = tk.StringVar(
            value=str(int(prefs.get("max_tokens", 1024) or 1024))
        )

        def _card(title: str) -> tk.Frame:
            shell = tk.Frame(inner, bg=T_PAGE)
            shell.pack(fill=tk.X, pady=(0, 14))
            card = tk.Frame(
                shell,
                bg=T_PANEL,
                highlightbackground=T_BORDER,
                highlightthickness=1,
            )
            card.pack(fill=tk.X)
            cin = tk.Frame(card, bg=T_PANEL)
            cin.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
            tk.Label(
                cin,
                text=title,
                font=(self._font_ui[0], self._font_ui[1], "bold"),
                bg=T_PANEL,
                fg=VISION_CARD_TITLE,
                anchor=tk.W,
            ).pack(fill=tk.X)
            body = tk.Frame(cin, bg=T_PANEL)
            body.pack(fill=tk.BOTH, expand=True, pady=(12, 0))
            return body

        svc = _card("本地多模态服务")
        r1 = tk.Frame(svc, bg=T_PANEL)
        r1.pack(fill=tk.X)
        r1.grid_columnconfigure(1, weight=1)
        tk.Label(r1, text="服务根 URL", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12), pady=(0, 8)
        )
        ttk.Entry(r1, textvariable=self._vision_base_url, width=56).grid(
            row=0, column=1, sticky="ew", pady=(0, 8)
        )
        tk.Label(
            r1,
            text="须以 /v1 结尾（与 OpenAI SDK 一致）",
            bg=T_PANEL,
            fg=VISION_MUTED,
            font=hint_small,
        ).grid(row=1, column=1, sticky=tk.W)
        r2 = tk.Frame(svc, bg=T_PANEL)
        r2.pack(fill=tk.X, pady=(12, 0))
        r2.grid_columnconfigure(1, weight=1)
        tk.Label(r2, text="模型 ID / 路径", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.NW, padx=(0, 12), pady=(4, 0)
        )
        ttk.Entry(r2, textvariable=self._vision_model).grid(
            row=0, column=1, sticky="ew", pady=(4, 0)
        )
        tk.Label(
            r2,
            text="Hub 名或本机绝对路径（含 config.json 的目录）",
            bg=T_PANEL,
            fg=VISION_MUTED,
            font=hint_small,
        ).grid(row=1, column=1, sticky=tk.W, pady=(4, 0))
        r3 = tk.Frame(svc, bg=T_PANEL)
        r3.pack(fill=tk.X, pady=(12, 0))
        r3.grid_columnconfigure(1, weight=1)
        tk.Label(r3, text="API Key（可选）", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12)
        )
        ttk.Entry(r3, textvariable=self._vision_api_key, show="*").grid(
            row=0, column=1, sticky="ew"
        )
        tk.Label(
            r3,
            text="本地服务可填 EMPTY；留空则按 EMPTY 发送",
            bg=T_PANEL,
            fg=VISION_MUTED,
            font=hint_small,
        ).grid(row=1, column=1, sticky=tk.W, pady=(4, 0))
        r4 = tk.Frame(svc, bg=T_PANEL)
        r4.pack(fill=tk.X, pady=(12, 0))
        r4.grid_columnconfigure(1, weight=1)
        tk.Label(r4, text="max_tokens", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12)
        )
        ttk.Entry(r4, textvariable=self._vision_max_tokens, width=12).grid(
            row=0, column=1, sticky=tk.W
        )

        inp = _card("输入")
        mode_row = tk.Frame(inp, bg=T_PANEL)
        mode_row.pack(fill=tk.X, pady=(0, 10))
        tk.Label(mode_row, text="来源", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).pack(
            side=tk.LEFT, padx=(0, 14)
        )
        ttk.Radiobutton(
            mode_row,
            text="图片文件",
            value="image",
            variable=self._vision_mode,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Radiobutton(
            mode_row,
            text="视频（抽单帧）",
            value="video",
            variable=self._vision_mode,
        ).pack(side=tk.LEFT)

        path_row = tk.Frame(inp, bg=T_PANEL)
        path_row.pack(fill=tk.X)
        path_row.grid_columnconfigure(1, weight=1)
        tk.Label(path_row, text="路径", bg=T_PANEL, fg=T_TEXT, font=self._font_ui).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 12)
        )
        ttk.Entry(path_row, textvariable=self._vision_path).grid(
            row=0, column=1, sticky="ew", padx=(0, 8)
        )

        def _vision_browse() -> None:
            mode = self._vision_mode.get()
            if mode == "image":
                p = filedialog.askopenfilename(
                    parent=self,
                    title="选择图片",
                    filetypes=[
                        ("图片", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                        ("全部", "*.*"),
                    ],
                )
            else:
                p = filedialog.askopenfilename(
                    parent=self,
                    title="选择视频",
                    filetypes=[
                        ("视频", "*.mp4 *.mkv *.webm *.mov *.avi *.ts"),
                        ("全部", "*.*"),
                    ],
                )
            if p:
                self._vision_path.set(p)

        ttk.Button(path_row, text="浏览…", command=_vision_browse, width=9).grid(
            row=0, column=2, sticky=tk.E
        )

        at_row = tk.Frame(inp, bg=T_PANEL)
        at_row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(
            at_row,
            text="抽帧时刻（秒）",
            bg=T_PANEL,
            fg=T_TEXT,
            font=self._font_ui,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Entry(at_row, textvariable=self._vision_at_sec, width=10).pack(side=tk.LEFT)
        tk.Label(
            at_row,
            text="仅视频模式使用",
            bg=T_PANEL,
            fg=VISION_MUTED,
            font=hint_small,
        ).pack(side=tk.LEFT, padx=(12, 0))

        pr = _card("提示词")
        self._vision_prompt_txt = tk.Text(
            pr,
            height=4,
            wrap=tk.WORD,
            font=self._font_content,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=T_BORDER,
            highlightcolor=VISION_CARD_TITLE,
            bg=T_PANEL,
            fg=T_TEXT,
            insertbackground=T_TEXT,
            selectbackground=T_SELECT,
        )
        self._vision_prompt_txt.pack(fill=tk.BOTH, expand=True)
        _p = str(prefs.get("prompt", VISION_DEFAULT_PROMPT) or VISION_DEFAULT_PROMPT)
        self._vision_prompt_txt.insert("1.0", _p)

        out_shell = tk.Frame(inner, bg=T_PAGE)
        out_shell.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        out_card = tk.Frame(
            out_shell,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        out_card.pack(fill=tk.BOTH, expand=True)
        out_inner = tk.Frame(out_card, bg=T_PANEL)
        out_inner.pack(fill=tk.BOTH, expand=True, padx=18, pady=(16, 16))
        tk.Label(
            out_inner,
            text="模型回复",
            font=(self._font_ui[0], self._font_ui[1], "bold"),
            bg=T_PANEL,
            fg=VISION_CARD_TITLE,
            anchor=tk.W,
        ).pack(fill=tk.X)
        out_body = tk.Frame(out_inner, bg=T_PANEL)
        out_body.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._vision_out = self._build_text_view(out_body, mono=False)
        self._vision_out.configure(height=12)

        _vc_bind_wheel(inner)
        vc.bind("<MouseWheel>", _vc_wheel, add="+")
        vc.bind("<Button-4>", _vc_wheel, add="+")
        vc.bind("<Button-5>", _vc_wheel, add="+")

    def _vision_save_prefs(self) -> None:
        try:
            mt = int(self._vision_max_tokens.get().strip() or "1024")
            mt = max(64, min(mt, 32768))
        except ValueError:
            mt = 1024
        try:
            atf = float(self._vision_at_sec.get().strip() or "1")
            if atf < 0:
                atf = 0.0
        except ValueError:
            atf = 1.0
        data = {
            "base_url": self._vision_base_url.get().strip(),
            "model": self._vision_model.get().strip(),
            "api_key": self._vision_api_key.get().strip(),
            "media_path": self._vision_path.get().strip(),
            "mode": self._vision_mode.get().strip(),
            "video_at": atf,
            "max_tokens": mt,
            "prompt": self._vision_prompt_txt.get("1.0", tk.END).strip(),
        }
        try:
            _save_vision_prefs_file(data)
        except OSError as e:
            messagebox.showerror("保存失败", str(e))
            return
        messagebox.showinfo("已保存", "画面理解偏好已写入 local_vision_prefs.json")

    def _vision_run_clicked(self) -> None:
        if self._vision_busy:
            return
        venv_py = ROOT / "venv_qwen35" / "Scripts" / "python.exe"
        if not venv_py.is_file():
            messagebox.showerror(
                "缺少环境",
                "未找到 venv_qwen35\\Scripts\\python.exe。\n请先运行 install_qwen35_venv.ps1。",
            )
            return
        script = ROOT / "qwen35_vision_client.py"
        if not script.is_file():
            messagebox.showerror("缺少脚本", f"未找到 {script}")
            return

        path = self._vision_path.get().strip()
        if not path:
            messagebox.showwarning("提示", "请填写图片或视频路径，或点击「浏览…」选择。")
            return
        p = Path(os.path.expanduser(path.strip('"')))
        try:
            ok_file = p.is_file()
        except OSError:
            ok_file = False
        if not ok_file:
            messagebox.showwarning("提示", f"文件不存在或无法访问：\n{p}")
            return

        mode = self._vision_mode.get().strip()
        if mode == "image":
            suf = p.suffix.lower()
            if suf not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
                if not messagebox.askyesno(
                    "确认",
                    "当前扩展名可能不是常见图片格式，仍要继续吗？",
                ):
                    return
        else:
            suf = p.suffix.lower()
            if suf not in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".mpeg", ".mpg", ".ts", ".m2ts"}:
                if not messagebox.askyesno(
                    "确认",
                    "当前扩展名可能不是常见视频格式，仍要继续吗？",
                ):
                    return
            try:
                atf = float(self._vision_at_sec.get().strip() or "1")
                if atf < 0:
                    atf = 0.0
            except ValueError:
                messagebox.showwarning("提示", "抽帧时刻请输入数字（秒）。")
                return

        try:
            mt = int(self._vision_max_tokens.get().strip() or "1024")
            mt = max(64, min(mt, 8192))
        except ValueError:
            messagebox.showwarning("提示", "max_tokens 请输入整数。")
            return

        prompt = self._vision_prompt_txt.get("1.0", tk.END).strip()
        if not prompt:
            prompt = VISION_DEFAULT_PROMPT

        base = self._vision_base_url.get().strip()
        model = self._vision_model.get().strip()
        if not base or not model:
            messagebox.showwarning("提示", "请填写服务根 URL 与模型路径/ID。")
            return

        self._vision_busy = True
        self._vision_run_btn.configure(state=tk.DISABLED)
        self._vision_out.delete("1.0", tk.END)
        self._vision_out.insert(tk.END, "正在请求本地多模态服务，请稍候…\n")

        def worker() -> None:
            err_msg = ""
            out_txt = ""
            try:
                env = os.environ.copy()
                key = self._vision_api_key.get().strip()
                env["OPENAI_API_KEY"] = key if key else "EMPTY"
                cmd = [
                    str(venv_py),
                    str(script),
                    "--quiet",
                    "--base-url",
                    base,
                    "--model",
                    model,
                    "--prompt",
                    prompt,
                    "--max-tokens",
                    str(mt),
                ]
                if mode == "image":
                    cmd.extend(["--image", str(p.resolve())])
                else:
                    cmd.extend(
                        ["--video", str(p.resolve()), "--at", str(atf)]
                    )
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(ROOT),
                    env=env,
                    timeout=900,
                )
                out_txt = (proc.stdout or "").strip()
                err_tail = (proc.stderr or "").strip()
                if proc.returncode != 0:
                    err_msg = err_tail or f"进程退出码 {proc.returncode}"
                    if out_txt:
                        err_msg = f"{err_msg}\n\nstdout:\n{out_txt}"
                elif not out_txt:
                    err_msg = err_tail or "模型未返回内容"
            except subprocess.TimeoutExpired:
                err_msg = "请求超时（超过 15 分钟）"
            except OSError as e:
                err_msg = str(e)

            def done() -> None:
                self._vision_busy = False
                self._vision_run_btn.configure(state=tk.NORMAL)
                self._vision_out.delete("1.0", tk.END)
                if err_msg:
                    self._vision_out.insert(tk.END, err_msg)
                else:
                    self._vision_out.insert(tk.END, out_txt)

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _sync_asr_symbol(self) -> None:
        """与说明同字号：未选 □、已选 ✔（不用系统小复选框，避免画成 X）。"""
        if self.asr_if_no_subs_var.get():
            self._asr_sym.configure(text="✔", fg=T_ACCENT)
        else:
            self._asr_sym.configure(text="□", fg=T_MUTED)

    def _show_view(self, key: str) -> None:
        if key not in self._views:
            return
        self._current_nav = key
        self._views[key].tkraise()
        for k, row in self._nav_rows.items():
            bg = NAV_ACTIVE if k == key else NAV_BG
            row.configure(bg=bg)
            self._nav_labels[k].configure(bg=bg)

    def _setup_fonts_and_styles(self) -> str:
        """返回主操作按钮的 ttk style 名。"""
        fs = self._font_soft
        ui_pt = max(9, min(11, int(round(9 * fs))))
        content_pt = max(9, min(12, int(round(10 * fs))))
        mono_pt = max(9, min(12, int(round(10 * fs))))

        sans_ui = _pick_sans_cjk(self)
        self._font_ui = (sans_ui, ui_pt)
        self._font_content: tuple[str, int] = (sans_ui, content_pt)
        self._font_log: tuple[str, int] = (_pick_mono_family(self), mono_pt)

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
                foreground=T_MUTED,
                borderwidth=1,
                relief="solid",
            )
            style.configure("TLabelframe.Label", background=T_PAGE, foreground=T_MUTED)
            style.configure("TNotebook", background=T_PAGE, borderwidth=0)
            style.configure("TNotebook.Tab", background=T_RAISED, foreground=T_MUTED)
            style.map(
                "TNotebook.Tab",
                background=[("selected", T_PANEL)],
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

        for name in ("TLabel", "TButton", "TNotebook.Tab", "TEntry", "TLabelframe", "TLabelframe.Label"):
            try:
                style.configure(name, font=self._font_ui)
            except tk.TclError:
                pass

        tab_pad_x = max(10, int(round(10 * fs)))
        tab_pad_y = max(4, int(round(5 * fs)))
        try:
            style.configure("TNotebook.Tab", padding=[tab_pad_x, tab_pad_y])
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
            self._secondary_button_style = "Secondary.TButton"
            return "Accent.TButton"
        except tk.TclError:
            try:
                style.configure("TButton", padding=(btn_pad_x, btn_pad_y))
            except tk.TclError:
                pass
            self._secondary_button_style = "TButton"
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
            font=(fam, min(sz_i + 4, 22), "bold"),
            foreground=T_ACCENT,
            spacing1=2,
            spacing3=8,
        )
        txt.tag_configure(
            "report_h1",
            font=(fam, min(sz_i + 2, 18), "bold"),
            foreground="#111827",
            spacing1=10,
            spacing3=4,
        )
        txt.tag_configure(
            "report_subhead",
            font=(fam, min(sz_i + 1, 17), "bold"),
            foreground="#374151",
            spacing1=8,
            spacing3=2,
        )
        txt.tag_configure(
            "report_meta",
            font=(fam, max(8, sz_i - 1)),
            foreground=T_MUTED,
        )

    def _set_report_editor_content(self, raw: str) -> None:
        txt = self.report_txt
        txt.delete("1.0", tk.END)
        content = sanitize_analysis_display(raw)
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

    def start_run(self) -> None:
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

        self._busy = True
        self.run_btn.configure(state=tk.DISABLED)
        p_try = Path(os.path.expanduser(url.strip('"')))
        local_run = bool(p_try.is_file() and is_supported_local_media(p_try))
        if local_run:
            self.status.configure(text="运行中…（本地转写，无需登录）")
        else:
            self.status.configure(text="运行中…（若弹出 Chromium，请完成登录）")
        self.log_txt.delete("1.0", tk.END)
        self.merged_txt.delete("1.0", tk.END)
        self.report_txt.delete("1.0", tk.END)

        def on_line(line: str) -> None:
            self.after(0, lambda: self._append_log(line))

        def on_done(code: int) -> None:
            self.after(0, lambda: self._finish_run(code))

        wm = self.whisper_model_var.get().strip()
        if wm not in WHISPER_MODEL_CHOICES:
            wm = default_whisper_model_choice()
        self._sync_llm_env_from_form()
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        lp = self.llm_provider_var.get().strip() or "auto"
        run_pipeline(
            url,
            on_line,
            on_done,
            asr_if_no_subs=bool(self.asr_if_no_subs_var.get()),
            whisper_model=wm,
            llm_provider=lp,
        )

    def _append_log(self, line: str) -> None:
        self.log_txt.insert(tk.END, line)
        self.log_txt.see(tk.END)

    def _finish_run(self, code: int) -> None:
        self._busy = False
        self.run_btn.configure(state=tk.NORMAL)
        self.reload_files()
        if code == 0:
            self.status.configure(text="完成")
        else:
            self.status.configure(text=f"进程退出码：{code}（请查看运行日志）")
            messagebox.showerror(
                "未完成",
                f"命令退出码 {code}，请在本页下方「运行日志」中查看详情。",
            )

    def reload_files(self) -> None:
        if MERGED.is_file():
            self.merged_txt.delete("1.0", tk.END)
            self.merged_txt.insert(
                "1.0", MERGED.read_text(encoding="utf-8", errors="replace")
            )
        if ANALYSIS.is_file():
            self._set_report_editor_content(
                ANALYSIS.read_text(encoding="utf-8", errors="replace")
            )


class AgentSession:
    """分析报告底部对话区：单面板会话与 API 轮次。"""

    def __init__(self, app: App) -> None:
        self._app = app
        self._provider = resolve_provider(app.llm_provider_var.get().strip())
        self._turns: list[tuple[str, str]] = []
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
        self._app._sync_llm_env_from_form()
        self._provider = resolve_provider(self._app.llm_provider_var.get().strip())
        if self._provider is None or self._busy:
            if self._provider is None and not self._busy:
                messagebox.showwarning(
                    "无法发送",
                    "请先在「API 与模型」填写对应平台的 Key 并点击「应用」，"
                    "或将「首选提供商」改为「自动」并至少填写一个平台的 Key。",
                )
            return
        if not MERGED.is_file():
            messagebox.showwarning(
                "无法发送",
                "尚未生成合并文稿 out/transcript_merged.txt。\n请先点击「开始提取与分析」。",
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
            merged = MERGED.read_text(encoding="utf-8", errors="replace")
            analysis = ""
            if ANALYSIS.is_file():
                analysis = ANALYSIS.read_text(encoding="utf-8", errors="replace")
            reply, tag, payload = chat_followup(
                self._provider,
                merged,
                user_visible,
                analysis_excerpt=analysis or None,
                prior_turns=self._turns,
            )
            self._turns.append((payload, reply))
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
            p._add_assistant_bubble(reply, f"Agent · {tag}")

    def _finish_err(self, err: str) -> None:
        self._busy = False
        for p in self._panels:
            p._hide_typing()
            p._send_btn.configure(state=tk.NORMAL)
            p._clear_btn.configure(state=tk.NORMAL)
            p._status.configure(text="出错 · 请重试")
            p._add_assistant_bubble(err, "Error", error=True)


class AgentChatPanel(tk.Frame):
    """分析报告页底部对话区（聊天记录 + 输入框）。"""

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
        fam, sz = self._font_body[0], int(round(self._font_body[1]))
        self._font_small = (fam, max(8, sz - 1))

        pad_x = 0 if compact else 10
        pad_y = (0, 2) if compact else (8, 6)
        top = tk.Frame(self, bg=T_BG if compact else A_BG)
        top.pack(fill=tk.BOTH, expand=True, padx=pad_x, pady=pad_y)

        if self._provider is None:
            ttk.Label(
                top,
                text=(
                    "未配置可用的 API Key，或当前「首选提供商」与已填 Key 不匹配。\n\n"
                    "请打开左侧「API 与模型」填写 Key 并点击「应用」；"
                    "也可使用 local_api_keys.py（参考 local_api_keys.example.py）。"
                ),
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
