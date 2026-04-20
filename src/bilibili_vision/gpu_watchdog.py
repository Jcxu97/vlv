"""GPU preflight + watchdog helpers for the local inference servers.

Keeps the CUDA/bitsandbytes imports lazy so this module stays importable on
CPU-only machines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import GPUError, GPUMemoryError
from .log_config import get_logger

_log = get_logger("vlv.gpu")


@dataclass
class GPUStatus:
    available: bool
    name: str = ""
    free_bytes: int = 0
    total_bytes: int = 0

    @property
    def free_gib(self) -> float:
        return self.free_bytes / (1024 ** 3)

    @property
    def total_gib(self) -> float:
        return self.total_bytes / (1024 ** 3)


def probe_gpu() -> GPUStatus:
    """Returns CUDA memory state. Never raises — returns available=False on any
    import error so callers can gracefully fall back to CPU / cloud LLMs."""
    try:
        import torch
    except Exception as e:
        _log.debug("torch unavailable: %s", e)
        return GPUStatus(available=False)
    try:
        if not torch.cuda.is_available():
            return GPUStatus(available=False)
        idx = torch.cuda.current_device()
        name = torch.cuda.get_device_name(idx)
        free, total = torch.cuda.mem_get_info(idx)
        return GPUStatus(
            available=True, name=name, free_bytes=int(free), total_bytes=int(total)
        )
    except Exception as e:
        _log.warning("GPU probe failed: %s", e)
        return GPUStatus(available=False)


def assert_gpu_headroom(
    *, min_free_gib: float, model_label: str = "model"
) -> GPUStatus:
    """Raise GPUMemoryError if less than `min_free_gib` is free on the CUDA
    device. Callers should catch this and switch to a cloud LLM."""
    s = probe_gpu()
    if not s.available:
        raise GPUError(
            "No CUDA GPU detected; cannot start local inference.",
            hint="Install CUDA drivers or switch to a cloud LLM in Settings.",
        )
    if s.free_gib + 1e-6 < min_free_gib:
        raise GPUMemoryError(
            f"{model_label} needs ≥ {min_free_gib:.1f} GiB free VRAM "
            f"but only {s.free_gib:.2f} GiB available on {s.name}.",
            hint="Close other GPU-hungry apps or pick a smaller model.",
        )
    _log.info(
        "GPU preflight OK: %s, %.2f / %.2f GiB free",
        s.name,
        s.free_gib,
        s.total_gib,
    )
    return s


def cuda_empty_cache_safe() -> None:
    """Call torch.cuda.empty_cache() if torch is importable and CUDA is live."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


class CrashCounter:
    """Trips after `threshold` crashes within `window_sec`, letting the server
    decide to self-terminate instead of entering an infinite restart loop."""

    def __init__(self, *, threshold: int = 3, window_sec: float = 120.0) -> None:
        self._threshold = threshold
        self._window = window_sec
        self._events: list[float] = []

    def record(self) -> None:
        import time as _time

        now = _time.monotonic()
        self._events = [t for t in self._events if now - t <= self._window]
        self._events.append(now)

    @property
    def tripped(self) -> bool:
        import time as _time

        now = _time.monotonic()
        recent = [t for t in self._events if now - t <= self._window]
        return len(recent) >= self._threshold

    def reset(self) -> None:
        self._events.clear()
