from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from web_listening.blocks.acquisition_execution_plan import (
    AcquisitionExecutionPlanError, canonical_json, compile_acquisition_execution_plan, preview_envelope,
)
from web_listening.blocks.acquisition_profile import (
    AcquisitionAdapterConfig, AcquisitionProfile, AcquisitionRecipeMapping,
    AcquisitionResourceLimits, AcquisitionSafetyPolicy,
)
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, compute_semantic_scope_fingerprint
from web_listening.executors.registry import ExecutorMetadata, ExecutorRegistry, default_preview_registry
from web_listening.contracts import SiteSkillExecutor, SiteSkillRecipe, VerificationRule
from web_listening.site_skill_registry import ResolvedSiteSkill, default_registry_root, resolve_site_skill_contract, validate_site_skill_package


def inputs():
    package = default_registry_root() / "example-news" / "1.0.0"
    validation = validate_site_skill_package(package)
    skill = resolve_site_skill_contract(site_key="example-news", version="1.0.0", package_sha256=validation["package_sha256"])
    script = skill.script_sha256["scripts/recipe.py"]
    based_on = {
        "acquisition_profile_id": "example-profile", "site_skill_version": "1.0.0",
        "site_skill_package_sha256": skill.package_sha256, "site_skill_recipe_id": "news-http",
        "site_skill_script_sha256": script, "executor_version": "1.0.0",
    }
    scope = MonitorScopePlan("legacy-value", "example-news", "Example", "dev", "2026-01-01Z", "approved", "manual",
        "Track news", "https://example.com/news", "https://example.com/", "http", {}, "selected_scope",
        "selected_scope_default", "site_root", ["/news"], ["/"], max_depth=3, max_pages=25, max_files=10,
        based_on=based_on)
    profile = AcquisitionProfile(profile_id="example-profile", site_key="example-news", generated_at="2026-01-01Z",
        default_adapter="web_http", safety=AcquisitionSafetyPolicy(allowed_domains=["example.com"]),
        adapters=[AcquisitionAdapterConfig(adapter="web_http")],
        recipe_mappings=[AcquisitionRecipeMapping(adapter="web_http", recipe_id="news-http")])
    return scope, profile, skill, default_preview_registry()


def test_governed_plan_is_canonical_deterministic_and_has_independent_fingerprints():
    scope, profile, skill, registry = inputs()
    first = compile_acquisition_execution_plan(scope, profile, skill, registry)
    second = compile_acquisition_execution_plan(replace(scope, generated_at="2099-01-01Z"),
        profile.model_copy(update={"generated_at": "2099-01-01Z"}), skill, registry)
    assert first.to_json() == second.to_json()
    assert first.scope_fingerprint != first.acquisition_fingerprint
    assert json.dumps(json.loads(first.to_json()), sort_keys=True, separators=(",", ":")) == first.to_json()
    assert preview_envelope(first)["schema_version"] == "acquisition-execution-plan-preview.v1"
    assert [step["adapter"] for step in first.steps] == ["web_http"]
    assert first.to_dict()["steps"][0]["verification_rules"] == [{"description": "Require a successful status.", "rule_id": "status-ok"}]


def test_scope_fingerprint_excludes_all_acquisition_authority_but_tracks_scope_and_budgets():
    scope, profile, skill, registry = inputs()
    baseline = compute_semantic_scope_fingerprint(scope)
    assert compute_semantic_scope_fingerprint(replace(scope, fetch_mode="browser", fetch_config_json={"x": 1})) == baseline
    assert compile_acquisition_execution_plan(scope, profile, skill, registry).scope_fingerprint == baseline
    changed_profile = profile.model_copy(update={"quality_gates": profile.quality_gates.model_copy(update={"min_words": 999})})
    assert compile_acquisition_execution_plan(scope, changed_profile, skill, registry).scope_fingerprint == baseline
    assert compute_semantic_scope_fingerprint(replace(scope, max_pages=26)) != baseline
    assert compute_semantic_scope_fingerprint(replace(scope, allowed_page_prefixes=["/other"])) != baseline


