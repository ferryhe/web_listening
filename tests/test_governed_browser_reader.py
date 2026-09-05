from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest

from web_listening.blocks.acquisition_execution_plan import AcquisitionExecutionPlan
from web_listening.blocks.access_gateway import AccessGatewayOriginError
from web_listening.blocks.governed_read import (
    MockClientReadGateway,
    build_runtime_read_gateway,
)
from web_listening.blocks import staged_workflow
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.registry import ExecutorMetadata
from web_listening.executors.playwright_wrapper import (
    BrowserAcquisitionAdapter,
    BrowserAcquisitionExecutor,
    BrowserCaptureCancelled,
    BrowserCaptureError,
    _PlaywrightSession,
    _default_session_factory,
    prepare_browser_acquisition_adapter,
)

_DIGEST = "a" * 64
_PLAN_DIGEST = "b" * 64


def _plan(*, config: dict[str, object] | None = None) -> AcquisitionExecutionPlan:
    step = MappingProxyType(
        {
            "position": 0,
            "adapter": "browser_rendered",
            "recipe_id": "news-browser",
            "executor_id": "browser_rendered",
            "executor_version": "1.0.0",
            "entrypoint": "scripts/browser.py",
            "script_sha256": _DIGEST,
            "required_capabilities": ("browser_render",),
            "executor_capabilities": ("browser_render",),
            "requires_authorized_access": False,
            "verification_rules": (),
            "limits": MappingProxyType(
                {
                    "timeout_seconds": 5.0,
                    "stdout_bytes": 4096,
                    "stderr_bytes": 1024,
                }
            ),
            **({"config": MappingProxyType(dict(config))} if config else {}),
        }
    )
    return AcquisitionExecutionPlan(
        schema_version="acquisition-execution-plan.v1",
        mode="governed",
        site_key="example",
        scope_fingerprint_algorithm="sha256:monitor-scope-semantic.v2",
        scope_fingerprint="c" * 64,
        acquisition_fingerprint_algorithm="sha256:acquisition-execution-plan.v1",
        acquisition_fingerprint=_PLAN_DIGEST,
        profile_id="profile",
        site_skill_id="example-skill",
        site_skill_version="1.0.0",
        site_skill_package_sha256=_DIGEST,
        recipe_id="news-browser",
        executor_id="browser_rendered",
        executor_version="1.0.0",
        entrypoint="scripts/browser.py",
        script_sha256=_DIGEST,
        required_capabilities=("browser_render",),
        quality_gates=MappingProxyType(
            {
                "min_words": 0,
                "min_links": 0,
                "min_document_links": 0,
                "blocked_markers": (),
            }
        ),
        limits=step["limits"],
        scope_budgets=MappingProxyType(
            {"max_depth": 1, "max_files": 1, "max_pages": 2}
        ),
        steps=(step,),
        warnings=(),
    )


class _ReadGateway:
    user_agent = "test-agent"

    def __init__(self, *, html: str, final_url: str = "https://example.com/final"):
        self.html = html
        self.final_url = final_url
        self.calls: list[tuple[str, int | None]] = []
        self.timeouts: list[float | None] = []

    def read(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        timeout_seconds: float | None = None,
    ):
        self.calls.append((url, max_body_bytes))
        self.timeouts.append(timeout_seconds)
        body = self.html.encode("utf-8")
        return SimpleNamespace(
            body=body,
            final_url=self.final_url,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            content_type="text/html; charset=utf-8",
            sha256=hashlib.sha256(body).hexdigest(),
            access_decision=SimpleNamespace(
                decision_id="access-decision-0123456789abcdef",
                decision_sha256="d" * 64,
                reason_code="robots.absent",
                redirect_hops=(),
            ),
        )


class _Session:
    runtime_id = "playwright"
    runtime_version = "1.62.0"

    def __init__(
        self, *, rendered_html: str | None = None, error: BaseException | None = None
    ):
        self.rendered_html = rendered_html
        self.error = error
        self.events: list[object] = []
        self.close_error: BaseException | None = None

    def open(self) -> None:
        self.events.append("open")
        if self.error is not None and getattr(self.error, "phase", "") == "open":
            raise self.error

    def render(
        self,
        response,
        *,
        timeout_milliseconds: int,
        wait_until: str | None = None,
    ) -> str:
        self.events.append(
            ("render", response.final_url, timeout_milliseconds, wait_until)
        )
        if self.error is not None and getattr(self.error, "phase", "") != "open":
            raise self.error
        return (
            self.rendered_html
            if self.rendered_html is not None
            else response.body.decode()
        )

    def close(self) -> None:
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error


