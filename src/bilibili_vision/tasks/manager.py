"""TaskManager — run subprocesses and background callables with cancel + timeout."""
from __future__ import annotations

import enum
import os
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .cancellation import CancellationToken
from ..log_config import get_logger

_log = get_logger("vlv.tasks")


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


@dataclass
class TaskHandle:
    task_id: str
    name: str
    token: CancellationToken
    future: Future
    status: TaskStatus = TaskStatus.PENDING
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[BaseException] = None

    def cancel(self) -> None:
        self.token.cancel()

    def wait(self, timeout: float | None = None) -> Any:
        return self.future.result(timeout=timeout)


class TaskManager:
    """Thread-pool backed. A single process-wide manager is usually enough;
    tests and the GUI can pass their own instance for isolation."""

    _default: Optional["TaskManager"] = None

    def __init__(self, *, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="vlv")
        self._tasks: dict[str, TaskHandle] = {}
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "TaskManager":
        if cls._default is None:
            cls._default = TaskManager()
        return cls._default

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "",
        token: Optional[CancellationToken] = None,
        **kwargs: Any,
    ) -> TaskHandle:
        tok = token or CancellationToken()
        task_id = uuid.uuid4().hex[:12]
        fut: Future = Future()
        handle = TaskHandle(
            task_id=task_id, name=name or fn.__name__, token=tok, future=fut
        )
        with self._lock:
            self._tasks[task_id] = handle

        def runner() -> None:
            handle.status = TaskStatus.RUNNING
            _log.debug("task start: %s (%s)", handle.name, task_id)
            try:
                if tok.is_cancelled:
                    raise _cancelled()
                result = fn(*args, token=tok, **kwargs) if _accepts_token(fn) else fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001
                handle.error = e
                handle.finished_at = time.time()
                handle.status = (
                    TaskStatus.CANCELLED if tok.is_cancelled else TaskStatus.FAILED
                )
                fut.set_exception(e)
                _log.info(
                    "task end: %s (%s) status=%s", handle.name, task_id, handle.status
                )
                return
            handle.finished_at = time.time()
            handle.status = TaskStatus.COMPLETED
            fut.set_result(result)
            _log.info("task end: %s (%s) status=completed", handle.name, task_id)

        self._pool.submit(runner)
        return handle

    def run_subprocess(
        self,
        cmd: list[str],
        *,
        name: str = "",
        timeout: Optional[float] = None,
        token: Optional[CancellationToken] = None,
        on_stdout: Optional[Callable[[str], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
        **popen_kwargs: Any,
    ) -> TaskHandle:
        """Launch `cmd` under a cancel-aware wrapper. Cancellation triggers
        `taskkill /T` on Windows and SIGTERM→SIGKILL on POSIX."""
        tok = token or CancellationToken()
        return self.submit(
            _run_subprocess_body,
            cmd,
            timeout,
            on_stdout,
            on_stderr,
            popen_kwargs,
            name=name or f"subprocess:{cmd[0] if cmd else '?'}",
            token=tok,
        )

    def active_handles(self) -> list[TaskHandle]:
        with self._lock:
            return [
                h for h in self._tasks.values()
                if h.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
            ]

    def get(self, task_id: str) -> Optional[TaskHandle]:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel_all(self) -> None:
        for h in list(self.active_handles()):
            h.cancel()

    def shutdown(self, *, wait: bool = True) -> None:
        self.cancel_all()
        self._pool.shutdown(wait=wait)


def _cancelled() -> "BaseException":
    from ..errors import TaskCancelledError

    return TaskCancelledError("cancelled before start")


def _accepts_token(fn: Callable) -> bool:
    try:
        import inspect

        sig = inspect.signature(fn)
        return "token" in sig.parameters
    except (TypeError, ValueError):
        return False


def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=5,
            )
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, AttributeError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, AttributeError):
                    proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_subprocess_body(
    cmd: list[str],
    timeout: Optional[float],
    on_stdout: Optional[Callable[[str], None]],
    on_stderr: Optional[Callable[[str], None]],
    popen_kwargs: dict,
    *,
    token: CancellationToken,
) -> int:
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    preexec = None
    if os.name != "nt":
        preexec = os.setsid
    popen_kwargs.setdefault("stdout", subprocess.PIPE if on_stdout else subprocess.DEVNULL)
    popen_kwargs.setdefault("stderr", subprocess.PIPE if on_stderr else subprocess.DEVNULL)
    popen_kwargs.setdefault("text", True)
    popen_kwargs.setdefault("encoding", "utf-8")
    popen_kwargs.setdefault("errors", "replace")
    if os.name == "nt":
        popen_kwargs.setdefault("creationflags", flags)
    else:
        popen_kwargs.setdefault("preexec_fn", preexec)
    proc = subprocess.Popen(cmd, **popen_kwargs)
    token.on_cancel(lambda: _kill_process_tree(proc))

    deadline = time.monotonic() + timeout if timeout else None
    readers: list[threading.Thread] = []
    if on_stdout and proc.stdout is not None:
        readers.append(threading.Thread(target=_pump, args=(proc.stdout, on_stdout), daemon=True))
    if on_stderr and proc.stderr is not None:
        readers.append(threading.Thread(target=_pump, args=(proc.stderr, on_stderr), daemon=True))
    for r in readers:
        r.start()

    while True:
        if proc.poll() is not None:
            break
        if token.is_cancelled:
            _kill_process_tree(proc)
            for r in readers:
                r.join(timeout=1.0)
            token.check_raise()
        if deadline and time.monotonic() > deadline:
            _kill_process_tree(proc)
            from ..errors import NetworkError

            raise NetworkError(f"subprocess timed out after {timeout:.0f}s: {cmd[0]}")
        time.sleep(0.1)

    for r in readers:
        r.join(timeout=1.0)
    return int(proc.returncode or 0)


def _pump(stream, callback) -> None:
    try:
        for line in iter(stream.readline, ""):
            try:
                callback(line.rstrip("\n"))
            except Exception:
                pass
    except Exception:
        pass
