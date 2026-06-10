from __future__ import annotations

from web_listening.blocks.acquisition_profile import CaptureAttempt
from web_listening.contracts.tool_result import (
    TOOL_RESULT_CONTRACT_VERSION,
    ToolResult,
    ToolResultQualityGates,
    tool_result_from_capture_attempt,
)


def test_tool_result_defaults_include_contract_version_and_quality_gates():
    result = ToolResult(
        ok=True,
        has_data=False,
        data_status="not_applicable",
        quality_gates=ToolResultQualityGates(
            requested={"min_words": 120},
            effective={"min_words": 80},
        ),
        stop_reason="control_operation_completed",
    )

    payload = result.model_dump(mode="json")

    assert payload["meta"]["contract_version"] == TOOL_RESULT_CONTRACT_VERSION
    assert payload["quality_gates"] == {
        "requested": {"min_words": 120},
        "effective": {"min_words": 80},
    }
    assert payload["attempts"] == []
    assert payload["data"] == {}
    assert payload["error"] is None


def test_capture_attempt_passed_maps_to_present_tool_result():
    attempt = CaptureAttempt(
        adapter="web_http",
        status="passed",
        url="https://example.com",
        final_url="https://example.com/",
        status_code=200,
        word_count=420,
        link_count=8,
        document_link_count=2,
        recommended_next_adapter="",
        metadata={"source": "test"},
    )

    result = tool_result_from_capture_attempt(
        attempt,
        requested_quality_gates={"min_words": 120, "min_links": 3},
        effective_quality_gates={"min_words": 120, "min_links": 3},
        data={"url": attempt.final_url},
    )

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "present"
    assert result.data_count == 1
    assert result.tool == "web_http"
    assert result.stop_reason == "usable_data_found"
    assert result.next_tool is None
    assert result.next_action is None
    assert result.data == {"url": "https://example.com/"}
    assert result.data_quality.passed is True
    assert result.data_quality.status_code == 200
    assert result.data_quality.word_count == 420
    assert result.data_quality.link_count == 8
    assert result.data_quality.document_link_count == 2
    assert result.data_quality.failure_reasons == []
    assert result.quality_gates.requested == {"min_words": 120, "min_links": 3}
    assert result.quality_gates.effective == {"min_words": 120, "min_links": 3}
    assert result.attempts == [attempt.model_dump(mode="json")]


def test_capture_attempt_failed_quality_gate_maps_next_adapter():
    attempt = CaptureAttempt(
        adapter="web_http",
        status="failed_quality_gate",
        url="https://example.com",
        status_code=200,
        word_count=5,
        link_count=0,
        failure_reason="word_count 5 below min_words 120",
        recommended_next_adapter="browser_rendered",
    )

    result = tool_result_from_capture_attempt(attempt)

    assert result.ok is True
    assert result.has_data is False
    assert result.data_status == "failed_quality_gate"
    assert result.data_count == 0
    assert result.stop_reason == "insufficient_data"
    assert result.next_tool == "browser_rendered"
    assert result.next_action == "try_adapter:browser_rendered"
    assert result.data_quality.passed is False
    assert result.data_quality.failure_reasons == ["word_count 5 below min_words 120"]
    assert result.error is None


def test_capture_attempt_blocked_maps_to_blocked_without_operational_error():
    attempt = CaptureAttempt(
        adapter="browser_rendered",
        status="blocked",
        url="https://example.com",
        status_code=403,
        failure_reason="blocked marker detected: cloudflare",
        recommended_next_adapter="",
    )

    result = tool_result_from_capture_attempt(attempt)

    assert result.ok is True
    assert result.has_data is False
    assert result.data_status == "blocked"
    assert result.stop_reason == "blocked"
    assert result.data_quality.blocked is True
    assert result.error is None


def test_capture_attempt_error_maps_to_structured_error():
    attempt = CaptureAttempt(
        adapter="web_http",
        status="error",
        url="https://example.com",
        failure_reason="ReadTimeout: timed out",
        recommended_next_adapter="browser_rendered",
    )

    result = tool_result_from_capture_attempt(attempt)

    assert result.ok is False
    assert result.has_data is False
    assert result.data_status == "error"
    assert result.stop_reason == "error"
    assert result.next_tool == "browser_rendered"
    assert result.error is not None
    assert result.error.model_dump(mode="json") == {
        "code": "capture_error",
        "message": "ReadTimeout: timed out",
        "retryable": False,
        "safe_to_escalate": False,
        "exception_type": "",
    }