def _prepared(
    gateway,
    session: _Session,
    *,
    config: dict[str, object] | None = None,
) -> BrowserAcquisitionAdapter:
    plan = _plan(config=config)
    return prepare_browser_acquisition_adapter(
        plan,
        plan.steps[0],
        gateway,
        session_factory=lambda: session,
    )


def _request(*, config: dict[str, object] | None = None) -> CaptureRequest:
    return CaptureRequest(
        site_key="example",
        site_skill_id="example-skill",
        site_skill_version="1.0.0",
        site_skill_digest=_DIGEST,
        recipe_id="news-browser",
        run_id="run-1",
        scope_id="scope-1",
        request_id="request-1",
        executor_id="browser_rendered",
        url="https://example.com/start",
        requested_at=datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
        config=config or {},
        metadata={"content_kind": "page"},
    )


def _mixed_gateway_plan(
    *,
    http_timeout: float,
    browser_timeout: float,
    http_bytes: int = 8192,
    browser_bytes: int = 4096,
) -> AcquisitionExecutionPlan:
    base = _plan()
    browser_step = dict(base.steps[0])
    browser_step["position"] = 1
    browser_step["limits"] = MappingProxyType(
        {
            "timeout_seconds": browser_timeout,
            "stdout_bytes": browser_bytes,
            "stderr_bytes": 1024,
        }
    )
    http_step = {
        **browser_step,
        "position": 0,
        "adapter": "web_http",
        "executor_id": "web_http",
        "entrypoint": "scripts/http.py",
        "required_capabilities": ("http_get",),
        "executor_capabilities": ("http_get",),
        "limits": MappingProxyType(
            {
                "timeout_seconds": http_timeout,
                "stdout_bytes": http_bytes,
                "stderr_bytes": 1024,
            }
        ),
    }
    return replace(
        base,
        executor_id="web_http",
        entrypoint="scripts/http.py",
        required_capabilities=("http_get",),
        limits=http_step["limits"],
        steps=(MappingProxyType(http_step), MappingProxyType(browser_step)),
    )


def _patch_staged_compile_inputs(
    monkeypatch,
    compiled: AcquisitionExecutionPlan,
    *,
    gateway_builder,
):
    profile = SimpleNamespace(safety=SimpleNamespace(allowed_domains=("example.com",)))
    capabilities = {
        "web_http": frozenset({"http_get"}),
        "browser_rendered": frozenset({"browser_render"}),
    }
    preview = SimpleNamespace(
        metadata={
            executor_id: ExecutorMetadata(
                executor_id,
                "1.0.0",
                capabilities[executor_id],
                30.0,
                4 * 1024 * 1024,
                64 * 1024,
            )
            for executor_id in {str(step["executor_id"]) for step in compiled.steps}
        }
    )
    monkeypatch.setattr(
        "web_listening.blocks.acquisition_profile.load_acquisition_profile",
        lambda *args, **kwargs: profile,
    )
    monkeypatch.setattr(
        "web_listening.site_skill_registry.resolve_site_skill_contract",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "web_listening.blocks.acquisition_execution_plan.compile_acquisition_execution_plan",
        lambda *args: compiled,
    )
    monkeypatch.setattr(
        "web_listening.executors.registry.default_preview_registry", lambda: preview
    )
    monkeypatch.setattr(
        "web_listening.blocks.governed_read.build_runtime_read_gateway",
        gateway_builder,
    )
    return SimpleNamespace(
        site_key="example",
        seed_url="https://example.com/start",
        homepage_url="https://example.com/",
        based_on={
            "acquisition_profile_id": "profile",
            "site_skill_version": "1.0.0",
            "site_skill_package_sha256": _DIGEST,
            "site_skill_recipe_id": "news-browser",
            "site_skill_script_sha256": _DIGEST,
            "executor_version": "1.0.0",
        },
    )


def test_unprepared_adapter_rejects_before_runtime_or_gateway() -> None:
    adapter = BrowserAcquisitionAdapter()

    with pytest.raises(BrowserCaptureError) as captured:
        adapter.capture("https://example.com/start", config={})

    assert captured.value.code == "browser_authority_required"


def test_prepared_adapter_rejects_config_drift_before_runtime_or_gateway() -> None:
    gateway = _ReadGateway(html="<html><body>ok</body></html>")
    session = _Session()
    adapter = _prepared(gateway, session, config={"wait_until": "domcontentloaded"})

    with pytest.raises(BrowserCaptureError) as captured:
        adapter.capture(
            "https://example.com/start", config={"wait_until": "networkidle"}
        )

    assert captured.value.code == "browser_authority_mismatch"
    assert gateway.calls == []
    assert session.events == []


