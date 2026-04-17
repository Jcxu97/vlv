"""
B 站无字幕时：yt-dlp 下音频 + faster-whisper 转写；亦支持本机 MP3/MP4 等路径直接转写。
依赖 ffmpeg/ffprobe；优先系统 PATH，否则 static-ffmpeg。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bilibili_vision.paths import PROJECT_ROOT as _PROJECT_ROOT

# 与便携目录 whisper-models/<id> 一致；GUI 与预下载脚本共用此列表。
WHISPER_MODEL_CHOICES: tuple[str, ...] = ("large-v3", "small")


def is_offline_mode() -> bool:
    """若设置 BILIBILI_OFFLINE=1（或 true/yes），禁止访问 Hugging Face 下载模型与 static-ffmpeg 拉取。"""
    v = (os.environ.get("BILIBILI_OFFLINE") or "").strip().lower()
    return v in ("1", "true", "yes")


def _bundled_model_bin(model_size: str) -> Path:
    return _PROJECT_ROOT / "whisper-models" / model_size / "model.bin"


def default_whisper_model_choice() -> str:
    """
    优先使用已随包放入 whisper-models/ 的模型，避免仅带 small 时仍默认 large-v3 导致联网下载。
    """
    for name in WHISPER_MODEL_CHOICES:
        if _bundled_model_bin(name).is_file():
            return name
    return WHISPER_MODEL_CHOICES[0]


def resolve_whisper_model_path(model_size: str, explicit: str | None) -> str | None:
    """
    显式 --whisper-model-path > 环境变量 WHISPER_MODEL_PATH >
    项目内 whisper-models/<model_size>（随包预下载）> None（首次从 Hugging Face 拉取）。
    """
    if explicit:
        p = Path(str(explicit).strip().strip('"')).expanduser()
        return str(p.resolve())
    env = (os.environ.get("WHISPER_MODEL_PATH") or "").strip()
    if env:
        return str(Path(env).expanduser().resolve())
    bundled = _PROJECT_ROOT / "whisper-models" / model_size
    if bundled.is_dir() and _bundled_model_bin(model_size).is_file():
        print(f"使用内置 Whisper 模型目录：{bundled}", flush=True)
        return str(bundled.resolve())
    return None


# 本地直读/可抽音频的常见后缀（ffmpeg 能处理的更多，此处列常用项）
AUDIO_SUFFIXES = frozenset(
    {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".ape"}
)
VIDEO_SUFFIXES = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".mpeg",
        ".mpg",
        ".ts",
        ".m2ts",
        ".wmv",
    }
)
SUPPORTED_LOCAL_MEDIA_SUFFIXES = AUDIO_SUFFIXES | VIDEO_SUFFIXES


def is_supported_local_media(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SUPPORTED_LOCAL_MEDIA_SUFFIXES


def _project_bundled_ffmpeg_dir() -> str | None:
    """若项目根下 ffmpeg 子目录中同时有 ffmpeg 与 ffprobe，则优先使用（免访问 GitHub）。"""
    d = _PROJECT_ROOT / "ffmpeg"
    if not d.is_dir():
        return None
    if sys.platform == "win32":
        fe, pe = d / "ffmpeg.exe", d / "ffprobe.exe"
    else:
        fe, pe = d / "ffmpeg", d / "ffprobe"
    if fe.is_file() and pe.is_file():
        return str(d.resolve())
    return None


def _try_static_ffmpeg_download() -> str | None:
    """通过 static-ffmpeg 从网络拉取二进制；失败时重试（GitHub 在国内常不稳定）。"""
    if is_offline_mode():
        return None
    try:
        import static_ffmpeg
    except ImportError:
        return None
    last_err: BaseException | None = None
    for attempt in range(1, 6):
        try:
            static_ffmpeg.add_paths()
            d = _ffmpeg_bin_dir_from_path()
            if d:
                return d
        except BaseException as e:
            last_err = e
            if attempt < 5:
                wait = min(3 * attempt, 15)
                print(
                    f"static-ffmpeg 获取失败（第 {attempt}/5 次）：{e}\n"
                    f"  {wait}s 后重试；若持续失败多为无法访问 GitHub，请改用系统 ffmpeg 或见下方说明。",
                    flush=True,
                )
                time.sleep(float(wait))
    if last_err is not None:
        print(
            "static-ffmpeg 已放弃：请将 ffmpeg.exe 与 ffprobe.exe 放入本程序目录下的 ffmpeg 文件夹，\n"
            "或安装 FFmpeg 并加入系统 PATH，或设置环境变量 BILIBILI_FFMPEG_LOCATION（目录路径）。",
            flush=True,
        )
    return None


def _ffmpeg_bin_dir_from_path() -> str | None:
    fe = shutil.which("ffmpeg")
    pe = shutil.which("ffprobe")
    if not fe or not pe:
        return None
    fd = Path(fe).resolve().parent
    if Path(pe).resolve().parent != fd:
        return None
    return str(fd)


def _prepend_path(bin_dir: str) -> None:
    """让 yt-dlp / faster-whisper 子进程能调用 ffmpeg、ffprobe。"""
    old = os.environ.get("PATH", "")
    if old and not old.startswith(bin_dir + os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + old
    elif not old:
        os.environ["PATH"] = bin_dir


def resolve_ffmpeg_bin_dir(explicit: str | None) -> str:
    """
    返回同时包含 ffmpeg 与 ffprobe 的目录，供 --ffmpeg-location 使用。
    explicit: 目录路径，或指向 ffmpeg 可执行文件的完整路径。
    """
    if explicit:
        p = Path(explicit.strip().strip('"'))
        if p.is_file():
            return str(p.resolve().parent)
        if p.is_dir():
            return str(p.resolve())
        raise FileNotFoundError(f"无效的 ffmpeg 路径：{explicit!r}")

    env = (
        os.environ.get("BILIBILI_FFMPEG_LOCATION")
        or os.environ.get("FFMPEG_LOCATION")
        or ""
    ).strip()
    if env:
        return resolve_ffmpeg_bin_dir(env)

    d = _project_bundled_ffmpeg_dir()
    if d:
        print(f"使用项目目录 ffmpeg/ 中的 ffmpeg：{d}", flush=True)
        return d

    d = _ffmpeg_bin_dir_from_path()
    if d:
        return d

    d = _try_static_ffmpeg_download()
    if d:
        print(f"已启用 static-ffmpeg 自带 ffmpeg/ffprobe：{d}", flush=True)
        return d

    raise RuntimeError(
        "未找到 ffmpeg 与 ffprobe（抽取/转码音频需要二者在同一目录）。\n"
        "可选方案：\n"
        "  • 在本程序根目录创建文件夹 ffmpeg，放入 ffmpeg.exe 与 ffprobe.exe（推荐，免访问 GitHub）\n"
        "  • 安装 FFmpeg 并加入系统 PATH，或 winget install FFmpeg\n"
        "  • 设置环境变量 BILIBILI_FFMPEG_LOCATION 为二者所在目录\n"
        "  • 传入 --ffmpeg-location\n"
        "  • 便携环境可换网络/VPN 后重试（static-ffmpeg 需从 GitHub 下载）"
    )


def download_audio(
    url: str,
    out_dir: Path,
    no_playlist: bool,
    cookies: Path | None,
    *,
    ffmpeg_location: str | None = None,
) -> list[Path]:
    """下载音频到 out_dir，返回生成的音频文件路径列表。"""
    bin_dir = resolve_ffmpeg_bin_dir(ffmpeg_location)
    _prepend_path(bin_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(out_dir / "%(id)s_p%(playlist_index)s_audio.%(ext)s")
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-x",
        "--audio-format",
        "m4a",
        "--audio-quality",
        "0",
        "--ffmpeg-location",
        bin_dir,
        "-o",
        out_tmpl,
    ]
    if no_playlist:
        cmd.append("--no-playlist")
    if cookies and cookies.is_file():
        cmd.extend(["--cookies", str(cookies)])
    cmd.append(url)
    print("运行:", " ".join(cmd), flush=True)
    try:
        subprocess.run(cmd, check=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"yt-dlp 下载超时（30 分钟未返回）：{url}；可能网络缓慢或被墙，已中止。"
        ) from e
    paths = sorted(out_dir.glob("*_audio.m4a"))
    if not paths:
        paths = sorted(out_dir.glob("*_audio.*"))
    return paths


def _ffmpeg_executable(bin_dir: str) -> str:
    fd = Path(bin_dir)
    if sys.platform == "win32":
        exe = fd / "ffmpeg.exe"
        if exe.is_file():
            return str(exe)
    else:
        exe = fd / "ffmpeg"
        if exe.is_file():
            return str(exe)
    w = shutil.which("ffmpeg")
    if w:
        return w
    raise RuntimeError("未找到 ffmpeg 可执行文件")


def extract_audio_from_video(
    video_path: Path,
    out_dir: Path,
    ffmpeg_bin_dir: str,
) -> Path:
    """从视频抽出 AAC m4a，供 Whisper 使用。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{video_path.stem}_extracted_audio.m4a"
    ff = _ffmpeg_executable(ffmpeg_bin_dir)
    cmd = [
        ff,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path.resolve()),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(out),
    ]
    print("运行:", " ".join(cmd), flush=True)
    try:
        subprocess.run(cmd, check=True, timeout=1800)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg 抽音超时（30 分钟未返回）：{video_path}；已中止以免挂住主流程。"
        ) from e
    return out


