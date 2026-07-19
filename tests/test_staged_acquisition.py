import base64
from dataclasses import replace
import hashlib
from datetime import datetime, timezone
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest

from web_listening.blocks.acquisition_gateway import GovernedAcquisitionGateway, LegacyCrawlerGateway
from web_listening.blocks.crawler import FetchResult, HttpCrawler
from web_listening.blocks.staged_workflow import _compile_acquisition_gateway, _portable_json
from web_listening.contracts import CaptureContent, CaptureError, CaptureResult
from web_listening.executors.http_wrapper import HttpAcquisitionAdapter
from web_listening.executors.registry import ExecutorMetadata
from web_listening.executors.wrapper_protocol import result_from_fetch


def _plan():
    digest = "a" * 64
    steps = tuple(MappingProxyType({
        "position": position, "executor_id": executor, "executor_version": "1.0.0",
        "recipe_id": f"recipe-{position}", "script_sha256": digest, "config": {"position": position},
    }) for position, executor in enumerate(("web_http", "browser_rendered")))
    return SimpleNamespace(mode="governed", steps=steps, acquisition_fingerprint="b" * 64,
                           site_key="demo", site_skill_id="demo-skill", site_skill_version="1.0.0",
                           site_skill_package_sha256=digest,
                           quality_gates=MappingProxyType({"min_words": 1, "min_links": 0,
                                                          "min_document_links": 0, "blocked_markers": ()}))


def _result(request, *, success=True):
    now = datetime.now(timezone.utc)
    lineage = {field: getattr(request, field) for field in
               ("request_id", "site_key", "site_skill_id", "site_skill_version",
                "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id")}
    if success:
        return CaptureResult(**lineage, state="succeeded", started_at=now, finished_at=now,
                             final_url=request.url, status_code=200,
                             content=CaptureContent(media_type="text/html", text="accepted"))
    return CaptureResult(**lineage, state="failed", started_at=now, finished_at=now,
                         error=CaptureError(code="blocked", message="blocked"))


def test_governed_gateway_uses_frozen_fallback_and_deterministic_request_identity():
    seen = []

    class Registry:
        def execute(self, request):
            seen.append(request)
            return _result(request, success=len(seen) > 1)

    gateway = GovernedAcquisitionGateway(_plan(), Registry())
    first = gateway.acquire("https://example.com/a", run_id="run-1", scope_id="scope-1")
    first_ids = [item.request_id for item in seen]
    seen.clear()
    second = gateway.acquire("https://example.com/a", run_id="run-1", scope_id="scope-1")

    assert first.accepted and second.accepted
    assert first.attempts == ("blocked", "accepted")
    assert [item.executor_id for item in seen] == ["web_http", "browser_rendered"]
    assert [item.request_id for item in seen] == first_ids
    assert all(item.metadata["acquisition_fingerprint"] == "b" * 64 for item in seen)


def test_legacy_gateway_preserves_fetch_mode_and_config():
    captured = {}

    class Crawler:
        def fetch_page(self, url, *, fetch_mode, fetch_config_json):
            captured.update(url=url, mode=fetch_mode, config=fetch_config_json)
            return FetchResult("<p>x</p>", "x", "x", "x", "x", {}, url, 200)

    outcome = LegacyCrawlerGateway(Crawler(), fetch_mode="playwright",
                                   fetch_config_json={"wait_for": "main"}).acquire(
                                       "https://example.com/", run_id="1", scope_id="2")
    assert outcome.accepted
    assert captured == {"url": "https://example.com/", "mode": "playwright",
                        "config": {"wait_for": "main"}}


@pytest.mark.parametrize("failure", ["lineage", "protocol", "redirect"])
def test_governed_gateway_treats_untrusted_result_mismatches_as_terminal(failure):
    seen = []

    class Registry:
        def execute(self, request):
            seen.append(request)
            if failure == "protocol":
                return {"state": "succeeded"}
            result = _result(request)
            if failure == "lineage":
                return result.model_copy(update={"recipe_id": "unplanned"})
            return result.model_copy(update={"final_url": "https://evil.example/login"})

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/a", run_id="run-1", scope_id="scope-1"
    )

    assert not outcome.accepted
    assert len(seen) == 1
    assert outcome.classification in {"lineage_mismatch", "protocol_error", "unsafe_redirect"}


