from __future__ import annotations

from typing import Any

import pytest

import web_listening.blocks.acquisition_fallback as fallback_module
from web_listening.blocks.acquisition_fallback import acquire_with_fallback_result, should_continue
from web_listening.blocks.acquisition_profile import (
    AcquisitionAdapterConfig,
    AcquisitionProfile,
    AcquisitionQualityGates,
    AcquisitionSafetyPolicy,
    CaptureAttempt,
)
from web_listening.blocks.crawler import FetchResult
from web_listening.contracts.tool_result import ToolResult, ToolResultError


def make_fetch_result(
    *,
    text: str = " ".join(f"word{i}" for i in range(160)),
    status_code: int | None = 200,
    final_url: str = "https://example.com/final",
    link_count: int = 5,
    document_link_count: int = 1,
    metadata_json: dict[str, Any] | None = None,
) -> FetchResult:
    metadata = {"link_count": link_count, "document_link_count": document_link_count}
    if metadata_json:
        metadata.update(metadata_json)
    return FetchResult(
        raw_html="<html></html>",
        cleaned_html="<main></main>",
        content_text=text,
        markdown=text,
        fit_markdown=text,
        metadata_json=metadata,
        final_url=final_url,
        status_code=status_code,
    )


def make_profile(
    *,
    fallback_order: list | None = None,
    quality_gates: AcquisitionQualityGates | None = None,
    safety: AcquisitionSafetyPolicy | None = None,
    adapters: list[AcquisitionAdapterConfig] | None = None,
) -> AcquisitionProfile:
    chain = fallback_order or ["browser_rendered", "sitemap", "rss"]
    return AcquisitionProfile(
        profile_id="example-acquisition-profile",
        site_key="example",
        generated_at="2026-06-10T00:00:00Z",
        default_adapter="web_http",
        fallback_order=chain,
        quality_gates=quality_gates or AcquisitionQualityGates(min_words=3, min_links=1, min_document_links=0),
        safety=safety or AcquisitionSafetyPolicy(allowed_domains=["example.com"]),
        adapters=adapters
        or [AcquisitionAdapterConfig(adapter=adapter_id) for adapter_id in ["web_http", *chain]],
    )


class FakeAdapter:
    adapter_id: str

    def __init__(self, adapter_id: str, result: FetchResult | Exception):
        self.adapter_id = adapter_id
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def capture(self, url: str, *, config=None) -> FetchResult:
        self.calls.append({"url": url, "config": config})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_first_adapter_fails_quality_gate_second_passes():
    profile = make_profile(fallback_order=["browser_rendered"])
    http = FakeAdapter("web_http", make_fetch_result(text="too short", link_count=0))
    browser = FakeAdapter("browser_rendered", make_fetch_result(text="one two three four", link_count=1))

    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={"web_http": http, "browser_rendered": browser},
    )

    assert result.ok is True
    assert result.has_data is True
    assert result.data_status == "present"
    assert result.tool == "browser_rendered"
    assert result.stop_reason == "usable_data_found"
    assert result.data["content_text_preview"] == "one two three four"
    assert [attempt["tool"] for attempt in result.attempts] == ["web_http", "browser_rendered"]
    assert result.attempts[0]["data_status"] == "failed_quality_gate"
    assert result.attempts[1]["data_status"] == "present"
    assert http.calls and browser.calls


