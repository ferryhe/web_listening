from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from web_listening.api.app import create_app
from web_listening.api import routes
from web_listening.blocks.access_gateway import (
    AccessGateway,
    AccessGatewayBudgetError,
    AccessGatewayConfig,
    AccessGatewayOriginError,
    AccessGatewayRedirectError,
    AccessGatewayTransportError,
)
from web_listening.blocks.acquisition_gateway import GovernedAcquisitionGateway
from web_listening.blocks.governed_read import (
    AccessRejectedError,
    GovernedReadGateway,
    governed_read_failure_payload,
)
from web_listening.blocks.monitor_scope_planner import (
    MonitorScopePlan,
    compute_scope_fingerprint,
    render_yaml_text,
)
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    RawHttpResponse,
    normalize_http_url,
)
from web_listening.blocks.storage import (
    ExecutionArtifactOwnershipError,
    Storage,
)
from web_listening.blocks.tree_crawler import TreeCrawler
from web_listening.cli import app as cli_app
from web_listening.contracts import CaptureContent, CaptureResult
from web_listening.contracts.site_diagnostic import DiagnosticIdentity, canonical_sha256
from web_listening.mcp.tools import (
    web_listening_bootstrap_scope,
)
from web_listening.models import CrawlScope, Job, Site
import web_listening.blocks.staged_workflow as staged_workflow
import web_listening.blocks.tree_bootstrap_workflow as tree_bootstrap_workflow


def _scope_plan(
    *, seed_url: str = "https://example.com/", max_pages: int = 2
) -> MonitorScopePlan:
    return MonitorScopePlan(
        scope_fingerprint="scope-fingerprint",
        site_key="demo",
        display_name="Demo",
        catalog="dev",
        generated_at="2026-08-21T00:00:00Z",
        selection_review_status="approved",
        selection_mode="manual",
        business_goal="Track governed content.",
        seed_url=seed_url,
        homepage_url=seed_url,
        fetch_mode="http",
        fetch_config_json={},
        tree_strategy="selected_scope",
        tree_budget_profile="selected_scope_default",
        file_scope_mode="site_root",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
        scope_id=1,
        max_depth=0,
        max_pages=max_pages,
        max_files=0,
    )


def _write_authority(tmp_path: Path, plan: MonitorScopePlan) -> tuple[Path, Path]:
    scope_path = tmp_path / "scope.yaml"
    profile_path = tmp_path / "profile.yaml"
    scope_path.write_text(render_yaml_text(plan), encoding="utf-8")
    profile_path.write_text("profile_id: sealed-profile\n", encoding="utf-8")
    return scope_path, profile_path


class _Transport:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.requests: list[str] = []

    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
    ) -> RawHttpResponse:
        del user_agent, identity_sha256
        self.requests.append(url)
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        status, body, headers = response
        return RawHttpResponse(status, headers, [body])


def _governed_gateway(
    transport: _Transport,
    *,
    budget_limit: int = 8,
    max_body_bytes: int = 1024 * 1024,
) -> GovernedAcquisitionGateway:
    visible_identity = {
        "identity_id": "prepared-scope-test",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/2.0",
    }
    identity = DiagnosticIdentity(
        **visible_identity,
        identity_sha256=canonical_sha256(visible_identity),
    )
    reader = GovernedReadGateway(
        AccessGateway(
            AccessGatewayConfig(
                identity=identity,
                allowed_origins=frozenset(
                    {
                        normalize_http_url("https://example.com/")[1],
                        normalize_http_url("https://changed.example/")[1],
                    }
                ),
                diagnostic_artifact_sha256="a" * 64,
                pacing_interval=timedelta(0),
                budget_limit=budget_limit,
            ),
            transport=transport,
        ),
        max_body_bytes=max_body_bytes,
    )
    step = {
        "position": 0,
        "executor_id": "web_http",
        "executor_version": "2.0.0",
        "recipe_id": "sealed-http",
        "script_sha256": "b" * 64,
        "config": {},
        "limits": {},
    }
    compiled = SimpleNamespace(
        mode="governed",
        steps=(step,),
        acquisition_fingerprint="c" * 64,
        scope_fingerprint="scope-fingerprint",
        profile_id="sealed-profile",
        site_key="demo",
        site_skill_id="demo-skill",
        site_skill_version="1.0.0",
        site_skill_package_sha256="d" * 64,
        scope_budgets={"max_depth": 0, "max_pages": 2, "max_files": 0},
        quality_gates={
            "min_words": 0,
            "min_links": 0,
            "min_document_links": 0,
            "blocked_markers": (),
        },
    )

    class Registry:
        executors = {}

        @staticmethod
        def execute(request):
            response = reader.read(str(request.url))
            lineage = {
                field: getattr(request, field)
                for field in (
                    "request_id",
                    "site_key",
                    "site_skill_id",
                    "site_skill_version",
                    "site_skill_digest",
                    "recipe_id",
                    "run_id",
                    "scope_id",
                    "executor_id",
                )
            }
            now = datetime.now(timezone.utc)
            return CaptureResult(
                **lineage,
                state="succeeded",
                started_at=now,
                finished_at=now,
                final_url=response.final_url,
                status_code=response.status_code,
                content=CaptureContent(
                    media_type=response.content_type or "text/html",
                    text=response.body.decode("utf-8", errors="replace"),
                    sha256=response.sha256,
                ),
                metadata={"access_decision_id": response.access_decision.decision_id},
            )

    return GovernedAcquisitionGateway(compiled, Registry())


def _empty_database(path: Path) -> bytes:
    storage = Storage(path)
    storage.close()
    return path.read_bytes()


def _track_storage_construction(monkeypatch) -> list[Path]:
    opened: list[Path] = []
    original_init = Storage.__init__

    def tracked_init(self, db_path):
        opened.append(Path(db_path))
        original_init(self, db_path)

    monkeypatch.setattr(Storage, "__init__", tracked_init)
    return opened


@pytest.mark.parametrize(
    "robots_response",
    [
        (401, b"", {}),
        (403, b"", {}),
        (
            200,
            b"User-agent: *\nDisallow: /\n",
            {"content-type": "text/plain; charset=utf-8"},
        ),
        TimeoutError("offline robots timeout"),
    ],
)
def test_staged_initial_robots_rejection_precedes_every_write(
    tmp_path: Path,
    monkeypatch,
    robots_response: object,
) -> None:
    plan = _scope_plan()
    scope_path, profile_path = _write_authority(tmp_path, plan)
    db_path = tmp_path / "state.db"
    before = _empty_database(db_path)
    storage_opens = _track_storage_construction(monkeypatch)
    report_path = tmp_path / "report.md"
    downloads_path = tmp_path / "downloads"
    transport = _Transport({"https://example.com/robots.txt": robots_response})
    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(staged_workflow.settings, "downloads_dir", downloads_path)
    monkeypatch.setattr(
        staged_workflow,
        "_compile_acquisition_gateway",
        lambda *a, **k: _governed_gateway(transport),
    )

    with pytest.raises(AccessRejectedError):
        staged_workflow.bootstrap_scope(
            scope_path=scope_path,
            acquisition_profile_path=profile_path,
            report_path=report_path,
        )

    assert transport.requests == ["https://example.com/robots.txt"]
    assert storage_opens == []
    assert db_path.read_bytes() == before
    assert not report_path.exists()
    assert not downloads_path.exists()


def test_staged_reuses_admitted_seed_response_without_second_target_request(
    tmp_path: Path,
    monkeypatch,
) -> None:
    plan = _scope_plan(max_pages=1)
    scope_path, profile_path = _write_authority(tmp_path, plan)
    db_path = tmp_path / "state.db"
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://example.com/": (
                200,
                b"<html><body><main>admitted once</main></body></html>",
                {"content-type": "text/html"},
            ),
        }
    )
    gateway = _governed_gateway(transport)
    observed_db_state: list[bool] = []
    original_acquire = gateway.acquire

    def acquire_before_storage(*args, **kwargs):
        observed_db_state.append(db_path.exists())
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(gateway, "acquire", acquire_before_storage)
    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(
        staged_workflow, "_compile_acquisition_gateway", lambda *a, **k: gateway
    )

    staged_workflow.bootstrap_scope(
        scope_path=scope_path,
        acquisition_profile_path=profile_path,
        report_path=tmp_path / "report.md",
    )

    assert observed_db_state == [False]
    assert transport.requests.count("https://example.com/") == 1