def test_governed_gateway_rejects_runtime_version_mismatch_before_execution():
    class Registry:
        metadata = {"web_http": SimpleNamespace(version="2.0.0")}

        def execute(self, request):  # pragma: no cover - construction must fail
            raise AssertionError

    with pytest.raises(ValueError, match="does not match the frozen plan"):
        GovernedAcquisitionGateway(_plan(), Registry())


@pytest.mark.parametrize("retryable", [False, True])
def test_protocol_failure_is_terminal_without_fallback(retryable):
    seen = []

    class Registry:
        def execute(self, request):
            seen.append(request)
            now = datetime.now(timezone.utc)
            lineage = {field: getattr(request, field) for field in
                       ("request_id", "site_key", "site_skill_id", "site_skill_version",
                        "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id")}
            return CaptureResult(
                **lineage, state="failed", started_at=now, finished_at=now,
                error=CaptureError(code="executor_protocol_error", message="invalid", retryable=retryable),
            )

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/a", run_id="run-1", scope_id="scope-1"
    )

    assert outcome.classification == "integrity_error"
    assert len(seen) == 1


def test_unsafe_redirect_is_checked_before_not_found_classification():
    class Registry:
        def execute(self, request):
            return _result(request).model_copy(update={
                "final_url": "https://evil.example/missing", "status_code": 404,
            })

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/a", run_id="run-1", scope_id="scope-1"
    )

    assert outcome.classification == "unsafe_redirect"
    assert not outcome.coverage_complete


def test_document_capture_skips_page_word_and_link_quality_thresholds():
    plan = _plan()
    plan.quality_gates = MappingProxyType({
        "min_words": 999, "min_links": 999, "min_document_links": 999,
        "blocked_markers": (),
    })

    class Registry:
        def execute(self, request):
            return _result(request).model_copy(update={
                "content": CaptureContent(
                    media_type="application/pdf", text=base64.b64encode(b"%PDF").decode(),
                    sha256=hashlib.sha256(b"%PDF").hexdigest(),
                    metadata={"representation": "base64", "sha256_scope": "decoded-bytes"},
                ),
            })

    outcome = GovernedAcquisitionGateway(plan, Registry()).acquire(
        "https://example.com/report.pdf", run_id="run-1", scope_id="scope-1",
        content_kind="document",
    )

    assert outcome.accepted
    assert outcome.request.metadata["content_kind"] == "document"


@pytest.mark.parametrize(
    ("encoded", "digest"),
    [
        ("%%%not-base64%%%", hashlib.sha256(b"anything").hexdigest()),
        (base64.b64encode(b"actual bytes").decode(), hashlib.sha256(b"other bytes").hexdigest()),
    ],
)
def test_document_integrity_failure_is_terminal_without_fallback(encoded, digest):
    seen = []

    class Registry:
        def execute(self, request):
            seen.append(request)
            return _result(request).model_copy(update={
                "content": CaptureContent(
                    media_type="application/pdf", text=encoded, sha256=digest,
                    metadata={"representation": "base64", "sha256_scope": "decoded-bytes"},
                ),
            })

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/report.pdf", run_id="run-1", scope_id="scope-1",
        content_kind="document",
    )

    assert outcome.classification == "integrity_error"
    assert not outcome.accepted
    assert len(seen) == 1


def test_page_quality_uses_normalized_content_not_script_or_hidden_text():
    plan = _plan()
    plan.quality_gates = MappingProxyType({
        "min_words": 3, "min_links": 0, "min_document_links": 0,
        "blocked_markers": (),
    })

    class Registry:
        def execute(self, request):
            return _result(request).model_copy(update={
                "content": CaptureContent(
                    media_type="text/html",
                    text="<script>one two three four</script><p>visible</p>",
                ),
            })

    outcome = GovernedAcquisitionGateway(plan, Registry()).acquire(
        "https://example.com/a", run_id="run-1", scope_id="scope-1"
    )

    assert not outcome.accepted
    assert outcome.attempts == ("failed_quality_gate", "failed_quality_gate")