@pytest.mark.parametrize(
    "config",
    [
        {"wait_until": "settled"},
        {"wait_until": 1},
        {"viewport": {"width": 1280, "height": 720}},
    ],
)
def test_preparation_rejects_unsupported_browser_config_before_actions(
    config: dict[str, object],
) -> None:
    gateway = _ReadGateway(html="<html><body>unused</body></html>")
    session = _Session()
    plan = _plan(config=config)

    with pytest.raises(BrowserCaptureError) as captured:
        prepare_browser_acquisition_adapter(
            plan,
            plan.steps[0],
            gateway,
            session_factory=lambda: session,
        )

    assert captured.value.code == "browser_config_unsupported"
    assert gateway.calls == []
    assert session.events == []


def test_prepared_adapter_passes_sealed_wait_until_to_session() -> None:
    gateway = _ReadGateway(html="<html><body>source</body></html>")
    session = _Session(rendered_html="<html><body>rendered</body></html>")
    adapter = _prepared(gateway, session, config={"wait_until": "networkidle"})

    result = adapter.capture(
        "https://example.com/start", config={"wait_until": "networkidle"}
    )

    assert result.content_text == "rendered"
    assert session.events == [
        "open",
        ("render", "https://example.com/final", 5000, "networkidle"),
        "close",
    ]


def test_prepared_adapter_returns_rendered_fetch_result_and_recomputable_digest() -> (
    None
):
    gateway = _ReadGateway(
        html=(
            "<html><body><main id='article'>Official notice</main>"
            "<script>document.querySelector('#article').textContent='Rendered notice'</script>"
            "</body></html>"
        )
    )
    rendered = "<html><body><main id='article'>Rendered notice</main></body></html>"
    session = _Session(rendered_html=rendered)

    result = _prepared(gateway, session).capture("https://example.com/start", config={})

    assert result.raw_html == rendered
    assert result.content_text == "Rendered notice"
    assert result.markdown == "Rendered notice"
    assert result.fit_markdown == "Rendered notice"
    assert result.final_url == "https://example.com/final"
    assert result.status_code == 200
    assert result.metadata_json["requested_url"] == "https://example.com/start"
    assert result.metadata_json["adapter_id"] == "browser_rendered"
    assert result.metadata_json["adapter_version"] == "1.0.0"
    assert result.metadata_json["runtime_id"] == "playwright"
    assert result.metadata_json["runtime_version"] == "1.62.0"
    assert (
        result.metadata_json["content_sha256"]
        == hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    )
    assert gateway.calls == [("https://example.com/start", 4096)]
    assert session.events == [
        "open",
        ("render", "https://example.com/final", 5000, "domcontentloaded"),
        "close",
    ]


def test_executor_emits_stable_success_capture_result() -> None:
    rendered = "<html><body><p>Small official announcement.</p></body></html>"
    gateway = _ReadGateway(html=rendered)
    adapter = _prepared(gateway, _Session(rendered_html=rendered))

    result = BrowserAcquisitionExecutor(adapter).execute(_request())

    assert isinstance(result, CaptureResult)
    assert result.state == "succeeded"
    assert str(result.final_url) == "https://example.com/final"
    assert result.status_code == 200
    assert result.content is not None
    assert result.content.text == rendered
    assert result.content.sha256 == hashlib.sha256(rendered.encode()).hexdigest()
    assert result.metadata["requested_url"] == "https://example.com/start"
    assert result.metadata["final_url"] == "https://example.com/final"
    assert result.metadata["content_text"] == "Small official announcement."
    assert result.metadata["markdown"] == "Small official announcement."
    assert result.metadata["fit_markdown"] == "Small official announcement."
    assert result.started_at <= result.finished_at


def test_prepared_adapter_and_executor_authority_cannot_be_replaced() -> None:
    adapter = _prepared(
        _ReadGateway(html="<html><body>notice</body></html>"), _Session()
    )
    executor = BrowserAcquisitionExecutor(adapter)

    with pytest.raises(AttributeError):
        adapter._authority = None
    with pytest.raises(AttributeError):
        executor._adapter = BrowserAcquisitionAdapter()


def test_existing_offline_gateway_governs_requested_and_redirect_urls() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>redirected notice</p></body></html>",
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = MockClientReadGateway(
        client, user_agent="web-listening-bot/test", max_body_bytes=4096
    )
    session = _Session()
    adapter = _prepared(gateway, session)

    result = adapter.capture("https://example.com/start", config={})

    assert seen == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert result.final_url == "https://example.com/final"
    assert result.content_text == "redirected notice"


