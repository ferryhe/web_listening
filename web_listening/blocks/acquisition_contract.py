from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from web_listening.contracts._protocol import is_secret_like_key
from web_listening.contracts.access_decision import (
    _canonical_url,
    _validate_non_sensitive_text,
)
from web_listening.contracts.site_diagnostic import canonical_json


CONTRACT_VERSION = "acquisition-manifest.v1"
VALIDATION_VERSION = "acquisition-contract-validation.v1"
MAX_PORTABLE_JSON_INTEGER = 9_007_199_254_740_991
EXPECTED_SCHEMA_SHA256 = (
    "3498f1028058ff7159bd199e652a972c58a13a01a980f7c06a5e8be06db4054c"
)
EXPECTED_FIXTURE_SHA256 = (
    "3dac2f9044436bea779877818e72daf5f60346a3831c183b9fdb661c314efc91"
)
EXPECTED_FILES = frozenset(
    {"schema.json", "fixture.json", "producer-confirmation.json"}
)
EXPECTED_CONFIRMATION_IDENTITY = {
    "contract_version": CONTRACT_VERSION,
    "producer_repository": "ferryhe/web_listening",
    "producer_feature_issue": 46,
    "producer_contract_issue": 48,
    "evidence_scope": "producer-confirmed",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact-[0-9a-f]{24}$")
_DERIVED_IDENTITY_RE = re.compile(
    r"^urn:web-listening:derived:artifact-[0-9a-f]{24}:[a-z0-9][a-z0-9._-]{0,31}$"
)
_ALLOWED_REDIRECT_HTTP_STATUSES = frozenset({301, 302, 303, 307, 308})
_LOCAL_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef", "$recursiveRef"})
_REQUIRED_FORMATS = frozenset({"date-time", "uri"})


class AcquisitionContractError(ValueError):
    """A fail-closed bundle validation error with a stable public reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"acquisition contract validation failed ({reason_code})")


def acquisition_contract_error_result(reason_code: str) -> dict[str, object]:
    return {
        "schema_version": VALIDATION_VERSION,
        "valid": False,
        "reason_code": reason_code,
        "message": "acquisition contract validation failed",
        "contract_version": None,
    }


def artifact_identity_sha256(
    *,
    source_run_id: str,
    normalized_source_identity: str,
    sha256: str | None,
    manifest_version: str = CONTRACT_VERSION,
) -> str:
    identity = {
        "manifest_version": manifest_version,
        "normalized_source_identity": normalized_source_identity,
        "sha256": sha256,
        "source_run_id": source_run_id,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def artifact_id_for_identity(
    *,
    source_run_id: str,
    normalized_source_identity: str,
    sha256: str | None,
    manifest_version: str = CONTRACT_VERSION,
) -> str:
    digest = artifact_identity_sha256(
        source_run_id=source_run_id,
        normalized_source_identity=normalized_source_identity,
        sha256=sha256,
        manifest_version=manifest_version,
    )
    return f"artifact-{digest[:24]}"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _load_json_file(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise AcquisitionContractError("bundle.path_invalid") from exc
    if size > 20 * 1024 * 1024:
        raise AcquisitionContractError("bundle.size_limit")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise AcquisitionContractError("bundle.invalid_json") from exc
    if not isinstance(payload, dict):
        raise AcquisitionContractError("bundle.invalid_json")
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw or not raw.endswith(b"\n"):
        raise AcquisitionContractError("bundle.noncanonical_bytes")
    return raw, payload


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _required_format_checker() -> FormatChecker:
    checker = FormatChecker()
    if not _REQUIRED_FORMATS <= checker.checkers.keys():
        raise AcquisitionContractError("schema.format_unavailable")
    return checker


def _reject_schema_retrieval(uri: str) -> None:
    raise NoSuchResource(ref=uri)


def _validate_confirmation(
    confirmation: Mapping[str, Any],
    *,
    schema_sha256: str,
    fixture_sha256: str,
) -> None:
    expected_keys = set(EXPECTED_CONFIRMATION_IDENTITY) | {
        "schema_sha256",
        "fixture_sha256",
    }
    if set(confirmation) != expected_keys:
        raise AcquisitionContractError("confirmation.identity_mismatch")
    if any(
        confirmation.get(key) != value
        for key, value in EXPECTED_CONFIRMATION_IDENTITY.items()
    ):
        raise AcquisitionContractError("confirmation.identity_mismatch")
    declared_schema_sha = confirmation.get("schema_sha256")
    declared_fixture_sha = confirmation.get("fixture_sha256")
    if not (
        isinstance(declared_schema_sha, str)
        and isinstance(declared_fixture_sha, str)
        and _SHA256_RE.fullmatch(declared_schema_sha)
        and _SHA256_RE.fullmatch(declared_fixture_sha)
    ):
        raise AcquisitionContractError("confirmation.identity_mismatch")
    if (
        declared_schema_sha != EXPECTED_SCHEMA_SHA256
        or declared_fixture_sha != EXPECTED_FIXTURE_SHA256
        or schema_sha256 != EXPECTED_SCHEMA_SHA256
        or fixture_sha256 != EXPECTED_FIXTURE_SHA256
    ):
        raise AcquisitionContractError("bundle.digest_mismatch")


def _validate_schema_identity(schema: Mapping[str, Any]) -> None:
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or schema.get("$id")
        != "https://github.com/ferryhe/web_listening/contracts/acquisition-manifest.v1/schema.json"
        or schema.get("title") != CONTRACT_VERSION
        or schema.get("version") != CONTRACT_VERSION
    ):
        raise AcquisitionContractError("schema.identity_mismatch")
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, TypeError, ValueError, RecursionError) as exc:
        raise AcquisitionContractError("schema.invalid") from exc

    def visit(node: object, *, is_root: bool = False) -> None:
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if node.get("type") == "object":
                if node.get("additionalProperties") is not False:
                    raise AcquisitionContractError("schema.open_shape")
            if isinstance(properties, Mapping) and "run_id" in properties:
                raise AcquisitionContractError("schema.identity_mismatch")
            if not is_root and "$id" in node:
                raise AcquisitionContractError("schema.reference_invalid")
            for key, value in node.items():
                if key in _LOCAL_REFERENCE_KEYWORDS and (
                    not isinstance(value, str) or not value.startswith("#")
                ):
                    raise AcquisitionContractError("schema.reference_invalid")
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    try:
        visit(schema, is_root=True)
    except RecursionError as exc:
        raise AcquisitionContractError("schema.invalid") from exc
    properties = schema.get("properties")
    if not isinstance(properties, Mapping) or "source_run_id" not in properties:
        raise AcquisitionContractError("schema.identity_mismatch")


def _scan_non_sensitive(value: object, *, location: str = "fixture") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if is_secret_like_key(key, include_namespaced_exact_names=True):
                raise AcquisitionContractError("bundle.sensitive_data")
            _scan_non_sensitive(child, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_non_sensitive(child, location=f"{location}[{index}]")
    elif isinstance(value, str):
        try:
            _validate_non_sensitive_text(value, location=location)
        except ValueError as exc:
            raise AcquisitionContractError("bundle.sensitive_data") from exc


def _validate_url(value: object) -> None:
    if not isinstance(value, str):
        raise AcquisitionContractError("fixture.invalid")
    try:
        if _canonical_url(value) != value:
            raise ValueError("non-canonical URL")
    except ValueError as exc:
        message = str(exc).casefold()
        reason = (
            "bundle.sensitive_data"
            if "sensitive" in message
            or "credential" in message
            or "userinfo" in message
            else "fixture.invalid"
        )
        raise AcquisitionContractError(reason) from exc


def _validate_lineage(artifacts: list[Mapping[str, Any]]) -> None:
    artifact_ids = [artifact.get("artifact_id") for artifact in artifacts]
    if any(not isinstance(value, str) for value in artifact_ids):
        raise AcquisitionContractError("fixture.invalid")
    ids = set(artifact_ids)
    if len(ids) != len(artifact_ids):
        raise AcquisitionContractError("artifact.identity_mismatch")

    edges: dict[str, set[str]] = {str(value): set() for value in artifact_ids}
    for artifact in artifacts:
        artifact_id = str(artifact["artifact_id"])
        lineage = artifact.get("lineage")
        discovered_from = artifact.get("discovered_from")
        if not isinstance(lineage, Mapping) or not isinstance(discovered_from, Mapping):
            raise AcquisitionContractError("fixture.invalid")
        references = [
            lineage.get("parent_artifact_id"),
            lineage.get("source_artifact_id"),
            discovered_from.get("artifact_id"),
        ]
        derived = lineage.get("derived_from_artifact_ids")
        if not isinstance(derived, list):
            raise AcquisitionContractError("fixture.invalid")
        references.extend(derived)
        for reference in references:
            if reference is None:
                continue
            if reference not in ids or reference == artifact_id:
                raise AcquisitionContractError("lineage.invalid")
            edges[artifact_id].add(str(reference))

    pending = {
        artifact_id: set(references) for artifact_id, references in edges.items()
    }
    resolved: set[str] = set()
    while pending:
        ready = {
            artifact_id
            for artifact_id, references in pending.items()
            if references <= resolved
        }
        if not ready:
            raise AcquisitionContractError("lineage.invalid")
        resolved.update(ready)
        for artifact_id in ready:
            del pending[artifact_id]


def _validate_discovery(artifacts: list[Mapping[str, Any]]) -> None:
    by_id = {artifact.get("artifact_id"): artifact for artifact in artifacts}
    for artifact in artifacts:
        role = artifact.get("artifact_role")
        lineage = artifact.get("lineage")
        discovered_from = artifact.get("discovered_from")
        if not isinstance(lineage, Mapping) or not isinstance(discovered_from, Mapping):
            raise AcquisitionContractError("fixture.invalid")

        kind = discovered_from.get("kind")
        discovered_artifact_id = discovered_from.get("artifact_id")
        discovered_source_url = discovered_from.get("source_url")
        parent_artifact_id = lineage.get("parent_artifact_id")
        source_artifact_id = lineage.get("source_artifact_id")

        if kind == "seed":
            valid = (
                role == "source"
                and discovered_artifact_id is None
                and discovered_source_url is None
                and parent_artifact_id is None
            )
        elif kind == "search":
            valid = (
                role == "source"
                and discovered_artifact_id is None
                and isinstance(discovered_source_url, str)
                and parent_artifact_id is None
            )
        elif kind == "link":
            valid = (
                role == "source"
                and isinstance(discovered_artifact_id, str)
                and isinstance(discovered_source_url, str)
                and discovered_artifact_id == parent_artifact_id
            )
        elif kind == "crawler":
            valid = (
                role == "source"
                and isinstance(discovered_source_url, str)
                and (discovered_artifact_id is None) == (parent_artifact_id is None)
                and (
                    discovered_artifact_id is None
                    or discovered_artifact_id == parent_artifact_id
                )
            )
        elif kind == "derived":
            valid = (
                role == "derived"
                and isinstance(discovered_artifact_id, str)
                and isinstance(discovered_source_url, str)
                and discovered_artifact_id == parent_artifact_id == source_artifact_id
            )
        else:
            raise AcquisitionContractError("fixture.invalid")
        if not valid:
            raise AcquisitionContractError("lineage.invalid")

        if discovered_artifact_id is not None:
            discovered_artifact = by_id.get(discovered_artifact_id)
            if (
                discovered_artifact is None
                or discovered_source_url != discovered_artifact.get("final_url")
            ):
                raise AcquisitionContractError("lineage.invalid")


def _validate_artifacts(fixture: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    artifacts = fixture.get("artifacts")
    source_run_id = fixture.get("source_run_id")
    manifest_version = fixture.get("manifest_version")
    if not isinstance(artifacts, list) or not artifacts:
        raise AcquisitionContractError("fixture.invalid")

    typed_artifacts: list[Mapping[str, Any]] = []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise AcquisitionContractError("fixture.invalid")
        typed_artifacts.append(artifact)
        if artifact.get("source_run_id") != source_run_id:
            raise AcquisitionContractError("artifact.identity_mismatch")
        retrieval = artifact.get("retrieval")
        if not isinstance(retrieval, Mapping):
            raise AcquisitionContractError("fixture.invalid")
        size_bytes = retrieval.get("size_bytes")
        if size_bytes is not None and (
            type(size_bytes) is not int
            or not 0 <= size_bytes <= MAX_PORTABLE_JSON_INTEGER
        ):
            raise AcquisitionContractError("fixture.invalid")
        digest = retrieval.get("sha256")
        if digest is not None and not isinstance(digest, str):
            raise AcquisitionContractError("fixture.invalid")
        normalized_identity = artifact.get("normalized_source_identity")
        if not isinstance(normalized_identity, str):
            raise AcquisitionContractError("artifact.identity_mismatch")
        expected_id = artifact_id_for_identity(
            source_run_id=str(source_run_id),
            normalized_source_identity=normalized_identity,
            sha256=digest,
            manifest_version=str(manifest_version),
        )
        if artifact.get("artifact_id") != expected_id:
            raise AcquisitionContractError("artifact.identity_mismatch")
        if not _ARTIFACT_ID_RE.fullmatch(expected_id):
            raise AcquisitionContractError("artifact.identity_mismatch")

        role = artifact.get("artifact_role")
        source_url = artifact.get("source_url")
        lineage = artifact.get("lineage")
        if not isinstance(lineage, Mapping):
            raise AcquisitionContractError("fixture.invalid")
        source_artifact_id = lineage.get("source_artifact_id")
        derived_from_artifact_ids = lineage.get("derived_from_artifact_ids")
        if not isinstance(derived_from_artifact_ids, list):
            raise AcquisitionContractError("fixture.invalid")
        if role == "source":
            if normalized_identity != source_url:
                raise AcquisitionContractError("artifact.identity_mismatch")
            if source_artifact_id is not None or derived_from_artifact_ids:
                raise AcquisitionContractError("lineage.invalid")
        elif role == "derived":
            if not _DERIVED_IDENTITY_RE.fullmatch(normalized_identity):
                raise AcquisitionContractError("artifact.identity_mismatch")
            if not isinstance(
                source_artifact_id, str
            ) or not normalized_identity.startswith(
                f"urn:web-listening:derived:{source_artifact_id}:"
            ):
                raise AcquisitionContractError("lineage.invalid")
            if source_artifact_id not in derived_from_artifact_ids:
                raise AcquisitionContractError("lineage.invalid")
        else:
            raise AcquisitionContractError("fixture.invalid")

        for key in ("requested_url", "source_url", "final_url"):
            _validate_url(artifact.get(key))
        discovered_from = artifact.get("discovered_from")
        if not isinstance(discovered_from, Mapping):
            raise AcquisitionContractError("fixture.invalid")
        discovered_url = discovered_from.get("source_url")
        if discovered_url is not None:
            _validate_url(discovered_url)

        redirects = artifact.get("redirect_chain")
        if not isinstance(redirects, list):
            raise AcquisitionContractError("fixture.invalid")
        expected_from = artifact.get("requested_url")
        for ordinal, hop in enumerate(redirects):
            if not isinstance(hop, Mapping):
                raise AcquisitionContractError("fixture.invalid")
            if hop.get("ordinal") != ordinal or hop.get("from_url") != expected_from:
                raise AcquisitionContractError("redirect.invalid")
            if hop.get("http_status") not in _ALLOWED_REDIRECT_HTTP_STATUSES:
                raise AcquisitionContractError("redirect.invalid")
            _validate_url(hop.get("from_url"))
            _validate_url(hop.get("to_url"))
            if (
                urlsplit(str(hop.get("from_url"))).scheme.casefold() == "https"
                and urlsplit(str(hop.get("to_url"))).scheme.casefold() == "http"
            ):
                raise AcquisitionContractError("redirect.invalid")
            expected_from = hop.get("to_url")
        reject_ordinals = [
            ordinal
            for ordinal, hop in enumerate(redirects)
            if hop.get("decision") == "reject"
        ]
        if reject_ordinals and (
            reject_ordinals != [len(redirects) - 1]
            or artifact.get("artifact_status") != "rejected"
        ):
            raise AcquisitionContractError("redirect.invalid")
        if expected_from != artifact.get("final_url"):
            raise AcquisitionContractError("redirect.invalid")
        if redirects and artifact.get("access_decision_id") != redirects[-1].get(
            "access_decision_id"
        ):
            raise AcquisitionContractError("redirect.invalid")

        artifact_uri = retrieval.get("artifact_uri")
        if digest is None:
            if artifact_uri is not None:
                raise AcquisitionContractError("artifact.identity_mismatch")
        elif artifact_uri != f"artifact:sha256:{digest}":
            raise AcquisitionContractError("artifact.identity_mismatch")

    artifact_statuses = [
        artifact.get("artifact_status") for artifact in typed_artifacts
    ]
    expected_run_status = "partial"
    for uniform_status in ("completed", "rejected", "failed"):
        if all(status == uniform_status for status in artifact_statuses):
            expected_run_status = uniform_status
            break
    if fixture.get("run_status") != expected_run_status:
        raise AcquisitionContractError("status.invalid")

    _validate_discovery(typed_artifacts)
    _validate_lineage(typed_artifacts)
    return typed_artifacts


def _validate_fixture_coverage(
    fixture: Mapping[str, Any], artifacts: list[Mapping[str, Any]]
) -> None:
    retrievals = [artifact["retrieval"] for artifact in artifacts]
    media_types = {retrieval.get("mime_type") for retrieval in retrievals}
    if not {"text/html", "application/pdf", "text/markdown"} <= media_types:
        raise AcquisitionContractError("fixture.coverage_incomplete")
    decisions = {
        hop.get("decision")
        for artifact in artifacts
        for hop in artifact.get("redirect_chain", [])
    }
    if not {"allow", "reject"} <= decisions:
        raise AcquisitionContractError("fixture.coverage_incomplete")
    if not any(artifact["lineage"].get("parent_artifact_id") for artifact in artifacts):
        raise AcquisitionContractError("fixture.coverage_incomplete")
    if not any(artifact["lineage"].get("source_artifact_id") for artifact in artifacts):
        raise AcquisitionContractError("fixture.coverage_incomplete")

    completed = [
        artifact
        for artifact in artifacts
        if artifact.get("artifact_status") == "completed"
    ]
    same_url_new_content = any(
        left.get("normalized_source_identity")
        == right.get("normalized_source_identity")
        and left["retrieval"].get("sha256") != right["retrieval"].get("sha256")
        for index, left in enumerate(completed)
        for right in completed[index + 1 :]
    )
    different_url_same_bytes = any(
        left.get("normalized_source_identity")
        != right.get("normalized_source_identity")
        and left["retrieval"].get("sha256") == right["retrieval"].get("sha256")
        for index, left in enumerate(completed)
        for right in completed[index + 1 :]
    )
    if not same_url_new_content or not different_url_same_bytes:
        raise AcquisitionContractError("fixture.coverage_incomplete")

    replay = fixture.get("replay")
    if not isinstance(replay, Mapping):
        raise AcquisitionContractError("fixture.coverage_incomplete")
    expected_ids = [artifact["artifact_id"] for artifact in artifacts]
    if (
        replay.get("mode") != "idempotent"
        or replay.get("mutates_input") is not False
        or replay.get("expected_artifact_ids") != expected_ids
    ):
        raise AcquisitionContractError("replay.invalid")


def validate_acquisition_contract_bundle(
    bundle_path: str | Path,
) -> dict[str, object]:
    """Validate the canonical producer bundle without network or mutation."""
    bundle = Path(bundle_path)
    if not bundle.is_dir() or bundle.is_symlink():
        raise AcquisitionContractError("bundle.path_invalid")
    try:
        entries = {entry.name: entry for entry in bundle.iterdir()}
    except OSError as exc:
        raise AcquisitionContractError("bundle.path_invalid") from exc
    missing = EXPECTED_FILES - set(entries)
    if missing:
        raise AcquisitionContractError("bundle.missing_file")
    if set(entries) != EXPECTED_FILES:
        raise AcquisitionContractError("bundle.unknown_file")
    if any(not path.is_file() or path.is_symlink() for path in entries.values()):
        raise AcquisitionContractError("bundle.path_invalid")

    schema_raw, schema = _load_json_file(entries["schema.json"])
    fixture_raw, fixture = _load_json_file(entries["fixture.json"])
    _, confirmation = _load_json_file(entries["producer-confirmation.json"])

    evidence_scope = fixture.get("evidence_scope")
    if evidence_scope in {"sample", "sample-only", "test", "test-only"}:
        raise AcquisitionContractError("bundle.sample_only")
    if fixture.get("manifest_version") != CONTRACT_VERSION:
        raise AcquisitionContractError("bundle.version_unsupported")

    schema_sha256 = _sha256(schema_raw)
    fixture_sha256 = _sha256(fixture_raw)
    _validate_confirmation(
        confirmation,
        schema_sha256=schema_sha256,
        fixture_sha256=fixture_sha256,
    )
    _validate_schema_identity(schema)
    try:
        _scan_non_sensitive(fixture)
        Draft202012Validator(
            schema,
            format_checker=_required_format_checker(),
            registry=Registry(retrieve=_reject_schema_retrieval),
        ).validate(fixture)
    except AcquisitionContractError:
        raise
    except Unresolvable as exc:
        raise AcquisitionContractError("schema.reference_invalid") from exc
    except (ValidationError, TypeError, ValueError, RecursionError) as exc:
        reason = "fixture.invalid"
        if isinstance(exc, ValidationError):
            path = set(exc.absolute_path)
            if "redirect_chain" in path:
                reason = "redirect.invalid"
            elif "lineage" in path or "discovered_from" in path:
                reason = "lineage.invalid"
        raise AcquisitionContractError(reason) from exc

    artifacts = _validate_artifacts(fixture)
    _validate_fixture_coverage(fixture, artifacts)

    return {
        "schema_version": VALIDATION_VERSION,
        "valid": True,
        "reason_code": "contract.valid",
        "message": "acquisition contract bundle is valid",
        "contract_version": CONTRACT_VERSION,
        "producer_repository": EXPECTED_CONFIRMATION_IDENTITY["producer_repository"],
        "producer_feature_issue": EXPECTED_CONFIRMATION_IDENTITY[
            "producer_feature_issue"
        ],
        "producer_contract_issue": EXPECTED_CONFIRMATION_IDENTITY[
            "producer_contract_issue"
        ],
        "evidence_scope": EXPECTED_CONFIRMATION_IDENTITY["evidence_scope"],
        "schema_sha256": schema_sha256,
        "fixture_sha256": fixture_sha256,
        "artifact_count": len(artifacts),
    }


__all__ = [
    "CONTRACT_VERSION",
    "EXPECTED_FIXTURE_SHA256",
    "EXPECTED_SCHEMA_SHA256",
    "MAX_PORTABLE_JSON_INTEGER",
    "VALIDATION_VERSION",
    "AcquisitionContractError",
    "acquisition_contract_error_result",
    "artifact_id_for_identity",
    "artifact_identity_sha256",
    "validate_acquisition_contract_bundle",
]
