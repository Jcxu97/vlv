"""Tests for the VLV error hierarchy."""
from __future__ import annotations

import pytest


def test_error_codes_unique():
    from bilibili_vision import errors

    codes = []
    for name in dir(errors):
        cls = getattr(errors, name)
        if isinstance(cls, type) and issubclass(cls, errors.VLVError):
            codes.append(cls.code)
    assert len(codes) == len(set(codes)), f"duplicate error codes: {codes}"


def test_error_full_includes_code_and_message():
    from bilibili_vision.errors import ExtractionError

    err = ExtractionError("could not parse info dict", hint="try --no-check-certificate")
    msg = err.full()
    assert "VLV_E401" in msg
    assert "could not parse info dict" in msg
    assert "try --no-check-certificate" in msg


def test_hierarchy_isinstance():
    from bilibili_vision.errors import (
        VLVError,
        LLMError,
        LLMTimeoutError,
        GPUError,
        GPUMemoryError,
    )

    assert issubclass(LLMTimeoutError, LLMError)
    assert issubclass(GPUMemoryError, GPUError)
    assert issubclass(LLMError, VLVError)
