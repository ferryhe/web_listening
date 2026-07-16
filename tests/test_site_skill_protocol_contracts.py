from __future__ import annotations

import inspect
import json
import math
from hashlib import sha256
from copy import copy, deepcopy
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from web_listening.contracts import (
    AcquisitionAttempt,
    CaptureContent,
    CaptureRequest,
    CaptureResult,
    SiteSkill,
    SiteSkillExecutor,
)
from web_listening.contracts._protocol import (
    ImmutableJsonMapping,
    validate_portable_json,
)


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "docs" / "testing" / "fixtures"


def load_fixture(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def validate_json(model, payload: dict):
    return model.model_validate_json(json.dumps(payload))


def duplicate_json_key(payload: dict, key: str, duplicate_value) -> str:
    serialized = json.dumps(payload)
    original = json.dumps(key) + ":"
    replacement = json.dumps(key) + ":" + json.dumps(duplicate_value) + "," + original
    assert original in serialized
    return serialized.replace(original, replacement, 1)


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("site-skill-v1.sample.json", SiteSkill),
        ("capture-request-v1.sample.json", CaptureRequest),
        ("capture-result-v1.sample.json", CaptureResult),
        ("acquisition-attempt-v2.sample.json", AcquisitionAttempt),
    ],
)
def test_canonical_json_fixtures_validate_and_round_trip(filename, model):
    fixture = (FIXTURE_DIR / filename).read_text(encoding="utf-8")
    parsed = model.model_validate_json(fixture)
    assert json.loads(parsed.model_dump_json()) == json.loads(fixture)


def test_canonical_fixtures_share_exact_site_skill_byte_digest_lineage():
    expected_digest = "76bd33212578397c7b056b690ac316818ddc6294b4a14e560111e603fcc41ada"
    skill_bytes = (FIXTURE_DIR / "site-skill-v1.sample.json").read_bytes()
    assert sha256(skill_bytes).hexdigest() == expected_digest

    request = load_fixture("capture-request-v1.sample.json")
    result = load_fixture("capture-result-v1.sample.json")
    attempt = load_fixture("acquisition-attempt-v2.sample.json")
    assert request["site_skill_digest"] == expected_digest
    assert result["site_skill_digest"] == expected_digest
    assert attempt["request"]["site_skill_digest"] == expected_digest
    assert attempt["result"]["site_skill_digest"] == expected_digest


@pytest.mark.parametrize(
    ("filename", "model", "wrong_version"),
    [
        ("site-skill-v1.sample.json", SiteSkill, "site-skill.v2"),
        ("capture-request-v1.sample.json", CaptureRequest, "capture-request.v2"),
        ("capture-result-v1.sample.json", CaptureResult, "capture-result.v2"),
        (
            "acquisition-attempt-v2.sample.json",
            AcquisitionAttempt,
            "acquisition-attempt.v1",
        ),
    ],
)
def test_wrong_schema_versions_are_rejected(filename, model, wrong_version):
    payload = load_fixture(filename)
    payload["schema_version"] = wrong_version
    with pytest.raises(ValidationError):
        validate_json(model, payload)


@pytest.mark.parametrize(
    ("filename", "model", "governed_key", "duplicate_value"),
    [
        ("site-skill-v1.sample.json", SiteSkill, "skill_id", "duplicate-skill"),
        (
            "capture-request-v1.sample.json",
            CaptureRequest,
            "request_id",
            "duplicate-request",
        ),
        (
            "capture-result-v1.sample.json",
            CaptureResult,
            "request_id",
            "duplicate-request",
        ),
        (
            "acquisition-attempt-v2.sample.json",
            AcquisitionAttempt,
            "attempt_id",
            "duplicate-attempt",
        ),
    ],
)
@pytest.mark.parametrize(
    "input_type",
    [
        pytest.param(str, id="str"),
        pytest.param(lambda value: value.encode(), id="bytes"),
        pytest.param(lambda value: bytearray(value.encode()), id="bytearray"),
    ],
)
def test_contract_wire_json_rejects_duplicate_top_level_governed_keys(
    filename, model, governed_key, duplicate_value, input_type
):
    wire_json = duplicate_json_key(load_fixture(filename), governed_key, duplicate_value)
    with pytest.raises(
        ValueError,
        match=rf"duplicate JSON object key: ['\"]{governed_key}['\"]",
    ):
        model.model_validate_json(input_type(wire_json))


