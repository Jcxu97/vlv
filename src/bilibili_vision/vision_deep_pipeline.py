"""
第二页「多模态深度」编排：抽帧（限量）→ 去重 → OCR（结构化行）→ 同一 Gemma 服务稀疏「一句看图」
→ 可选分层重写「深度内容分析」（字幕 + OCR + 画面描述）→ 写出 md/json/srt。

依赖分步可选：未安装 PySceneDetect / PaddleOCR / 本地多模态服务时跳过对应步骤并在日志中说明。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from bilibili_vision.paths import PROJECT_ROOT


def get_out() -> Path:
    raw = os.environ.get("BILIBILI_VISION_OUT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return PROJECT_ROOT / "out"


def vision_root() -> Path:
    return get_out() / "vision_work"


def frames_dir() -> Path:
    return vision_root() / "frames"


def dedup_dir() -> Path:
    return vision_root() / "frames_dedup"

_paddle_ocr_singleton: Any = None
_paddle_ocr_key: tuple[bool, bool] | None = None


def _log(cb: Callable[[str], None] | None, msg: str) -> None:
    line = msg.rstrip() + "\n"
    print(line, end="", flush=True)
    if cb:
        cb(line)


def _emit_gui_progress(m: str, v: int = 0, t: str = "") -> None:
    """供 GUI 顶栏进度条解析；m: i=不确定 d=确定 h=隐藏。"""
    print(
        "__GUI_PROGRESS__ " + json.dumps({"m": m, "v": int(v), "t": t}, ensure_ascii=False),
        flush=True,
    )


def _find_ffmpeg() -> Path | None:
    cand = PROJECT_ROOT / "ffmpeg" / "ffmpeg.exe"
    if cand.is_file():
        return cand
    cand2 = PROJECT_ROOT / "ffmpeg" / "ffmpeg"
    if cand2.is_file():
        return cand2
    w = shutil.which("ffmpeg")
    return Path(w) if w else None


def _is_video_file(p: Path) -> bool:
    return p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".mpeg", ".mpg", ".ts", ".m2ts"}


def _resolve_video_path(cfg: dict[str, Any], log: Callable[[str], None] | None) -> Path | None:
    src = str(cfg.get("source", "")).strip()
    if not src:
        return None
    p = Path(os.path.expanduser(src.strip('"')))
    if p.is_file() and _is_video_file(p):
        return p.resolve()
    o = get_out()
    o.mkdir(parents=True, exist_ok=True)
    cands: list[Path] = []
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".mpeg", ".mpg", ".ts", ".m2ts"):
        cands.extend(o.glob(f"*{ext}"))
    if not cands:
        return None
    cands.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    pick = cands[0].resolve()
    _log(log, f"[画面] 输入非本地视频路径，改用 out 内最近媒体：{pick.name}")
    return pick


def _video_type_profile(cfg: dict[str, Any]) -> tuple[float, int]:
    """(抽帧间隔倍率, 时间轴最大帧数)；上限由 max_timeline_frame_ceiling 控制（细看可到 120）。"""
    vt = str(cfg.get("video_type", "auto") or "auto").strip().lower()
    mult = {"gaming": 2.0, "vlog": 1.5, "tutorial": 1.0, "lecture": 1.0, "auto": 1.0}.get(vt, 1.0)
    cap = int(cfg.get("max_timeline_frames", 20) or 20)
    ceiling = int(cfg.get("max_timeline_frame_ceiling", 48) or 48)
    ceiling = max(8, min(160, ceiling))
    cap = max(1, min(ceiling, cap))
    return mult, cap


def _pick_timeline_spread(frames: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """长视频时不在只取最前几秒：在整段时间轴上均匀抽样至多 k 帧。"""
    n = len(frames)
    if k <= 0 or n <= k:
        return list(frames)
    if k == 1:
        return [frames[n // 2]]
    idxs = sorted({min(n - 1, int(round(i * (n - 1) / (k - 1)))) for i in range(k)})
    return [frames[i] for i in idxs]


def extract_frames_ffmpeg(
    video: Path,
    interval_sec: float,
    log: Callable[[str], None] | None,
) -> list[dict[str, Any]]:
    """按固定时间间隔抽帧，返回 [{\"t\": 秒, \"path\": str}, ...]。"""
    ff = _find_ffmpeg()
    if ff is None:
        _log(log, "[画面] 未找到 ffmpeg（请将 ffmpeg.exe 放入项目 ffmpeg/ 或 PATH）。")
        return []
    fd = frames_dir()
    fd.mkdir(parents=True, exist_ok=True)
    for old in fd.glob("vf_*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    fps = 1.0 / max(0.25, float(interval_sec))
    pattern = str(fd / "vf_%06d.jpg")
    cmd = [
        str(ff),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video.resolve()),
        "-vf",
        f"fps={fps:.6f}",
        "-q:v",
        "3",
        pattern,
    ]
    _log(log, f"[画面] FFmpeg 抽帧 fps={fps:.4f}（约每 {interval_sec:g}s 一帧）…")
    try:
        subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT), timeout=7200)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        _log(log, f"[画面] FFmpeg 抽帧失败：{e}")
        return []
    frames: list[dict[str, Any]] = []
    for i, fpath in enumerate(sorted(fd.glob("vf_*.jpg")), start=1):
        t = (i - 1) * float(interval_sec)
        frames.append({"t": round(t, 3), "path": str(fpath.resolve())})
    _log(log, f"[画面] 得到 {len(frames)} 张初筛帧。")
    return frames