def test_quality_gate_override_controls_document_link_escalation():
    profile = make_profile(fallback_order=["browser_rendered"])
    http_result = make_fetch_result(
        text="one two three four",
        link_count=1,
        document_link_count=0,
    )
    browser_result = make_fetch_result(
        text="one two three four",
        link_count=1,
        document_link_count=1,
    )

    passes_without_document_requirement = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", http_result),
            "browser_rendered": FakeAdapter("browser_rendered", browser_result),
        },
        quality_gates={"min_words": 3, "min_links": 1, "min_document_links": 0},
    )
    requires_document = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", http_result),
            "browser_rendered": FakeAdapter("browser_rendered", browser_result),
        },
        quality_gates={"min_words": 3, "min_links": 1, "min_document_links": 1},
    )

    assert passes_without_document_requirement.tool == "web_http"
    assert len(passes_without_document_requirement.attempts) == 1
    assert requires_document.tool == "browser_rendered"
    assert len(requires_document.attempts) == 2
    assert requires_document.quality_gates.requested["min_document_links"] == 1
    assert requires_document.quality_gates.effective["min_document_links"] == 1


def test_document_discovery_goal_preset_continues_until_document_links_are_found():
    profile = make_profile(fallback_order=["browser_rendered"])
    http_result = make_fetch_result(
        text=" ".join(f"word{i}" for i in range(130)),
        link_count=2,
        document_link_count=0,
    )
    browser_result = make_fetch_result(
        text=" ".join(f"word{i}" for i in range(130)),
        link_count=2,
        document_link_count=1,
    )

    page_text = acquire_with_fallback_result(
        "https://example.com/reports",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", http_result),
            "browser_rendered": FakeAdapter("browser_rendered", browser_result),
        },
        goal_preset="page_text",
    )
    document_discovery = acquire_with_fallback_result(
        "https://example.com/reports",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", http_result),
            "browser_rendered": FakeAdapter("browser_rendered", browser_result),
        },
        goal_preset="document_discovery",
    )

    assert page_text.tool == "web_http"
    assert len(page_text.attempts) == 1
    assert page_text.quality_gates.effective["min_document_links"] == 0
    assert page_text.meta["goal_preset"] == "page_text"
    assert document_discovery.tool == "browser_rendered"
    assert [attempt["tool"] for attempt in document_discovery.attempts] == ["web_http", "browser_rendered"]
    assert document_discovery.attempts[0]["data_status"] == "failed_quality_gate"
    assert document_discovery.quality_gates.requested == {}
    assert document_discovery.quality_gates.effective["min_document_links"] == 1
    assert document_discovery.meta["goal_preset"] == "document_discovery"


@pytest.mark.parametrize(
    ("goal_preset", "expected_gates"),
    [
        ("page_text", {"min_words": 120, "min_links": 0, "min_document_links": 0}),
        ("section_discovery", {"min_words": 80, "min_links": 3, "min_document_links": 0}),
        ("document_discovery", {"min_words": 80, "min_links": 0, "min_document_links": 1}),
        ("change_monitoring", {"min_words": 20, "min_links": 0, "min_document_links": 0}),
    ],
)
def test_goal_presets_apply_default_quality_gates(goal_preset: str, expected_gates: dict[str, int]):
    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=make_profile(fallback_order=[]),
        adapters={"web_http": FakeAdapter("web_http", make_fetch_result())},
        goal_preset=goal_preset,
    )

    assert result.quality_gates.requested == {}
    for gate, expected in expected_gates.items():
        assert result.quality_gates.effective[gate] == expected
    assert result.meta["goal_preset"] == goal_preset


def test_explicit_quality_gates_win_over_goal_preset_defaults():
    browser = FakeAdapter("browser_rendered", make_fetch_result(document_link_count=1))

    result = acquire_with_fallback_result(
        "https://example.com/reports",
        profile=make_profile(fallback_order=["browser_rendered"]),
        adapters={
            "web_http": FakeAdapter("web_http", make_fetch_result(document_link_count=0)),
            "browser_rendered": browser,
        },
        goal_preset="document_discovery",
        quality_gates={"min_words": 1, "min_links": 0, "min_document_links": 0},
    )

    assert result.tool == "web_http"
    assert len(result.attempts) == 1
    assert browser.calls == []
    assert result.quality_gates.requested["min_document_links"] == 0
    assert result.quality_gates.effective["min_document_links"] == 0
    assert result.meta["goal_preset"] == "document_discovery"


