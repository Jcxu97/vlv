"""
VLV GUI 子进程管线调度:负责串起 bilibili_pipeline → analyze_transcript → vision_deep_pipeline。

与 App 解耦(App 仅作类型注解),运行时通过鸭子类型访问 app 的几个属性/方法:
  - app._pipeline_cancel_requested (threading.Event)
  - app._register_pipeline_subprocess / _unregister_pipeline_subprocess
  - app._on_session_output_dir
  - app.after
  - app._local_chat_hub / ._app_destroy_started(atexit 回调用)
"""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from bilibili_vision.llm_timeouts import subprocess_analyze_timeout_sec
from bilibili_vision.paths import PROJECT_ROOT, subprocess_env
from bilibili_vision.transcribe_local import (
    WHISPER_MODEL_CHOICES,
    default_whisper_model_choice,
)

if TYPE_CHECKING:
    from bilibili_vision.gui import App


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
    app: "App | None" = None,
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
                # 外层子进程超时由 llm_timeouts 统一推导:内层 HTTP + Python 启动 overhead,
                # 避免再出现"内层放宽、外层忘改"的误杀。
                timeout_sec = subprocess_analyze_timeout_sec(
                    provider_local=(lp == "local"), deep=deep_analyze,
                )
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
