from __future__ import annotations

import asyncio
import gzip
import hashlib
import http.client
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest

from web_listening.blocks import access_gateway as access_gateway_module
from web_listening.blocks import agentic_orchestration as agentic
from web_listening.blocks import governed_read as governed_read_module
from web_listening.blocks import site_diagnostic as site_diagnostic_module
from web_listening.blocks.access_gateway import (
    AccessGateway,
    AccessGatewayConfig,
    AccessGatewayOriginError,
    AccessGatewayPolicyError,
    AccessGatewayRedirectError,
    AccessGatewayTransportError,
)
from web_listening.blocks.acquisition_execution_plan import (
    compile_acquisition_execution_plan,
)
from web_listening.blocks.acquisition_profile import (
    AcquisitionAdapterConfig,
    AcquisitionProfile,
    AcquisitionRecipeMapping,
    AcquisitionSafetyPolicy,
)
from web_listening.blocks.agentic_orchestration import (
    AgenticAuthority,
    AgenticCandidate,
    AgenticOrchestrationError,
    AgenticOrchestrator,
    AgenticRunResult,
    AgenticSiteRules,
    AgenticTaskRepository,
    HtmlLinkCrawlerAdapter,
    load_agentic_site_rules,
    prepare_agentic_authority,
)
from web_listening.blocks.governed_read import (
    GovernedReadGateway,
    MockClientReadGateway,
)
from web_listening.blocks.immutable_artifacts import ArtifactStore
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    RawHttpResponse,
    SafePinnedTransport,
    TransportFailure,
    normalize_http_url,
)
from web_listening.blocks.storage import Storage
from web_listening.contracts import (
    RuntimeRequirement,
    SecretPolicy,
    SiteSkill,
    SiteSkillExecutor,
    SiteSkillRecipe,
    VerificationRule,
)
from web_listening.contracts import access_decision as access_decision_module
from web_listening.contracts.site_diagnostic import DiagnosticIdentity, canonical_sha256
from web_listening.executors.registry import ExecutorMetadata, ExecutorRegistry
from web_listening.site_skill_registry import ResolvedSiteSkill

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _rules(*, max_requests: int = 4, max_files: int = 4) -> AgenticSiteRules:
    return AgenticSiteRules.model_validate(
        {
            "schema_version": "agentic-site-rules.v1",
            "rule_id": "example-agentic",
            "version": "1.0.0",
            "site_key": "example",
            "scope": {
                "seed_urls": ["https://example.invalid/root.html"],
                "allowed_origins": ["https://example.invalid"],
                "allow_patterns": ["https://example.invalid/**"],
                "queries": [],
            },
            "budgets": {
                "max_depth": 1,
                "max_requests": max_requests,
                "max_bytes": 4096,
                "max_files": max_files,
                "max_concurrency": 2,
                "max_retries": 1,
            },
            "content_types": ["text/html"],
        }
    )


def _compiler_inputs(
    *,
    executor_id: str = "web_http",
    recipe_id: str = "http",
    capabilities: frozenset[str] = frozenset({"http_get"}),
    authorize_browser: bool = False,
    allowed_domains: tuple[str, ...] = ("example.invalid",),
    seed_url: str = "https://example.invalid/root.html",
    homepage_url: str = "https://example.invalid/",
    allowed_page_prefixes: tuple[str, ...] = ("/",),
    allowed_file_prefixes: tuple[str, ...] = ("/",),
) -> SimpleNamespace:
    script_sha256 = "2" * 64
    manifest = SiteSkill(
        skill_id="site-skill-example",
        site_key="example",
        version="1.0.0",
        status="active",
        generated_at=NOW,
        runtime_requirements=(
            RuntimeRequirement(requirement_id="python", description="Python runtime"),
        ),
        secret_policy=SecretPolicy(
            allow_secret_references=False,
            forbid_secret_values=True,
        ),
        allowed_domains=allowed_domains,
        default_executor_id=executor_id,
        default_recipe_id=recipe_id,
        executors=(SiteSkillExecutor(executor_id=executor_id),),
        recipes=(
            SiteSkillRecipe(
                recipe_id=recipe_id,
                executor_id=executor_id,
                profile_ref="profiles/default.yaml",
                entrypoint="scripts/http.py",
                required_capabilities=tuple(sorted(capabilities)),
                verification_rules=(
                    VerificationRule(rule_id="status", description="2xx status"),
                ),
            ),
        ),
    )
    resolved = ResolvedSiteSkill(
        manifest=manifest,
        package_sha256="1" * 64,
        script_sha256=MappingProxyType({"scripts/http.py": script_sha256}),
    )
    registry = ExecutorRegistry.preview(
        {
            executor_id: ExecutorMetadata(
                executor_id=executor_id,
                version="1.0.0",
                capabilities=capabilities,
                timeout_seconds=10.0,
                stdout_bytes=8192,
                stderr_bytes=1024,
            )
        }
    )
    scope = MonitorScopePlan(
        "legacy",
        "example",
        "Example",
        "test",
        "2026-08-21T12:00:00Z",
        "approved",
        "manual",
        "Agentic hardening scope",
        seed_url,
        homepage_url,
        "http",
        {},
        "selected_scope",
        "selected_scope_default",
        "site_root",
        list(allowed_page_prefixes),
        list(allowed_file_prefixes),
        max_depth=2,
        max_pages=8,
        max_files=8,
        based_on={
            "acquisition_profile_id": "example-profile",
            "site_skill_version": manifest.version,
            "site_skill_package_sha256": resolved.package_sha256,
            "site_skill_recipe_id": recipe_id,
            "site_skill_script_sha256": script_sha256,
            "executor_version": "1.0.0",
        },
    )
    profile = AcquisitionProfile(
        profile_id="example-profile",
        site_key="example",
        generated_at="2026-08-21T12:00:00Z",
        default_adapter=executor_id,
        safety=AcquisitionSafetyPolicy(
            allowed_domains=list(allowed_domains),
            allow_stealth_browser=authorize_browser,
            require_authorized_access=authorize_browser,
        ),
        adapters=[AcquisitionAdapterConfig(adapter=executor_id)],
        recipe_mappings=[
            AcquisitionRecipeMapping(adapter=executor_id, recipe_id=recipe_id)
        ],
    )
    plan = compile_acquisition_execution_plan(scope, profile, resolved, registry)
    return SimpleNamespace(
        scope=scope,
        profile=profile,
        resolved_site_skill=resolved,
        executor_registry=registry,
        execution_plan=plan,
    )


def _resolved_authority():
    inputs = _compiler_inputs()
    return inputs.resolved_site_skill, inputs.execution_plan


def _legacy_authority() -> AgenticAuthority:
    return AgenticAuthority(
        site_skill_id="site-skill-example",
        site_skill_version="1.0.0",
        site_skill_package_sha256="1" * 64,
        execution_plan_id="acquisition-plan-example",
        execution_plan_version="acquisition-execution-plan.v1",
        execution_plan_sha256="2" * 64,
        read_adapter_id="web_http",
        read_adapter_version="1.0.0",
    )


class _EncodedTransport:
    def __init__(self, body: bytes, *, filename: str = "report.html") -> None:
        self.body = body
        self.filename = filename

    def request(self, url: str, **kwargs) -> RawHttpResponse:
        del kwargs
        if url.endswith("/robots.txt"):
            return RawHttpResponse(status=404, headers={}, body_chunks=())
        return RawHttpResponse(
            status=200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "content-encoding": "gzip",
                "content-disposition": f'attachment; filename="{self.filename}"',
            },
            body_chunks=(self.body,),
        )


def _gateway(
    body: bytes,
    *,
    authority_sha256: str | None = None,
    filename: str = "report.html",
) -> MockClientReadGateway:
    del authority_sha256

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/html; charset=utf-8",
                "content-encoding": "gzip",
                "content-disposition": f'attachment; filename="{filename}"',
            },
            stream=httpx.ByteStream(body),
            request=request,
        )

    gateway, client = _offline_gateway(handler)
    gateway._test_client = client
    return gateway


def _production_gateway(transport: SafePinnedTransport) -> GovernedReadGateway:
    visible = {
        "identity_id": "agentic-context",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/2.0",
    }
    identity = DiagnosticIdentity(
        **visible,
        identity_sha256=canonical_sha256(visible),
    )
    origin = normalize_http_url("https://example.invalid/")[1]
    return GovernedReadGateway(
        AccessGateway(
            AccessGatewayConfig(
                identity=identity,
                allowed_origins=frozenset({origin}),
                diagnostic_artifact_sha256=(
                    _compiler_inputs().execution_plan.acquisition_fingerprint
                ),
                pacing_interval=timedelta(0),
                budget_limit=8,
            ),
            transport=transport,
            clock=lambda: NOW,
            sleeper=lambda _: None,
        ),
        max_body_bytes=8192,
    )


def _context_gateway(
    transport,
    *,
    clock=lambda: NOW,
    sleeper=lambda _: None,
    policy_ttl: timedelta = timedelta(hours=1),
    pacing_interval: timedelta = timedelta(0),
) -> MockClientReadGateway:
    def handler(request: httpx.Request) -> httpx.Response:
        raw = transport.request(
            str(request.url),
            user_agent="web-listening-bot/2.0",
            identity_sha256="0" * 64,
        )
        try:
            body = b"".join(raw.body_chunks)
        finally:
            raw.close()
        return httpx.Response(
            raw.status,
            headers=dict(raw.headers),
            stream=httpx.ByteStream(body),
            request=request,
        )

    gateway, client = _offline_gateway(handler)
    gateway._prepare_origins(("https://example.invalid",))
    inner = gateway.gateway
    inner.config = replace(
        inner.config,
        policy_ttl=policy_ttl,
        pacing_interval=pacing_interval,
        budget_limit=8,
    )
    inner._clock = clock
    inner._sleep = sleeper
    gateway._test_client = client
    return gateway


def _offline_gateway(handler) -> tuple[MockClientReadGateway, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        MockClientReadGateway(
            client,
            user_agent="web-listening-bot/2.0",
            max_body_bytes=8192,
        ),
        client,
    )


def _prepared(
    *,
    store: ArtifactStore,
    gateway: GovernedReadGateway | MockClientReadGateway,
    execution_plan=None,
    inputs=None,
):
    inputs = inputs or _compiler_inputs()
    return prepare_agentic_authority(
        scope=inputs.scope,
        profile=inputs.profile,
        resolved_site_skill=inputs.resolved_site_skill,
        executor_registry=inputs.executor_registry,
        execution_plan=execution_plan or inputs.execution_plan,
        read_gateway=gateway,
        artifact_store=store,
    )


def test_authority_is_derived_from_resolved_contracts_and_shaped_reader_is_rejected(
    tmp_path: Path,
) -> None:
    inputs = _compiler_inputs()
    plan = inputs.execution_plan
    expected_plan_sha = hashlib.sha256(plan.to_json().encode("utf-8")).hexdigest()
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    authority = _prepared(
        store=store, gateway=_gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    ).authority
    assert authority.execution_plan_sha256 == expected_plan_sha
    try:
        with pytest.raises(AgenticOrchestrationError, match=r"gateway\.type_invalid"):
            prepare_agentic_authority(
                scope=inputs.scope,
                profile=inputs.profile,
                resolved_site_skill=inputs.resolved_site_skill,
                executor_registry=inputs.executor_registry,
                execution_plan=inputs.execution_plan,
                read_gateway=SimpleNamespace(read=lambda *args, **kwargs: None),
                artifact_store=store,
            )
    finally:
        storage.close()


def test_run_lease_and_budget_reservation_are_persisted_compare_and_set(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    repo.create_run(
        run_id="source-run-lease",
        rules=_rules(max_requests=4, max_files=2),
        authority=_legacy_authority(),
    )
    acquired: list[int | None] = []
    barrier = threading.Barrier(2)

    def claim(owner: str) -> None:
        barrier.wait()
        acquired.append(repo.acquire_run_lease("source-run-lease", owner=owner))

    threads = [
        threading.Thread(target=claim, args=(f"owner-{index}",)) for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert acquired.count(None) == 1
    assert [value for value in acquired if value is not None] == [1]
    assert (
        repo.reserve_read_budget(
            "source-run-lease",
            max_requests=3,
            max_bytes=4096,
            max_files=2,
            max_concurrency=1,
        )
        == 4096
    )
    with pytest.raises(
        AgenticOrchestrationError, match=r"budget\.concurrency_exhausted"
    ):
        repo.reserve_read_budget(
            "source-run-lease",
            max_requests=3,
            max_bytes=4096,
            max_files=2,
            max_concurrency=1,
        )
    repo.finish_read_budget(
        "source-run-lease",
        bytes_read=100,
        files=0,
        max_bytes=4096,
        max_files=2,
    )
    assert (
        repo.reserve_read_budget(
            "source-run-lease",
            max_requests=3,
            max_bytes=4096,
            max_files=2,
            max_concurrency=1,
        )
        == 3996
    )
    with pytest.raises(AgenticOrchestrationError, match=r"budget\.bytes_exhausted"):
        repo.finish_read_budget(
            "source-run-lease",
            bytes_read=3997,
            files=0,
            max_bytes=4096,
            max_files=2,
        )
    repo.finish_read_budget(
        "source-run-lease",
        bytes_read=0,
        files=0,
        max_bytes=4096,
        max_files=2,
    )
    storage.close()


def test_governed_read_preserves_wire_decode_and_filename_evidence() -> None:
    entity = b"<!doctype html><html><body>encoded</body></html>"
    encoded = gzip.compress(entity, mtime=0)
    result = _gateway(encoded).read("https://example.invalid/report.html")

    assert result.body == entity
    assert result.wire_bytes == len(encoded)
    assert result.decoded_bytes == len(entity)
    assert result.wire_encoding == "gzip"
    assert result.content_encoding == "utf-8"
    assert result.filename == "report.html"


def test_gzip_and_filename_evidence_reaches_immutable_store_and_contradiction_fails(
    tmp_path: Path,
) -> None:
    entity = b"<!doctype html><html><body>encoded</body></html>"
    encoded = gzip.compress(entity, mtime=0)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    completed_gateway = _gateway(encoded)
    completed = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=completed_gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-gzip-evidence")
    observation = completed.artifacts[0].observation
    assert observation.wire_encoding == "gzip"
    assert observation.content_encoding == "utf-8"
    assert observation.filename == "report.html"

    rejected_gateway = _gateway(encoded, filename="report.pdf")
    rejected = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=rejected_gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-filename-contradiction")
    assert rejected.parent.status == "failed"
    assert rejected.tasks[0].failure_code == "artifact_store.mime.extension_mismatch"
    storage.close()


def test_storage_failure_or_baseexception_cannot_leave_running_task_or_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        store,
        "_publish_blob",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(rules=_rules(), run_id="source-run-interrupt")

    parent = orchestrator.repository.require_run("source-run-interrupt")
    tasks = orchestrator.repository.list_tasks(parent.run_id)
    assert parent.status == "cancelled"
    assert all(task.status in {"completed", "failed", "cancelled"} for task in tasks)
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
        == 0
    )
    storage.close()


def test_sqlite_failure_rolls_back_artifact_and_compensates_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = b"<!doctype html><html><body>atomic</body></html>"
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(entity, mtime=0))
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        orchestrator.repository,
        "add_observation",
        lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("disk")),
    )

    with pytest.raises(sqlite3.OperationalError):
        orchestrator.run(rules=_rules(), run_id="source-run-sqlite-failure")

    parent = orchestrator.repository.require_run("source-run-sqlite-failure")
    assert parent.status == "failed"
    assert orchestrator.repository.list_tasks(parent.run_id)[0].status == "failed"
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
        == 0
    )
    assert not tuple((tmp_path / "artifacts").rglob("*.gz"))
    storage.close()


def test_required_children_are_atomically_sealed_and_terminal_state_is_immutable(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    repo.create_run(
        run_id="source-run-sealed",
        rules=_rules(),
        authority=_legacy_authority(),
    )
    task = repo.create_task(
        run_id="source-run-sealed",
        task_key="search:required",
        kind="search",
        required=True,
        query="required",
        discovery_kind="search",
    )
    repo.seal_required_tasks("source-run-sealed")
    assert (
        repo.create_task(
            run_id="source-run-sealed",
            task_key="search:required",
            kind="search",
            required=True,
            query="required",
            discovery_kind="search",
        )
        == task
    )
    with pytest.raises(AgenticOrchestrationError, match=r"task\.required_set_sealed"):
        repo.create_task(
            run_id="source-run-sealed",
            task_key="read:https://example.invalid/late.html",
            kind="read",
            required=True,
            requested_url="https://example.invalid/late.html",
            discovery_kind="seed",
        )
    task = repo.transition_task(task.task_id, status="running")
    repo.transition_task(task.task_id, status="completed")
    first = repo.finalize_run("source-run-sealed", requested_status="completed")
    assert repo.finalize_run("source-run-sealed", requested_status="completed") == first
    with pytest.raises(AgenticOrchestrationError, match=r"run\.transition_invalid"):
        repo.finalize_run("source-run-sealed", requested_status="partial")
    storage.close()


def test_rule_loader_rejects_recursive_parser_resource_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text("schema_version: agentic-site-rules.v1", encoding="utf-8")
    monkeypatch.setattr(
        agentic.yaml,
        "load",
        lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError()),
    )
    with pytest.raises(AgenticOrchestrationError, match=r"rules\.invalid"):
        load_agentic_site_rules(path)


