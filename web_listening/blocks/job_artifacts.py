from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from web_listening.blocks.storage import Storage
from web_listening.models import Job


@dataclass(slots=True)
class JobArtifactEnvelopeData:
    job: Job
    artifact_path: str
    content: str
    report_payload: Optional[dict[str, object]] = None


def _read_text_if_present(path_value: str) -> str:
    candidate = Path(path_value)
    if not path_value or not candidate.exists() or not candidate.is_file():
        return ""
    return candidate.read_text(encoding="utf-8")


def load_job_or_raise(*, db_path: str | Path, job_id: int) -> Job:
    storage = Storage(db_path)
    try:
        job = storage.get_job(job_id)
    finally:
        storage.close()
    if job is None:
        raise LookupError("Job not found")
    return job


def load_job_delivery_payload_or_raise(*, db_path: str | Path, job_id: int) -> dict[str, object]:
    return load_job_or_raise(db_path=db_path, job_id=job_id).to_delivery_payload()


def load_latest_scope_report_artifact_or_raise(*, db_path: str | Path, scope_id: int) -> JobArtifactEnvelopeData:
    storage = Storage(db_path)
    try:
        job = storage.get_latest_job(scope_id=scope_id, job_type="scope.report", status="completed")
    finally:
        storage.close()
    if job is None:
        raise LookupError("Completed report artifact not found")
    artifact_path = str(job.produced_artifacts.get("output_path") or "")
    if not artifact_path:
        raise LookupError("Completed report artifact path missing")
    report_payload = job.produced_artifacts.get("report_payload")
    if not isinstance(report_payload, dict):
        report_payload = None
    return JobArtifactEnvelopeData(
        job=job,
        artifact_path=artifact_path,
        content=_read_text_if_present(artifact_path),
        report_payload=report_payload,
    )


def load_latest_scope_manifest_artifact_or_create(
    *,
    db_path: str | Path,
    scope_id: int,
    resolve_scope_path: Callable[[int], Path],
    export_manifest: Callable[..., object],
) -> JobArtifactEnvelopeData:
    storage = Storage(db_path)
    try:
        job = storage.get_latest_job(scope_id=scope_id, job_type="scope.manifest")
    finally:
        storage.close()
    if job is None:
        scope_path = resolve_scope_path(scope_id)
        started = datetime.now(timezone.utc)
        artifacts = export_manifest(scope_path=scope_path)
        storage = Storage(db_path)
        try:
            job = storage.add_job(
                Job(
                    job_type="scope.manifest",
                    status="completed",
                    stage="completed",
                    progress=100,
                    scope_id=scope_id,
                    run_id=getattr(artifacts.manifest, "run_id", None),
                    produced_artifacts={
                        "scope_path": str(scope_path),
                        "yaml_path": str(artifacts.yaml_path),
                        "report_path": str(artifacts.report_path),
                    },
                    accepted_at=started,
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                )
            )
        finally:
            storage.close()
    artifact_path = str(job.produced_artifacts.get("yaml_path") or "")
    if not artifact_path:
        raise LookupError("Manifest artifact path missing")
    return JobArtifactEnvelopeData(
        job=job,
        artifact_path=artifact_path,
        content=_read_text_if_present(artifact_path),
        report_payload=None,
    )