def prepare_audio_for_whisper(
    src: Path,
    out_dir: Path,
    ffmpeg_location: str | None,
) -> Path:
    """返回可直接送入 faster-whisper 的音频路径（视频会先抽成 m4a）。"""
    bin_dir = resolve_ffmpeg_bin_dir(ffmpeg_location)
    _prepend_path(bin_dir)
    suf = src.suffix.lower()
    if suf in AUDIO_SUFFIXES:
        return src.resolve()
    if suf in VIDEO_SUFFIXES:
        return extract_audio_from_video(src, out_dir, bin_dir)
    raise ValueError(f"不支持的扩展名：{suf}")


def _safe_filename_stem(stem: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")
    return (s[:120] if s else "media")


def run_asr_for_local_path(
    src: Path,
    out_dir: Path,
    *,
    model_size: str,
    device_pref: str,
    ffmpeg_location: str | None = None,
    model_path: str | None = None,
) -> list[Path]:
    """对本机音视频做转写，写出 {stem}_local_asr.srt。"""
    src = src.resolve()
    if not is_supported_local_media(src):
        raise FileNotFoundError(f"不是支持的本地音视频文件：{src}")
    print(f"本地媒体：{src}", flush=True)
    audio = prepare_audio_for_whisper(src, out_dir, ffmpeg_location)
    stem = _safe_filename_stem(src.stem)
    srt_path = out_dir / f"{stem}_local_asr.srt"
    transcribe_audio_file(
        audio,
        srt_path,
        model_size=model_size,
        device_pref=device_pref,
        model_path=model_path,
    )
    return [srt_path]


def _srt_timestamp(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec % 1) * 1000))
    if ms >= 1000:
        ms = 0
        s += 1
    if s >= 60:
        s -= 60
        m += 1
    if m >= 60:
        m -= 60
        h += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments, path: Path) -> None:
    lines: list[str] = []
    idx = 0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        idx += 1
        t0 = _srt_timestamp(float(seg.start))
        t1 = _srt_timestamp(float(seg.end))
        lines.append(str(idx))
        lines.append(f"{t0} --> {t1}")
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