def test_forged_plan_dataclass_cannot_become_agentic_authority(tmp_path: Path) -> None:
    _, plan = _resolved_authority()
    forged = replace(plan, acquisition_fingerprint="f" * 64)
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    with pytest.raises(AgenticOrchestrationError, match=r"authority\.binding_invalid"):
        _prepared(store=store, gateway=gateway, execution_plan=forged)
    prepared = _prepared(store=store, gateway=gateway)
    with pytest.raises(TypeError):
        replace(prepared, execution_plan=forged)
    storage.close()


def test_gateway_and_artifact_store_subclasses_are_not_trusted_capabilities(
    tmp_path: Path,
) -> None:
    concrete_gateway = _production_gateway(SafePinnedTransport(timeout=1.0))

    class DerivedAccessGateway(AccessGateway):
        pass

    class DerivedArtifactStore(ArtifactStore):
        pass

    derived_gateway = GovernedReadGateway(
        DerivedAccessGateway(
            concrete_gateway.gateway.config,
            transport=concrete_gateway.gateway.transport,
        ),
        max_body_bytes=concrete_gateway.max_body_bytes,
    )
    storage = Storage(tmp_path / "db.sqlite")
    concrete_store = ArtifactStore(storage, root=tmp_path / "artifacts")
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.type_invalid"):
        _prepared(store=concrete_store, gateway=derived_gateway)
    with pytest.raises(AgenticOrchestrationError, match=r"authority\.type_invalid"):
        _prepared(
            store=DerivedArtifactStore(storage, root=tmp_path / "derived-artifacts"),
            gateway=concrete_gateway,
        )
    storage.close()


