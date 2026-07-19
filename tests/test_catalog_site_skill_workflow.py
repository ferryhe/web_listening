from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from web_listening.blocks.acquisition_execution_plan import compile_acquisition_execution_plan
import yaml

from web_listening.blocks.acquisition_profile import AcquisitionProfile
from web_listening.contracts import CaptureContent, CaptureResult
from web_listening.executors.registry import ExecutorRegistry
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan
from web_listening.executors.registry import default_preview_registry
from web_listening.site_skill_registry import resolve_site_skill_contract, validate_site_skill_package

ROOT = Path(__file__).parents[1]
SITES = ROOT / "web_listening/skills/sites"


def _resolved(key: str):
    package = SITES / key / "1.0.0"
    digest = validate_site_skill_package(package)["package_sha256"]
    return resolve_site_skill_contract(site_key=key, version="1.0.0", package_sha256=digest, root=SITES)


def _scope(key: str, url: str) -> MonitorScopePlan:
    return MonitorScopePlan("legacy", key, key, "catalog", "2026-01-01Z", "approved", "manual",
        "Catalog monitoring", url, url, "http", {}, "selected_scope", "selected_scope_default",
        "site_root", ["/"], ["/"], max_depth=1, max_pages=2, max_files=1, based_on={})


def _profile(key: str) -> AcquisitionProfile:
    payload = yaml.safe_load((SITES / key / "1.0.0/profiles/default.yaml").read_text(encoding="utf-8"))
    payload.pop("allowed_domains")
    return AcquisitionProfile.model_validate(payload)


def test_all_catalog_skills_compile_governed_http_plans() -> None:
    keys = [item["site_key"] for name in ("dev_test_sites.json", "smoke_site_catalog.json")
            for item in json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))]
    registry = default_preview_registry()
    for key in keys:
        resolved = _resolved(key)
        profile = _profile(key)
        url = profile.adapters[0].config["monitor_url"]
        scope = _scope(key, url)
        scope.based_on.update({
            "acquisition_profile_id": profile.profile_id,
            "site_skill_version": "1.0.0",
            "site_skill_package_sha256": resolved.package_sha256,
            "site_skill_recipe_id": "catalog-http",
            "site_skill_script_sha256": resolved.script_sha256["scripts/recipe.py"],
            "executor_version": registry.metadata["web_http"].version,
        })
        plan = compile_acquisition_execution_plan(scope, profile, resolved, registry)
        assert plan.mode == "governed"
        assert plan.site_key == key


def test_shared_domains_keep_separate_site_identity() -> None:
    assert _resolved("bcbs").manifest.site_key == "bcbs"
    assert _resolved("bis").manifest.site_key == "bis"
    assert _resolved("bcbs").package_sha256 != _resolved("bis").package_sha256


def test_governed_gateway_rejects_unsafe_redirect() -> None:
    resolved = _resolved("soa")
    profile = _profile("soa")
    preview = default_preview_registry()
    class FakeExecutor:
        executor_id = "web_http"

        def execute(self, request):
            now = datetime.now(timezone.utc)
            return CaptureResult(
                request_id=request.request_id, site_key=request.site_key, site_skill_id=request.site_skill_id,
                site_skill_version=request.site_skill_version, site_skill_digest=request.site_skill_digest,
                recipe_id=request.recipe_id, run_id=request.run_id, scope_id=request.scope_id,
                executor_id=request.executor_id, state="succeeded", started_at=now, finished_at=now,
                status_code=200, final_url="https://attacker.invalid/",
                content=CaptureContent(media_type="text/html", text="safe words " * 200), metadata={})
    registry = ExecutorRegistry({"web_http": FakeExecutor()}, metadata={"web_http": preview.metadata["web_http"]})
    scope = _scope("soa", profile.adapters[0].config["monitor_url"])
    scope.based_on.update({"acquisition_profile_id": profile.profile_id, "site_skill_version": "1.0.0",
        "site_skill_package_sha256": resolved.package_sha256, "site_skill_recipe_id": "catalog-http",
        "site_skill_script_sha256": resolved.script_sha256["scripts/recipe.py"],
        "executor_version": registry.metadata["web_http"].version})
    plan = compile_acquisition_execution_plan(scope, profile, resolved, registry)
    from web_listening.blocks.acquisition_gateway import GovernedAcquisitionGateway
    outcome = GovernedAcquisitionGateway(plan, registry).acquire(scope.seed_url, run_id="run", scope_id="scope")
    assert outcome.classification == "unsafe_redirect"
    assert outcome.page is None
