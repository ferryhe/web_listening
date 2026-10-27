from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.qualify_fastapi_0_139_2_to_0_162_0 import (
    BUILD_MANIFEST,
    _verify_installed_wheel,
    canonical_json,
    compare,
)

FIXTURES = Path(__file__).parents[1] / "docs/testing/fixtures"
WHEEL_SHA256 = "a" * 64


class _FakeDistribution:
    version = "3.2.0"

    def __init__(self, direct_url: str | None) -> None:
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url


def _direct_url(sha256: str) -> str:
    return json.dumps(
        {
            "archive_info": {
                "hash": f"sha256={sha256}",
                "hashes": {"sha256": sha256},
            },
            "url": "file:///tmp/web_listening-3.2.0-py3-none-any.whl",
        }
    )


def test_installed_wheel_requires_matching_pep610_archive_hash() -> None:
    evidence = _verify_installed_wheel(
        _FakeDistribution(_direct_url(WHEEL_SHA256)),
        supplied_wheel_sha256=WHEEL_SHA256,
        imported_version="3.2.0",
        openapi_version="3.2.0",
    )

    assert evidence["archive_sha256"] == WHEEL_SHA256
    assert evidence["metadata_version"] == "3.2.0"
    assert evidence["direct_url"].startswith("file:")


def test_installed_wheel_rejects_missing_pep610_provenance() -> None:
    with pytest.raises(ValueError, match="no direct_url.json wheel provenance"):
        _verify_installed_wheel(
            _FakeDistribution(None),
            supplied_wheel_sha256=WHEEL_SHA256,
            imported_version="3.2.0",
            openapi_version="3.2.0",
        )


def test_installed_wheel_rejects_different_pep610_archive_hash() -> None:
    with pytest.raises(ValueError, match="does not match supplied wheel SHA-256"):
        _verify_installed_wheel(
            _FakeDistribution(_direct_url("b" * 64)),
            supplied_wheel_sha256=WHEEL_SHA256,
            imported_version="3.2.0",
            openapi_version="3.2.0",
        )


def test_installed_wheel_rejects_version_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match imported package version"):
        _verify_installed_wheel(
            _FakeDistribution(_direct_url(WHEEL_SHA256)),
            supplied_wheel_sha256=WHEEL_SHA256,
            imported_version="3.2.1",
            openapi_version="3.2.0",
        )


def test_installed_wheel_rejects_openapi_info_version_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match OpenAPI info.version"):
        _verify_installed_wheel(
            _FakeDistribution(_direct_url(WHEEL_SHA256)),
            supplied_wheel_sha256=WHEEL_SHA256,
            imported_version="3.2.0",
            openapi_version="3.2.1",
        )


def _snapshot(matrix: str, *, openapi: dict | None = None) -> dict:
    return {
        "schema_version": "fastapi-0.139.2-to-0.162.0-qualification.v1",
        "matrix": matrix,
        "build": BUILD_MANIFEST,
        "project_wheel": {
            "sha256": "same-wheel",
            "installed_archive_sha256": "same-wheel",
            "installed_distribution_version": "3.2.0",
            "imported_version": "3.2.0",
            "openapi_info_version": "3.2.0",
        },
        "environment": {"packages": [{"name": "fastapi", "version": "0.139.2"}]},
        "api": {
            "routes": [{"path": "/probe", "method": "POST", "responses": {"422": {}}}],
            "openapi": openapi or {"paths": {"/probe": {"post": {"requestBody": {"required": True}}}}},
            "responses": {"invalid": {"status": 422, "body": {"detail": []}}},
            "testclient_lifespan": {"entered": True, "exited": True},
        },
        "mcp_tool_names": ["tool-a"],
    }


def test_canonical_json_sorts_objects_and_preserves_array_order() -> None:
    assert canonical_json({"b": [2, 1], "a": {"z": 0, "x": 1}}) == (
        '{"a":{"x":1,"z":0},"b":[2,1]}'
    )


def test_compare_reports_semantic_api_change_without_normalizing_it_away() -> None:
    baseline = _snapshot("baseline")
    candidate = _snapshot("candidate", openapi={"paths": {"/probe": {"post": {"requestBody": {"required": False}}}}})

    result = compare(baseline, candidate)

    assert result["checks"]["same_api_routes_and_status_schemas"] is True
    assert result["checks"]["same_full_openapi"] is False
    assert result["compatible"] is False


def test_compare_reports_each_dependency_delta() -> None:
    baseline = _snapshot("baseline")
    baseline["environment"]["packages"] = [
        {"name": "fastapi", "version": "0.139.2"},
        {"name": "removed-package", "version": "1.0"},
    ]
    candidate = _snapshot("candidate")
    candidate["environment"]["packages"] = [
        {"name": "fastapi", "version": "0.162.0"},
        {"name": "added-package", "version": "2.0"},
    ]

    changes = compare(baseline, candidate)["package_changes"]

    assert changes == [
        {"name": "added-package", "baseline": None, "candidate": "2.0"},
        {"name": "fastapi", "baseline": "0.139.2", "candidate": "0.162.0"},
        {"name": "removed-package", "baseline": "1.0", "candidate": None},
    ]


def test_committed_matrix_evidence_and_hash_locks_are_closed_and_compatible() -> None:
    baseline = json.loads(
        (FIXTURES / "fastapi-0.139.2-linux-amd64-py3.12.14.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = json.loads(
        (FIXTURES / "fastapi-0.162.0-linux-amd64-py3.12.14.json").read_text(
            encoding="utf-8"
        )
    )
    comparison = json.loads(
        (FIXTURES / "fastapi-0.139.2-to-0.162.0-comparison.json").read_text(
            encoding="utf-8"
        )
    )

    assert compare(baseline, candidate) == comparison
    assert comparison["compatible"] is True
    assert baseline["build"] == candidate["build"] == BUILD_MANIFEST
    for snapshot in (baseline, candidate):
        project_wheel = snapshot["project_wheel"]
        assert project_wheel["installed_distribution_version"] == "3.2.0"
        assert project_wheel["imported_version"] == project_wheel["openapi_info_version"]
        assert project_wheel["installed_archive_sha256"] == project_wheel["sha256"]
        assert project_wheel["installed_direct_url"].startswith("file:")
        defaults = snapshot["packaged_tree_catalog_defaults"]
        assert defaults["dev"]["exists"] is False
        assert defaults["smoke"]["exists"] is False
        assert "/site-packages/config/" in defaults["dev"]["path"]
        assert "/site-packages/config/" in defaults["smoke"]["path"]
    for snapshot, version in ((baseline, "0.139.2"), (candidate, "0.162.0")):
        assert snapshot["environment"]["fastapi"] == version
        locked = {}
        lock_path = FIXTURES / f"fastapi-{version}-linux-amd64-py3.12.14.lock"
        for line in lock_path.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(
                r"([a-z0-9][a-z0-9_.-]*)==([^ ]+) --hash=sha256:([0-9a-f]{64})",
                line,
            )
            assert match is not None, line
            name, resolved_version, _digest = match.groups()
            assert name not in locked
            locked[name.replace("_", "-").lower()] = resolved_version
        resolved = {
            item["name"]: item["version"]
            for item in snapshot["environment"]["packages"]
            if item["name"] not in {"pip", "setuptools", "web-listening"}
        }
        assert locked == resolved
        assert snapshot["project_wheel"]["sha256"] == baseline["project_wheel"]["sha256"]
        assert snapshot["normalization"]["removed_fields"] == []
        assert snapshot["normalization"]["business_fields_ignored"] == []