def test_terminal_ledger_rejects_missing_final_observation_and_artifact_lineage(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    completed = orchestrator.run(rules=_rules(), run_id="source-run-terminal-evidence")
    with pytest.raises(sqlite3.IntegrityError, match="requires running state"):
        orchestrator.repository.add_observation(
            task=completed.tasks[0],
            attempt=2,
            status="failed",
            final_url="https://example.invalid/root.html",
            access_decision_id=completed.tasks[0].access_decision_id,
            artifact_id=None,
            reason_code="test.late_observation",
            redirect_chain=(),
        )
    storage.conn.execute("DROP TRIGGER guard_agentic_observations_delete")
    storage.conn.execute(
        "DELETE FROM agentic_observations WHERE observation_id = ?",
        (completed.observations[-1].observation_id,),
    )
    storage.conn.commit()

    with pytest.raises(AgenticOrchestrationError, match=r"ledger\.invalid"):
        orchestrator.repository.require_run(completed.parent.run_id)
    storage.close()


def test_body_failure_keeps_gateway_context_and_persists_access_decision(
    tmp_path: Path,
) -> None:
    with pytest.raises(BodyFailure) as captured:
        _gateway(b"not-gzip").read("https://example.invalid/report.html")
    assert captured.value.decision.decision_id
    assert captured.value.final_url == "https://example.invalid/report.html"
    assert captured.value.status_code == 200
    assert captured.value.redirect_hops == ()
    with pytest.raises(BodyFailure) as metadata_failure:
        _gateway(
            gzip.compress(b"<html>ok</html>", mtime=0), filename="../unsafe.html"
        ).read("https://example.invalid/report.html")
    assert metadata_failure.value.decision.decision_id
    assert metadata_failure.value.final_url == "https://example.invalid/report.html"
    assert metadata_failure.value.status_code == 200

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(b"not-gzip")
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-body-context")
    assert result.observations[-1].access_decision_id is not None
    storage.close()


def test_redirect_failure_keeps_context_and_persists_access_decision(
    tmp_path: Path,
) -> None:
    class MissingLocationTransport:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            return RawHttpResponse(status=302, headers={}, body_chunks=())

    with pytest.raises(AccessGatewayRedirectError) as captured:
        _context_gateway(MissingLocationTransport()).read(
            "https://example.invalid/root.html"
        )
    assert captured.value.decision.decision_id
    assert captured.value.current_url == "https://example.invalid/root.html"
    assert captured.value.final_url == "https://example.invalid/root.html"
    assert captured.value.status_code == 302
    assert captured.value.redirect_hops == ()

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _context_gateway(MissingLocationTransport())
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-redirect-context")
    assert result.observations[-1].access_decision_id is not None
    storage.close()


def test_post_reservation_policy_failure_keeps_and_persists_context(
    tmp_path: Path,
) -> None:
    class ManualClock:
        def __init__(self) -> None:
            self.value = NOW

        def __call__(self) -> datetime:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += timedelta(seconds=seconds)

    class HtmlTransport:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    def policy_gateway() -> GovernedReadGateway:
        clock = ManualClock()
        gateway = _context_gateway(
            HtmlTransport(),
            clock=clock,
            sleeper=lambda _: clock.advance(10),
            policy_ttl=timedelta(seconds=5),
            pacing_interval=timedelta(seconds=1),
        )
        gateway.read("https://example.invalid/prime.html")
        return gateway

    with pytest.raises(AccessGatewayPolicyError) as captured:
        policy_gateway().read("https://example.invalid/root.html")
    assert captured.value.decision.decision_id
    assert captured.value.current_url == "https://example.invalid/root.html"
    assert captured.value.final_url == "https://example.invalid/root.html"
    assert captured.value.status_code is None
    assert captured.value.redirect_hops == ()

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = policy_gateway()
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-policy-context")
    assert result.observations[-1].access_decision_id is not None
    storage.close()


def test_malformed_crawler_candidates_become_stable_partial_warning(
    tmp_path: Path,
) -> None:
    class MalformedCrawler:
        adapter_id = "malformed-crawler"
        adapter_version = "1.0.0"

        def discover(self, **kwargs):
            del kwargs
            return (object(),)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=MalformedCrawler(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-malformed-crawler")

    assert result.parent.status == "partial"
    assert "crawler.discovery_failed" in result.parent.warnings
    storage.close()


def test_round3_prepared_scope_rejects_broader_rules_before_io(tmp_path: Path) -> None:
    inputs = _compiler_inputs()
    inputs.scope.seed_url = "https://example.invalid/reports/root.html"
    inputs.scope.homepage_url = "https://example.invalid/reports/"
    inputs.scope.allowed_page_prefixes = ["/reports"]
    inputs.scope.allowed_file_prefixes = ["/reports"]
    inputs.execution_plan = compile_acquisition_execution_plan(
        inputs.scope,
        inputs.profile,
        inputs.resolved_site_skill,
        inputs.executor_registry,
    )
    sends: list[str] = []

    gateway, client = _offline_gateway(
        lambda request: (
            sends.append(str(request.url)) or httpx.Response(404, request=request)
        )
    )
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = prepare_agentic_authority(
        scope=inputs.scope,
        profile=inputs.profile,
        resolved_site_skill=inputs.resolved_site_skill,
        executor_registry=inputs.executor_registry,
        execution_plan=inputs.execution_plan,
        read_gateway=gateway,
        artifact_store=store,
    )
    payload = _rules().model_dump(mode="json")
    payload["scope"]["seed_urls"] = ["https://example.invalid/admin/root.html"]
    broad = AgenticSiteRules.model_validate(payload)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    with pytest.raises(AgenticOrchestrationError, match=r"rules\.scope_broader"):
        orchestrator.run(rules=broad, run_id="source-run-scope-broader")
    assert sends == []
    client.close()
    storage.close()


def test_round3_mock_client_swap_is_rejected_before_io(tmp_path: Path) -> None:
    sends: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sends.append(str(request.url))
        return httpx.Response(200, text="ok")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = MockClientReadGateway(
        client, user_agent="web-listening-bot/2.0", max_body_bytes=8192
    )
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(store=store, gateway=gateway)
    replacement = httpx.Client(transport=httpx.MockTransport(handler))
    gateway._transport.client = replacement
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        AgenticOrchestrator(
            storage=storage,
            prepared_authority=prepared,
            crawler_adapter=HtmlLinkCrawlerAdapter(),
        )
    assert sends == []
    client.close()
    replacement.close()
    storage.close()


def test_round3_artifact_store_write_configuration_is_sealed(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    other = Storage(tmp_path / "other.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store, gateway=_gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    )
    store.storage = other
    with pytest.raises(
        AgenticOrchestrationError, match=r"artifact_store\.seal_invalid"
    ):
        AgenticOrchestrator(
            storage=storage,
            prepared_authority=prepared,
            crawler_adapter=HtmlLinkCrawlerAdapter(),
        )
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0] == 0
    )
    assert other.conn.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0] == 0
    storage.close()
    other.close()


def test_round3_public_observation_rejects_unbound_provenance(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    repo.create_run(
        run_id="source-run-provenance", rules=_rules(), authority=_legacy_authority()
    )
    task = repo.create_task(
        run_id="source-run-provenance",
        task_key="read:https://example.invalid/root.html",
        kind="read",
        required=True,
        requested_url="https://example.invalid/root.html",
        depth=0,
        discovery_kind="seed",
        adapter_id="web_http",
        adapter_version="1.0.0",
    )
    task = repo.transition_task(task.task_id, status="running")
    with pytest.raises(
        AgenticOrchestrationError, match=r"observation\.provenance_invalid"
    ):
        repo.add_observation(
            task=task,
            attempt=1,
            status="failed",
            final_url="https://example.invalid/next.html",
            access_decision_id="access-decision-0000000000000000",
            artifact_id=None,
            reason_code="gateway.redirect",
            redirect_chain=({"ordinal": 99, "body": "secret"},),
        )
    storage.close()


def test_round3_run_request_budget_counts_each_content_target_not_robots(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    class RedirectTransport:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            sent.append(url)
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            if url.endswith("root.html"):
                return RawHttpResponse(
                    status=302,
                    headers={"location": "/next.html"},
                    body_chunks=(),
                )
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>never</html>",),
            )

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _context_gateway(RedirectTransport())
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(max_requests=1), run_id="source-run-target-budget")
    assert result.parent.requests_used == 1
    assert result.tasks[0].failure_code == "budget.requests_exhausted"
    assert sum(not value.endswith("/robots.txt") for value in sent) == 1
    storage.close()


def test_round3_failure_context_is_durable_in_ledger(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=_gateway(b"not-gzip")),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-durable-context")
    observation = result.observations[-1]
    assert observation.current_url == "https://example.invalid/root.html"
    assert observation.status_code == 200
    row = storage.conn.execute(
        "SELECT current_url, status_code FROM agentic_observations"
    ).fetchone()
    assert tuple(row) == (observation.current_url, observation.status_code)
    storage.close()


def test_round3_candidate_kind_is_a_runtime_closed_enum() -> None:
    with pytest.raises(
        AgenticOrchestrationError, match=r"candidate\.discovery_invalid"
    ):
        AgenticCandidate(
            url="https://example.invalid/root.html",
            discovery_kind="bogus",
            discovered_from_url="https://example.invalid/",
        )


def test_round3_finalize_uses_exact_derived_parent_outcome(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    repo.create_run(
        run_id="source-run-outcome", rules=_rules(), authority=_legacy_authority()
    )
    required = repo.create_task(
        run_id="source-run-outcome",
        task_key="search:required",
        kind="search",
        required=True,
        query="required",
        discovery_kind="search",
    )
    optional = repo.create_task(
        run_id="source-run-outcome",
        task_key="search:optional",
        kind="search",
        required=False,
        query="optional",
        discovery_kind="search",
    )
    repo.seal_required_tasks("source-run-outcome")
    repo.transition_task(required.task_id, status="running")
    repo.transition_task(required.task_id, status="completed")
    repo.transition_task(
        optional.task_id, status="failed", failure_code="search.adapter_error"
    )
    with pytest.raises(AgenticOrchestrationError, match=r"run\.outcome_conflict"):
        repo.finalize_run("source-run-outcome", requested_status="completed")
    assert (
        repo.finalize_run(
            "source-run-outcome",
            requested_status="partial",
            warnings=("optional_child_failed",),
        ).status
        == "partial"
    )
    storage.close()


def test_round3_migration_replaces_missing_trigger_and_bumps_version(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    AgenticTaskRepository(storage, clock=lambda: NOW)
    storage.conn.execute("DROP TRIGGER guard_agentic_observations_insert")
    storage.conn.execute("DELETE FROM agentic_ledger_schema")
    storage.conn.execute(
        "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
    )
    storage.conn.commit()
    AgenticTaskRepository(storage, clock=lambda: NOW)
    version = storage.conn.execute(
        "SELECT schema_name, version FROM agentic_ledger_schema"
    ).fetchone()
    trigger = storage.conn.execute(
        "SELECT group_concat(sql, ' ') FROM sqlite_master WHERE type='trigger' AND name LIKE 'guard_agentic_%'"
    ).fetchone()[0]
    assert tuple(version) == ("agentic-ledger.v2", 2)
    assert "requires running state" in trigger
    storage.close()


def test_round3_replay_requires_terminal_compatible_source(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    repo.create_run(
        run_id="source-run-replay-source", rules=_rules(), authority=_legacy_authority()
    )
    with pytest.raises(AgenticOrchestrationError, match=r"replay\.source_not_terminal"):
        repo.create_run(
            run_id="source-run-replay-target",
            rules=_rules(),
            authority=_legacy_authority(),
            replay_of_run_id="source-run-replay-source",
        )
    storage.close()


def test_round4_prepared_gateway_seals_normal_and_mock_transport_state(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    original_transport = SafePinnedTransport(timeout=10.0, chunk_size=1024)
    gateway = _production_gateway(original_transport)
    prepared = _prepared(store=store, gateway=gateway)

    gateway.gateway.transport = SafePinnedTransport(timeout=10.0, chunk_size=1024)
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        AgenticOrchestrator(
            storage=storage,
            prepared_authority=prepared,
            crawler_adapter=HtmlLinkCrawlerAdapter(),
        )
    gateway.gateway.transport = original_transport
    gateway.gateway.config = replace(
        gateway.gateway.config, pacing_interval=timedelta(seconds=1)
    )
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    assert sent == []

    mock_sent: list[str] = []

    def mock_response(request: httpx.Request) -> httpx.Response:
        mock_sent.append(str(request.url))
        return httpx.Response(200, text=str(request.url))

    client = httpx.Client(transport=httpx.MockTransport(mock_response))
    mock = MockClientReadGateway(
        client, user_agent="web-listening-bot/2.0", max_body_bytes=8192
    )
    mock_prepared = _prepared(store=store, gateway=mock)
    original_user_agent = mock._user_agent
    mock._user_agent = "changed-bot/1.0"
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        mock_prepared.validate()
    mock._user_agent = original_user_agent
    internal_gateway = next(iter(mock._gateways.values())).gateway
    original_states = internal_gateway._origin_states
    internal_gateway._origin_states = {}
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        mock_prepared.validate()
    internal_gateway._origin_states = original_states
    mock._transport = SimpleNamespace(client=client)
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        mock_prepared.validate()
    assert mock_sent == []
    client.close()
    storage.close()


def test_round4_rejects_non_http_compiled_execution_before_storage_or_io(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    for executor_id, authorize in (
        ("browser_rendered", False),
        ("cloakbrowser", True),
        ("browseract", True),
    ):
        inputs = _compiler_inputs(
            executor_id=executor_id,
            recipe_id=f"{executor_id}-recipe",
            capabilities=frozenset({executor_id}),
            authorize_browser=authorize,
        )
        gateway = _gateway(
            gzip.compress(b"<html>never</html>", mtime=0),
            authority_sha256=inputs.execution_plan.acquisition_fingerprint,
        )
        with pytest.raises(
            AgenticOrchestrationError, match=r"authority\.http_execution_required"
        ):
            prepare_agentic_authority(
                scope=inputs.scope,
                profile=inputs.profile,
                resolved_site_skill=inputs.resolved_site_skill,
                executor_registry=inputs.executor_registry,
                execution_plan=inputs.execution_plan,
                read_gateway=gateway,
                artifact_store=store,
            )
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM artifact_blobs").fetchone()[0] == 0
    )
    storage.close()


def test_round4_page_and_file_scope_and_success_budgets_are_separate(
    tmp_path: Path,
) -> None:
    inputs = _compiler_inputs()
    inputs.scope.seed_url = "https://example.invalid/pages/root.html"
    inputs.scope.homepage_url = "https://example.invalid/pages/"
    inputs.scope.allowed_page_prefixes = ["/pages"]
    inputs.scope.allowed_file_prefixes = ["/files"]
    inputs.scope.max_depth = 1
    inputs.scope.max_pages = 4
    inputs.scope.max_files = 1
    inputs.execution_plan = compile_acquisition_execution_plan(
        inputs.scope,
        inputs.profile,
        inputs.resolved_site_skill,
        inputs.executor_registry,
    )

    class TypedTransport:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            if url.endswith("/pages/root.html"):
                return RawHttpResponse(
                    status=302,
                    headers={"location": "/pages/root-final.html"},
                    body_chunks=(),
                )
            if url.endswith(".pdf"):
                return RawHttpResponse(
                    status=200,
                    headers={"content-type": "application/pdf"},
                    body_chunks=(b"%PDF-1.7\n",),
                )
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    gateway = _context_gateway(TypedTransport())
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = prepare_agentic_authority(
        scope=inputs.scope,
        profile=inputs.profile,
        resolved_site_skill=inputs.resolved_site_skill,
        executor_registry=inputs.executor_registry,
        execution_plan=inputs.execution_plan,
        read_gateway=gateway,
        artifact_store=store,
    )
    payload = _rules(max_requests=5, max_files=1).model_dump(mode="json")
    payload["scope"]["seed_urls"] = [
        "https://example.invalid/pages/root.html",
        "https://example.invalid/files/wrong.html",
        "https://example.invalid/files/one.pdf",
        "https://example.invalid/pages/later.html",
    ]
    payload["scope"]["allow_patterns"] = [
        "https://example.invalid/pages/**",
        "https://example.invalid/files/**",
    ]
    payload["budgets"]["max_depth"] = 0
    payload["content_types"] = ["text/html", "application/pdf"]
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(
        rules=AgenticSiteRules.model_validate(payload),
        run_id="source-run-page-file-scope",
    )
    by_url = {task.requested_url: task for task in result.tasks}
    assert by_url["https://example.invalid/files/wrong.html"].failure_code == (
        "scope.final_url_rejected"
    )
    assert by_url["https://example.invalid/files/one.pdf"].status == "completed"
    assert by_url["https://example.invalid/pages/later.html"].status == "completed"
    assert result.observations[0].final_url == (
        "https://example.invalid/pages/root-final.html"
    )
    assert result.parent.pages_used == 2
    assert result.parent.files_used == 1
    storage.close()


def test_round4_redirect_rejection_keeps_closed_original_failure_provenance(
    tmp_path: Path,
) -> None:
    class CrossOriginRedirect:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            return RawHttpResponse(
                status=302,
                headers={"location": "https://outside.invalid/next.html"},
                body_chunks=(),
            )

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _context_gateway(CrossOriginRedirect())
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-cross-origin-reject")
    observation = result.observations[-1]
    assert observation.reason_code == "gateway.origin"
    assert observation.current_url == "https://example.invalid/root.html"
    assert observation.final_url == "https://outside.invalid/next.html"
    assert observation.status_code == 302
    assert observation.redirect_chain == ()
    storage.close()


def test_round4_post_artifact_baseexception_and_stale_reservation_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class InterruptCrawler:
        adapter_id = "interrupt-crawler"
        adapter_version = "1.0.0"

        def discover(self, **kwargs):
            del kwargs
            raise KeyboardInterrupt()

    storage = Storage(tmp_path / "interrupt.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "interrupt-artifacts")
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(
            store=store,
            gateway=_gateway(gzip.compress(b"<html>ok</html>", mtime=0)),
        ),
        crawler_adapter=InterruptCrawler(),
        clock=lambda: NOW,
    )
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run(rules=_rules(), run_id="source-run-crawler-interrupt")
    interrupted = orchestrator.repository.require_run("source-run-crawler-interrupt")
    assert interrupted.status == "cancelled"
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
        == 1
    )
    storage.close()

    masking_storage = Storage(tmp_path / "masking.sqlite")
    masking_store = ArtifactStore(masking_storage, root=tmp_path / "masking-artifacts")
    masking = AgenticOrchestrator(
        storage=masking_storage,
        prepared_authority=_prepared(
            store=masking_store,
            gateway=_gateway(gzip.compress(b"<html>ok</html>", mtime=0)),
        ),
        crawler_adapter=InterruptCrawler(),
        clock=lambda: NOW,
    )

    def fail_interruption_write(*args, **kwargs) -> None:
        del args, kwargs
        raise sqlite3.OperationalError("simulated interruption persistence failure")

    monkeypatch.setattr(masking.repository, "interrupt_run", fail_interruption_write)
    with pytest.raises(KeyboardInterrupt):
        masking.run(rules=_rules(), run_id="source-run-no-sqlite-masking")
    masking_storage.close()

    recovery_storage = Storage(tmp_path / "recovery.sqlite")
    recovery_store = ArtifactStore(
        recovery_storage, root=tmp_path / "recovery-artifacts"
    )
    recovery = AgenticOrchestrator(
        storage=recovery_storage,
        prepared_authority=_prepared(
            store=recovery_store,
            gateway=_gateway(gzip.compress(b"<html>unused</html>", mtime=0)),
        ),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    recovery.repository.create_run(
        run_id="source-run-stale-read",
        rules=_rules(),
        authority=recovery.authority,
    )
    task = recovery.repository.create_task(
        run_id="source-run-stale-read",
        task_key="read:https://example.invalid/root.html",
        kind="read",
        required=True,
        requested_url="https://example.invalid/root.html",
        discovery_kind="seed",
        adapter_id="web_http",
        adapter_version="1.0.0",
    )
    recovery.repository.seal_required_tasks("source-run-stale-read")
    recovery.repository.transition_task(task.task_id, status="running")
    recovery.repository.begin_read_budget(
        "source-run-stale-read",
        max_requests=4,
        max_bytes=4096,
        max_files=4,
        max_concurrency=2,
    )
    recovered = recovery.run(rules=_rules(), run_id="source-run-stale-read")
    assert recovered.parent.status == "failed"
    assert (
        recovery_storage.conn.execute(
            "SELECT active_reads FROM agentic_runs WHERE run_id = 'source-run-stale-read'"
        ).fetchone()[0]
        == 0
    )
    recovery_storage.close()


def test_round4_v1_migration_backfills_evidence_and_future_version_fails_closed(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "v1.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "v1-artifacts")
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(
            store=store,
            gateway=_gateway(gzip.compress(b"<html>ok</html>", mtime=0)),
        ),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    completed = orchestrator.run(rules=_rules(), run_id="source-run-v1-upgrade")
    for trigger_name in agentic._LEDGER_TRIGGER_NAMES:
        storage.conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    for table in ("agentic_runs", "agentic_tasks", "agentic_observations"):
        storage.conn.execute(f"UPDATE {table} SET schema_version = 'agentic-ledger.v1'")
    storage.conn.execute("ALTER TABLE agentic_runs DROP COLUMN pages_used")
    storage.conn.execute("ALTER TABLE agentic_observations DROP COLUMN current_url")
    storage.conn.execute("ALTER TABLE agentic_observations DROP COLUMN status_code")
    storage.conn.execute("DELETE FROM agentic_ledger_schema")
    storage.conn.execute(
        "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
    )
    storage.conn.commit()
    assert "pages_used" not in {
        row[1] for row in storage.conn.execute("PRAGMA table_info(agentic_runs)")
    }
    assert "current_url" not in {
        row[1]
        for row in storage.conn.execute("PRAGMA table_info(agentic_observations)")
    }
    upgraded = AgenticTaskRepository(storage, clock=lambda: NOW)
    observation = upgraded.list_observations(completed.parent.run_id)[0]
    assert observation.current_url == observation.final_url
    assert observation.status_code == 200
    assert tuple(
        storage.conn.execute(
            "SELECT schema_name, version FROM agentic_ledger_schema"
        ).fetchone()
    ) == ("agentic-ledger.v2", 2)
    storage.close()

    future = Storage(tmp_path / "future.sqlite")
    AgenticTaskRepository(future, clock=lambda: NOW)
    future.conn.execute("DELETE FROM agentic_ledger_schema")
    future.conn.execute(
        "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v99', 99)"
    )
    future.conn.commit()
    with pytest.raises(AgenticOrchestrationError, match=r"ledger\.version_unsupported"):
        AgenticTaskRepository(future, clock=lambda: NOW)
    assert tuple(
        future.conn.execute(
            "SELECT schema_name, version FROM agentic_ledger_schema"
        ).fetchone()
    ) == ("agentic-ledger.v99", 99)
    future.close()


def test_round5_transport_seals_allow_only_exact_production_and_mock_call_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    sent: list[str] = []
    transport = SafePinnedTransport(timeout=10.0, chunk_size=1024)
    production = _production_gateway(transport)
    prepared = _prepared(store=store, gateway=production)

    transport.request = lambda *args, **kwargs: sent.append("instance")
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    del transport.request

    original_request = SafePinnedTransport.request

    def changed_request(self, url: str, **kwargs) -> RawHttpResponse:
        del self, url, kwargs
        sent.append("class")
        return RawHttpResponse(status=500, headers={}, body_chunks=())

    with monkeypatch.context() as scoped:
        scoped.setattr(SafePinnedTransport, "request", changed_request)
        with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
            prepared.validate()
    assert SafePinnedTransport.request is original_request

    invalid_production = _production_gateway(SafePinnedTransport(timeout=1.0))
    invalid_production.gateway.transport = _EncodedTransport(
        gzip.compress(b"<html/>", mtime=0)
    )
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.transport_invalid"):
        _prepared(store=store, gateway=invalid_production)

    mock, client = _offline_gateway(
        lambda request: httpx.Response(
            404 if request.url.path == "/robots.txt" else 200
        )
    )
    mock_prepared = _prepared(store=store, gateway=mock)
    mock_transport = client._transport
    setattr(
        mock_transport,
        mock._transport._handler_attribute,
        lambda request: httpx.Response(200, request=request),
    )
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        mock_prepared.validate()
    assert sent == []
    client.close()
    storage.close()


def test_round5_legacy_mock_robots_compatibility_and_agentic_mode_seal(
    tmp_path: Path,
) -> None:
    legacy_requests: list[str] = []

    def legacy_handler(request: httpx.Request) -> httpx.Response:
        legacy_requests.append(str(request.url))
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>legacy target</html>",
        )

    legacy, legacy_client = _offline_gateway(legacy_handler)
    result = legacy.read("https://example.invalid/legacy")
    assert result.status_code == 200
    assert legacy_requests == ["https://example.invalid/legacy"]
    legacy_client.close()

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared_gateway, prepared_client = _offline_gateway(
        lambda request: httpx.Response(404, request=request)
    )
    prepared = _prepared(store=store, gateway=prepared_gateway)
    sealed_mode = prepared_gateway._transport._robots_mode
    prepared_gateway._transport._robots_mode = type(
        prepared_gateway._transport
    )._LEGACY_ROBOTS_MODE
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    prepared_gateway._transport._robots_mode = sealed_mode
    prepared_client.close()
    storage.close()


def test_round5_decisionless_gateway_failures_keep_closed_durable_provenance(
    tmp_path: Path,
) -> None:
    def html_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>never</html>",
            request=request,
        )

    def run_failure(label: str, mutate) -> tuple[AgenticRunResult, Storage]:
        storage = Storage(tmp_path / f"{label}.sqlite")
        store = ArtifactStore(storage, root=tmp_path / f"{label}-artifacts")
        gateway, client = _offline_gateway(html_handler)
        prepared = _prepared(store=store, gateway=gateway)
        mutate(gateway)
        result = AgenticOrchestrator(
            storage=storage,
            prepared_authority=prepared,
            crawler_adapter=HtmlLinkCrawlerAdapter(),
            clock=lambda: NOW,
        ).run(rules=_rules(), run_id=f"source-run-{label}")
        client.close()
        return result, storage

    def exhaust_origin_budget(gateway: MockClientReadGateway) -> None:
        inner = next(iter(gateway._gateways.values())).gateway
        state = next(iter(inner._origin_states.values()))
        state.budget_window_started_at = datetime.now(UTC)
        state.budget_used = inner.config.budget_limit

    def expire_before_pacing(gateway: MockClientReadGateway) -> None:
        inner = next(iter(gateway._gateways.values())).gateway
        state = next(iter(inner._origin_states.values()))
        state.last_request_started_at = datetime.now(UTC) + timedelta(hours=2)

    global_storage = Storage(tmp_path / "global-budget.sqlite")
    global_store = ArtifactStore(
        global_storage, root=tmp_path / "global-budget-artifacts"
    )
    global_gateway, global_client = _offline_gateway(html_handler)
    global_payload = _rules(max_requests=1).model_dump(mode="json")
    global_payload["scope"]["seed_urls"].append("https://example.invalid/second.html")
    global_result = AgenticOrchestrator(
        storage=global_storage,
        prepared_authority=_prepared(store=global_store, gateway=global_gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(
        rules=AgenticSiteRules.model_validate(global_payload),
        run_id="source-run-initial-global-budget",
    )
    global_observation = global_result.observations[-1]
    assert global_result.parent.status == "partial"
    assert global_observation.reason_code == "budget.requests_exhausted"
    assert global_observation.access_decision_id is None
    assert global_observation.current_url == global_observation.requested_url
    assert global_observation.final_url == global_observation.requested_url
    assert (
        global_storage.conn.execute(
            "SELECT active_reads FROM agentic_runs WHERE run_id = 'source-run-initial-global-budget'"
        ).fetchone()[0]
        == 0
    )
    global_client.close()
    global_storage.close()

    for label, mutate, reason in (
        ("initial-budget", exhaust_origin_budget, "gateway.budget"),
        ("initial-policy", expire_before_pacing, "gateway.policy"),
    ):
        result, storage = run_failure(label, mutate)
        observation = result.observations[-1]
        assert result.parent.status == "failed"
        assert observation.reason_code == reason
        assert observation.access_decision_id is None
        assert observation.current_url == observation.requested_url
        assert observation.final_url == observation.requested_url
        assert observation.status_code is None
        assert (
            storage.conn.execute(
                "SELECT active_reads FROM agentic_runs WHERE run_id = ?",
                (result.parent.run_id,),
            ).fetchone()[0]
            == 0
        )
        storage.close()

    robots_storage = Storage(tmp_path / "robots.sqlite")
    robots_store = ArtifactStore(robots_storage, root=tmp_path / "robots-artifacts")

    def robots_safety(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            raise TransportFailure("tls_policy", "sensitive host path", safety=True)
        raise AssertionError("target content must not be sent")

    robots_gateway, robots_client = _offline_gateway(robots_safety)
    robots_result = AgenticOrchestrator(
        storage=robots_storage,
        prepared_authority=_prepared(store=robots_store, gateway=robots_gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=_rules(), run_id="source-run-robots-safety")
    robots_observation = robots_result.observations[-1]
    assert robots_observation.reason_code == "gateway.transport.tls_policy"
    assert robots_observation.access_decision_id is None
    assert (
        robots_storage.conn.execute(
            "SELECT active_reads FROM agentic_runs WHERE run_id = 'source-run-robots-safety'"
        ).fetchone()[0]
        == 0
    )
    robots_client.close()
    robots_storage.close()

    inputs = _compiler_inputs(
        allowed_domains=("example.invalid", "second.invalid"),
        homepage_url="https://second.invalid/",
    )
    redirect_storage = Storage(tmp_path / "redirect.sqlite")
    redirect_store = ArtifactStore(
        redirect_storage, root=tmp_path / "redirect-artifacts"
    )

    def redirect_then_robots_failure(request: httpx.Request) -> httpx.Response:
        if request.url.host == "second.invalid" and request.url.path == "/robots.txt":
            raise TransportFailure("tls_policy", "never persist me", safety=True)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            302,
            headers={"location": "https://second.invalid/final.html"},
            request=request,
        )

    redirect_gateway, redirect_client = _offline_gateway(redirect_then_robots_failure)
    redirect_payload = _rules().model_dump(mode="json")
    redirect_payload["scope"]["allowed_origins"].append("https://second.invalid")
    redirect_payload["scope"]["allow_patterns"].append("https://second.invalid/**")
    redirect_result = AgenticOrchestrator(
        storage=redirect_storage,
        prepared_authority=_prepared(
            store=redirect_store, gateway=redirect_gateway, inputs=inputs
        ),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(
        rules=AgenticSiteRules.model_validate(redirect_payload),
        run_id="source-run-next-origin-policy",
    )
    redirected = redirect_result.observations[-1]
    assert redirected.reason_code == "gateway.transport.tls_policy"
    assert redirected.current_url == "https://second.invalid/final.html"
    assert redirected.final_url == redirected.current_url
    assert (
        redirected.access_decision_id
        == redirected.redirect_chain[-1]["access_decision_id"]
    )
    assert redirected.access_decision_id is not None
    assert (
        redirect_storage.conn.execute(
            "SELECT active_reads FROM agentic_runs WHERE run_id = 'source-run-next-origin-policy'"
        ).fetchone()[0]
        == 0
    )
    redirect_client.close()
    redirect_storage.close()


def test_round5_adapter_exception_boundary_covers_call_and_iteration_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenSearch:
        adapter_id = "broken-search"
        adapter_version = "1.0.0"
        authorized = True

        def __init__(self, error: Exception, *, during_iteration: bool) -> None:
            self.error = error
            self.during_iteration = during_iteration

        def search(self, query: str):
            del query
            if not self.during_iteration:
                raise self.error

            def candidates():
                yield AgenticCandidate(
                    url="https://example.invalid/search.html",
                    discovery_kind="search",
                    discovered_from_url="https://example.invalid/results",
                )
                raise self.error

            return candidates()

    class BrokenCrawler:
        adapter_id = "broken-crawler"
        adapter_version = "1.0.0"

        def __init__(self, error: Exception, *, during_iteration: bool) -> None:
            self.error = error
            self.during_iteration = during_iteration

        def discover(self, **kwargs):
            if not self.during_iteration:
                raise self.error

            def candidates():
                yield AgenticCandidate(
                    url="https://example.invalid/child.html",
                    discovery_kind="crawler",
                    discovered_from_url=kwargs["final_url"],
                )
                raise self.error

            return candidates()

    def build(label: str, *, search=None, crawler=None) -> AgenticOrchestrator:
        storage = Storage(tmp_path / f"{label}.sqlite")
        store = ArtifactStore(storage, root=tmp_path / f"{label}-artifacts")
        gateway, _client = _offline_gateway(
            lambda request: httpx.Response(
                404 if request.url.path == "/robots.txt" else 200,
                headers={"content-type": "text/html"},
                    content=b"<html><body>crawler boundary fixture</body></html>",
                request=request,
            )
        )
        orchestrator = AgenticOrchestrator(
            storage=storage,
            prepared_authority=_prepared(store=store, gateway=gateway),
            crawler_adapter=crawler or HtmlLinkCrawlerAdapter(),
            search_adapter=search,
            clock=lambda: NOW,
        )
        orchestrator._test_client = _client
        return orchestrator

    search_rules_payload = _rules().model_dump(mode="json")
    search_rules_payload["scope"]["queries"] = [{"text": "bounded", "required": False}]
    search_rules = AgenticSiteRules.model_validate(search_rules_payload)
    for index, (error, during_iteration) in enumerate(
        (
            (ValueError("call"), False),
            (KeyError("iteration"), True),
            (AttributeError("iteration"), True),
        )
    ):
        orchestrator = build(
            f"search-{index}",
            search=BrokenSearch(error, during_iteration=during_iteration),
        )
        result = orchestrator.run(
            rules=search_rules, run_id=f"source-run-search-{index}"
        )
        search_task = next(task for task in result.tasks if task.kind == "search")
        assert search_task.failure_code == "search.adapter_error"
        orchestrator._test_client.close()
        orchestrator.storage.close()

    for index, (error, during_iteration) in enumerate(
        ((AttributeError("call"), False), (ValueError("iteration"), True))
    ):
        orchestrator = build(
            f"crawler-{index}",
            crawler=BrokenCrawler(error, during_iteration=during_iteration),
        )
        result = orchestrator.run(
            rules=_rules(), run_id=f"source-run-crawler-boundary-{index}"
        )
        assert "crawler.discovery_failed" in result.parent.warnings
        orchestrator._test_client.close()
        orchestrator.storage.close()

    scheduling = build(
        "scheduling",
        search=BrokenSearch(ValueError("unused"), during_iteration=True),
    )

    def repository_failure(**kwargs):
        del kwargs
        raise AgenticOrchestrationError("task.persist_failed")

    monkeypatch.setattr(scheduling, "_schedule_candidate", repository_failure)
    scheduling.search_adapter = type(
        "OneCandidateSearch",
        (),
        {
            "adapter_id": "one-candidate-search",
            "adapter_version": "1.0.0",
            "authorized": True,
            "search": lambda self, query: (
                AgenticCandidate(
                    url="https://example.invalid/search.html",
                    discovery_kind="search",
                    discovered_from_url="https://example.invalid/results",
                ),
            ),
        },
    )()
    scheduling._search_snapshot = scheduling._snapshot_adapter(
        scheduling.search_adapter, search=True
    )
    with pytest.raises(AgenticOrchestrationError, match=r"task\.persist_failed"):
        scheduling.run(rules=search_rules, run_id="source-run-scheduling-not-swallowed")
    scheduling._test_client.close()
    scheduling.storage.close()


def test_round5_v1_migration_validates_every_task_and_rejects_running_runs(
    tmp_path: Path,
) -> None:
    def downgrade(storage: Storage) -> None:
        for trigger_name in agentic._LEDGER_TRIGGER_NAMES:
            storage.conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        for table in ("agentic_runs", "agentic_tasks", "agentic_observations"):
            storage.conn.execute(
                f"UPDATE {table} SET schema_version = 'agentic-ledger.v1'"
            )
        storage.conn.execute("ALTER TABLE agentic_runs DROP COLUMN pages_used")
        storage.conn.execute("ALTER TABLE agentic_observations DROP COLUMN current_url")
        storage.conn.execute("ALTER TABLE agentic_observations DROP COLUMN status_code")
        storage.conn.execute("DELETE FROM agentic_ledger_schema")
        storage.conn.execute(
            "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
        )
        storage.conn.commit()

    for label, corrupt in (
        (
            "queued",
            lambda storage, task: storage.conn.execute(
                "UPDATE agentic_tasks SET task_ordinal = -1 WHERE task_id = ?",
                (task.task_id,),
            ),
        ),
        (
            "completed-missing-artifact",
            lambda storage, task: storage.conn.execute(
                """UPDATE agentic_tasks SET status = 'completed', finished_at = ?
                   WHERE task_id = ?""",
                ("2026-08-21T12:00:00Z", task.task_id),
            ),
        ),
        ("running-retry", lambda storage, task: None),
    ):
        storage = Storage(tmp_path / f"{label}.sqlite")
        repo = AgenticTaskRepository(storage, clock=lambda: NOW)
        repo.create_run(
            run_id=f"source-run-v1-{label}",
            rules=_rules(),
            authority=_legacy_authority(),
        )
        task = repo.create_task(
            run_id=f"source-run-v1-{label}",
            task_key="read:https://example.invalid/root.html",
            kind="read",
            required=True,
            requested_url="https://example.invalid/root.html",
            discovery_kind="seed",
        )
        repo.seal_required_tasks(f"source-run-v1-{label}")
        if label in {"completed-missing-artifact", "running-retry"}:
            task = repo.transition_task(task.task_id, status="running")
        if label == "running-retry":
            repo.add_observation(
                task=task,
                attempt=1,
                status="failed",
                current_url=task.requested_url,
                final_url=task.requested_url,
                status_code=None,
                access_decision_id="access-decision-0000000000000000",
                artifact_id=None,
                reason_code="gateway.transport.timeout",
                redirect_chain=(),
            )
        for trigger_name in agentic._LEDGER_TRIGGER_NAMES:
            storage.conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        corrupt(storage, task)
        storage.conn.commit()
        downgrade(storage)
        before = tuple(storage.conn.iterdump())
        with pytest.raises(
            AgenticOrchestrationError, match=r"ledger\.migration_invalid"
        ):
            AgenticTaskRepository(storage, clock=lambda: NOW)
        assert tuple(storage.conn.iterdump()) == before
        storage.close()


def test_round5_version_preflight_is_read_only_before_schema_work(
    tmp_path: Path,
) -> None:
    schema_actions = {
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_ALTER_TABLE,
    }

    def assert_read_only_rejection(storage: Storage) -> None:
        before = tuple(storage.conn.iterdump())
        actions: list[int] = []

        def authorize(action, arg1, arg2, database, source) -> int:
            del arg1, arg2, database, source
            actions.append(action)
            return sqlite3.SQLITE_OK

        storage.conn.set_authorizer(authorize)
        try:
            with pytest.raises(
                AgenticOrchestrationError, match=r"ledger\.version_unsupported"
            ):
                AgenticTaskRepository(storage, clock=lambda: NOW)
        finally:
            storage.conn.set_authorizer(None)
        assert not schema_actions.intersection(actions)
        assert tuple(storage.conn.iterdump()) == before

    future = Storage(tmp_path / "future-preflight.sqlite")
    AgenticTaskRepository(future, clock=lambda: NOW)
    future.conn.execute("DELETE FROM agentic_ledger_schema")
    future.conn.execute(
        "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v99', 99)"
    )
    future.conn.commit()
    assert_read_only_rejection(future)
    future.close()

    renamed = Storage(tmp_path / "renamed-preflight.sqlite")
    AgenticTaskRepository(renamed, clock=lambda: NOW)
    renamed.conn.execute(
        "ALTER TABLE agentic_ledger_schema RENAME TO renamed_ledger_schema"
    )
    renamed.conn.commit()
    assert_read_only_rejection(renamed)
    renamed.close()

    missing = Storage(tmp_path / "missing-column-preflight.sqlite")
    AgenticTaskRepository(missing, clock=lambda: NOW)
    for trigger_name in agentic._LEDGER_TRIGGER_NAMES:
        missing.conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
    missing.conn.execute("DROP INDEX idx_agentic_tasks_run")
    missing.conn.execute("ALTER TABLE agentic_tasks DROP COLUMN task_ordinal")
    missing.conn.commit()
    assert_read_only_rejection(missing)
    missing.close()


def test_round6_redirect_target_is_scoped_and_reserved_before_each_send(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        if request.url.path == "/reports/root.html":
            return httpx.Response(
                302,
                request=request,
                headers={"location": "/admin/secret.html"},
            )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>must not be sent</html>",
        )

    inputs = _compiler_inputs(
        seed_url="https://example.invalid/reports/root.html",
        allowed_page_prefixes=("/reports",),
        allowed_file_prefixes=("/reports",),
    )
    payload = _rules().model_dump(mode="json")
    payload["scope"]["seed_urls"] = [inputs.scope.seed_url]
    payload["scope"]["allow_patterns"] = ["https://example.invalid/reports/**"]
    rules = AgenticSiteRules.model_validate(payload)
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway, inputs=inputs),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=rules, run_id="source-run-redirect-scope")
    target_sends = [url for url in sent if not url.endswith("/robots.txt")]
    assert target_sends == ["https://example.invalid/reports/root.html"]
    assert result.observations[-1].reason_code == "gateway.origin"
    assert (
        result.observations[-1].current_url
        == "https://example.invalid/admin/secret.html"
    )
    assert result.parent.requests_used == 1
    client.close()
    storage.close()


def test_round6_prepared_call_routes_reject_instance_and_class_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    mock, client = _offline_gateway(
        lambda request: httpx.Response(404, request=request)
    )
    prepared = _prepared(store=store, gateway=mock)
    for owner, attribute in (
        (GovernedReadGateway, "read"),
        (MockClientReadGateway, "read"),
        (AccessGateway, "request"),
        (AccessGateway, "request_with_context"),
        (ArtifactStore, "store_observation"),
        (ArtifactStore, "get_observation"),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(owner, attribute, lambda *args, **kwargs: None)
            with pytest.raises(
                AgenticOrchestrationError,
                match=r"(?:gateway|artifact_store)\.seal_invalid",
            ):
                prepared.validate()
    store.store_observation = lambda **kwargs: None
    with pytest.raises(
        AgenticOrchestrationError, match=r"artifact_store\.seal_invalid"
    ):
        prepared.validate()
    del store.store_observation
    client.close()

    production_store = ArtifactStore(storage, root=tmp_path / "production-artifacts")
    transport = SafePinnedTransport(timeout=10.0, chunk_size=1024)
    production = _prepared(
        store=production_store,
        gateway=_production_gateway(transport),
    )
    transport._addresses = lambda *args: ["203.0.113.1"]
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        production.validate()
    del transport._addresses
    with monkeypatch.context() as scoped:
        scoped.setattr(SafePinnedTransport, "_addresses", lambda *args: [])
        with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
            production.validate()
    storage.close()


def test_round6_production_transport_limits_are_bound_to_reviewed_plan(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    for transport in (
        SafePinnedTransport(timeout=9.0, chunk_size=1024),
        SafePinnedTransport(timeout=10.0, chunk_size=0),
        SafePinnedTransport(timeout=10.0, chunk_size=8193),
    ):
        with pytest.raises(
            AgenticOrchestrationError, match=r"gateway\.authority_mismatch"
        ):
            _prepared(store=store, gateway=_production_gateway(transport))
    storage.close()


def test_round6_monotonic_lease_fence_blocks_expired_owner_mutations(
    tmp_path: Path,
) -> None:
    now = [NOW]
    storage = Storage(tmp_path / "db.sqlite")
    first = AgenticTaskRepository(storage, clock=lambda: now[0])
    first.create_run(
        run_id="source-run-fenced",
        rules=_rules(),
        authority=_legacy_authority(),
    )
    first_fence = first.acquire_run_lease(
        "source-run-fenced", owner="owner-first", ttl_seconds=1
    )
    assert type(first_fence) is int and first_fence == 1
    now[0] += timedelta(seconds=2)
    second = AgenticTaskRepository(storage, clock=lambda: now[0])
    second_fence = second.acquire_run_lease(
        "source-run-fenced", owner="owner-second", ttl_seconds=30
    )
    assert type(second_fence) is int and second_fence == 2
    with pytest.raises(AgenticOrchestrationError, match=r"run\.lease_lost"):
        first.begin_read_budget(
            "source-run-fenced",
            max_requests=4,
            max_bytes=4096,
            max_files=4,
            max_concurrency=1,
        )
    with pytest.raises(AgenticOrchestrationError, match=r"run\.lease_conflict"):
        first.release_run_lease("source-run-fenced", owner="owner-first")
    row = storage.conn.execute(
        "SELECT lease_owner, lease_epoch FROM agentic_runs WHERE run_id = ?",
        ("source-run-fenced",),
    ).fetchone()
    assert tuple(row) == ("owner-second", 2)
    second.release_run_lease("source-run-fenced", owner="owner-second")
    storage.close()


def test_round6_current_ledger_preflight_rejects_alien_exact_shape_read_only(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    AgenticTaskRepository(storage, clock=lambda: NOW)
    storage.conn.execute("CREATE INDEX idx_agentic_alien ON agentic_tasks(status)")
    storage.conn.commit()
    before = tuple(storage.conn.iterdump())
    with pytest.raises(AgenticOrchestrationError, match=r"ledger\.version_unsupported"):
        AgenticTaskRepository(storage, clock=lambda: NOW)
    assert tuple(storage.conn.iterdump()) == before
    storage.close()


def test_round6_transport_retryability_is_preserved_for_closed_kinds() -> None:
    for kind in ("connect", "remote_disconnected"):
        error = AccessGatewayTransportError(kind, "closed", retryable=True)
        reason, retryable, _decision = agentic._gateway_failure(error)
        assert reason == f"gateway.transport.{kind}"
        assert retryable is True


def test_round6_legacy_transport_retryability_infers_only_approved_kinds() -> None:
    approved = {
        "connect",
        "connect_or_http",
        "dns",
        "network",
        "remote_disconnected",
        "timeout",
    }
    assert all(
        AccessGatewayTransportError(kind, "closed").retryable for kind in approved
    )
    assert not AccessGatewayTransportError("tls_policy", "closed").retryable
    assert not AccessGatewayTransportError("credential_dump", "closed").retryable
    assert not AccessGatewayTransportError(
        "timeout", "closed", retryable=False
    ).retryable


def test_round6_unknown_transport_kind_maps_to_one_safe_reason() -> None:
    error = AccessGatewayTransportError(
        "credential_dump", "must never persist", retryable=True
    )
    reason, retryable, _decision = agentic._gateway_failure(error)
    assert reason == "gateway.transport.unclassified_transport"
    assert retryable is False
    assert "credential" not in reason


def test_round6_adapter_snapshots_reject_search_and_crawler_identity_drift(
    tmp_path: Path,
) -> None:
    class DriftingSearch:
        adapter_id = "drifting-search"
        adapter_version = "1.0.0"
        authorized = True

        def search(self, query: str):
            del query

            def candidates():
                self.adapter_id = "changed-search"
                yield AgenticCandidate(
                    url="https://example.invalid/search.html",
                    discovery_kind="search",
                    discovered_from_url="https://example.invalid/results",
                )

            return candidates()

    class DriftingCrawler:
        adapter_id = "drifting-crawler"
        adapter_version = "1.0.0"

        def discover(self, *, body, final_url, parent_artifact_id, depth):
            del body, parent_artifact_id, depth

            def candidates():
                self.adapter_version = "2.0.0"
                yield AgenticCandidate(
                    url="https://example.invalid/child.html",
                    discovery_kind="crawler",
                    discovered_from_url=final_url,
                )

            return candidates()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404 if request.url.path == "/robots.txt" else 200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>root</html>",
        )

    payload = _rules().model_dump(mode="json")
    payload["scope"]["queries"] = [{"text": "updates", "required": False}]
    search_rules = AgenticSiteRules.model_validate(payload)
    for label, crawler, search, rules in (
        ("search", HtmlLinkCrawlerAdapter(), DriftingSearch(), search_rules),
        ("crawler", DriftingCrawler(), None, _rules()),
    ):
        storage = Storage(tmp_path / f"{label}.sqlite")
        store = ArtifactStore(storage, root=tmp_path / f"{label}-artifacts")
        gateway, client = _offline_gateway(handler)
        result = AgenticOrchestrator(
            storage=storage,
            prepared_authority=_prepared(store=store, gateway=gateway),
            crawler_adapter=crawler,
            search_adapter=search,
            clock=lambda: NOW,
        ).run(rules=rules, run_id=f"source-run-adapter-drift-{label}")
        assert not any(
            task.requested_url and "child" in task.requested_url
            for task in result.tasks
        )
        assert not any(
            task.requested_url and "search" in task.requested_url
            for task in result.tasks
        )
        if label == "search":
            task = next(task for task in result.tasks if task.kind == "search")
            assert task.adapter_id == "drifting-search"
            assert task.failure_code == "search.adapter_error"
        else:
            assert "crawler.discovery_failed" in result.parent.warnings
        client.close()
    storage.close()


def test_round7_mock_robots_prepare_is_atomic_and_rebuilds_legacy_policy(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "failed.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "failed-artifacts")
    failed_requests: list[str] = []

    def failed_handler(request: httpx.Request) -> httpx.Response:
        failed_requests.append(request.url.path)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>legacy</html>",
        )

    failed_gateway, failed_client = _offline_gateway(failed_handler)
    failed_gateway.read("https://example.invalid/legacy")
    legacy_gateways = failed_gateway._gateways
    legacy_cache = dict(next(iter(legacy_gateways.values())).gateway._policy_cache)
    failed_gateway._max_body_bytes -= 1
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        _prepared(store=store, gateway=failed_gateway)
    assert (
        failed_gateway._transport._robots_mode
        is type(failed_gateway._transport)._LEGACY_ROBOTS_MODE
    )
    assert failed_gateway._prepared_origins is None
    assert failed_gateway._gateways is legacy_gateways
    assert next(iter(legacy_gateways.values())).gateway._policy_cache == legacy_cache
    failed_gateway.read("https://example.invalid/still-legacy")
    assert failed_requests == ["/legacy", "/still-legacy"]
    failed_client.close()
    storage.close()

    cases = (
        ("disallow", 200, b"User-agent: *\nDisallow: /\n"),
        ("unauthorized", 401, b""),
        ("forbidden", 403, b""),
    )
    for label, robots_status, robots_body in cases:
        case_storage = Storage(tmp_path / f"{label}.sqlite")
        case_store = ArtifactStore(case_storage, root=tmp_path / f"{label}-artifacts")
        requests: list[str] = []

        def handler(
            request: httpx.Request,
            *,
            status: int = robots_status,
            body: bytes = robots_body,
            seen: list[str] = requests,
        ) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    status,
                    request=request,
                    headers={"content-type": "text/plain; charset=utf-8"},
                    content=body,
                )
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/html"},
                content=b"<html>must not be sent</html>",
            )

        gateway, client = _offline_gateway(handler)
        gateway.read("https://example.invalid/legacy")
        requests.clear()
        prepared = _prepared(store=case_store, gateway=gateway)
        result = AgenticOrchestrator(
            storage=case_storage,
            prepared_authority=prepared,
            crawler_adapter=HtmlLinkCrawlerAdapter(),
            clock=lambda: NOW,
        ).run(rules=_rules(), run_id=f"source-run-round7-{label}")
        assert result.parent.status in {"rejected", "failed"}
        assert requests == ["/robots.txt"]
        client.close()
        case_storage.close()


def test_round7_expired_lease_and_send_claim_prevent_stale_transport(
    tmp_path: Path,
) -> None:
    now = [NOW]
    storage = Storage(tmp_path / "db.sqlite")
    first = AgenticTaskRepository(storage, clock=lambda: now[0])
    second = AgenticTaskRepository(storage, clock=lambda: now[0])

    def prepare_run(run_id: str) -> None:
        first.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
        assert first.acquire_run_lease(run_id, owner="owner-first", ttl_seconds=1)
        first.begin_read_budget(
            run_id,
            max_requests=4,
            max_bytes=4096,
            max_files=4,
            max_concurrency=1,
        )

    race_run = "source-run-round7-send-race"
    prepare_run(race_run)
    target_sends: list[str] = []
    takeover_fences: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        now[0] += timedelta(seconds=2)
        takeover_fences.append(
            second.acquire_run_lease(race_run, owner="owner-second", ttl_seconds=30)
        )
        target_sends.append(request.url.path)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>ok</html>",
        )

    gateway, client = _offline_gateway(handler)
    gateway._prepare_origins(("https://example.invalid",))

    def claim_send(_url: str, _decision: object):
        claim = first.claim_target_send(
            race_run,
            max_requests=4,
            timeout_seconds=10.0,
        )
        return lambda: first.release_target_send(claim)

    result = gateway.read(
        "https://example.invalid/root.html",
        before_target_request=claim_send,
    )
    assert result.status_code == 200
    assert target_sends == ["/root.html"]
    assert takeover_fences == [None]
    assert (
        second.acquire_run_lease(race_run, owner="owner-second", ttl_seconds=30) is None
    )
    now[0] = NOW + timedelta(seconds=11)
    successor_fence = second.acquire_run_lease(
        race_run, owner="owner-second", ttl_seconds=30
    )
    assert successor_fence == 2
    second.finish_read_budget(
        race_run,
        bytes_read=0,
        files=0,
        max_bytes=4096,
        max_files=4,
    )

    expired_run = "source-run-round7-expired"
    now[0] = NOW
    prepare_run(expired_run)
    now[0] += timedelta(seconds=2)
    with pytest.raises(AgenticOrchestrationError, match=r"run\.lease_lost"):
        first.claim_target_send(
            expired_run,
            max_requests=4,
            timeout_seconds=10.0,
        )
    assert target_sends == ["/root.html"]
    client.close()
    storage.close()


def test_round7_v1_preflight_accepts_only_known_exact_schema_subset(
    tmp_path: Path,
) -> None:
    def downgrade(storage: Storage) -> None:
        storage.conn.execute("DELETE FROM agentic_ledger_schema")
        storage.conn.execute(
            "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
        )
        storage.conn.commit()

    corruptions = {
        "column": lambda storage: storage.conn.execute(
            "ALTER TABLE agentic_tasks ADD COLUMN credential_dump TEXT"
        ),
        "index": lambda storage: storage.conn.execute(
            "CREATE INDEX foreign_index ON agentic_tasks(status)"
        ),
        "object": lambda storage: storage.conn.execute(
            "CREATE TABLE agentic_alien(secret TEXT)"
        ),
        "trigger": lambda storage: storage.conn.execute(
            """CREATE TRIGGER foreign_trigger AFTER INSERT ON agentic_tasks
               BEGIN SELECT 1; END"""
        ),
    }
    for label, corrupt in corruptions.items():
        storage = Storage(tmp_path / f"{label}.sqlite")
        AgenticTaskRepository(storage, clock=lambda: NOW)
        downgrade(storage)
        corrupt(storage)
        storage.conn.commit()
        before = tuple(storage.conn.iterdump())
        with pytest.raises(
            AgenticOrchestrationError, match=r"ledger\.version_unsupported"
        ):
            AgenticTaskRepository(storage, clock=lambda: NOW)
        assert tuple(storage.conn.iterdump()) == before
        storage.close()

    valid = Storage(tmp_path / "valid.sqlite")
    AgenticTaskRepository(valid, clock=lambda: NOW)
    downgrade(valid)
    AgenticTaskRepository(valid, clock=lambda: NOW)
    assert tuple(
        valid.conn.execute(
            "SELECT schema_name, version FROM agentic_ledger_schema"
        ).fetchone()
    ) == ("agentic-ledger.v2", 2)
    AgenticTaskRepository(valid, clock=lambda: NOW)
    valid.close()


def test_round8_send_claim_covers_lazy_body_close_redirect_and_is_idempotent(
    tmp_path: Path,
) -> None:
    now = [NOW]
    storage = Storage(tmp_path / "db.sqlite")
    first = AgenticTaskRepository(storage, clock=lambda: now[0])
    second = AgenticTaskRepository(storage, clock=lambda: now[0])
    takeover_attempts: list[tuple[str, int | None]] = []
    claims: dict[str, object] = {}

    def prepare_run(run_id: str) -> None:
        first.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
        assert first.acquire_run_lease(run_id, owner="owner-first", ttl_seconds=1)
        first.begin_read_budget(
            run_id,
            max_requests=4,
            max_bytes=4096,
            max_files=4,
            max_concurrency=1,
        )

    class ObservedStream(httpx.SyncByteStream):
        def __init__(
            self,
            run_id: str,
            *,
            body: bytes = b"<html>ok</html>",
            interrupt: bool = False,
            advance_during_close: bool = False,
        ) -> None:
            self.run_id = run_id
            self.body = body
            self.interrupt = interrupt
            self.advance_during_close = advance_during_close

        def __iter__(self):
            assert not storage.execution_transaction_active
            now[0] += timedelta(seconds=2)
            takeover_attempts.append(
                (
                    f"{self.run_id}:body",
                    second.acquire_run_lease(
                        self.run_id, owner="owner-second", ttl_seconds=30
                    ),
                )
            )
            if self.interrupt:
                raise KeyboardInterrupt()
            yield self.body

        def close(self) -> None:
            assert not storage.execution_transaction_active
            if self.advance_during_close:
                now[0] += timedelta(seconds=2)
            takeover_attempts.append(
                (
                    f"{self.run_id}:close",
                    second.acquire_run_lease(
                        self.run_id, owner="owner-second", ttl_seconds=30
                    ),
                )
            )

    streams: dict[str, ObservedStream] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        stream = streams[request.url.path]
        status = 302 if request.url.path == "/redirect" else 200
        headers = (
            {"location": "https://outside.invalid/next.html"}
            if status == 302
            else {"content-type": "text/html"}
        )
        return httpx.Response(status, request=request, headers=headers, stream=stream)

    gateway, client = _offline_gateway(handler)
    gateway._prepare_origins(("https://example.invalid",))

    def claim_callback(run_id: str):
        def claim(_url: str, _decision: object):
            token = first.claim_target_send(
                run_id,
                max_requests=4,
                timeout_seconds=10.0,
            )
            claims[run_id] = token
            return lambda: first.release_target_send(token)

        return claim

    successful_run = "source-run-round8-lazy"
    prepare_run(successful_run)
    streams["/lazy"] = ObservedStream(successful_run)
    assert (
        gateway.read(
            "https://example.invalid/lazy",
            before_target_request=claim_callback(successful_run),
        ).status_code
        == 200
    )
    first.release_target_send(claims[successful_run])
    first.finish_read_budget(
        successful_run,
        bytes_read=0,
        files=0,
        max_bytes=4096,
        max_files=4,
    )
    assert (
        second.acquire_run_lease(successful_run, owner="owner-second", ttl_seconds=30)
        is None
    )
    now[0] = NOW + timedelta(seconds=11)
    assert (
        second.acquire_run_lease(successful_run, owner="owner-second", ttl_seconds=30)
        == 2
    )

    interrupted_run = "source-run-round8-interrupted"
    now[0] = NOW
    prepare_run(interrupted_run)
    streams["/interrupted"] = ObservedStream(interrupted_run, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        gateway.read(
            "https://example.invalid/interrupted",
            before_target_request=claim_callback(interrupted_run),
        )
    first.release_target_send(claims[interrupted_run])
    assert (
        second.acquire_run_lease(interrupted_run, owner="owner-second", ttl_seconds=30)
        is None
    )
    now[0] = NOW + timedelta(seconds=11)
    assert (
        second.acquire_run_lease(interrupted_run, owner="owner-second", ttl_seconds=30)
        == 2
    )

    redirect_run = "source-run-round8-redirect"
    now[0] = NOW
    prepare_run(redirect_run)
    streams["/redirect"] = ObservedStream(redirect_run, advance_during_close=True)
    with pytest.raises(AccessGatewayOriginError):
        gateway.read(
            "https://example.invalid/redirect",
            before_target_request=claim_callback(redirect_run),
        )
    first.release_target_send(claims[redirect_run])
    assert (
        second.acquire_run_lease(redirect_run, owner="owner-second", ttl_seconds=30)
        is None
    )
    now[0] = NOW + timedelta(seconds=11)
    assert (
        second.acquire_run_lease(redirect_run, owner="owner-second", ttl_seconds=30)
        == 2
    )

    assert takeover_attempts == [
        (f"{successful_run}:body", None),
        (f"{successful_run}:close", None),
        (f"{interrupted_run}:body", None),
        (f"{interrupted_run}:close", None),
        (f"{redirect_run}:close", None),
    ]
    client.close()
    storage.close()


def test_round8_mock_transition_uses_one_sealed_atomic_state_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>ok</html>",
        )

    gateway, client = _offline_gateway(handler)
    gateway.read("https://example.invalid/legacy")
    requests.clear()
    lock = gateway._state_lock
    ready = threading.Barrier(2)
    result: list[int] = []

    def read_concurrently() -> None:
        ready.wait()
        result.append(gateway.read("https://example.invalid/concurrent").status_code)

    lock.acquire()
    reader = threading.Thread(target=read_concurrently)
    reader.start()
    ready.wait()
    reader.join(timeout=0.2)
    assert reader.is_alive()
    preparation = gateway._preview_origins(("https://example.invalid",))
    gateway._commit_origins(preparation)
    lock.release()
    reader.join(timeout=2)
    assert not reader.is_alive()
    assert result == [200]
    assert requests == ["/robots.txt", "/concurrent"]

    prepared = _prepared(store=store, gateway=gateway)
    gateway._state_lock = threading.RLock()
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    gateway._state_lock = lock
    original = MockClientReadGateway._gateway_for_origin
    with monkeypatch.context() as scoped:
        scoped.setattr(MockClientReadGateway, "_gateway_for_origin", lambda *args: None)
        with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
            prepared.validate()
    assert MockClientReadGateway._gateway_for_origin is original
    client.close()
    storage.close()


def test_round8_v1_preflight_binds_named_objects_and_validates_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def downgrade(storage: Storage) -> None:
        storage.conn.execute("DELETE FROM agentic_ledger_schema")
        storage.conn.execute(
            "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
        )
        storage.conn.commit()

    corruptions = {
        "trigger-owner": (
            "DROP TRIGGER guard_agentic_observations_insert",
            """CREATE TRIGGER guard_agentic_observations_insert
               BEFORE INSERT ON agentic_tasks BEGIN SELECT 1; END""",
        ),
        "index-owner": (
            "DROP INDEX idx_agentic_tasks_run",
            "CREATE INDEX idx_agentic_tasks_run ON agentic_runs(run_id)",
        ),
        "index-sql": (
            "DROP INDEX idx_agentic_tasks_run",
            """CREATE INDEX idx_agentic_tasks_run
               ON agentic_tasks(run_id, task_ordinal)
               WHERE run_id IS NOT NULL""",
        ),
    }
    for label, statements in corruptions.items():
        storage = Storage(tmp_path / f"{label}.sqlite")
        AgenticTaskRepository(storage, clock=lambda: NOW)
        downgrade(storage)
        for statement in statements:
            storage.conn.execute(statement)
        storage.conn.commit()
        before = tuple(storage.conn.iterdump())
        with pytest.raises(
            AgenticOrchestrationError, match=r"ledger\.version_unsupported"
        ):
            AgenticTaskRepository(storage, clock=lambda: NOW)
        assert tuple(storage.conn.iterdump()) == before
        storage.close()

    storage = Storage(tmp_path / "post-migration.sqlite")
    AgenticTaskRepository(storage, clock=lambda: NOW)
    downgrade(storage)
    before = tuple(storage.conn.iterdump())
    original = AgenticTaskRepository._create_ledger_guards

    def corrupt_after_guards(repository: AgenticTaskRepository) -> None:
        original(repository)
        repository.storage.conn.execute("DROP INDEX idx_agentic_tasks_run")
        repository.storage.conn.execute(
            "CREATE INDEX idx_agentic_tasks_run ON agentic_tasks(status)"
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            AgenticTaskRepository, "_create_ledger_guards", corrupt_after_guards
        )
        with pytest.raises(
            AgenticOrchestrationError, match=r"ledger\.version_unsupported"
        ):
            AgenticTaskRepository(storage, clock=lambda: NOW)
    assert tuple(storage.conn.iterdump()) == before
    AgenticTaskRepository(storage, clock=lambda: NOW)
    AgenticTaskRepository(storage, clock=lambda: NOW)
    storage.close()


def test_round9_gateway_call_graph_is_sealed_again_at_the_send_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html>ok</html>",
        )

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    prepared = _prepared(store=store, gateway=gateway)
    inner = gateway.gateway
    for owner, attribute in (
        (AccessGateway, "_policy_for"),
        (AccessGateway, "_authorize_request"),
        (AccessGateway, "_gate_origin"),
        (access_gateway_module, "_request_transport"),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(owner, attribute, lambda *args, **kwargs: None)
            with pytest.raises(
                AgenticOrchestrationError, match=r"gateway\.seal_invalid"
            ):
                prepared.validate()
            assert requests == []

    for owner, attribute in (
        (AccessGateway, "_authorize_request"),
        (access_gateway_module, "_request_transport"),
    ):
        target_count = len([path for path in requests if path != "/robots.txt"])
        with monkeypatch.context() as scoped:

            def mutate_after_authorization(
                _url: str,
                _decision: object,
                *,
                sealed_owner=owner,
                sealed_attribute=attribute,
            ):
                scoped.setattr(
                    sealed_owner,
                    sealed_attribute,
                    lambda *args, **kwargs: None,
                )

            with pytest.raises(AccessGatewayTransportError, match="call graph changed"):
                inner.request_with_context(
                    "https://example.invalid/root.html",
                    consume=lambda raw, _context: b"".join(raw.body_chunks),
                    before_target_request=mutate_after_authorization,
                )
        assert len([path for path in requests if path != "/robots.txt"]) == target_count
    client.close()
    storage.close()


def test_round9_send_claim_renews_through_trickle_and_stops_on_fence_loss(
    tmp_path: Path,
) -> None:
    now = [NOW]
    storage = Storage(tmp_path / "db.sqlite")
    first = AgenticTaskRepository(storage, clock=lambda: now[0])
    second = AgenticTaskRepository(storage, clock=lambda: now[0])
    takeover_attempts: list[int | None] = []
    streams: dict[str, httpx.SyncByteStream] = {}

    def prepare_run(run_id: str) -> None:
        first.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
        assert first.acquire_run_lease(run_id, owner="owner-first", ttl_seconds=1)
        first.begin_read_budget(
            run_id,
            max_requests=4,
            max_bytes=4096,
            max_files=4,
            max_concurrency=1,
        )

    class TrickleStream(httpx.SyncByteStream):
        def __init__(self, run_id: str, *, lose_fence: bool = False) -> None:
            self.run_id = run_id
            self.lose_fence = lose_fence
            self.closed = False

        def __iter__(self):
            increments = (11,) if self.lose_fence else (6, 6, 6)
            for increment in increments:
                now[0] += timedelta(seconds=increment)
                takeover_attempts.append(
                    second.acquire_run_lease(
                        self.run_id, owner="owner-second", ttl_seconds=30
                    )
                )
                yield b"x"

        def close(self) -> None:
            self.closed = True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            stream=streams[request.url.path],
        )

    gateway, client = _offline_gateway(handler)
    gateway._prepare_origins(("https://example.invalid",))
    guards: dict[str, object] = {}

    def claim_callback(run_id: str):
        def claim(_url: str, _decision: object):
            guard = agentic._TargetSendLease(
                first,
                first.claim_target_send(
                    run_id,
                    max_requests=4,
                    timeout_seconds=10.0,
                ),
                timeout_seconds=10.0,
            )
            guards[run_id] = guard
            return guard

        return claim

    successful_run = "source-run-round9-trickle"
    prepare_run(successful_run)
    successful_stream = TrickleStream(successful_run)
    streams["/trickle"] = successful_stream
    result = gateway.read(
        "https://example.invalid/trickle",
        before_target_request=claim_callback(successful_run),
    )
    assert result.body == b"xxx"
    assert successful_stream.closed
    assert takeover_attempts == [None, None, None]
    guards[successful_run]()
    first.finish_read_budget(
        successful_run,
        bytes_read=3,
        files=0,
        max_bytes=4096,
        max_files=4,
    )
    assert (
        second.acquire_run_lease(successful_run, owner="owner-second", ttl_seconds=30)
        is None
    )
    expiry = storage.conn.execute(
        "SELECT lease_expires_at FROM agentic_runs WHERE run_id = ?",
        (successful_run,),
    ).fetchone()[0]
    now[0] = datetime.fromisoformat(expiry)
    assert (
        second.acquire_run_lease(successful_run, owner="owner-second", ttl_seconds=30)
        == 2
    )

    failed_run = "source-run-round9-renew-failed"
    now[0] = NOW
    prepare_run(failed_run)
    failed_stream = TrickleStream(failed_run, lose_fence=True)
    streams["/renew-failed"] = failed_stream
    with pytest.raises(BodyFailure, match="unclassified_body_failure"):
        gateway.read(
            "https://example.invalid/renew-failed",
            before_target_request=claim_callback(failed_run),
        )
    assert failed_stream.closed
    assert takeover_attempts[-1] == 2
    client.close()
    storage.close()


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("body", BodyFailure),
        ("redirect-close", KeyboardInterrupt),
        ("cancel", asyncio.CancelledError),
        ("base", KeyboardInterrupt),
    ),
)
def test_round9_release_cleanup_never_masks_a_primary_failure(
    mode: str,
    expected: type[BaseException],
) -> None:
    class FaultStream(httpx.SyncByteStream):
        def __iter__(self):
            if mode == "cancel":
                raise asyncio.CancelledError()
            if mode == "base":
                raise KeyboardInterrupt()
            yield b"xx" if mode == "body" else b""

        def close(self) -> None:
            if mode == "redirect-close":
                raise KeyboardInterrupt()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            302 if mode == "redirect-close" else 200,
            request=request,
            headers=(
                {"location": "/next"}
                if mode == "redirect-close"
                else {"content-type": "text/html"}
            ),
            stream=FaultStream(),
        )

    def failing_release(_url: str, _decision: object):
        def release() -> None:
            raise RuntimeError("release cleanup failed")

        return release

    gateway, client = _offline_gateway(handler)
    gateway._prepare_origins(("https://example.invalid",))
    with pytest.raises(expected) as captured:
        gateway.read(
            "https://example.invalid/root.html",
            max_body_bytes=1,
            before_target_request=failing_release,
        )
    assert any(
        "target-send release failed" in note
        for note in getattr(captured.value, "__notes__", ())
    )
    assert isinstance(captured.value.__context__, RuntimeError)
    client.close()

    success, success_client = _offline_gateway(
        lambda request: httpx.Response(
            404 if request.url.path == "/robots.txt" else 200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"x",
        )
    )
    success._prepare_origins(("https://example.invalid",))
    with pytest.raises(RuntimeError, match="release cleanup failed"):
        success.read(
            "https://example.invalid/root.html",
            before_target_request=failing_release,
        )
    success_client.close()


def test_round9_ledger_preflight_rejects_noncanonical_table_semantics_read_only(
    tmp_path: Path,
) -> None:
    variants = {
        "check": ", CHECK (requests_used >= 0))",
        "foreign-key": (
            ", FOREIGN KEY (replay_of_run_id) REFERENCES agentic_runs(run_id))"
        ),
        "generated": ", hidden_probe TEXT GENERATED ALWAYS AS ('x') VIRTUAL)",
        "strict": ") STRICT",
        "without-rowid": ") WITHOUT ROWID",
    }

    def rewrite_table_sql(storage: Storage, suffix: str) -> None:
        original = storage.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agentic_runs'"
        ).fetchone()[0]
        replacement = original.rstrip()[:-1] + suffix
        storage.conn.execute("PRAGMA writable_schema = ON")
        storage.conn.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = 'agentic_runs'",
            (replacement,),
        )
        storage.conn.execute("PRAGMA writable_schema = OFF")
        schema_version = storage.conn.execute("PRAGMA schema_version").fetchone()[0]
        storage.conn.execute(f"PRAGMA schema_version = {schema_version + 1}")
        storage.conn.commit()

    for legacy in (False, True):
        for label, suffix in variants.items():
            path = tmp_path / f"{'v1' if legacy else 'v2'}-{label}.sqlite"
            storage = Storage(path)
            AgenticTaskRepository(storage, clock=lambda: NOW)
            if legacy:
                storage.conn.execute("DELETE FROM agentic_ledger_schema")
                storage.conn.execute(
                    "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
                )
            rewrite_table_sql(storage, suffix)
            storage.close()
            reopened = Storage(path)
            before = tuple(reopened.conn.iterdump())
            with pytest.raises(
                AgenticOrchestrationError, match=r"ledger\.version_unsupported"
            ):
                AgenticTaskRepository(reopened, clock=lambda: NOW)
            assert tuple(reopened.conn.iterdump()) == before
            reopened.close()

    valid_path = tmp_path / "valid-v1.sqlite"
    valid = Storage(valid_path)
    AgenticTaskRepository(valid, clock=lambda: NOW)
    valid.conn.execute("DELETE FROM agentic_ledger_schema")
    valid.conn.execute(
        "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
    )
    valid.conn.commit()
    AgenticTaskRepository(valid, clock=lambda: NOW)
    valid.close()
    reopened = Storage(valid_path)
    AgenticTaskRepository(reopened, clock=lambda: NOW)
    reopened.close()


