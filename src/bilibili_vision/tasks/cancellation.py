"""Cooperative cancellation primitive shared by every VLV task."""
from __future__ import annotations

import threading
from typing import Callable


class CancellationToken:
    """Thread-safe one-shot cancellation signal.

    Consumers call `check_raise()` to abort early or `is_cancelled` to probe.
    Producers (the cancel button, a timeout watchdog, a parent task) call
    `cancel()` once. Callbacks registered via `on_cancel()` fire synchronously
    on the cancelling thread in LIFO order, so heavy cleanup should happen
    off-thread.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._callbacks: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        if self._event.is_set():
            return
        self._event.set()
        with self._lock:
            callbacks = list(reversed(self._callbacks))
        for cb in callbacks:
            try:
                cb()
            except Exception:
                # Never let a cleanup callback break the cancellation path.
                pass

    def check_raise(self) -> None:
        if self._event.is_set():
            from ..errors import TaskCancelledError

            raise TaskCancelledError("Task was cancelled.")

    def on_cancel(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if self._event.is_set():
                # Already cancelled — fire immediately.
                inline = True
            else:
                self._callbacks.append(callback)
                inline = False
        if inline:
            try:
                callback()
            except Exception:
                pass

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)
