from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from web_listening.blocks.monitor_scope_planner import compute_scope_fingerprint, load_monitor_scope_plan
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import CrawlScope, Job

T = TypeVar("T")


def _summarize_artifacts(produced_artifacts: dict[str, object] | None) -> dict[str, object]:
    artifacts = produced_artifacts or {}
    artifact_keys = sorted(artifacts.keys())
    path_keys = [key for key in artifact_keys if key.endswith("_path")]
    return {
        "artifact_count": len(artifact_keys),
        "artifact_keys": artifact_keys,
        "path_keys": path_keys,
        "has_artifacts": bool(artifact_keys),
    }


@dataclass
class JobProgressReporter:
    job_id: int

    def update(
        self,
        *,
        status: str | None = None,
        stage: str | None = None,
        stage_message: str | None = None,
        progress: int | None = None,
        scope_id: int | None = None,
        run_id: int | None = None,
        produced_artifacts: dict[str, object] | None = None,
        artifact_summary: dict[str, object] | None = None,
        error: str | None = None,
        error_code: str | None = None,
        error_detail: dict[str, object] | None = None,
        is_retryable: bool | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> Job:
        fields: dict[str, object] = {}
        if status is not None:
            fields["status"] = status
        if stage is not None:
            fields["stage"] = stage
        if stage_message is not None:
            fields["stage_message"] = stage_message
        if progress is not None:
            fields["progress"] = max(0, min(100, int(progress)))
        if scope_id is not None:
            fields["scope_id"] = scope_id
        if run_id is not None:
            fields["run_id"] = run_id
        if produced_artifacts is not None:
            fields["produced_artifacts"] = produced_artifacts
            fields["artifact_summary"] = artifact_summary or _summarize_artifacts(produced_artifacts)
        elif artifact_summary is not None:
            fields["artifact_summary"] = artifact_summary
        if error is not None:
            fields["error"] = error
        if error_code is not None:
            fields["error_code"] = error_code
        if error_detail is not None:
            fields["error_detail"] = error_detail
        if is_retryable is not None:
            fields["is_retryable"] = is_retryable
        if started_at is not None:
            fields["started_at"] = started_at
        if finished_at is not None:
            fields["finished_at"] = finished_at

        storage = Storage(settings.db_path)
        try:
            updated = storage.update_job(self.job_id, **fields)
        finally:
            storage.close()
        if updated is None:
            raise RuntimeError(f"Job {self.job_id} disappeared during update")
        return updated


def persist_job_result(
    *,
    job_type: str,
    scope_id: int | None = None,
    run_id: int | None = None,
    produced_artifacts: dict[str, object] | None = None,
    error: str = "",
    status: str = "completed",
    accepted_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    stage: str | None = None,
    stage_message: str = "",
    error_code: str = "",
    error_detail: dict[str, object] | None = None,
    is_retryable: bool = False,
    artifact_summary: dict[str, object] | None = None,
) -> Job:
    artifacts = produced_artifacts or {}
    resolved_stage = stage or ("completed" if status == "completed" else "failed" if status == "failed" else "accepted")
    storage = Storage(settings.db_path)
    try:
        return storage.add_job(
            Job(
                job_type=job_type,
                status=status,
                stage=resolved_stage,
                stage_message=stage_message,
                progress=100 if status in {"completed", "failed"} else 0,
                scope_id=scope_id,
                run_id=run_id,
                produced_artifacts=artifacts,
                artifact_summary=artifact_summary or _summarize_artifacts(artifacts),
                error=error,
                error_code=error_code,
                error_detail=error_detail or {},
                is_retryable=is_retryable,
                accepted_at=accepted_at,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
    finally:
        storage.close()


def execute_job(*, job_type: str, scope_id: int | None, runner: Callable[..., dict[str, object]]) -> Job:
    accepted_at = datetime.now(timezone.utc)
    storage = Storage(settings.db_path)
    try:
        job = storage.add_job(
            Job(
                job_type=job_type,
                status="queued",
                stage="accepted",
                stage_message="Job accepted and waiting to start.",
                progress=0,
                scope_id=scope_id,
                accepted_at=accepted_at,
            )
        )
    finally:
        storage.close()

    reporter = JobProgressReporter(job_id=job.job_id)
    reporter.update(
        status="running",
        stage="loading_scope",
        stage_message="Loading scope inputs and preparing execution.",
        progress=5,
        started_at=accepted_at,
    )

    try:
        signature = inspect.signature(runner)
        result = runner(reporter) if len(signature.parameters) >= 1 else runner()
        result = result or {}
    except Exception as exc:
        failed_at = datetime.now(timezone.utc)
        updated = reporter.update(
            status="failed",
            stage="failed",
            stage_message="Job failed before completion.",
            progress=max(5, min(job.progress if hasattr(job, "progress") else 5, 95)),
            error=str(exc),
            error_code="job_execution_error",
            error_detail={
                "exception_type": exc.__class__.__name__,
                "message": str(exc),
                "job_type": job_type,
            },
            is_retryable=True,
            finished_at=failed_at,
        )
        job = updated
        raise

    artifacts = result.get("produced_artifacts", {})
    finished = datetime.now(timezone.utc)
    updated = reporter.update(
        status="completed",
        stage="completed",
        stage_message="Job completed successfully.",
        progress=100,
        scope_id=result.get("scope_id", scope_id),
        run_id=result.get("run_id"),
        produced_artifacts=artifacts,
        artifact_summary=result.get("artifact_summary") or _summarize_artifacts(artifacts),
        error="",
        error_code="",
        error_detail={},
        is_retryable=False,
        finished_at=finished,
    )
    return updated


def resolve_scope_plan_path(scope_id: int, *, scope: CrawlScope | None = None, data_dir: str | Path | None = None) -> Path:
    root = Path(data_dir or settings.data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Could not find data directory `{root}` for scope plan lookup.")

    plans_root = root / "plans"
    search_root = plans_root if plans_root.exists() else root
    pattern = "monitor_scope_*.yaml" if plans_root.exists() else "*.yaml"

    expected_fingerprint = None
    if scope is not None:
        expected_fingerprint = compute_scope_fingerprint(
            seed_url=scope.seed_url,
            allowed_page_prefixes=scope.allowed_page_prefixes,
            allowed_file_prefixes=scope.allowed_file_prefixes,
            fetch_mode=scope.fetch_mode,
        )

    for candidate in sorted(search_root.rglob(pattern)):
        try:
            plan = load_monitor_scope_plan(candidate)
        except Exception:
            continue
        if plan.scope_id == scope_id:
            return candidate
        if expected_fingerprint is not None and plan.scope_fingerprint == expected_fingerprint:
            return candidate
        if scope is not None and (
            plan.seed_url.rstrip("/") == scope.seed_url.rstrip("/")
            and plan.allowed_page_prefixes == scope.allowed_page_prefixes
            and plan.allowed_file_prefixes == scope.allowed_file_prefixes
        ):
            return candidate
    raise FileNotFoundError(f"Could not locate a monitor scope plan YAML for scope_id={scope_id} under `{root}`.")
