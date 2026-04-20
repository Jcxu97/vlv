"""VLV error hierarchy.

Every error carries a stable code so users can quote it when reporting issues.
GUI exception handlers read .code and .user_message to render friendly dialogs.
"""
from __future__ import annotations

from typing import Optional


class VLVError(Exception):
    code: str = "VLV_E000"
    user_message: str = "An unexpected error occurred."

    def __init__(self, message: str = "", *, hint: Optional[str] = None) -> None:
        super().__init__(message or self.user_message)
        self.hint = hint

    def full(self) -> str:
        parts = [f"[{self.code}] {self.user_message}"]
        detail = str(self)
        if detail and detail != self.user_message:
            parts.append(detail)
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        return " | ".join(parts)


class NetworkError(VLVError):
    code = "VLV_E101"
    user_message = "Network request failed."


class RateLimitError(NetworkError):
    code = "VLV_E102"
    user_message = "Remote service rate-limited this request."


class LLMError(VLVError):
    code = "VLV_E201"
    user_message = "LLM call failed."


class LLMAuthError(LLMError):
    code = "VLV_E202"
    user_message = "LLM authentication failed. Check your API key."


class LLMTimeoutError(LLMError):
    code = "VLV_E203"
    user_message = "LLM call timed out."


class GPUError(VLVError):
    code = "VLV_E301"
    user_message = "Local GPU inference error."


class GPUMemoryError(GPUError):
    code = "VLV_E302"
    user_message = "Local GPU ran out of memory."


class GPUCrashError(GPUError):
    code = "VLV_E303"
    user_message = "Local inference server crashed (access violation / segfault)."


class ExtractionError(VLVError):
    code = "VLV_E401"
    user_message = "Failed to extract video data."


class UnsupportedURLError(ExtractionError):
    code = "VLV_E402"
    user_message = "URL is not recognised by any configured platform adapter."


class LoginRequiredError(ExtractionError):
    code = "VLV_E403"
    user_message = "This video requires login or a valid cookies file."


class ConfigError(VLVError):
    code = "VLV_E501"
    user_message = "Configuration error."


class TaskCancelledError(VLVError):
    code = "VLV_E601"
    user_message = "Task was cancelled by the user."