def dedup_phash(
    frames: list[dict[str, Any]],
    max_diff: int,
    log: Callable[[str], None] | None,
) -> list[dict[str, Any]]:
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        _log(log, "[去重] 未安装 pillow / imagehash，跳过感知哈希去重。")
        return frames
    dd = dedup_dir()
    dd.mkdir(parents=True, exist_ok=True)
    for old in dd.glob("*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    md = max(0, min(20, int(max_diff)))
    out: list[dict[str, Any]] = []
    last_h = None
    for fr in frames:
        p = Path(fr["path"])
        if not p.is_file():
            continue
        try:
            im = Image.open(p).convert("RGB")
            h = imagehash.phash(im)
        except OSError:
            continue
        if last_h is not None and h - last_h <= md:
            continue
        last_h = h
        dst = dd / p.name
        try:
            dst.write_bytes(p.read_bytes())
        except OSError:
            dst = p
        out.append({"t": fr["t"], "path": str(dst.resolve())})
    _log(log, f"[去重] 感知哈希（阈值≤{md}）后保留 {len(out)} 帧。")
    return out


def run_scenedetect_on_video(video: Path, log: Callable[[str], None] | None) -> list[float]:
    try:
        from scenedetect import detect, ContentDetector  # type: ignore
    except ImportError:
        _log(log, "[场景] 未安装 scenedetect，跳过 PySceneDetect。")
        return []
    try:
        scene_list = detect(str(video), ContentDetector())
        times: list[float] = []
        for scene in scene_list:
            if isinstance(scene, tuple) and len(scene) >= 1:
                start = scene[0]
                times.append(float(start.get_seconds()))
            elif hasattr(scene, "start"):
                times.append(float(scene.start.get_seconds()))  # type: ignore[union-attr]
        _log(log, f"[场景] PySceneDetect 检测到 {len(times)} 个切点。")
        return times
    except Exception as e:
        _log(log, f"[场景] PySceneDetect 失败：{e}")
        return []


def _get_paddle_ocr(*, use_gpu: bool, log: Callable[[str], None] | None) -> Any:
    global _paddle_ocr_singleton, _paddle_ocr_key
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except ImportError:
        return None
    key = (use_gpu,)
    if _paddle_ocr_singleton is not None and _paddle_ocr_key == key:
        return _paddle_ocr_singleton
    _log(log, "[OCR] 初始化 PaddleOCR（首次较慢）…")
    try:
        ocr = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=use_gpu, show_log=False)
    except TypeError:
        try:
            ocr = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=use_gpu)
        except TypeError:
            ocr = PaddleOCR(use_angle_cls=True, lang="ch")
    _paddle_ocr_singleton = ocr
    _paddle_ocr_key = key
    return ocr