def _redirecting_timeout_gateway():
    seen: list[tuple[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.extensions.get("web_listening_timeout_seconds"),
            )
        )
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        if str(request.url) == "https://example.com/start":
            return httpx.Response(
                302,
                headers={"location": "https://example.net/final"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>redirected notice</p></body></html>",
            request=request,
        )

    gateway = MockClientReadGateway(
        httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="web-listening-bot/test",
        max_body_bytes=4096,
    )
    gateway._prepare_origins(("https://example.com/start", "https://example.net/final"))
    return gateway, seen


def test_browser_step_timeout_covers_robots_and_cross_origin_redirect_chain() -> None:
    gateway, seen = _redirecting_timeout_gateway()

    result = _prepared(gateway, _Session()).capture(
        "https://example.com/start", config={}
    )

    assert result.final_url == "https://example.net/final"
    assert seen == [
        ("https://example.com/robots.txt", 5.0),
        ("https://example.com/start", 5.0),
        ("https://example.net/robots.txt", 5.0),
        ("https://example.net/final", 5.0),
    ]


def test_http_step_timeout_covers_robots_and_cross_origin_redirect_chain() -> None:
    gateway, seen = _redirecting_timeout_gateway()

    result = gateway.read(
        "https://example.com/start",
        max_body_bytes=4096,
        timeout_seconds=30.0,
    )

    assert result.final_url == "https://example.net/final"
    assert seen == [
        ("https://example.com/robots.txt", 30.0),
        ("https://example.com/start", 30.0),
        ("https://example.net/robots.txt", 30.0),
        ("https://example.net/final", 30.0),
    ]


def test_policy_single_flight_keeps_timeout_local_to_fetching_caller() -> None:
    robots_started = threading.Event()
    release_robots = threading.Event()
    seen: list[tuple[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.extensions.get("web_listening_timeout_seconds"),
            )
        )
        if request.url.path == "/robots.txt":
            robots_started.set()
            assert release_robots.wait(timeout=2)
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><p>notice</p></body></html>",
            request=request,
        )

    gateway = MockClientReadGateway(
        httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="web-listening-bot/test",
        max_body_bytes=4096,
    )
    gateway._prepare_origins(("https://example.com/start",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            gateway.read,
            "https://example.com/start",
            max_body_bytes=4096,
            timeout_seconds=30.0,
        )
        assert robots_started.wait(timeout=2)
        second = pool.submit(
            gateway.read,
            "https://example.com/start",
            max_body_bytes=4096,
            timeout_seconds=5.0,
        )
        release_robots.set()
        assert first.result(timeout=2).status_code == 200
        assert second.result(timeout=2).status_code == 200

    assert [item for item in seen if item[0].endswith("/robots.txt")] == [
        ("https://example.com/robots.txt", 30.0)
    ]
    assert sorted(
        timeout for url, timeout in seen if not url.endswith("/robots.txt")
    ) == [5.0, 30.0]


def test_sealed_offline_gateway_rejects_other_origin_before_render() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>notice</body></html>",
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = MockClientReadGateway(
        client, user_agent="web-listening-bot/test", max_body_bytes=4096
    )
    gateway._prepare_origins(("https://example.com/start",))
    session = _Session()
    adapter = _prepared(gateway, session)

    with pytest.raises(BrowserCaptureError) as captured:
        adapter.capture("https://other.example/start", config={})

    assert captured.value.code == "navigation_failed"
    assert seen == []
    assert session.events == ["open", "close"]


def test_production_gateway_origin_rejection_propagates_before_launch_or_request() -> (
    None
):
    gateway = build_runtime_read_gateway(
        authority_sha256=_PLAN_DIGEST,
        seed_urls=("https://example.com/start",),
        allowed_domains=(),
        user_agent="web-listening-bot/test",
        max_body_bytes=4096,
        timeout_seconds=1.0,
        budget_limit=2,
    )
    session = _Session()
    adapter = _prepared(gateway, session)

    with pytest.raises(AccessGatewayOriginError):
        adapter.capture("https://other.example/start", config={})

    assert session.events == ["open", "close"]


def test_staged_compiler_binds_browser_executor_to_compiled_plan_and_gateway(
    monkeypatch,
) -> None:
    compiled = _plan()
    read_gateway = _ReadGateway(html="<html><body><p>render source</p></body></html>")
    session = _Session(rendered_html="<html><body><p>rendered result</p></body></html>")
    preview = SimpleNamespace(
        metadata={
            "browser_rendered": ExecutorMetadata(
                "browser_rendered",
                "1.0.0",
                frozenset({"browser_render"}),
                30.0,
                4 * 1024 * 1024,
                64 * 1024,
            )
        }
    )
    profile = SimpleNamespace(safety=SimpleNamespace(allowed_domains=("example.com",)))
    outer_plan = SimpleNamespace(
        site_key="example",
        seed_url="https://example.com/start",
        homepage_url="https://example.com/",
        based_on={
            "acquisition_profile_id": "profile",
            "site_skill_version": "1.0.0",
            "site_skill_package_sha256": _DIGEST,
            "site_skill_recipe_id": "news-browser",
            "site_skill_script_sha256": _DIGEST,
            "executor_version": "1.0.0",
        },
    )
    original_prepare = prepare_browser_acquisition_adapter

    monkeypatch.setattr(
        "web_listening.blocks.acquisition_profile.load_acquisition_profile",
        lambda *args, **kwargs: profile,
    )
    monkeypatch.setattr(
        "web_listening.site_skill_registry.resolve_site_skill_contract",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "web_listening.blocks.acquisition_execution_plan.compile_acquisition_execution_plan",
        lambda *args: compiled,
    )
    monkeypatch.setattr(
        "web_listening.executors.registry.default_preview_registry", lambda: preview
    )
    monkeypatch.setattr(
        "web_listening.blocks.governed_read.build_runtime_read_gateway",
        lambda **kwargs: read_gateway,
    )
    monkeypatch.setattr(
        "web_listening.executors.playwright_wrapper.prepare_browser_acquisition_adapter",
        lambda plan, step, gateway: original_prepare(
            plan,
            step,
            gateway,
            session_factory=lambda: session,
        ),
    )

    gateway = staged_workflow._compile_acquisition_gateway(
        outer_plan, acquisition_profile_path="profile.yaml"
    )
    outcome = gateway.acquire(
        "https://example.com/start", run_id="run-1", scope_id="scope-1"
    )

    assert outcome.accepted
    assert outcome.page is not None
    assert outcome.page.content_text == "rendered result"
    assert outcome.request is not None
    assert outcome.request.metadata["acquisition_fingerprint"] == _PLAN_DIGEST
    assert read_gateway.calls == [("https://example.com/start", 4096)]
    gateway.close()


@pytest.mark.parametrize("browser_timeout", [5.0, 30.0])
def test_mixed_timeouts_keep_shared_max_and_exact_executor_limits(
    monkeypatch, browser_timeout: float
) -> None:
    compiled = _mixed_gateway_plan(
        http_timeout=30.0,
        browser_timeout=browser_timeout,
    )
    read_gateway = _ReadGateway(html="<html><body>source notice</body></html>")
    session = _Session(rendered_html="<html><body>rendered notice</body></html>")
    gateway_builds: list[dict[str, object]] = []

    def build_gateway(**kwargs):
        gateway_builds.append(kwargs)
        return read_gateway

    outer_plan = _patch_staged_compile_inputs(
        monkeypatch,
        compiled,
        gateway_builder=build_gateway,
    )
    original_prepare = prepare_browser_acquisition_adapter
    monkeypatch.setattr(
        "web_listening.executors.playwright_wrapper.prepare_browser_acquisition_adapter",
        lambda plan, step, gateway: original_prepare(
            plan,
            step,
            gateway,
            session_factory=lambda: session,
        ),
    )

    gateway = staged_workflow._compile_acquisition_gateway(
        outer_plan, acquisition_profile_path="profile.yaml"
    )
    http_request = _request().model_copy(update={"executor_id": "web_http"})
    http_result = gateway.registry.execute(http_request)
    browser_result = gateway.registry.execute(_request())

    assert http_result.state == "succeeded"
    assert browser_result.state == "succeeded"
    assert gateway_builds == [
        {
            **gateway_builds[0],
            "timeout_seconds": 30.0,
            "max_body_bytes": 8192,
        }
    ]
    assert read_gateway.calls == [
        ("https://example.com/start", 8192),
        ("https://example.com/start", 4096),
    ]
    assert read_gateway.timeouts == [30.0, browser_timeout]
    gateway.close()


def test_gateway_limits_preserve_web_http_only_pair() -> None:
    mixed = _mixed_gateway_plan(http_timeout=30.0, browser_timeout=30.0)
    http_only = replace(
        mixed,
        steps=(mixed.steps[0],),
    )

    assert staged_workflow._sealed_gateway_limits(http_only) == (30.0, 8192)


@pytest.mark.parametrize(
    ("rendered", "code"),
    [
        ("<html><body></body></html>", "empty_content"),
        (
            "<html><body><p>Please verify you are human to continue</p></body></html>",
            "blocked",
        ),
    ],
)
def test_empty_and_blocked_rendered_dom_are_stable_failures(
    rendered: str, code: str
) -> None:
    gateway = _ReadGateway(html=rendered)
    session = _Session(rendered_html=rendered)

    with pytest.raises(BrowserCaptureError) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    assert captured.value.code == code
    assert captured.value.final_url == "https://example.com/final"
    assert captured.value.status_code == 200
    assert session.events[-1] == "close"


def test_rendered_dom_cannot_exceed_compiled_step_byte_limit() -> None:
    gateway = _ReadGateway(html="<html><body>small</body></html>")
    session = _Session(rendered_html="<html><body>" + ("x" * 4096) + "</body></html>")

    with pytest.raises(BrowserCaptureError) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    assert captured.value.code == "rendered_content_limit"
    assert captured.value.final_url == "https://example.com/final"
    assert session.events[-1] == "close"


@pytest.mark.parametrize(
    "code",
    ["missing_runtime", "launch_failure", "navigation_timeout", "page_closed"],
)
def test_runtime_failures_keep_finite_codes_and_cleanup(code: str) -> None:
    error = BrowserCaptureError(code)
    error.phase = "open" if code == "missing_runtime" else "render"
    gateway = _ReadGateway(html="<html><body>notice</body></html>")
    session = _Session(error=error)

    with pytest.raises(BrowserCaptureError) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    assert captured.value.code == code
    assert session.events[-1] == "close"
    assert gateway.calls == (
        [] if code == "missing_runtime" else [("https://example.com/start", 4096)]
    )


@pytest.mark.parametrize(
    "code",
    [
        "missing_runtime",
        "launch_failure",
        "navigation_timeout",
        "page_closed",
        "close_failure",
    ],
)
def test_executor_maps_browser_failures_to_stable_capture_results(code: str) -> None:
    error = BrowserCaptureError(code)
    error.phase = "open" if code == "missing_runtime" else "render"
    session = _Session(error=None if code == "close_failure" else error)
    if code == "close_failure":
        session.close_error = error
    adapter = _prepared(_ReadGateway(html="<html><body>notice</body></html>"), session)

    result = BrowserAcquisitionExecutor(adapter).execute(_request())

    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == code
    assert result.error.message == code
    assert result.content is None
    assert result.metadata["requested_url"] == "https://example.com/start"


def test_cancellation_is_stable_and_preserves_cancellation_semantics() -> None:
    cancellation = asyncio.CancelledError()
    session = _Session(error=cancellation)
    gateway = _ReadGateway(html="<html><body>notice</body></html>")

    with pytest.raises(BrowserCaptureCancelled) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    assert isinstance(captured.value, asyncio.CancelledError)
    assert captured.value.code == "cancelled"
    assert session.events[-1] == "close"


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), SystemExit(7)])
def test_base_exception_wins_over_close_failure(interrupt: BaseException) -> None:
    session = _Session(error=interrupt)
    session.close_error = BrowserCaptureError("close_failure")
    gateway = _ReadGateway(html="<html><body>notice</body></html>")

    with pytest.raises(type(interrupt)) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    assert captured.value is interrupt
    assert session.events[-1] == "close"


