from __future__ import annotations

from typing import Protocol

from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.contracts._protocol import ExecutorId


class AcquisitionExecutor(Protocol):
    """Trusted parent-side executor for a frozen capture request."""

    executor_id: ExecutorId

    def execute(self, request: CaptureRequest) -> CaptureResult:
        ...


__all__ = ["AcquisitionExecutor"]