@pytest.mark.parametrize(
    ("filename", "model", "container_key", "nested_json", "duplicate_key"),
    [
        (
            "site-skill-v1.sample.json",
            SiteSkill,
            "metadata",
            '{"layer":{"region":"first","region":"second"}}',
            "region",
        ),
        (
            "capture-request-v1.sample.json",
            CaptureRequest,
            "config",
            '{"layers":[{"mode":"first","mode":"second"}]}',
            "mode",
        ),
        (
            "capture-result-v1.sample.json",
            CaptureResult,
            "error",
            '{"code":"blocked","message":"Blocked","metadata":{"detail":{"status":"first","status":"second"}}}',
            "status",
        ),
    ],
)
@pytest.mark.parametrize(
    "input_type",
    [
        pytest.param(str, id="str"),
        pytest.param(lambda value: value.encode(), id="bytes"),
        pytest.param(lambda value: bytearray(value.encode()), id="bytearray"),
    ],
)
def test_contract_wire_json_rejects_duplicate_keys_at_any_nesting_level(
    filename, model, container_key, nested_json, duplicate_key, input_type
):
    payload = load_fixture(filename)
    if filename == "capture-result-v1.sample.json":
        payload["state"] = "failed"
        payload["content"] = None
        payload["error"] = None
    serialized = json.dumps(payload)
    marker = json.dumps(container_key) + ": " + json.dumps(payload[container_key])
    wire_json = serialized.replace(marker, json.dumps(container_key) + ": " + nested_json, 1)
    assert wire_json != serialized
    with pytest.raises(
        ValueError,
        match=rf"duplicate JSON object key: ['\"]{duplicate_key}['\"]",
    ):
        model.model_validate_json(input_type(wire_json))


def test_contract_type_adapter_preserves_strict_python_validation():
    payload = load_fixture("capture-request-v1.sample.json")
    payload["request_id"] = 123
    with pytest.raises(ValidationError):
        TypeAdapter(CaptureRequest).validate_python(payload)


@pytest.mark.parametrize(
    "input_type",
    [
        pytest.param(str, id="str"),
        pytest.param(lambda value: value.encode(), id="bytes"),
        pytest.param(lambda value: bytearray(value.encode()), id="bytearray"),
    ],
)
def test_duplicate_json_key_rejection_survives_forced_model_rebuild(input_type):
    CaptureRequest.model_rebuild(force=True)
    wire_json = duplicate_json_key(
        load_fixture("capture-request-v1.sample.json"),
        "request_id",
        "duplicate-request",
    )
    with pytest.raises(ValueError, match="duplicate JSON object key: 'request_id'"):
        CaptureRequest.model_validate_json(input_type(wire_json))


@pytest.mark.parametrize(
    "partial_mode", [True, "on", "trailing-strings", False, "off"]
)
def test_contract_wire_json_explicitly_rejects_partial_validation(partial_mode):
    truncated_duplicate_json = (
        '{"request_id":"first","request_id":"last-value-must-not-win"'
    )
    with pytest.raises(TypeError, match="experimental_allow_partial is not supported"):
        CaptureRequest.model_validate_json(
            truncated_duplicate_json,
            experimental_allow_partial=partial_mode,
        )


def test_importing_contracts_does_not_modify_ordinary_type_adapter_json_behavior():
    signature = inspect.signature(TypeAdapter.validate_json)
    assert TypeAdapter.validate_json.__module__.startswith("pydantic")
    adapter = TypeAdapter(dict[str, int])
    assert adapter.validate_json('{"value":1,"value":2}') == {"value": 2}
    if "experimental_allow_partial" in signature.parameters:
        assert adapter.validate_json(
            '{"value":1} trailing', experimental_allow_partial=True
        ) == {"value": 1}


def test_default_type_adapter_python_behavior_remains_untouched():
    assert TypeAdapter(int).validate_python("1") == 1


@pytest.mark.parametrize("extra", ["allow", "ignore"])
def test_contract_wire_json_rejects_extra_policy_weakening(extra):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["unknown"] = True
    with pytest.raises(TypeError, match="extra must be None or 'forbid'"):
        CaptureRequest.model_validate_json(json.dumps(payload), extra=extra)


@pytest.mark.parametrize("extra", [None, "forbid"])
def test_contract_wire_json_accepts_compatible_extra_policy(extra):
    request = CaptureRequest.model_validate_json(
        json.dumps(load_fixture("capture-request-v1.sample.json")), extra=extra
    )
    assert request.request_id == "request-example-news-001"


