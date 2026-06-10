from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlparse
from typing import Any, Literal

from web_listening.blocks.acquisition_capture import build_builtin_adapters, run_capture_attempt
from web_listening.blocks.acquisition_profile import (
    AdapterId,
    AcquisitionAdapterConfig,
    AcquisitionProfile,
    AcquisitionQualityGates,
    AcquisitionSafetyPolicy,
    CaptureAttempt,
    build_default_acquisition_profile,
)
from web_listening.contracts.tool_result import (
    ToolResult,
    ToolResultDataQuality,
    ToolResultError,
    ToolResultQualityGates,
    tool_result_from_capture_attempt,
)

DEFAULT_CHAINS: dict[str, list[AdapterId]] = {
    "public_web_default": ["web_http", "browser_rendered", "sitemap", "rss"],
    "document_discovery": ["web_http", "sitemap", "browser_rendered", "rss"],
    "dynamic_page_default": ["web_http", "browser_rendered", "sitemap", "rss"],
    "authorized_fallback": ["web_http", "browser_rendered", "cloakbrowser", "batch_python"],
}

GoalPreset = Literal["page_text", "section_discovery", "document_discovery", "change_monitoring"]

GOAL_PRESET_QUALITY_GATES: dict[GoalPreset, dict[str, Any]] = {
    "page_text": {"min_words": 120, "min_links": 0, "min_document_links": 0},
    "section_discovery": {"min_words": 80, "min_links": 3, "min_document_links": 0},
    "document_discovery": {"min_words": 80, "min_links": 0, "min_document_links": 1},
    "change_monitoring": {"min_words": 20, "min_links": 0, "min_document_links": 0},
}

_RESERVED_ADAPTER_REASON = "adapter is reserved / not probe-capable in this build"
_TERMINAL_STATUSES = {"present", "artifact_only", "not_found", "auth_required", "permission_denied", "running"}


