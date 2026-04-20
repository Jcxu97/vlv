"""Task system: run subprocesses and background jobs with timeout + cancellation.

Every long-running child process (yt-dlp, Whisper, local LLM serve) should go
through `TaskManager.run_subprocess()` so GUI actions can always observe and
cancel it.
"""
from .cancellation import CancellationToken
from .manager import TaskManager, TaskHandle, TaskStatus

__all__ = [
    "CancellationToken",
    "TaskManager",
    "TaskHandle",
    "TaskStatus",
]