def test_contract_wire_json_rejects_strictness_weakening():
    payload = load_fixture("capture-request-v1.sample.json")
    payload["request_id"] = 123
    with pytest.raises(TypeError, match="strict=False is not supported"):
        CaptureRequest.model_validate_json(json.dumps(payload), strict=False)
    with pytest.raises(ValidationError):
        CaptureRequest.model_validate_json(json.dumps(payload), strict=True)


@pytest.mark.parametrize("extra", ["allow", "ignore"])
def test_contract_python_validation_rejects_extra_policy_weakening(extra):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["unknown"] = True
    with pytest.raises(TypeError, match="extra must be None or 'forbid'"):
        CaptureRequest.model_validate(payload, extra=extra)


@pytest.mark.parametrize(
    "validation_kwargs",
    [
        pytest.param({}, id="defaults"),
        pytest.param({"strict": True}, id="strict-true"),
        pytest.param({"extra": "forbid"}, id="extra-forbid"),
        pytest.param(
            {"strict": True, "extra": "forbid"}, id="strict-true-extra-forbid"
        ),
    ],
)
def test_contract_python_validation_accepts_compatible_policy(validation_kwargs):
    payload = validate_json(
        CaptureRequest, load_fixture("capture-request-v1.sample.json")
    ).model_dump(mode="python")
    request = CaptureRequest.model_validate(payload, **validation_kwargs)
    assert request.request_id == "request-example-news-001"


def test_contract_python_validation_rejects_strictness_weakening():
    payload = load_fixture("capture-request-v1.sample.json")
    payload["request_id"] = 123
    with pytest.raises(TypeError, match="strict=False is not supported"):
        CaptureRequest.model_validate(payload, strict=False)
    with pytest.raises(ValidationError):
        CaptureRequest.model_validate(payload, strict=True)