def acquire_with_fallback_result(
    url: str,
    *,
    profile: AcquisitionProfile | None = None,
    adapters: Mapping[str, Any] | None = None,
    quality_gates: AcquisitionQualityGates | Mapping[str, Any] | None = None,
    allowed_domains: Sequence[str] | str | None = None,
    strategy: str | None = None,
    goal_preset: GoalPreset | str | None = None,
    max_attempts: int | None = None,
    inline_content_limit: int = 2_000,
) -> ToolResult:
    """Run acquisition attempts in fallback order and return one ToolResult.

    The engine deliberately stays small and core-only: callers may provide fake
    adapters for tests or dependency injection, otherwise built-in executable
    adapters are used. Reserved adapters in a profile chain are recorded as
    non-terminal ``not_applicable`` attempts instead of failing the chain.
    """

    resolved_goal_preset = _resolve_goal_preset(goal_preset)
    preset_quality_gates = _goal_preset_quality_gates(resolved_goal_preset)
    effective_quality_gates_input = quality_gates if quality_gates is not None else preset_quality_gates
    requested_gates = _requested_quality_gates(quality_gates)
    resolved_profile = _resolve_profile(
        url,
        profile=profile,
        quality_gates=effective_quality_gates_input,
        allowed_domains=allowed_domains,
        strategy=strategy,
    )
    effective_gates = resolved_profile.quality_gates.model_dump(mode="json")
    warnings: list[str] = []

    def finalize(result: ToolResult) -> ToolResult:
        return _with_goal_preset_meta(result, resolved_goal_preset)

    input_safety_error = _input_url_safety_error(url, resolved_profile.safety.allowed_domains)
    if input_safety_error is not None:
        return finalize(_terminal_result(
            tool="",
            data_status="permission_denied",
            stop_reason="unsafe_url",
            attempts=[],
            requested_quality_gates=requested_gates,
            effective_quality_gates=effective_gates,
            warnings=[input_safety_error],
            error=ToolResultError(
                code="unsafe_url",
                message=input_safety_error,
                retryable=False,
                safe_to_escalate=False,
            ),
        ))

    executable_adapters = dict(adapters) if adapters is not None else build_builtin_adapters()
    chain = _adapter_chain(resolved_profile, strategy)
    if max_attempts is not None:
        chain = chain[: max(0, max_attempts)]

    capture_attempts: list[CaptureAttempt] = []
    attempt_records: list[dict[str, Any]] = []
    last_result: ToolResult | None = None

    for adapter_id in chain:
        if _adapter_disabled(resolved_profile, adapter_id):
            skipped = _skipped_result(
                url,
                adapter_id,
                reason="adapter is disabled in the acquisition profile",
                requested_quality_gates=requested_gates,
                effective_quality_gates=effective_gates,
            )
            attempt_records.append(_attempt_record(skipped, skipped=True))
            last_result = skipped
            continue

        if adapter_id == "cloakbrowser" and not resolved_profile.safety.permits_cloakbrowser:
            unsafe_warning = "cloakbrowser fallback requires authorized stealth-browser safety flags"
            if last_result is None:
                return finalize(_terminal_result(
                    tool="cloakbrowser",
                    data_status="permission_denied",
                    stop_reason="unsafe_escalation",
                    attempts=attempt_records,
                    requested_quality_gates=requested_gates,
                    effective_quality_gates=effective_gates,
                    warnings=[unsafe_warning],
                    error=ToolResultError(
                        code="unsafe_escalation",
                        message=unsafe_warning,
                        retryable=False,
                        safe_to_escalate=False,
                    ),
                ))
            return finalize(_terminal_result(
                tool="cloakbrowser",
                data_status="permission_denied",
                stop_reason="unsafe_escalation",
                attempts=attempt_records,
                requested_quality_gates=requested_gates,
                effective_quality_gates=effective_gates,
                warnings=[unsafe_warning],
                error=ToolResultError(
                    code="unsafe_escalation",
                    message=unsafe_warning,
                    retryable=False,
                    safe_to_escalate=False,
                ),
            ))

        adapter = executable_adapters.get(adapter_id)
        if adapter is None:
            skipped = _skipped_result(
                url,
                adapter_id,
                reason=_RESERVED_ADAPTER_REASON,
                requested_quality_gates=requested_gates,
                effective_quality_gates=effective_gates,
            )
            attempt_records.append(_attempt_record(skipped, skipped=True))
            last_result = skipped
            continue

        attempt = run_capture_attempt(url, adapter, resolved_profile, capture_attempts)
        capture_attempts.append(attempt)
        result = _result_from_attempt(
            attempt,
            requested_quality_gates=requested_gates,
            effective_quality_gates=effective_gates,
            inline_content_limit=inline_content_limit,
        )
        result = _apply_status_code_terminal_mapping(result, attempt)
        final_url_warning = _final_url_safety_warning(
            attempt.final_url,
            resolved_profile.safety.allowed_domains,
        )
        if final_url_warning:
            result = result.model_copy(
                update={
                    "ok": False,
                    "has_data": False,
                    "data_status": "permission_denied",
                    "data_count": 0,
                    "data": {},
                    "data_quality": result.data_quality.model_copy(
                        update={
                            "passed": False,
                            "failure_reasons": [*result.data_quality.failure_reasons, final_url_warning],
                        }
                    ),
                    "warnings": [*result.warnings, final_url_warning],
                    "error": ToolResultError(
                        code="unsafe_final_url",
                        message=final_url_warning,
                        retryable=False,
                        safe_to_escalate=False,
                    ),
                    "stop_reason": "unsafe_final_url",
                    "next_tool": None,
                    "next_action": None,
                }
            )
        attempt_records.append(_attempt_record(result))
        last_result = result

        if not should_continue(result):
            return finalize(_with_attempt_history(result, attempt_records, warnings))

    if last_result is None:
        return finalize(_terminal_result(
            tool="",
            data_status="not_applicable",
            stop_reason="no_available_adapter",
            attempts=attempt_records,
            requested_quality_gates=requested_gates,
            effective_quality_gates=effective_gates,
            warnings=warnings,
        ))

    stop_reason = "max_attempts_reached" if max_attempts is not None and len(chain) >= max_attempts else "no_available_adapter"
    return finalize(_with_attempt_history(
        last_result.model_copy(update={"next_tool": None, "next_action": None, "stop_reason": stop_reason}),
        attempt_records,
        warnings,
    ))


def should_continue(result: ToolResult) -> bool:
    if result.data_status in _TERMINAL_STATUSES:
        return False
    if result.data_status in {"empty", "failed_quality_gate", "blocked"}:
        return True
    if result.data_status == "not_applicable":
        return bool(result.data.get("skipped"))
    if result.data_status == "error":
        if result.error is None:
            return False
        return result.error.retryable and result.error.safe_to_escalate
    return False


