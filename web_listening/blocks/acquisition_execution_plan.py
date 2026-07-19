"""Pure deterministic compiler for acquisition-execution-plan.v1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from web_listening.blocks.acquisition_profile import AcquisitionProfile
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, compute_semantic_scope_fingerprint
from web_listening.executors.registry import ExecutorRegistry
from web_listening.site_skill_registry import ResolvedSiteSkill

SCHEMA_VERSION = "acquisition-execution-plan.v1"
PREVIEW_SCHEMA_VERSION = "acquisition-execution-plan-preview.v1"
_BINDINGS = (
    "acquisition_profile_id", "site_skill_version", "site_skill_package_sha256",
    "site_skill_recipe_id", "site_skill_script_sha256", "executor_version",
)


class AcquisitionExecutionPlanError(ValueError):
    def __init__(self, code: str, message: str, *, field: str = ".") -> None:
        super().__init__(message)
        self.code, self.field = code, field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "message": str(self)}


@dataclass(frozen=True, slots=True)
class AcquisitionExecutionPlan:
    schema_version: str
    mode: str
    site_key: str
    scope_fingerprint_algorithm: str
    scope_fingerprint: str
    acquisition_fingerprint_algorithm: str
    acquisition_fingerprint: str
    profile_id: str | None
    site_skill_id: str | None
    site_skill_version: str | None
    site_skill_package_sha256: str | None
    recipe_id: str | None
    executor_id: str | None
    executor_version: str | None
    entrypoint: str | None
    script_sha256: str | None
    required_capabilities: tuple[str, ...]
    quality_gates: dict[str, Any]
    limits: dict[str, Any]
    scope_budgets: dict[str, int]
    steps: tuple[dict[str, Any], ...]
    warnings: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {name: _thaw(getattr(self, name)) for name in self.__dataclass_fields__}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())


def canonical_json(value: object) -> str:
    portable = _validate_portable_json(value)
    return json.dumps(_thaw(portable), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _validate_portable_json(value: Any) -> Any:
    """Validate acquisition authority without coercing caller-controlled values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("authority.non_finite", "governed acquisition authority contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        checked: dict[str, Any] = {}
        for key, child in value.items():
            if type(key) is not str:
                _fail("authority.non_portable", "governed acquisition authority must use string mapping keys")
            checked[key] = _validate_portable_json(child)
        return checked
    if isinstance(value, (list, tuple)):
        return tuple(_validate_portable_json(child) for child in value)
    _fail("authority.non_portable", "governed acquisition authority contains a non-portable value")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _fail(code: str, message: str, field: str = ".") -> None:
    raise AcquisitionExecutionPlanError(code, message, field=field)


def _host(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _validate_scope_url(value: str, field: str) -> str:
    if value != value.strip() or any(character.isspace() or unicodedata.category(character).startswith("C") for character in value):
        _fail("scope.url_invalid", "scope URL must be a safe absolute http/https URL", field)
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        _fail("scope.url_invalid", "scope URL must be a safe absolute http/https URL", field)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        _fail("scope.url_invalid", "scope URL must be a safe absolute http/https URL", field)
    return parsed.hostname.lower().rstrip(".")


def _allowed(host: str, domains: set[str]) -> bool:
    return bool(host) and any(host == item or host.endswith("." + item) for item in domains)


def compile_acquisition_execution_plan(scope: MonitorScopePlan, profile: AcquisitionProfile | None,
                                       site_skill: ResolvedSiteSkill | None,
                                       registry: ExecutorRegistry | None) -> AcquisitionExecutionPlan:
    for key, value in scope.based_on.items():
        if type(key) is not str or (key in _BINDINGS and type(value) is not str):
            _fail("bindings.type_invalid", "governed acquisition binding keys and values must be strings", "based_on")
        if key in _BINDINGS and value != value.strip():
            _fail("bindings.value_invalid", "governed acquisition binding values must use exact canonical strings", f"based_on.{key}")
    values = {key: scope.based_on.get(key, "") for key in _BINDINGS}
    present = {key for key in _BINDINGS if key in scope.based_on}
    scope_fp = compute_semantic_scope_fingerprint(scope)
    budgets = {"max_depth": scope.max_depth, "max_files": scope.max_files, "max_pages": scope.max_pages}
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in budgets.values()):
        _fail("scope.budget_invalid", "scope budgets must be positive integers", "scope_budgets")
    if not present:
        base = {
            "mode": "legacy_compatibility", "site_key": scope.site_key,
            "scope_fingerprint": scope_fp, "scope_budgets": budgets,
            "fetch_mode": str(scope.fetch_mode).strip().lower(),
            "fetch_config_json": scope.fetch_config_json,
            "warning": "governed acquisition bindings are absent; existing bootstrap/run authority is unchanged",
        }
        base = _validate_portable_json(base)
        acquisition_fp = hashlib.sha256(canonical_json(base).encode()).hexdigest()
        return AcquisitionExecutionPlan(
            schema_version=SCHEMA_VERSION, mode="legacy_compatibility", site_key=scope.site_key,
            scope_fingerprint_algorithm="sha256:monitor-scope-semantic.v2", scope_fingerprint=scope_fp,
            acquisition_fingerprint_algorithm="sha256:acquisition-execution-plan.v1", acquisition_fingerprint=acquisition_fp,
            profile_id=None, site_skill_id=None, site_skill_version=None, site_skill_package_sha256=None,
            recipe_id=None, executor_id=None, executor_version=None, entrypoint=None, script_sha256=None,
            required_capabilities=(), quality_gates=_freeze({}), limits=_freeze({}), scope_budgets=_freeze(budgets), steps=(),
            warnings=_freeze([{"code": "legacy_compatibility", "message": base["warning"]}]))
    if len(present) != len(_BINDINGS):
        _fail("bindings.partial", "governed acquisition bindings must be all present or all absent", "based_on")
    if profile is None or site_skill is None or registry is None:
        _fail("inputs.missing", "governed compilation requires profile, resolved Site Skill, and executor registry")
    manifest = site_skill.manifest
    if len({scope.site_key, profile.site_key, manifest.site_key}) != 1:
        _fail("site_key.mismatch", "scope, profile, and Site Skill site_key must match", "site_key")
    exact = {
        "acquisition_profile_id": profile.profile_id,
        "site_skill_version": manifest.version,
        "site_skill_package_sha256": site_skill.package_sha256,
    }
    for key, expected in exact.items():
        if values[key] != expected:
            _fail(f"{key}.mismatch", f"{key} does not match resolved input", f"based_on.{key}")
    skill_domains = {item.lower().rstrip(".") for item in manifest.allowed_domains}
    profile_domains = {item.lower().rstrip(".") for item in profile.safety.allowed_domains}
    if not profile_domains or not all(_allowed(domain, skill_domains) for domain in profile_domains):
        _fail("domains.profile_broader", "profile domains must be a non-empty subset of Site Skill domains", "safety.allowed_domains")
    for url_field in ("seed_url", "homepage_url"):
        if not _allowed(_validate_scope_url(getattr(scope, url_field), url_field), profile_domains):
            _fail("scope.host_not_allowed", "scope hosts must be allowed by profile and Site Skill", url_field)
    adapters = {item.adapter: item for item in profile.adapters}
    ordered: list[str] = []
    for adapter_id in (profile.default_adapter, *profile.fallback_order):
        if adapter_id in ordered:
            continue
        declaration = adapters.get(adapter_id)
        if declaration is None or not declaration.enabled:
            _fail("adapter.not_enabled", "every planned adapter must be explicitly present and enabled", "adapters")
        ordered.append(adapter_id)
    mappings: dict[str, list[str]] = {}
    for mapping in profile.recipe_mappings:
        mappings.setdefault(mapping.adapter, []).append(mapping.recipe_id)
    enabled_recipes = [item for item in manifest.recipes if item.enabled]
    quality = profile.quality_gates.model_dump(mode="json")
    _validate_portable_json(profile.model_dump(mode="python"))
    if quality["min_words"] < 0 or quality["min_links"] < 0 or quality["min_document_links"] < 0:
        _fail("quality.invalid", "quality gate counts must be non-negative", "quality_gates")
    steps: list[dict[str, Any]] = []
    for position, adapter_id in enumerate(ordered):
        recipe_ids = mappings.get(adapter_id, [])
        if len(recipe_ids) != 1:
            _fail("recipe.mapping_invalid", "each planned adapter requires exactly one explicit recipe mapping", "recipe_mappings")
        candidates = [item for item in enabled_recipes if item.recipe_id == recipe_ids[0] and item.executor_id == adapter_id]
        if len(candidates) != 1:
            _fail("recipe.mapping_invalid", "recipe mapping must identify one enabled recipe for the adapter", "recipe_mappings")
        recipe = candidates[0]
        declarations = [item for item in manifest.executors if item.executor_id == adapter_id and item.enabled]
        if len(declarations) != 1:
            _fail("executor.declaration_invalid", "recipe requires one enabled Site Skill executor declaration", "executors")
        metadata = registry.metadata.get(recipe.executor_id)
        if metadata is None:
            _fail("executor.metadata_missing", "trusted immutable executor metadata is required", "executor_id")
        required = set(recipe.required_capabilities)
        if not required.issubset(metadata.capabilities):
            _fail("executor.capabilities_missing", "executor lacks required recipe capabilities", "required_capabilities")
        script_digest = site_skill.script_sha256.get(recipe.entrypoint)
        if not script_digest:
            _fail("script.digest_missing", "recipe entrypoint requires an exact script digest", "entrypoint")
        if metadata.requires_authorized_access and not profile.safety.require_authorized_access:
            _fail("safety.executor_authorization_required", "executor requires authorized-access approval", "safety.require_authorized_access")
        if adapter_id == "cloakbrowser" and not profile.safety.permits_cloakbrowser:
            _fail("safety.cloakbrowser_authorization_required", "cloakbrowser requires stealth and authorized-access approval", "safety")
        if adapter_id == "browseract" and not profile.safety.permits_browseract:
            _fail("safety.browseract_authorization_required", "browseract requires its existing authorization approval", "safety")
        requested = profile.resource_limits.model_dump(exclude_none=True)
        requested.update(profile.adapter_resource_limits.get(adapter_id, profile.resource_limits.__class__()).model_dump(exclude_none=True))
        limits = {"timeout_seconds": metadata.timeout_seconds, "stdout_bytes": metadata.stdout_bytes, "stderr_bytes": metadata.stderr_bytes}
        for name, value in requested.items():
            if value > limits[name]:
                _fail("limits.exceeds_executor", "profile resource limit exceeds trusted executor ceiling", f"resource_limits.{name}")
            limits[name] = value
        step = {
            "position": position, "adapter": adapter_id, "recipe_id": recipe.recipe_id,
            "executor_id": recipe.executor_id, "executor_version": metadata.version,
            "entrypoint": recipe.entrypoint, "script_sha256": script_digest,
            "required_capabilities": sorted(required), "executor_capabilities": sorted(metadata.capabilities),
            "requires_authorized_access": metadata.requires_authorized_access,
            "verification_rules": [item.model_dump(mode="json") for item in recipe.verification_rules],
            "limits": limits,
        }
        steps.append(step)
    first = steps[0]
    if values["site_skill_recipe_id"] != first["recipe_id"]:
        _fail("recipe.binding_mismatch", "default recipe mapping does not match governed binding", "based_on.site_skill_recipe_id")
    if values["site_skill_script_sha256"] != first["script_sha256"]:
        _fail("script.digest_mismatch", "default recipe script digest does not match governed binding", "based_on.site_skill_script_sha256")
    if values["executor_version"] != first["executor_version"]:
        _fail("executor.version_mismatch", "default executor version does not match governed binding", "based_on.executor_version")
    limits = first["limits"]
    authority = {
        "site_key": scope.site_key, "scope_fingerprint": scope_fp, "profile_id": profile.profile_id,
        "fetch_mode": str(scope.fetch_mode).strip().lower(), "fetch_config_json": scope.fetch_config_json,
        "profile": profile.model_dump(mode="json", exclude={"generated_at", "notes"}),
        "site_skill_id": manifest.skill_id, "site_skill_version": manifest.version,
        "site_skill_package_sha256": site_skill.package_sha256, "steps": steps,
        "quality_gates": quality, "scope_budgets": budgets,
    }
    authority = _validate_portable_json(authority)
    acquisition_fp = hashlib.sha256(canonical_json(authority).encode()).hexdigest()
    return AcquisitionExecutionPlan(
        schema_version=SCHEMA_VERSION, mode="governed", site_key=scope.site_key,
        scope_fingerprint_algorithm="sha256:monitor-scope-semantic.v2", scope_fingerprint=scope_fp,
        acquisition_fingerprint_algorithm="sha256:acquisition-execution-plan.v1", acquisition_fingerprint=acquisition_fp,
        profile_id=profile.profile_id, site_skill_id=manifest.skill_id, site_skill_version=manifest.version,
        site_skill_package_sha256=site_skill.package_sha256, recipe_id=first["recipe_id"], executor_id=first["executor_id"],
        executor_version=first["executor_version"], entrypoint=first["entrypoint"], script_sha256=first["script_sha256"],
        required_capabilities=tuple(first["required_capabilities"]), quality_gates=_freeze(quality), limits=_freeze(limits),
        scope_budgets=_freeze(budgets), steps=_freeze(steps), warnings=())


def preview_envelope(plan: AcquisitionExecutionPlan) -> dict[str, Any]:
    return {"schema_version": PREVIEW_SCHEMA_VERSION, "ok": True, "plan": plan.to_dict(), "error": None}


def failure_envelope(error: AcquisitionExecutionPlanError) -> dict[str, Any]:
    return {"schema_version": PREVIEW_SCHEMA_VERSION, "ok": False, "plan": None, "error": error.to_dict()}