def test_contract_models_are_strict_frozen_and_forbid_coercion_or_unknown_fields():
    payload = load_fixture("capture-request-v1.sample.json")
    payload["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_json(CaptureRequest, payload)
    payload.pop("unknown")
    payload["request_id"] = 123
    with pytest.raises(ValidationError):
        validate_json(CaptureRequest, payload)
    request = validate_json(
        CaptureRequest, load_fixture("capture-request-v1.sample.json")
    )
    with pytest.raises(ValidationError, match="frozen"):
        request.request_id = "changed"


def test_nested_contract_collections_are_frozen_and_remain_json_serializable():
    payload = load_fixture("capture-request-v1.sample.json")
    payload["config"] = {
        "headers": {"accept": "text/html"},
        "fallbacks": ["web_http"],
    }
    request = validate_json(CaptureRequest, payload)
    with pytest.raises(TypeError):
        request.config["apiKey"] = "bypass-validation"
    with pytest.raises(TypeError):
        dict.__setitem__(request.config, "safe", "bypass")
    with pytest.raises((AttributeError, TypeError)):
        request.config.update({"safe": "bypass"})
    with pytest.raises(TypeError):
        request.config["headers"]["accept"] = "text/plain"
    with pytest.raises((AttributeError, TypeError)):
        request.config["fallbacks"].append("unsafe")
    with pytest.raises(TypeError):
        list.append(request.config["fallbacks"], "unsafe")
    with pytest.raises(TypeError):
        list.__setitem__(request.config["fallbacks"], 0, "unsafe")
    assert validate_json(CaptureRequest, request.model_dump(mode="json")) == request
    assert request.model_dump(mode="json")["config"] == payload["config"]
    assert json.loads(request.model_dump_json())["config"] == payload["config"]
    assert CaptureRequest.model_json_schema()["properties"]["config"]["type"] == "object"
    copied = request.model_copy()
    assert copied == request
    assert copied.config is not request.config

    skill = validate_json(SiteSkill, load_fixture("site-skill-v1.sample.json"))
    with pytest.raises((AttributeError, TypeError)):
        skill.allowed_domains.append("bypass.example")
    with pytest.raises((AttributeError, TypeError)):
        skill.recipes[0].required_capabilities.append("bypass")


def test_omitted_json_defaults_are_validated_and_deeply_immutable():
    request_payload = load_fixture("capture-request-v1.sample.json")
    request_payload.pop("config", None)
    request_payload.pop("metadata", None)
    request = validate_json(CaptureRequest, request_payload)
    assert request.config == request.metadata == {}
    with pytest.raises(TypeError):
        dict.__setitem__(request.config, "safe", True)

    skill_payload = load_fixture("site-skill-v1.sample.json")
    skill_payload.pop("metadata", None)
    skill_payload["executors"][0].pop("config", None)
    skill = validate_json(SiteSkill, skill_payload)
    with pytest.raises(TypeError):
        dict.__setitem__(skill.metadata, "safe", True)
    with pytest.raises(TypeError):
        dict.__setitem__(skill.executors[0].config, "safe", True)


def test_immutable_json_mapping_is_frozen_and_cannot_leak_injection():
    source = {"nested": {"safe": "value"}}
    request_payload = load_fixture("capture-request-v1.sample.json")
    request_payload["config"] = source
    request = validate_json(CaptureRequest, request_payload)

    original_json = request.model_dump_json()
    with pytest.raises(AttributeError):
        request.config._ImmutableJsonMapping__data = {"safe": "changed"}
    with pytest.raises(AttributeError):
        del request.config._ImmutableJsonMapping__data
    assert request.model_dump_json() == original_json
    source["apiKey"] = "must-not-serialize"
    source["nested"]["clientsecret"] = "must-not-serialize"
    serialized = request.model_dump_json()
    assert "must-not-serialize" not in serialized
    assert "apiKey" not in serialized
    assert "clientsecret" not in serialized

    mapping = ImmutableJsonMapping({"a": 1})
    assert mapping == {"a": 1}
    assert dict(mapping) == {"a": 1}
    assert "a" in mapping
    assert "a" in mapping.keys()
    assert ("a", 1) not in mapping
    assert ("a", 1) not in mapping.keys()
    assert mapping != (("a", 1),)
    assert list(mapping.items()) == [("a", 1)]
    assert copy(mapping) == mapping
    assert copy(mapping) is mapping
    assert deepcopy(mapping) == mapping
    assert deepcopy(mapping) is mapping


def test_immutable_json_mapping_rejects_duplicate_tuple_pair_keys():
    with pytest.raises(ValueError, match="keys must be unique"):
        ImmutableJsonMapping((("a", 1), ("a", 2)))


def test_large_immutable_json_mapping_serialization_does_not_use_key_lookups(
    monkeypatch,
):
    expected = {f"field-{index}": index for index in range(10_000)}
    payload = load_fixture("capture-request-v1.sample.json")
    payload["config"] = expected
    request = validate_json(CaptureRequest, payload)

    def fail_lookup(self, key):
        raise AssertionError("serialization must iterate mapping items directly")

    monkeypatch.setattr(ImmutableJsonMapping, "__getitem__", fail_lookup)
    assert json.loads(request.model_dump_json())["config"] == expected


def test_model_copy_updates_are_fully_revalidated():
    result = validate_json(CaptureResult, load_fixture("capture-result-v1.sample.json"))
    with pytest.raises(ValidationError, match="failed result requires error"):
        result.model_copy(update={"state": "failed"})
    with pytest.raises(ValidationError, match="secret-like key"):
        result.model_copy(update={"metadata": {"privateKey": "no"}})

    attempt = validate_json(
        AcquisitionAttempt, load_fixture("acquisition-attempt-v2.sample.json")
    )
    mismatched = attempt.result.model_copy(update={"request_id": "different"})
    with pytest.raises(ValidationError, match="result.request_id"):
        attempt.model_copy(update={"result": mismatched})
    request_data = attempt.request.model_dump(mode="json")
    request_data["metadata"] = {"APIKey": "no"}
    with pytest.raises(ValidationError, match="secret-like key"):
        attempt.model_copy(update={"request": request_data})

    skill = validate_json(SiteSkill, load_fixture("site-skill-v1.sample.json"))
    with pytest.raises(ValidationError, match="default_executor_id"):
        skill.model_copy(update={"default_executor_id": "rss"})
    with pytest.raises(ValidationError, match="secret-like key"):
        skill.executors[0].model_copy(update={"config": {"proxyAuth": "no"}})


@pytest.mark.parametrize("update", [None, {}])
def test_model_copy_without_updates_revalidates_injected_contract_state(update):
    request = validate_json(CaptureRequest, load_fixture("capture-request-v1.sample.json"))
    object.__setattr__(request, "config", {"clientsecret": "must-not-serialize"})
    with pytest.raises(ValidationError, match="secret-like key"):
        request.model_copy(update=update)


def test_existing_nested_contract_instances_are_fully_revalidated():
    attempt = validate_json(
        AcquisitionAttempt, load_fixture("acquisition-attempt-v2.sample.json")
    )
    object.__setattr__(
        attempt.request, "metadata", {"clientSecret": "must-not-serialize"}
    )
    with pytest.raises(ValidationError, match="secret-like key"):
        AcquisitionAttempt.model_validate(
            {
                **attempt.model_dump(mode="python", exclude={"request"}),
                "request": attempt.request,
            }
        )
    with pytest.raises(ValidationError, match="secret-like key"):
        attempt.model_copy()

    skill = validate_json(SiteSkill, load_fixture("site-skill-v1.sample.json"))
    object.__setattr__(skill.executors[0], "config", {"apiKey": "no"})
    with pytest.raises(ValidationError, match="secret-like key"):
        SiteSkill.model_validate(
            {
                **skill.model_dump(mode="python", exclude={"executors"}),
                "executors": (skill.executors[0], *skill.executors[1:]),
            }
        )
    with pytest.raises(ValidationError, match="secret-like key"):
        skill.model_copy()


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "10.20.30"])
def test_site_skill_accepts_canonical_three_component_versions(version):
    payload = load_fixture("site-skill-v1.sample.json")
    payload["version"] = version
    assert validate_json(SiteSkill, payload).version == version