def _resolve_profile(
    url: str,
    *,
    profile: AcquisitionProfile | None,
    quality_gates: AcquisitionQualityGates | Mapping[str, Any] | None,
    allowed_domains: Sequence[str] | str | None,
    strategy: str | None,
) -> AcquisitionProfile:
    parsed = urlparse(url)
    site_key = (parsed.hostname or "site").replace(".", "-")
    resolved = profile or build_default_acquisition_profile(site_key=site_key)

    updates: dict[str, Any] = {}
    if quality_gates is not None:
        updates["quality_gates"] = _coerce_quality_gates(quality_gates)
    if allowed_domains is not None:
        safety_payload = resolved.safety.model_dump(mode="json")
        safety_payload["allowed_domains"] = allowed_domains
        updates["safety"] = AcquisitionSafetyPolicy(**safety_payload)
    if strategy in DEFAULT_CHAINS:
        chain = DEFAULT_CHAINS[strategy]
        updates["default_adapter"] = chain[0]
        updates["fallback_order"] = chain[1:]
        updates["adapters"] = _profile_adapters_for_chain(resolved, chain)
    return resolved.model_copy(update=updates)


def _resolve_goal_preset(goal_preset: GoalPreset | str | None) -> GoalPreset | None:
    if goal_preset is None:
        return None
    if not isinstance(goal_preset, str):
        raise ValueError("goal_preset must be a string")
    if goal_preset not in GOAL_PRESET_QUALITY_GATES:
        allowed = ", ".join(GOAL_PRESET_QUALITY_GATES)
        raise ValueError(f"goal_preset must be one of: {allowed}")
    return goal_preset  # type: ignore[return-value]


def _goal_preset_quality_gates(goal_preset: GoalPreset | None) -> AcquisitionQualityGates | None:
    if goal_preset is None:
        return None
    return AcquisitionQualityGates(**GOAL_PRESET_QUALITY_GATES[goal_preset])


def _profile_adapters_for_chain(
    profile: AcquisitionProfile,
    chain: Sequence[AdapterId],
) -> list[AcquisitionAdapterConfig]:
    existing = {adapter.adapter: adapter for adapter in profile.adapters}
    return [existing.get(adapter_id, AcquisitionAdapterConfig(adapter=adapter_id)) for adapter_id in chain]


def _coerce_quality_gates(
    quality_gates: AcquisitionQualityGates | Mapping[str, Any],
) -> AcquisitionQualityGates:
    if isinstance(quality_gates, AcquisitionQualityGates):
        return quality_gates
    return AcquisitionQualityGates(**dict(quality_gates))


