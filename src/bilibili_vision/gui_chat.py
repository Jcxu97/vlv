"""
VLV GUI 对话页: LocalChatHub(本地 OpenAI 兼容 + 云端 API 路由 + 侧栏会话管理)。

从 gui.py 抽出的 ~1134 行;与 App 解耦(鸭子类型访问 app 属性/方法)。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from bilibili_vision.gui_common import (
    A_CARD,
    A_CARD_BORDER,
    A_ERR,
    A_META,
    A_TEXT,
    A_USER,
    BUBBLE_ERROR_BORDER,
    BUBBLE_USER_BORDER,
    CARD_BORDER,
    COMPOSER_INPUT_BG,
    COMPOSER_TOOLBAR_BG,
    CollapsibleCard,
    DEFAULT_LOCAL_OPENAI_BASE,
    DEFAULT_LOCAL_OPENAI_MODEL_ID,
    LLM_GUI_PREF_JSON,
    NAV_ACTIVE_BG,
    NAV_BG,
    SCROLL_THUMB,
    SCROLL_THUMB_ACTIVE,
    SCROLL_TROUGH,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XS,
    T_MUTED,
    T_PAGE,
    T_PANEL,
    T_SELECT,
    T_SURFACE,
    T_TEXT,
)
from bilibili_vision.fsatomic import atomic_write_text
from bilibili_vision.gui_helpers import _attach_tooltip, _widget_under_ancestor, bind_text_mousewheel
from bilibili_vision.gui_inference import (
    LocalInferenceBackend,
    LocalInferenceServerPanel,
    _local_openai_base_for_ui,
    _maybe_rewrite_lm_studio_local_url,
)
from bilibili_vision.llm_analyze import (
    MAX_LOCAL_CHAT_MERGED_SNIPPET,
    OpenAICompatibleRequestCancelled,
    SYSTEM_LOCAL_CHAT_ZH,
    chat_followup,
    local_openai_compatible_chat_round,
    resolve_provider,
)
from bilibili_vision.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from bilibili_vision.gui import App


LOCAL_CHAT_ROOT = PROJECT_ROOT / "local_chat_data"


def _load_llm_gui_prefs_merged() -> dict:
    try:
        if LLM_GUI_PREF_JSON.is_file():
            raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


class LocalChatHub(ttk.Frame):
    """对话：可选「本地 OpenAI 兼容」或「云端 API（与 API 与模型页一致）」；侧栏会话、多轮、本地模式可附图。"""

    def __init__(self, parent: tk.Misc, app: "App") -> None:
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

        settings_card = CollapsibleCard(
            settings_pane, "对话设置", initially_open=False,
            font=(app._font_title[0], app._font_title[1], "bold"),
        )
        settings_card.pack(fill=tk.BOTH, expand=False, pady=(0, SPACING_SM))
        settings_inner = settings_card.body

        tk.Label(
            left,
            text="会话",
            font=app._font_hint,
            bg=NAV_BG,
            fg=T_MUTED,
        ).pack(anchor=tk.W, padx=12, pady=(10, 4))
        nb_row = tk.Frame(left, bg=NAV_BG)
        nb_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        del_btn = ttk.Button(
            nb_row, text="✕", width=3, command=self._delete_current,
            style=app._secondary_button_style,
        )
        del_btn.pack(side=tk.RIGHT, padx=(4, 0))
        _attach_tooltip(del_btn, lambda: "删除当前会话")
        ttk.Button(nb_row, text="＋ 新对话", command=self._new_session).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        lb_fr = tk.Frame(left, bg=NAV_BG)
        lb_fr.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 12))
        sb_l = tk.Scrollbar(
            lb_fr,
            orient=tk.VERTICAL,
            width=app._scrollbar_px,
            troughcolor=SCROLL_TROUGH,
            bg=SCROLL_THUMB,
            activebackground=SCROLL_THUMB_ACTIVE,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            jump=1,
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
            selectmode=tk.EXTENDED,
            exportselection=False,
        )
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_l.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.configure(yscrollcommand=sb_l.set)
        sb_l.configure(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_sidebar_select)
        self._listbox.bind("<Delete>", self._on_sidebar_delete_key)
        self._listbox.bind("<BackSpace>", self._on_sidebar_delete_key)

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
            bg=SCROLL_THUMB,
            activebackground=SCROLL_THUMB_ACTIVE,
            borderwidth=0,
            highlightthickness=0,
            relief="flat",
            jump=1,
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
            bg=COMPOSER_INPUT_BG,
            highlightbackground=CARD_BORDER,
            highlightthickness=1,
        )
        self._composer.pack(fill=tk.X, padx=SPACING_XS, pady=(SPACING_XS, 0))
        self._inp = tk.Text(
            self._composer,
            height=3,
            wrap=tk.WORD,
            font=app._font_content,
            padx=SPACING_MD,
            pady=SPACING_MD,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=COMPOSER_INPUT_BG,
            fg=A_TEXT,
            insertbackground=A_TEXT,
            undo=True,
            maxundo=-1,
            autoseparators=True,
        )
        self._inp.pack(fill=tk.BOTH, expand=True, padx=SPACING_XS, pady=(SPACING_XS, 0))
        bind_text_mousewheel(self._inp, lines_per_notch=app._text_wheel_lines)
        self._attach_lbl = tk.Label(
            self._composer,
            text="",
            font=self._font_small,
            bg=COMPOSER_INPUT_BG,
            fg=A_META,
            anchor=tk.W,
        )
        self._attach_lbl.pack(fill=tk.X, padx=SPACING_MD, pady=(0, SPACING_XS))
        bar_row = tk.Frame(self._composer, bg=COMPOSER_INPUT_BG)
        bar_row.pack(fill=tk.X, padx=SPACING_SM, pady=(0, SPACING_SM))
        left_bar = tk.Frame(bar_row, bg=COMPOSER_INPUT_BG)
        left_bar.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(
            left_bar,
            text="📎 图片",
            command=self._pick_images,
            style=app._secondary_button_style,
        ).pack(side=tk.LEFT)
        ttk.Button(
            left_bar,
            text="清除",
            command=self._clear_pending,
            style=app._secondary_button_style,
        ).pack(side=tk.LEFT, padx=(SPACING_XS, SPACING_SM))
        self._stream_chk = ttk.Checkbutton(
            left_bar,
            text="流式（仅本地）",
            variable=self._loc_stream,
        )
        self._stream_chk.pack(side=tk.LEFT, padx=(0, SPACING_SM))
        self._status = tk.Label(
            left_bar,
            text="Ctrl+Enter 发送",
            font=app._font_hint,
            bg=COMPOSER_INPUT_BG,
            fg=A_META,
            anchor=tk.W,
        )
        self._status.pack(side=tk.LEFT)
        right_bar = tk.Frame(bar_row, bg=COMPOSER_INPUT_BG)
        right_bar.pack(side=tk.RIGHT, fill=tk.Y)
        self._btn_stop = ttk.Button(
            right_bar,
            text="停止",
            command=self._on_cancel_chat,
            style="Stop.TButton",
            state=tk.DISABLED,
        )
        self._btn_stop.pack(side=tk.RIGHT, padx=(SPACING_SM, 0))
        ttk.Button(
            right_bar,
            text="➤ 发送",
            command=self._on_send,
            style=app._run_button_style,
        ).pack(side=tk.RIGHT)
        self._inp.bind("<Control-Return>", self._on_ctrl_enter)
        self._inp.bind("<Control-z>", self._on_composer_undo)
        self._inp.bind("<Control-Z>", self._on_composer_undo)
        self._inp.bind("<Control-y>", self._on_composer_redo)
        self._inp.bind("<Control-Y>", self._on_composer_redo)
        self._inp.bind("<Control-Shift-z>", self._on_composer_redo)
        self._inp.bind("<Control-Shift-Z>", self._on_composer_redo)

        self._title_save_after: str | None = None
        self._sync_hub_route_ui()

    def _persist_local_chat_hub_backend(self) -> None:
        try:
            merged = _load_llm_gui_prefs_merged()
            merged["local_chat_hub_backend_saved"] = self._hub_route.get().strip()
            atomic_write_text(
                LLM_GUI_PREF_JSON,
                json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
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
        if len(sel) != 1:
            return
        i = int(sel[0])
        if 0 <= i < len(self._lb_ids):
            sid = self._lb_ids[i]
            if sid != self._current_sid:
                self._load_session(sid)

    def _on_sidebar_delete_key(self, _event: object) -> str:
        self._delete_current()
        return "break"

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
        atomic_write_text(
            self._meta_path(sid),
            json.dumps(self._meta, ensure_ascii=False, indent=2) + "\n",
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
        atomic_write_text(
            self._meta_path(self._current_sid),
            json.dumps(self._meta, ensure_ascii=False, indent=2) + "\n",
        )

    def _ensure_session(self) -> str:
        if self._current_sid and self._meta_path(self._current_sid).is_file():
            return self._current_sid
        self._new_session()
        return self._current_sid or ""

    def _delete_current(self) -> None:
        sel = self._listbox.curselection()
        sids: list[str] = []
        for i in sel:
            idx = int(i)
            if 0 <= idx < len(self._lb_ids):
                sids.append(self._lb_ids[idx])
        if not sids and self._current_sid:
            sids = [self._current_sid]
        if not sids:
            messagebox.showinfo("提示", "请先选择一个会话。")
            return
        n = len(sids)
        prompt = (
            f"删除选中的 {n} 个会话及其附件图片？"
            if n > 1 else "删除当前会话及其中的附件图片？"
        )
        if not messagebox.askyesno("确认", prompt):
            return
        for sid in sids:
            shutil.rmtree(self._session_dir(sid), ignore_errors=True)
        if self._current_sid in sids:
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

    def _scroll_bottom(self) -> None:
        self.update_idletasks()
        self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))
        self._chat_canvas.yview_moveto(1.0)

    def _add_user_bubble(self, text: str, img_names: list[str]) -> None:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(SPACING_SM, 2))
        ts = datetime.now().strftime("%H:%M")
        meta = tk.Frame(row, bg=z)
        meta.pack(fill=tk.X, padx=(0, SPACING_MD))
        tk.Label(
            meta, text=f"You  \u00b7  {ts}", font=self._font_small,
            bg=z, fg=A_META,
        ).pack(side=tk.RIGHT)
        bubble = tk.Frame(row, bg=A_USER, highlightbackground=BUBBLE_USER_BORDER, highlightthickness=1)
        bubble.pack(anchor=tk.E, padx=(60, SPACING_SM), pady=(2, SPACING_XS))
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        wu, _ = self._bubble_dims(cw)
        body = self._bubble_text(bubble, text, bg=A_USER, wrap_px=wu)
        body.pack(padx=SPACING_MD, pady=SPACING_SM, anchor=tk.W)
        self._wrap_labels.append((body, "user"))
        if img_names:
            tk.Label(
                bubble,
                text="\U0001f4ce " + "\u3001".join(img_names[:6])
                + (" \u2026" if len(img_names) > 6 else ""),
                font=self._font_small, bg=A_USER, fg=A_META, anchor=tk.W,
            ).pack(anchor=tk.W, padx=SPACING_MD, pady=(0, SPACING_SM))
        self._scroll_bottom()

    def _add_assistant_bubble(self, text: str, *, error: bool = False) -> None:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(SPACING_SM, 2))
        tk.Label(
            row, text="Assistant", font=self._font_small,
            bg=z, fg=A_META,
        ).pack(anchor=tk.W, padx=(SPACING_SM, 0))
        bg = A_ERR if error else A_CARD
        border = BUBBLE_ERROR_BORDER if error else CARD_BORDER
        card = tk.Frame(row, bg=bg, highlightbackground=border, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(SPACING_SM, 60), pady=(SPACING_XS, SPACING_SM), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_dims(cw)
        body = self._bubble_text(card, text, bg=bg, wrap_px=wa)
        body.pack(padx=SPACING_LG, pady=SPACING_MD, anchor=tk.W, fill=tk.X)
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

    def _on_composer_undo(self, _event: object) -> str:
        try:
            self._inp.edit_undo()
        except tk.TclError:
            pass
        return "break"

    def _on_composer_redo(self, _event: object) -> str:
        try:
            self._inp.edit_redo()
        except tk.TclError:
            pass
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
            atomic_write_text(
                mp,
                json.dumps(disk, ensure_ascii=False, indent=2) + "\n",
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

