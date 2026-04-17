"""
VLV GUI 本机推理子进程后端与 UI：LocalInferenceBackend + FlowBar + ServerPanel。

从 gui.py 抽出的三联模块：
  - LocalInferenceBackend：进程启动/停止/端口探针/日志分发等纯后端逻辑
  - LocalInferenceFlowBar：主「运行」页顶部的启动条
  - LocalInferenceServerPanel：「本地对话」下的推理服务面板

与 App / LocalChatHub 通过鸭子类型交互；类型注解用 TYPE_CHECKING 前向引用避免循环。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from bilibili_vision.gui_common import (
    DEFAULT_LOCAL_OPENAI_BASE,
    DEFAULT_LOCAL_OPENAI_MODEL_ID,
    DEFAULT_OLLAMA_CHAT_MODEL_ID,
    LLM_GUI_PREF_JSON,
    LOCAL_INF_FLOW_HELP,
    T_BORDER,
    T_MUTED,
    T_PANEL,
    T_TEXT,
)
from bilibili_vision.fsatomic import atomic_write_text
from bilibili_vision.gui_helpers import _attach_tooltip, bind_text_mousewheel
from bilibili_vision.gui_pipeline import _decode_subprocess_line, _terminate_process_tree
from bilibili_vision.llm_analyze import probe_local_openai_chat_health
from bilibili_vision.paths import PROJECT_ROOT, subprocess_env

if TYPE_CHECKING:
    from bilibili_vision.gui import App
    from bilibili_vision.gui_chat import LocalChatHub


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

    def __init__(self, app: "App", chat_hub: "LocalChatHub") -> None:
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
            atomic_write_text(
                LLM_GUI_PREF_JSON,
                json.dumps(m, ensure_ascii=False, indent=2) + "\n",
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

    def __init__(self, parent: tk.Misc, infer: LocalInferenceBackend, app: "App", *, pad: int) -> None:
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
        intro = ttk.Frame(outer)
        intro.pack(fill=tk.X, pady=(0, 12))
        ttk.Label(
            intro,
            text="推理服务",
            font=(app._font_ui[0], app._font_ui[1], "bold"),
        ).pack(anchor=tk.W)
        for line in (
            "· 日志显示在下方；关主窗口时会一并结束本窗口启动的子进程。",
            "· 「服务 URL」「模型 ID」在上方「对话」标签里填写（本页不重复显示）。",
            "· 点「刷新状态」后若显示「探针进行中…」表示正在请求模型，请等待出现 ✓ 或 ✗。",
        ):
            ttk.Label(
                intro,
                text=line,
                foreground=T_MUTED,
                wraplength=560,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(2, 0))

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
