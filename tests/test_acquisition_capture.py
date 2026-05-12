import pytest
import httpx

from web_listening.blocks.acquisition_capture import (
    HttpAcquisitionAdapter,
    build_builtin_adapters,
    evaluate_capture_attempt,
    evaluate_fetch_result,
    run_capture_attempt,
)
from web_listening.blocks.acquisition_profile import (
    AcquisitionAdapterConfig,
    AcquisitionProfile,
    AcquisitionQualityGates,
    CaptureAttempt,
)
from web_listening.blocks.crawler import FetchResult, HttpCrawler


def make_fetch_result(
    *,
    text: str = " ".join(f"word{i}" for i in range(160)),
    status_code: int | None = 200,
    final_url: str = "https://example.com/final",
    link_count: int = 5,
    document_link_count: int = 1,
    metadata_json: dict | None = None,
) -> FetchResult:
    metadata = {
        "link_count": link_count,
        "document_link_count": document_link_count,
    }
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


def make_profile(**updates) -> AcquisitionProfile:
    payload = {
        "profile_id": "example-acquisition-profile",
        "site_key": "example",
        "generated_at": "2026-05-12T12:00:00Z",
        "default_adapter": "web_http",
        "fallback_order": ["browser_rendered", "sitemap"],
        "quality_gates": AcquisitionQualityGates(
            min_words=120,
            min_links=3,
            min_document_links=1,
            require_status_ok=True,
            blocked_markers=["access denied", "captcha"],
        ),
        "adapters": [
            AcquisitionAdapterConfig(adapter="web_http"),
            AcquisitionAdapterConfig(adapter="browser_rendered"),
            AcquisitionAdapterConfig(adapter="sitemap"),
        ],
    }
    payload.update(updates)
    return AcquisitionProfile(**payload)


class FakeAdapter:
    adapter_id = "web_http"

    def __init__(self, result: FetchResult):
        self.result = result
        self.configs = []

    def capture(self, url: str, *, config=None) -> FetchResult:
        self.configs.append(config)
        return self.result


class RaisingAdapter:
    adapter_id = "web_http"

    def capture(self, url: str, *, config=None) -> FetchResult:
        raise RuntimeError("adapter exploded")


def test_http_like_good_fetch_result_passes_and_records_counts():
    gates = AcquisitionQualityGates(min_words=3, min_links=2, min_document_links=1)
    result = make_fetch_result(text="one two three four", link_count=2, document_link_count=1)

    attempt = evaluate_fetch_result("web_http", "https://example.com/", result, gates)

    assert attempt.status == "passed"
    assert attempt.failure_reason == ""
    assert attempt.word_count == 4
    assert attempt.link_count == 2
    assert attempt.document_link_count == 1
    assert attempt.final_url == "https://example.com/final"
    assert attempt.status_code == 200


def test_status_code_failure_when_status_ok_is_required():
    gates = AcquisitionQualityGates(min_words=3, min_links=0, require_status_ok=True)
    result = make_fetch_result(text="one two three four", status_code=503, link_count=0)

    attempt = evaluate_fetch_result("web_http", "https://example.com/", result, gates)

    assert attempt.status == "failed_quality_gate"
    assert "status_code 503" in attempt.failure_reason


def test_redirect_status_fails_when_status_ok_is_required():
    gates = AcquisitionQualityGates(min_words=3, min_links=0, require_status_ok=True)
    result = make_fetch_result(text="one two three four", status_code=302, link_count=0)

    attempt = evaluate_fetch_result("web_http", "https://example.com/", result, gates)

    assert attempt.status == "failed_quality_gate"
    assert "status_code 302" in attempt.failure_reason


@pytest.mark.parametrize(
    ("result", "expected_reason"),
    [
        (make_fetch_result(text="too short", link_count=3, document_link_count=1), "word_count 2 < min_words 3"),
        (
            make_fetch_result(text="one two three", link_count=1, document_link_count=1),
            "link_count 1 < min_links 2",
        ),
        (
            make_fetch_result(text="one two three", link_count=2, document_link_count=0),
            "document_link_count 0 < min_document_links 1",
        ),
    ],
)
def test_too_few_words_links_or_document_links_fail_with_useful_reason(
    result: FetchResult,
    expected_reason: str,
):
    gates = AcquisitionQualityGates(min_words=3, min_links=2, min_document_links=1)

    attempt = evaluate_fetch_result("web_http", "https://example.com/", result, gates)

    assert attempt.status == "failed_quality_gate"
    assert expected_reason in attempt.failure_reason