@pytest.mark.parametrize(("field", "value"), [("fetch_mode", "browser"), ("fetch_config_json", {"render": True})])
def test_fetch_authority_changes_acquisition_but_not_scope_fingerprint(field, value):
    scope, profile, skill, registry = inputs()
    changed = replace(scope, **{field: value})
    original = compile_acquisition_execution_plan(scope, profile, skill, registry)
    updated = compile_acquisition_execution_plan(changed, profile, skill, registry)
    assert updated.scope_fingerprint == original.scope_fingerprint
    assert updated.acquisition_fingerprint != original.acquisition_fingerprint


def test_plan_nested_authority_is_deeply_immutable_and_serialization_stays_consistent():
    scope, profile, skill, registry = inputs()
    plan = compile_acquisition_execution_plan(scope, profile, skill, registry)
    wire = plan.to_json()
    with pytest.raises(TypeError):
        plan.quality_gates["min_words"] = 1
    with pytest.raises(TypeError):
        plan.limits["stdout_bytes"] = 1
    with pytest.raises(TypeError):
        plan.steps[0]["limits"]["stdout_bytes"] = 1
    with pytest.raises((AttributeError, TypeError)):
        plan.steps[0]["required_capabilities"].append("evil")
    assert plan.to_json() == wire


@pytest.mark.parametrize("url", [
    "ftp://example.com/news", "//example.com/news", "https://user:pass@example.com/news",
    "https://example.com/ bad", "https://example.com/\u0001bad",
])
def test_governed_scope_urls_require_safe_absolute_http_urls(url):
    scope, profile, skill, registry = inputs()
    with pytest.raises(AcquisitionExecutionPlanError) as caught:
        compile_acquisition_execution_plan(replace(scope, seed_url=url), profile, skill, registry)
    assert caught.value.code == "scope.url_invalid"