def run_paddle_ocr_on_frame(
    image_path: Path,
    *,
    bottom_crop_pct: int,
    full_frame: bool,
    scale_2x: bool,
    use_gpu: bool,
    log: Callable[[str], None] | None,
) -> str:
    ocr = _get_paddle_ocr(use_gpu=use_gpu, log=log)
    if ocr is None:
        return ""
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        im = Image.open(image_path).convert("RGB")
        w, h = im.size
        if not full_frame:
            crop_h = max(1, int(h * max(5, min(50, bottom_crop_pct)) / 100))
            im = im.crop((0, h - crop_h, w, h))
        if scale_2x:
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS
            im = im.resize((im.width * 2, im.height * 2), resample)
        vr = vision_root()
        tmp = vr / "_ocr_work.jpg"
        vr.mkdir(parents=True, exist_ok=True)
        im.save(tmp, quality=95)
        work_path = tmp
    except OSError:
        work_path = image_path
    try:
        r = ocr.ocr(str(work_path), cls=True)
    except Exception:
        return ""
    lines: list[str] = []
    if r and r[0]:
        for block in r[0]:
            if block and len(block) >= 2:
                lines.append(str(block[1][0]))
    return " ".join(lines).strip()


def run_vlm_on_frame(
    image_path: Path,
    base_url: str,
    model: str,
    api_key: str,
    log: Callable[[str], None] | None,
    *,
    provider: str = "openai_compatible",
) -> tuple[str, str]:
    from .frame_vision_gemma import FRAME_VISION_MAX_TOKENS, FRAME_VISION_PROMPT_ZH

    prov = (provider or "openai_compatible").strip().lower()
    if prov not in ("openai_compatible", "gemini", "anthropic"):
        prov = "openai_compatible"

    if prov == "gemini":
        try:
            from .llm_analyze import gemini_vision_caption_frame

            key = (api_key or "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
            cap, _tag = gemini_vision_caption_frame(
                key,
                model,
                image_path,
                FRAME_VISION_PROMPT_ZH,
            )
            return (cap or "").strip(), ""
        except Exception as e:
            return "", str(e)

    if prov == "anthropic":
        try:
            from .llm_analyze import anthropic_vision_caption_frame

            key = (api_key or "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip()
            cap, _m = anthropic_vision_caption_frame(
                key,
                model,
                image_path,
                FRAME_VISION_PROMPT_ZH,
            )
            return (cap or "").strip(), ""
        except Exception as e:
            return "", str(e)

    script = Path(__file__).resolve().parent / "local_vlm_openai_client.py"
    # 与 vision_deep_pipeline 自身同一解释器（多为 python_embed），避免误用 venv_gemma4
    # 里带 openai/jiter 的环境而在「智能应用控制」下 DLL 被拦。
    py_exe = Path(sys.executable)
    if not script.is_file():
        _log(log, "[VLM] 未找到 local_vlm_openai_client.py，跳过。")
        return "", ""
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = api_key if api_key else "EMPTY"
    env["LOCAL_VLM_USE_OPENAI_SDK"] = "0"
    cmd = [
        str(py_exe),
        str(script),
        "--quiet",
        "--base-url",
        base_url.rstrip("/"),
        "--model",
        model,
        "--image",
        str(image_path.resolve()),
        "--prompt",
        FRAME_VISION_PROMPT_ZH,
        "--max-tokens",
        str(int(FRAME_VISION_MAX_TOKENS)),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_ROOT),
            env=env,
            timeout=720,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if len(err) > 6000:
                err = err[:6000] + "\n…(stderr 已截断)"
            return "", err
        return (proc.stdout or "").strip(), ""
    except (subprocess.TimeoutExpired, OSError) as e:
        _log(log, f"[VLM] 调用异常：{e}")
        return "", str(e)


def merge_write_outputs(
    *,
    cfg: dict[str, Any],
    timeline: list[dict[str, Any]],
    deep_txt: str,
    log: Callable[[str], None] | None,
    vlm_aborted_after_errors: bool = False,
) -> None:
    o = get_out()
    o.mkdir(parents=True, exist_ok=True)
    deep_path = o / "video_analysis_deep.txt"
    base_text = deep_txt
    if deep_path.is_file():
        base_text = deep_path.read_text(encoding="utf-8", errors="replace")

    payload: dict[str, Any] = {
        "video_type": cfg.get("video_type", "auto"),
        "config": cfg,
        "timeline": timeline,
        "deep_text_path": str(deep_path.resolve()) if deep_path.is_file() else None,
        "merged_transcript": str((get_out() / "transcript_merged.txt").resolve()),
    }
    if cfg.get("output_json", True):
        jp = get_out() / "video_analysis_deep.json"
        jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _log(log, f"[输出] JSON → {jp}")

    if cfg.get("output_md", True):
        md = get_out() / "video_analysis_deep.md"
        lines = [
            "# 深度内容分析（多模态融合）\n",
        ]
        if vlm_aborted_after_errors:
            lines.append(
                "\n> **重要**：本地 VLM 已因连接失败被整段跳过。"
                "无字幕或口播极短的视频若缺少 VLM，仅靠 OCR 往往无法理解画面内容。"
                "请启动与「画面管线 → ③ VLM」中**根 URL、模型 ID**一致的多模态服务，"
                "并核对端口：本仓库 Gemma 常见 `http://127.0.0.1:18090/v1`，Ollama 常见 `http://127.0.0.1:11434/v1`。\n\n"
            )
        lines.extend(
            [
                "\n## 文本深度分析\n\n",
                base_text.strip(),
                "\n\n## 画面时间轴（OCR / Gemma 一句描述）\n\n",
            ]
        )
        for ev in timeline:
            ts = ev.get("t", 0)
            lines.append(f"### t={ts:.2f}s\n\n")
            if ev.get("ocr"):
                lines.append(f"- **OCR**: {ev['ocr']}\n")
            if ev.get("vlm"):
                lines.append(f"- **画面**: {ev['vlm']}\n")
            lines.append("\n")
        md.write_text("".join(lines), encoding="utf-8")
        _log(log, f"[输出] Markdown → {md}")

    if cfg.get("output_srt", True):
        sp = get_out() / "vision_outputs" / "video_analysis.srt"
        sp.parent.mkdir(parents=True, exist_ok=True)
        blocks: list[str] = []
        idx = 1
        for ev in timeline:
            text_parts = []
            if ev.get("ocr"):
                text_parts.append("[OCR] " + ev["ocr"][:200])
            if ev.get("vlm"):
                text_parts.append("[画面] " + ev["vlm"][:300])
            if not text_parts:
                continue
            body = " ".join(text_parts).replace("\n", " ")
            t = float(ev["t"])
            h, rem = divmod(int(t), 3600)
            m, s = divmod(rem, 60)
            ms = int(round((t - int(t)) * 1000))
            start = f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
            t2 = t + 2.0
            h2, rem2 = divmod(int(t2), 3600)
            m2, s2 = divmod(rem2, 60)
            ms2 = int(round((t2 - int(t2)) * 1000))
            end = f"{h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}"
            blocks.append(f"{idx}\n{start} --> {end}\n{body}\n")
            idx += 1
        sp.write_text("\n".join(blocks), encoding="utf-8")
        _log(log, f"[输出] SRT → {sp}")


def run_from_config(cfg: dict[str, Any], log: Callable[[str], None] | None = None) -> int:
    if not cfg.get("enabled", True):
        _log(log, "[多模态] 已在界面关闭，跳过画面管线。")
        _emit_gui_progress("h", 0, "")
        return 0

    video = _resolve_video_path(cfg, log)
    if video is None:
        _log(
            log,
            "[画面] 无可用本地视频（请使用本机 mp4/mkv 等，或将视频放入当前任务输出目录），仅写出 JSON 骨架。",
        )
        merge_write_outputs(cfg=cfg, timeline=[], deep_txt="", log=log)
        _emit_gui_progress("h", 0, "")
        return 0

    _vm = str(cfg.get("view_mode", "") or "").strip().lower()
    if _vm == "fine":
        ui_raw = cfg.get("frame_interval_ui_sec")
        eff_raw = cfg.get("frame_interval_sec")
        try:
            if ui_raw is not None and eff_raw is not None:
                ui_f = float(ui_raw)
                eff_f = float(eff_raw)
                if ui_f > eff_f + 1e-6:
                    _log(
                        log,
                        f"[配置] 细看：界面抽帧间隔为 {ui_f:g}s，已收紧为 {eff_f:g}s 参与 FFmpeg 抽帧。\n",
                    )
        except (TypeError, ValueError):
            pass
        _log(
            log,
            "[配置] 观看密度：细看（时间轴更多 VLM 帧、分层深度切段更细；在线 API 时调用量与费用显著增加）。\n",
        )
    elif _vm == "coarse":
        _log(log, "[配置] 观看密度：粗看。\n")

    base_iv = float(cfg.get("frame_interval_sec", 1.0) or 1.0)
    mult, max_frames = _video_type_profile(cfg)
    interval = max(0.25, base_iv * mult)

    frames = (
        extract_frames_ffmpeg(video, interval, log) if cfg.get("frame_extract", True) else []
    )

    if cfg.get("scene_detect", True):
        run_scenedetect_on_video(video, log)

    phash_on = bool(cfg.get("phash_dedup", True))
    max_diff = int(cfg.get("phash_max_diff", 6) or 6)
    if phash_on and frames:
        frames = dedup_phash(frames, max_diff, log)
    elif frames:
        frames = [{"t": f["t"], "path": f["path"]} for f in frames]

    timeline: list[dict[str, Any]] = []
    from .video_context_builder import format_ocr_timeline_line, format_vlm_timeline_line

    ocr_on = bool(cfg.get("ocr_enable", True))
    bottom_pct = int(cfg.get("ocr_bottom_crop_pct", 18) or 18)
    ocr_full = bool(cfg.get("ocr_full_frame", False))
    ocr_2x = bool(cfg.get("ocr_scale_2x", False))
    ocr_gpu = bool(cfg.get("ocr_use_gpu", True))
    vlm_on = bool(cfg.get("vlm_enable", True))
    vlm_base = str(cfg.get("vlm_base_url", "http://127.0.0.1:18090/v1")).strip()
    vlm_model = str(cfg.get("vlm_model", "")).strip()
    vlm_key = str(cfg.get("vlm_api_key", "")).strip()
    vlm_provider = str(cfg.get("vlm_provider") or "openai_compatible").strip().lower()
    if vlm_provider not in ("openai_compatible", "gemini", "anthropic"):
        vlm_provider = "openai_compatible"
    vlm_active = bool(vlm_on and vlm_model)
    vlm_fail_streak = 0
    vlm_aborted = False

    if vlm_on and vlm_model:
        if vlm_provider == "openai_compatible":
            _log(
                log,
                f"[VLM] OpenAI 兼容：根 URL={vlm_base!r}，模型={vlm_model!r}（本机 serve 或云端均可，模型须支持视觉）。",
            )
        elif vlm_provider == "gemini":
            _log(log, f"[VLM] 在线 Gemini：模型={vlm_model!r}（须为支持图像的 Gemini 模型）。")
        else:
            _log(log, f"[VLM] 在线 Anthropic：模型={vlm_model!r}（须为支持视觉的 Claude）。")
    elif vlm_on and not vlm_model:
        _log(
            log,
            "[VLM] 已开启但未填写「模型 ID」，跳过 VLM（仅 OCR）。请在界面「画面管线」③ 填写模型名。",
        )

    to_proc = _pick_timeline_spread(frames, max_frames)
    n_frames = len(to_proc)
    if len(frames) > n_frames:
        _log(
            log,
            f"[画面] 共 {len(frames)} 帧，均匀抽样 {n_frames} 帧做 OCR/VLM（覆盖整段视频，类型上限={max_frames}）。",
        )
    if n_frames > 0:
        _emit_gui_progress("i", 0, "多模态：准备逐帧 OCR / VLM…")

    from .frame_vision_gemma import should_drop_frame_caption

    for idx, fr in enumerate(to_proc):
        if n_frames:
            pct = int(100 * idx / max(n_frames, 1))
            _emit_gui_progress(
                "d",
                pct,
                f"多模态：帧 {idx + 1}/{n_frames}（OCR/VLM，在线或本地模型请耐心等待）",
            )
        p = Path(fr["path"])
        entry: dict[str, Any] = {"t": fr["t"], "path": str(p)}
        if ocr_on:
            txt = run_paddle_ocr_on_frame(
                p,
                bottom_crop_pct=bottom_pct,
                full_frame=ocr_full,
                scale_2x=ocr_2x,
                use_gpu=ocr_gpu,
                log=log,
            )
            if txt:
                entry["ocr"] = format_ocr_timeline_line(fr["t"], txt)
        if vlm_active:
            cap, verr = run_vlm_on_frame(
                p, vlm_base, vlm_model, vlm_key, log, provider=vlm_provider
            )
            if cap:
                vlm_fail_streak = 0
                if not should_drop_frame_caption(cap):
                    entry["vlm"] = format_vlm_timeline_line(fr["t"], cap)
            else:
                vlm_fail_streak += 1
                if vlm_fail_streak == 1 and verr:
                    _log(log, "[VLM] 首帧请求失败，完整错误如下（后续若仍失败将只计数并在连续 3 次后跳过 VLM）：\n" + verr)
                    if any(
                        x in verr
                        for x in (
                            "10061",
                            "积极拒绝",
                            "Connection refused",
                            "URLError",
                            "拒绝连接",
                        )
                    ):
                        _log(
                            log,
                            "[VLM] 连接被拒绝：本机没有在上述「根 URL」对应端口上提供 OpenAI 兼容服务。"
                            "无字幕、主要靠画面时必须先启动本地多模态模型（如 SERVE_GEMMA4 或「本地推理服务」→启动）。"
                            "若用 Ollama，根 URL 一般为 http://127.0.0.1:11434/v1 ；Gemma 本仓库默认 http://127.0.0.1:18090/v1 ；"
                            "勿与未监听的端口（如误填 8090）混用。",
                        )
                elif vlm_fail_streak in (2, 3):
                    _log(log, f"[VLM] 第 {vlm_fail_streak} 帧仍失败（同因多半与首帧相同，不再重复打印全文）。")
                if vlm_fail_streak >= 3:
                    _log(
                        log,
                        "[VLM] 已连续失败 3 帧，本任务剩余帧不再调用 VLM（OCR 仍继续）。"
                        "请检查：① 本地 OpenAI 兼容服务是否已启动，界面里 VLM 根 URL/模型名是否一致；"
                        "② 显存是否足够；③ 更新到当前仓库的 local_vlm_openai_client.py（默认 urllib，"
                        "勿对子进程设置 LOCAL_VLM_USE_OPENAI_SDK=1）；④ 智能应用控制是否拦截 jiter DLL。",
                    )
                    vlm_active = False
                    vlm_aborted = True
        if entry.get("ocr") or entry.get("vlm"):
            timeline.append(entry)

    if n_frames:
        _emit_gui_progress("d", 100, f"多模态：已完成 {n_frames} 帧，正在分层深度 / 写出 md/json/srt…")

    deep_txt = ""
    dp = get_out() / "video_analysis_deep.txt"
    if dp.is_file():
        deep_txt = dp.read_text(encoding="utf-8", errors="replace")

    hier = bool(cfg.get("hierarchical_deep", True))
    if hier and vlm_model.strip():
        mp = get_out() / "transcript_merged.txt"
        if mp.is_file():
            try:
                from .llm_analyze import hierarchical_multimodal_deep_report

                merged_full = mp.read_text(encoding="utf-8", errors="replace")
                bp = get_out() / "video_analysis.txt"
                basic = ""
                if bp.is_file():
                    basic = bp.read_text(encoding="utf-8", errors="replace")
                seg_sec = float(cfg.get("segment_sec", 180) or 180)
                deep_new = hierarchical_multimodal_deep_report(
                    merged_full,
                    basic,
                    timeline,
                    base_url=vlm_base,
                    model=vlm_model,
                    api_key=vlm_key,
                    backend=vlm_provider,
                    segment_sec=max(60.0, seg_sec),
                    max_segments=max(1, int(cfg.get("max_segments", 16) or 16)),
                    transcript_max_chars=max(2000, int(cfg.get("transcript_segment_max_chars", 5200) or 5200)),
                    ocr_max_lines=max(4, int(cfg.get("ocr_max_lines", 14) or 14)),
                    ocr_max_chars=max(800, int(cfg.get("ocr_max_chars", 2400) or 2400)),
                    vision_max_lines=max(4, int(cfg.get("vision_max_lines", 10) or 10)),
                    segment_block_hard_cap=max(4000, int(cfg.get("segment_block_hard_cap", 11000) or 11000)),
                    input_path=mp,
                    log=log,
                )
                if deep_new:
                    dp.parent.mkdir(parents=True, exist_ok=True)
                    dp.write_text(deep_new, encoding="utf-8")
                    deep_txt = deep_new
                    _log(log, "[分层深度] 已用字幕 + OCR + 画面描述重写 video_analysis_deep.txt。\n")
            except Exception as e:
                _log(log, f"[分层深度] 保留原深度稿（失败原因）：{e}\n")

    merge_write_outputs(
        cfg=cfg,
        timeline=timeline,
        deep_txt=deep_txt,
        log=log,
        vlm_aborted_after_errors=vlm_aborted,
    )
    _emit_gui_progress("h", 0, "")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="多模态深度管线（抽帧/OCR/Gemma一句/分层深度/融合输出）")
    ap.add_argument("--config", type=Path, required=True, help="JSON 配置文件路径")
    args = ap.parse_args()
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    code = run_from_config(cfg, log=None)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
