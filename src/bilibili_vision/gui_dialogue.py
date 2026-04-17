"""
VLV GUI 对话页: AgentSession(共享会话) + AgentChatPanel(报告与对话面板)。

从 gui.py 抽出,与 App 用鸭子类型交互;依赖 gui_common / gui_helpers / llm_analyze。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

from bilibili_vision.gui_common import (
    A_BG,
    A_CARD,
    A_CARD_BORDER,
    A_COMPOSER,
    A_ERR,
    A_META,
    A_TEXT,
    A_USER,
    BUBBLE_ASSIST_HEAD,
    BUBBLE_ERROR_BORDER,
    BUBBLE_USER_BORDER,
    CARD_BORDER,
    SCROLL_THUMB,
    SCROLL_THUMB_ACTIVE,
    SCROLL_TROUGH,
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    SPACING_XS,
    T_BG,
    T_SURFACE,
)
from bilibili_vision.gui_helpers import _widget_under_ancestor, bind_text_mousewheel
from bilibili_vision.llm_analyze import chat_followup, collect_timeline_frame_paths

if TYPE_CHECKING:
    from bilibili_vision.gui import App


class AgentSession:
    """主界面「报告与对话」标签：共享会话与 API 轮次。"""

    def __init__(self, app: "App") -> None:
        self._app = app
        app.prepare_chat_env_for_dialogue()
        self._provider = app.resolve_chat_provider_for_dialogue()
        self._turns: list[tuple[str, str, bool]] = []
        self._panels: list[AgentChatPanel] = []
        self._busy = False

    def attach(self, panel: "AgentChatPanel") -> None:
        if panel not in self._panels:
            self._panels.append(panel)

    def detach(self, panel: "AgentChatPanel") -> None:
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
            bg=SCROLL_THUMB,
            activebackground=SCROLL_THUMB_ACTIVE,
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
            undo=True,
            maxundo=-1,
            autoseparators=True,
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
        )
        self._send_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._clear_btn = ttk.Button(
            bottom_bar,
            text="清空",
            command=self._clear_turns,
        )
        self._clear_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._inp.bind("<Control-Return>", self._on_ctrl_enter)
        self._inp.bind("<Control-z>", self._on_composer_undo)
        self._inp.bind("<Control-Z>", self._on_composer_undo)
        self._inp.bind("<Control-y>", self._on_composer_redo)
        self._inp.bind("<Control-Y>", self._on_composer_redo)
        self._inp.bind("<Control-Shift-z>", self._on_composer_redo)
        self._inp.bind("<Control-Shift-Z>", self._on_composer_redo)

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
        row.pack(fill=tk.X, pady=(SPACING_SM, 2))
        meta = tk.Frame(row, bg=z)
        meta.pack(fill=tk.X, padx=(0, SPACING_MD))
        ts = datetime.now().strftime("%H:%M")
        tk.Label(
            meta, text=f"You  \u00b7  {ts}", font=self._font_small,
            bg=z, fg=A_META,
        ).pack(side=tk.RIGHT)

        bubble = tk.Frame(row, bg=A_USER, highlightbackground=BUBBLE_USER_BORDER, highlightthickness=1)
        bubble.pack(anchor=tk.E, padx=(60, SPACING_SM), pady=(2, SPACING_XS))
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        wu, _ = self._bubble_wraps(cw)
        body = self._make_bubble_text(
            bubble, text, bg=A_USER, fg=A_TEXT, wrap_px=wu
        )
        body.pack(padx=SPACING_MD, pady=SPACING_SM, anchor=tk.W)
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
        row.pack(fill=tk.X, pady=(SPACING_SM, 2))
        head = tk.Label(
            row, text=subtitle, font=self._font_small,
            bg=z, fg=A_META if muted else BUBBLE_ASSIST_HEAD,
        )
        head.pack(anchor=tk.W, padx=(SPACING_SM, 0))
        bg = A_ERR if error else A_CARD
        border = BUBBLE_ERROR_BORDER if error else CARD_BORDER
        card = tk.Frame(row, bg=bg, highlightbackground=border, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(SPACING_SM, 60), pady=(SPACING_XS, SPACING_SM), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_wraps(cw)
        body = self._make_bubble_text(
            card, text, bg=bg, fg=A_TEXT, wrap_px=wa
        )
        body.pack(padx=SPACING_LG, pady=SPACING_MD, anchor=tk.W, fill=tk.X)
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