@pytest.mark.parametrize(
    "version", ["01.0.0", "1.00.0", "1.0.01", "1.0", "1.0.0-alpha", "1.0.0+build"]
)
def test_site_skill_rejects_noncanonical_or_extended_versions(version):
    payload = load_fixture("site-skill-v1.sample.json")
    payload["version"] = version
    with pytest.raises(ValidationError):
        validate_json(SiteSkill, payload)


def test_site_skill_manifest_consistency_and_unique_governed_values():
    payload = load_fixture("site-skill-v1.sample.json")
    payload["executors"].append(deepcopy(payload["executors"][0]))
    with pytest.raises(ValidationError, match="unique executor_id"):
        validate_json(SiteSkill, payload)
    payload = load_fixture("site-skill-v1.sample.json")
    payload["recipes"].append(deepcopy(payload["recipes"][0]))
    with pytest.raises(ValidationError, match="unique recipe_id"):
        validate_json(SiteSkill, payload)
    payload = load_fixture("site-skill-v1.sample.json")
    payload["allowed_domains"].append("example.com")
    with pytest.raises(ValidationError, match="allowed_domains must be unique"):
        validate_json(SiteSkill, payload)
    payload = load_fixture("site-skill-v1.sample.json")
    payload["runtime_requirements"].append(
        deepcopy(payload["runtime_requirements"][0])
    )
    with pytest.raises(ValidationError, match="runtime_requirements values"):
        validate_json(SiteSkill, payload)


@pytest.mark.parametrize(
    "status", ["draft", "probed", "reviewed", "active", "deprecated"]
)
def test_site_skill_accepts_exact_lifecycle_statuses(status):
    payload = load_fixture("site-skill-v1.sample.json")
    payload["status"] = status
    assert validate_json(SiteSkill, payload).status == status


@pytest.mark.parametrize("status", ["retired", "production", "", None])
def test_site_skill_rejects_statuses_outside_v1_lifecycle(status):
    payload = load_fixture("site-skill-v1.sample.json")
    payload["status"] = status
    with pytest.raises(ValidationError):
        validate_json(SiteSkill, payload)


def test_site_skill_secret_policy_requires_forbidden_values_and_consistent_schemes():
    payload = load_fixture("site-skill-v1.sample.json")
    payload["secret_policy"]["forbid_secret_values"] = False
    with pytest.raises(ValidationError):
        validate_json(SiteSkill, payload)

    payload = load_fixture("site-skill-v1.sample.json")
    payload["secret_policy"]["allowed_reference_schemes"] = []
    with pytest.raises(ValidationError, match="non-empty when references are allowed"):
        validate_json(SiteSkill, payload)

    payload = load_fixture("site-skill-v1.sample.json")
    payload["secret_policy"] = {
        "allow_secret_references": False,
        "forbid_secret_values": True,
    }
    assert validate_json(SiteSkill, payload).secret_policy.allowed_reference_schemes == ()

    payload["secret_policy"]["allowed_reference_schemes"] = ["env"]
    with pytest.raises(ValidationError, match="empty when references are forbidden"):
        validate_json(SiteSkill, payload)