_cuda_dlls_registered = False
_hf_env_initialized = False


def _ensure_hf_env() -> None:
    global _hf_env_initialized
    if _hf_env_initialized:
        return
    _hf_env_initialized = True
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _register_windows_cuda_dll_paths() -> None:
    """把 pip 安装的 nvidia-* 包里的 bin 加入 DLL 搜索路径（Windows）。

    必须在 import ctranslate2 / faster_whisper **之前**调用，否则 cuBLAS 已按错误路径加载。
    只执行一次；后续调用为 no-op。
    """
    global _cuda_dlls_registered
    if _cuda_dlls_registered:
        return
    _cuda_dlls_registered = True
    if sys.platform != "win32" or not hasattr(os, "add_dll_directory"):
        return
    try:
        import site
    except ImportError:
        return
    roots: list[str] = []
    if hasattr(site, "getsitepackages"):
        roots.extend(site.getsitepackages())
    u = getattr(site, "getusersitepackages", lambda: "")()
    if u:
        roots.append(u)
    roots.append(sys.prefix)
    seen: set[str] = set()

    def _add(d: Path) -> None:
        if not d.is_dir():
            return
        key = str(d.resolve())
        if key in seen:
            return
        if not any(d.glob("*.dll")):
            return
        try:
            os.add_dll_directory(key)
            seen.add(key)
        except OSError:
            return

    subdirs = (
        "nvidia/cublas/bin",
        "nvidia/cudnn/bin",
        "nvidia/cufft/bin",
        "nvidia/curand/bin",
        "nvidia/cuda_runtime/bin",
        "nvidia/cuda_nvrtc/bin",
    )
    for base in roots:
        base_p = Path(base)
        for sub in subdirs:
            _add(base_p / sub.replace("/", os.sep))
        # 兜底：site-packages/nvidia 下所有含 dll 的 bin（兼容 wheel 布局变化）
        nv = base_p / "nvidia"
        if nv.is_dir():
            for bin_dir in nv.rglob("bin"):
                if bin_dir.is_dir():
                    _add(bin_dir)

    if seen:
        old = os.environ.get("PATH", "")
        old_lower = {p.strip().lower() for p in old.split(os.pathsep) if p.strip()}
        missing = [s for s in sorted(seen) if s.lower() not in old_lower]
        if missing:
            os.environ["PATH"] = os.pathsep.join(missing) + os.pathsep + old