def test_gateway_construction_cleanup_preserves_original_error_and_closes_all(monkeypatch):
    closed = []

    class Crawler:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)
            if self.name == "web_http":
                raise RuntimeError("closer failed")

    def adapter(name):
        return type("Adapter", (), {"__init__": lambda self: setattr(self, "crawler", Crawler(name))})

    steps = tuple({
        "position": position, "executor_id": name, "executor_version": "1.0.0",
        "recipe_id": name, "script_sha256": "a" * 64, "config": {},
    } for position, name in enumerate(("web_http", "browser_rendered", "cloakbrowser")))
    compiled = SimpleNamespace(steps=steps)
    metadata = {name: SimpleNamespace(version="1.0.0") for name in
                ("web_http", "browser_rendered", "cloakbrowser")}

    monkeypatch.setattr("web_listening.blocks.acquisition_profile.load_acquisition_profile", lambda *a, **k: object())
    monkeypatch.setattr("web_listening.site_skill_registry.resolve_site_skill_contract", lambda **k: object())
    monkeypatch.setattr("web_listening.blocks.acquisition_execution_plan.compile_acquisition_execution_plan",
                        lambda *a: compiled)
    monkeypatch.setattr("web_listening.executors.registry.default_preview_registry",
                        lambda: SimpleNamespace(metadata=metadata))
    monkeypatch.setattr("web_listening.executors.registry.ExecutorRegistry",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("original construction error")))
    monkeypatch.setattr("web_listening.executors.http_wrapper.HttpAcquisitionAdapter", adapter("web_http"))
    monkeypatch.setattr("web_listening.executors.playwright_wrapper.BrowserAcquisitionAdapter",
                        adapter("browser_rendered"))
    monkeypatch.setattr("web_listening.executors.cloakbrowser_wrapper.CloakBrowserAcquisitionAdapter",
                        adapter("cloakbrowser"))

    plan = SimpleNamespace(site_key="demo", based_on={"acquisition_profile_id": "profile"})
    with pytest.raises(ValueError, match="original construction error"):
        _compile_acquisition_gateway(plan, acquisition_profile_path="profile.yaml")

    assert set(closed) == {"web_http", "browser_rendered", "cloakbrowser"}


def test_formal_gateway_constructs_and_dispatches_browseract_without_other_drivers(monkeypatch):
    seen = []

    class BrowserAct:
        executor_id = "browseract"

        def __init__(self, executable, *, limits):
            seen.append(("construct", executable, limits))

        def execute(self, request):
            seen.append(("execute", request.executor_id, dict(request.config)))
            return _result(request)

    step = {
        "position": 0, "executor_id": "browseract", "executor_version": "1.0.6",
        "recipe_id": "news-browseract", "script_sha256": "a" * 64,
        "config": {"executable": "/opt/browseract/bin/browser-act", "recipe": "stealth_extract"},
        "limits": {"timeout_seconds": 12.0, "stdout_bytes": 2048, "stderr_bytes": 1024},
    }
    compiled = _plan()
    compiled.steps = (MappingProxyType(step),)
    metadata = {"browseract": ExecutorMetadata(
        "browseract", "1.0.6", frozenset({"browser_read_only"}),
        30.0, 4 * 1024 * 1024, 64 * 1024, True,
    )}

    monkeypatch.setattr("web_listening.blocks.acquisition_profile.load_acquisition_profile", lambda *a, **k: object())
    monkeypatch.setattr("web_listening.site_skill_registry.resolve_site_skill_contract", lambda **k: object())
    monkeypatch.setattr("web_listening.blocks.acquisition_execution_plan.compile_acquisition_execution_plan",
                        lambda *a: compiled)
    monkeypatch.setattr("web_listening.executors.registry.default_preview_registry",
                        lambda: SimpleNamespace(metadata=metadata))
    monkeypatch.setattr("web_listening.executors.browseract.BrowserActExecutor", BrowserAct)
    for path in (
        "web_listening.executors.http_wrapper.HttpAcquisitionAdapter",
        "web_listening.executors.playwright_wrapper.BrowserAcquisitionAdapter",
        "web_listening.executors.cloakbrowser_wrapper.CloakBrowserAcquisitionAdapter",
    ):
        monkeypatch.setattr(path, lambda: pytest.fail("another driver was selected"))

    plan = SimpleNamespace(site_key="demo", based_on={"acquisition_profile_id": "profile"})
    gateway = _compile_acquisition_gateway(plan, acquisition_profile_path="profile.yaml")
    outcome = gateway.acquire("https://example.com/a", run_id="run-1", scope_id="scope-1")
    gateway.close()

    assert outcome.accepted
    assert seen[0][0:2] == ("construct", "/opt/browseract/bin/browser-act")
    assert seen[0][2].timeout_seconds == 12.0
    assert seen[1] == ("execute", "browseract", {"recipe": "stealth_extract"})
    assert len(seen) == 2