def test_site_skill_enforces_enabled_default_recipe_executor_consistency():
    payload = load_fixture("site-skill-v1.sample.json")
    payload["recipes"][0]["enabled"] = False
    with pytest.raises(ValidationError, match="default_recipe_id"):
        validate_json(SiteSkill, payload)
    payload = load_fixture("site-skill-v1.sample.json")
    payload["recipes"][0]["executor_id"] = "browser_rendered"
    with pytest.raises(ValidationError, match="default recipe and default executor"):
        validate_json(SiteSkill, payload)
    payload = load_fixture("site-skill-v1.sample.json")
    payload["recipes"][0]["executor_id"] = "browseract"
    with pytest.raises(
        ValidationError, match="enabled recipes must reference enabled executors"
    ):
        validate_json(SiteSkill, payload)


@pytest.mark.parametrize("executor_id", ["selenium", "playwright", "browser_act", 1])
def test_unsupported_executor_ids_are_rejected(executor_id):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["executor_id"] = executor_id
    with pytest.raises(ValidationError):
        validate_json(CaptureRequest, payload)


@pytest.mark.parametrize(
    "script_path",
    [
        "",
        "/tmp/runner.py",
        ".",
        "../runner.py",
        "site/../runner.py",
        "site//runner.py",
        "runner",
        r"site\runner.py",
        "site/./runner.py",
        "site/runner.py\x00",
        "C:/x.py",
        "a:b.py",
        "aux.py",
        "nested/CON.py",
        "nested/com1/tool.py",
        "nested/LPT9.txt/runner.py",
        "CONIN$.py",
        "nested/conout$.py",
        "COM¹.py",
        "nested/lpt¹/runner.py",
        "nested/trailing./runner.py",
        "nested/trailing /runner.py",
        "nested/a<b>/runner.py",
        'nested/a"b/runner.py',
        "nested/a|b/runner.py",
        "nested/a?b/runner.py",
        "nested/a*b/runner.py",
    ],
)
def test_executor_rejects_noncanonical_script_paths(script_path):
    with pytest.raises(ValidationError, match="script_path"):
        SiteSkillExecutor(executor_id="browseract", script_path=script_path)


@pytest.mark.parametrize("field", ["profile_ref", "entrypoint"])
@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute/file.py",
        "a//b.py",
        "a/../b.py",
        "a\\b.py",
        "a/./b.py",
        "a/b.py\x00",
        "C:/x.py",
        "a:b.py",
        "aux.py",
        "nested/PRN.yaml",
        "nested/nul/profile.json",
        "nested/CONIN$/profile.json",
        "nested/com¹/profile.json",
        "nested/trailing./profile.json",
        "nested/a<b>/profile.json",
        'nested/a"b/profile.json',
        "nested/a|b/profile.json",
        "nested/a?b/profile.json",
        "nested/a*b/profile.json",
    ],
)
def test_recipe_paths_apply_raw_canonical_validation(field, value):
    payload = load_fixture("site-skill-v1.sample.json")
    payload["recipes"][0][field] = value
    with pytest.raises(ValidationError, match=field):
        validate_json(SiteSkill, payload)


@pytest.mark.parametrize(
    "value",
    [
        "", "/tmp/a", ".", "..", "a/../b", "a//b", r"a\b", "a/./b", "a/\x00b",
        "C:/x.py", "a:b.py", "aux.py", "nested/CON.txt", "nested/COM9/file.html",
        "CONOUT$", "nested/LPT¹/file.html", "nested/trailing./file.html",
        "nested/a<b>/file.html", 'nested/a"b/file.html', "nested/a|b/file.html",
        "nested/a?b/file.html", "nested/a*b/file.html",
    ],
)
def test_artifact_paths_are_canonical_portable_posix_relative_paths(value):
    with pytest.raises(ValidationError, match="artifact_path"):
        CaptureContent(media_type="text/html", artifact_path=value)


def test_browseract_is_separate_from_playwright_compatibility_id():
    assert (
        SiteSkillExecutor(executor_id="browser_rendered").executor_id
        == "browser_rendered"
    )
    assert SiteSkillExecutor(executor_id="browseract").executor_id == "browseract"


