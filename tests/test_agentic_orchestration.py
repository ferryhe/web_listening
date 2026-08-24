from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from web_listening.blocks.access_gateway import AccessGatewayTransportError
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
    AgenticTaskRepository,
    HtmlLinkCrawlerAdapter,
    load_agentic_site_rules,
    prepare_agentic_authority,
)
from web_listening.blocks.governed_read import MockClientReadGateway
from web_listening.blocks.immutable_artifacts import ArtifactStore, ArtifactStoreError
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan
from web_listening.blocks.storage import Storage
from web_listening.contracts import (
    RuntimeRequirement,
    SecretPolicy,
    SiteSkill,
    SiteSkillExecutor,
    SiteSkillRecipe,
    VerificationRule,
)
from web_listening.executors.registry import ExecutorMetadata, ExecutorRegistry
from web_listening.site_skill_registry import ResolvedSiteSkill

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
HTML = b"<!doctype html><html><body><a href='/child.html'>child</a></body></html>"
CHILD = b"<!doctype html><html><body>child</body></html>"


def _rules_text(
    *,
    seeds: tuple[str, ...] = ("https://example.invalid/root.html",),
    queries: str = "[]",
    max_depth: int = 1,
    max_requests: int = 8,
    max_bytes: int = 4096,
    max_files: int = 8,
    max_retries: int = 1,
    content_types: tuple[str, ...] = ("text/html",),
) -> str:
    rendered_seeds = "\n".join(f"    - {item}" for item in seeds)
    rendered_types = "\n".join(f"  - {item}" for item in content_types)
    return f"""schema_version: agentic-site-rules.v1
rule_id: example-agentic
version: 1.0.0
site_key: example
scope:
  seed_urls:
{rendered_seeds}
  allowed_origins:
    - https://example.invalid
  allow_patterns:
    - https://example.invalid/**
  queries: {queries}
budgets:
  max_depth: {max_depth}
  max_requests: {max_requests}
  max_bytes: {max_bytes}
  max_files: {max_files}
  max_concurrency: 2
  max_retries: {max_retries}
content_types:
{rendered_types}
"""


@pytest.fixture
def rules_path(tmp_path: Path) -> Path:
    path = tmp_path / "agentic-rules.yaml"
    path.write_text(_rules_text(), encoding="utf-8")
    return path


def _compiler_inputs() -> SimpleNamespace:
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
        allowed_domains=("example.invalid",),
        default_executor_id="web_http",
        default_recipe_id="http",
        executors=(SiteSkillExecutor(executor_id="web_http"),),
        recipes=(
            SiteSkillRecipe(
                recipe_id="http",
                executor_id="web_http",
                profile_ref="profiles/default.yaml",
                entrypoint="scripts/http.py",
                required_capabilities=("http_get",),
                verification_rules=(
                    VerificationRule(rule_id="status", description="2xx status"),
                ),
            ),
        ),
    )
    resolved = ResolvedSiteSkill(
        manifest=manifest,
        package_sha256="1" * 64,
        script_sha256={"scripts/http.py": script_sha256},
    )
    registry = ExecutorRegistry.preview(
        {
            "web_http": ExecutorMetadata(
                executor_id="web_http",
                version="1.0.0",
                capabilities=frozenset({"http_get"}),
                timeout_seconds=10.0,
                stdout_bytes=64 * 1024 * 1024,
                stderr_bytes=1024,
            )
        }
    )
    based_on = {
        "acquisition_profile_id": "example-profile",
        "site_skill_version": manifest.version,
        "site_skill_package_sha256": resolved.package_sha256,
        "site_skill_recipe_id": "http",
        "site_skill_script_sha256": script_sha256,
        "executor_version": "1.0.0",
    }
    scope = MonitorScopePlan(
        "legacy",
        "example",
        "Example",
        "test",
        "2026-08-21T12:00:00Z",
        "approved",
        "manual",
        "Agentic test scope",
        "https://example.invalid/root.html",
        "https://example.invalid/",
        "http",
        {},
        "selected_scope",
        "selected_scope_default",
        "site_root",
        ["/"],
        ["/"],
        max_depth=100,
        max_pages=100,
        max_files=100,
        based_on=based_on,
    )
    profile = AcquisitionProfile(
        profile_id="example-profile",
        site_key="example",
        generated_at="2026-08-21T12:00:00Z",
        default_adapter="web_http",
        safety=AcquisitionSafetyPolicy(allowed_domains=["example.invalid"]),
        adapters=[AcquisitionAdapterConfig(adapter="web_http")],
        recipe_mappings=[
            AcquisitionRecipeMapping(adapter="web_http", recipe_id="http")
        ],
    )
    plan = compile_acquisition_execution_plan(scope, profile, resolved, registry)
    derived = AgenticAuthority(
        site_skill_id=manifest.skill_id,
        site_skill_version=manifest.version,
        site_skill_package_sha256=resolved.package_sha256,
        execution_plan_id=f"acquisition-plan-{plan.acquisition_fingerprint[:24]}",
        execution_plan_version=plan.schema_version,
        execution_plan_sha256=hashlib.sha256(
            plan.to_json().encode("utf-8")
        ).hexdigest(),
        read_adapter_id=plan.executor_id or "",
        read_adapter_version=plan.executor_version or "",
    )
    return SimpleNamespace(
        scope=scope,
        profile=profile,
        executor_registry=registry,
        resolved_site_skill=resolved,
        execution_plan=plan,
        **derived.to_dict(),
    )


