"""LLM 调用的超时策略 — 内层 HTTP 与外层子进程共享同一组常量。

单一真源:`gui_pipeline.py` 的外层 `subprocess.run(timeout=...)` 由本模块推导,
不会再出现"内层放宽、外层忘改"的组合杀。
"""
from __future__ import annotations

import os

HTTP_TIMEOUT_BASIC_LOCAL_SEC = 900
HTTP_TIMEOUT_BASIC_CLOUD_SEC = 120
HTTP_TIMEOUT_DEEP_LOCAL_SEC = 1800
HTTP_TIMEOUT_DEEP_CLOUD_SEC = 600
SUBPROCESS_OVERHEAD_SEC = 300

_ENV_BASIC = "LLM_HTTP_TIMEOUT_SEC"
_ENV_DEEP = "LLM_DEEP_HTTP_TIMEOUT_SEC"


def is_local_url(base_url: str) -> bool:
    low = (base_url or "").lower()
    return "127.0.0.1" in low or "localhost" in low


def _env_override(name: str, lo: int, hi: int = 7200) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return None


def http_timeout_sec(base_url: str) -> int:
    ov = _env_override(_ENV_BASIC, 30)
    if ov is not None:
        return ov
    return HTTP_TIMEOUT_BASIC_LOCAL_SEC if is_local_url(base_url) else HTTP_TIMEOUT_BASIC_CLOUD_SEC


def deep_http_timeout_sec(base_url: str) -> int:
    ov = _env_override(_ENV_DEEP, 60)
    if ov is not None:
        return ov
    return HTTP_TIMEOUT_DEEP_LOCAL_SEC if is_local_url(base_url) else HTTP_TIMEOUT_DEEP_CLOUD_SEC


def subprocess_analyze_timeout_sec(*, provider_local: bool, deep: bool) -> int:
    """外层 analyze_transcript 子进程的等待上限。

    子进程内部会依次做 `基础总结`(HTTP) + 可选 `深度分析`(HTTP),加上 Python 启动/
    文件 IO 的固定开销,因此这里求和 + overhead。
    """
    basic_env = _env_override(_ENV_BASIC, 30)
    basic = (
        basic_env
        if basic_env is not None
        else (HTTP_TIMEOUT_BASIC_LOCAL_SEC if provider_local else HTTP_TIMEOUT_BASIC_CLOUD_SEC)
    )
    if not deep:
        return basic + SUBPROCESS_OVERHEAD_SEC
    deep_env = _env_override(_ENV_DEEP, 60)
    deep_t = (
        deep_env
        if deep_env is not None
        else (HTTP_TIMEOUT_DEEP_LOCAL_SEC if provider_local else HTTP_TIMEOUT_DEEP_CLOUD_SEC)
    )
    return basic + deep_t + SUBPROCESS_OVERHEAD_SEC
