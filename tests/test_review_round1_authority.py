from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_listening.blocks.access_gateway import AccessGateway, AccessGatewayConfig
from web_listening.blocks.acquisition_gateway import AcquisitionOutcome
from web_listening.blocks.crawler import FetchResult
from web_listening.blocks.governed_read import AccessRejectedError, GovernedReadGateway
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    RawHttpResponse,
    normalize_http_url,
)
from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_crawler import TreeCrawler
from web_listening.contracts.site_diagnostic import DiagnosticIdentity, canonical_sha256
from web_listening.models import CrawlScope
import web_listening.blocks.governed_read as governed_read
import web_listening.blocks.staged_workflow as staged_workflow
import web_listening.blocks.tree_bootstrap_workflow as tree_bootstrap_workflow
import web_listening.blocks.tree_run_workflow as tree_run_workflow


class _Transport:
    def __init__(self, responses: dict[str, tuple[int, bytes, dict[str, str]]]):
        self.responses = responses
        self.requests: list[str] = []

    def request(
        self, url: str, *, user_agent: str, identity_sha256: str
    ) -> RawHttpResponse:
        del user_agent, identity_sha256
        self.requests.append(url)
        status, body, headers = self.responses[url]
        return RawHttpResponse(status=status, headers=headers, body_chunks=(body,))


def _target() -> SimpleNamespace:
    return SimpleNamespace(
        catalog="dev",
        site_key="demo",
        display_name="Demo",
        seed_url="https://example.com/",
        homepage_url="https://example.com/",
        fetch_mode="http",
        fetch_config_json={},
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
        tree_strategy="homepage_full",
        tree_budget_profile="production_default",
        tree_max_depth=None,
        tree_max_pages=None,
        tree_max_files=None,
        notes="",
    )


@pytest.mark.parametrize("robots_status", [401, 403])
def test_canonical_discovery_propagates_gateway_rejection_before_artifact_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    robots_status: int,
) -> None:
    transport = _Transport({"https://example.com/robots.txt": (robots_status, b"", {})})
    monkeypatch.setattr(
        staged_workflow, "load_tree_targets", lambda catalog: [_target()]
    )
    monkeypatch.setattr(
        staged_workflow, "filter_tree_targets", lambda targets, keys: targets
    )
    monkeypatch.setattr(governed_read, "SafePinnedTransport", lambda *a, **k: transport)
    yaml_path = tmp_path / "inventory.yaml"
    report_path = tmp_path / "inventory.md"

    with pytest.raises(AccessRejectedError):
        staged_workflow.discover_sections(
            catalog="dev",
            max_pages=1,
            yaml_path=yaml_path,
            report_path=report_path,
        )

    assert transport.requests == ["https://example.com/robots.txt"]
    assert not yaml_path.exists()
    assert not report_path.exists()


def test_canonical_discovery_default_path_uses_governed_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://example.com/": (
                200,
                b"<html><main>governed discovery</main></html>",
                {"content-type": "text/html"},
            ),
        }
    )
    monkeypatch.setattr(
        staged_workflow, "load_tree_targets", lambda catalog: [_target()]
    )
    monkeypatch.setattr(
        staged_workflow, "filter_tree_targets", lambda targets, keys: targets
    )
    monkeypatch.setattr(governed_read, "SafePinnedTransport", lambda *a, **k: transport)

    artifacts = staged_workflow.discover_sections(
        catalog="dev",
        max_pages=1,
        yaml_path=tmp_path / "inventory.yaml",
        report_path=tmp_path / "inventory.md",
    )

    assert artifacts.inventory.sites[0].pages_discovered == 1
    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/",
    ]


def test_canonical_discovery_body_failure_leaves_no_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://example.com/": (
                200,
                b"x" * (4 * 1024 * 1024 + 1),
                {"content-type": "text/html"},
            ),
        }
    )
    monkeypatch.setattr(
        staged_workflow, "load_tree_targets", lambda catalog: [_target()]
    )
    monkeypatch.setattr(
        staged_workflow, "filter_tree_targets", lambda targets, keys: targets
    )
    monkeypatch.setattr(governed_read, "SafePinnedTransport", lambda *a, **k: transport)
    output_root = tmp_path / "not-created"
    yaml_path = output_root / "inventory.yaml"
    report_path = output_root / "reports" / "inventory.md"

    with pytest.raises(BodyFailure, match="wire_budget_exhausted"):
        staged_workflow.discover_sections(
            catalog="dev",
            max_pages=1,
            yaml_path=yaml_path,
            report_path=report_path,
        )

    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/",
    ]
    assert not output_root.exists()


def _outcome(url: str, *, accepted: bool) -> AcquisitionOutcome:
    page = (
        FetchResult(
            "<main>seed</main>",
            "<main>seed</main>",
            "seed",
            "seed",
            "seed",
            {},
            url,
            200,
        )
        if accepted
        else None
    )
    return AcquisitionOutcome(
        request=SimpleNamespace(url=url),
        result=None,
        page=page,
        classification="accepted" if accepted else "rejected",
        attempts=("accepted" if accepted else "rejected",),
        coverage_complete=accepted,
    )


def _scope() -> CrawlScope:
    return CrawlScope(
        site_id=1,
        seed_url="https://example.com/",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
        max_depth=0,
        max_pages=1,
        max_files=0,
        follow_files=False,
        fetch_mode="http",
        fetch_config_json={},
    )


def test_tree_bootstrap_rejects_unaccepted_seed_before_storage_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tree_bootstrap_workflow,
        "Storage",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Storage constructed")),
    )

    with pytest.raises(ValueError, match="accepted exact seed"):
        tree_bootstrap_workflow.run_bootstrap(
            catalog="dev",
            max_depth=0,
            max_pages=1,
            max_files=0,
            targets=[_target()],
            acquisition_gateway=object(),
            initial_outcome=_outcome("https://example.com/", accepted=False),
        )