@pytest.fixture
def authority() -> SimpleNamespace:
    return _compiler_inputs()


@pytest.fixture
def stores(tmp_path: Path):
    storage = Storage(tmp_path / "web-listening.db")
    artifact_store = ArtifactStore(storage, root=tmp_path / "downloads")
    try:
        yield storage, artifact_store
    finally:
        storage.close()


class FakeReadGateway:
    def __new__(cls, responses: dict[str, tuple[bytes, str]]):
        calls: list[tuple[str, None]] = []
        failures: dict[str, list[Exception]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if request.url.path == "/robots.txt":
                return httpx.Response(404, request=request)
            calls.append((url, None))
            pending = failures.get(url, [])
            if pending:
                failure = pending.pop(0)
                if isinstance(failure, AccessGatewayTransportError):
                    if failure.kind == "timeout":
                        raise TimeoutError(str(failure))
                    if failure.kind in {"network", "dns"}:
                        raise ConnectionError(str(failure))
                raise failure
            body, content_type = responses[url]
            return httpx.Response(
                200,
                headers={"content-type": content_type},
                stream=httpx.ByteStream(body),
                request=request,
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        gateway = MockClientReadGateway(
            client,
            user_agent="web-listening-bot/2.0",
            max_body_bytes=64 * 1024 * 1024,
        )
        gateway.calls = calls
        gateway.failures = failures
        gateway._test_client = client
        return gateway


class FailingSearchAdapter:
    adapter_id = "authorized_search"
    adapter_version = "1.0.0"
    authorized = True

    def search(self, query: str):
        del query
        raise RuntimeError("Bearer should-never-be-persisted")


class ReplaySearchAdapter:
    adapter_id = "authorized_search"
    adapter_version = "1.0.0"
    authorized = True

    def __init__(self, url: str) -> None:
        self.url = url

    def search(self, query: str):
        del query
        return (
            AgenticCandidate(
                url=self.url,
                discovery_kind="search",
                discovered_from_url="https://search.invalid/results",
                required=False,
            ),
        )


class FailingCrawlerAdapter:
    adapter_id = "failing_crawler"
    adapter_version = "1.0.0"

    def discover(self, **kwargs):
        del kwargs
        raise RuntimeError("Cookie=should-never-be-persisted")


class ManyCandidateCrawlerAdapter:
    adapter_id = "bounded_crawler"
    adapter_version = "1.0.0"

    def __init__(self) -> None:
        self.yielded = 0

    def discover(self, **kwargs):
        final_url = kwargs["final_url"]
        while self.yielded < 1000:
            self.yielded += 1
            yield AgenticCandidate(
                url=f"https://example.invalid/candidate-{self.yielded}.html",
                discovery_kind="crawler",
                discovered_from_url=final_url,
            )


def _orchestrator(
    *,
    stores,
    gateway,
    authority,
    search_adapter=None,
    cancel_requested=None,
) -> AgenticOrchestrator:
    storage, _ = stores
    prepared = _prepared(stores=stores, gateway=gateway, authority=authority)
    return AgenticOrchestrator(
        storage=storage,
        prepared_authority=prepared,
        crawler_adapter=HtmlLinkCrawlerAdapter(),
        search_adapter=search_adapter,
        cancel_requested=cancel_requested,
        clock=lambda: NOW,
    )


def _prepared(*, stores, gateway, authority):
    _, artifact_store = stores
    return prepare_agentic_authority(
        scope=authority.scope,
        profile=authority.profile,
        resolved_site_skill=authority.resolved_site_skill,
        executor_registry=authority.executor_registry,
        execution_plan=authority.execution_plan,
        read_gateway=gateway,
        artifact_store=artifact_store,
    )


def test_site_rules_are_strict_versioned_bounded_and_digest_stable(
    rules_path: Path, tmp_path: Path
) -> None:
    first = load_agentic_site_rules(rules_path)
    second = load_agentic_site_rules(rules_path)

    assert first.schema_version == "agentic-site-rules.v1"
    assert first.version == "1.0.0"
    assert first.rules_sha256 == second.rules_sha256
    assert first.matches("https://example.invalid/child.html")
    assert not first.matches("https://outside.invalid/child.html")

    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text(_rules_text() + "site_key: replacement\n", encoding="utf-8")
    with pytest.raises(AgenticOrchestrationError) as error:
        load_agentic_site_rules(duplicate)
    assert error.value.reason_code == "rules.invalid"


def test_committed_agentic_site_rules_fixture_is_valid(tmp_path: Path) -> None:
    rules = load_agentic_site_rules(
        Path("docs/testing/fixtures/agentic-site-rules-v1.sample.yaml")
    )

    assert rules.rule_id == "example-agentic"
    assert rules.budgets.max_requests == 20
    assert rules.content_types == ("text/html", "application/pdf")

    sensitive = tmp_path / "sensitive.yaml"
    sensitive.write_text(
        _rules_text(seeds=("https://example.invalid/root.html?api_key=secret",)),
        encoding="utf-8",
    )
    with pytest.raises(AgenticOrchestrationError) as error:
        load_agentic_site_rules(sensitive)
    assert error.value.reason_code == "rules.invalid"

    boolean_budget = tmp_path / "boolean-budget.yaml"
    boolean_budget.write_text(
        _rules_text().replace("max_requests: 8", "max_requests: true"),
        encoding="utf-8",
    )
    with pytest.raises(AgenticOrchestrationError) as error:
        load_agentic_site_rules(boolean_budget)
    assert error.value.reason_code == "rules.invalid"

    encoded_query = tmp_path / "encoded-query.yaml"
    encoded_query.write_text(
        _rules_text(queries="[password%253Dhidden]"), encoding="utf-8"
    )
    with pytest.raises(AgenticOrchestrationError) as error:
        load_agentic_site_rules(encoded_query)
    assert error.value.reason_code == "rules.invalid"


def test_parent_cannot_complete_until_every_required_child_is_terminal(
    stores, rules_path: Path, authority: AgenticAuthority
) -> None:
    storage, _ = stores
    rules = load_agentic_site_rules(rules_path)
    repository = AgenticTaskRepository(storage)
    parent = repository.create_run(
        run_id="source-run-barrier",
        rules=rules,
        authority=authority,
    )
    repository.create_task(
        run_id=parent.run_id,
        task_key="required-seed",
        kind="read",
        required=True,
        requested_url=rules.scope.seed_urls[0],
        depth=0,
        discovery_kind="seed",
    )
    repository.seal_required_tasks(parent.run_id)

    with pytest.raises(AgenticOrchestrationError) as error:
        repository.finalize_run(parent.run_id, requested_status="completed")

    assert error.value.reason_code == "task.required_children_pending"


def test_complete_run_uses_gateway_stores_originals_and_traces_lineage(
    stores, rules_path: Path, authority: AgenticAuthority
) -> None:
    rules = load_agentic_site_rules(rules_path)
    gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (HTML, "text/html; charset=utf-8"),
            "https://example.invalid/child.html": (CHILD, "text/html"),
        }
    )
    orchestrator = _orchestrator(stores=stores, gateway=gateway, authority=authority)

    result = orchestrator.run(rules=rules, run_id="source-run-complete")

    assert result.parent.status == "completed"
    assert len(result.artifacts) == 2
    assert [call[0] for call in gateway.calls] == [
        "https://example.invalid/root.html",
        "https://example.invalid/child.html",
    ]
    assert all(item.access_decision_id for item in result.observations)
    child = next(
        item
        for item in result.artifacts
        if item.observation.final_url.endswith("/child.html")
    )
    root = next(
        item
        for item in result.artifacts
        if item.observation.final_url.endswith("/root.html")
    )
    assert child.lineage[0].related_artifact_id == root.observation.artifact_id
    assert child.observation.adapter_id == authority.read_adapter_id
    assert child.observation.access_decision_id.startswith("access-decision-")
    assert result.parent.site_skill_version == authority.site_skill_version
    assert result.parent.execution_plan_sha256 == authority.execution_plan_sha256