def test_bootstrap_and_run_reuse_exact_ordered_tracking_query_seed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seed_url = "https://example.com/public?b=2&utm_source=review&a=1"
    sanitized_url = "https://example.com/public?a=1&b=2"
    plan = replace(
        _scope_plan(seed_url=seed_url, max_pages=1),
        max_depth=0,
        max_files=0,
    )
    plan = replace(
        plan,
        scope_fingerprint=compute_scope_fingerprint(
            seed_url=plan.seed_url,
            allowed_page_prefixes=plan.allowed_page_prefixes,
            allowed_file_prefixes=plan.allowed_file_prefixes,
            fetch_mode=plan.fetch_mode,
        ),
    )
    scope_path, profile_path = _write_authority(tmp_path, plan)
    db_path = tmp_path / "query-seed.db"
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            seed_url: (
                200,
                b"<html><main>exact admitted seed</main></html>",
                {"content-type": "text/html"},
            ),
        }
    )
    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(staged_workflow.settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        staged_workflow,
        "_compile_acquisition_gateway",
        lambda *args, **kwargs: _governed_gateway(transport),
    )

    bootstrap = staged_workflow.bootstrap_scope(
        scope_path=scope_path,
        acquisition_profile_path=profile_path,
        report_path=tmp_path / "bootstrap.md",
    )
    assert bootstrap.results[0].status == "completed"
    staged_workflow.run_scope(
        scope_path=scope_path,
        acquisition_profile_path=profile_path,
        report_path=tmp_path / "run.md",
    )

    assert transport.requests.count(seed_url) == 2
    assert sanitized_url not in transport.requests


def _late_rejection_plan(kind: str) -> MonitorScopePlan:
    seed_url = "https://example.com/public"
    return replace(
        _scope_plan(seed_url=seed_url, max_pages=2),
        max_depth=1,
        max_files=1 if kind in {"file", "page_after_file"} else 0,
    )


def _late_rejection_transport(kind: str) -> _Transport:
    if kind == "file":
        links = '<a href="/files/denied.pdf">denied file</a>'
    elif kind == "page_after_file":
        links = (
            '<a href="/files/allowed.pdf">allowed file</a>'
            '<a href="/private">denied page</a>'
        )
    else:
        links = '<a href="/private">denied page</a>'
    return _Transport(
        {
            "https://example.com/robots.txt": (
                200,
                b"User-agent: *\nDisallow: /private\nDisallow: /files/denied.pdf\n",
                {"content-type": "text/plain; charset=utf-8"},
            ),
            "https://example.com/public": (
                200,
                f"<html><main>allowed seed</main>{links}</html>".encode(),
                {"content-type": "text/html"},
            ),
            "https://example.com/files/allowed.pdf": (
                200,
                b"allowed governed document bytes",
                {"content-type": "application/pdf"},
            ),
        }
    )


def _tree_snapshot(
    root: Path, *, excluded: set[Path]
) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_dir())
    )
    files = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path not in excluded
    }
    return directories, files


@pytest.mark.parametrize("kind", ["page", "file", "page_after_file"])
@pytest.mark.parametrize("interface", ["cli", "api", "mcp"])
def test_late_robots_rejection_restores_execution_and_job_state(
    tmp_path: Path,
    monkeypatch,
    interface: str,
    kind: str,
) -> None:
    plan = _late_rejection_plan(kind)
    scope_path, profile_path = _write_authority(tmp_path, plan)
    db_path = tmp_path / f"{interface}-{kind}.db"
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    (downloads_path / "preexisting.bin").write_bytes(b"preexisting-download")
    report_path = tmp_path / f"{interface}-{kind}-report.md"
    report_path.write_bytes(b"preexisting-report")
    storage = Storage(db_path)
    storage.add_job(Job(job_type="preexisting", status="completed"))
    before_rows = {
        table: storage.conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in (
            "jobs",
            "sites",
            "crawl_scopes",
            "crawl_runs",
            "tracked_pages",
            "page_snapshots",
            "page_edges",
            "tracked_files",
            "file_observations",
            "documents",
            "document_blobs",
            "acquisition_attempts",
        )
    }
    storage.close()
    before_db = db_path.read_bytes()
    excluded = {db_path, db_path.with_name(f"{db_path.name}-journal")}
    before_tree = _tree_snapshot(tmp_path, excluded=excluded)
    transport = _late_rejection_transport(kind)

    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(staged_workflow.settings, "data_dir", tmp_path)
    monkeypatch.setattr(staged_workflow.settings, "downloads_dir", downloads_path)
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        staged_workflow,
        "_compile_acquisition_gateway",
        lambda *args, **kwargs: _governed_gateway(transport),
    )

    if interface == "cli":
        response = CliRunner().invoke(
            cli_app,
            [
                "bootstrap-scope",
                "--scope-path",
                str(scope_path),
                "--acquisition-profile-path",
                str(profile_path),
                "--report-path",
                str(report_path),
                "--download-files",
                "--json",
            ],
        )
        assert response.exit_code == 1
        payload = json.loads(response.stdout)
    elif interface == "api":
        response = TestClient(create_app()).post(
            "/api/v1/monitor-scopes/1/bootstrap",
            json={
                "scope_path": str(scope_path),
                "acquisition_profile_path": str(profile_path),
                "report_path": str(report_path),
                "download_files": True,
            },
        )
        assert response.status_code == 403
        payload = response.json()
    else:
        payload = web_listening_bootstrap_scope(
            str(scope_path),
            acquisition_profile_path=str(profile_path),
            report_path=str(report_path),
            download_files=True,
        )

    assert payload["schema_version"] == "access-rejection-error.v1"
    assert payload["reason_code"] == "robots.disallowed"
    expected_requests = [
        "https://example.com/robots.txt",
        "https://example.com/public",
    ]
    if kind == "page_after_file":
        expected_requests.append("https://example.com/files/allowed.pdf")
    assert transport.requests == expected_requests
    after_storage = Storage(db_path)
    try:
        after_rows = {
            table: after_storage.conn.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in before_rows
        }
    finally:
        after_storage.close()
    assert (
        after_rows == before_rows,
        db_path.read_bytes() == before_db,
        _tree_snapshot(tmp_path, excluded=excluded) == before_tree,
    ) == (True, True, True)


