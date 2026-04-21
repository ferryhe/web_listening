from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_listening.blocks.job_artifacts import (
    JobArtifactEnvelopeData,
    load_job_delivery_payload_or_raise,
    load_job_or_raise,
    load_latest_scope_manifest_artifact_or_create,
    load_latest_scope_report_artifact_or_raise,
)
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Job, Site


def test_load_job_or_raise_returns_persisted_job(tmp_path):
    db_path = tmp_path / "jobs.db"
    started = datetime.now(timezone.utc)
    storage = Storage(db_path)
    try:
        job = storage.add_job(
            Job(
                job_type="scope.report",
                status="completed",
                stage="completed",
                progress=100,
                scope_id=7,
                run_id=11,
                produced_artifacts={"output_path": str(tmp_path / "report.md")},
                accepted_at=started,
                started_at=started,
                finished_at=started,
            )
        )
    finally:
        storage.close()

    loaded = load_job_or_raise(db_path=db_path, job_id=job.job_id)
    payload = load_job_delivery_payload_or_raise(db_path=db_path, job_id=job.job_id)

    assert loaded.job_id == job.job_id
    assert payload["job"]["job_id"] == job.job_id
    assert payload["contract_version"] == "job_delivery.v1"


def test_load_latest_scope_report_artifact_or_raise_returns_envelope(tmp_path):
    db_path = tmp_path / "jobs.db"
    report_path = tmp_path / "reports" / "tracking.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("# Tracking Report\n", encoding="utf-8")
    started = datetime.now(timezone.utc)
    storage = Storage(db_path)
    try:
        job = storage.add_job(
            Job(
                job_type="scope.report",
                status="completed",
                stage="completed",
                progress=100,
                scope_id=12,
                run_id=34,
                produced_artifacts={
                    "output_path": str(report_path),
                    "output_format": "md",
                    "report_payload": {"next_action": "review_changes"},
                },
                accepted_at=started,
                started_at=started,
                finished_at=started,
            )
        )
    finally:
        storage.close()

    envelope = load_latest_scope_report_artifact_or_raise(db_path=db_path, scope_id=12)

    assert isinstance(envelope, JobArtifactEnvelopeData)
    assert envelope.job.job_id == job.job_id
    assert envelope.artifact_path == str(report_path)
    assert envelope.report_payload == {"next_action": "review_changes"}


def test_load_latest_scope_manifest_artifact_or_create_materializes_missing_job(tmp_path):
    db_path = tmp_path / "jobs.db"
    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/research"],
            allowed_file_prefixes=["/"],
            fetch_mode="http",
            is_initialized=True,
        )
    )
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
    storage.close()

    scope_path = tmp_path / "plans" / "monitor_scope_demo.yaml"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text("scope_id: 1\nsite_key: demo\n", encoding="utf-8")

    yaml_path = tmp_path / "plans" / "document_manifest_demo.yaml"
    report_path = tmp_path / "reports" / "document_manifest_demo.md"

    def fake_export_manifest(*, scope_path):
        yaml_path.write_text("manifest: demo\n", encoding="utf-8")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Manifest\n", encoding="utf-8")
        return SimpleNamespace(
            manifest=SimpleNamespace(run_id=run.id),
            yaml_path=yaml_path,
            report_path=report_path,
        )

    envelope = load_latest_scope_manifest_artifact_or_create(
        db_path=db_path,
        scope_id=scope.id,
        resolve_scope_path=lambda requested_scope_id: scope_path,
        export_manifest=fake_export_manifest,
    )

    assert envelope.artifact_path == str(yaml_path)
    assert envelope.report_payload is None

    storage = Storage(db_path)
    try:
        saved_job = storage.get_latest_job(scope_id=scope.id, job_type="scope.manifest")
    finally:
        storage.close()
    assert saved_job is not None
    assert saved_job.produced_artifacts["yaml_path"] == str(yaml_path)
    assert saved_job.artifact_summary == {
        "artifact_count": 3,
        "artifact_keys": ["report_path", "scope_path", "yaml_path"],
        "path_keys": ["report_path", "scope_path", "yaml_path"],
        "has_artifacts": True,
    }


def test_load_job_or_raise_raises_lookup_error_for_missing_job(tmp_path):
    with pytest.raises(LookupError, match="Job not found"):
        load_job_or_raise(db_path=tmp_path / "jobs.db", job_id=999)