def test_same_run_replay_is_stable_and_creates_no_duplicate_observations(
    stores, rules_path: Path, authority: AgenticAuthority
) -> None:
    rules = load_agentic_site_rules(rules_path)
    gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (HTML, "text/html"),
            "https://example.invalid/child.html": (CHILD, "text/html"),
        }
    )
    orchestrator = _orchestrator(stores=stores, gateway=gateway, authority=authority)

    first = orchestrator.run(rules=rules, run_id="source-run-idempotent")
    calls_after_first = tuple(gateway.calls)
    second = orchestrator.run(rules=rules, run_id="source-run-idempotent")

    assert second == first
    assert tuple(gateway.calls) == calls_after_first
    storage, _ = stores
    count = storage.conn.execute(
        "SELECT COUNT(*) AS count FROM agentic_observations"
    ).fetchone()["count"]
    assert count == len(first.observations)


def test_optional_search_failure_makes_parent_partial_without_secret_diagnostics(
    stores, tmp_path: Path, authority: AgenticAuthority
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        _rules_text(queries="[{text: climate report, required: false}]"),
        encoding="utf-8",
    )
    rules = load_agentic_site_rules(path)
    gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (
                b"<!doctype html><html><body>root</body></html>",
                "text/html",
            )
        }
    )
    result = _orchestrator(
        stores=stores,
        gateway=gateway,
        authority=authority,
        search_adapter=FailingSearchAdapter(),
    ).run(rules=rules, run_id="source-run-optional-fail")

    assert result.parent.status == "partial"
    assert "optional_child_failed" in result.parent.warnings
    assert any(
        task.kind == "search" and task.status == "failed" for task in result.tasks
    )
    storage, _ = stores
    persisted = storage.conn.execute(
        "SELECT failure_code FROM agentic_tasks WHERE kind = 'search'"
    ).fetchone()["failure_code"]
    assert persisted == "search.adapter_error"
    assert "Bearer" not in storage.db_path.read_bytes().decode("latin-1")