@pytest.mark.parametrize(
    ("failure_kind", "content_kind", "expected_failure"),
    [
        ("transport", "page", AccessGatewayTransportError),
        ("origin", "page", AccessGatewayOriginError),
        ("budget", "file", AccessGatewayBudgetError),
        ("body", "file", BodyFailure),
    ],
)
def test_late_governed_read_failure_rolls_back_all_execution_state(
    tmp_path: Path,
    monkeypatch,
    failure_kind: str,
    content_kind: str,
    expected_failure: type[BaseException],
) -> None:
    seed_url = "https://example.com/public"
    late_url = (
        "https://example.com/files/late.pdf"
        if content_kind == "file"
        else "https://example.com/late"
    )
    link = "/files/late.pdf" if content_kind == "file" else "/late"
    plan = replace(
        _scope_plan(seed_url=seed_url, max_pages=2),
        max_depth=1,
        max_files=1 if content_kind == "file" else 0,
    )
    scope_path, profile_path = _write_authority(tmp_path, plan)
    db_path = tmp_path / f"late-{failure_kind}.db"
    report_path = tmp_path / f"late-{failure_kind}.md"
    report_path.write_bytes(b"preexisting-report")
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    (downloads_path / "preexisting.bin").write_bytes(b"preexisting")
    storage = Storage(db_path)
    storage.add_job(Job(job_type="preexisting", status="completed"))
    storage.close()
    before_db = db_path.read_bytes()
    excluded = {db_path, db_path.with_name(f"{db_path.name}-journal")}
    before_tree = _tree_snapshot(tmp_path, excluded=excluded)
    late_response: object
    if failure_kind == "transport":
        late_response = TimeoutError("late transport timeout")
    elif failure_kind == "origin":
        late_response = (302, b"", {"location": "https://outside.example/late"})
    elif failure_kind == "body":
        late_response = (
            200,
            b"x" * 128,
            {"content-type": "application/pdf"},
        )
    else:
        late_response = (200, b"late", {"content-type": "application/pdf"})
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            seed_url: (
                200,
                f'<html><main>seed</main><a href="{link}">late</a></html>'.encode(),
                {"content-type": "text/html"},
            ),
            late_url: late_response,
        }
    )
    gateway = _governed_gateway(
        transport,
        budget_limit=1 if failure_kind == "budget" else 8,
        max_body_bytes=96 if failure_kind == "body" else 1024 * 1024,
    )
    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(staged_workflow.settings, "data_dir", tmp_path)
    monkeypatch.setattr(staged_workflow.settings, "downloads_dir", downloads_path)
    monkeypatch.setattr(
        staged_workflow, "_compile_acquisition_gateway", lambda *a, **k: gateway
    )

    with pytest.raises(expected_failure):
        staged_workflow.bootstrap_scope(
            scope_path=scope_path,
            acquisition_profile_path=profile_path,
            report_path=report_path,
            download_files=True,
        )

    assert db_path.read_bytes() == before_db
    assert _tree_snapshot(tmp_path, excluded=excluded) == before_tree
    expected_requests = ["https://example.com/robots.txt", seed_url]
    if failure_kind != "budget":
        expected_requests.append(late_url)
    assert transport.requests == expected_requests


@pytest.mark.parametrize(
    ("failure", "error_type", "error_code", "retryable"),
    [
        (
            AccessGatewayTransportError("timeout", "private"),
            "AccessGatewayTransportError",
            "gateway.transport.timeout",
            True,
        ),
        (
            AccessGatewayOriginError("private"),
            "AccessGatewayOriginError",
            "gateway.origin",
            False,
        ),
        (
            AccessGatewayRedirectError("private"),
            "AccessGatewayRedirectError",
            "gateway.redirect",
            False,
        ),
        (
            AccessGatewayBudgetError("private"),
            "AccessGatewayBudgetError",
            "gateway.budget",
            False,
        ),
        (
            BodyFailure(
                "wire_budget_exhausted",
                wire=8,
                decoded=8,
                retryable=False,
            ),
            "BodyFailure",
            "body.wire_budget_exhausted",
            False,
        ),
    ],
)
def test_governed_read_runtime_error_payload_is_typed_stable_and_redacted(
    failure: BaseException,
    error_type: str,
    error_code: str,
    retryable: bool,
) -> None:
    assert governed_read_failure_payload(failure) == {
        "schema_version": "governed-read-error.v1",
        "error_type": error_type,
        "error_code": error_code,
        "message": "governed target read failed",
        "retryable": retryable,
    }


def test_late_governed_transport_failure_has_interface_parity_and_zero_job_state(
    tmp_path: Path, monkeypatch
) -> None:
    expected_payload = {
        "schema_version": "governed-read-error.v1",
        "error_type": "AccessGatewayTransportError",
        "error_code": "gateway.transport.timeout",
        "message": "governed target read failed",
        "retryable": True,
    }
    payloads: list[dict[str, object]] = []
    for interface in ("cli", "api", "mcp"):
        root = tmp_path / interface
        root.mkdir()
        plan = replace(
            _scope_plan(seed_url="https://example.com/public", max_pages=2),
            max_depth=1,
        )
        scope_path, profile_path = _write_authority(root, plan)
        db_path = root / "state.db"
        report_path = root / "report.md"
        report_path.write_bytes(b"preexisting-report")
        storage = Storage(db_path)
        storage.add_job(Job(job_type="preexisting", status="completed"))
        storage.close()
        before_db = db_path.read_bytes()
        transport = _Transport(
            {
                "https://example.com/robots.txt": (404, b"", {}),
                "https://example.com/public": (
                    200,
                    b'<main>seed</main><a href="/late">late</a>',
                    {"content-type": "text/html"},
                ),
                "https://example.com/late": TimeoutError("private timeout detail"),
            }
        )
        monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
        monkeypatch.setattr(staged_workflow.settings, "data_dir", root)
        monkeypatch.setattr(routes.settings, "db_path", db_path)
        monkeypatch.setattr(routes.settings, "data_dir", root)
        monkeypatch.setattr(
            staged_workflow,
            "_compile_acquisition_gateway",
            lambda *a, _transport=transport, **k: _governed_gateway(_transport),
        )

        if interface == "cli":
            response = CliRunner().invoke(
                cli_app,
                [
                    "bootstrap-scope",
                    "--scope-path",
                    str(scope_path),
                    "--acquisition-profile-path",
                    str(profile_path),
                    "--report-path",
                    str(report_path),
                    "--json",
                ],
            )
            assert response.exit_code == 1
            payload = json.loads(response.stdout)
        elif interface == "api":
            response = TestClient(create_app()).post(
                "/api/v1/monitor-scopes/1/bootstrap",
                json={
                    "scope_path": str(scope_path),
                    "acquisition_profile_path": str(profile_path),
                    "report_path": str(report_path),
                },
            )
            assert response.status_code == 502
            payload = response.json()
        else:
            payload = web_listening_bootstrap_scope(
                str(scope_path),
                acquisition_profile_path=str(profile_path),
                report_path=str(report_path),
            )
        payloads.append(payload)
        assert db_path.read_bytes() == before_db
        assert report_path.read_bytes() == b"preexisting-report"
        assert transport.requests == [
            "https://example.com/robots.txt",
            "https://example.com/public",
            "https://example.com/late",
        ]

    assert payloads == [expected_payload, expected_payload, expected_payload]