@pytest.mark.parametrize(
    "alias_name",
    (
        "_FROZEN_ACCESS_GATEWAY_REQUEST_WITH_CONTEXT",
        "_FROZEN_ACCESS_CACHE_KEY",
        "_FROZEN_ACCESS_NORMALIZE_AND_GATE",
        "_FROZEN_ACCESS_GATE_ORIGIN",
        "_FROZEN_ACCESS_POLICY_FOR",
        "_FROZEN_ACCESS_FETCH_POLICY",
        "_FROZEN_ACCESS_AUTHORIZE_REQUEST",
        "_FROZEN_ACCESS_CAUSAL_NOW",
        "_FROZEN_ACCESS_FRESH_POLICY_TIME",
        "_FROZEN_ACCESS_SEAL_RUNTIME",
        "_FROZEN_ACCESS_VALIDATE_RUNTIME",
        "_FROZEN_SAFE_PINNED_REQUEST",
        "_FROZEN_SAFE_PINNED_ADDRESSES",
        "_FROZEN_REQUEST_TRANSPORT",
    ),
)
def test_round9_actual_gateway_execution_aliases_are_sealed_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_name: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    prepared = _prepared(store=store, gateway=gateway)
    monkeypatch.setattr(
        access_gateway_module,
        alias_name,
        lambda *args, **kwargs: None,
    )
    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    assert requests == []
    client.close()
    storage.close()