@pytest.mark.parametrize("value", ["1", True, float("nan"), float("inf")])
def test_resource_limits_reject_coercion_and_non_finite_values(value):
    with pytest.raises(ValueError):
        AcquisitionResourceLimits(timeout_seconds=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_nested_profile_authority_is_structured_failure(value):
    scope, profile, skill, registry = inputs()
    profile = profile.model_copy(update={"adapters": [AcquisitionAdapterConfig(adapter="web_http", config={"bad": value})]})
    with pytest.raises(AcquisitionExecutionPlanError) as caught:
        compile_acquisition_execution_plan(scope, profile, skill, registry)
    assert caught.value.code == "authority.non_finite"
    with pytest.raises(ValueError):
        canonical_json({"bad": value})


@pytest.mark.parametrize("value", [set(["b", "a"]), frozenset(["b", "a"]), b"secret", object()])
def test_nonportable_fetch_authority_is_structured_failure(value):
    scope, _, _, _ = inputs()
    scope.based_on = {}
    scope.fetch_config_json = {"value": value}
    with pytest.raises(AcquisitionExecutionPlanError) as caught:
        compile_acquisition_execution_plan(scope, None, None, None)
    assert caught.value.code == "authority.non_portable"
    assert str(value) not in str(caught.value)


def test_mixed_integer_and_string_authority_keys_are_rejected_without_collision():
    scope, _, _, _ = inputs()
    scope.based_on = {}
    scope.fetch_config_json = {1: "first", "1": "second"}
    with pytest.raises(AcquisitionExecutionPlanError) as caught:
        compile_acquisition_execution_plan(scope, None, None, None)
    assert caught.value.code == "authority.non_portable"


def test_portable_authority_failure_is_hash_seed_independent():
    code = """from web_listening.blocks.acquisition_execution_plan import canonical_json, AcquisitionExecutionPlanError
try:
 canonical_json({'value': {'b', 'a'}})
except AcquisitionExecutionPlanError as exc:
 print(exc.code + ':' + str(exc))
"""
    outputs = []
    for seed in ("1", "2"):
        environment = dict(os.environ, PYTHONHASHSEED=seed)
        outputs.append(subprocess.check_output([sys.executable, "-c", code], text=True, env=environment))
    assert outputs[0] == outputs[1] == "authority.non_portable:governed acquisition authority contains a non-portable value\n"


def test_complete_profile_chain_requires_explicit_enabled_unambiguous_mapping():
    scope, profile, skill, registry = inputs()
    missing = profile.model_copy(update={"fallback_order": ["rss"]})
    with pytest.raises(AcquisitionExecutionPlanError, match="explicitly present and enabled"):
        compile_acquisition_execution_plan(scope, missing, skill, registry)
    unmapped = profile.model_copy(update={"recipe_mappings": []})
    with pytest.raises(AcquisitionExecutionPlanError, match="mapping"):
        compile_acquisition_execution_plan(scope, unmapped, skill, registry)


def test_complete_default_and_fallback_chain_is_frozen_in_order():
    scope, profile, skill, registry = inputs()
    rss_recipe = SiteSkillRecipe(recipe_id="news-rss", executor_id="rss", profile_ref="profiles/default.yaml",
        entrypoint="scripts/recipe.py", required_capabilities=("rss_read",),
        verification_rules=(VerificationRule(rule_id="status-ok", description="Require a successful status."),))
    manifest = skill.manifest.model_copy(update={
        "executors": skill.manifest.executors + (SiteSkillExecutor(executor_id="rss"),),
        "recipes": skill.manifest.recipes + (rss_recipe,),
    })
    skill = ResolvedSiteSkill(manifest, skill.package_sha256, skill.script_sha256)
    profile = profile.model_copy(update={
        "fallback_order": ["rss"],
        "adapters": profile.adapters + [AcquisitionAdapterConfig(adapter="rss")],
        "recipe_mappings": profile.recipe_mappings + [AcquisitionRecipeMapping(adapter="rss", recipe_id="news-rss")],
    })
    plan = compile_acquisition_execution_plan(scope, profile, skill, registry)
    assert [(step["position"], step["adapter"], step["recipe_id"]) for step in plan.steps] == [
        (0, "web_http", "news-http"), (1, "rss", "news-rss")]


def test_resource_limit_overrides_are_bounded_and_effective():
    scope, profile, skill, registry = inputs()
    profile = profile.model_copy(update={"resource_limits": AcquisitionResourceLimits(timeout_seconds=10, stdout_bytes=1000, stderr_bytes=500)})
    plan = compile_acquisition_execution_plan(scope, profile, skill, registry)
    assert plan.steps[0]["limits"] == {"timeout_seconds": 10.0, "stdout_bytes": 1000, "stderr_bytes": 500}
    with pytest.raises(ValueError):
        AcquisitionResourceLimits(timeout_seconds=float("inf"))


def test_preview_registry_uses_product_versions_and_strict_limit_types():
    assert default_preview_registry().metadata["browseract"].version == "1.0.6"
    with pytest.raises(ValueError):
        ExecutorRegistry.preview({"web_http": ExecutorMetadata("web_http", "1", frozenset({"http_get"}), float("inf"), 1, 1)})
    with pytest.raises(ValueError):
        ExecutorRegistry.preview({"web_http": ExecutorMetadata("web_http", "1", frozenset({"http_get"}), 1, True, 1)})
    for capabilities in ({""}, {1}):
        with pytest.raises(ValueError, match="capabilities must be non-empty strings"):
            ExecutorRegistry.preview({"web_http": ExecutorMetadata("web_http", "1", capabilities, 1, 1, 1)})


@pytest.mark.parametrize("value", ["false", 0, 1, None])
@pytest.mark.parametrize("entry_path", ["runtime", "preview"])
def test_registry_requires_exact_boolean_authorization_metadata(value, entry_path):
    metadata = ExecutorMetadata("web_http", "1", frozenset({"http_get"}), 1, 1, 1, value)
    with pytest.raises(ValueError, match="must be a boolean"):
        if entry_path == "preview":
            ExecutorRegistry.preview({"web_http": metadata})
        else:
            class Executor:
                executor_id = "web_http"
            ExecutorRegistry({"web_http": Executor()}, metadata={"web_http": metadata})


@pytest.mark.parametrize("value", [False, True])
def test_registry_accepts_boolean_authorization_metadata(value):
    registry = ExecutorRegistry.preview({
        "web_http": ExecutorMetadata("web_http", "1", frozenset({"http_get"}), 1, 1, 1, value)})
    assert registry.metadata["web_http"].requires_authorized_access is value


@pytest.mark.parametrize("entry_path", ["runtime", "preview"])
def test_registry_detaches_mutable_capabilities_and_preserves_plan_fingerprint(entry_path):
    scope, profile, skill, _ = inputs()
    capabilities = {"http_get"}
    metadata = ExecutorMetadata("web_http", "1.0.0", capabilities, 30.0, 4 * 1024 * 1024, 64 * 1024)
    if entry_path == "preview":
        registry = ExecutorRegistry.preview({"web_http": metadata})
    else:
        class Executor:
            executor_id = "web_http"

        registry = ExecutorRegistry({"web_http": Executor()}, metadata={"web_http": metadata})

    first = compile_acquisition_execution_plan(scope, profile, skill, registry)
    capabilities.add("caller_mutation")
    second = compile_acquisition_execution_plan(scope, profile, skill, registry)

    assert registry.metadata["web_http"] is not metadata
    assert registry.metadata["web_http"].capabilities == frozenset({"http_get"})
    assert first.acquisition_fingerprint == second.acquisition_fingerprint


def test_trusted_executor_authorization_is_enforced_for_every_step():
    scope, profile, skill, registry = inputs()
    metadata = dict(registry.metadata)
    original = metadata["web_http"]
    metadata["web_http"] = replace(original, requires_authorized_access=True)
    with pytest.raises(AcquisitionExecutionPlanError) as caught:
        compile_acquisition_execution_plan(scope, profile, skill, ExecutorRegistry.preview(metadata))
    assert caught.value.code == "safety.executor_authorization_required"


def test_trusted_executor_authorization_flag_is_fingerprinted_when_authorized():
    scope, profile, skill, registry = inputs()
    profile = profile.model_copy(update={"safety": AcquisitionSafetyPolicy(
        allowed_domains=["example.com"], require_authorized_access=True)})
    baseline = compile_acquisition_execution_plan(scope, profile, skill, registry)
    metadata = dict(registry.metadata)
    metadata["web_http"] = replace(metadata["web_http"], requires_authorized_access=True)
    changed = compile_acquisition_execution_plan(scope, profile, skill, ExecutorRegistry.preview(metadata))
    assert changed.acquisition_fingerprint != baseline.acquisition_fingerprint
    assert changed.steps[0]["requires_authorized_access"] is True


def test_profile_domain_subset_and_host_authority():
    scope, profile, skill, registry = inputs()
    narrower = profile.model_copy(update={"safety": AcquisitionSafetyPolicy(allowed_domains=["sub.example.com"])})
    with pytest.raises(AcquisitionExecutionPlanError, match="scope hosts"):
        compile_acquisition_execution_plan(scope, narrower, skill, registry)
    broader = profile.model_copy(update={"safety": AcquisitionSafetyPolicy(allowed_domains=["example.com", "evil.test"])})
    with pytest.raises(AcquisitionExecutionPlanError, match="subset"):
        compile_acquisition_execution_plan(scope, broader, skill, registry)


@pytest.mark.parametrize("key", [
    "acquisition_profile_id", "site_skill_version", "site_skill_package_sha256",
    "site_skill_recipe_id", "site_skill_script_sha256", "executor_version",
])
def test_exact_governed_binding_mismatch_fails_closed(key):
    scope, profile, skill, registry = inputs()
    scope.based_on[key] = "wrong"
    with pytest.raises((AcquisitionExecutionPlanError, ValueError)):
        compile_acquisition_execution_plan(scope, profile, skill, registry)


def test_legacy_mode_has_exactly_one_warning_and_partial_rejected():
    scope, _, _, _ = inputs()
    scope.based_on = {}
    plan = compile_acquisition_execution_plan(scope, None, None, None)
    assert plan.mode == "legacy_compatibility"
    assert len(plan.warnings) == 1
    scope.based_on = {"site_skill_version": "1.0.0"}
    with pytest.raises(AcquisitionExecutionPlanError) as caught:
        compile_acquisition_execution_plan(scope, None, None, None)
    assert caught.value.code == "bindings.partial"


def test_fixture_matches_contract():
    scope, profile, skill, registry = inputs()
    actual = preview_envelope(compile_acquisition_execution_plan(scope, profile, skill, registry))
    fixture = json.loads((Path(__file__).parents[1] / "docs/testing/fixtures/acquisition-execution-plan-v1.sample.json").read_text())
    assert canonical_json(actual) == canonical_json(fixture)
