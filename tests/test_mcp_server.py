from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from web_listening.blocks.acquisition_profile import CaptureAttempt, build_default_acquisition_profile
from web_listening.contracts.tool_result import ToolResult
from web_listening.mcp import tools
from web_listening.mcp.server import create_server
from web_listening.models import Job


def test_list_acquisition_tools_returns_tool_result_envelope():
    payload = tools.web_listening_list_acquisition_tools()

    result = ToolResult(**payload)
    assert result.ok is True
    assert result.tool == "web_listening_list_acquisition_tools"
    assert result.data_status == "present"
    assert result.data_count and result.data_count >= 3
    assert "tools" in result.data


def test_probe_tool_once_maps_capture_attempt_to_tool_result(monkeypatch):
    def fake_probe_acquisition_url(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["url"] == "https://example.com/report"
        assert kwargs["adapter_id"] == "web_http"
        return {
            "contract_version": "acquisition-probe.v1",
            "profile": build_default_acquisition_profile(
                site_key="example",
                allowed_domains=["example.com"],
            ).model_dump(mode="json"),
            "attempt": CaptureAttempt(
                adapter="web_http",
                status="passed",
                url="https://example.com/report",
                final_url="https://example.com/report",
                status_code=200,
                word_count=250,
                link_count=5,
                document_link_count=1,
            ).model_dump(mode="json"),
            "available_tools": {},
            "next_action": "use_adapter_output",
        }

    monkeypatch.setattr(tools, "probe_acquisition_url", fake_probe_acquisition_url)

    result = ToolResult(
        **tools.web_listening_probe_tool_once(
            "https://example.com/report",
            site_key="example",
            adapter="web_http",
            quality_gates={"min_words": 120},
            safety={"allowed_domains": ["example.com"]},
        )
    )

    assert result.ok is True
    assert result.has_data is True
    assert result.tool == "web_http"
    assert result.data_status == "present"
    assert result.data["final_url"] == "https://example.com/report"
    assert result.meta["contract_version"] == "web-listening-tool-result.v1"


def test_probe_tool_once_does_not_echo_invalid_adapter_secret():
    result = ToolResult(
        **tools.web_listening_probe_tool_once(
            "https://example.com/report",
            site_key="example",
            adapter="Bearer SECRET123",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_acquisition_request"
    assert "SECRET123" not in result.error.message


def test_recommend_next_tool_returns_next_adapter():
    profile = build_default_acquisition_profile(site_key="example", allowed_domains=["example.com"])
    attempts = [
        CaptureAttempt(
            adapter="web_http",
            status="failed_quality_gate",
            url="https://example.com",
            failure_reason="too few words",
        ).model_dump(mode="json")
    ]

    result = ToolResult(**tools.web_listening_recommend_next_tool(profile=profile.model_dump(mode="json"), attempts=attempts))

    assert result.ok is True
    assert result.has_data is False
    assert result.next_tool == "browser_rendered"
    assert result.next_action == "try_adapter:browser_rendered"


def test_recommend_next_tool_accepts_planned_minimal_mcp_shape():
    result = ToolResult(
        **tools.web_listening_recommend_next_tool(
            strategy="public_web_default",
            attempts=[
                {
                    "tool": "web_http",
                    "data_status": "failed_quality_gate",
                    "data_quality": {"word_count": 10, "link_count": 1},
                }
            ],
            safety={"allow_stealth_browser": False, "require_authorized_access": False},
        )
    )

    assert result.ok is True
    assert result.data_status == "not_applicable"
    assert result.has_data is False
    assert result.data["next_tool"] == "browser_rendered"


def test_recommend_next_tool_redacts_profile_adapter_config():
    profile = build_default_acquisition_profile(site_key="example", allowed_domains=["example.com"])
    profile_payload = profile.model_dump(mode="json")
    profile_payload["adapters"][0]["config"] = {"Authorization": "Bearer SECRET123"}
    profile_payload["adapters"][0]["safety"] = {"cookie": "SECRET456"}

    result = ToolResult(**tools.web_listening_recommend_next_tool(profile=profile_payload, attempts=[]))

    rendered = str(result.data)
    assert "SECRET123" not in rendered
    assert "SECRET456" not in rendered
    assert "config" not in result.data["profile"]["adapters"][0]


def test_acquire_with_fallback_tool_serializes_core_result(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_acquire_with_fallback_result(url: str, **kwargs: Any) -> ToolResult:
        captured["url"] = url
        captured.update(kwargs)
        return ToolResult(
            ok=True,
            has_data=True,
            data_status="present",
            data_count=1,
            tool="web_http",
            data={"content_text_preview": "hello"},
            stop_reason="usable_data_found",
        )

    monkeypatch.setattr(tools, "acquire_with_fallback_result", fake_acquire_with_fallback_result)

    result = ToolResult(
        **tools.web_listening_acquire_with_fallback(
            "https://example.com",
            site_key="example",
            goal="find public reports",
            goal_preset="document_discovery",
            strategy="public_web_default",
            quality_gates={"min_words": 10},
            safety={"allowed_domains": ["example.com"]},
            max_attempts=2,
        )
    )

    assert result.ok is True
    assert result.tool == "web_http"
    assert captured["url"] == "https://example.com"
    assert captured["strategy"] == "public_web_default"
    assert captured["goal_preset"] == "document_discovery"
    assert captured["quality_gates"] == {"min_words": 10}
    assert captured["allowed_domains"] == ["example.com"]
    assert captured["max_attempts"] == 2
    assert result.meta["goal"] == "find public reports"


def test_acquire_with_fallback_tool_rejects_invalid_goal_preset():
    result = ToolResult(
        **tools.web_listening_acquire_with_fallback(
            "https://example.com",
            site_key="example",
            goal_preset="not_a_preset",
            max_attempts=0,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_acquisition_request"
    assert "goal_preset must be one of" in result.error.message


def test_acquire_with_fallback_tool_rejects_non_string_goal_preset():
    result = ToolResult(
        **tools.web_listening_acquire_with_fallback(
            "https://example.com",
            site_key="example",
            goal_preset=["document_discovery"],  # type: ignore[arg-type]
            max_attempts=0,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_acquisition_request"
    assert result.error.message == "goal_preset must be a string"


def test_acquire_with_fallback_tool_defaults_allowed_domains_to_input_host(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_acquire_with_fallback_result(url: str, **kwargs: Any) -> ToolResult:
        captured.update(kwargs)
        return ToolResult(
            ok=True,
            has_data=False,
            data_status="not_applicable",
            data_count=0,
            tool="",
            stop_reason="no_available_adapter",
        )

    monkeypatch.setattr(tools, "acquire_with_fallback_result", fake_acquire_with_fallback_result)

    tools.web_listening_acquire_with_fallback("https://example.com/page", site_key="example", max_attempts=0)

    assert captured["allowed_domains"] == ["example.com"]


def test_acquire_with_fallback_tool_uses_normalized_url(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_acquire_with_fallback_result(url: str, **kwargs: Any) -> ToolResult:
        captured["url"] = url
        captured.update(kwargs)
        return ToolResult(
            ok=True,
            has_data=False,
            data_status="not_applicable",
            data_count=0,
            tool="",
            stop_reason="no_available_adapter",
        )

    monkeypatch.setattr(tools, "acquire_with_fallback_result", fake_acquire_with_fallback_result)

    tools.web_listening_acquire_with_fallback("  https://example.com/page  ", site_key="example", max_attempts=0)

    assert captured["url"] == "https://example.com/page"
    assert captured["allowed_domains"] == ["example.com"]


@pytest.mark.parametrize(
    "safety",
    [
        {"allowed_domains": []},
        {"allow_stealth_browser": False},
        {"require_authorized_access": False},
    ],
)
def test_acquire_with_fallback_tool_rejects_profile_path_safety_key_presence(
    tmp_path: Path,
    safety: dict[str, Any],
):
    profile = build_default_acquisition_profile(site_key="example", allowed_domains=["example.org"])
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile.model_dump(mode="json")), encoding="utf-8")

    result = ToolResult(
        **tools.web_listening_acquire_with_fallback(
            "https://example.net/page",
            profile_path=str(profile_path),
            safety=safety,
            max_attempts=0,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_acquisition_request"


def test_acquire_with_fallback_tool_rejects_profile_path_safety_override(tmp_path: Path):
    profile = build_default_acquisition_profile(site_key="example", allowed_domains=["example.org"])
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile.model_dump(mode="json")), encoding="utf-8")

    result = ToolResult(
        **tools.web_listening_acquire_with_fallback(
            "https://example.net/page",
            profile_path=str(profile_path),
            safety={"allowed_domains": ["example.net"]},
            max_attempts=0,
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_acquisition_request"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://user:pass@example.com/secret",
    ],
)
def test_acquire_with_fallback_tool_rejects_unsafe_urls(url: str):
    result = ToolResult(**tools.web_listening_acquire_with_fallback(url, site_key="example", max_attempts=0))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_acquisition_request"


def test_acquire_with_fallback_tool_does_not_echo_invalid_profile_secret():
    result = ToolResult(
        **tools.web_listening_acquire_with_fallback(
            "https://example.com",
            profile={"schema_version": "acquisition-profile.v1", "api_key": "SECRET123"},
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert "SECRET123" not in result.error.message


def test_acquire_with_fallback_tool_returns_error_envelope_on_invalid_profile():
    payload = tools.web_listening_acquire_with_fallback(
        "https://example.com",
        profile={"schema_version": "acquisition-profile.v1"},
    )

    result = ToolResult(**payload)
    assert result.ok is False
    assert result.data_status == "error"
    assert result.error is not None
    assert result.error.code == "fallback_acquisition_failed"


def test_report_scope_invalid_output_format_is_observable():
    result = ToolResult(**tools.web_listening_report_scope("scope.yaml", output_format="html"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "invalid_workflow_request"
    assert result.error.message == "output_format must be one of: md, yaml"


def test_workflow_job_helper_maps_completed_artifacts_to_artifact_only():
    job = Job(
        job_id=42,
        job_type="scope.report",
        status="completed",
        scope_id=7,
        run_id=9,
        produced_artifacts={"output_path": "/tmp/report.md"},
        accepted_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        finished_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
    )

    result = ToolResult(**tools._tool_result_from_job(job, tool="web_listening_report_scope"))

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "artifact_only"
    assert result.data_count == 1
    assert result.artifacts["produced"] == {"output_path": "/tmp/report.md"}
    assert result.next_action == "read_job_artifacts"
    assert result.stop_reason == "job_completed_with_artifacts"


def test_workflow_job_helper_maps_running_and_failed_jobs():
    running = Job(job_id=1, job_type="scope.run", status="running")
    failed = Job(
        job_id=2,
        job_type="scope.run",
        status="failed",
        error="boom",
        error_code="run_failed",
        is_retryable=True,
    )

    running_result = ToolResult(**tools._tool_result_from_job(running, tool="web_listening_get_job"))
    failed_result = ToolResult(**tools._tool_result_from_job(failed, tool="web_listening_get_job"))

    assert running_result.ok is True
    assert running_result.has_data is False
    assert running_result.data_status == "running"
    assert running_result.next_action == "poll_job_status"
    assert failed_result.ok is False
    assert failed_result.data_status == "error"
    assert failed_result.error is not None
    assert failed_result.error.code == "run_failed"
    assert failed_result.error.retryable is True


def test_workflow_job_helper_redacts_failed_job_error_message():
    failed = Job(
        job_id=3,
        job_type="scope.run",
        status="failed",
        error="failed reading /tmp/SECRET123/report.md",
        error_code="run_failed",
    )

    result = ToolResult(**tools._tool_result_from_job(failed, tool="web_listening_get_job"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "run_failed"
    assert result.error.message == "job failed"
    assert "SECRET123" not in result.error.message
    assert "/tmp" not in result.error.message


def test_get_job_returns_persisted_job_envelope(monkeypatch):
    job = Job(job_id=5, job_type="scope.manifest", status="completed", produced_artifacts={"yaml_path": "/tmp/a.yaml"})
    captured: dict[str, Any] = {}

    class FakeStorage:
        def __init__(self, db_path: Path):
            captured["db_path"] = db_path

        def get_job(self, job_id: int) -> Job | None:
            captured["job_id"] = job_id
            return job

        def close(self) -> None:
            captured["closed"] = True

    import web_listening.blocks.storage as storage_module

    monkeypatch.setattr(storage_module, "Storage", FakeStorage)

    result = ToolResult(**tools.web_listening_get_job(5))

    assert result.ok is True
    assert result.data_status == "artifact_only"
    assert result.data["job"]["job_id"] == 5
    assert captured["job_id"] == 5
    assert captured["closed"] is True


def test_get_job_returns_not_found_error(monkeypatch):
    class FakeStorage:
        def __init__(self, db_path: Path):
            pass

        def get_job(self, job_id: int) -> None:
            return None

        def close(self) -> None:
            pass

    import web_listening.blocks.storage as storage_module

    monkeypatch.setattr(storage_module, "Storage", FakeStorage)

    result = ToolResult(**tools.web_listening_get_job(404))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "job_not_found"


def test_read_artifact_inlines_small_file_under_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tools.settings, "data_dir", tmp_path)
    artifact = tmp_path / "reports" / "small.txt"
    artifact.parent.mkdir()
    artifact.write_text("hello", encoding="utf-8")

    result = ToolResult(**tools.web_listening_read_artifact("reports/small.txt", inline_limit=32))

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "present"
    assert result.data_count == 1
    assert result.data["content"] == "hello"
    assert result.artifacts["size_bytes"] == 5


def test_read_artifact_returns_metadata_for_large_file_under_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tools.settings, "data_dir", tmp_path)
    artifact = tmp_path / "large.txt"
    artifact.write_text("0123456789", encoding="utf-8")

    result = ToolResult(**tools.web_listening_read_artifact(str(artifact), inline_limit=4))

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "artifact_only"
    assert result.data_count == 1
    assert result.data["content"] == ""
    assert result.data["size_bytes"] == 10
    assert result.stop_reason == "artifact_too_large"


def test_read_artifact_returns_metadata_for_small_binary_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tools.settings, "data_dir", tmp_path)
    artifact = tmp_path / "binary.bin"
    artifact.write_bytes(b"\xff\xfe\xfd")

    result = ToolResult(**tools.web_listening_read_artifact("binary.bin", inline_limit=32))

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "artifact_only"
    assert result.data_count == 1
    assert result.data["content"] == ""
    assert result.data["size_bytes"] == 3
    assert result.stop_reason == "artifact_not_text"


@pytest.mark.parametrize("content", [b"\x00\x01ABC", b"\x7fABC", "\u0085ABC".encode("utf-8")])
def test_read_artifact_returns_metadata_for_control_byte_file(
    tmp_path: Path,
    monkeypatch,
    content: bytes,
):
    monkeypatch.setattr(tools.settings, "data_dir", tmp_path)
    artifact = tmp_path / "control.bin"
    artifact.write_bytes(content)

    result = ToolResult(**tools.web_listening_read_artifact("control.bin", inline_limit=32))

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "artifact_only"
    assert result.data["content"] == ""
    assert result.stop_reason == "artifact_not_text"


def test_read_artifact_refuses_path_traversal(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET123", encoding="utf-8")
    monkeypatch.setattr(tools.settings, "data_dir", data_dir)

    result = ToolResult(**tools.web_listening_read_artifact("../secret.txt"))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "artifact_read_failed"
    assert "SECRET123" not in result.error.message


@pytest.mark.asyncio
async def test_mcp_server_registers_expected_tools():
    server = create_server()

    listed = await server.list_tools()
    names = {tool.name for tool in listed}

    assert {
        "web_listening_list_acquisition_tools",
        "web_listening_probe_tool_once",
        "web_listening_recommend_next_tool",
        "web_listening_acquire_with_fallback",
        "web_listening_bootstrap_scope",
        "web_listening_run_scope",
        "web_listening_report_scope",
        "web_listening_export_manifest",
        "web_listening_get_job",
        "web_listening_read_artifact",
    }.issubset(names)
