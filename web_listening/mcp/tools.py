from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from web_listening.blocks.acquisition_fallback import (
    DEFAULT_CHAINS,
    GOAL_PRESET_QUALITY_GATES,
    acquire_with_fallback_result,
)
from web_listening.blocks.acquisition_profile import (
    AcquisitionProfile,
    CaptureAttempt,
    build_default_acquisition_profile,
    load_acquisition_profile,
    recommend_next_adapter,
)
from web_listening.blocks.acquisition_tools import (
    AcquisitionToolError,
    acquisition_tools_catalog,
    probe_acquisition_url,
    validate_http_url,
)
from web_listening.config import settings
from web_listening.contracts.tool_result import (
    ToolResult,
    ToolResultError,
    tool_result_from_capture_attempt,
)


def web_listening_list_acquisition_tools() -> dict[str, Any]:
    """Return the acquisition tool catalog in the shared ToolResult envelope."""

    catalog = acquisition_tools_catalog()
    return _success_result(
        tool="web_listening_list_acquisition_tools",
        data={"catalog": catalog, "tools": catalog.get("tools", [])},
        data_count=len(catalog.get("tools", [])),
        stop_reason="catalog_returned",
    )


def web_listening_probe_tool_once(
    url: str,
    *,
    adapter: str | None = None,
    adapter_id: str = "web_http",
    site_key: str | None = None,
    profile_path: str | None = None,
    quality_gates: dict[str, Any] | None = None,
    safety: dict[str, Any] | None = None,
    allowed_domains: list[str] | str | None = None,
    allow_stealth_browser: bool = False,
    require_authorized_access: bool = False,
) -> dict[str, Any]:
    """Run one acquisition adapter and return a ToolResult-compatible dict.

    The public MCP schema uses ``adapter`` and nested ``safety``. ``adapter_id``
    and flattened safety fields remain accepted for compatibility with existing
    local helper callers.
    """

    try:
        safety_payload = safety or {}
        payload = probe_acquisition_url(
            url=url,
            site_key=site_key,
            adapter_id=adapter or adapter_id,
            profile_path=profile_path,
            allowed_domains=_normalize_allowed_domains(safety_payload.get("allowed_domains", allowed_domains)),
            allow_stealth_browser=bool(safety_payload.get("allow_stealth_browser", allow_stealth_browser)),
            require_authorized_access=bool(
                safety_payload.get("require_authorized_access", require_authorized_access)
            ),
        )
        attempt = CaptureAttempt(**payload["attempt"])
        profile = payload.get("profile", {})
        effective_gates = quality_gates or (profile.get("quality_gates", {}) if isinstance(profile, dict) else {})
        result = tool_result_from_capture_attempt(
            attempt,
            requested_quality_gates=quality_gates or {},
            effective_quality_gates=effective_gates,
            data={
                "profile": _safe_profile_payload(profile),
                "final_url": attempt.final_url or attempt.url,
            }
            if attempt.status == "passed"
            else {"profile": _safe_profile_payload(profile)},
            meta={"operation": "web_listening_probe_tool_once"},
        )
        return result.model_dump(mode="json")
    except AcquisitionToolError:
        return _error_result(
            "web_listening_probe_tool_once",
            code="invalid_acquisition_request",
            message="invalid acquisition request",
        )
    except Exception as exc:  # pragma: no cover - defensive MCP boundary
        return _error_result(
            "web_listening_probe_tool_once",
            code="probe_failed",
            message=_safe_error_message(exc, fallback="probe acquisition failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_recommend_next_tool(
    *,
    attempts: list[dict[str, Any]],
    strategy: str | None = None,
    safety: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    profile_path: str | None = None,
    site_key: str | None = None,
    allowed_domains: list[str] | str | None = None,
) -> dict[str, Any]:
    """Recommend the next acquisition adapter from a profile and prior attempts.

    This is a pure decision helper; it does not fetch network resources.
    """

    try:
        resolved_profile = _resolve_profile_payload(
            url="",
            profile=profile,
            profile_path=profile_path,
            site_key=site_key or "mcp-request",
            allowed_domains=allowed_domains,
            safety=safety,
            strategy=strategy,
        )
        parsed_attempts = [_capture_attempt_from_mcp_attempt(attempt) for attempt in attempts]
        next_tool = recommend_next_adapter(resolved_profile, parsed_attempts)
        return _success_result(
            tool="web_listening_recommend_next_tool",
            data={
                "next_tool": next_tool or None,
                "profile": _safe_profile_payload(resolved_profile.model_dump(mode="json")),
                "attempt_count": len(parsed_attempts),
            },
            data_count=1 if next_tool else 0,
            data_status="not_applicable",
            has_data=False,
            next_tool=next_tool or None,
            next_action=f"try_adapter:{next_tool}" if next_tool else None,
            stop_reason="next_tool_recommended" if next_tool else "no_available_adapter",
        )
    except Exception as exc:
        return _error_result(
            "web_listening_recommend_next_tool",
            code="recommendation_failed",
            message=_safe_error_message(exc, fallback="recommendation failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_acquire_with_fallback(
    url: str,
    *,
    profile: dict[str, Any] | None = None,
    profile_path: str | None = None,
    site_key: str | None = None,
    goal: str | None = None,
    goal_preset: str | None = None,
    strategy: str | None = None,
    quality_gates: dict[str, Any] | None = None,
    safety: dict[str, Any] | None = None,
    allowed_domains: list[str] | str | None = None,
    max_attempts: int | None = None,
    inline_content_limit: int = 2_000,
) -> dict[str, Any]:
    """Run the shared core fallback engine for MCP callers."""

    try:
        url = validate_http_url(url)
        if goal_preset is not None and not isinstance(goal_preset, str):
            raise AcquisitionToolError("goal_preset must be a string")
        if goal_preset is not None and goal_preset not in GOAL_PRESET_QUALITY_GATES:
            allowed = ", ".join(GOAL_PRESET_QUALITY_GATES)
            raise AcquisitionToolError(f"goal_preset must be one of: {allowed}")
        if profile_path and _has_inline_safety_override(safety=safety, allowed_domains=allowed_domains):
            raise AcquisitionToolError(
                "profile_path loads a complete acquisition profile; inline safety overrides are not allowed with profile_path"
            )
        default_allowed_domains = _default_allowed_domains_for_url(
            url,
            profile_provided=bool(profile or profile_path),
            safety=safety,
            allowed_domains=allowed_domains,
        )
        resolved_profile = _resolve_profile_payload(
            url=url,
            profile=profile,
            profile_path=profile_path,
            site_key=site_key,
            allowed_domains=default_allowed_domains,
            safety=safety,
            strategy=None,
        )
        result = acquire_with_fallback_result(
            url,
            profile=resolved_profile,
            strategy=strategy,
            goal_preset=goal_preset,
            quality_gates=quality_gates,
            allowed_domains=default_allowed_domains,
            max_attempts=max_attempts,
            inline_content_limit=inline_content_limit,
        )
        if goal:
            result = result.model_copy(update={"meta": {**result.meta, "goal": goal}})
        return result.model_dump(mode="json")
    except AcquisitionToolError as exc:
        return _error_result(
            "web_listening_acquire_with_fallback",
            code="invalid_acquisition_request",
            message=str(exc),
        )
    except Exception as exc:
        return _error_result(
            "web_listening_acquire_with_fallback",
            code="fallback_acquisition_failed",
            message=_safe_error_message(exc, fallback="fallback acquisition failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_bootstrap_scope(
    scope_path: str,
    *,
    download_files: bool = False,
    refresh_existing: bool = False,
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
    report_path: str | None = None,
    summary_path: str | None = None,
    include_summary: bool = False,
) -> dict[str, Any]:
    """Bootstrap a stored monitor scope and return its persisted job envelope."""

    try:
        from web_listening.blocks.job_orchestration import persist_job_result
        from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
        from web_listening.blocks.staged_workflow import bootstrap_scope as staged_bootstrap_scope

        plan = load_monitor_scope_plan(scope_path)
        started = datetime.now(timezone.utc)
        artifacts = staged_bootstrap_scope(
            scope_path=scope_path,
            download_files=download_files,
            refresh_existing=refresh_existing,
            max_depth=max_depth,
            max_pages=max_pages,
            max_files=max_files,
            report_path=report_path or None,
            summary_path=summary_path or None,
            include_summary=include_summary,
        )
        first = artifacts.results[0] if artifacts.results else None
        job = persist_job_result(
            job_type="scope.bootstrap",
            scope_id=first.scope_id if first else plan.scope_id,
            run_id=first.run_id if first else None,
            produced_artifacts={
                "scope_path": str(scope_path),
                "report_path": str(artifacts.report_path),
                **({"summary_path": str(artifacts.summary_path)} if artifacts.summary_path else {}),
            },
            accepted_at=started,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
        return _tool_result_from_job(job, tool="web_listening_bootstrap_scope")
    except Exception as exc:
        return _error_result(
            "web_listening_bootstrap_scope",
            code="workflow_bootstrap_failed",
            message=_safe_error_message(exc, fallback="workflow bootstrap failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_run_scope(
    scope_path: str,
    *,
    download_files: bool = False,
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
    report_path: str | None = None,
) -> dict[str, Any]:
    """Run an initialized monitor scope incrementally and return its job envelope."""

    try:
        from web_listening.blocks.job_orchestration import persist_job_result
        from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
        from web_listening.blocks.staged_workflow import run_scope as staged_run_scope

        plan = load_monitor_scope_plan(scope_path)
        started = datetime.now(timezone.utc)
        artifacts = staged_run_scope(
            scope_path=scope_path,
            download_files=download_files,
            max_depth=max_depth,
            max_pages=max_pages,
            max_files=max_files,
            report_path=report_path or None,
        )
        job = persist_job_result(
            job_type="scope.run",
            scope_id=artifacts.result.scope_id or plan.scope_id,
            run_id=artifacts.result.run_id,
            produced_artifacts={"scope_path": str(scope_path), "report_path": str(artifacts.report_path)},
            accepted_at=started,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
        return _tool_result_from_job(job, tool="web_listening_run_scope")
    except Exception as exc:
        return _error_result(
            "web_listening_run_scope",
            code="workflow_run_failed",
            message=_safe_error_message(exc, fallback="workflow run failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_report_scope(
    scope_path: str,
    *,
    task_path: str | None = None,
    run_id: int | None = None,
    output: str | None = None,
    output_format: str = "md",
    acquisition_profile_path: str | None = None,
    capture_attempt_path: str | None = None,
) -> dict[str, Any]:
    """Export a tracking report for one monitor scope and return its job envelope."""

    normalized_format = (output_format or "md").strip().lower()
    if normalized_format not in {"md", "yaml"}:
        return _error_result(
            "web_listening_report_scope",
            code="invalid_workflow_request",
            message="output_format must be one of: md, yaml",
        )

    try:
        from web_listening.blocks.job_orchestration import persist_job_result
        from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
        from web_listening.blocks.staged_workflow import report_scope as staged_report_scope

        plan = load_monitor_scope_plan(scope_path)
        started = datetime.now(timezone.utc)
        artifacts = staged_report_scope(
            scope_path=scope_path,
            task_path=task_path or None,
            run_id=run_id,
            output_path=output or None,
            output_format=normalized_format,
            acquisition_profile_path=acquisition_profile_path or None,
            capture_attempt_path=capture_attempt_path or None,
        )
        acquisition_artifacts = {
            **({"task_path": str(task_path)} if task_path else {}),
            **({"acquisition_profile_path": str(acquisition_profile_path)} if acquisition_profile_path else {}),
            **({"capture_attempt_path": str(capture_attempt_path)} if capture_attempt_path else {}),
        }
        job = persist_job_result(
            job_type="scope.report",
            scope_id=plan.scope_id,
            run_id=artifacts.report.run_id,
            produced_artifacts={
                "scope_path": str(scope_path),
                "output_path": str(artifacts.output_path),
                "output_format": artifacts.output_format,
                **acquisition_artifacts,
            },
            accepted_at=started,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
        return _tool_result_from_job(job, tool="web_listening_report_scope")
    except Exception as exc:
        return _error_result(
            "web_listening_report_scope",
            code="workflow_report_failed",
            message=_safe_error_message(exc, fallback="workflow report failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_export_manifest(
    scope_path: str,
    *,
    run_id: int | None = None,
    yaml_path: str | None = None,
    report_path: str | None = None,
    manifest_json_path: str | None = None,
    acquisition_profile_path: str | None = None,
    capture_attempt_path: str | None = None,
) -> dict[str, Any]:
    """Export scope manifest artifacts and return the persisted job envelope."""

    try:
        from web_listening.blocks.job_orchestration import persist_job_result
        from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
        from web_listening.blocks.staged_workflow import export_manifest as staged_export_manifest

        plan = load_monitor_scope_plan(scope_path)
        started = datetime.now(timezone.utc)
        artifacts = staged_export_manifest(
            scope_path=scope_path,
            run_id=run_id,
            yaml_path=yaml_path or None,
            report_path=report_path or None,
            manifest_json_path=manifest_json_path or None,
            acquisition_profile_path=acquisition_profile_path or None,
            capture_attempt_path=capture_attempt_path or None,
        )
        acquisition_artifacts = {
            **({"acquisition_profile_path": str(acquisition_profile_path)} if acquisition_profile_path else {}),
            **({"capture_attempt_path": str(capture_attempt_path)} if capture_attempt_path else {}),
        }
        job = persist_job_result(
            job_type="scope.manifest",
            scope_id=plan.scope_id,
            run_id=artifacts.manifest.run_id,
            produced_artifacts={
                "scope_path": str(scope_path),
                "manifest_json_path": str(artifacts.manifest_json_path),
                "yaml_path": str(artifacts.yaml_path),
                "report_path": str(artifacts.report_path),
                **acquisition_artifacts,
            },
            accepted_at=started,
            started_at=started,
            finished_at=datetime.now(timezone.utc),
        )
        return _tool_result_from_job(job, tool="web_listening_export_manifest")
    except Exception as exc:
        return _error_result(
            "web_listening_export_manifest",
            code="workflow_manifest_failed",
            message=_safe_error_message(exc, fallback="workflow manifest failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_get_job(job_id: int) -> dict[str, Any]:
    """Return one persisted workflow job in a ToolResult envelope."""

    try:
        from web_listening.blocks.storage import Storage

        storage = Storage(settings.db_path)
        try:
            job = storage.get_job(job_id)
        finally:
            storage.close()
        if job is None:
            return _error_result(
                "web_listening_get_job",
                code="job_not_found",
                message="job not found",
            )
        return _tool_result_from_job(job, tool="web_listening_get_job")
    except Exception as exc:
        return _error_result(
            "web_listening_get_job",
            code="job_lookup_failed",
            message=_safe_error_message(exc, fallback="job lookup failed"),
            exception_type=type(exc).__name__,
        )


def web_listening_read_artifact(
    path: str,
    *,
    inline_limit: int = 32_768,
) -> dict[str, Any]:
    """Safely read a workflow artifact under WL_DATA_DIR."""

    try:
        artifact_path = _resolve_safe_artifact_path(path)
        stat = artifact_path.stat()
        payload: dict[str, Any] = {
            "path": str(artifact_path),
            "size_bytes": stat.st_size,
            "inline_limit": inline_limit,
        }
        if stat.st_size <= max(0, inline_limit):
            try:
                content = artifact_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                payload["content"] = ""
                data_status = "artifact_only"
                stop_reason = "artifact_not_text"
            else:
                if _looks_like_text_artifact(content):
                    payload["content"] = content
                    data_status = "present"
                    stop_reason = "artifact_inlined"
                else:
                    payload["content"] = ""
                    data_status = "artifact_only"
                    stop_reason = "artifact_not_text"
        else:
            payload["content"] = ""
            data_status = "artifact_only"
            stop_reason = "artifact_too_large"
        return ToolResult(
            ok=True,
            has_data=True,
            data_status=data_status,  # type: ignore[arg-type]
            data_count=1,
            tool="web_listening_read_artifact",
            data=payload,
            artifacts={"path": str(artifact_path), "size_bytes": stat.st_size},
            stop_reason=stop_reason,
        ).model_dump(mode="json")
    except Exception as exc:
        return _error_result(
            "web_listening_read_artifact",
            code="artifact_read_failed",
            message=_safe_error_message(exc, fallback="artifact read failed"),
            exception_type=type(exc).__name__,
        )



def _resolve_profile_payload(
    *,
    url: str,
    profile: dict[str, Any] | None,
    profile_path: str | None,
    site_key: str | None,
    allowed_domains: list[str] | str | None,
    safety: dict[str, Any] | None = None,
    strategy: str | None = None,
) -> AcquisitionProfile:
    if profile and profile_path:
        raise ValueError("profile and profile_path are mutually exclusive")
    if profile:
        return AcquisitionProfile(**profile)
    if profile_path:
        return load_acquisition_profile(Path(profile_path))
    normalized_site_key = (site_key or _site_key_from_url(url)).strip()
    if not normalized_site_key:
        raise ValueError("site_key is required when url is not provided")
    safety_payload = safety or {}
    resolved = build_default_acquisition_profile(
        site_key=normalized_site_key,
        allowed_domains=_normalize_allowed_domains(safety_payload.get("allowed_domains", allowed_domains)) or [],
        allow_stealth_browser=bool(safety_payload.get("allow_stealth_browser", False)),
        require_authorized_access=bool(safety_payload.get("require_authorized_access", False)),
    )
    if strategy in DEFAULT_CHAINS:
        chain = DEFAULT_CHAINS[strategy]
        resolved = resolved.model_copy(update={"default_adapter": chain[0], "fallback_order": chain[1:]})
    return resolved


def _capture_attempt_from_mcp_attempt(payload: dict[str, Any]) -> CaptureAttempt:
    adapter = payload.get("adapter") or payload.get("tool") or "web_http"
    data_status = str(payload.get("data_status") or "")
    raw_data_quality = payload.get("data_quality")
    data_quality: dict[str, Any] = raw_data_quality if isinstance(raw_data_quality, dict) else {}
    return CaptureAttempt(
        adapter=adapter,
        status=str(payload.get("status") or _capture_status_from_data_status(data_status)),
        url=str(payload.get("url") or ""),
        final_url=str(payload.get("final_url") or ""),
        status_code=data_quality.get("status_code") or payload.get("status_code"),
        word_count=int(data_quality.get("word_count") or payload.get("word_count") or 0),
        link_count=int(data_quality.get("link_count") or payload.get("link_count") or 0),
        document_link_count=int(data_quality.get("document_link_count") or payload.get("document_link_count") or 0),
        failure_reason=str(payload.get("reason") or payload.get("failure_reason") or ""),
    )


def _capture_status_from_data_status(data_status: str) -> str:
    if data_status == "present":
        return "passed"
    if data_status in {"blocked", "permission_denied", "auth_required"}:
        return "blocked"
    if data_status == "error":
        return "error"
    return "failed_quality_gate"


def _site_key_from_url(url: str) -> str:
    hostname = urlparse(url).hostname or ""
    return hostname.replace(".", "-")


def _default_allowed_domains_for_url(
    url: str,
    *,
    profile_provided: bool,
    safety: dict[str, Any] | None,
    allowed_domains: list[str] | str | None,
) -> list[str] | None:
    if safety and safety.get("allowed_domains") is not None:
        return _normalize_allowed_domains(safety.get("allowed_domains"))
    if allowed_domains is not None:
        return _normalize_allowed_domains(allowed_domains)
    if profile_provided:
        return None
    host = urlparse(url).hostname or ""
    return [host] if host else None


def _has_inline_safety_override(*, safety: dict[str, Any] | None, allowed_domains: list[str] | str | None) -> bool:
    if allowed_domains is not None:
        return True
    if not safety:
        return False
    return any(
        field in safety
        for field in ("allowed_domains", "allow_stealth_browser", "require_authorized_access")
    )


def _normalize_allowed_domains(value: list[str] | str | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return list(value)


def _safe_error_message(exc: Exception, *, fallback: str) -> str:
    # Pydantic validation errors echo rejected input values; never reflect them
    # through an agent-facing MCP error envelope.
    if isinstance(exc, AcquisitionToolError):
        return str(exc)
    return fallback


def _safe_profile_payload(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict):
        return {}
    safe_adapters = []
    for adapter in profile.get("adapters", []):
        if not isinstance(adapter, dict):
            continue
        safe_adapters.append(
            {
                "adapter": adapter.get("adapter", ""),
                "enabled": bool(adapter.get("enabled", True)),
                "reason": adapter.get("reason", ""),
            }
        )
    return {
        "schema_version": profile.get("schema_version", ""),
        "profile_id": profile.get("profile_id", ""),
        "site_key": profile.get("site_key", ""),
        "strategy": profile.get("strategy", ""),
        "default_adapter": profile.get("default_adapter", ""),
        "fallback_order": list(profile.get("fallback_order", [])),
        "quality_gates": dict(profile.get("quality_gates", {})),
        "safety": _safe_safety_payload(profile.get("safety", {})),
        "adapters": safe_adapters,
    }


def _tool_result_from_job(job: Any, *, tool: str) -> dict[str, Any]:
    payload = job.to_delivery_payload()
    job_payload = payload.get("job", {}) if isinstance(payload, dict) else {}
    error_payload = payload.get("error", {}) if isinstance(payload, dict) else {}
    artifacts_payload = payload.get("artifacts", {}) if isinstance(payload, dict) else {}
    produced = artifacts_payload.get("produced", {}) if isinstance(artifacts_payload, dict) else {}
    status = str(job_payload.get("status") or getattr(job, "status", ""))

    if status in {"queued", "running"}:
        data_status = "running"
        ok = True
        has_data = False
        stop_reason = "job_running"
    elif status == "failed":
        data_status = "error"
        ok = False
        has_data = False
        stop_reason = "job_failed"
    elif produced:
        data_status = "artifact_only"
        ok = True
        has_data = True
        stop_reason = "job_completed_with_artifacts"
    else:
        data_status = "not_applicable"
        ok = True
        has_data = False
        stop_reason = "job_completed"

    error = None
    if not ok:
        error = ToolResultError(
            code=str(error_payload.get("code") or "job_failed") if isinstance(error_payload, dict) else "job_failed",
            message="job failed",
            retryable=bool(error_payload.get("is_retryable", False)) if isinstance(error_payload, dict) else False,
            safe_to_escalate=False,
        )

    return ToolResult(
        ok=ok,
        has_data=has_data,
        data_status=data_status,  # type: ignore[arg-type]
        data_count=len(produced) if isinstance(produced, dict) else 0,
        tool=tool,
        data={"job": job_payload, "artifact_contract": payload.get("artifact_contract", {})},
        artifacts=artifacts_payload if isinstance(artifacts_payload, dict) else {},
        next_action=payload.get("next_action") if isinstance(payload, dict) else None,
        error=error,
        stop_reason=stop_reason,
        meta={"contract_version": "web-listening-tool-result.v1", "source_contract": payload.get("contract_version")},
    ).model_dump(mode="json")


def _resolve_safe_artifact_path(path: str) -> Path:
    data_root = settings.data_dir.resolve()
    requested = Path(path)
    resolved = requested.resolve() if requested.is_absolute() else (data_root / requested).resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError("artifact path must be under WL_DATA_DIR") from exc
    if not resolved.is_file():
        raise ValueError("artifact path does not exist or is not a file")
    return resolved


def _looks_like_text_artifact(content: str) -> bool:
    return all(char in "\t\n\r" or (32 <= ord(char) < 127) or ord(char) > 159 for char in content)


def _safe_safety_payload(safety: Any) -> dict[str, Any]:
    if not isinstance(safety, dict):
        return {}
    return {
        "allowed_domains": list(safety.get("allowed_domains", [])),
        "allow_stealth_browser": bool(safety.get("allow_stealth_browser", False)),
        "require_authorized_access": bool(safety.get("require_authorized_access", False)),
    }


def _success_result(
    *,
    tool: str,
    data: dict[str, Any],
    data_count: int,
    stop_reason: str,
    data_status: str = "present",
    has_data: bool = True,
    next_tool: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    return ToolResult(
        ok=True,
        has_data=has_data,
        data_status=data_status,  # type: ignore[arg-type]
        data_count=data_count,
        tool=tool,
        data=data,
        next_tool=next_tool,
        next_action=next_action,
        stop_reason=stop_reason,
    ).model_dump(mode="json")


def _error_result(
    tool: str,
    *,
    code: str,
    message: str,
    exception_type: str = "",
) -> dict[str, Any]:
    return ToolResult(
        ok=False,
        has_data=False,
        data_status="error",
        data_count=0,
        tool=tool,
        error=ToolResultError(
            code=code,
            message=message,
            retryable=False,
            safe_to_escalate=False,
            exception_type=exception_type,
        ),
        stop_reason="error",
    ).model_dump(mode="json")


__all__ = [
    "web_listening_acquire_with_fallback",
    "web_listening_bootstrap_scope",
    "web_listening_export_manifest",
    "web_listening_get_job",
    "web_listening_list_acquisition_tools",
    "web_listening_probe_tool_once",
    "web_listening_read_artifact",
    "web_listening_recommend_next_tool",
    "web_listening_report_scope",
    "web_listening_run_scope",
]