@pytest.mark.parametrize("mutation", ("config", "transport"))
def test_round9_runtime_snapshot_blocks_response_time_authority_swap_before_more_io(
    tmp_path: Path,
    mutation: str,
) -> None:
    requests: list[str] = []
    replacement_requests: list[str] = []
    inner: AccessGateway | None = None

    class ReplacementTransport:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            replacement_requests.append(url)
            return RawHttpResponse(status=404, headers={}, body_chunks=())

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inner
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        assert inner is not None
        inner.config = replace(
            inner.config,
            allowed_origins=frozenset(
                {
                    *inner.config.allowed_origins,
                    normalize_http_url("https://other.invalid/root.html")[1],
                }
            ),
        )
        if mutation == "transport":
            inner.transport = ReplacementTransport()
        return httpx.Response(
            302,
            headers={"location": "https://other.invalid/next.html"},
            request=request,
        )

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    _prepared(store=store, gateway=gateway)
    inner = gateway.gateway
    with pytest.raises(AccessGatewayTransportError, match="call graph changed"):
        inner.request_with_context(
            "https://example.invalid/root.html",
            consume=lambda raw, _context: b"".join(raw.body_chunks),
        )
    assert requests == [
        "https://example.invalid/robots.txt",
        "https://example.invalid/root.html",
    ]
    assert replacement_requests == []
    client.close()
    storage.close()


