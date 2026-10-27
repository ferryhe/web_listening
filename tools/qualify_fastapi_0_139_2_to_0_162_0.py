"""Capture and compare the isolated FastAPI 0.139.2 / 0.162.0 qualification.

Run this from an isolated Python 3.12.14 Linux/amd64 environment with the
project wheel installed. Snapshot normalization sorts JSON object keys only;
it preserves every OpenAPI value and array, so schema or behavior changes stay
visible.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import platform
import re
from pathlib import Path
from typing import Any

QUALIFICATION_VERSION = "fastapi-0.139.2-to-0.162.0-qualification.v1"
FASTAPI_VERSIONS = {"baseline": "0.139.2", "candidate": "0.162.0"}
EXPECTED_MCP = "1.28.1"
BUILD_MANIFEST = {
    "source_commit": "fd541f07942d7cdcb6a554225bbcbfec2f20147f",
    "python": "3.12.14",
    "pip": "25.0.1",
    "setuptools": "80.9.0",
    "wheel": "0.45.1",
    "extras": ["dev", "mcp"],
}
HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_installed_wheel(
    distribution: importlib.metadata.Distribution,
    *,
    supplied_wheel_sha256: str,
    imported_version: str,
    openapi_version: str,
) -> dict[str, str]:
    metadata_version = distribution.version
    if metadata_version != imported_version:
        raise ValueError(
            "installed web-listening METADATA version "
            f"{metadata_version!r} does not match imported package version "
            f"{imported_version!r}"
        )
    if metadata_version != openapi_version:
        raise ValueError(
            "installed web-listening METADATA version "
            f"{metadata_version!r} does not match OpenAPI info.version "
            f"{openapi_version!r}"
        )

    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise ValueError(
            "installed web-listening distribution has no direct_url.json wheel provenance"
        )
    try:
        direct_url = json.loads(direct_url_text)
    except json.JSONDecodeError as exc:
        raise ValueError("installed web-listening direct_url.json is invalid JSON") from exc
    if not isinstance(direct_url, dict) or not isinstance(direct_url.get("url"), str):
        raise ValueError("installed web-listening direct_url.json has no source URL")

    archive_info = direct_url.get("archive_info")
    if not isinstance(archive_info, dict):
        raise ValueError(
            "installed web-listening direct_url.json has no archive_info wheel provenance"
        )
    hashes = archive_info.get("hashes")
    archive_sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
    legacy_hash = archive_info.get("hash")
    if archive_sha256 is None and isinstance(legacy_hash, str):
        algorithm, separator, value = legacy_hash.partition("=")
        if separator and algorithm.lower() == "sha256":
            archive_sha256 = value
    if not isinstance(archive_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", archive_sha256) is None:
        raise ValueError(
            "installed web-listening direct_url.json has no valid archive SHA-256"
        )
    if isinstance(legacy_hash, str) and legacy_hash.startswith("sha256="):
        if legacy_hash.removeprefix("sha256=") != archive_sha256:
            raise ValueError(
                "installed web-listening direct_url.json contains conflicting SHA-256 values"
            )
    if archive_sha256 != supplied_wheel_sha256:
        raise ValueError(
            "installed web-listening archive SHA-256 "
            f"{archive_sha256!r} does not match supplied wheel SHA-256 "
            f"{supplied_wheel_sha256!r}"
        )

    return {
        "metadata_version": metadata_version,
        "direct_url": str(direct_url.get("url", "")),
        "archive_sha256": archive_sha256,
    }


def _installed_packages() -> list[dict[str, str]]:
    packages = {
        distribution.metadata["Name"].lower().replace("_", "-"): distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    return [{"name": name, "version": version} for name, version in sorted(packages.items())]


def _api_probe_snapshot() -> dict[str, object]:
    from fastapi.testclient import TestClient
    from web_listening.api.app import create_app

    app = create_app()
    openapi = app.openapi()
    routes = [
        {"path": path, "method": method.upper(), "responses": operation.get("responses", {})}
        for path, path_item in sorted(openapi["paths"].items())
        for method, operation in sorted(path_item.items())
        if method in HTTP_METHODS
    ]

    malformed_body = b'{"scope_path":'
    with TestClient(app) as client:
        malformed = client.post(
            "/api/v1/acquisition/execution-plans/preview",
            content=malformed_body,
            headers={"content-type": "application/json"},
        )
        invalid_request = client.post("/api/v1/acquisition/probe", json={})
        disabled_legacy = client.post("/api/v1/sites/1/check")
        catalog = client.get("/api/v1/acquisition/tools")
        preview_without_profile = client.post(
            "/api/v1/acquisition/execution-plans/preview",
            json={"scope_path": "../qualification-canary.yaml"},
        )
        preview_with_null_profile = client.post(
            "/api/v1/acquisition/execution-plans/preview",
            json={"scope_path": "../qualification-canary.yaml", "profile_path": None},
        )

    responses = {
        "malformed_preview_json": {
            "status": malformed.status_code,
            "body": malformed.json(),
        },
        "missing_probe_fields": {
            "status": invalid_request.status_code,
            "body": invalid_request.json(),
        },
        "optional_profile_omitted": {
            "status": preview_without_profile.status_code,
            "body": preview_without_profile.json(),
        },
        "optional_profile_explicit_null": {
            "status": preview_with_null_profile.status_code,
            "body": preview_with_null_profile.json(),
        },
        "disabled_legacy_check": {
            "status": disabled_legacy.status_code,
            "body": disabled_legacy.json(),
        },
        "acquisition_tools_catalog": {
            "status": catalog.status_code,
            "contract_version": catalog.json().get("contract_version"),
            "tool_names": sorted(
                tool["adapter"] for tool in catalog.json().get("tools", [])
            ),
        },
    }
    return {
        "openapi": openapi,
        "routes": routes,
        "responses": responses,
        "testclient_lifespan": {"entered": True, "exited": True},
    }


def _packaged_tree_catalog_defaults() -> dict[str, object]:
    from web_listening.tree_targets import DEV_TARGETS_PATH, SMOKE_TARGETS_PATH

    return {
        "dev": {"path": str(DEV_TARGETS_PATH), "exists": DEV_TARGETS_PATH.is_file()},
        "smoke": {
            "path": str(SMOKE_TARGETS_PATH),
            "exists": SMOKE_TARGETS_PATH.is_file(),
        },
        "note": "These defaults resolve relative to the installed module; repository config files are not included in the wheel.",
    }


async def _mcp_tool_names() -> list[str]:
    from web_listening.mcp.server import create_server

    return sorted(tool.name for tool in await create_server().list_tools())


def capture(matrix: str, wheel: Path) -> dict[str, object]:
    if matrix not in FASTAPI_VERSIONS:
        raise ValueError(f"unknown matrix: {matrix}")
    actual_python = platform.python_version()
    actual_fastapi = importlib.metadata.version("fastapi")
    actual_mcp = importlib.metadata.version("mcp")
    if actual_python != "3.12.14":
        raise ValueError(f"expected Python 3.12.14, got {actual_python}")
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError(f"expected Linux/amd64, got {platform.system()}/{platform.machine()}")
    if actual_fastapi != FASTAPI_VERSIONS[matrix]:
        raise ValueError(f"expected FastAPI {FASTAPI_VERSIONS[matrix]}, got {actual_fastapi}")
    if actual_mcp != EXPECTED_MCP:
        raise ValueError(f"expected MCP {EXPECTED_MCP}, got {actual_mcp}")

    import web_listening

    module_path = Path(web_listening.__file__).resolve()
    source_root = Path(__file__).resolve().parents[1]
    if source_root == module_path or source_root in module_path.parents:
        raise ValueError(f"project imported from source tree: {module_path}")
    if "site-packages" not in module_path.parts:
        raise ValueError(f"project import is not from site-packages: {module_path}")

    api_snapshot = _api_probe_snapshot()
    wheel_sha256 = sha256(wheel)
    distribution = importlib.metadata.distribution("web-listening")
    openapi_version = api_snapshot["openapi"]["info"]["version"]
    installed_wheel = _verify_installed_wheel(
        distribution,
        supplied_wheel_sha256=wheel_sha256,
        imported_version=web_listening.__version__,
        openapi_version=openapi_version,
    )

    return {
        "schema_version": QUALIFICATION_VERSION,
        "matrix": matrix,
        "build": BUILD_MANIFEST,
        "environment": {
            "python": actual_python,
            "platform": f"{platform.system()}/{platform.machine().lower()}",
            "fastapi": actual_fastapi,
            "starlette": importlib.metadata.version("starlette"),
            "pydantic": importlib.metadata.version("pydantic"),
            "anyio": importlib.metadata.version("anyio"),
            "httpx": importlib.metadata.version("httpx"),
            "mcp": actual_mcp,
            "packages": _installed_packages(),
        },
        "project_wheel": {
            "filename": wheel.name,
            "sha256": wheel_sha256,
            "installed_import_path": str(module_path),
            "imported_version": web_listening.__version__,
            "installed_distribution_version": installed_wheel["metadata_version"],
            "openapi_info_version": openapi_version,
            "installed_archive_sha256": installed_wheel["archive_sha256"],
            "installed_direct_url": installed_wheel["direct_url"],
        },
        "api": api_snapshot,
        "mcp_tool_names": asyncio.run(_mcp_tool_names()),
        "packaged_tree_catalog_defaults": _packaged_tree_catalog_defaults(),
        "normalization": {
            "rule": "recursively sort JSON object keys; preserve every value and array order",
            "removed_fields": [],
            "business_fields_ignored": [],
        },
    }


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, object]:
    if baseline.get("matrix") != "baseline" or candidate.get("matrix") != "candidate":
        raise ValueError("comparison requires baseline and candidate snapshots")
    checks = {
        "same_build_manifest": baseline["build"] == candidate["build"],
        "same_project_wheel": baseline["project_wheel"]["sha256"] == candidate["project_wheel"]["sha256"],
        "installed_distribution_matches_project_wheel": all(
            snapshot["project_wheel"]["installed_archive_sha256"]
            == snapshot["project_wheel"]["sha256"]
            and snapshot["project_wheel"]["installed_distribution_version"]
            == snapshot["project_wheel"]["imported_version"]
            == snapshot["project_wheel"]["openapi_info_version"]
            for snapshot in (baseline, candidate)
        ),
        "same_api_routes_and_status_schemas": baseline["api"]["routes"] == candidate["api"]["routes"],
        "same_full_openapi": baseline["api"]["openapi"] == candidate["api"]["openapi"],
        "same_validation_error_and_endpoint_payloads": baseline["api"]["responses"] == candidate["api"]["responses"],
        "same_testclient_lifespan": baseline["api"]["testclient_lifespan"] == candidate["api"]["testclient_lifespan"],
        "same_mcp_tool_names": baseline["mcp_tool_names"] == candidate["mcp_tool_names"],
    }
    baseline_packages = {item["name"]: item["version"] for item in baseline["environment"]["packages"]}
    candidate_packages = {item["name"]: item["version"] for item in candidate["environment"]["packages"]}
    package_changes = [
        {"name": name, "baseline": baseline_packages.get(name), "candidate": candidate_packages.get(name)}
        for name in sorted(baseline_packages.keys() | candidate_packages.keys())
        if baseline_packages.get(name) != candidate_packages.get(name)
    ]
    return {
        "schema_version": QUALIFICATION_VERSION,
        "checks": checks,
        "package_changes": package_changes,
        "compatible": all(checks.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture", help="capture one installed matrix")
    capture_parser.add_argument("--matrix", choices=sorted(FASTAPI_VERSIONS), required=True)
    capture_parser.add_argument("--wheel", type=Path, required=True)
    capture_parser.add_argument("--write", type=Path, required=True)
    compare_parser = subparsers.add_parser("compare", help="compare captured matrices")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "capture":
        result = capture(args.matrix, args.wheel)
    else:
        result = compare(
            json.loads(args.baseline.read_text(encoding="utf-8")),
            json.loads(args.candidate.read_text(encoding="utf-8")),
        )
    output = canonical_json(result) + "\n"
    args.write.parent.mkdir(parents=True, exist_ok=True)
    args.write.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if args.command == "capture" or result["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