def _probe_ct2_cuda_works() -> bool:
    """
    仅 get_cuda_device_count()>0 不足以判断：驱动可见 GPU 时仍可能缺少 cublas64_12.dll 等，
    导致 faster-whisper 加载失败。做一次轻量 GPU 侧试探。
    """
    if sys.platform == "win32":
        _register_windows_cuda_dll_paths()
    try:
        import numpy as np
        from ctranslate2 import Device, StorageView, get_cuda_device_count

        if get_cuda_device_count() <= 0:
            return False
        view = StorageView.from_array(np.array([0.0], dtype=np.float32))
        view.to_device(Device.cuda)
        return True
    except BaseException:
        return False


def resolve_whisper_device(requested: str) -> tuple[str, str]:
    """返回 (device, compute_type)。"""
    r = (requested or "auto").lower().strip()
    if r == "cpu":
        return "cpu", "int8"
    if r == "cuda":
        return "cuda", "float16"
    if sys.platform == "win32":
        _register_windows_cuda_dll_paths()
    try:
        import ctranslate2  # type: ignore[import-untyped]

        if ctranslate2.get_cuda_device_count() > 0 and _probe_ct2_cuda_works():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


_whisper_model_cache: dict[tuple[str, str, str], object] = {}


def _create_whisper_model(model_ref: str, device_pref: str):
    """创建或返回缓存的 WhisperModel；GPU 初始化失败时自动回退 CPU。"""
    _ensure_hf_env()
    if sys.platform == "win32":
        _register_windows_cuda_dll_paths()
    from faster_whisper import WhisperModel

    device, compute_type = resolve_whisper_device(device_pref)
    cache_key = (model_ref, device, compute_type)
    cached = _whisper_model_cache.get(cache_key)
    if cached is not None:
        print(f"复用已加载的 faster-whisper 模型 {model_ref!r}（{device} / {compute_type}）", flush=True)
        return cached

    print(
        f"加载 faster-whisper 模型 {model_ref!r}（{device} / {compute_type}）…",
        flush=True,
    )
    try:
        m = WhisperModel(model_ref, device=device, compute_type=compute_type)
        _whisper_model_cache[cache_key] = m
        return m
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:
        if device != "cuda":
            raise
        detail = f"{type(e).__name__}: {e!s}" if str(e) else type(e).__name__
        print(
            f"GPU 不可用（{detail}），改用 CPU。\n"
            "若需 GPU：安装与 ctranslate2 匹配的 CUDA 12 运行库、更新 NVIDIA 驱动，"
            "或执行 pip install -r requirements-gpu.txt。"
            " 可显式使用 --whisper-device cpu 跳过检测。",
            flush=True,
        )
        fallback_key = (model_ref, "cpu", "int8")
        m = WhisperModel(model_ref, device="cpu", compute_type="int8")
        _whisper_model_cache[fallback_key] = m
        return m


