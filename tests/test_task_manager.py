"""Tests for the task system: cancellation tokens and TaskManager."""
from __future__ import annotations

import sys
import time

import pytest


def test_cancellation_token_basics():
    from bilibili_vision.tasks import CancellationToken
    from bilibili_vision.errors import TaskCancelledError

    t = CancellationToken()
    assert not t.is_cancelled
    t.check_raise()  # no-op
    t.cancel()
    assert t.is_cancelled
    with pytest.raises(TaskCancelledError):
        t.check_raise()


def test_cancellation_callbacks_fire_lifo():
    from bilibili_vision.tasks import CancellationToken

    fired: list[int] = []
    t = CancellationToken()
    t.on_cancel(lambda: fired.append(1))
    t.on_cancel(lambda: fired.append(2))
    t.cancel()
    assert fired == [2, 1]

    # Registering after cancellation fires inline.
    t.on_cancel(lambda: fired.append(3))
    assert fired == [2, 1, 3]


def test_task_manager_runs_and_completes():
    from bilibili_vision.tasks import TaskManager, TaskStatus

    mgr = TaskManager(max_workers=2)
    try:
        h = mgr.submit(lambda: 42, name="answer")
        assert h.wait(timeout=5.0) == 42
        assert h.status == TaskStatus.COMPLETED
    finally:
        mgr.shutdown()


def test_task_manager_cancels_cooperative_task():
    from bilibili_vision.tasks import TaskManager, CancellationToken, TaskStatus
    from bilibili_vision.errors import TaskCancelledError

    mgr = TaskManager(max_workers=2)
    try:
        def work(*, token: CancellationToken) -> None:
            for _ in range(50):
                token.check_raise()
                time.sleep(0.05)

        h = mgr.submit(work, name="busy")
        time.sleep(0.1)
        h.cancel()
        with pytest.raises(TaskCancelledError):
            h.wait(timeout=5.0)
        assert h.status == TaskStatus.CANCELLED
    finally:
        mgr.shutdown()


def test_task_manager_captures_exception():
    from bilibili_vision.tasks import TaskManager, TaskStatus

    mgr = TaskManager(max_workers=2)
    try:
        def boom():
            raise ValueError("oops")

        h = mgr.submit(boom, name="err")
        with pytest.raises(ValueError, match="oops"):
            h.wait(timeout=2.0)
        assert h.status == TaskStatus.FAILED
        assert isinstance(h.error, ValueError)
    finally:
        mgr.shutdown()


def test_task_manager_subprocess_captures_stdout():
    from bilibili_vision.tasks import TaskManager

    mgr = TaskManager(max_workers=2)
    captured: list[str] = []
    try:
        h = mgr.run_subprocess(
            [sys.executable, "-c", "print('hello-from-subprocess')"],
            name="echo",
            on_stdout=captured.append,
        )
        rc = h.wait(timeout=15.0)
        assert rc == 0
        assert any("hello-from-subprocess" in s for s in captured)
    finally:
        mgr.shutdown()


def test_task_manager_subprocess_cancellable():
    from bilibili_vision.tasks import TaskManager
    from bilibili_vision.errors import TaskCancelledError

    mgr = TaskManager(max_workers=2)
    try:
        h = mgr.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            name="sleeper",
        )
        time.sleep(0.3)
        h.cancel()
        with pytest.raises(TaskCancelledError):
            h.wait(timeout=10.0)
    finally:
        mgr.shutdown()


def test_active_handles_tracks_running():
    from bilibili_vision.tasks import TaskManager

    mgr = TaskManager(max_workers=2)
    try:
        h = mgr.submit(lambda: time.sleep(0.5), name="sleeping")
        time.sleep(0.1)
        active = mgr.active_handles()
        assert any(x.task_id == h.task_id for x in active)
        h.wait(timeout=2.0)
        active2 = mgr.active_handles()
        assert not any(x.task_id == h.task_id for x in active2)
    finally:
        mgr.shutdown()