@pytest.mark.parametrize(
    ("filename", "path"),
    [
        ("site-skill-v1.sample.json", ("generated_at",)),
        ("capture-request-v1.sample.json", ("requested_at",)),
        ("capture-result-v1.sample.json", ("started_at",)),
        ("capture-result-v1.sample.json", ("finished_at",)),
    ],
)
def test_every_timestamp_field_requires_a_timezone(filename, path):
    payload = load_fixture(filename)
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = "2026-07-16T12:00:00"
    model = (
        SiteSkill
        if filename.startswith("site")
        else CaptureRequest
        if "request" in filename
        else CaptureResult
    )
    with pytest.raises(ValidationError, match="timezone offset"):
        validate_json(model, payload)


@pytest.mark.parametrize(
    ("filename", "field"),
    [
        ("capture-request-v1.sample.json", "url"),
        ("capture-result-v1.sample.json", "final_url"),
    ],
)
@pytest.mark.parametrize(
    "url", ["https://user@example.com/a", "https://user:pass@example.com/a"]
)
def test_request_and_result_urls_reject_credentials(filename, field, url):
    payload = load_fixture(filename)
    payload[field] = url
    model = CaptureRequest if "request" in filename else CaptureResult
    with pytest.raises(ValidationError, match="credentials or userinfo"):
        validate_json(model, payload)


@pytest.mark.parametrize(
    "url",
    [
        "https:/user:pass@example.com/path",
        "https:user:pass@example.com/path",
        r"https:\user:pass@example.com\path",
    ],
)
def test_request_urls_reject_browser_special_scheme_userinfo_obfuscation(url):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["url"] = url
    with pytest.raises(ValidationError, match="credentials or userinfo"):
        validate_json(CaptureRequest, payload)


@pytest.mark.parametrize(
    "secret_key",
    [
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "api_key",
        "api-key",
        "proxy_password",
        "proxy_username",
        "access_token",
        "clientSecret",
        "apiKey",
        "APIKey",
        "apikey",
        "privateKey",
        "refreshToken",
        "proxyAuth",
        "proxyPassword",
        "sessionCookie",
        "clientsecret",
        "refreshtoken",
        "sessioncookie",
        "accesstoken",
        "OAUTHTOKEN",
        "XAPIKEY",
        "AWSACCESSKEYID",
        "clientapikey",
        "accesskey",
        "AWSAPIKEY",
        "googleapikey",
        "GOOGLEAPIKEY",
        "myaccesskey",
        "MYACCESSKEY",
        "myaccesskeyid",
        "MYACCESSKEYID",
        "ＡＰＩＫｅｙ",
        "ｃｌｉｅｎｔＳｅｃｒｅｔ",
    ],
)
@pytest.mark.parametrize("location", ["config", "metadata", "error_metadata"])
def test_portable_json_serialization_rejects_nested_secret_like_keys(
    secret_key, location
):
    if location == "config":
        payload = load_fixture("capture-request-v1.sample.json")
        payload["config"] = {"nested": [{secret_key: "must-not-serialize"}]}
        model = CaptureRequest
    elif location == "metadata":
        payload = load_fixture("capture-result-v1.sample.json")
        payload["metadata"] = {"nested": [{secret_key: "must-not-serialize"}]}
        model = CaptureResult
    else:
        payload = load_fixture("capture-result-v1.sample.json")
        payload["state"] = "failed"
        payload["content"] = None
        payload["error"] = {
            "code": "blocked",
            "message": "Blocked",
            "metadata": {secret_key: "must-not-serialize"},
        }
        model = CaptureResult
    serialized = json.dumps(payload)
    with pytest.raises(ValidationError, match="secret-like key"):
        model.model_validate_json(serialized)


@pytest.mark.parametrize("field", ["config", "metadata"])
def test_portable_json_errors_identify_the_validated_top_level_field(field):
    payload = load_fixture("capture-request-v1.sample.json")
    payload[field] = {"nested": [{"apiKey": "must-not-serialize"}]}

    with pytest.raises(ValidationError) as exc_info:
        validate_json(CaptureRequest, payload)

    message = str(exc_info.value)
    assert f"{field}.nested[0] contains forbidden secret-like key: apiKey" in message


def test_direct_portable_json_validation_uses_a_generic_location_label():
    with pytest.raises(ValueError, match=r"JSON value\.nested\[0\]"):
        validate_portable_json({"nested": [{"apiKey": "must-not-serialize"}]})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("input_mode", ["python", "json"])