@pytest.mark.parametrize(
    "interrupt", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(7)]
)
def test_close_base_exception_wins_over_ordinary_primary(
    interrupt: BaseException,
) -> None:
    session = _Session(error=BrowserCaptureError("navigation_timeout"))
    session.close_error = interrupt
    gateway = _ReadGateway(html="<html><body>notice</body></html>")

    expected = (
        BrowserCaptureCancelled
        if isinstance(interrupt, asyncio.CancelledError)
        else type(interrupt)
    )
    with pytest.raises(expected) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    if isinstance(interrupt, asyncio.CancelledError):
        assert captured.value.code == "cancelled"
    else:
        assert captured.value is interrupt
    assert session.events[-1] == "close"


def test_close_failure_after_success_is_stable() -> None:
    session = _Session(rendered_html="<html><body>notice</body></html>")
    session.close_error = BrowserCaptureError("close_failure")
    gateway = _ReadGateway(html="<html><body>notice</body></html>")

    with pytest.raises(BrowserCaptureError) as captured:
        _prepared(gateway, session).capture("https://example.com/start", config={})

    assert captured.value.code == "close_failure"


def test_playwright_session_blocks_all_non_document_requests_and_closes_every_resource(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"qualified")

    class Route:
        def __init__(self, url: str, *, navigation: bool) -> None:
            self.request = SimpleNamespace(
                url=url, is_navigation_request=lambda: navigation
            )

        def fulfill(self, **kwargs) -> None:
            events.append(("fulfill", self.request.url, kwargs))

        def abort(self, reason: str) -> None:
            events.append(("abort", self.request.url, reason))

    class Page:
        def route(self, pattern, handler) -> None:
            events.append(("route", pattern))
            self.handler = handler

        def route_web_socket(self, pattern, handler) -> None:
            events.append(("route_web_socket", pattern))
            socket = SimpleNamespace(close=lambda: events.append("web_socket.close"))
            handler(socket)

        def goto(self, url, *, wait_until, timeout):
            events.append(("goto", url, wait_until, timeout))
            self.handler(Route(url, navigation=True))
            self.handler(Route("https://tracker.invalid/pixel.js", navigation=False))
            return SimpleNamespace(status=200)

        def content(self):
            return "<html><body>rendered</body></html>"

        def close(self):
            events.append("page.close")
            raise RuntimeError("expected close fault")

    class Context:
        def new_page(self):
            events.append("new_page")
            return Page()

        def close(self):
            events.append("context.close")

    class Browser:
        def new_context(self, **kwargs):
            events.append(("new_context", kwargs))
            return Context()

        def close(self):
            events.append("browser.close")

    class Chromium:
        executable_path = str(executable)

        def launch(self, **kwargs):
            events.append(("launch", kwargs))
            return Browser()

    class Driver:
        chromium = Chromium()

        def stop(self):
            events.append("playwright.stop")

    class Starter:
        def start(self):
            events.append("playwright.start")
            return Driver()

    api = SimpleNamespace(
        sync_playwright=lambda: Starter(),
        Error=RuntimeError,
        TimeoutError=TimeoutError,
    )
    session = _PlaywrightSession(
        sync_api=api,
        runtime_version="1.62.0",
        temporary_directory_factory=lambda: _TemporaryDirectory(tmp_path / "runtime"),
    )
    response = SimpleNamespace(
        body=b"<html><body><script>inline()</script></body></html>",
        final_url="https://example.com/final",
        status_code=200,
        content_type="text/html",
    )

    session.open()
    assert session.render(
        response,
        timeout_milliseconds=5000,
        wait_until="networkidle",
    ).endswith("</html>")
    with pytest.raises(BrowserCaptureError) as captured:
        session.close()

    assert captured.value.code == "close_failure"
    assert [
        event for event in events if isinstance(event, tuple) and event[0] == "fulfill"
    ]
    assert (
        "abort",
        "https://tracker.invalid/pixel.js",
        "blockedbyclient",
    ) in events
    assert (
        "launch",
        {
            "headless": True,
            "downloads_path": str(tmp_path / "runtime"),
            "timeout": 5000,
        },
    ) in events
    assert (
        "goto",
        "https://example.com/final",
        "networkidle",
        5000,
    ) in events
    assert events[-4:] == [
        "page.close",
        "context.close",
        "browser.close",
        "playwright.stop",
    ]
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize(
    ("phase", "expected_code"),
    [
        ("missing_binary", "missing_runtime"),
        ("launch", "launch_failure"),
        ("timeout", "navigation_timeout"),
        ("page_closed", "page_closed"),
    ],
)
def test_playwright_session_failure_phases_clean_owned_resources(
    tmp_path: Path, phase: str, expected_code: str
) -> None:
    events: list[str] = []
    executable = tmp_path / "chrome.exe"
    if phase != "missing_binary":
        executable.write_bytes(b"qualified")

    class NavigationTimeout(RuntimeError):
        pass

    class Page:
        def route(self, _pattern, handler) -> None:
            self.handler = handler

        def route_web_socket(self, _pattern, handler) -> None:
            handler(SimpleNamespace(close=lambda: events.append("web_socket.close")))

        def goto(self, url, **_kwargs):
            if phase == "timeout":
                raise NavigationTimeout("timeout")
            route = SimpleNamespace(
                request=SimpleNamespace(url=url, is_navigation_request=lambda: True),
                fulfill=lambda **_kwargs: None,
                abort=lambda _reason: None,
            )
            self.handler(route)
            return SimpleNamespace(status=200)

        def content(self):
            if phase == "page_closed":
                raise RuntimeError("target page closed")
            return "<html><body>rendered</body></html>"

        def close(self):
            events.append("page.close")

    class Context:
        def new_page(self):
            return Page()

        def close(self):
            events.append("context.close")

    class Browser:
        def new_context(self, **_kwargs):
            return Context()

        def close(self):
            events.append("browser.close")

    class Chromium:
        executable_path = str(executable)

        def launch(self, **_kwargs):
            if phase == "launch":
                raise RuntimeError("launch")
            return Browser()

    class Driver:
        chromium = Chromium()

        def stop(self):
            events.append("playwright.stop")

    api = SimpleNamespace(
        sync_playwright=lambda: SimpleNamespace(start=lambda: Driver()),
        TimeoutError=NavigationTimeout,
    )
    session = _PlaywrightSession(
        sync_api=api,
        runtime_version="1.62.0",
        temporary_directory_factory=lambda: _TemporaryDirectory(
            tmp_path / "failure-runtime"
        ),
    )
    response = SimpleNamespace(
        body=b"<html><body>source</body></html>",
        final_url="https://example.com/final",
        status_code=200,
        content_type="text/html",
    )

    try:
        session.open()
        with pytest.raises(BrowserCaptureError) as captured:
            session.render(
                response,
                timeout_milliseconds=10,
                wait_until="domcontentloaded",
            )
    except BrowserCaptureError as captured_open:
        assert phase == "missing_binary"
        captured = SimpleNamespace(value=captured_open)
    finally:
        session.close()

    assert captured.value.code == expected_code
    assert events[-1] == "playwright.stop"
    if phase in {"timeout", "page_closed"}:
        assert events == [
            "web_socket.close",
            "page.close",
            "context.close",
            "browser.close",
            "playwright.stop",
        ]
    assert not (tmp_path / "failure-runtime").exists()