def test_request_and_byte_budgets_are_hard_and_produce_partial_status(
    stores, tmp_path: Path, authority: AgenticAuthority
) -> None:
    path = tmp_path / "rules.yaml"
    path.write_text(
        _rules_text(
            seeds=(
                "https://example.invalid/root.html",
                "https://example.invalid/second.html",
            ),
            max_requests=1,
            max_bytes=len(HTML),
        ),
        encoding="utf-8",
    )
    rules = load_agentic_site_rules(path)
    gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (HTML, "text/html"),
            "https://example.invalid/second.html": (CHILD, "text/html"),
            "https://example.invalid/child.html": (CHILD, "text/html"),
        }
    )
    result = _orchestrator(stores=stores, gateway=gateway, authority=authority).run(
        rules=rules, run_id="source-run-request-budget"
    )

    assert result.parent.status == "partial"
    assert result.parent.requests_used == 1
    assert result.parent.bytes_used == len(HTML)
    assert len(gateway.calls) == 1
    assert "budget.requests_exhausted" in result.parent.warnings


def test_content_type_reject_fail_retry_cancel_and_replay_lineage_are_offline(
    stores, tmp_path: Path, authority: AgenticAuthority
) -> None:
    reject_path = tmp_path / "reject.yaml"
    reject_path.write_text(_rules_text(max_depth=0), encoding="utf-8")
    rules = load_agentic_site_rules(reject_path)
    reject_gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (
                b'{"not":"allowed"}',
                "application/json",
            )
        }
    )
    rejected = _orchestrator(
        stores=stores, gateway=reject_gateway, authority=authority
    ).run(rules=rules, run_id="source-run-rejected")
    assert rejected.parent.status == "rejected"
    assert rejected.tasks[0].status == "rejected"
    assert rejected.observations[0].access_decision_id
    assert rejected.artifacts == ()

    fail_gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (
                b"<!doctype html><html></html>",
                "text/html",
            )
        }
    )
    fail_gateway.failures["https://example.invalid/root.html"] = [
        AccessGatewayTransportError("timeout", "secret diagnostic"),
        AccessGatewayTransportError("timeout", "secret diagnostic"),
    ]
    failed = _orchestrator(
        stores=stores, gateway=fail_gateway, authority=authority
    ).run(rules=rules, run_id="source-run-failed")
    assert failed.parent.status == "failed"
    assert failed.parent.requests_used == 2
    assert len(failed.observations) == 2
    assert all(
        item.reason_code == "gateway.transport.timeout" for item in failed.observations
    )

    replay_gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (
                b"<!doctype html><html><body>recovered</body></html>",
                "text/html",
            )
        }
    )
    replayed = _orchestrator(
        stores=stores, gateway=replay_gateway, authority=authority
    ).run(
        rules=rules,
        run_id="source-run-replayed",
        replay_of_run_id="source-run-failed",
    )
    assert replayed.parent.status == "completed"
    assert replayed.tasks[0].replay_of_task_id == failed.tasks[0].task_id

    cancel_rules_path = tmp_path / "cancel.yaml"
    cancel_rules_path.write_text(_rules_text(max_depth=1), encoding="utf-8")
    cancel_rules = load_agentic_site_rules(cancel_rules_path)
    cancel_checks = iter((False, False, True))
    cancel_gateway = FakeReadGateway(
        {
            "https://example.invalid/root.html": (HTML, "text/html"),
            "https://example.invalid/child.html": (CHILD, "text/html"),
        }
    )
    cancelled = _orchestrator(
        stores=stores,
        gateway=cancel_gateway,
        authority=authority,
        cancel_requested=lambda: next(cancel_checks, True),
    ).run(rules=cancel_rules, run_id="source-run-cancelled")
    assert cancelled.parent.status == "cancelled"
    assert len(cancel_gateway.calls) == 1
    assert any(task.status == "cancelled" for task in cancelled.tasks)