def _requested_quality_gates(
    quality_gates: AcquisitionQualityGates | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if quality_gates is None:
        return {}
    if isinstance(quality_gates, AcquisitionQualityGates):
        return quality_gates.model_dump(mode="json")
    return dict(quality_gates)


def _adapter_chain(profile: AcquisitionProfile, strategy: str | None) -> list[AdapterId]:
    if strategy in DEFAULT_CHAINS:
        return list(DEFAULT_CHAINS[strategy])
    chain: list[AdapterId] = [profile.default_adapter]
    for adapter_id in profile.fallback_order:
        if adapter_id not in chain:
            chain.append(adapter_id)
    return chain


def _adapter_disabled(profile: AcquisitionProfile, adapter_id: str) -> bool:
    return any(adapter.adapter == adapter_id and not adapter.enabled for adapter in profile.adapters)


def _result_from_attempt(
    attempt: CaptureAttempt,
    *,
    requested_quality_gates: Mapping[str, Any],
    effective_quality_gates: Mapping[str, Any],
    inline_content_limit: int,
) -> ToolResult:
    result = tool_result_from_capture_attempt(
        attempt,
        requested_quality_gates=requested_quality_gates,
        effective_quality_gates=effective_quality_gates,
        data=_safe_attempt_data(attempt, inline_content_limit=inline_content_limit),
    )
    structured_error = _structured_error_from_attempt(attempt)
    if structured_error is not None:
        result = result.model_copy(update={"error": structured_error})
    return result


def _structured_error_from_attempt(attempt: CaptureAttempt) -> ToolResultError | None:
    payload = attempt.metadata.get("error")
    if not isinstance(payload, Mapping):
        return None
    return ToolResultError(
        code=str(payload.get("code") or "capture_error"),
        message=str(payload.get("message") or attempt.failure_reason or "Capture attempt failed"),
        retryable=bool(payload.get("retryable", False)),
        safe_to_escalate=bool(payload.get("safe_to_escalate", False)),
        exception_type=str(payload.get("exception_type") or ""),
    )


def _apply_status_code_terminal_mapping(result: ToolResult, attempt: CaptureAttempt) -> ToolResult:
    if attempt.status_code in {404, 410}:
        return _status_code_terminal_result(result, data_status="not_found", stop_reason="not_found")
    if attempt.status_code == 401:
        return _status_code_terminal_result(result, data_status="auth_required", stop_reason="auth_required")
    if attempt.status_code == 403 and result.data_status != "blocked":
        return _status_code_terminal_result(result, data_status="permission_denied", stop_reason="permission_denied")
    return result


def _status_code_terminal_result(result: ToolResult, *, data_status: Any, stop_reason: str) -> ToolResult:
    failure_reasons = list(result.data_quality.failure_reasons)
    if not failure_reasons:
        failure_reasons.append(stop_reason)
    return result.model_copy(
        update={
            "has_data": False,
            "data_status": data_status,
            "data_count": 0,
            "data": {},
            "data_quality": result.data_quality.model_copy(
                update={"passed": False, "failure_reasons": failure_reasons}
            ),
            "stop_reason": stop_reason,
            "next_tool": None,
            "next_action": None,
        }
    )

def _safe_attempt_data(attempt: CaptureAttempt, *, inline_content_limit: int) -> dict[str, Any]:
    if attempt.status != "passed":
        return {}
    data = {"final_url": attempt.final_url or attempt.url}
    preview = attempt.metadata.get("content_text_preview") or attempt.metadata.get("markdown_preview")
    if isinstance(preview, str) and inline_content_limit > 0:
        data["content_text_preview"] = preview[:inline_content_limit]
    return data


def _skipped_result(
    url: str,
    adapter_id: AdapterId,
    *,
    reason: str,
    requested_quality_gates: Mapping[str, Any],
    effective_quality_gates: Mapping[str, Any],
) -> ToolResult:
    return ToolResult(
        ok=True,
        has_data=False,
        data_status="not_applicable",
        data_count=0,
        tool=adapter_id,
        quality_gates=ToolResultQualityGates(
            requested=dict(requested_quality_gates),
            effective=dict(effective_quality_gates),
        ),
        data={"url": url, "skipped": True, "reason": reason},
        stop_reason="adapter_not_applicable",
    )


def _terminal_result(
    *,
    tool: str,
    data_status: Any,
    stop_reason: str,
    attempts: list[dict[str, Any]],
    requested_quality_gates: Mapping[str, Any],
    effective_quality_gates: Mapping[str, Any],
    warnings: list[str] | None = None,
    error: ToolResultError | None = None,
) -> ToolResult:
    return ToolResult(
        ok=error is None,
        has_data=False,
        data_status=data_status,
        data_count=0,
        tool=tool,
        quality_gates=ToolResultQualityGates(
            requested=dict(requested_quality_gates),
            effective=dict(effective_quality_gates),
        ),
        attempts=attempts,
        warnings=warnings or [],
        error=error,
        stop_reason=stop_reason,
    )


def _with_attempt_history(
    result: ToolResult,
    attempts: list[dict[str, Any]],
    warnings: list[str],
) -> ToolResult:
    return result.model_copy(update={"attempts": attempts, "warnings": [*warnings, *result.warnings]})


def _with_goal_preset_meta(result: ToolResult, goal_preset: GoalPreset | None) -> ToolResult:
    if goal_preset is None:
        return result
    return result.model_copy(update={"meta": {**result.meta, "goal_preset": goal_preset}})


def _attempt_record(result: ToolResult, *, skipped: bool = False) -> dict[str, Any]:
    record: dict[str, Any] = {
        "tool": result.tool,
        "ok": result.ok,
        "has_data": result.has_data,
        "data_status": result.data_status,
        "stop_reason": result.stop_reason,
    }
    if skipped:
        record["skipped"] = True
        record["reason"] = str(result.data.get("reason", ""))
    if result.data_quality != ToolResultDataQuality():
        record["data_quality"] = result.data_quality.model_dump(mode="json")
    if result.error is not None:
        record["error"] = result.error.model_dump(mode="json")
    if result.warnings:
        record["warnings"] = list(result.warnings)
    return record


def _input_url_safety_error(url: str, allowed_domains: Sequence[str]) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "url must be an absolute http(s) URL"
    if allowed_domains and not _host_allowed(parsed.hostname, allowed_domains):
        return f"url host {parsed.hostname} is outside allowed_domains"
    return None


def _final_url_safety_warning(final_url: str, allowed_domains: Sequence[str]) -> str:
    if not final_url or not allowed_domains:
        return ""
    parsed = urlparse(final_url)
    if parsed.hostname and _host_allowed(parsed.hostname, allowed_domains):
        return ""
    return f"final_url host {parsed.hostname or '<unknown>'} is outside allowed_domains"


def _host_allowed(host: str, allowed_domains: Sequence[str]) -> bool:
    normalized_host = host.rstrip(".").casefold()
    for domain in allowed_domains:
        normalized_domain = str(domain).strip().rstrip(".").casefold()
        if normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}"):
            return True
    return False


__all__ = [
    "DEFAULT_CHAINS",
    "GOAL_PRESET_QUALITY_GATES",
    "GoalPreset",
    "acquire_with_fallback_result",
    "should_continue",
]