@pytest.mark.parametrize(
    "mutation",
    ("wrapper-request", "client-transport", "handler"),
)
def test_round9_mock_response_time_transport_capability_swap_stops_before_next_io(
    tmp_path: Path,
    mutation: str,
) -> None:
    requests: list[str] = []
    replacement_requests: list[str] = []
    offline: MockClientReadGateway | None = None
    client: httpx.Client | None = None

    class ReplacementTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            replacement_requests.append(str(request.url))
            return httpx.Response(200, request=request, content=b"replacement")

    def replacement_request(url: str, **kwargs) -> RawHttpResponse:
        del kwargs
        replacement_requests.append(url)
        return RawHttpResponse(status=200, headers={}, body_chunks=(b"evil",))

    def replacement_handler(request: httpx.Request) -> httpx.Response:
        replacement_requests.append(str(request.url))
        return httpx.Response(200, request=request, content=b"replacement")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal client, offline
        requests.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        assert client is not None
        assert offline is not None
        if mutation == "wrapper-request":
            offline._transport.request = replacement_request
        elif mutation == "client-transport":
            client._transport = ReplacementTransport()
        else:
            setattr(
                client._transport,
                offline._transport._handler_attribute,
                replacement_handler,
            )
        return httpx.Response(
            302,
            request=request,
            headers={"location": "/next.html"},
        )

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    offline, client = _offline_gateway(handler)
    original_transport = client._transport
    original_handler = getattr(
        original_transport, offline._transport._handler_attribute
    )
    _prepared(store=store, gateway=offline)
    with pytest.raises(AccessGatewayTransportError) as raised:
        offline.gateway.request_with_context(
            "https://example.invalid/root.html",
            consume=lambda raw, _context: b"".join(raw.body_chunks),
        )
    assert raised.value.kind == "transport_integrity"
    assert requests == [
        "https://example.invalid/robots.txt",
        "https://example.invalid/root.html",
    ]
    assert replacement_requests == []
    if "request" in vars(offline._transport):
        del offline._transport.request
    client._transport = original_transport
    setattr(
        original_transport,
        offline._transport._handler_attribute,
        original_handler,
    )
    client.close()
    storage.close()


@pytest.mark.parametrize(
    "attribute",
    (
        "storage",
        "prepared_authority",
        "artifact_store",
        "read_gateway",
        "resolved_site_skill",
        "execution_plan",
        "authority",
    ),
)
def test_round10_orchestrator_exposes_only_one_fixed_prepared_capability(
    tmp_path: Path,
    attribute: str,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    prepared = _prepared(store=store, gateway=gateway)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )

    with pytest.raises(AttributeError):
        setattr(orchestrator, attribute, object())

    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    storage.close()


@pytest.mark.parametrize(
    "helper_name",
    (
        "access_policy_cache_key_sha256",
        "build_access_policy",
        "build_origin_policy_evidence",
        "read_bounded_body",
        "parse_content_type",
        "decode_robots_utf8",
        "looks_like_html",
        "parse_robots",
    ),
)
def test_round10_access_policy_helper_graph_is_sealed_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    prepared = _prepared(store=store, gateway=gateway)
    monkeypatch.setattr(
        access_gateway_module, helper_name, lambda *args, **kwargs: None
    )

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()

    assert requests == []
    client.close()
    storage.close()


@pytest.mark.parametrize(
    "helper_name",
    (
        "read_bounded_body",
        "_attach_body_failure_context",
        "_content_disposition_filename",
        "_response_character_encoding",
        "_FROZEN_ACCESS_REQUEST_WITH_CONTEXT",
    ),
)
def test_round10_governed_read_helper_graph_is_sealed_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    prepared = _prepared(store=store, gateway=gateway)
    monkeypatch.setattr(governed_read_module, helper_name, lambda *args, **kwargs: None)

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()

    assert requests == []
    client.close()
    storage.close()


@pytest.mark.parametrize("attribute", ("gateway", "max_body_bytes"))
def test_round10_governed_reader_authority_cannot_be_reassigned(
    attribute: str,
) -> None:
    gateway, client = _offline_gateway(
        lambda request: httpx.Response(404, request=request)
    )
    gateway._prepare_origins(("https://example.invalid",))
    governed = next(iter(gateway._gateways.values()))
    with pytest.raises(AttributeError):
        setattr(governed, attribute, object())
    client.close()


def test_round10_governed_helper_swap_during_transport_stops_before_body_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_calls: list[str] = []

    def evil_helper(*args, **kwargs):
        del args, kwargs
        helper_calls.append("evil")
        raise AssertionError("mutable helper executed")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        monkeypatch.setattr(governed_read_module, "read_bounded_body", evil_helper)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"ok",
        )

    gateway, client = _offline_gateway(handler)
    gateway._prepare_origins(("https://example.invalid",))
    gateway.gateway._seal_runtime()
    with pytest.raises(AccessGatewayTransportError) as raised:
        gateway.read("https://example.invalid/root.html")
    assert raised.value.kind == "transport_integrity"
    assert helper_calls == []
    client.close()