def test_strategy_document_discovery_does_not_imply_goal_preset():
    result = acquire_with_fallback_result(
        "https://example.com/reports",
        profile=make_profile(fallback_order=["browser_rendered"]),
        adapters={
            "web_http": FakeAdapter("web_http", make_fetch_result(document_link_count=0)),
            "browser_rendered": FakeAdapter("browser_rendered", make_fetch_result(document_link_count=1)),
        },
        strategy="document_discovery",
    )

    assert result.tool == "web_http"
    assert len(result.attempts) == 1
    assert result.quality_gates.effective["min_document_links"] == 0
    assert "goal_preset" not in result.meta


def test_non_string_goal_preset_is_rejected_before_membership_check():
    with pytest.raises(ValueError, match="goal_preset must be a string"):
        acquire_with_fallback_result(
            "https://example.com/page",
            profile=make_profile(fallback_order=[]),
            adapters={"web_http": FakeAdapter("web_http", make_fetch_result())},
            goal_preset=["document_discovery"],  # type: ignore[arg-type]
        )


def test_goal_preset_meta_is_preserved_on_unsafe_url_terminal_result():
    result = acquire_with_fallback_result(
        "https://evil.example/page",
        profile=make_profile(),
        adapters={"web_http": FakeAdapter("web_http", make_fetch_result())},
        allowed_domains=["example.com"],
        goal_preset="document_discovery",
    )

    assert result.ok is False
    assert result.stop_reason == "unsafe_url"
    assert result.meta["goal_preset"] == "document_discovery"


def test_goal_preset_meta_is_preserved_on_no_adapter_terminal_result():
    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=make_profile(fallback_order=[]),
        adapters={"web_http": FakeAdapter("web_http", make_fetch_result())},
        max_attempts=0,
        goal_preset="change_monitoring",
    )

    assert result.data_status == "not_applicable"
    assert result.stop_reason == "no_available_adapter"
    assert result.meta["goal_preset"] == "change_monitoring"


def test_goal_preset_meta_is_preserved_on_unsafe_escalation_terminal_result():
    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=make_profile(fallback_order=[]),
        strategy="authorized_fallback",
        adapters={"web_http": FakeAdapter("web_http", make_fetch_result(text="short", link_count=0))},
        goal_preset="document_discovery",
    )

    assert result.ok is False
    assert result.stop_reason == "unsafe_escalation"
    assert result.meta["goal_preset"] == "document_discovery"


def test_reserved_adapters_are_recorded_as_non_terminal_skips():
    profile = make_profile(fallback_order=["sitemap", "browser_rendered", "rss"])
    http = FakeAdapter("web_http", make_fetch_result(text="too short", link_count=0))
    browser = FakeAdapter("browser_rendered", make_fetch_result(text="one two three four", link_count=1))

    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={"web_http": http, "browser_rendered": browser},
    )

    assert result.has_data is True
    assert [attempt["tool"] for attempt in result.attempts] == ["web_http", "sitemap", "browser_rendered"]
    assert result.attempts[1] == {
        "tool": "sitemap",
        "ok": True,
        "has_data": False,
        "data_status": "not_applicable",
        "stop_reason": "adapter_not_applicable",
        "skipped": True,
        "reason": "adapter is reserved / not probe-capable in this build",
    }


def test_unauthorized_cloakbrowser_escalation_returns_permission_denied_terminal_result():
    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=make_profile(),
        strategy="authorized_fallback",
        adapters={"web_http": FakeAdapter("web_http", make_fetch_result(text="short", link_count=0))},
    )

    assert result.ok is False
    assert result.has_data is False
    assert result.data_status == "permission_denied"
    assert result.tool == "cloakbrowser"
    assert result.stop_reason == "unsafe_escalation"
    assert result.error is not None
    assert result.error.code == "unsafe_escalation"
    assert [attempt["tool"] for attempt in result.attempts] == ["web_http", "browser_rendered"]
    assert result.attempts[1]["skipped"] is True


