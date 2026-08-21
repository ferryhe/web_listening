from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib import request as urllib_request

import pytest
from jsonschema import Draft202012Validator, ValidationError

import web_listening.blocks.acquisition_contract as acquisition_contract
from web_listening.blocks.acquisition_contract import (
    AcquisitionContractError,
    validate_acquisition_contract_bundle,
)
from web_listening.contracts.site_diagnostic import canonical_json


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "contracts/acquisition-manifest.v1"
EXPECTED_SCHEMA_SHA256 = (
    "3498f1028058ff7159bd199e652a972c58a13a01a980f7c06a5e8be06db4054c"
)
EXPECTED_FIXTURE_SHA256 = (
    "3dac2f9044436bea779877818e72daf5f60346a3831c183b9fdb661c314efc91"
)
MAX_PORTABLE_JSON_INTEGER = 9_007_199_254_740_991
DISCOVERY_CONTRADICTIONS = (
    "seed-artifact",
    "seed-source",
    "link-artifact",
    "link-source",
    "link-parent",
    "search-artifact",
    "search-source",
    "search-parent",
    "crawler-source",
    "crawler-parent-without-artifact",
    "crawler-artifact-without-parent",
    "derived-artifact",
    "derived-source",
    "derived-parent",
)


def _load(name: str) -> dict[str, object]:
    return json.loads((BUNDLE / name).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_bundle(tmp_path: Path) -> Path:
    destination = tmp_path / "acquisition-manifest.v1"
    destination.mkdir()
    for name in ("schema.json", "fixture.json", "producer-confirmation.json"):
        (destination / name).write_bytes((BUNDLE / name).read_bytes())
    return destination


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _resign(bundle: Path) -> None:
    confirmation = json.loads(
        (bundle / "producer-confirmation.json").read_text(encoding="utf-8")
    )
    confirmation["schema_sha256"] = _sha256(bundle / "schema.json")
    confirmation["fixture_sha256"] = _sha256(bundle / "fixture.json")
    _write_json(bundle / "producer-confirmation.json", confirmation)


def _trust_current_test_bundle(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    """Reach defense-in-depth validation after a deliberate test mutation."""

    monkeypatch.setattr(
        acquisition_contract,
        "EXPECTED_SCHEMA_SHA256",
        _sha256(bundle / "schema.json"),
    )
    monkeypatch.setattr(
        acquisition_contract,
        "EXPECTED_FIXTURE_SHA256",
        _sha256(bundle / "fixture.json"),
    )


def _apply_discovery_contradiction(
    fixture: dict[str, object], contradiction: str
) -> None:
    artifacts = fixture["artifacts"]
    seed, crawler_root, link, search, derived = (
        artifacts[0],
        artifacts[1],
        artifacts[2],
        artifacts[3],
        artifacts[4],
    )
    if contradiction == "seed-artifact":
        seed["discovered_from"]["artifact_id"] = link["artifact_id"]
    elif contradiction == "seed-source":
        seed["discovered_from"]["source_url"] = link["final_url"]
    elif contradiction == "link-artifact":
        link["discovered_from"]["artifact_id"] = None
    elif contradiction == "link-source":
        link["discovered_from"]["source_url"] = None
    elif contradiction == "link-parent":
        link["lineage"]["parent_artifact_id"] = None
    elif contradiction == "search-artifact":
        search["discovered_from"]["artifact_id"] = seed["artifact_id"]
    elif contradiction == "search-source":
        search["discovered_from"]["source_url"] = None
    elif contradiction == "search-parent":
        search["lineage"]["parent_artifact_id"] = seed["artifact_id"]
    elif contradiction == "crawler-source":
        crawler_root["discovered_from"]["source_url"] = None
    elif contradiction == "crawler-parent-without-artifact":
        crawler_root["lineage"]["parent_artifact_id"] = seed["artifact_id"]
    elif contradiction == "crawler-artifact-without-parent":
        crawler_root["discovered_from"]["artifact_id"] = seed["artifact_id"]
    elif contradiction == "derived-artifact":
        derived["discovered_from"]["artifact_id"] = None
    elif contradiction == "derived-source":
        derived["discovered_from"]["source_url"] = None
    elif contradiction == "derived-parent":
        derived["lineage"]["parent_artifact_id"] = None
    else:
        raise AssertionError(f"unknown discovery contradiction: {contradiction}")


def test_canonical_bundle_is_draft_2020_12_and_producer_confirmed() -> None:
    schema = _load("schema.json")
    fixture = _load("fixture.json")
    confirmation = _load("producer-confirmation.json")

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/acquisition-manifest.v1/schema.json")
    assert schema["title"] == "acquisition-manifest.v1"
    assert schema["version"] == "acquisition-manifest.v1"
    assert schema["$defs"]["redirectHop"]["properties"]["http_status"] == {
        "type": "integer",
        "enum": [301, 302, 303, 307, 308],
    }
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=acquisition_contract._required_format_checker(),
    ).validate(fixture)

    assert _sha256(BUNDLE / "schema.json") == EXPECTED_SCHEMA_SHA256
    assert _sha256(BUNDLE / "fixture.json") == EXPECTED_FIXTURE_SHA256
    assert acquisition_contract.EXPECTED_SCHEMA_SHA256 == EXPECTED_SCHEMA_SHA256
    assert acquisition_contract.EXPECTED_FIXTURE_SHA256 == EXPECTED_FIXTURE_SHA256

    assert confirmation == {
        "contract_version": "acquisition-manifest.v1",
        "producer_repository": "ferryhe/web_listening",
        "producer_feature_issue": 46,
        "producer_contract_issue": 48,
        "evidence_scope": "producer-confirmed",
        "schema_sha256": _sha256(BUNDLE / "schema.json"),
        "fixture_sha256": _sha256(BUNDLE / "fixture.json"),
    }
    for name in ("schema.json", "fixture.json", "producer-confirmation.json"):
        raw = (BUNDLE / name).read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r" not in raw
        raw.decode("utf-8")


def test_fixture_covers_identity_lineage_redirect_and_replay_cases() -> None:
    fixture = _load("fixture.json")
    artifacts = fixture["artifacts"]
    assert isinstance(artifacts, list)

    media_types = {artifact["retrieval"]["mime_type"] for artifact in artifacts}
    assert {"text/html", "application/pdf", "text/markdown"} <= media_types
    assert any(artifact["lineage"]["parent_artifact_id"] for artifact in artifacts)
    assert any(artifact["lineage"]["source_artifact_id"] for artifact in artifacts)
    decisions = {
        hop["decision"] for artifact in artifacts for hop in artifact["redirect_chain"]
    }
    assert {"allow", "reject"} <= decisions
    assert all(artifact["access_decision_id"] for artifact in artifacts)

    completed = [
        artifact for artifact in artifacts if artifact["artifact_status"] == "completed"
    ]
    same_source_pairs = [
        (left, right)
        for index, left in enumerate(completed)
        for right in completed[index + 1 :]
        if left["normalized_source_identity"] == right["normalized_source_identity"]
        and left["retrieval"]["sha256"] != right["retrieval"]["sha256"]
    ]
    assert same_source_pairs
    same_blob_pairs = [
        (left, right)
        for index, left in enumerate(completed)
        for right in completed[index + 1 :]
        if left["normalized_source_identity"] != right["normalized_source_identity"]
        and left["retrieval"]["sha256"] == right["retrieval"]["sha256"]
        and left["artifact_id"] != right["artifact_id"]
    ]
    assert same_blob_pairs

    replay = fixture["replay"]
    assert replay["mode"] == "idempotent"
    assert replay["mutates_input"] is False
    assert replay["expected_artifact_ids"] == [
        artifact["artifact_id"] for artifact in artifacts
    ]

    by_id = {artifact["artifact_id"]: artifact for artifact in artifacts}
    by_kind: dict[str, list[dict[str, object]]] = {}
    for artifact in artifacts:
        by_kind.setdefault(artifact["discovered_from"]["kind"], []).append(artifact)
    assert set(by_kind) == {"seed", "link", "search", "crawler", "derived"}
    assert by_kind["seed"][0]["discovered_from"] == {
        "kind": "seed",
        "artifact_id": None,
        "source_url": None,
    }
    assert by_kind["search"][0]["discovered_from"]["artifact_id"] is None
    assert {
        artifact["discovered_from"]["artifact_id"] is None
        for artifact in by_kind["crawler"]
    } == {False, True}
    for kind in ("link", "derived"):
        artifact = by_kind[kind][0]
        discovered = artifact["discovered_from"]
        parent = by_id[discovered["artifact_id"]]
        assert discovered["source_url"] == parent["final_url"]


def test_size_bytes_uses_the_portable_json_integer_maximum_everywhere() -> None:
    schema = _load("schema.json")
    retrieval = schema["$defs"]["retrieval"]["properties"]["size_bytes"]["oneOf"][0]
    completed = schema["$defs"]["artifact"]["allOf"][0]["then"]["properties"][
        "retrieval"
    ]["properties"]["size_bytes"]

    assert acquisition_contract.MAX_PORTABLE_JSON_INTEGER == MAX_PORTABLE_JSON_INTEGER
    assert retrieval["maximum"] == completed["maximum"] == MAX_PORTABLE_JSON_INTEGER

    fixture = _load("fixture.json")
    fixture["artifacts"][0]["retrieval"]["size_bytes"] = MAX_PORTABLE_JSON_INTEGER
    Draft202012Validator(schema).validate(fixture)
    for invalid in (MAX_PORTABLE_JSON_INTEGER + 1, 10**99):
        fixture["artifacts"][0]["retrieval"]["size_bytes"] = invalid
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(fixture)


def test_semantic_size_limit_is_defense_in_depth() -> None:
    fixture = _load("fixture.json")
    fixture["artifacts"][0]["retrieval"]["size_bytes"] = MAX_PORTABLE_JSON_INTEGER
    acquisition_contract._validate_artifacts(fixture)

    for invalid in (MAX_PORTABLE_JSON_INTEGER + 1, 10**99):
        fixture["artifacts"][0]["retrieval"]["size_bytes"] = invalid
        with pytest.raises(AcquisitionContractError) as error:
            acquisition_contract._validate_artifacts(fixture)
        assert error.value.reason_code == "fixture.invalid"


@pytest.mark.parametrize("contradiction", DISCOVERY_CONTRADICTIONS)
def test_schema_rejects_discovery_kind_contradictions(contradiction: str) -> None:
    schema = _load("schema.json")
    fixture = _load("fixture.json")
    _apply_discovery_contradiction(fixture, contradiction)

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(fixture)


@pytest.mark.parametrize("contradiction", DISCOVERY_CONTRADICTIONS)
def test_semantic_validator_rejects_discovery_kind_contradictions(
    contradiction: str,
) -> None:
    fixture = _load("fixture.json")
    _apply_discovery_contradiction(fixture, contradiction)

    with pytest.raises(AcquisitionContractError) as error:
        acquisition_contract._validate_artifacts(fixture)

    assert error.value.reason_code == "lineage.invalid"


@pytest.mark.parametrize(
    "contradiction",
    [
        "link-reference",
        "link-source-url",
        "crawler-reference",
        "crawler-source-url",
        "derived-reference",
        "derived-source-url",
    ],
)
def test_semantic_validator_binds_discovery_reference_and_source_url(
    contradiction: str,
) -> None:
    fixture = _load("fixture.json")
    artifacts = fixture["artifacts"]
    alternate = artifacts[1]
    if contradiction.startswith("link-"):
        artifact = artifacts[2]
    elif contradiction.startswith("crawler-"):
        artifact = artifacts[5]
    else:
        artifact = artifacts[4]
    if contradiction.endswith("reference"):
        artifact["discovered_from"]["artifact_id"] = alternate["artifact_id"]
        artifact["discovered_from"]["source_url"] = alternate["final_url"]
    else:
        artifact["discovered_from"]["source_url"] = (
            "https://example.invalid/inconsistent-source"
        )

    with pytest.raises(AcquisitionContractError) as error:
        acquisition_contract._validate_artifacts(fixture)

    assert error.value.reason_code == "lineage.invalid"


def test_schema_explicitly_bounds_normative_strings_and_replay() -> None:
    schema = _load("schema.json")
    artifact = schema["$defs"]["artifact"]
    completed = artifact["allOf"][0]["then"]["properties"]["retrieval"]["properties"]
    retrieval = schema["$defs"]["retrieval"]["properties"]

    assert schema["properties"]["produced_at"]["maxLength"] == 64
    assert (
        schema["$defs"]["adapter"]["properties"]["adapter_version"]["maxLength"] == 64
    )
    assert retrieval["retrieved_at"]["oneOf"][0]["maxLength"] == 64
    assert completed["retrieved_at"]["maxLength"] == 64
    assert retrieval["mime_type"]["oneOf"][0]["maxLength"] == 255
    assert completed["mime_type"]["maxLength"] == 255
    assert (
        schema["$defs"]["replay"]["properties"]["expected_artifact_ids"]["maxItems"]
        == schema["properties"]["artifacts"]["maxItems"]
        == 1000
    )


@pytest.mark.parametrize(
    "field",
    [
        "adapter_version",
        "produced_at",
        "retrieved_at",
        "mime_type",
        "expected_artifact_ids",
    ],
)
def test_schema_enforces_normative_bounds_without_format_checker(field: str) -> None:
    schema = _load("schema.json")
    fixture = _load("fixture.json")
    if field == "adapter_version":
        fixture["artifacts"][0]["acquisition_adapter"][field] = f"{'1' * 65}.0.0"
    elif field == "produced_at":
        fixture[field] = "x" * 65
    elif field == "retrieved_at":
        fixture["artifacts"][0]["retrieval"][field] = "x" * 65
    elif field == "mime_type":
        fixture["artifacts"][0]["retrieval"][field] = f"{'a' * 128}/{'b' * 128}"
    else:
        fixture["replay"][field] = [f"artifact-{index:024x}" for index in range(1001)]

    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(fixture)


def test_schema_itself_closes_secret_fields_and_url_userinfo() -> None:
    schema = _load("schema.json")
    fixture = _load("fixture.json")
    validator = Draft202012Validator(
        schema,
        format_checker=acquisition_contract._required_format_checker(),
    )

    fixture["authorization"] = "forbidden"
    with pytest.raises(ValidationError):
        validator.validate(fixture)

    fixture = _load("fixture.json")
    fixture["artifacts"][0]["requested_url"] = (
        "https://user:password@example.invalid/report"
    )
    with pytest.raises(ValidationError):
        validator.validate(fixture)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.invalid/reports/@annual",
        "https://example.invalid/reports?contact=ops@example.invalid",
        "https://example.invalid/reports#section@2026",
    ],
)
def test_schema_allows_at_outside_the_url_authority(url: str) -> None:
    schema = _load("schema.json")
    fixture = _load("fixture.json")
    fixture["artifacts"][0]["requested_url"] = url

    Draft202012Validator(
        schema,
        format_checker=acquisition_contract._required_format_checker(),
    ).validate(fixture)
    if "#" not in url:
        acquisition_contract._validate_url(url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_url", "https://user@example.invalid/report"),
        ("source_url", "https://user:password@example.invalid/report"),
        ("produced_at", "not-a-date-time"),
        ("normalized_source_identity", "not a uri"),
    ],
)
def test_schema_enforces_uri_and_date_time_formats(field: str, value: str) -> None:
    schema = _load("schema.json")
    fixture = _load("fixture.json")
    if field == "produced_at":
        fixture[field] = value
    else:
        fixture["artifacts"][0][field] = value

    with pytest.raises(ValidationError):
        Draft202012Validator(
            schema,
            format_checker=acquisition_contract._required_format_checker(),
        ).validate(fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("produced_at", "not-a-date-time"),
        ("normalized_source_identity", "not a uri"),
    ],
)
def test_governed_validator_rejects_malformed_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    fixture_path = bundle / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if field == "produced_at":
        fixture[field] = value
    else:
        fixture["artifacts"][0][field] = value
    _write_json(fixture_path, fixture)
    _resign(bundle)
    _trust_current_test_bundle(monkeypatch, bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "fixture.invalid"


def test_governed_validator_fails_if_required_format_checker_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteFormatChecker:
        checkers = {"uri": object()}

    monkeypatch.setattr(
        acquisition_contract,
        "FormatChecker",
        IncompleteFormatChecker,
    )

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(BUNDLE)

    assert error.value.reason_code == "schema.format_unavailable"


@pytest.mark.parametrize("edit", ["status-enum", "required-field"])
def test_validator_rejects_self_resigned_breaking_schema_edits(
    tmp_path: Path,
    edit: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    schema_path = bundle / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if edit == "status-enum":
        schema["$defs"]["status"]["enum"].append("cancelled")
    else:
        schema["required"].remove("manifest_version")
    _write_json(schema_path, schema)
    _resign(bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "bundle.digest_mismatch"


def test_validator_rejects_self_resigned_fixture_edits(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    fixture_path = bundle / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["produced_at"] = "2026-08-20T13:00:01Z"
    _write_json(fixture_path, fixture)
    _resign(bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "bundle.digest_mismatch"


def test_validator_governs_missing_local_schema_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _copy_bundle(tmp_path)
    schema_path = bundle / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["artifacts"]["items"]["$ref"] = "#/$defs/missing"
    _write_json(schema_path, schema)
    _resign(bundle)
    _trust_current_test_bundle(monkeypatch, bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "schema.reference_invalid"


@pytest.mark.parametrize("keyword", ["$ref", "$dynamicRef", "$recursiveRef"])
def test_validator_rejects_external_schema_reference_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keyword: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    schema_path = bundle / "schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["properties"]["artifacts"]["items"][keyword] = (
        "https://attacker.invalid/schema.json"
    )
    _write_json(schema_path, schema)
    _resign(bundle)
    _trust_current_test_bundle(monkeypatch, bundle)
    calls: list[str] = []

    def fail_if_called(request: object, *args: object, **kwargs: object) -> object:
        calls.append(str(request))
        raise AssertionError("network retrieval attempted")

    monkeypatch.setattr(urllib_request, "urlopen", fail_if_called)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "schema.reference_invalid"
    assert calls == []


@pytest.mark.parametrize("http_status", [304, 306, 399])
def test_validator_rejects_non_redirect_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    http_status: int,
) -> None:
    bundle = _copy_bundle(tmp_path)
    fixture_path = bundle / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    fixture["artifacts"][0]["redirect_chain"][0]["http_status"] = http_status
    _write_json(fixture_path, fixture)
    _resign(bundle)
    _trust_current_test_bundle(monkeypatch, bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "redirect.invalid"


def test_validator_rejects_https_to_http_redirect_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _copy_bundle(tmp_path)
    fixture_path = bundle / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    hop = fixture["artifacts"][0]["redirect_chain"][0]
    hop["to_url"] = hop["to_url"].replace("https://", "http://", 1)
    fixture["artifacts"][0]["final_url"] = hop["to_url"]
    _write_json(fixture_path, fixture)
    _resign(bundle)
    _trust_current_test_bundle(monkeypatch, bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "redirect.invalid"


@pytest.mark.parametrize(
    "contradiction",
    ["source-primary", "source-derived-list", "derived-missing-primary"],
)
def test_validator_rejects_role_lineage_contradictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contradiction: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    fixture_path = bundle / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    source_id = fixture["artifacts"][0]["artifact_id"]
    if contradiction == "source-primary":
        fixture["artifacts"][1]["lineage"]["source_artifact_id"] = source_id
    elif contradiction == "source-derived-list":
        fixture["artifacts"][1]["lineage"]["derived_from_artifact_ids"] = [source_id]
    else:
        fixture["artifacts"][4]["lineage"]["derived_from_artifact_ids"] = [
            fixture["artifacts"][1]["artifact_id"]
        ]
    _write_json(fixture_path, fixture)
    _resign(bundle)
    _trust_current_test_bundle(monkeypatch, bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "lineage.invalid"


def test_validator_result_is_canonical_repeatable_and_read_only() -> None:
    before = {path.name: path.read_bytes() for path in BUNDLE.iterdir()}

    first = validate_acquisition_contract_bundle(BUNDLE)
    second = validate_acquisition_contract_bundle(BUNDLE)

    assert first == second
    assert first["schema_version"] == "acquisition-contract-validation.v1"
    assert first["valid"] is True
    assert first["reason_code"] == "contract.valid"
    assert first["contract_version"] == "acquisition-manifest.v1"
    assert first["schema_sha256"] == _sha256(BUNDLE / "schema.json")
    assert first["fixture_sha256"] == _sha256(BUNDLE / "fixture.json")
    assert canonical_json(first) == canonical_json(second)
    assert before == {path.name: path.read_bytes() for path in BUNDLE.iterdir()}


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("missing", "bundle.missing_file"),
        ("corrupt", "bundle.invalid_json"),
        ("digest", "bundle.digest_mismatch"),
        ("confirmation", "confirmation.identity_mismatch"),
        ("schema", "schema.open_shape"),
        ("identity", "artifact.identity_mismatch"),
        ("derived", "lineage.invalid"),
        ("lineage", "lineage.invalid"),
        ("redirect", "redirect.invalid"),
        ("redirect-decision", "redirect.invalid"),
        ("status", "status.invalid"),
        ("uri", "artifact.identity_mismatch"),
        ("unknown", "fixture.invalid"),
        ("sample", "bundle.sample_only"),
        ("old", "bundle.version_unsupported"),
        ("secret", "bundle.sensitive_data"),
        ("extra", "bundle.unknown_file"),
    ],
)
def test_validator_fails_closed_for_invalid_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    reason_code: str,
) -> None:
    bundle = _copy_bundle(tmp_path)
    fixture_path = bundle / "fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        fixture_path.unlink()
    elif mutation == "corrupt":
        fixture_path.write_text("{", encoding="utf-8", newline="\n")
    elif mutation == "digest":
        fixture["produced_at"] = "2026-08-20T13:00:01Z"
        _write_json(fixture_path, fixture)
    elif mutation == "confirmation":
        confirmation_path = bundle / "producer-confirmation.json"
        confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
        confirmation["producer_repository"] = "example/other"
        _write_json(confirmation_path, confirmation)
    elif mutation == "schema":
        schema_path = bundle / "schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["additionalProperties"] = True
        _write_json(schema_path, schema)
        _resign(bundle)
    elif mutation == "identity":
        fixture["artifacts"][0]["artifact_id"] = "artifact-" + "0" * 24
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "derived":
        fixture["artifacts"][4]["lineage"]["source_artifact_id"] = fixture["artifacts"][
            1
        ]["artifact_id"]
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "lineage":
        fixture["artifacts"][2]["lineage"]["parent_artifact_id"] = (
            "artifact-" + "0" * 24
        )
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "redirect":
        fixture["artifacts"][0]["redirect_chain"][0]["from_url"] = (
            "https://example.invalid/wrong"
        )
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "redirect-decision":
        fixture["artifacts"][5]["artifact_status"] = "failed"
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "status":
        fixture["run_status"] = "completed"
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "uri":
        fixture["artifacts"][0]["retrieval"]["artifact_uri"] = (
            "artifact:sha256:" + "f" * 64
        )
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "unknown":
        fixture["unknown_breaking_field"] = True
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "sample":
        fixture["evidence_scope"] = "sample-only"
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "old":
        fixture["manifest_version"] = "web-listening-manifest.v1"
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "secret":
        fixture["artifacts"][0]["requested_url"] += "?api_key=do-not-leak"
        _write_json(fixture_path, fixture)
        _resign(bundle)
    elif mutation == "extra":
        (bundle / "sample.json").write_text("{}\n", encoding="utf-8", newline="\n")

    if mutation in {
        "schema",
        "identity",
        "derived",
        "lineage",
        "redirect",
        "redirect-decision",
        "status",
        "uri",
        "unknown",
        "sample",
        "old",
        "secret",
    }:
        _trust_current_test_bundle(monkeypatch, bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == reason_code
    assert "do-not-leak" not in str(error.value)


def test_validator_rejects_non_lf_contract_bytes(tmp_path: Path) -> None:
    bundle = _copy_bundle(tmp_path)
    schema_path = bundle / "schema.json"
    schema_path.write_bytes(schema_path.read_bytes().replace(b"\n", b"\r\n"))
    _resign(bundle)

    with pytest.raises(AcquisitionContractError) as error:
        validate_acquisition_contract_bundle(bundle)

    assert error.value.reason_code == "bundle.noncanonical_bytes"