def test_governed_gateway_closes_owned_executors_idempotently():
    closed = []

    class Executor:
        def close(self):
            closed.append(True)

    class Registry:
        executors = {"web_http": Executor()}

    gateway = GovernedAcquisitionGateway(_plan(), Registry())
    gateway.close()
    gateway.close()

    assert closed == [True]


def test_governed_gateway_attempts_all_closers_before_raising_and_remains_closed():
    closed = []

    class FailingExecutor:
        def close(self):
            closed.append("failing")
            raise RuntimeError("close failed")

    class SuccessfulExecutor:
        def close(self):
            closed.append("successful")

    class Registry:
        executors = {"first": FailingExecutor(), "second": SuccessfulExecutor()}

    gateway = GovernedAcquisitionGateway(_plan(), Registry())
    with pytest.raises(RuntimeError, match="close failed"):
        gateway.close()
    gateway.close()

    assert closed == ["failing", "successful"]


def test_governed_gateway_falls_back_after_failed_page_quality_gate():
    seen = []

    class Registry:
        def execute(self, request):
            seen.append(request.executor_id)
            text = "" if len(seen) == 1 else "accepted"
            return _result(request).model_copy(update={
                "content": CaptureContent(media_type="text/html", text=text),
            })

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/a", run_id="run-1", scope_id="scope-1"
    )

    assert outcome.accepted
    assert outcome.attempts == ("failed_quality_gate", "accepted")
    assert seen == ["web_http", "browser_rendered"]


def test_real_http_adapter_metadata_is_portable_through_result_and_gateway():
    def respond(request):
        return httpx.Response(
            200, request=request,
            text="<html><body><h1>Ordinary heading</h1><p>accepted words</p></body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    adapter = HttpAcquisitionAdapter(HttpCrawler(client=client))

    class Registry:
        def execute(self, request):
            started = datetime.now(timezone.utc)
            page = adapter.capture(str(request.url), config={})
            page = replace(page, metadata_json=_portable_json({**page.metadata_json,
                                                               "headings": ("Ordinary heading",)}))
            return result_from_fetch(request, page, started)

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/", run_id="run-1", scope_id="scope-1"
    )

    assert outcome.accepted
    assert outcome.page.metadata_json["headings"] == ["Ordinary heading"]
    client.close()


def test_result_metadata_cannot_replace_quality_checked_page_content():
    class Registry:
        def execute(self, request):
            return _result(request).model_copy(update={
                "content": CaptureContent(
                    media_type="text/html",
                    text='<main><p>accepted</p><a href="/report.pdf">report</a></main>',
                ),
                "metadata": {
                    "raw_html": "BLOCKED metadata", "content_text": "BLOCKED metadata",
                    "markdown": "BLOCKED metadata", "fit_markdown": "BLOCKED metadata",
                },
            })

    outcome = GovernedAcquisitionGateway(_plan(), Registry()).acquire(
        "https://example.com/", run_id="run-1", scope_id="scope-1"
    )

    assert outcome.accepted
    assert "BLOCKED" not in outcome.page.raw_html
    assert "BLOCKED" not in outcome.page.content_text
    assert "/report.pdf" in outcome.page.raw_html
