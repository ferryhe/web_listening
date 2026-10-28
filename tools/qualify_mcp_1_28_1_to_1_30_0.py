"""Capture an isolated MCP 1.28.1 / 1.30.0 qualification on Linux/amd64.

The MCP probes use the real stdio client/server transport. Target-like reader
services are replaced in the child process with fixed offline results; the
qualification never sends a public target request.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

QUALIFICATION_VERSION = "mcp-1.28.1-to-1.30.0-fastapi-0.139.2-qualification.v1"
SOURCE_COMMIT = "c119453726be53c0bfd30ca9f98e19567069fde8"
PYTHON_VERSION = "3.12.14"
FASTAPI_VERSION = "0.139.2"
PROJECT_VERSION = "3.2.0"
MCP_VERSIONS = {"baseline": "1.28.1", "candidate": "1.30.0"}
COMBINATIONS = {"base": None, "dev": "dev", "mcp": "mcp"}
BUILD_MANIFEST = {
    "source_commit": SOURCE_COMMIT,
    "python": PYTHON_VERSION,
    "pip": "25.0.1",
    "setuptools": "80.9.0",
    "wheel": "0.45.1",
    "docker_image": "python:3.12.14-slim",
    "docker_image_sha256": "f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f",
    "platform": "Linux/amd64",
}
EXPECTED_TOOL_NAMES = sorted(
    {
        "web_listening_list_acquisition_tools",
        "web_listening_probe_tool_once",
        "web_listening_recommend_next_tool",
        "web_listening_fetch_article_content",
        "web_listening_acquire_with_fallback",
        "web_listening_bootstrap_scope",
        "web_listening_run_scope",
        "web_listening_report_scope",
        "web_listening_export_manifest",
        "web_listening_get_job",
        "web_listening_read_artifact",
    }
)
MCP_CANCELLATION_SCHEMA = "mcp-stdio-cancellation-lifecycle.v1"
HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}

# The server child keeps FastMCP, its registered functions, stdio framing, and
# MCP serialization real. Only the target-reading service seams return local
# deterministic values, and the workflow probes use nonexistent local paths.
CHILD_BOOTSTRAP = r'''
import asyncio
import importlib
import importlib.metadata
import json
import os
import sys
from types import SimpleNamespace
from pathlib import Path
from web_listening.blocks.acquisition_profile import CaptureAttempt, build_default_acquisition_profile
from web_listening.contracts.tool_result import ToolResult
from web_listening.mcp import tools
import web_listening
import web_listening.blocks.article_content as article_content
server_module = importlib.import_module("web_listening.mcp.server")

distribution = importlib.metadata.distribution("web-listening")
direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
archive_info = direct_url.get("archive_info", {})
archive_hash = (archive_info.get("hashes") or {}).get("sha256")
if archive_hash is None and isinstance(archive_info.get("hash"), str):
    algorithm, separator, value = archive_info["hash"].partition("=")
    if separator and algorithm == "sha256":
        archive_hash = value
module_paths = {
    name: str(Path(module.__file__).resolve())
    for name, module in {
        "web_listening": web_listening,
        "web_listening.blocks.article_content": article_content,
        "web_listening.mcp.server": server_module,
        "web_listening.mcp.tools": tools,
    }.items()
}
site_packages = Path(web_listening.__file__).resolve().parent.parent
if (distribution.version != os.environ.get("WL_EXPECTED_PROJECT_VERSION")
        or archive_hash != os.environ.get("WL_EXPECTED_PROJECT_WHEEL_SHA256")
        or any(not Path(path).is_relative_to(site_packages) for path in module_paths.values())):
    raise RuntimeError("stdio child project modules did not load from the expected installed wheel")
Path(os.environ["WL_DATA_DIR"], "child-import-provenance.json").write_text(
    json.dumps({
        "distribution": "web-listening",
        "version": distribution.version,
        "wheel_archive_sha256": archive_hash,
        "child_executable": sys.executable,
        "site_packages_root": str(site_packages),
        "module_paths": module_paths,
    }, sort_keys=True),
    encoding="utf-8",
)

def offline_probe(**kwargs):
    profile = build_default_acquisition_profile(
        site_key="mcp-qualification", allowed_domains=["example.com"]
    ).model_dump(mode="json")
    attempt = CaptureAttempt(
        adapter="web_http", status="passed", url=kwargs["url"],
        final_url=kwargs["url"], status_code=200, word_count=240, link_count=3,
        document_link_count=1,
    ).model_dump(mode="json")
    return {"contract_version": "acquisition-probe.v1", "profile": profile,
            "attempt": attempt, "available_tools": {}, "next_action": "use_adapter_output"}

def offline_fallback(url, **kwargs):
    return ToolResult(
        ok=True, has_data=True, data_status="present", data_count=1,
        tool="web_http", data={"final_url": url, "content_text_preview": "offline fixture"},
        stop_reason="usable_data_found",
    )

tools.probe_acquisition_url = offline_probe
tools.acquire_with_fallback_result = offline_fallback

article_content.fetch_article_content = lambda url, **kwargs: SimpleNamespace(
    model_dump=lambda mode: {
        "ok": True, "has_data": True, "data_status": "present", "data_count": 1,
        "tool": "web_http", "data": {"final_url": url, "text": "offline fixture"},
        "stop_reason": "usable_data_found",
    }
)

original_get_job = tools.web_listening_get_job
request_lifecycle_path = Path(os.environ["WL_CANCELLATION_MARKER_PATH"])
request_lifecycle_path.parent.mkdir(parents=True, exist_ok=True)
def record_request_lifecycle(event):
    with request_lifecycle_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"request": "get_job:99999999", "event": event}) + "\n")
        stream.flush()
async def cancellable_get_job(job_id: int) -> dict[str, object]:
    if job_id == 99999999:
        record_request_lifecycle("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            record_request_lifecycle("cancelled")
            raise
        finally:
            record_request_lifecycle("finished")
    return original_get_job(job_id)
tools.web_listening_get_job = cancellable_get_job

server_module.main()
'''


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _packages() -> list[dict[str, str]]:
    return sorted(
        [
            {
                "name": dist.metadata["Name"].lower().replace("_", "-"),
                "version": dist.version,
            }
            for dist in importlib.metadata.distributions()
            if dist.metadata.get("Name")
        ],
        key=lambda item: item["name"],
    )


def _lock_packages(lock_path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([^ ]+) --hash=sha256:([0-9a-f]{64})",
            line,
        )
        if match is None:
            raise ValueError(f"invalid hash lock row: {line!r}")
        name, version, _digest = match.groups()
        key = name.lower().replace("_", "-").replace(".", "-")
        if key in locked:
            raise ValueError(f"duplicate package in hash lock: {name}")
        locked[key] = version
    if not locked:
        raise ValueError("hash lock is empty")
    return locked


def render_hash_lock(report: dict[str, Any], *, excluded: set[str] | None = None) -> str:
    """Render a complete pip install-report lock using each selected artifact hash."""
    excluded_names = {name.lower().replace("_", "-") for name in excluded or {"web-listening"}}
    rows: dict[str, tuple[str, str]] = {}
    for item in report.get("install", []):
        metadata = item.get("metadata", {})
        name = str(metadata.get("name", "")).lower().replace("_", "-")
        version = str(metadata.get("version", ""))
        if not name or not version:
            raise ValueError("pip install report has an unnamed or unversioned distribution")
        if name in excluded_names:
            continue
        info = item.get("download_info", {}).get("archive_info", {})
        hashes = info.get("hashes", {})
        digest = hashes.get("sha256")
        if digest is None and isinstance(info.get("hash"), str):
            algorithm, separator, value = info["hash"].partition("=")
            if separator and algorithm.lower() == "sha256":
                digest = value
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"pip install report has no selected wheel SHA-256 for {name}=={version}")
        if name in rows:
            raise ValueError(f"pip install report contains duplicate distribution {name}")
        rows[name] = (version, digest)
    if not rows:
        raise ValueError("pip install report resolved no lockable distributions")
    return "".join(f"{name}=={version} --hash=sha256:{digest}\n" for name, (version, digest) in sorted(rows.items()))


def _check_extra_constraints() -> dict[str, object]:
    from pip._vendor.packaging.requirements import Requirement

    distribution = importlib.metadata.distribution("web-listening")
    requirements = distribution.requires or []
    result: dict[str, object] = {}
    for extra in ("dev", "mcp"):
        matches: list[Requirement] = []
        for raw in requirements:
            requirement = Requirement(raw)
            if requirement.name.lower() == "mcp" and requirement.marker is not None and requirement.marker.evaluate({"extra": extra}):
                matches.append(requirement)
        bounds = set(str(matches[0].specifier).replace(" ", "").split(",")) if len(matches) == 1 else set()
        if bounds != {">=1.28.1", "<2.0.0"}:
            raise ValueError(f"extra {extra!r} does not preserve mcp>=1.28.1,<2.0.0: {matches}")
        result[extra] = f"{matches[0].name}{matches[0].specifier}"
    return result


def _api_snapshot() -> dict[str, object]:
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
    with TestClient(app) as client:
        malformed = client.post(
            "/api/v1/acquisition/execution-plans/preview",
            content=b'{"scope_path":',
            headers={"content-type": "application/json"},
        )
        invalid = client.post("/api/v1/acquisition/probe", json={})
        omitted = client.post(
            "/api/v1/acquisition/execution-plans/preview",
            json={"scope_path": "../qualification-canary.yaml"},
        )
        explicit_null = client.post(
            "/api/v1/acquisition/execution-plans/preview",
            json={"scope_path": "../qualification-canary.yaml", "profile_path": None},
        )
        disabled = client.post("/api/v1/sites/1/check")
        catalog = client.get("/api/v1/acquisition/tools")
    return {
        "openapi": openapi,
        "routes": routes,
        "responses": {
            "malformed_preview_json": {"status": malformed.status_code, "body": malformed.json()},
            "missing_probe_fields": {"status": invalid.status_code, "body": invalid.json()},
            "optional_profile_omitted": {"status": omitted.status_code, "body": omitted.json()},
            "optional_profile_explicit_null": {"status": explicit_null.status_code, "body": explicit_null.json()},
            "disabled_legacy_check": {"status": disabled.status_code, "body": disabled.json()},
            "acquisition_tools_catalog": {
                "status": catalog.status_code,
                "contract_version": catalog.json().get("contract_version"),
                "tool_names": sorted(tool["adapter"] for tool in catalog.json().get("tools", [])),
            },
        },
        "testclient_lifespan": {"entered": True, "exited": True},
    }


def _run_cli(args: list[str], *, cwd: Path, env: dict[str, str]) -> dict[str, object]:
    local_entrypoint = Path(sys.executable).with_name("web-listening")
    command = str(local_entrypoint) if local_entrypoint.is_file() else shutil.which("web-listening")
    if command is None:
        raise ValueError("web-listening entry point is not installed")
    completed = subprocess.run(
        [command, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30, check=False
    )
    return {"argv": ["web-listening", *args], "returncode": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr}


def _cli_snapshot() -> dict[str, object]:
    cwd = Path("/tmp/issue84-cli-cwd")
    cwd.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "NO_COLOR": "1", "PYTHONNOUSERSITE": "1", "COLUMNS": "120"}
    help_output = _run_cli(["--help"], cwd=cwd, env=env)
    catalog_first = _run_cli(["list-acquisition-tools", "--json"], cwd=cwd, env=env)
    catalog_second = _run_cli(["list-acquisition-tools", "--json"], cwd=cwd, env=env)
    return {
        "help": help_output,
        "catalog_first": catalog_first,
        "catalog_second": catalog_second,
        "catalog_idempotent": catalog_first == catalog_second,
    }


def _child_pids() -> list[int]:
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    if not children.is_file():
        return []
    content = children.read_text(encoding="ascii").strip()
    return sorted(int(item) for item in content.split())


def _tool_arguments(name: str) -> dict[str, Any]:
    missing_scope = "/tmp/issue84-mcp-data/missing-scope.yaml"
    missing_profile = "/tmp/issue84-mcp-data/missing-profile.yaml"
    args: dict[str, dict[str, Any]] = {
        "web_listening_list_acquisition_tools": {},
        "web_listening_probe_tool_once": {
            "url": "https://example.com/report", "site_key": "example",
            "adapter": "web_http", "safety": {"allowed_domains": ["example.com"]},
        },
        "web_listening_recommend_next_tool": {
            "strategy": "public_web_default",
            "attempts": [{"tool": "web_http", "data_status": "failed_quality_gate",
                          "data_quality": {"word_count": 10, "link_count": 1}}],
            "safety": {"allowed_domains": ["example.com"]},
        },
        "web_listening_fetch_article_content": {
            "url": "https://example.com/report", "site_key": "example",
            "allowed_domains": ["example.com"],
        },
        "web_listening_acquire_with_fallback": {
            "url": "https://example.com/report", "site_key": "example",
            "goal": "offline qualification", "max_attempts": 1,
        },
        "web_listening_bootstrap_scope": {
            "scope_path": missing_scope, "acquisition_profile_path": missing_profile,
        },
        "web_listening_run_scope": {
            "scope_path": missing_scope, "acquisition_profile_path": missing_profile,
        },
        "web_listening_report_scope": {"scope_path": missing_scope},
        "web_listening_export_manifest": {"scope_path": missing_scope},
        "web_listening_get_job": {"job_id": 2147483647},
        "web_listening_read_artifact": {"path": "sample.txt", "inline_limit": 64},
    }
    try:
        return args[name]
    except KeyError as exc:
        raise ValueError(f"no valid MCP probe was defined for {name}") from exc


def _invalid_arguments(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema", {})
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if required:
        key = required[0]
        field = properties.get(key, {})
        types = field.get("type")
        if isinstance(types, list):
            types = next((item for item in types if item != "null"), None)
        if types == "string":
            bad: object = {"qualification": "wrong-type"}
        elif types == "integer" or types == "number":
            bad = "not-a-number"
        elif types == "boolean":
            bad = "not-a-boolean"
        elif types == "array":
            bad = {"not": "an-array"}
        elif types == "object":
            bad = ["not", "an-object"]
        else:
            bad = {"qualification": "wrong-type"}
        return {key: bad}
    return {"qualification_unknown_argument": True}


def _result_payload(result: object) -> dict[str, object]:
    return result.model_dump(mode="json", by_alias=True, exclude_none=False)


def _read_handler_events(marker_path: Path) -> list[str]:
    if not marker_path.exists():
        return []
    return [json.loads(line)["event"] for line in marker_path.read_text(encoding="utf-8").splitlines()]


def _handler_state(events: list[str]) -> str:
    if "finished" in events:
        return "finished"
    if "cancelled" in events:
        return "cancelled"
    if "started" in events:
        return "running"
    return "not_started"


async def _wait_for_handler_start(marker_path: Path) -> list[str]:
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        events = _read_handler_events(marker_path)
        if "started" in events:
            return events
        await asyncio.sleep(0.01)
    raise TimeoutError("stdio child did not start the original cancellable get_job handler")


def _valid_cancellation_evidence(value: object) -> bool:
    if not isinstance(value, dict) or "server_responded_after_cancel" in value:
        return False
    original = value.get("original_request")
    followup = value.get("followup_request")
    notifications = value.get("client_outgoing_notification_methods")
    return (
        value.get("probe_schema") == MCP_CANCELLATION_SCHEMA
        and value.get("local_client_task_cancelled") is True
        and isinstance(original, dict)
        and original.get("handler_started_before_cancel") is True
        and original.get("handler_state_before_transport_eof") in {"running", "cancelled", "finished"}
        and original.get("handler_state_after_transport_eof") in {"running", "cancelled", "finished"}
        and isinstance(original.get("handler_cancelled_before_transport_eof"), bool)
        and isinstance(original.get("handler_cancelled_after_transport_eof"), bool)
        and isinstance(value.get("cancellation_notification_observed"), bool)
        and isinstance(followup, dict)
        and followup.get("response_received_before_transport_eof") is True
        and isinstance(notifications, list)
    )


async def _stdio_probe(
    data_dir: Path, marker_path: Path, wheel_info: dict[str, str]
) -> dict[str, object]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = {
        **os.environ,
        "WL_DATA_DIR": str(data_dir),
        "WL_CANCELLATION_MARKER_PATH": str(marker_path),
        "WL_EXPECTED_PROJECT_VERSION": wheel_info["version"],
        "WL_EXPECTED_PROJECT_WHEEL_SHA256": wheel_info["sha256"],
        "NO_COLOR": "1",
        "PYTHONNOUSERSITE": "1",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", CHILD_BOOTSTRAP],
        cwd="/tmp/issue84-cli-cwd",
        env=env,
    )
    before = _child_pids()
    notification_methods: list[str] = []
    local_task_cancelled = False
    handler_events_before_eof: list[str] = []
    followup_payload: dict[str, object] = {}
    async with stdio_client(params) as (read_stream, write_stream):
        during = _child_pids()
        async with ClientSession(read_stream, write_stream) as session:
            original_send_notification = session.send_notification

            async def record_send_notification(*args: object, **kwargs: object) -> object:
                notification = args[0] if args else kwargs.get("notification")
                method = getattr(notification, "method", None)
                root = getattr(notification, "root", None)
                if method is None:
                    method = getattr(root, "method", None)
                if method is None and hasattr(notification, "model_dump"):
                    method = notification.model_dump(mode="json", by_alias=True).get("method")
                notification_methods.append(str(method or type(notification).__name__))
                return await original_send_notification(*args, **kwargs)

            session.send_notification = record_send_notification
            initialized = await session.initialize()
            listed = await session.list_tools()
            definitions = sorted(
                [tool.model_dump(mode="json", by_alias=True, exclude_none=False) for tool in listed.tools],
                key=lambda item: item["name"],
            )
            valid: dict[str, object] = {}
            invalid: dict[str, object] = {}
            for definition in definitions:
                name = definition["name"]
                valid[name] = _result_payload(await session.call_tool(name, _tool_arguments(name)))
                try:
                    invalid[name] = _result_payload(
                        await session.call_tool(name, _invalid_arguments(definition))
                    )
                except Exception as exc:
                    invalid[name] = {"raised": type(exc).__name__, "message": str(exc)}

            pending = asyncio.create_task(
                session.call_tool("web_listening_get_job", {"job_id": 99999999})
            )
            await _wait_for_handler_start(marker_path)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                local_task_cancelled = True
            await asyncio.sleep(0.05)
            handler_events_before_eof = _read_handler_events(marker_path)
            followup_payload = _result_payload(
                await session.call_tool("web_listening_get_job", {"job_id": 2147483647})
            )

    after = _child_pids()
    handler_events_after_eof = _read_handler_events(marker_path)
    cancellation = {
        "probe_schema": MCP_CANCELLATION_SCHEMA,
        "request": {"tool": "web_listening_get_job", "job_id": 99999999},
        "local_client_task_cancelled": local_task_cancelled,
        "client_outgoing_notification_methods": notification_methods,
        "cancellation_notification_observed": "notifications/cancelled" in notification_methods,
        "original_request": {
            "handler_started_before_cancel": "started" in handler_events_before_eof,
            "handler_state_before_transport_eof": _handler_state(handler_events_before_eof),
            "handler_cancelled_before_transport_eof": "cancelled" in handler_events_before_eof,
            "handler_finished_before_transport_eof": "finished" in handler_events_before_eof,
            "handler_state_after_transport_eof": _handler_state(handler_events_after_eof),
            "handler_cancelled_after_transport_eof": "cancelled" in handler_events_after_eof,
            "handler_finished_after_transport_eof": "finished" in handler_events_after_eof,
            "events_before_transport_eof": handler_events_before_eof,
            "events_after_transport_eof": handler_events_after_eof,
        },
        "followup_request": {
            "job_id": 2147483647,
            "response_received_before_transport_eof": True,
            "is_error": followup_payload.get("isError"),
            "structured_content_received": followup_payload.get("structuredContent") is not None,
        },
        "child_reaped_after_transport_eof": before == after and len(during) > len(before),
    }
    if not _valid_cancellation_evidence(cancellation):
        raise ValueError("stdio cancellation probe did not capture the original request lifecycle")
    eof = _eof_probe({**env, "WL_DATA_DIR": str(data_dir), "NO_COLOR": "1"})
    child_import_provenance = json.loads(
        (data_dir / "child-import-provenance.json").read_text(encoding="utf-8")
    )
    invalid["web_listening_list_acquisition_tools"] = {
        "protocol_error": eof["invalid_no_argument_tool_call"].get("error"),
        "raw_response": eof["invalid_no_argument_tool_call"],
    }
    return {
        "protocol_version": initialized.protocolVersion,
        "server_info": initialized.serverInfo.model_dump(mode="json", by_alias=True, exclude_none=False),
        "child_import_provenance": child_import_provenance,
        "tools": definitions,
        "valid_calls": valid,
        "invalid_calls": invalid,
        "cancellation": cancellation,
        "stdio_client_cleanup": {"children_before": len(before), "children_during": len(during),
                                  "children_after": len(after),
                                  "all_children_reaped": before == after and len(during) > len(before)},
        "target_read_policy": "injected fixed offline services; missing local scope files only; no public target request",
        "eof_probe": eof,
    }


def _read_json_line(process: subprocess.Popen[str], timeout: float = 10) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("stdio child has no stdout")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        if not selector.select(timeout):
            raise TimeoutError("stdio child did not respond before timeout")
        line = process.stdout.readline()
    finally:
        selector.close()
    payload = json.loads(line)
    if not isinstance(payload, dict):
        raise ValueError("stdio child returned a non-object JSON-RPC message")
    return payload


def _eof_probe(env: dict[str, str]) -> dict[str, object]:
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD_BOOTSTRAP], cwd="/tmp/issue84-cli-cwd", env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(canonical_json({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "issue84-qualification", "version": "1"}},
        }) + "\n")
        process.stdin.flush()
        initialized = _read_json_line(process)
        protocol_version = initialized.get("result", {}).get("protocolVersion")
        process.stdin.write(canonical_json({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.write(canonical_json({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }) + "\n")
        process.stdin.flush()
        listed = _read_json_line(process)
        tool_names = sorted(item["name"] for item in listed.get("result", {}).get("tools", []))
        process.stdin.write(canonical_json({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "web_listening_list_acquisition_tools", "arguments": []},
        }) + "\n")
        process.stdin.flush()
        invalid_no_argument_call = _read_json_line(process)
        process.stdin.close()
        exit_code = process.wait(timeout=10)
        stderr = process.stderr.read() if process.stderr is not None else ""
        return {"protocol_version": protocol_version, "tool_names": tool_names,
                "eof_exit_code": exit_code, "exited_on_eof": exit_code == 0,
                "invalid_no_argument_tool_call": invalid_no_argument_call,
                "stderr": stderr}
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _verify_wheel(wheel: Path) -> dict[str, str]:
    import web_listening

    distribution = importlib.metadata.distribution("web-listening")
    metadata_version = distribution.version
    openapi_version = _api_snapshot()["openapi"]["info"]["version"]
    if web_listening.__version__ != metadata_version or openapi_version != metadata_version:
        raise ValueError("installed distribution, imported module, and OpenAPI versions differ")
    if metadata_version != PROJECT_VERSION:
        raise ValueError(f"expected project wheel {PROJECT_VERSION}, got {metadata_version}")
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise ValueError("installed wheel has no direct_url.json provenance")
    direct_url = json.loads(direct_url_text)
    archive_info = direct_url.get("archive_info", {})
    archive_hash = (archive_info.get("hashes") or {}).get("sha256")
    if archive_hash is None and isinstance(archive_info.get("hash"), str):
        algorithm, separator, value = archive_info["hash"].partition("=")
        if separator and algorithm == "sha256":
            archive_hash = value
    supplied = sha256(wheel)
    if archive_hash != supplied:
        raise ValueError("installed PEP 610 wheel SHA-256 does not match supplied wheel")
    module_path = Path(web_listening.__file__).resolve()
    if "site-packages" not in module_path.parts:
        raise ValueError(f"project import did not come from site-packages: {module_path}")
    return {"sha256": supplied, "version": metadata_version, "import_path": str(module_path),
            "direct_url": direct_url.get("url", ""), "installed_archive_sha256": archive_hash}


def _verify_lock_install(lock_path: Path) -> dict[str, object]:
    locked = _lock_packages(lock_path)
    actual = {item["name"]: item["version"] for item in _packages()
              if item["name"] not in {"pip", "setuptools", "wheel", "web-listening"}}
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True,
                           text=True, timeout=30, check=False)
    if actual != locked:
        raise ValueError(f"installed set differs from lock: missing={sorted(locked.keys() - actual.keys())}, "
                         f"extra={sorted(actual.keys() - locked.keys())}, "
                         f"version_mismatch={sorted(k for k in locked.keys() & actual.keys() if locked[k] != actual[k])}")
    if check.returncode != 0:
        raise ValueError(f"pip check failed: {check.stdout}{check.stderr}")
    return {"lock_sha256": sha256(lock_path), "locked_package_count": len(locked),
            "installed_set_matches_lock": True, "pip_check_returncode": check.returncode,
            "pip_check_output": check.stdout + check.stderr}


def capture(matrix: str, combination: str, wheel: Path, lock: Path) -> dict[str, object]:
    if matrix not in MCP_VERSIONS or combination not in COMBINATIONS:
        raise ValueError("unknown matrix or install combination")
    if platform.python_version() != PYTHON_VERSION:
        raise ValueError(f"expected Python {PYTHON_VERSION}, got {platform.python_version()}")
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError(f"expected Linux/amd64, got {platform.system()}/{platform.machine()}")
    if importlib.metadata.version("fastapi") != FASTAPI_VERSION:
        raise ValueError(f"expected FastAPI {FASTAPI_VERSION}")
    wanted_mcp = MCP_VERSIONS[matrix] if combination != "base" else None
    try:
        actual_mcp = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        actual_mcp = None
    if actual_mcp != wanted_mcp:
        raise ValueError(f"expected MCP {wanted_mcp!r}, got {actual_mcp!r}")
    packages = _packages()
    lock_check = _verify_lock_install(lock)
    constraints = _check_extra_constraints()
    wheel_info = _verify_wheel(wheel)
    api = _api_snapshot()
    cli = _cli_snapshot()
    if any(command["returncode"] != 0 for command in (cli["help"], cli["catalog_first"], cli["catalog_second"])):
        raise ValueError("a CLI qualification command did not exit successfully")
    if not cli["catalog_idempotent"]:
        raise ValueError("CLI JSON catalog output changed across identical reruns")
    mcp: dict[str, object] | None = None
    if wanted_mcp:
        data_dir = Path("/tmp/issue84-mcp-data")
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "sample.txt").write_text("offline artifact fixture\n", encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix=f"issue84-mcp-cancel-{matrix}-{combination}-") as directory:
            marker_path = Path(directory) / "cancelled-request-lifecycle.jsonl"
            mcp = asyncio.run(_stdio_probe(data_dir, marker_path, wheel_info))
            if [tool["name"] for tool in mcp["tools"]] != EXPECTED_TOOL_NAMES:
                raise ValueError("stdio tool inventory does not match the 11 registered wrappers")
            if not mcp["stdio_client_cleanup"]["all_children_reaped"]:
                raise ValueError("stdio client left a child process after transport cleanup")
            if not _valid_cancellation_evidence(mcp["cancellation"]):
                raise ValueError("stdio cancellation probe did not capture the original request lifecycle")
            eof = mcp["eof_probe"]
            if eof["eof_exit_code"] != 0 or eof["tool_names"] != EXPECTED_TOOL_NAMES:
                raise ValueError("raw stdio EOF probe did not list tools and exit cleanly")
            if not eof["invalid_no_argument_tool_call"].get("error"):
                raise ValueError("raw stdio did not reject a non-object arguments value")
    return {
        "schema_version": QUALIFICATION_VERSION,
        "matrix": matrix,
        "combination": combination,
        "build": BUILD_MANIFEST,
        "environment": {
            "python": platform.python_version(), "platform": f"{platform.system()}/{platform.machine().lower()}",
            "fastapi": FASTAPI_VERSION, "mcp": actual_mcp,
            "starlette": importlib.metadata.version("starlette"),
            "pydantic": importlib.metadata.version("pydantic"),
            "anyio": importlib.metadata.version("anyio"),
            "httpx": importlib.metadata.version("httpx"), "packages": packages,
        },
        "project_wheel": wheel_info,
        "constraints": constraints,
        "installation": lock_check,
        "api": api,
        "cli": cli,
        "mcp_stdio": mcp,
    }


def _project_wheel_identity(snapshot: dict[str, Any]) -> tuple[str, str, str] | None:
    wheel = snapshot.get("project_wheel")
    if not isinstance(wheel, dict):
        return None
    version = wheel.get("version")
    selected_hash = wheel.get("sha256")
    installed_hash = wheel.get("installed_archive_sha256")
    if (
        version != PROJECT_VERSION
        or not isinstance(selected_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", selected_hash)
        or not isinstance(installed_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", installed_hash)
        or selected_hash != installed_hash
    ):
        return None
    return version, selected_hash, installed_hash


def _child_import_provenance_matches(snapshot: dict[str, Any]) -> bool:
    mcp = snapshot.get("mcp_stdio")
    if not isinstance(mcp, dict):
        return False
    provenance = mcp.get("child_import_provenance")
    if not isinstance(provenance, dict):
        return False
    module_paths = provenance.get("module_paths")
    site_packages = provenance.get("site_packages_root")
    wheel_identity = _project_wheel_identity(snapshot)
    if not isinstance(module_paths, dict) or not module_paths or not site_packages or wheel_identity is None:
        return False
    try:
        root = Path(site_packages).resolve()
        paths_match = all(Path(path).resolve().is_relative_to(root) for path in module_paths.values())
    except (OSError, TypeError, ValueError):
        return False
    return (
        provenance.get("distribution") == "web-listening"
        and provenance.get("version") == wheel_identity[0]
        and provenance.get("wheel_archive_sha256") == wheel_identity[1]
        and root.name == "site-packages"
        and paths_match
    )


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, object]:
    if baseline.get("matrix") != "baseline" or candidate.get("matrix") != "candidate":
        raise ValueError("comparison requires baseline and candidate snapshots")
    if baseline.get("combination") != candidate.get("combination"):
        raise ValueError("comparison requires matching install combinations")
    combo = baseline["combination"]
    baseline_wheel = _project_wheel_identity(baseline)
    candidate_wheel = _project_wheel_identity(candidate)
    baseline_packages = {item["name"]: item["version"] for item in baseline["environment"]["packages"]}
    candidate_packages = {item["name"]: item["version"] for item in candidate["environment"]["packages"]}
    package_changes = [
        {"name": name, "baseline": baseline_packages.get(name), "candidate": candidate_packages.get(name)}
        for name in sorted(baseline_packages.keys() | candidate_packages.keys())
        if baseline_packages.get(name) != candidate_packages.get(name)
    ]
    mcp_checks: dict[str, bool] = {}
    if combo != "base":
        baseline_mcp = baseline["mcp_stdio"]
        candidate_mcp = candidate["mcp_stdio"]
        baseline_server_info = dict(baseline_mcp["server_info"])
        candidate_server_info = dict(candidate_mcp["server_info"])
        baseline_server_info.pop("version", None)
        candidate_server_info.pop("version", None)
        mcp_checks = {
            "same_tool_inventory_and_schemas": baseline_mcp["tools"] == candidate_mcp["tools"],
            "same_protocol_handshake": baseline_mcp["protocol_version"] == candidate_mcp["protocol_version"]
            and baseline_server_info == candidate_server_info,
            "server_info_versions_match_installed_mcp": baseline_mcp["server_info"].get("version")
            == baseline["environment"]["mcp"]
            and candidate_mcp["server_info"].get("version") == candidate["environment"]["mcp"],
            "same_valid_result_envelopes": baseline_mcp["valid_calls"] == candidate_mcp["valid_calls"],
            "same_invalid_error_envelopes": baseline_mcp["invalid_calls"] == candidate_mcp["invalid_calls"],
            "valid_original_request_cancellation_evidence": _valid_cancellation_evidence(baseline_mcp["cancellation"])
            and _valid_cancellation_evidence(candidate_mcp["cancellation"]),
            "same_cancellation_lifecycle": baseline_mcp["cancellation"] == candidate_mcp["cancellation"],
            "same_stdio_cleanup": baseline_mcp["stdio_client_cleanup"] == candidate_mcp["stdio_client_cleanup"],
            "both_stdio_children_loaded_project_from_installed_wheel": _child_import_provenance_matches(baseline)
            and _child_import_provenance_matches(candidate),
            "same_eof_lifecycle": {
                key: baseline_mcp["eof_probe"].get(key)
                for key in ("protocol_version", "tool_names", "eof_exit_code", "exited_on_eof")
            } == {
                key: candidate_mcp["eof_probe"].get(key)
                for key in ("protocol_version", "tool_names", "eof_exit_code", "exited_on_eof")
            },
        }
    checks = {
        "same_build_manifest": baseline["build"] == candidate["build"],
        "same_project_wheel": baseline_wheel is not None and baseline_wheel == candidate_wheel,
        "both_installed_wheel_hashes_verified": baseline_wheel is not None and candidate_wheel is not None,
        "same_full_openapi": baseline["api"]["openapi"] == candidate["api"]["openapi"],
        "same_api_routes_and_responses": baseline["api"]["routes"] == candidate["api"]["routes"]
        and baseline["api"]["responses"] == candidate["api"]["responses"],
        "same_testclient_lifespan": baseline["api"]["testclient_lifespan"] == candidate["api"]["testclient_lifespan"],
        "same_cli_help": baseline["cli"]["help"] == candidate["cli"]["help"],
        "same_cli_json_and_idempotence": baseline["cli"]["catalog_first"] == candidate["cli"]["catalog_first"]
        and baseline["cli"]["catalog_idempotent"] and candidate["cli"]["catalog_idempotent"],
        "both_locks_match_and_pip_check": all(
            item["installation"]["installed_set_matches_lock"]
            and item["installation"]["pip_check_returncode"] == 0 for item in (baseline, candidate)
        ),
        "mcp_contract_checks": all(mcp_checks.values()) if mcp_checks else True,
    }
    return {"schema_version": QUALIFICATION_VERSION, "combination": combo, "checks": checks,
            "mcp_checks": mcp_checks, "package_changes": package_changes,
            "compatible": all(checks.values())}


def verify_rebuild(seed: dict[str, Any], rebuild: dict[str, Any]) -> dict[str, object]:
    if seed.get("matrix") != rebuild.get("matrix") or seed.get("combination") != rebuild.get("combination"):
        raise ValueError("rebuild comparison requires the same matrix and combination")
    seed_packages = {item["name"]: item["version"] for item in seed["environment"]["packages"]}
    rebuilt_packages = {item["name"]: item["version"] for item in rebuild["environment"]["packages"]}
    seed_wheel = _project_wheel_identity(seed)
    rebuild_wheel = _project_wheel_identity(rebuild)
    same_project_wheel = seed_wheel is not None and seed_wheel == rebuild_wheel
    return {"schema_version": QUALIFICATION_VERSION, "matrix": seed["matrix"],
            "combination": seed["combination"], "same_resolved_package_set": seed_packages == rebuilt_packages,
            "seed_project_wheel_valid": seed_wheel is not None,
            "rebuild_project_wheel_valid": rebuild_wheel is not None,
            "same_project_wheel": same_project_wheel,
            "seed_lock_sha256": seed["installation"]["lock_sha256"],
            "rebuild_lock_sha256": rebuild["installation"]["lock_sha256"],
            "both_pip_check_passed": seed["installation"]["pip_check_returncode"] == 0
            and rebuild["installation"]["pip_check_returncode"] == 0,
            "compatible": seed_packages == rebuilt_packages
            and same_project_wheel
            and seed["installation"]["lock_sha256"] == rebuild["installation"]["lock_sha256"]
            and seed["installation"]["pip_check_returncode"] == rebuild["installation"]["pip_check_returncode"] == 0}


def _write_json(path: Path, value: object) -> str:
    output = canonical_json(value) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(output, encoding="utf-8")
    print(output, end="")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture_parser = subparsers.add_parser("capture", help="capture an installed matrix and stdio contract")
    capture_parser.add_argument("--matrix", choices=sorted(MCP_VERSIONS), required=True)
    capture_parser.add_argument("--combination", choices=sorted(COMBINATIONS), required=True)
    capture_parser.add_argument("--wheel", type=Path, required=True)
    capture_parser.add_argument("--lock", type=Path, required=True)
    capture_parser.add_argument("--write", type=Path, required=True)
    compare_parser = subparsers.add_parser("compare", help="compare baseline and candidate snapshots")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.add_argument("--write", type=Path, required=True)
    rebuild_parser = subparsers.add_parser("compare-rebuild", help="compare seed and independent rebuild package sets")
    rebuild_parser.add_argument("--seed", type=Path, required=True)
    rebuild_parser.add_argument("--rebuild", type=Path, required=True)
    rebuild_parser.add_argument("--write", type=Path, required=True)
    lock_parser = subparsers.add_parser("lock-from-report", help="render all selected report wheel hashes")
    lock_parser.add_argument("--report", type=Path, required=True)
    lock_parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        result = capture(args.matrix, args.combination, args.wheel, args.lock)
        _write_json(args.write, result)
        return 0
    elif args.command == "compare":
        result = compare(json.loads(args.baseline.read_text(encoding="utf-8")),
                         json.loads(args.candidate.read_text(encoding="utf-8")))
    elif args.command == "compare-rebuild":
        result = verify_rebuild(json.loads(args.seed.read_text(encoding="utf-8")),
                                json.loads(args.rebuild.read_text(encoding="utf-8")))
    else:
        lock_text = render_hash_lock(json.loads(args.report.read_text(encoding="utf-8")))
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(lock_text, encoding="utf-8")
        print(lock_text, end="")
        return 0
    _write_json(args.write, result)
    return 0 if result["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