def test_storage_execution_transaction_nesting_commit_and_journal_rollback(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "transaction.db"
    journal_root = tmp_path / "artifacts"
    journal_root.mkdir()
    preexisting = journal_root / "preexisting.bin"
    preexisting.write_bytes(b"keep")
    storage = Storage(db_path)
    baseline_db = db_path.read_bytes()

    storage.begin_execution_transaction()
    storage.begin_execution_transaction()
    storage.add_site(Site(url="https://rollback.example/", name="rollback"))
    created = journal_root / "nested" / "created.bin"
    storage.ensure_execution_artifact_directory(
        created.parent, cleanup_root=journal_root
    )
    created.write_bytes(b"remove")
    storage.register_execution_created_path(created, cleanup_root=journal_root)
    storage.commit_execution_transaction()
    with sqlite3.connect(db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 0
    storage.rollback_execution_transaction()

    assert db_path.read_bytes() == baseline_db
    assert preexisting.read_bytes() == b"keep"
    assert not created.exists()
    assert not created.parent.exists()

    storage.begin_execution_transaction()
    storage.begin_execution_transaction()
    storage.add_site(Site(url="https://commit.example/", name="commit"))
    committed = journal_root / "committed.bin"
    committed.write_bytes(b"persist")
    storage.register_execution_created_path(committed, cleanup_root=journal_root)
    storage.commit_execution_transaction()
    with sqlite3.connect(db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 0
    storage.commit_execution_transaction()
    storage.close()

    with sqlite3.connect(db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 1
    assert committed.read_bytes() == b"persist"


def test_execution_rollback_preserves_preexisting_empty_parent_and_ancestor(
    tmp_path: Path,
) -> None:
    cleanup_root = tmp_path / "artifacts"
    parent = cleanup_root / "preexisting-ancestor" / "preexisting-parent"
    parent.mkdir(parents=True)
    storage = Storage(tmp_path / "preexisting-directories.db")
    storage.begin_execution_transaction()
    created = parent / "created.bin"
    created.write_bytes(b"rollback")
    storage.register_execution_created_path(created, cleanup_root=cleanup_root)

    storage.rollback_execution_transaction()

    assert not created.exists()
    assert parent.is_dir()
    assert parent.parent.is_dir()
    storage.close()


def test_execution_rollback_removes_only_explicitly_journaled_new_directories(
    tmp_path: Path,
) -> None:
    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    storage = Storage(tmp_path / "created-directories.db")
    storage.begin_execution_transaction()
    parent = cleanup_root / "created-ancestor" / "created-parent"
    storage.ensure_execution_artifact_directory(parent, cleanup_root=cleanup_root)
    created = parent / "created.bin"
    created.write_bytes(b"rollback")
    storage.register_execution_created_path(created, cleanup_root=cleanup_root)

    storage.rollback_execution_transaction()

    assert not created.exists()
    assert not parent.exists()
    assert not parent.parent.exists()
    assert cleanup_root.is_dir()
    storage.close()


@pytest.mark.parametrize("secure_dir_fd", [False, True])
def test_execution_directory_rollback_quarantines_before_final_rmdir_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secure_dir_fd: bool,
) -> None:
    required = {os.open, os.stat, os.rename, os.rmdir}
    if secure_dir_fd and not required.issubset(os.supports_dir_fd):
        pytest.skip("secure directory-fd rollback is unavailable")

    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    storage = Storage(tmp_path / f"directory-rmdir-race-{secure_dir_fd}.db")
    storage.begin_execution_transaction()
    owned = cleanup_root / "owned"
    storage.ensure_execution_artifact_directory(owned, cleanup_root=cleanup_root)
    displaced_owned = cleanup_root / "displaced-owned"
    replacement_identity: list[tuple[int, int]] = []
    real_rename = os.rename
    real_rmdir = os.rmdir

    def resolved_path(target, *, dir_fd=None) -> Path | None:
        candidate = Path(os.fsdecode(target))
        if candidate.is_absolute():
            return candidate
        if dir_fd is None or not Path("/proc/self/fd").is_dir():
            return None
        return Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / candidate

    def replace_at_final_rmdir(target, *args, **kwargs):
        candidate = resolved_path(target, dir_fd=kwargs.get("dir_fd"))
        if not replacement_identity and candidate is not None:
            if candidate == owned:
                real_rename(owned, displaced_owned)
                owned.mkdir()
            elif (
                candidate.name.startswith(".web-listening-rollback-")
                and candidate.parent == owned.parent
            ):
                owned.mkdir()
            else:
                return real_rmdir(target, *args, **kwargs)
            replacement = owned.stat(follow_symlinks=False)
            replacement_identity.append((replacement.st_dev, replacement.st_ino))
        return real_rmdir(target, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", replace_at_final_rmdir)
    if secure_dir_fd:
        supported_dir_fd = set(os.supports_dir_fd)
        supported_dir_fd.discard(real_rmdir)
        supported_dir_fd.add(replace_at_final_rmdir)
    else:
        supported_dir_fd = set()
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)

    storage.rollback_execution_transaction()

    assert replacement_identity
    replacement = owned.stat(follow_symlinks=False)
    assert (replacement.st_dev, replacement.st_ino) == replacement_identity[0]
    storage.close()


@pytest.mark.parametrize("secure_dir_fd", [False, True])
def test_execution_directory_rollback_restore_collision_preserves_both_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secure_dir_fd: bool,
) -> None:
    required = {os.open, os.stat, os.rename, os.rmdir}
    if secure_dir_fd and not required.issubset(os.supports_dir_fd):
        pytest.skip("secure directory-fd rollback is unavailable")

    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    storage = Storage(tmp_path / f"directory-restore-race-{secure_dir_fd}.db")
    storage.begin_execution_transaction()
    owned = cleanup_root / "owned"
    storage.ensure_execution_artifact_directory(owned, cleanup_root=cleanup_root)
    displaced_owned = cleanup_root / "displaced-owned"
    owned.rename(displaced_owned)
    owned.mkdir()
    replacement_b = owned.stat(follow_symlinks=False)
    replacement_c: list[tuple[int, int]] = []
    real_no_replace = getattr(storage, "_rename_directory_no_replace", None)

    def collide_before_restore(source, destination, *args, **kwargs):
        destination_name = os.fsdecode(destination)
        if destination_name in {owned.name, os.fspath(owned)} and not replacement_c:
            owned.mkdir()
            collided = owned.stat(follow_symlinks=False)
            replacement_c.append((collided.st_dev, collided.st_ino))
        if real_no_replace is None:
            return os.rename(source, destination, *args, **kwargs)
        return real_no_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(
        storage,
        "_rename_directory_no_replace",
        collide_before_restore,
        raising=False,
    )
    monkeypatch.setattr(
        os,
        "supports_dir_fd",
        set(os.supports_dir_fd) if secure_dir_fd else set(),
    )

    with pytest.raises(RuntimeError, match="quarantine|restore"):
        storage.rollback_execution_transaction()

    assert replacement_c
    current = owned.stat(follow_symlinks=False)
    assert (current.st_dev, current.st_ino) == replacement_c[0]
    quarantined = list(cleanup_root.glob(".web-listening-rollback-*"))
    assert len(quarantined) == 1
    retained = quarantined[0].stat(follow_symlinks=False)
    assert (retained.st_dev, retained.st_ino) == (
        replacement_b.st_dev,
        replacement_b.st_ino,
    )
    assert displaced_owned.is_dir()
    storage.close()


def test_execution_directory_rollback_never_reuses_preexisting_quarantine_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    quarantine = cleanup_root / ".web-listening-rollback-fixed"
    quarantine.mkdir()
    marker = quarantine / "preexisting.bin"
    marker.write_bytes(b"keep")
    storage = Storage(tmp_path / "directory-quarantine-name.db")
    storage.begin_execution_transaction()
    owned = cleanup_root / "owned"
    storage.ensure_execution_artifact_directory(owned, cleanup_root=cleanup_root)
    monkeypatch.setattr(storage, "_quarantine_name", lambda: quarantine.name)

    with pytest.raises((FileExistsError, RuntimeError)):
        storage.rollback_execution_transaction()

    assert marker.read_bytes() == b"keep"
    assert owned.is_dir()
    storage.close()


@pytest.mark.parametrize("secure_dir_fd", [False, True])
def test_execution_directory_quarantine_effect_then_baseexception_restores_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secure_dir_fd: bool,
) -> None:
    required = {os.open, os.stat, os.rename, os.rmdir}
    if secure_dir_fd and not required.issubset(os.supports_dir_fd):
        pytest.skip("secure directory-fd rollback is unavailable")

    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    storage = Storage(tmp_path / f"directory-effect-{secure_dir_fd}.db")
    storage.begin_execution_transaction()
    owned = cleanup_root / "owned"
    storage.ensure_execution_artifact_directory(owned, cleanup_root=cleanup_root)
    real_no_replace = storage._rename_directory_no_replace
    primary = KeyboardInterrupt("directory quarantine effect")
    interrupted = False

    def interrupt_after_move(source, destination, *args, **kwargs):
        nonlocal interrupted
        real_no_replace(source, destination, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise primary

    monkeypatch.setattr(
        storage,
        "_rename_directory_no_replace",
        interrupt_after_move,
    )
    monkeypatch.setattr(
        os,
        "supports_dir_fd",
        set(os.supports_dir_fd) if secure_dir_fd else set(),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        storage.rollback_execution_transaction()

    assert caught.value is primary
    assert owned.is_dir()
    assert not list(cleanup_root.glob(".web-listening-rollback-*"))
    storage.close()


@pytest.mark.parametrize("lifecycle", ["commit", "rollback", "close"])
def test_execution_directory_journal_nested_lifecycle_closes_descriptors_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: str,
) -> None:
    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    storage = Storage(tmp_path / f"directory-{lifecycle}.db")
    storage.begin_execution_transaction()
    storage.begin_execution_transaction()
    directory = cleanup_root / "created-ancestor" / "created-parent"
    storage.ensure_execution_artifact_directory(directory, cleanup_root=cleanup_root)
    descriptors = {
        ownership.descriptor for ownership in storage._execution_created_directories
    }
    close_counts = {descriptor: 0 for descriptor in descriptors}
    real_close = os.close

    def tracked_close(descriptor: int) -> None:
        if descriptor in close_counts:
            close_counts[descriptor] += 1
        real_close(descriptor)

    monkeypatch.setattr(os, "close", tracked_close)
    storage.commit_execution_transaction()
    assert all(close_counts[descriptor] == 0 for descriptor in descriptors)

    if lifecycle == "commit":
        storage.commit_execution_transaction()
        assert directory.is_dir()
    elif lifecycle == "rollback":
        storage.rollback_execution_transaction()
        assert not directory.exists()
    else:
        storage.close()
        assert not directory.exists()

    assert all(close_counts[descriptor] == 1 for descriptor in descriptors)
    if lifecycle != "close":
        storage.close()
    assert all(close_counts[descriptor] == 1 for descriptor in descriptors)


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_execution_directory_rollback_preserves_replacement_and_victim(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    cleanup_root = tmp_path / "artifacts"
    cleanup_root.mkdir()
    storage = Storage(tmp_path / f"directory-replacement-{replacement_kind}.db")
    storage.begin_execution_transaction()
    directory = cleanup_root / "created-parent"
    storage.ensure_execution_artifact_directory(directory, cleanup_root=cleanup_root)
    displaced = cleanup_root / "displaced-owned-directory"
    directory.rename(displaced)
    victim = cleanup_root / "victim"
    victim.mkdir()
    (victim / "keep.bin").write_bytes(b"keep")
    if replacement_kind == "directory":
        directory.mkdir()
        (directory / "replacement.bin").write_bytes(b"replacement")
    else:
        try:
            directory.symlink_to(victim, target_is_directory=True)
        except OSError:
            storage.rollback_execution_transaction()
            storage.close()
            pytest.skip("directory symlinks are unavailable")

    storage.rollback_execution_transaction()

    assert displaced.is_dir()
    assert (victim / "keep.bin").read_bytes() == b"keep"
    if replacement_kind == "directory":
        assert (directory / "replacement.bin").read_bytes() == b"replacement"
    else:
        assert directory.is_symlink()
    storage.close()


def test_execution_journal_rejects_replaced_inode_before_ownership_transfer(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "journal-replacement.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    publisher_source = cleanup_root / "publisher-source.bin"
    publisher_source.write_bytes(b"publisher-owned")
    target = cleanup_root / "target.bin"
    os.link(publisher_source, target)
    owned = target.stat(follow_symlinks=False)
    target.unlink()
    target.write_bytes(b"replacement")
    replacement = target.stat(follow_symlinks=False)
    assert (replacement.st_dev, replacement.st_ino) != (owned.st_dev, owned.st_ino)
    baseline_db = storage.db_path.read_bytes()
    storage.begin_execution_transaction()
    storage.add_site(Site(url="https://mismatch.example/", name="mismatch"))

    with pytest.raises(ValueError, match="identity"):
        storage.register_execution_created_path(
            target,
            cleanup_root=cleanup_root,
            expected_identity=(owned.st_dev, owned.st_ino),
        )

    storage.rollback_execution_transaction()
    assert storage.db_path.read_bytes() == baseline_db
    after = target.stat(follow_symlinks=False)
    assert target.read_bytes() == b"replacement"
    assert (after.st_dev, after.st_ino) == (replacement.st_dev, replacement.st_ino)
    storage.close()


@pytest.mark.parametrize("replacement_kind", ["file", "symlink"])
def test_execution_journal_rollback_preserves_post_handoff_replacement_and_victim(
    tmp_path: Path, replacement_kind: str
) -> None:
    storage = Storage(tmp_path / f"journal-rollback-{replacement_kind}.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"publisher-owned")
    owned = target.stat(follow_symlinks=False)
    victim = cleanup_root / "victim.bin"
    victim.write_bytes(b"victim")
    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=(owned.st_dev, owned.st_ino),
    )
    target.unlink()
    if replacement_kind == "symlink":
        try:
            target.symlink_to(victim.name)
        except OSError as exc:
            storage.rollback_execution_transaction()
            storage.close()
            pytest.skip(f"symlink creation unavailable: {exc}")
        replacement = target.lstat()
    else:
        target.write_bytes(b"replacement")
        replacement = target.stat(follow_symlinks=False)

    storage.rollback_execution_transaction()

    current = target.lstat()
    assert (current.st_dev, current.st_ino) == (
        replacement.st_dev,
        replacement.st_ino,
    )
    if replacement_kind == "symlink":
        assert target.is_symlink()
    else:
        assert target.read_bytes() == b"replacement"
    assert victim.read_bytes() == b"victim"
    storage.close()


def test_execution_rollback_quarantines_name_before_replacement_identity_check(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "journal-quarantine-race.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    owned = target.stat(follow_symlinks=False)
    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=(owned.st_dev, owned.st_ino),
    )
    real_rename = os.rename
    swapped = False

    def replace_before_quarantine(source, destination, *args, **kwargs):
        nonlocal swapped
        source_name = os.fspath(source)
        if not swapped and (
            source_name == os.fspath(target) or source_name == target.name
        ):
            swapped = True
            target.unlink()
            target.write_bytes(b"replacement")
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", replace_before_quarantine)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_rename)
    supported_dir_fd.add(replace_before_quarantine)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)
    storage.rollback_execution_transaction()

    assert swapped
    assert target.read_bytes() == b"replacement"
    assert not list(cleanup_root.glob(".web-listening-rollback-*"))
    storage.close()


def test_execution_rollback_restore_collision_preserves_both_names_and_surfaces(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "journal-quarantine-collision.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    owned = target.stat(follow_symlinks=False)
    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=(owned.st_dev, owned.st_ino),
    )
    target.unlink()
    target.write_bytes(b"replacement-b")
    real_link = os.link
    real_rename = os.rename
    collided = False

    def create_collision() -> None:
        nonlocal collided
        if not collided:
            collided = True
            target.write_bytes(b"replacement-c")

    def collide_before_restore(source, destination, *args, **kwargs):
        nonlocal collided
        source_dir_fd = kwargs.get("src_dir_fd")
        source_location = os.fspath(source)
        if source_dir_fd is not None and Path("/proc/self/fd").is_dir():
            source_location = os.readlink(f"/proc/self/fd/{source_dir_fd}")
        if ".web-listening-rollback-" in source_location:
            create_collision()
        return real_link(source, destination, *args, **kwargs)

    def collide_before_windows_restore(source, destination, *args, **kwargs):
        if ".web-listening-rollback-" in os.fspath(source):
            create_collision()
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", collide_before_restore)
    monkeypatch.setattr(os, "rename", collide_before_windows_restore)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_link)
    supported_dir_fd.discard(real_rename)
    supported_dir_fd.update({collide_before_restore, collide_before_windows_restore})
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)
    with pytest.raises(RuntimeError, match="quarantine|restore"):
        storage.rollback_execution_transaction()

    assert collided
    assert target.read_bytes() == b"replacement-c"
    quarantines = list(cleanup_root.glob(".web-listening-rollback-*"))
    assert len(quarantines) == 1
    quarantined = list(quarantines[0].iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"replacement-b"
    storage.close()


def test_execution_rollback_serializes_shared_directory_name_transitions(
    tmp_path: Path, monkeypatch
) -> None:
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    owned = target.stat(follow_symlinks=False)
    storages = [Storage(tmp_path / f"journal-{index}.db") for index in range(2)]
    for storage in storages:
        storage.begin_execution_transaction()
        storage.register_execution_created_path(
            target,
            cleanup_root=cleanup_root,
            expected_identity=(owned.st_dev, owned.st_ino),
        )
    real_rename = os.rename
    first_entered = threading.Event()
    release_first = threading.Event()
    rename_calls: list[int] = []

    def blocking_rename(source, destination, *args, **kwargs):
        rename_calls.append(threading.get_ident())
        if len(rename_calls) == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "rename", blocking_rename)
    threads = [
        threading.Thread(target=storage.rollback_execution_transaction)
        for storage in storages
    ]
    threads[0].start()
    assert first_entered.wait(timeout=5)
    threads[1].start()
    threads[1].join(timeout=0.05)
    assert threads[1].is_alive()
    assert len(rename_calls) == 1
    release_first.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert len(rename_calls) == 1
    assert not target.exists()
    assert not list(cleanup_root.glob(".web-listening-rollback-*"))
    for storage in storages:
        storage.close()


def test_execution_journal_rejects_symlink_leaf_without_following_victim(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "journal-symlink.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    victim = cleanup_root / "victim.bin"
    victim.write_bytes(b"victim")
    victim_info = victim.stat(follow_symlinks=False)
    target = cleanup_root / "target.bin"
    try:
        target.symlink_to(victim.name)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    storage.begin_execution_transaction()

    with pytest.raises(ValueError, match="identity|regular"):
        storage.register_execution_created_path(
            target,
            cleanup_root=cleanup_root,
            expected_identity=(victim_info.st_dev, victim_info.st_ino),
        )

    storage.rollback_execution_transaction()
    assert target.is_symlink()
    assert victim.read_bytes() == b"victim"
    storage.close()


def test_execution_journal_rejects_lexical_parent_traversal_with_expected_identity(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "journal-traversal.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    outside_info = outside.stat(follow_symlinks=False)
    storage.begin_execution_transaction()

    with pytest.raises(ValueError, match="cleanup root|traversal"):
        storage.register_execution_created_path(
            cleanup_root / ".." / outside.name,
            cleanup_root=cleanup_root,
            expected_identity=(outside_info.st_dev, outside_info.st_ino),
        )

    assert outside.read_bytes() == b"outside"
    storage.rollback_execution_transaction()
    storage.close()


@pytest.mark.parametrize("lifecycle", ["commit", "rollback", "close"])
def test_execution_journal_holds_leaf_descriptor_until_outer_lifecycle(
    tmp_path: Path, monkeypatch, lifecycle: str
) -> None:
    storage = Storage(tmp_path / f"journal-descriptor-{lifecycle}.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    expected = target.stat(follow_symlinks=False)
    real_open = Storage._open_anchored_execution_path
    real_close = os.close
    regular_descriptors: list[int] = []
    close_counts: dict[int, int] = {}

    def tracking_open(*args, **kwargs):
        descriptor, opened = real_open(*args, **kwargs)
        regular_descriptors.append(descriptor)
        return descriptor, opened

    def tracking_close(descriptor):
        if descriptor in regular_descriptors:
            close_counts[descriptor] = close_counts.get(descriptor, 0) + 1
        return real_close(descriptor)

    monkeypatch.setattr(
        Storage, "_open_anchored_execution_path", staticmethod(tracking_open)
    )
    monkeypatch.setattr(os, "close", tracking_close)
    storage.begin_execution_transaction()
    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=(expected.st_dev, expected.st_ino),
    )

    assert len(regular_descriptors) == 1
    held_descriptor = regular_descriptors[0]
    assert close_counts.get(held_descriptor, 0) == 0
    assert os.fstat(held_descriptor).st_ino == expected.st_ino
    storage.commit_execution_transaction()
    assert close_counts.get(held_descriptor, 0) == 0

    if lifecycle == "commit":
        storage.commit_execution_transaction()
        assert target.read_bytes() == b"owned"
    elif lifecycle == "rollback":
        storage.rollback_execution_transaction()
        assert not target.exists()
    else:
        storage.close()
        assert not target.exists()
    assert close_counts.get(held_descriptor, 0) == 1
    with pytest.raises(OSError):
        os.fstat(held_descriptor)
    if lifecycle != "close":
        storage.close()


@pytest.mark.parametrize("commit_effected", [False, True])
def test_execution_journal_closes_leaf_descriptor_after_commit_base_exception(
    tmp_path: Path, monkeypatch, commit_effected: bool
) -> None:
    storage = Storage(tmp_path / f"journal-commit-fd-{commit_effected}.db")
    real_connection = storage.conn
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    expected = target.stat(follow_symlinks=False)
    real_open = Storage._open_anchored_execution_path
    real_close = os.close
    regular_descriptors: list[int] = []
    close_counts: dict[int, int] = {}
    failure = KeyboardInterrupt(f"commit fd effect {commit_effected}")
    close_failure = SystemExit(f"close fd effect {commit_effected}")

    def tracking_open(*args, **kwargs):
        descriptor, opened = real_open(*args, **kwargs)
        regular_descriptors.append(descriptor)
        return descriptor, opened

    def tracking_close(descriptor):
        if descriptor in regular_descriptors:
            close_counts[descriptor] = close_counts.get(descriptor, 0) + 1
            real_close(descriptor)
            raise close_failure
        return real_close(descriptor)

    class CommitFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            if commit_effected:
                real_connection.commit()
            raise failure

    monkeypatch.setattr(
        Storage, "_open_anchored_execution_path", staticmethod(tracking_open)
    )
    monkeypatch.setattr(os, "close", tracking_close)
    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=(expected.st_dev, expected.st_ino),
    )
    assert len(regular_descriptors) == 1
    held_descriptor = regular_descriptors[0]
    storage.conn = CommitFaultConnection()

    with pytest.raises(KeyboardInterrupt) as caught:
        storage.commit_execution_transaction()

    assert caught.value is failure
    assert close_counts.get(held_descriptor, 0) == 1
    with pytest.raises(OSError):
        os.fstat(held_descriptor)
    assert target.exists() is commit_effected
    storage.conn = real_connection
    storage.close()


def test_execution_journal_registration_failure_closes_untransferred_descriptor(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "journal-register-fd-failure.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"replacement")
    actual = target.stat(follow_symlinks=False)
    real_open = Storage._open_anchored_execution_path
    real_close = os.close
    regular_descriptors: list[int] = []
    close_counts: dict[int, int] = {}
    close_failure = SystemExit("registration cleanup failure")

    def tracking_open(*args, **kwargs):
        descriptor, opened = real_open(*args, **kwargs)
        regular_descriptors.append(descriptor)
        return descriptor, opened

    def tracking_close(descriptor):
        if descriptor in regular_descriptors:
            close_counts[descriptor] = close_counts.get(descriptor, 0) + 1
            real_close(descriptor)
            raise close_failure
        return real_close(descriptor)

    monkeypatch.setattr(
        Storage, "_open_anchored_execution_path", staticmethod(tracking_open)
    )
    monkeypatch.setattr(os, "close", tracking_close)
    storage.begin_execution_transaction()
    with pytest.raises(ValueError, match="identity"):
        storage.register_execution_created_path(
            target,
            cleanup_root=cleanup_root,
            expected_identity=(actual.st_dev, actual.st_ino + 1),
        )

    assert len(regular_descriptors) == 1
    held_descriptor = regular_descriptors[0]
    assert close_counts.get(held_descriptor, 0) == 1
    with pytest.raises(OSError):
        os.fstat(held_descriptor)
    storage.rollback_execution_transaction()
    assert target.read_bytes() == b"replacement"
    storage.close()


def test_storage_close_cleanup_failure_does_not_mask_rollback_primary(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "storage-close-primary.db")
    real_connection = storage.conn
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    expected = target.stat(follow_symlinks=False)
    primary = KeyboardInterrupt("rollback primary")
    secondary = SystemExit("connection close cleanup")

    class CloseFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        def close(self):
            real_connection.close()
            raise secondary

    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=(expected.st_dev, expected.st_ino),
    )
    real_rollback = storage.rollback_execution_transaction

    def rollback_then_fail():
        real_rollback()
        raise primary

    monkeypatch.setattr(storage, "rollback_execution_transaction", rollback_then_fail)
    storage.conn = CloseFaultConnection()
    with pytest.raises(KeyboardInterrupt) as caught:
        storage.close()

    assert caught.value is primary
    assert not target.exists()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="native inode-reuse regression requires Linux descriptor semantics",
)
def test_execution_journal_live_descriptor_prevents_native_aba_reuse(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "journal-native-aba.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    owned = target.stat(follow_symlinks=False)
    owned_identity = (owned.st_dev, owned.st_ino)
    fd_directory = Path("/proc/self/fd")
    before_fds = len(list(fd_directory.iterdir()))
    storage.begin_execution_transaction()
    storage.register_execution_created_path(
        target,
        cleanup_root=cleanup_root,
        expected_identity=owned_identity,
    )
    assert len(list(fd_directory.iterdir())) == before_fds + 1
    target.unlink()

    reused = False
    for index in range(4096):
        target.write_bytes(f"replacement-{index}".encode())
        current = target.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == owned_identity:
            reused = True
            break
        target.unlink()
    if not target.exists():
        target.write_bytes(b"replacement-final")
    replacement = target.read_bytes()

    storage.rollback_execution_transaction()

    assert not reused
    assert target.read_bytes() == replacement
    assert len(list(fd_directory.iterdir())) == before_fds
    storage.close()


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="native replacement hook requires Linux dir_fd stat semantics",
)
def test_execution_journal_rejects_native_replace_between_open_and_registration(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "journal-native-register-race.db")
    cleanup_root = tmp_path / "journal-root"
    cleanup_root.mkdir()
    target = cleanup_root / "target.bin"
    target.write_bytes(b"owned")
    owned = target.stat(follow_symlinks=False)
    real_stat = os.stat
    replaced = False
    fd_directory = Path("/proc/self/fd")
    before_fds = len(list(fd_directory.iterdir()))

    def replace_then_stat(path, *args, **kwargs):
        nonlocal replaced
        if (
            not replaced
            and path == target.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            replaced = True
            target.unlink()
            target.write_bytes(b"replacement")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", replace_then_stat)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_stat)
    supported_dir_fd.add(replace_then_stat)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)
    storage.begin_execution_transaction()
    with pytest.raises(ValueError, match="identity|changed"):
        storage.register_execution_created_path(
            target,
            cleanup_root=cleanup_root,
            expected_identity=(owned.st_dev, owned.st_ino),
        )

    storage.rollback_execution_transaction()
    assert replaced
    assert target.read_bytes() == b"replacement"
    assert len(list(fd_directory.iterdir())) == before_fds
    storage.close()


@pytest.mark.parametrize("commit_effected", [False, True])
def test_execution_commit_base_exception_reconciles_database_and_file_ownership(
    tmp_path: Path,
    commit_effected: bool,
) -> None:
    storage = Storage(tmp_path / f"commit-effect-{commit_effected}.db")
    real_connection = storage.conn
    baseline_db = storage.db_path.read_bytes()
    artifact_root = tmp_path / f"commit-effect-{commit_effected}-artifacts"
    artifact_root.mkdir()
    created_parent = artifact_root / "created-parent"
    created = created_parent / "created.bin"
    failure = KeyboardInterrupt(f"commit-effect-{commit_effected}")

    class CommitFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            if commit_effected:
                real_connection.commit()
            raise failure

    storage.begin_execution_transaction()
    storage.add_site(Site(url="https://commit.example/", name="commit"))
    storage.ensure_execution_artifact_directory(
        created_parent, cleanup_root=artifact_root
    )
    created.write_bytes(b"owned")
    storage.register_execution_created_path(created, cleanup_root=artifact_root)
    storage.conn = CommitFaultConnection()

    with pytest.raises(KeyboardInterrupt) as caught:
        storage.commit_execution_transaction()

    assert caught.value is failure
    assert not storage.execution_transaction_active
    assert storage._execution_created_paths == []
    assert storage._execution_created_directories == []
    with sqlite3.connect(storage.db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == int(
            commit_effected
        )
    assert created.exists() is commit_effected
    assert created_parent.exists() is commit_effected
    if not commit_effected:
        assert storage.db_path.read_bytes() == baseline_db
    storage.conn = real_connection
    storage.close()


def test_execution_commit_cleanup_failure_does_not_mask_primary_exception(
    tmp_path: Path, monkeypatch
) -> None:
    storage = Storage(tmp_path / "commit-cleanup-failure.db")
    real_connection = storage.conn
    artifact_root = tmp_path / "commit-cleanup-artifacts"
    artifact_root.mkdir()
    created = artifact_root / "created.bin"
    primary = KeyboardInterrupt("primary commit failure")
    secondary = SystemExit(47)

    class CommitFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            raise primary

    storage.begin_execution_transaction()
    storage.add_site(Site(url="https://cleanup.example/", name="cleanup"))
    created.write_bytes(b"owned")
    storage.register_execution_created_path(created, cleanup_root=artifact_root)
    real_rollback = storage.rollback_execution_transaction

    def rollback_then_fail():
        real_rollback()
        raise secondary

    monkeypatch.setattr(storage, "rollback_execution_transaction", rollback_then_fail)
    storage.conn = CommitFaultConnection()
    with pytest.raises(KeyboardInterrupt) as caught:
        storage.commit_execution_transaction()

    assert caught.value is primary
    assert not storage.execution_transaction_active
    assert storage._execution_created_paths == []
    assert not created.exists()
    storage.conn = real_connection
    monkeypatch.setattr(storage, "rollback_execution_transaction", real_rollback)
    storage.close()


@pytest.mark.parametrize("commit_effected", [False, True])
def test_tree_outer_commit_base_exception_reconciles_durable_execution(
    tmp_path: Path, monkeypatch, commit_effected: bool
) -> None:
    seed_url = "https://example.com/public"
    gateway = _governed_gateway(
        _Transport(
            {
                "https://example.com/robots.txt": (404, b"", {}),
                seed_url: (200, b"<main>seed</main>", {"content-type": "text/html"}),
            }
        )
    )
    admitted = gateway.acquire(
        seed_url, run_id="admission", scope_id="commit", content_kind="page"
    )
    storage = Storage(tmp_path / f"tree-commit-{commit_effected}.db")
    real_connection = storage.conn
    artifact_root = tmp_path / f"tree-commit-{commit_effected}-artifacts"
    artifact_root.mkdir()
    created = artifact_root / "created.bin"
    failure = KeyboardInterrupt(f"tree commit effect {commit_effected}")
    scope = CrawlScope(
        site_id=1,
        seed_url=seed_url,
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
    )
    tree = TreeCrawler(
        storage=storage,
        acquisition_gateway=gateway,
        initial_outcome=admitted,
        execution_seed_url=seed_url,
    )

    def execution(*args, **kwargs):
        del args, kwargs
        storage.add_site(Site(url=seed_url, name="commit"))
        created.write_bytes(b"owned")
        storage.register_execution_created_path(created, cleanup_root=artifact_root)
        return object()

    class CommitFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            if commit_effected:
                real_connection.commit()
            raise failure

    monkeypatch.setattr(tree, "_bootstrap_scope_in_transaction", execution)
    storage.conn = CommitFaultConnection()
    with pytest.raises(KeyboardInterrupt) as caught:
        tree.bootstrap_scope(scope, download_files=False)

    assert caught.value is failure
    assert not storage.execution_transaction_active
    assert storage._execution_created_paths == []
    with sqlite3.connect(storage.db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == int(
            commit_effected
        )
    assert created.exists() is commit_effected
    storage.conn = real_connection
    tree.close()
    storage.close()


def test_tree_artifact_identity_mismatch_refuses_commit_and_preserves_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    seed_url = "https://example.com/public"
    gateway = _governed_gateway(
        _Transport(
            {
                "https://example.com/robots.txt": (404, b"", {}),
                seed_url: (200, b"<main>seed</main>", {"content-type": "text/html"}),
            }
        )
    )
    admitted = gateway.acquire(
        seed_url, run_id="admission", scope_id="mismatch", content_kind="page"
    )
    storage = Storage(tmp_path / "tree-ownership-mismatch.db")
    baseline_db = storage.db_path.read_bytes()
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement")
    before = replacement.stat(follow_symlinks=False)
    failure = ExecutionArtifactOwnershipError("identity mismatch")
    scope = CrawlScope(
        site_id=1,
        seed_url=seed_url,
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
    )
    tree = TreeCrawler(
        storage=storage,
        acquisition_gateway=gateway,
        initial_outcome=admitted,
        execution_seed_url=seed_url,
    )

    def execution(*args, **kwargs):
        del args, kwargs
        storage.add_site(Site(url=seed_url, name="must rollback"))
        raise failure

    monkeypatch.setattr(tree, "_bootstrap_scope_in_transaction", execution)
    with pytest.raises(ExecutionArtifactOwnershipError) as caught:
        tree.bootstrap_scope(scope, download_files=False)

    after = replacement.stat(follow_symlinks=False)
    assert caught.value is failure
    assert storage.db_path.read_bytes() == baseline_db
    assert replacement.read_bytes() == b"replacement"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert not storage.execution_transaction_active
    tree.close()
    storage.close()


def test_tree_cancellation_rolls_back_execution_rows_and_created_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seed_url = "https://example.com/public"
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            seed_url: (200, b"<main>seed</main>", {"content-type": "text/html"}),
        }
    )
    gateway = _governed_gateway(transport)
    admitted = gateway.acquire(
        seed_url, run_id="admission", scope_id="cancel", content_kind="page"
    )
    db_path = tmp_path / "cancel.db"
    storage = Storage(db_path)
    baseline_db = db_path.read_bytes()
    artifact_root = tmp_path / "cancel-artifacts"
    artifact_root.mkdir()
    created = artifact_root / "created.bin"
    scope = CrawlScope(
        site_id=1,
        seed_url=seed_url,
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
    )
    tree = TreeCrawler(
        storage=storage,
        acquisition_gateway=gateway,
        initial_outcome=admitted,
        execution_seed_url=seed_url,
    )

    def interrupt(*args, **kwargs):
        del args, kwargs
        storage.add_site(Site(url=seed_url, name="must rollback"))
        created.write_bytes(b"must rollback")
        storage.register_execution_created_path(created, cleanup_root=artifact_root)
        raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(tree, "_bootstrap_scope_in_transaction", interrupt)
    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        tree.bootstrap_scope(scope, download_files=False)

    assert db_path.read_bytes() == baseline_db
    assert not created.exists()
    tree.close()
    storage.close()


def test_api_uses_one_sealed_authority_after_input_files_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_plan = _scope_plan(max_pages=2)
    original_plan = replace(
        original_plan,
        scope_fingerprint=compute_scope_fingerprint(
            seed_url=original_plan.seed_url,
            allowed_page_prefixes=original_plan.allowed_page_prefixes,
            allowed_file_prefixes=original_plan.allowed_file_prefixes,
            fetch_mode=original_plan.fetch_mode,
        ),
    )
    changed_plan = replace(
        original_plan,
        seed_url="https://changed.example/",
        homepage_url="https://changed.example/",
    )
    scope_path, profile_path = _write_authority(tmp_path, original_plan)
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://example.com/": (
                200,
                b"<main>sealed</main>",
                {"content-type": "text/html"},
            ),
            "https://changed.example/robots.txt": (404, b"", {}),
            "https://changed.example/": (
                200,
                b"<main>changed</main>",
                {"content-type": "text/html"},
            ),
        }
    )
    compiled_gateways: list[GovernedAcquisitionGateway] = []
    compiled_limits: list[int] = []
    load_count = 0
    original_load = staged_workflow.load_monitor_scope_plan

    def load_once(path):
        nonlocal load_count
        load_count += 1
        return original_load(path)

    def compile_once(plan, **kwargs):
        del kwargs
        compiled_limits.append(plan.max_pages)
        gateway = _governed_gateway(transport)
        compiled_gateways.append(gateway)
        if len(compiled_gateways) == 1:
            scope_path.write_text(render_yaml_text(changed_plan), encoding="utf-8")
            profile_path.write_text("profile_id: changed-profile\n", encoding="utf-8")
        return gateway

    handed_gateways: list[object] = []
    original_run_bootstrap = tree_bootstrap_workflow.run_bootstrap

    def record_handoff(**kwargs):
        handed_gateways.append(kwargs["acquisition_gateway"])
        return original_run_bootstrap(**kwargs)

    monkeypatch.setattr(staged_workflow.settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(staged_workflow.settings, "data_dir", tmp_path)
    monkeypatch.setattr(routes.settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)
    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", load_once)
    monkeypatch.setattr(staged_workflow, "_compile_acquisition_gateway", compile_once)
    monkeypatch.setattr(staged_workflow, "run_bootstrap", record_handoff)

    response = TestClient(create_app()).post(
        "/api/v1/monitor-scopes/1/bootstrap",
        json={
            "scope_path": str(scope_path),
            "acquisition_profile_path": str(profile_path),
            "max_pages": 1,
            "report_path": str(tmp_path / "api-report.md"),
            "include_summary": True,
            "summary_path": str(tmp_path / "api-summary.md"),
        },
    )

    assert response.status_code == 201, response.text
    assert load_count == 1
    assert len(compiled_gateways) == 1
    assert compiled_limits == [1]
    assert handed_gateways == compiled_gateways
    assert transport.requests.count("https://example.com/") == 1
    assert "https://changed.example/" not in transport.requests


def test_api_robots_rejection_creates_no_job_or_other_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scope_path, profile_path = _write_authority(tmp_path, _scope_plan())
    db_path = tmp_path / "api.db"
    before = _empty_database(db_path)
    storage_opens = _track_storage_construction(monkeypatch)
    transport = _Transport({"https://example.com/robots.txt": (403, b"", {})})
    compile_count = 0

    def compile_gateway(*args, **kwargs):
        nonlocal compile_count
        del args, kwargs
        compile_count += 1
        return _governed_gateway(transport)

    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)
    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(staged_workflow.settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        staged_workflow, "_compile_acquisition_gateway", compile_gateway
    )

    response = TestClient(create_app()).post(
        "/api/v1/monitor-scopes/1/bootstrap",
        json={
            "scope_path": str(scope_path),
            "acquisition_profile_path": str(profile_path),
        },
    )

    assert response.status_code == 403
    assert response.json()["reason_code"] == "robots.forbidden"
    assert compile_count == 1
    assert transport.requests == ["https://example.com/robots.txt"]
    assert storage_opens == []
    assert db_path.read_bytes() == before


def test_mcp_robots_rejection_creates_no_job_or_other_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scope_path, profile_path = _write_authority(tmp_path, _scope_plan())
    db_path = tmp_path / "mcp.db"
    before = _empty_database(db_path)
    storage_opens = _track_storage_construction(monkeypatch)
    transport = _Transport({"https://example.com/robots.txt": (401, b"", {})})
    monkeypatch.setattr(staged_workflow.settings, "db_path", db_path)
    monkeypatch.setattr(staged_workflow.settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        staged_workflow,
        "_compile_acquisition_gateway",
        lambda *a, **k: _governed_gateway(transport),
    )

    result = web_listening_bootstrap_scope(
        str(scope_path),
        acquisition_profile_path=str(profile_path),
        report_path=str(tmp_path / "mcp-report.md"),
    )

    assert result["schema_version"] == "access-rejection-error.v1"
    assert result["reason_code"] == "robots.auth_required"
    assert transport.requests == ["https://example.com/robots.txt"]
    assert storage_opens == []
    assert db_path.read_bytes() == before
    assert not (tmp_path / "mcp-report.md").exists()