def test_tree_crawler_rejected_seed_leaves_database_bytes_unchanged(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "tree.db"
    storage = Storage(db_path)
    try:
        before = db_path.read_bytes()
        tree = TreeCrawler(
            storage=storage,
            acquisition_gateway=object(),
            initial_outcome=_outcome("https://example.com/", accepted=False),
        )
        with pytest.raises(ValueError, match="accepted exact seed"):
            tree.bootstrap_scope(_scope(), download_files=False)
        assert db_path.read_bytes() == before
    finally:
        storage.close()


def test_legacy_incremental_wrapper_fails_before_catalog_or_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tree_run_workflow,
        "load_tree_targets",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("catalog loaded")),
        raising=False,
    )
    monkeypatch.setattr(
        tree_run_workflow,
        "Storage",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Storage constructed")),
    )

    with pytest.raises(ValueError, match="PreparedScopeExecution"):
        tree_run_workflow.run_incremental(
            catalog="dev",
            max_depth=0,
            max_pages=1,
            max_files=0,
            acquisition_gateway=object(),
        )


def test_sealed_web_http_limits_require_one_exact_positive_pair() -> None:
    compiled = SimpleNamespace(
        steps=(
            {
                "executor_id": "web_http",
                "limits": {"timeout_seconds": 7.5, "stdout_bytes": 1234},
            },
        )
    )

    assert staged_workflow._sealed_web_http_limits(compiled) == (7.5, 1234)

    compiled.steps[0]["limits"]["stdout_bytes"] = 0
    with pytest.raises(ValueError, match="sealed web_http limits"):
        staged_workflow._sealed_web_http_limits(compiled)

    compiled.steps = (
        {
            "executor_id": "web_http",
            "limits": {"timeout_seconds": 7.5, "stdout_bytes": 1234},
        },
        {
            "executor_id": "web_http",
            "limits": {"timeout_seconds": 8.0, "stdout_bytes": 1234},
        },
    )
    with pytest.raises(ValueError, match="complete and consistent"):
        staged_workflow._sealed_web_http_limits(compiled)


def test_runtime_gateway_uses_sealed_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}
    transport = _Transport({})

    def build_transport(*, timeout: float):
        captured["timeout"] = timeout
        return transport

    monkeypatch.setattr(governed_read, "SafePinnedTransport", build_transport)
    reader = governed_read.build_runtime_read_gateway(
        authority_sha256="a" * 64,
        seed_urls=("https://example.com/",),
        allowed_domains=(),
        user_agent="web-listening-bot/2.0",
        max_body_bytes=1234,
        timeout_seconds=7.5,
        budget_limit=1,
    )

    assert reader.max_body_bytes == 1234
    assert captured == {"timeout": 7.5}


def test_formal_gateway_derives_body_and_timeout_from_compiled_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = SimpleNamespace(
        mode="governed",
        steps=(
            {
                "position": 0,
                "executor_id": "web_http",
                "executor_version": "1.0.0",
                "recipe_id": "http",
                "script_sha256": "b" * 64,
                "config": {},
                "limits": {
                    "timeout_seconds": 6.25,
                    "stdout_bytes": 2468,
                    "stderr_bytes": 1024,
                },
            },
        ),
        acquisition_fingerprint="c" * 64,
        scope_budgets={"max_pages": 2, "max_files": 0},
    )
    profile = SimpleNamespace(safety=SimpleNamespace(allowed_domains=["example.com"]))
    plan = SimpleNamespace(
        site_key="demo",
        seed_url="https://example.com/",
        homepage_url="https://example.com/",
        based_on={
            "acquisition_profile_id": "profile",
            "site_skill_version": "1.0.0",
            "site_skill_package_sha256": "d" * 64,
            "site_skill_recipe_id": "http",
            "site_skill_script_sha256": "b" * 64,
            "executor_version": "1.0.0",
        },
    )
    captured: dict[str, object] = {}
    reader = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        "web_listening.blocks.acquisition_profile.load_acquisition_profile",
        lambda *args, **kwargs: profile,
    )
    monkeypatch.setattr(
        "web_listening.site_skill_registry.resolve_site_skill_contract",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "web_listening.blocks.acquisition_execution_plan.compile_acquisition_execution_plan",
        lambda *args: compiled,
    )
    monkeypatch.setattr(
        "web_listening.blocks.governed_read.build_runtime_read_gateway",
        lambda **kwargs: captured.update(kwargs) or reader,
    )

    gateway = staged_workflow._compile_acquisition_gateway(
        plan,
        acquisition_profile_path="profile.yaml",
    )
    try:
        assert captured["max_body_bytes"] == 2468
        assert captured["timeout_seconds"] == 6.25
    finally:
        gateway.close()


def test_runtime_gateway_body_limit_rejects_oversize_before_consumer_returns() -> None:
    visible = {
        "identity_id": "limit-test",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/2.0",
    }
    identity = DiagnosticIdentity(
        **visible,
        identity_sha256=canonical_sha256(visible),
    )
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://example.com/": (200, b"12345", {}),
        }
    )
    reader = GovernedReadGateway(
        AccessGateway(
            AccessGatewayConfig(
                identity=identity,
                allowed_origins=frozenset(
                    {normalize_http_url("https://example.com/")[1]}
                ),
                diagnostic_artifact_sha256="a" * 64,
                pacing_interval=timedelta(0),
                budget_limit=1,
            ),
            transport=transport,
        ),
        max_body_bytes=4,
    )

    with pytest.raises(Exception, match="limit|budget|exceed"):
        reader.read("https://example.com/")