@pytest.mark.live
def test_qualified_playwright_renders_governed_offline_fixture() -> None:
    try:
        runtime_version = metadata.version("playwright")
    except metadata.PackageNotFoundError:
        pytest.skip("Playwright 1.62.0 is not installed on this host")
    if runtime_version != "1.62.0":
        pytest.skip(f"host Playwright is {runtime_version}, not qualified 1.62.0")

    preflight = _default_session_factory()
    try:
        preflight.open()
    except BrowserCaptureError as exc:
        if exc.code == "missing_runtime":
            pytest.skip("the qualified Chromium binary is not installed on this host")
        raise
    finally:
        preflight.close()

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><body><main id='notice'></main>"
                "<script src='https://example.net/blocked.js' "
                "onerror=\"document.body.dataset.subresource='blocked'\"></script>"
                "<script>setTimeout(()=>{document.querySelector('#notice').textContent="
                "'Rendered delayed notice';document.body.dataset.rendered='yes'},100)"
                "</script></body></html>"
            ),
            request=request,
        )

    gateway = MockClientReadGateway(
        httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="web-listening-bot/offline-browser-test",
        max_body_bytes=4096,
    )
    plan = _plan(config={"wait_until": "networkidle"})
    adapter = prepare_browser_acquisition_adapter(
        plan, plan.steps[0], gateway, session_factory=_default_session_factory
    )

    result = adapter.capture(
        "https://example.com/start", config={"wait_until": "networkidle"}
    )

    assert seen == [
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert result.final_url == "https://example.com/final"
    assert result.status_code == 200
    assert result.content_text == "Rendered delayed notice"
    assert result.markdown == "Rendered delayed notice"
    assert result.fit_markdown == "Rendered delayed notice"
    assert 'data-rendered="yes"' in result.raw_html
    assert 'data-subresource="blocked"' in result.raw_html
    assert (
        result.metadata_json["content_sha256"]
        == hashlib.sha256(result.raw_html.encode("utf-8")).hexdigest()
    )


class _TemporaryDirectory:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> str:
        self.path.mkdir()
        return str(self.path)

    def __exit__(self, *_args) -> None:
        self.path.rmdir()


def test_browser_result_fixtures_are_canonical_and_idempotent() -> None:
    fixture_root = Path(__file__).parents[1] / "docs/testing/fixtures"
    paths = sorted(fixture_root.glob("browser-rendered-capture-result-v1.*.json"))

    assert [path.stem.rsplit(".", 1)[-1] for path in paths] == [
        "blocked",
        "cancelled",
        "empty_content",
        "missing_runtime",
        "redirect",
        "success",
        "timeout",
    ]
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        result = CaptureResult.model_validate_json(raw)
        canonical = (
            json.dumps(
                result.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        )
        assert raw == canonical
        assert json.loads(canonical) == payload