def test_all_adapters_fail_returns_all_attempts_and_no_data():
    profile = make_profile(fallback_order=["browser_rendered", "sitemap"])

    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", make_fetch_result(text="short", link_count=0)),
            "browser_rendered": FakeAdapter("browser_rendered", make_fetch_result(text="tiny", link_count=0)),
        },
    )

    assert result.ok is True
    assert result.has_data is False
    assert result.stop_reason == "no_available_adapter"
    assert [attempt["tool"] for attempt in result.attempts] == ["web_http", "browser_rendered", "sitemap"]
    assert result.attempts[-1]["data_status"] == "not_applicable"


def test_max_attempts_is_respected():
    profile = make_profile(fallback_order=["browser_rendered"])

    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", make_fetch_result(text="short", link_count=0)),
            "browser_rendered": FakeAdapter("browser_rendered", make_fetch_result(text="one two three four", link_count=1)),
        },
        max_attempts=1,
    )

    assert result.has_data is False
    assert result.stop_reason == "max_attempts_reached"
    assert [attempt["tool"] for attempt in result.attempts] == ["web_http"]


def test_allowed_domains_blocks_input_without_calling_adapter():
    adapter = FakeAdapter("web_http", make_fetch_result())

    result = acquire_with_fallback_result(
        "https://evil.example/page",
        profile=make_profile(),
        adapters={"web_http": adapter},
        allowed_domains=["example.com"],
    )

    assert result.ok is False
    assert result.data_status == "permission_denied"
    assert result.stop_reason == "unsafe_url"
    assert result.attempts == []
    assert adapter.calls == []


def test_allowed_domains_accepts_sequence_inputs():
    adapter = FakeAdapter("web_http", make_fetch_result(text="one two three four", link_count=1))

    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=make_profile(),
        adapters={"web_http": adapter},
        allowed_domains=("example.com",),
    )

    assert result.data_status == "present"
    assert adapter.calls


def test_final_url_outside_allowed_domains_is_not_marked_usable():
    profile = make_profile(fallback_order=[])
    adapter = FakeAdapter(
        "web_http",
        make_fetch_result(text="one two three four", link_count=1, final_url="https://other.test/page"),
    )

    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={"web_http": adapter},
        allowed_domains=["example.com"],
    )

    assert result.has_data is False
    assert result.ok is False
    assert result.data_status == "permission_denied"
    assert result.data == {}
    assert result.data_quality.passed is False
    assert result.data_quality.failure_reasons == ["final_url host other.test is outside allowed_domains"]
    assert result.stop_reason == "unsafe_final_url"
    assert result.error is not None
    assert result.error.code == "unsafe_final_url"


def test_not_found_status_is_terminal():
    profile = make_profile(fallback_order=["browser_rendered"])
    browser = FakeAdapter("browser_rendered", make_fetch_result(text="one two three four", link_count=1))

    result = acquire_with_fallback_result(
        "https://example.com/missing",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", make_fetch_result(text="not found words", status_code=404, link_count=1)),
            "browser_rendered": browser,
        },
        quality_gates={"min_words": 1, "min_links": 0, "min_document_links": 0},
    )

    assert result.data_status == "not_found"
    assert result.has_data is False
    assert result.data_count == 0
    assert result.data == {}
    assert result.data_quality.passed is False
    assert result.stop_reason == "not_found"
    assert len(result.attempts) == 1
    assert browser.calls == []