def test_portable_json_rejects_non_finite_numbers_recursively(value, input_mode):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["metadata"] = {"nested": [value]}
    with pytest.raises(ValidationError, match="finite number"):
        if input_mode == "python":
            python_payload = validate_json(
                CaptureRequest, load_fixture("capture-request-v1.sample.json")
            ).model_dump(mode="python")
            python_payload["metadata"] = payload["metadata"]
            CaptureRequest.model_validate(python_payload)
        else:
            CaptureRequest.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "uri",
    [
        "http://user:password@proxy.example",
        "https://token@proxy.example/path",
        "socks5://user:password@proxy.example:1080",
    ],
)
@pytest.mark.parametrize("location", ["config", "metadata"])
def test_portable_json_rejects_nested_uri_userinfo_values(uri, location):
    payload = load_fixture("capture-request-v1.sample.json")
    payload[location] = {"nested": [{"proxy_url": uri}]}
    with pytest.raises(ValidationError, match="URI userinfo"):
        validate_json(CaptureRequest, payload)


@pytest.mark.parametrize(
    "uri", ["//user@proxy.example/path", "//user:password@proxy.example:8080/path"]
)
@pytest.mark.parametrize("location", ["config", "metadata", "error_metadata"])
def test_portable_json_rejects_nested_network_path_userinfo(uri, location):
    if location == "config":
        payload = load_fixture("capture-request-v1.sample.json")
        payload["config"] = {"nested": [{"proxy_url": uri}]}
        model = CaptureRequest
    else:
        payload = load_fixture("capture-result-v1.sample.json")
        model = CaptureResult
    if location == "error_metadata":
        payload["state"] = "failed"
        payload["content"] = None
        payload["error"] = {
            "code": "blocked",
            "message": "Blocked",
            "metadata": {"nested": [{"proxy_url": uri}]},
        }
    elif location == "metadata":
        payload[location] = {"nested": [{"proxy_url": uri}]}
    with pytest.raises(ValidationError, match="URI userinfo"):
        validate_json(model, payload)


@pytest.mark.parametrize(
    "uri",
    [
        "http：//user@proxy.example/path",
        "ｈｔｔｐ：／／user：password＠proxy.example/path",
        r"http:\\user@proxy.example\path",
        r"https:\\user:password@proxy.example\path",
    ],
)
def test_portable_json_uri_userinfo_detection_normalizes_compatibility_forms(uri):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["metadata"] = {"nested": [{"proxy_url": uri}]}
    with pytest.raises(ValidationError, match="URI userinfo"):
        validate_json(CaptureRequest, payload)


@pytest.mark.parametrize(
    "uri",
    [
        "https:/user:pass@example.com/path",
        "https:user:pass@example.com/path",
        r"https:\user:pass@example.com\path",
    ],
)
def test_portable_json_rejects_browser_special_scheme_userinfo_obfuscation(uri):
    payload = load_fixture("capture-request-v1.sample.json")
    payload["metadata"] = {"nested": [{"proxy_url": uri}]}
    with pytest.raises(ValidationError, match="URI userinfo"):
        validate_json(CaptureRequest, payload)


def test_portable_json_uri_detection_normalization_does_not_change_serialization():
    uri = r"https:\\proxy.example\public"
    payload = load_fixture("capture-request-v1.sample.json")
    payload["metadata"] = {"mirror": uri}
    request = validate_json(CaptureRequest, payload)
    assert request.model_dump(mode="json")["metadata"]["mirror"] == uri


@pytest.mark.parametrize(
    "field",
    [
        "site_key",
        "site_skill_id",
        "site_skill_version",
        "site_skill_digest",
        "recipe_id",
        "run_id",
        "scope_id",
        "request_id",
        "executor_id",
    ],
)
def test_attempt_rejects_every_request_result_lineage_mismatch(field):
    payload = load_fixture("acquisition-attempt-v2.sample.json")
    payload["result"][field] = "different" if field != "site_skill_digest" else "c" * 64
    with pytest.raises(ValidationError, match=field):
        validate_json(AcquisitionAttempt, payload)


def test_attempt_rejects_capture_before_request_and_whitespace_reason():
    payload = load_fixture("acquisition-attempt-v2.sample.json")
    payload["result"]["started_at"] = "2026-07-16T12:00:59Z"
    with pytest.raises(ValidationError, match="must not precede"):
        validate_json(AcquisitionAttempt, payload)
    payload = load_fixture("acquisition-attempt-v2.sample.json")
    payload["accepted"] = False
    payload["acceptance_reason"] = "   "
    with pytest.raises(ValidationError, match="acceptance_reason"):
        validate_json(AcquisitionAttempt, payload)