def test_round10_ledger_times_are_fixed_width_and_legacy_v2_rows_normalize(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    storage = Storage(path)
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    repo.create_run(
        run_id="source-run-canonical-time",
        rules=_rules(),
        authority=_legacy_authority(),
    )
    for trigger_name in agentic._LEDGER_TRIGGER_NAMES:
        storage.conn.execute(f"DROP TRIGGER {trigger_name}")
    storage.conn.execute(
        "UPDATE agentic_runs SET created_at = ? WHERE run_id = ?",
        ("2026-08-21T12:00:00Z", "source-run-canonical-time"),
    )
    repo._create_ledger_guards()
    storage.conn.commit()
    storage.close()

    reopened = Storage(path)
    AgenticTaskRepository(reopened, clock=lambda: NOW)
    row = reopened.conn.execute(
        "SELECT created_at FROM agentic_runs WHERE run_id = ?",
        ("source-run-canonical-time",),
    ).fetchone()
    assert row["created_at"] == "2026-08-21T12:00:00.000000Z"
    reopened.close()


@pytest.mark.parametrize(
    ("started_at", "moved_at"),
    (
        (NOW, NOW + timedelta(microseconds=500_000)),
        (NOW + timedelta(microseconds=500_000), NOW + timedelta(seconds=1)),
    ),
)
def test_round10_same_second_lease_and_send_times_compare_in_both_directions(
    tmp_path: Path,
    started_at: datetime,
    moved_at: datetime,
) -> None:
    current = [started_at]
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: current[0])
    run_id = f"source-run-time-{started_at.microsecond}"
    repo.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
    assert repo.acquire_run_lease(run_id, owner="owner-time", ttl_seconds=2) == 1
    repo.begin_read_budget(
        run_id,
        max_requests=4,
        max_bytes=4096,
        max_files=4,
        max_concurrency=1,
    )
    current[0] = moved_at
    assert repo.acquire_run_lease(run_id, owner="owner-time", ttl_seconds=2) == 2
    claim = repo.claim_target_send(
        run_id,
        max_requests=4,
        timeout_seconds=2,
    )
    current[0] += timedelta(microseconds=250_000)
    claim = repo.renew_target_send(claim, timeout_seconds=2)
    repo.release_target_send(claim)
    repo.finish_read_budget(
        run_id,
        bytes_read=1,
        files=0,
        max_bytes=4096,
        max_files=4,
    )
    values = storage.conn.execute(
        "SELECT created_at, lease_expires_at FROM agentic_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert values["created_at"].endswith(f".{started_at.microsecond:06d}Z")
    assert values["lease_expires_at"].endswith("Z")
    assert len(values["lease_expires_at"].rsplit(":", 1)[1]) == 10
    repo.release_run_lease(run_id, owner="owner-time")
    storage.close()


def test_round10_ledger_guards_freeze_identifiers_and_terminal_lease_epoch(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    run_id = "source-run-immutable-columns"
    repo.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
    task = repo.create_task(
        run_id=run_id,
        task_key="search:immutable",
        kind="search",
        required=True,
        query="immutable",
        discovery_kind="search",
    )
    repo.seal_required_tasks(run_id)

    with pytest.raises(sqlite3.DatabaseError):
        storage.conn.execute(
            "UPDATE agentic_runs SET run_id = ? WHERE run_id = ?",
            ("source-run-mutated", run_id),
        )
    with pytest.raises(sqlite3.DatabaseError):
        storage.conn.execute(
            "UPDATE agentic_tasks SET task_id = ? WHERE task_id = ?",
            ("child-task-mutated", task.task_id),
        )

    repo.transition_task(task.task_id, status="running")
    repo.transition_task(task.task_id, status="completed")
    repo.finalize_run(run_id, requested_status="completed", warnings=())
    with pytest.raises(sqlite3.DatabaseError):
        storage.conn.execute(
            "UPDATE agentic_runs SET lease_epoch = lease_epoch + 1 WHERE run_id = ?",
            (run_id,),
        )
    storage.close()


def test_round10_v1_same_name_noncanonical_trigger_is_rejected_read_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    storage = Storage(path)
    AgenticTaskRepository(storage, clock=lambda: NOW)
    storage.conn.execute("DELETE FROM agentic_ledger_schema")
    storage.conn.execute(
        "INSERT INTO agentic_ledger_schema VALUES ('agentic-ledger.v1', 1)"
    )
    storage.conn.execute("DROP TRIGGER guard_agentic_runs_delete")
    storage.conn.execute(
        """CREATE TRIGGER guard_agentic_runs_delete
           BEFORE DELETE ON agentic_runs BEGIN SELECT 1; END"""
    )
    storage.conn.commit()
    before = tuple(storage.conn.iterdump())

    with pytest.raises(AgenticOrchestrationError, match=r"ledger\.version_unsupported"):
        AgenticTaskRepository(storage, clock=lambda: NOW)

    assert tuple(storage.conn.iterdump()) == before
    storage.close()


@pytest.mark.parametrize(
    "mutation",
    ("subclass", "instance-method", "class-method", "prepared-predicate"),
)
def test_round11_rules_and_prepared_predicates_are_exact_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    prepared = _prepared(store=store, gateway=gateway)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    rules = _rules()
    if mutation == "subclass":

        class DerivedRules(AgenticSiteRules):
            pass

        rules = DerivedRules.model_validate(rules.model_dump(mode="json"))
    elif mutation == "instance-method":
        object.__setattr__(rules, "matches", lambda _url: True)
    elif mutation == "class-method":
        monkeypatch.setattr(AgenticSiteRules, "matches", lambda self, url: True)
    else:
        monkeypatch.setattr(
            agentic.PreparedAgenticAuthority,
            "contains_url",
            lambda self, url: True,
        )

    with pytest.raises(
        AgenticOrchestrationError,
        match=r"(?:rules|authority)\.seal_invalid",
    ):
        orchestrator.run(rules=rules, run_id=f"source-run-rule-{mutation}")

    assert requests == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    client.close()
    storage.close()


def test_round11_original_rules_snapshot_is_rechecked_before_policy_io(
    tmp_path: Path,
) -> None:
    requests: list[str] = []
    rules = _rules()
    mutated = False

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    def mutate_rules() -> bool:
        nonlocal mutated
        if not mutated:
            mutated = True
            object.__setattr__(
                rules,
                "scope",
                rules.scope.model_copy(update={"allow_patterns": ("/blocked/**",)}),
            )
        return False

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        cancel_requested=mutate_rules,
        clock=lambda: NOW,
    )

    with pytest.raises(AgenticOrchestrationError, match=r"rules\.seal_invalid"):
        orchestrator.run(rules=rules, run_id="source-run-rule-midflight")

    assert requests == []
    assert (
        orchestrator.repository.require_run("source-run-rule-midflight").status
        == "failed"
    )
    client.close()
    storage.close()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_is_public",
        "is_public_address",
        "_FROZEN_SAFE_PINNED_ADDRESSES",
        "_normalize_host",
        "_normalize_percent_path",
        "normalize_http_url",
        "canonical_host_header",
    ),
)
def test_round11_safe_pinned_helper_drift_is_rejected_without_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    resolver_calls: list[str] = []
    connection_calls: list[str] = []
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        site_diagnostic_module.socket,
        "getaddrinfo",
        lambda *args, **kwargs: resolver_calls.append("resolver"),
    )
    monkeypatch.setattr(
        site_diagnostic_module.socket,
        "create_connection",
        lambda *args, **kwargs: connection_calls.append("connect"),
    )
    monkeypatch.setattr(
        site_diagnostic_module,
        helper_name,
        lambda *args, **kwargs: True,
    )

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()

    assert resolver_calls == []
    assert connection_calls == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    storage.close()


def test_round11_observation_reloads_and_exactly_binds_the_durable_task(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    storage = Storage(path)
    repo = AgenticTaskRepository(storage, clock=lambda: NOW)
    run_id = "source-run-observation-task-binding"
    repo.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
    task = repo.create_task(
        run_id=run_id,
        task_key="read:https://example.invalid/root.html",
        kind="read",
        required=True,
        requested_url="https://example.invalid/root.html",
        depth=0,
        discovery_kind="seed",
        adapter_id="web_http",
        adapter_version="1.0.0",
    )
    task = repo.transition_task(task.task_id, status="running")
    forged = replace(
        task,
        requested_url="https://example.invalid/forged.html",
        task_key="read:https://example.invalid/forged.html",
    )
    storage.conn.commit()
    before_dump = tuple(storage.conn.iterdump())
    before_bytes = path.read_bytes()

    with pytest.raises(AgenticOrchestrationError, match=r"observation\.task_conflict"):
        repo.add_observation(
            task=forged,
            attempt=1,
            status="rejected",
            current_url=forged.requested_url,
            final_url=forged.requested_url,
            status_code=None,
            access_decision_id=None,
            artifact_id=None,
            reason_code="budget.requests_exhausted",
            redirect_chain=(),
        )

    assert tuple(storage.conn.iterdump()) == before_dump
    assert path.read_bytes() == before_bytes
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM agentic_observations").fetchone()[0]
        == 0
    )
    storage.close()


@pytest.mark.parametrize(
    "helper_name",
    (
        "fnmatchcase",
        "canonicalize_access_url",
        "_url_origin",
        "urlsplit",
        "_path_within_prefix",
    ),
)
def test_round12_rule_predicate_helper_drift_is_pre_effect_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    rules = _rules()
    monkeypatch.setattr(agentic, helper_name, lambda *args, **kwargs: True)

    with pytest.raises(AgenticOrchestrationError, match=r"authority\.seal_invalid"):
        orchestrator.run(
            rules=rules, run_id=f"source-run-rule-helper-{helper_name.strip('_')}"
        )

    assert requests == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    client.close()
    storage.close()


def test_round12_bound_rules_rechecks_a_helper_mutated_after_entry_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    original_validate = agentic._AgenticRunSnapshot.validate
    validations = 0

    def mutate_after_entry(snapshot) -> None:
        nonlocal validations
        validations += 1
        original_validate(snapshot)
        if validations == 2:
            monkeypatch.setattr(
                agentic,
                "canonicalize_access_url",
                lambda value: value,
            )

    monkeypatch.setattr(
        agentic._AgenticRunSnapshot,
        "validate",
        mutate_after_entry,
    )

    with pytest.raises(AgenticOrchestrationError, match=r"rules\.seal_invalid"):
        orchestrator.run(
            rules=_rules(),
            run_id="source-run-bound-helper-race",
        )

    assert validations == 0
    assert requests == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    client.close()
    storage.close()


@pytest.mark.parametrize(
    ("owner_name", "helper_name"),
    (
        ("socket", "getaddrinfo"),
        ("socket", "create_connection"),
        ("ssl", "create_default_context"),
        ("http", "HTTPConnection"),
        ("http", "HTTPSConnection"),
        ("module", "validate_domain"),
        ("module", "_remove_dot_segments"),
        ("module", "quote"),
        ("module", "urlunsplit"),
    ),
)
def test_round12_safe_pinned_complete_dispatch_drift_precedes_dns_and_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_name: str,
    helper_name: str,
) -> None:
    resolver_calls: list[str] = []
    connection_calls: list[str] = []
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    owner = {
        "socket": site_diagnostic_module.socket,
        "ssl": site_diagnostic_module.ssl,
        "http": site_diagnostic_module.http.client,
        "module": site_diagnostic_module,
    }[owner_name]
    monkeypatch.setattr(owner, helper_name, lambda *args, **kwargs: True)

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()

    assert resolver_calls == []
    assert connection_calls == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    storage.close()


def test_round12_target_claim_callback_never_runs_under_origin_lock_and_expiry_stops_send() -> (
    None
):
    class ManualClock:
        def __init__(self) -> None:
            self.value = NOW

        def __call__(self) -> datetime:
            return self.value

    class HtmlTransport:
        def __init__(self) -> None:
            self.targets = 0

        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            self.targets += 1
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    clock = ManualClock()
    transport = HtmlTransport()
    facade = _context_gateway(
        transport,
        clock=clock,
        policy_ttl=timedelta(seconds=5),
    )
    gateway = facade.gateway
    origin_lock = next(iter(gateway._origin_states.values())).lock

    def expire_after_claim(url: str, decision: object):
        del url, decision
        assert not origin_lock.locked()
        clock.value += timedelta(seconds=6)

    with pytest.raises(AccessGatewayPolicyError):
        gateway.request_with_context(
            "https://example.invalid/root.html",
            consume=lambda raw, context: (raw, context),
            before_target_request=expire_after_claim,
        )
    assert transport.targets == 0
    facade._test_client.close()


def test_round12_origin_and_storage_lock_order_is_deadlock_free(
    tmp_path: Path,
) -> None:
    class HtmlTransport:
        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    storage = Storage(tmp_path / "locks.sqlite")
    repository = AgenticTaskRepository(storage, clock=lambda: NOW)
    facade = _context_gateway(HtmlTransport())
    gateway = facade.gateway
    callback_entered = threading.Event()
    storage_owned = threading.Event()
    errors: list[BaseException] = []

    def consume(raw: RawHttpResponse, context: object) -> bytes:
        del context
        return b"".join(raw.body_chunks)

    def claim_after_storage_owner_starts(url: str, decision: object):
        del url, decision
        callback_entered.set()
        assert storage_owned.wait(2)
        with repository.transaction():
            pass

    def first_request() -> None:
        try:
            gateway.request_with_context(
                "https://example.invalid/first.html",
                consume=consume,
                before_target_request=claim_after_storage_owner_starts,
            )
        except BaseException as exc:  # noqa: BLE001 - thread evidence is asserted below.
            errors.append(exc)

    def storage_then_second_request() -> None:
        try:
            assert callback_entered.wait(2)
            with repository.transaction():
                storage_owned.set()
                gateway.request_with_context(
                    "https://example.invalid/second.html",
                    consume=consume,
                )
        except BaseException as exc:  # noqa: BLE001 - thread evidence is asserted below.
            errors.append(exc)

    first = threading.Thread(target=first_request)
    second = threading.Thread(target=storage_then_second_request)
    first.start()
    second.start()
    first.join(3)
    second.join(3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    facade._test_client.close()
    storage.close()


@pytest.mark.parametrize("pointer", ("repository", "prepared"))
def test_round12_run_uses_one_fixed_execution_snapshot_without_mixed_lineage(
    tmp_path: Path,
    pointer: str,
) -> None:
    storage = Storage(tmp_path / "primary.sqlite")
    alternate_storage = Storage(tmp_path / "alternate.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    alternate_store = ArtifactStore(
        alternate_storage, root=tmp_path / "alternate-artifacts"
    )
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    alternate_gateway = _gateway(gzip.compress(b"<html>other</html>", mtime=0))
    prepared = _prepared(store=store, gateway=gateway)
    alternate_prepared = _prepared(store=alternate_store, gateway=alternate_gateway)
    alternate_repository = AgenticTaskRepository(alternate_storage, clock=lambda: NOW)
    orchestrator: AgenticOrchestrator
    changed = False

    def swap_pointer() -> bool:
        nonlocal changed
        if not changed:
            changed = True
            value = (
                alternate_repository if pointer == "repository" else alternate_prepared
            )
            object.__setattr__(
                orchestrator,
                f"_AgenticOrchestrator__{pointer if pointer == 'repository' else 'prepared_authority'}",
                value,
            )
        return False

    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        cancel_requested=swap_pointer,
        clock=lambda: NOW,
    )

    with pytest.raises(
        AgenticOrchestrationError, match=r"authority\.execution_seal_invalid"
    ):
        orchestrator.run(rules=_rules(), run_id=f"source-run-snapshot-{pointer}")

    original = AgenticTaskRepository(storage, clock=lambda: NOW).require_run(
        f"source-run-snapshot-{pointer}"
    )
    assert original.status == "failed"
    assert (
        alternate_storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[
            0
        ]
        == 0
    )
    storage.close()
    alternate_storage.close()


def test_round13_run_snapshot_method_mutation_is_pre_effect_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(agentic._AgenticRunSnapshot, "validate", lambda self: None)

    with pytest.raises(AgenticOrchestrationError, match=r"rules\.seal_invalid"):
        orchestrator.run(rules=_rules(), run_id="source-run-snapshot-method")
    assert requests == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    client.close()
    storage.close()


@pytest.mark.parametrize(
    "helper_name",
    (
        "_canonical_url",
        "_canonical_query",
        "_query_decoding_passes",
        "_validate_non_sensitive_text",
        "_is_access_secret_like_key",
    ),
)
def test_round13_access_canonicalization_graph_drift_is_pre_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    rules = _rules()
    monkeypatch.setattr(
        access_decision_module, helper_name, lambda *args, **kwargs: True
    )

    with pytest.raises(AgenticOrchestrationError, match=r"authority\.seal_invalid"):
        orchestrator.run(
            rules=rules, run_id=f"source-run-access-graph-{helper_name.strip('_')}"
        )
    assert requests == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    client.close()
    storage.close()


def test_round13_safe_normalize_dispatch_method_drift_precedes_dns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    monkeypatch.setattr(
        site_diagnostic_module._SafePinnedDispatch,
        "normalize_http_url",
        lambda self, value: normalize_http_url(value),
    )

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    storage.close()


@pytest.mark.parametrize(
    ("owner", "method_name"),
    (
        (site_diagnostic_module.socket.socket, "settimeout"),
        (site_diagnostic_module.socket.socket, "getpeername"),
        (site_diagnostic_module.socket.socket, "close"),
        (site_diagnostic_module.ssl.SSLContext, "wrap_socket"),
        (http.client.HTTPConnection, "putrequest"),
        (http.client.HTTPConnection, "putheader"),
        (http.client.HTTPConnection, "endheaders"),
        (http.client.HTTPConnection, "getresponse"),
        (http.client.HTTPConnection, "close"),
        (http.client.HTTPResponse, "read"),
        (http.client.HTTPResponse, "getheaders"),
        (http.client.HTTPResponse, "close"),
    ),
)
def test_round13_safe_unbound_io_graph_drift_is_pre_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    method_name: str,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    monkeypatch.setattr(owner, method_name, lambda *args, **kwargs: None)

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    assert (
        storage.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agentic_runs'"
        ).fetchone()[0]
        == 0
    )
    storage.close()


def test_round13_execution_snapshot_rejects_crawler_pointer_swap(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway = _gateway(gzip.compress(b"<html>ok</html>", mtime=0))
    orchestrator: AgenticOrchestrator
    changed = False

    class OtherCrawler(HtmlLinkCrawlerAdapter):
        adapter_id = "other-crawler"

    def swap_adapter() -> bool:
        nonlocal changed
        if not changed:
            changed = True
            object.__setattr__(
                orchestrator,
                "_crawler_snapshot",
                orchestrator._snapshot_adapter(OtherCrawler(), search=False),
            )
        return False

    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        cancel_requested=swap_adapter,
        clock=lambda: NOW,
    )
    with pytest.raises(
        AgenticOrchestrationError, match=r"authority\.execution_seal_invalid"
    ):
        orchestrator.run(rules=_rules(), run_id="source-run-crawler-pointer")
    storage.close()