@pytest.mark.parametrize(
    ("status_code", "expected_status", "expected_stop_reason"),
    [
        (404, "not_found", "not_found"),
        (410, "not_found", "not_found"),
        (401, "auth_required", "auth_required"),
        (403, "permission_denied", "permission_denied"),
    ],
)
def test_terminal_status_codes_clear_usable_data_when_status_ok_gate_is_disabled(
    status_code: int,
    expected_status: str,
    expected_stop_reason: str,
):
    result = acquire_with_fallback_result(
        "https://example.com/page",
        profile=make_profile(fallback_order=["browser_rendered"]),
        adapters={
            "web_http": FakeAdapter(
                "web_http",
                make_fetch_result(text="one two three four", status_code=status_code, link_count=1),
            ),
            "browser_rendered": FakeAdapter("browser_rendered", make_fetch_result(text="fallback data", link_count=1)),
        },
        quality_gates={
            "min_words": 1,
            "min_links": 0,
            "min_document_links": 0,
            "require_status_ok": False,
        },
    )

    assert result.data_status == expected_status
    assert result.has_data is False
    assert result.data_count == 0
    assert result.data == {}
    assert result.data_quality.passed is False
    assert result.data_quality.failure_reasons == [expected_stop_reason]
    assert result.stop_reason == expected_stop_reason
    assert len(result.attempts) == 1


def test_structured_error_must_be_retryable_and_safe_to_escalate(monkeypatch):
    profile = make_profile(fallback_order=["browser_rendered"])
    attempts_by_adapter = {
        "web_http": CaptureAttempt(
            adapter="web_http",
            status="error",
            url="https://example.com/page",
            failure_reason="RateLimited: 429",
            recommended_next_adapter="browser_rendered",
            metadata={
                "error": {
                    "code": "rate_limited",
                    "message": "429",
                    "retryable": True,
                    "safe_to_escalate": False,
                    "exception_type": "RateLimited",
                }
            },
        ),
        "browser_rendered": CaptureAttempt(
            adapter="browser_rendered",
            status="passed",
            url="https://example.com/page",
            final_url="https://example.com/page",
            status_code=200,
            word_count=10,
            link_count=1,
        ),
    }

    def fake_run_capture_attempt(url, adapter, profile, prior_attempts=None):
        return attempts_by_adapter[adapter.adapter_id]

    monkeypatch.setattr(fallback_module, "run_capture_attempt", fake_run_capture_attempt)

    unsafe = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", RuntimeError("unused")),
            "browser_rendered": FakeAdapter("browser_rendered", RuntimeError("unused")),
        },
    )

    attempts_by_adapter["web_http"] = attempts_by_adapter["web_http"].model_copy(
        update={
            "metadata": {
                "error": {
                    "code": "timeout",
                    "message": "timeout",
                    "retryable": True,
                    "safe_to_escalate": True,
                    "exception_type": "TimeoutError",
                }
            }
        }
    )
    safe = acquire_with_fallback_result(
        "https://example.com/page",
        profile=profile,
        adapters={
            "web_http": FakeAdapter("web_http", RuntimeError("unused")),
            "browser_rendered": FakeAdapter("browser_rendered", RuntimeError("unused")),
        },
    )

    assert unsafe.data_status == "error"
    assert unsafe.error is not None
    assert unsafe.error.retryable is True
    assert unsafe.error.safe_to_escalate is False
    assert [attempt["tool"] for attempt in unsafe.attempts] == ["web_http"]
    assert safe.data_status == "present"
    assert [attempt["tool"] for attempt in safe.attempts] == ["web_http", "browser_rendered"]


def test_should_continue_requires_retryable_and_safe_to_escalate_error():
    unsafe = ToolResult(
        ok=False,
        has_data=False,
        data_status="error",
        error=ToolResultError(code="rate_limited", message="429", retryable=True, safe_to_escalate=False),
        stop_reason="error",
    )
    safe = ToolResult(
        ok=False,
        has_data=False,
        data_status="error",
        error=ToolResultError(code="timeout", message="timeout", retryable=True, safe_to_escalate=True),
        stop_reason="error",
    )

    assert should_continue(unsafe) is False
    assert should_continue(safe) is True
