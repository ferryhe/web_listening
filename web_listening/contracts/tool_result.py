from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from web_listening.blocks.acquisition_profile import CaptureAttempt


TOOL_RESULT_CONTRACT_VERSION = "web-listening-tool-result.v1"

DataStatus = Literal[
    "present",
    "empty",
    "partial",
    "failed_quality_gate",
    "blocked",
    "not_found",
    "auth_required",
    "permission_denied",
    "error",
    "artifact_only",
    "not_applicable",
    "running",
]


class ToolResultError(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    code: str
    message: str
    retryable: bool = False
    safe_to_escalate: bool = False
    exception_type: str = ""


class ToolResultQualityGates(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    requested: dict[str, Any] = Field(default_factory=dict)
    effective: dict[str, Any] = Field(default_factory=dict)


class ToolResultDataQuality(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    passed: bool = False
    score: float | None = None
    status_code: int | None = None
    word_count: int = 0
    link_count: int = 0
    document_link_count: int = 0
    blocked: bool = False
    failure_reasons: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    """Shared result envelope for acquisition tools and future transports."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    ok: bool
    has_data: bool
    data_status: DataStatus
    data_count: int | None = None
    tool: str = ""
    data_quality: ToolResultDataQuality = Field(default_factory=ToolResultDataQuality)
    quality_gates: ToolResultQualityGates = Field(default_factory=ToolResultQualityGates)
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    next_tool: str | None = None
    next_action: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ToolResultError | None = None
    stop_reason: str = ""
    meta: dict[str, Any] = Field(
        default_factory=lambda: {"contract_version": TOOL_RESULT_CONTRACT_VERSION}
    )


_CAPTURE_STATUS_MAPPING: dict[str, tuple[bool, bool, DataStatus, str]] = {
    "passed": (True, True, "present", "usable_data_found"),
    "failed_quality_gate": (True, False, "failed_quality_gate", "insufficient_data"),
    "blocked": (True, False, "blocked", "blocked"),
    "error": (False, False, "error", "error"),
}


def tool_result_from_capture_attempt(
    attempt: CaptureAttempt,
    *,
    requested_quality_gates: Mapping[str, Any] | None = None,
    effective_quality_gates: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> ToolResult:
    """Map an existing CaptureAttempt into the shared ToolResult envelope.

    This helper is intentionally pure: it does not run adapters, recommend new
    tools, or alter capture behavior.
    """

    ok, has_data, data_status, stop_reason = _CAPTURE_STATUS_MAPPING.get(
        attempt.status,
        (False, False, "error", "error"),
    )
    failure_reasons = [attempt.failure_reason] if attempt.failure_reason else []
    next_tool = attempt.recommended_next_adapter or None
    error = None
    if data_status == "error":
        error = ToolResultError(
            code="capture_error",
            message=attempt.failure_reason or "Capture attempt failed",
        )

    result_meta = {"contract_version": TOOL_RESULT_CONTRACT_VERSION}
    if meta:
        result_meta.update(dict(meta))

    return ToolResult(
        ok=ok,
        has_data=has_data,
        data_status=data_status,
        data_count=1 if has_data else 0,
        tool=attempt.adapter,
        data_quality=ToolResultDataQuality(
            passed=has_data,
            status_code=attempt.status_code,
            word_count=attempt.word_count,
            link_count=attempt.link_count,
            document_link_count=attempt.document_link_count,
            blocked=data_status == "blocked",
            failure_reasons=failure_reasons,
        ),
        quality_gates=ToolResultQualityGates(
            requested=dict(requested_quality_gates or {}),
            effective=dict(effective_quality_gates or requested_quality_gates or {}),
        ),
        data=dict(data or {}),
        attempts=[attempt.model_dump(mode="json")],
        next_tool=next_tool,
        next_action=f"try_adapter:{next_tool}" if next_tool else None,
        error=error,
        stop_reason=stop_reason,
        meta=result_meta,
    )


__all__ = [
    "DataStatus",
    "TOOL_RESULT_CONTRACT_VERSION",
    "ToolResult",
    "ToolResultDataQuality",
    "ToolResultError",
    "ToolResultQualityGates",
    "tool_result_from_capture_attempt",
]