def test_crawler_and_store_failures_become_stable_partial_or_failed_states(
    stores,
    rules_path: Path,
    authority: AgenticAuthority,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = load_agentic_site_rules(rules_path)
    gateway = FakeReadGateway(
        {"https://example.invalid/root.html": (HTML, "text/html")}
    )
    storage, artifact_store = stores
    crawler_failure = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(
            stores=stores, gateway=gateway, authority=authority
        ),
        crawler_adapter=FailingCrawlerAdapter(),
        clock=lambda: NOW,
    ).run(rules=rules, run_id="source-run-crawler-failure")

    assert crawler_failure.parent.status == "partial"
    assert "crawler.discovery_failed" in crawler_failure.parent.warnings
    assert "Cookie" not in storage.db_path.read_bytes().decode("latin-1")

    def fail_store(**kwargs):
        del kwargs
        raise ArtifactStoreError("mime.magic_mismatch")

    monkeypatch.setattr(artifact_store, "_prepare", fail_store)
    store_gateway = FakeReadGateway(
        {"https://example.invalid/root.html": (HTML, "text/html")}
    )
    store_failure = _orchestrator(
        stores=stores, gateway=store_gateway, authority=authority
    ).run(rules=rules, run_id="source-run-store-failure")

    assert store_failure.parent.status == "failed"
    assert store_failure.tasks[0].failure_code == "artifact_store.mime.magic_mismatch"
    assert store_failure.observations[0].access_decision_id


def test_repository_rejects_conflicting_task_replay(
    stores, rules_path: Path, authority: AgenticAuthority
) -> None:
    storage, _ = stores
    rules = load_agentic_site_rules(rules_path)
    repository = AgenticTaskRepository(storage, clock=lambda: NOW)
    repository.create_run(
        run_id="source-run-task-conflict", rules=rules, authority=authority
    )
    repository.create_task(
        run_id="source-run-task-conflict",
        task_key="same-key",
        kind="read",
        required=True,
        requested_url="https://example.invalid/root.html",
        depth=0,
        discovery_kind="seed",
    )

    with pytest.raises(AgenticOrchestrationError) as error:
        repository.create_task(
            run_id="source-run-task-conflict",
            task_key="same-key",
            kind="read",
            required=False,
            requested_url="https://example.invalid/other.html",
            depth=1,
            discovery_kind="crawler",
        )

    assert error.value.reason_code == "task.replay_conflict"


def test_candidate_iteration_is_bounded_by_remaining_request_budget(
    stores, tmp_path: Path, authority: AgenticAuthority
) -> None:
    path = tmp_path / "bounded.yaml"
    path.write_text(_rules_text(max_requests=1), encoding="utf-8")
    rules = load_agentic_site_rules(path)
    gateway = FakeReadGateway(
        {"https://example.invalid/root.html": (HTML, "text/html")}
    )
    crawler = ManyCandidateCrawlerAdapter()
    storage, _ = stores

    result = AgenticOrchestrator(
        storage=storage,
        prepared_authority=_prepared(
            stores=stores, gateway=gateway, authority=authority
        ),
        crawler_adapter=crawler,
        clock=lambda: NOW,
    ).run(rules=rules, run_id="source-run-bounded-candidates")

    assert result.parent.status == "partial"
    assert "budget.candidates_exhausted" in result.parent.warnings
    assert len(result.tasks) == 1
    assert crawler.yielded == 1
