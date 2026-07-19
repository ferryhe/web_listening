from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from dataclasses import dataclass
import math
from typing import get_args

from web_listening.contracts._protocol import ExecutorId
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.base import AcquisitionExecutor


_TRUSTED_EXECUTOR_IDS = frozenset(get_args(ExecutorId))


@dataclass(frozen=True, slots=True)
class ExecutorMetadata:
    """Immutable, parent-supplied facts used by read-only plan compilation."""

    executor_id: ExecutorId
    version: str
    capabilities: frozenset[str]
    timeout_seconds: float
    stdout_bytes: int
    stderr_bytes: int
    requires_authorized_access: bool = False


def _normalize_metadata(executor_id: ExecutorId, item: ExecutorMetadata) -> ExecutorMetadata:
    if executor_id not in _TRUSTED_EXECUTOR_IDS or item.executor_id != executor_id:
        raise ValueError(f"untrusted executor metadata for {executor_id!r}")
    if not item.version.strip() or not item.capabilities:
        raise ValueError("executor metadata requires version and capabilities")
    if not isinstance(item.capabilities, (set, frozenset)) or any(
            not isinstance(capability, str) or not capability.strip()
            for capability in item.capabilities):
        raise ValueError("executor metadata capabilities must be non-empty strings")
    if (isinstance(item.timeout_seconds, bool) or not isinstance(item.timeout_seconds, (int, float))
            or not math.isfinite(item.timeout_seconds) or item.timeout_seconds <= 0):
        raise ValueError("executor metadata timeout must be positive and finite")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (item.stdout_bytes, item.stderr_bytes)):
        raise ValueError("executor metadata byte limits must be positive integers")
    if type(item.requires_authorized_access) is not bool:
        raise ValueError("executor metadata requires_authorized_access must be a boolean")
    return ExecutorMetadata(
        executor_id=item.executor_id,
        version=item.version,
        capabilities=frozenset(item.capabilities),
        timeout_seconds=item.timeout_seconds,
        stdout_bytes=item.stdout_bytes,
        stderr_bytes=item.stderr_bytes,
        requires_authorized_access=item.requires_authorized_access,
    )


class ExecutorRegistry:
    """Explicit registry populated only by trusted parent code."""

    def __init__(self, executors: Mapping[ExecutorId, AcquisitionExecutor], *, metadata: Mapping[ExecutorId, ExecutorMetadata] | None = None) -> None:
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
        trusted_metadata: dict[ExecutorId, ExecutorMetadata] = {}
        for executor_id, item in (metadata or {}).items():
            if executor_id not in trusted:
                raise ValueError(f"untrusted executor metadata for {executor_id!r}")
            trusted_metadata[executor_id] = _normalize_metadata(executor_id, item)
        self._metadata = MappingProxyType(trusted_metadata)

    @classmethod
    def preview(cls, metadata: Mapping[ExecutorId, ExecutorMetadata]) -> ExecutorRegistry:
        instance = cls({})
        checked: dict[ExecutorId, ExecutorMetadata] = {}
        for executor_id, item in metadata.items():
            checked[executor_id] = _normalize_metadata(executor_id, item)
        instance._metadata = MappingProxyType(checked)
        return instance

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

    @property
    def metadata(self) -> Mapping[ExecutorId, ExecutorMetadata]:
        return self._metadata


def default_preview_registry() -> ExecutorRegistry:
    """Static product metadata only; constructing it never discovers a runtime."""
    capabilities = {
        "web_http": {"http_get"}, "browser_rendered": {"browser_render"},
        "sitemap": {"sitemap_read"}, "rss": {"rss_read"},
        "cloakbrowser": {"browser_render", "stealth_browser"},
        "browseract": {"browser_read_only"}, "batch_python": {"batch_extract"},
    }
    from web_listening.executors.browseract import BROWSERACT_VERSION
    versions = {executor_id: "1.0.0" for executor_id in capabilities}
    versions["browseract"] = BROWSERACT_VERSION
    return ExecutorRegistry.preview({
        executor_id: ExecutorMetadata(executor_id, versions[executor_id], frozenset(caps), 30.0, 4 * 1024 * 1024, 64 * 1024,
                                      executor_id in {"cloakbrowser", "browseract"})
        for executor_id, caps in capabilities.items()
    })


__all__ = ["ExecutorMetadata", "ExecutorRegistry", "default_preview_registry"]