def test_blocked_marker_fails_case_insensitively():
    gates = AcquisitionQualityGates(
        min_words=1,
        min_links=0,
        blocked_markers=["access denied"],
    )
    result = make_fetch_result(text="ACCESS DENIED by upstream", link_count=0)

    attempt = evaluate_fetch_result("browser_rendered", "https://example.com/", result, gates)

    assert attempt.status == "blocked"
    assert "blocked marker" in attempt.failure_reason
    assert "access denied" in attempt.failure_reason


def test_evaluate_capture_attempt_preserves_existing_blocked_evidence():
    gates = AcquisitionQualityGates(min_words=1, min_links=0, blocked_markers=["captcha"])
    attempt = CaptureAttempt(
        adapter="web_http",
        status="blocked",
        url="https://example.com/",
        status_code=200,
        word_count=50,
        link_count=10,
        document_link_count=2,
        failure_reason="blocked marker found: captcha",
    )

    evaluated = evaluate_capture_attempt(attempt, gates)

    assert evaluated.status == "blocked"
    assert evaluated.failure_reason == "blocked marker found: captcha"


def test_evaluate_capture_attempt_preserves_error_evidence():
    gates = AcquisitionQualityGates(min_words=1, min_links=0)
    attempt = CaptureAttempt(
        adapter="web_http",
        status="error",
        url="https://example.com/",
        status_code=200,
        word_count=50,
        link_count=10,
        document_link_count=2,
        failure_reason="RuntimeError: adapter exploded",
    )

    evaluated = evaluate_capture_attempt(attempt, gates)

    assert evaluated.status == "error"
    assert evaluated.failure_reason == "RuntimeError: adapter exploded"


def test_run_capture_attempt_catches_adapter_exception_and_recommends_next_adapter():
    profile = make_profile()

    attempt = run_capture_attempt("https://example.com/", RaisingAdapter(), profile)

    assert attempt.status == "error"
    assert attempt.failure_reason == "RuntimeError: adapter exploded"
    assert attempt.recommended_next_adapter == "browser_rendered"


def test_run_capture_attempt_skips_disabled_next_adapter_in_recommendation():
    profile = make_profile(
        adapters=[
            AcquisitionAdapterConfig(adapter="web_http"),
            AcquisitionAdapterConfig(adapter="browser_rendered", enabled=False),
            AcquisitionAdapterConfig(adapter="sitemap"),
        ]
    )
    result = make_fetch_result(text="too short", link_count=0, document_link_count=0)

    attempt = run_capture_attempt("https://example.com/", FakeAdapter(result), profile)

    assert attempt.status == "failed_quality_gate"
    assert attempt.recommended_next_adapter == "sitemap"


def test_http_acquisition_adapter_returns_fetch_result_for_non_ok_http_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            text="<html><body><main>not found content words here</main></body></html>",
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapter = HttpAcquisitionAdapter(HttpCrawler(client=client))
    profile = make_profile(
        quality_gates=AcquisitionQualityGates(
            min_words=1,
            min_links=0,
            min_document_links=0,
            require_status_ok=True,
        )
    )

    attempt = run_capture_attempt("https://example.com/missing", adapter, profile)

    assert attempt.status == "failed_quality_gate"
    assert attempt.status_code == 404
    assert attempt.final_url == "https://example.com/missing"
    assert attempt.word_count >= 1
    assert "status_code 404" in attempt.failure_reason


def test_build_builtin_adapters_exposes_only_http_and_browser_adapters():
    adapters = build_builtin_adapters()

    assert set(adapters) == {"web_http", "browser_rendered"}
    assert adapters["web_http"].adapter_id == "web_http"
    assert adapters["browser_rendered"].adapter_id == "browser_rendered"