def transcribe_audio_file(
    audio_path: Path,
    srt_out: Path,
    *,
    model_size: str,
    device_pref: str,
    model_path: str | None = None,
) -> None:
    effective = resolve_whisper_model_path(model_size, model_path)
    model_ref = str(Path(effective).expanduser().resolve()) if effective else model_size
    if effective:
        mp = Path(model_ref)
        if not mp.is_dir():
            raise FileNotFoundError(f"本地模型目录不存在：{mp}")
        if not (mp / "model.bin").is_file():
            raise FileNotFoundError(f"本地模型目录缺少 model.bin：{mp}")
        # 已使用本地目录时禁止 Hub 再联网（公司内网 / 离线机避免卡住或误连外网）
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        if is_offline_mode():
            raise RuntimeError(
                f"离线模式（已设置 BILIBILI_OFFLINE）：未找到本地模型 whisper-models/{model_size}/model.bin。\n"
                "请拷贝对应模型目录，或在 GUI/命令行改用已存在的模型（如 small），或使用 START_OFFLINE.bat 前确认已选本地有的模型。"
            )
        print(
            f"未找到本地 whisper-models/{model_size}，将从 Hugging Face 首次下载（需联网；"
            f"打包时带上 whisper-models 可免此步）。",
            flush=True,
        )

    model = _create_whisper_model(model_ref, device_pref)
    segments, info = model.transcribe(
        str(audio_path),
        vad_filter=True,
    )
    print(
        f"转写中…（检测到语言 {getattr(info, 'language', '?')}）",
        flush=True,
    )
    segs = list(segments)
    segments_to_srt(segs, srt_out)
    print(f"已写入：{srt_out}（共 {len(segs)} 段）", flush=True)


def audio_path_to_asr_srt(audio_path: Path) -> Path:
    stem = audio_path.stem
    if stem.endswith("_audio"):
        base = stem[: -len("_audio")]
    else:
        base = stem
    return audio_path.with_name(f"{base}_local_asr.srt")


def run_asr_for_url(
    url: str,
    out_dir: Path,
    *,
    no_playlist: bool,
    cookies: Path | None,
    model_size: str,
    device_pref: str,
    ffmpeg_location: str | None = None,
    model_path: str | None = None,
) -> list[Path]:
    """下载音频并逐文件转写，返回生成的 SRT 路径列表。"""
    audio_files = download_audio(
        url,
        out_dir,
        no_playlist,
        cookies,
        ffmpeg_location=ffmpeg_location,
    )
    if not audio_files:
        raise RuntimeError("未找到下载的音频文件（*_audio.*），请检查 yt-dlp 输出与网络。")
    srt_paths: list[Path] = []
    for ap in audio_files:
        srt_path = audio_path_to_asr_srt(ap)
        transcribe_audio_file(
            ap,
            srt_path,
            model_size=model_size,
            device_pref=device_pref,
            model_path=model_path,
        )
        srt_paths.append(srt_path)
    return srt_paths
