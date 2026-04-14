from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import CrawlScope, Job

T = TypeVar("T")


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
) -> Job:
    storage = Storage(settings.db_path)
    try:
        return storage.add_job(
            Job(
                job_type=job_type,
                status=status,
                progress=100 if status == "completed" else 0,
                scope_id=scope_id,
                run_id=run_id,
                produced_artifacts=produced_artifacts or {},
                error=error,
                accepted_at=accepted_at,
                started_at=started_at,
                finished_at=finished_at,
            )
        )
    finally:
        storage.close()


def execute_job(*, job_type: str, scope_id: int | None, runner: Callable[[], dict[str, object]]) -> Job:
    started = datetime.now(timezone.utc)
    storage = Storage(settings.db_path)
    try:
        job = storage.add_job(
            Job(
                job_type=job_type,
                status="running",
                progress=5,
                scope_id=scope_id,
                accepted_at=started,
                started_at=started,
            )
        )
    finally:
        storage.close()

    try:
        result = runner() or {}
    except Exception as exc:
        failed_at = datetime.now(timezone.utc)
        storage = Storage(settings.db_path)
        try:
            storage.update_job(
                job.job_id,
                status="failed",
                progress=100,
                error=str(exc),
                finished_at=failed_at,
            )
            updated = storage.get_job(job.job_id)
        finally:
            storage.close()
        if updated is not None:
            job = updated
        raise

    finished = datetime.now(timezone.utc)
    storage = Storage(settings.db_path)
    try:
        updated = storage.update_job(
            job.job_id,
            status="completed",
            progress=100,
            scope_id=result.get("scope_id", scope_id),
            run_id=result.get("run_id"),
            produced_artifacts=result.get("produced_artifacts", {}),
            error="",
            finished_at=finished,
        )
    finally:
        storage.close()
    return updated or job


def resolve_scope_plan_path(scope_id: int, *, scope: CrawlScope | None = None, data_dir: str | Path | None = None) -> Path:
    root = Path(data_dir or settings.data_dir)
    if not root.exists():
        raise FileNotFoundError(f"Could not find data directory `{root}` for scope plan lookup.")

    for candidate in sorted(root.rglob("*.yaml")):
        try:
            plan = load_monitor_scope_plan(candidate)
        except Exception:
            continue
        if plan.scope_id == scope_id:
            return candidate
        if scope is not None and plan.scope_fingerprint and plan.seed_url.rstrip("/") == scope.seed_url.rstrip("/"):
            return candidate
    raise FileNotFoundError(f"Could not locate a monitor scope plan YAML for scope_id={scope_id} under `{root}`.")
