from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import get_args

from web_listening.contracts._protocol import ExecutorId
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.base import AcquisitionExecutor


_TRUSTED_EXECUTOR_IDS = frozenset(get_args(ExecutorId))


class ExecutorRegistry:
    """Explicit registry populated only by trusted parent code."""

    def __init__(self, executors: Mapping[ExecutorId, AcquisitionExecutor]) -> None:
        trusted: dict[ExecutorId, AcquisitionExecutor] = {}
        for executor_id, executor in executors.items():
            if executor_id not in _TRUSTED_EXECUTOR_IDS:
                raise ValueError(f"untrusted executor mapping key {executor_id!r}")
            if executor_id in trusted:
                raise ValueError(f"duplicate trusted executor mapping for {executor_id!r}")
            if executor.executor_id != executor_id:
                raise ValueError(
                    f"trusted executor mapping key {executor_id!r} does not match "
                    f"executor identity {executor.executor_id!r}"
                )
            trusted[executor_id] = executor
        self._executors = MappingProxyType(trusted)

    def get(self, executor_id: ExecutorId) -> AcquisitionExecutor:
        try:
            return self._executors[executor_id]
        except KeyError as exc:
            raise KeyError(f"no trusted executor registered for {executor_id!r}") from exc

    def execute(self, request: CaptureRequest) -> CaptureResult:
        return self.get(request.executor_id).execute(request)

    @property
    def executors(self) -> Mapping[ExecutorId, AcquisitionExecutor]:
        return self._executors


__all__ = ["ExecutorRegistry"]
