import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from web_listening.api.app import create_app
from web_listening.api.routes import JobDeliveryPayload
from web_listening.blocks.job_artifacts import (
    load_job_delivery_payload_or_raise,
    load_job_or_raise,
)
from web_listening.blocks.job_orchestration import persist_job_result
from web_listening.blocks.storage import Storage
from web_listening.cli import app
from web_listening.config import settings
from web_listening.contracts import (
    AcquisitionBatchResultV2,
    acquisition_batch_result_from_initial_rejection,
    acquisition_batch_result_from_scope_run,
    aggregate_batch_result_v2,
    build_acquisition_batch_result_v2,
)
from web_listening.models import Job


def test_job_delivery_payload_validates_v2():
    payload = Job(job_type="scope.run").to_delivery_payload()
    payload["acquisition_result_v2"] = build_acquisition_batch_result_v2([])
    assert JobDeliveryPayload(**payload).acquisition_result_v2 is not None
    payload["acquisition_result_v2"]["counts"]["requested"] = 1
    with pytest.raises(ValueError):
        JobDeliveryPayload(**payload)


@pytest.fixture
def scope_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "jobs.db"))
    return SimpleNamespace(
        site_key="demo", seed_url="https://example.com/", scope_id=None,
        run_id=None, status="completed", pages_seen=2, files_seen=0,
        page_failures=0, file_failures=0,
    )


def assert_v2(payload, v1):
    assert payload["acquisition_result"] == v1
    v2 = payload["acquisition_result_v2"]
    assert v2 == aggregate_batch_result_v2(v1)
    AcquisitionBatchResultV2.model_validate_json(json.dumps(v2))
    counts = v2["counts"]
    assert counts["requested"] == sum(
        counts[name] for name in ("updated", "unchanged", "blocked", "failed", "unresolved")
    )


def test_aggregate_v2_accepts_v1_without_mutation(scope_result):
    v1 = acquisition_batch_result_from_scope_run(scope_result)
    before = deepcopy(v1)
    result = aggregate_batch_result_v2(v1)
    assert result == aggregate_batch_result_v2([v1, v1])
    assert v1 == before
    # V1 records have neither change classification nor artifact identity.
    assert result["counts"]["unresolved"] == 1
    assert result["counts"]["succeeded"] == 0


@pytest.mark.parametrize("classification,expected", [
    ("blocked", "blocked"), ("http_403", "blocked"), ("timeout", "failed"),
])
def test_aggregate_v2_preserves_v1_failure_evidence(classification, expected):
    v1 = acquisition_batch_result_from_initial_rejection(
        site_key="demo", requested_url="https://example.com/",
        outcome=SimpleNamespace(classification=classification, attempt_records=()),
    )
    result = aggregate_batch_result_v2([v1])
    assert result["counts"][expected] == 1
    assert result["counts"]["failed_evidence"] == v1["counts"]["failed_evidence"]
    assert result["dispositions"][0]["reason"] == v1["dispositions"][0]["reason"]


def test_persist_job_result_v2_additive(scope_result):
    v1 = acquisition_batch_result_from_scope_run(scope_result)
    job = persist_job_result(job_type="scope.run", acquisition_result=v1)
    loaded = load_job_or_raise(db_path=settings.db_path, job_id=job.job_id)
    assert loaded.acquisition_result == v1
    assert_v2(loaded.to_delivery_payload(), v1)
    assert loaded.acquisition_result_v2 == job.acquisition_result_v2
    assert_v2(load_job_delivery_payload_or_raise(
        db_path=settings.db_path, job_id=job.job_id
    ), v1)
    storage = Storage(settings.db_path)
    try:
        raw = storage.conn.execute(
            "SELECT acquisition_result_json FROM jobs WHERE job_id = ?", (job.job_id,)
        ).fetchone()[0]
        stored = json.loads(raw)
        assert stored.pop("acquisition_result_v2") == job.acquisition_result_v2
        assert json.dumps(stored) == json.dumps(v1)
        legacy = storage.add_job(Job(job_type="scope.run", acquisition_result=v1))
        assert legacy.acquisition_result_v2 in (None, {})
        assert storage.conn.execute(
            "SELECT acquisition_result_json FROM jobs WHERE job_id = ?",
            (legacy.job_id,),
        ).fetchone()[0] == json.dumps(v1)
    finally:
        storage.close()


def test_job_api_v2_field_exposed(scope_result):
    v1 = acquisition_batch_result_from_scope_run(scope_result)
    job = persist_job_result(job_type="scope.run", acquisition_result=v1)
    client = TestClient(create_app())
    for suffix in ("", "/payload"):
        response = client.get(f"/api/v1/jobs/{job.job_id}{suffix}")
        assert response.status_code == 200
        assert_v2(response.json(), v1)


def test_run_scope_v2_projection_additive(scope_result, tmp_path, monkeypatch):
    scope_path = tmp_path / "scope.yaml"
    profile_path = tmp_path / "profile.yaml"
    scope_path.write_text("site_key: demo\n")
    profile_path.write_text("profile_id: demo\n")
    plan = SimpleNamespace(scope_id=None)
    monkeypatch.setattr(
        "web_listening.blocks.staged_workflow.prepare_scope_execution",
        lambda **kwargs: SimpleNamespace(plan=plan),
    )
    monkeypatch.setattr(
        "web_listening.blocks.staged_workflow.run_scope",
        lambda **kwargs: SimpleNamespace(
            plan=plan, result=scope_result, report_path=tmp_path / "report.md"
        ),
    )
    args = ["run-scope", "--scope-path", str(scope_path),
            "--acquisition-profile-path", str(profile_path), "--json"]
    payloads = []
    for _ in range(2):
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        v1 = acquisition_batch_result_from_scope_run(scope_result)
        assert_v2(payload, v1)
        saved = load_job_delivery_payload_or_raise(
            db_path=settings.db_path, job_id=payload["job"]["job_id"]
        )
        assert_v2(saved, v1)
        assert saved["acquisition_result_v2"] == payload["acquisition_result_v2"]
        payloads.append(payload)
    assert payloads[0]["acquisition_result_v2"] == payloads[1]["acquisition_result_v2"]


def test_job_storage_roundtrip_v2(scope_result):
    v1 = acquisition_batch_result_from_scope_run(scope_result)
    v2 = build_acquisition_batch_result_v2([])
    original = Job(job_type="scope.run", acquisition_result=v1, acquisition_result_v2=v2)
    storage = Storage(settings.db_path)
    try:
        schema_before = storage.conn.execute("SELECT sql FROM sqlite_master").fetchall()
        saved = storage.add_job(original)
        assert storage.conn.execute("SELECT sql FROM sqlite_master").fetchall() == schema_before
    finally:
        storage.close()
    loaded = load_job_or_raise(db_path=settings.db_path, job_id=saved.job_id)
    assert loaded.acquisition_result_v2 == v2
    assert loaded.acquisition_result == v1
    assert original.acquisition_result == v1
    assert loaded.to_delivery_payload()["acquisition_result_v2"] == v2


def test_persist_job_result_rejects_invalid_v2(scope_result):
    with pytest.raises(ValueError):
        persist_job_result(job_type="scope.run", acquisition_result_v2={})


def test_persist_job_result_explicit_v2(scope_result):
    v2 = build_acquisition_batch_result_v2([])
    job = persist_job_result(job_type="scope.run", acquisition_result_v2=v2)
    assert load_job_or_raise(
        db_path=settings.db_path, job_id=job.job_id
    ).acquisition_result_v2 == v2