def test_round13_failed_mock_graph_validation_leaves_preparation_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(
        lambda request: httpx.Response(404, request=request)
    )
    before = (
        gateway._gateways,
        gateway._prepared_origins,
        gateway._transport._robots_mode,
        gateway._state_lock,
    )
    original = MockClientReadGateway._build_gateway
    monkeypatch.setattr(
        MockClientReadGateway,
        "_build_gateway",
        lambda self, origin, allowed: original(self, origin, allowed),
    )

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        _prepared(store=store, gateway=gateway)
    assert (
        gateway._gateways,
        gateway._prepared_origins,
        gateway._transport._robots_mode,
        gateway._state_lock,
    ) == before
    client.close()
    storage.close()


def test_round13_same_output_origin_helper_drift_never_publishes_mock_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(
        lambda request: httpx.Response(404, request=request)
    )
    original_origin = agentic._url_origin
    original_gateways = gateway._gateways
    original_gateway_items = tuple(gateway._gateways.items())
    original_origins = gateway._prepared_origins
    original_mode = gateway._transport._robots_mode
    original_lock = gateway._state_lock
    monkeypatch.setattr(
        agentic,
        "_url_origin",
        lambda value: original_origin(value),
    )

    with pytest.raises(AgenticOrchestrationError, match=r"authority\.seal_invalid"):
        _prepared(store=store, gateway=gateway)

    assert gateway._gateways is original_gateways
    assert tuple(gateway._gateways.items()) == original_gateway_items
    assert gateway._prepared_origins is original_origins
    assert gateway._transport._robots_mode is original_mode
    assert gateway._state_lock is original_lock
    client.close()
    storage.close()


def test_round13_origin_start_tickets_are_ordered_and_failed_claim_wakes_next() -> None:
    class RecordingTransport:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            self.targets.append(url)
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    transport = RecordingTransport()
    start_sleeping = threading.Event()
    fail_first = threading.Event()
    sleeper_enabled = False
    sleep_calls = 0

    def sleeper(delay: float) -> None:
        nonlocal sleep_calls
        assert delay > 0
        if not sleeper_enabled:
            return
        sleep_calls += 1
        if sleep_calls == 1:
            start_sleeping.set()
            assert fail_first.wait(2)
            raise RuntimeError("start failed")

    facade = _context_gateway(
        transport,
        pacing_interval=timedelta(seconds=1),
        sleeper=sleeper,
    )
    gateway = facade.gateway

    def consume(raw: RawHttpResponse, _context: object) -> bytes:
        return b"".join(raw.body_chunks)

    gateway.request_with_context("https://example.invalid/warm.html", consume=consume)
    transport.targets.clear()
    sleeper_enabled = True

    first_claimed = threading.Event()
    second_claimed = threading.Event()
    errors: list[BaseException] = []

    def first_claim(url: str, decision: object):
        del url, decision
        first_claimed.set()

    def second_claim(url: str, decision: object):
        del url, decision
        second_claimed.set()

    def request(path: str, callback) -> None:
        try:
            gateway.request_with_context(
                f"https://example.invalid/{path}",
                consume=consume,
                before_target_request=callback,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted thread evidence.
            errors.append(exc)

    first = threading.Thread(target=request, args=("first.html", first_claim))
    second = threading.Thread(target=request, args=("second.html", second_claim))
    first.start()
    assert first_claimed.wait(2)
    assert start_sleeping.wait(2)
    second.start()
    assert second_claimed.wait(2)
    assert transport.targets == []
    fail_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], AccessGatewayTransportError)
    assert transport.targets == ["https://example.invalid/second.html"]
    facade._test_client.close()


def test_round14_signed_pacing_slot_survives_callback_start_inversion() -> None:
    class RecordingTransport:
        def __init__(self) -> None:
            self.targets: list[str] = []
            self.target_started = threading.Event()

        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            self.targets.append(url)
            self.target_started.set()
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    transport = RecordingTransport()
    sleeper_entered = threading.Event()
    allow_signed_slot = threading.Event()

    def sleeper(delay: float) -> None:
        assert delay >= 1
        sleeper_entered.set()
        assert allow_signed_slot.wait(2)

    facade = _context_gateway(
        transport,
        pacing_interval=timedelta(seconds=1),
        sleeper=sleeper,
    )
    gateway = facade.gateway
    origin = normalize_http_url("https://example.invalid/")[1]
    gateway._policy_for(origin)
    transport.targets.clear()

    first_callback_entered = threading.Event()
    release_first_callback = threading.Event()
    second_callback_entered = threading.Event()
    results: dict[str, object] = {}
    errors: list[BaseException] = []

    def first_callback(_url: str, decision: object):
        results["first_decision"] = decision
        first_callback_entered.set()
        assert release_first_callback.wait(2)

    def second_callback(_url: str, decision: object):
        results["second_decision"] = decision
        second_callback_entered.set()

    def request(label: str, callback) -> None:
        try:
            results[label] = gateway.request_with_context(
                f"https://example.invalid/{label}.html",
                consume=lambda raw, _context: b"".join(raw.body_chunks),
                before_target_request=callback,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted thread evidence.
            errors.append(exc)

    first = threading.Thread(target=request, args=("first", first_callback))
    second = threading.Thread(target=request, args=("second", second_callback))
    first.start()
    assert first_callback_entered.wait(2)
    second.start()
    assert second_callback_entered.wait(2)
    sent_before_signed_slot = transport.target_started.wait(0.25)
    if not sent_before_signed_slot:
        assert sleeper_entered.wait(2)
    allow_signed_slot.set()
    release_first_callback.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert not sent_before_signed_slot
    assert transport.targets == [
        "https://example.invalid/second.html",
        "https://example.invalid/first.html",
    ]
    second_decision = results["second_decision"]
    second_result = results["second"]
    assert second_decision.origin_reservation is not None
    assert second_result.decision == second_decision
    assert (
        second_result.decision.origin_reservation.not_before
        == second_decision.origin_reservation.not_before
    )
    facade._test_client.close()


def test_round14_safe_normalized_origin_constructor_drift_precedes_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    original = site_diagnostic_module.NormalizedOrigin
    monkeypatch.setattr(
        site_diagnostic_module,
        "NormalizedOrigin",
        lambda **kwargs: original(**kwargs),
    )

    with pytest.raises(AgenticOrchestrationError, match=r"gateway\.seal_invalid"):
        prepared.validate()
    assert (
        storage.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agentic_runs'"
        ).fetchone()[0]
        == 0
    )
    storage.close()


def test_round14_prepared_validate_method_drift_cannot_widen_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    inputs = _compiler_inputs(
        seed_url="https://example.invalid/reports/root.html",
        allowed_page_prefixes=("/reports",),
        allowed_file_prefixes=("/reports",),
    )
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    prepared = _prepared(store=store, gateway=gateway, inputs=inputs)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        clock=lambda: NOW,
    )
    monkeypatch.setattr(agentic.PreparedAgenticAuthority, "validate", lambda self: None)
    object.__setattr__(
        prepared,
        "scope",
        replace(
            prepared.scope,
            seed_url="https://example.invalid/admin/root.html",
            allowed_page_prefixes=["/"],
            allowed_file_prefixes=["/"],
        ),
    )
    payload = _rules().model_dump(mode="json")
    payload["scope"]["seed_urls"] = ["https://example.invalid/admin/root.html"]
    rules = AgenticSiteRules.model_validate(payload)

    with pytest.raises(AgenticOrchestrationError, match=r"authority\.seal_invalid"):
        orchestrator.run(rules=rules, run_id="source-run-prepared-validate-drift")
    assert requests == []
    assert storage.conn.execute("SELECT COUNT(*) FROM agentic_runs").fetchone()[0] == 0
    client.close()
    storage.close()


def test_round14_adapter_snapshot_method_drift_cannot_publish_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptySearch:
        adapter_id = "empty-search"
        adapter_version = "1.0.0"
        authorized = True

        def search(self, query: str):
            del query
            return ()

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            404 if request.url.path == "/robots.txt" else 200,
            request=request,
            headers={"content-type": "text/html"},
            content=b"<html></html>",
        )

    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    orchestrator = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(store=store, gateway=gateway),
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        search_adapter=EmptySearch(),
        clock=lambda: NOW,
    )
    payload = _rules().model_dump(mode="json")
    payload["scope"]["queries"] = [{"text": "updates", "required": False}]
    payload["budgets"]["max_depth"] = 0
    rules = AgenticSiteRules.model_validate(payload)
    injected_url = "https://example.invalid/injected.html"
    monkeypatch.setattr(
        agentic._AdapterSnapshot,
        "invoke",
        lambda self, *args, **kwargs: (
            AgenticCandidate(
                url=injected_url,
                discovery_kind="search",
                discovered_from_url="https://example.invalid/results",
            ),
        ),
    )

    result = orchestrator.run(rules=rules, run_id="source-run-adapter-method-drift")
    assert injected_url not in requests
    assert all(task.requested_url != injected_url for task in result.tasks)
    search_task = next(task for task in result.tasks if task.kind == "search")
    assert search_task.failure_code == "search.adapter_error"
    client.close()
    storage.close()


def test_round14_target_send_uses_sealed_lease_progress_before_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [NOW]
    storage = Storage(tmp_path / "db.sqlite")
    first = AgenticTaskRepository(storage, clock=lambda: now[0])
    second = AgenticTaskRepository(storage, clock=lambda: now[0])
    run_id = "source-run-sealed-send-progress"
    first.create_run(run_id=run_id, rules=_rules(), authority=_legacy_authority())
    assert first.acquire_run_lease(run_id, owner="owner-first", ttl_seconds=1) == 1
    first.begin_read_budget(
        run_id,
        max_requests=4,
        max_bytes=4096,
        max_files=4,
        max_concurrency=1,
    )

    class RecordingTransport:
        def __init__(self) -> None:
            self.targets: list[str] = []

        def request(self, url: str, **kwargs) -> RawHttpResponse:
            del kwargs
            if url.endswith("/robots.txt"):
                return RawHttpResponse(status=404, headers={}, body_chunks=())
            self.targets.append(url)
            return RawHttpResponse(
                status=200,
                headers={"content-type": "text/html"},
                body_chunks=(b"<html>ok</html>",),
            )

    transport = RecordingTransport()
    facade = _context_gateway(transport, clock=lambda: now[0])
    gateway = facade.gateway
    gateway._policy_for(normalize_http_url("https://example.invalid/")[1])
    transport.targets.clear()
    successor_epoch: list[int | None] = []
    monkeypatch.setattr(agentic._TargetSendLease, "renew", lambda self: None)

    def claim(_url: str, _decision: object):
        guard = agentic._TargetSendLease(
            first,
            first.claim_target_send(
                run_id,
                max_requests=4,
                timeout_seconds=1.0,
            ),
            timeout_seconds=1.0,
        )
        now[0] += timedelta(seconds=2)
        successor_epoch.append(
            second.acquire_run_lease(run_id, owner="owner-second", ttl_seconds=30)
        )
        return guard

    with pytest.raises(AccessGatewayTransportError) as error:
        gateway.request_with_context(
            "https://example.invalid/root.html",
            consume=lambda raw, _context: b"".join(raw.body_chunks),
            before_target_request=claim,
        )
    assert error.value.kind == "transport_integrity"
    assert successor_epoch == [2]
    assert transport.targets == []
    row = storage.conn.execute(
        "SELECT lease_owner, lease_epoch FROM agentic_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert tuple(row) == ("owner-second", 2)
    facade._test_client.close()
    storage.close()


def test_round15_safe_percent_regex_drift_is_rejected_after_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    prepared = _prepared(
        store=store,
        gateway=_production_gateway(SafePinnedTransport(timeout=10.0, chunk_size=1024)),
    )
    gateway = prepared.read_gateway.gateway
    origin = normalize_http_url("https://example.invalid/")[1]
    expires_at = NOW + timedelta(hours=1)
    robots = site_diagnostic_module.ParsedRobots(
        gateway.config.identity.product_token,
        [],
        [],
        [],
        [],
    )
    evidence = site_diagnostic_module.build_origin_policy_evidence(
        origin=origin,
        robots=robots,
        robots_sha256=hashlib.sha256(b"").hexdigest(),
        robots_status="absent",
        identity=gateway.config.identity,
        fetched_at=NOW,
        expires_at=expires_at,
    )
    policy = gateway._build_policy(
        origin=origin,
        observation_kind="http_404",
        http_status=404,
        evidence=evidence,
        observed_at=NOW,
        expires_at=expires_at,
    )
    gateway._policy_cache[gateway._cache_key(origin)] = policy
    original_sub = site_diagnostic_module.re.sub
    rewritten_paths: list[str] = []
    target_effects: list[tuple[object, ...]] = []

    def altered_sub(pattern, replacement, value, *args, **kwargs):
        monkeypatch.setattr(site_diagnostic_module.re, "sub", original_sub)
        rewritten_paths.append(value)
        monkeypatch.setattr(
            site_diagnostic_module.socket,
            "getaddrinfo",
            lambda *call_args, **call_kwargs: target_effects.append(call_args) or [],
        )
        if value == "/allowed/item":
            return "/admin"
        return original_sub(pattern, replacement, value, *args, **kwargs)

    def install_after_authorization(_url: str, decision: object):
        assert decision.outcome == "allow"
        monkeypatch.setattr(site_diagnostic_module.re, "sub", altered_sub)

    with pytest.raises(AccessGatewayTransportError) as error:
        prepared.read_gateway.read(
            "https://example.invalid/allowed/item",
            max_body_bytes=1024,
            before_target_request=install_after_authorization,
        )

    assert error.value.kind == "transport_integrity"
    assert rewritten_paths == []
    assert target_effects == []
    assert (
        storage.conn.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
        == 0
    )
    assert (
        storage.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agentic_task_observations'"
        ).fetchone()[0]
        == 0
    )
    storage.close()


def test_round15_paired_predicate_dispatch_rebind_precedes_mock_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    inputs = _compiler_inputs(
        seed_url="https://example.invalid/reports/root.html",
        allowed_page_prefixes=("/reports",),
        allowed_file_prefixes=("/reports",),
    )
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    original_gateways = gateway._gateways
    original_gateway_items = tuple(gateway._gateways.items())
    original_origins = gateway._prepared_origins
    original_mode = gateway._transport._robots_mode
    original_lock = gateway._state_lock
    forged_dispatch = replace(
        agentic._AGENTIC_PREDICATE_DISPATCH,
        path_within_prefix=lambda _path, _prefix: True,
    )

    def matching_validator(dispatch):
        assert dispatch is forged_dispatch
        return (id(dispatch), id(dispatch.path_within_prefix))

    monkeypatch.setattr(agentic, "_AGENTIC_PREDICATE_DISPATCH", forged_dispatch)
    monkeypatch.setattr(
        agentic,
        "_AGENTIC_PREDICATE_DISPATCH_VALIDATOR",
        matching_validator,
    )

    with pytest.raises(AgenticOrchestrationError, match=r"authority\.seal_invalid"):
        _prepared(store=store, gateway=gateway, inputs=inputs)

    assert gateway._gateways is original_gateways
    assert tuple(gateway._gateways.items()) == original_gateway_items
    assert gateway._prepared_origins is original_origins
    assert gateway._transport._robots_mode is original_mode
    assert gateway._state_lock is original_lock
    assert requests == []
    assert (
        storage.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agentic_runs'"
        ).fetchone()[0]
        == 0
    )
    client.close()
    storage.close()


def test_round15_four_way_predicate_root_rebind_cannot_self_authorize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(404, request=request)

    inputs = _compiler_inputs(
        seed_url="https://example.invalid/reports/root.html",
        allowed_page_prefixes=("/reports",),
        allowed_file_prefixes=("/reports",),
    )
    storage = Storage(tmp_path / "db.sqlite")
    store = ArtifactStore(storage, root=tmp_path / "artifacts")
    gateway, client = _offline_gateway(handler)
    original_gateways = gateway._gateways
    original_gateway_items = tuple(gateway._gateways.items())
    original_origins = gateway._prepared_origins
    original_mode = gateway._transport._robots_mode
    original_lock = gateway._state_lock
    forged_dispatch = replace(
        agentic._AGENTIC_PREDICATE_DISPATCH,
        path_within_prefix=lambda _path, _prefix: True,
    )

    def matching_validator(dispatch):
        assert dispatch is forged_dispatch
        return (id(dispatch), id(dispatch.path_within_prefix))

    for name, value in (
        ("_AGENTIC_PREDICATE_DISPATCH", forged_dispatch),
        ("_AGENTIC_PREDICATE_DISPATCH_VALIDATOR", matching_validator),
        ("_FROZEN_AGENTIC_PREDICATE_DISPATCH", forged_dispatch),
        ("_FROZEN_AGENTIC_PREDICATE_DISPATCH_VALIDATOR", matching_validator),
    ):
        monkeypatch.setattr(agentic, name, value)

    with pytest.raises(AgenticOrchestrationError, match=r"authority\.seal_invalid"):
        _prepared(store=store, gateway=gateway, inputs=inputs)

    assert gateway._gateways is original_gateways
    assert tuple(gateway._gateways.items()) == original_gateway_items
    assert gateway._prepared_origins is original_origins
    assert gateway._transport._robots_mode is original_mode
    assert gateway._state_lock is original_lock
    assert requests == []
    assert (
        storage.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'agentic_runs'"
        ).fetchone()[0]
        == 0
    )
    client.close()
    storage.close()
