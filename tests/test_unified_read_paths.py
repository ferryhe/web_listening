from __future__ import annotations

import hashlib
import inspect
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from web_listening.blocks.access_gateway import AccessGateway, AccessGatewayConfig
from web_listening.blocks.governed_read import (
    AccessRejectedError,
    GovernedReadGateway,
    access_rejection_payload,
    build_runtime_read_gateway,
)
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    RawHttpResponse,
    normalize_http_url,
)
from web_listening.contracts.site_diagnostic import DiagnosticIdentity, canonical_sha256


def _identity() -> DiagnosticIdentity:
    visible = {
        "identity_id": "issue-51-test",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/2.0",
    }
    return DiagnosticIdentity(
        **visible,
        identity_sha256=canonical_sha256(visible),
    )


class _Transport:
    def __init__(self, responses: dict[str, tuple[int, bytes, dict[str, str]]]):
        self.responses = responses
        self.requests: list[str] = []

    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
    ) -> RawHttpResponse:
        del user_agent, identity_sha256
        self.requests.append(url)
        status, body, headers = self.responses[url]
        return RawHttpResponse(status, headers, [body])


def _reader(transport: _Transport, *origins: str) -> GovernedReadGateway:
    normalized_origins = frozenset(normalize_http_url(origin)[1] for origin in origins)
    gateway = AccessGateway(
        AccessGatewayConfig(
            identity=_identity(),
            allowed_origins=normalized_origins,
            diagnostic_artifact_sha256="a" * 64,
            pacing_interval=__import__("datetime").timedelta(0),
        ),
        transport=transport,
    )
    return GovernedReadGateway(gateway, max_body_bytes=1024 * 1024)


def test_html_and_sitemap_reads_use_one_gateway_with_manual_cross_origin_redirects() -> (
    None
):
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://cdn.example/robots.txt": (404, b"", {}),
            "https://example.com/sitemap.xml": (
                302,
                b"",
                {"location": "https://cdn.example/site.xml"},
            ),
            "https://cdn.example/site.xml": (
                200,
                b"<urlset><url><loc>https://example.com/a</loc></url></urlset>",
                {"content-type": "application/xml"},
            ),
        }
    )
    reader = _reader(transport, "https://example.com", "https://cdn.example")

    result = reader.read("https://example.com/sitemap.xml")

    assert result.final_url == "https://cdn.example/site.xml"
    assert result.status_code == 200
    assert result.body.startswith(b"<urlset>")
    assert result.access_decision.reason_code == "robots.absent"
    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/sitemap.xml",
        "https://cdn.example/robots.txt",
        "https://cdn.example/site.xml",
    ]


def test_bounded_body_uses_final_url_when_gzip_suffix_disappears_on_redirect() -> None:
    body = b"<urlset/>"
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://cdn.example/robots.txt": (404, b"", {}),
            "https://example.com/sitemap.xml.gz": (
                302,
                b"",
                {"location": "https://cdn.example/sitemap.xml"},
            ),
            "https://cdn.example/sitemap.xml": (
                200,
                body,
                {"content-type": "application/xml"},
            ),
        }
    )

    result = _reader(
        transport,
        "https://example.com",
        "https://cdn.example",
    ).read("https://example.com/sitemap.xml.gz")

    assert result.final_url == "https://cdn.example/sitemap.xml"
    assert result.body == body
    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/sitemap.xml.gz",
        "https://cdn.example/robots.txt",
        "https://cdn.example/sitemap.xml",
    ]


def test_bounded_body_uses_final_url_when_gzip_suffix_appears_on_redirect() -> None:
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://cdn.example/robots.txt": (404, b"", {}),
            "https://example.com/sitemap.xml": (
                302,
                b"",
                {"location": "https://cdn.example/sitemap.xml.gz"},
            ),
            "https://cdn.example/sitemap.xml.gz": (
                200,
                b"<urlset/>",
                {"content-type": "application/xml"},
            ),
        }
    )
    reader = _reader(
        transport,
        "https://example.com",
        "https://cdn.example",
    )

    with pytest.raises(BodyFailure, match="gzip_signal_mismatch"):
        reader.read("https://example.com/sitemap.xml")

    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/sitemap.xml",
        "https://cdn.example/robots.txt",
        "https://cdn.example/sitemap.xml.gz",
    ]


def test_robots_rejection_has_frozen_envelope_and_zero_target_consume() -> None:
    transport = _Transport({"https://example.com/robots.txt": (403, b"", {})})
    reader = _reader(transport, "https://example.com")

    with pytest.raises(AccessRejectedError) as raised:
        reader.read("https://example.com/private/report.pdf")

    payload = access_rejection_payload(raised.value)
    assert payload["schema_version"] == "access-rejection-error.v1"
    assert payload["reason_code"] == "robots.forbidden"
    assert payload["outcome"] == "reject"
    assert transport.requests == ["https://example.com/robots.txt"]


def test_runtime_read_gateway_preserves_access_gateway_default_pacing() -> None:
    reader = build_runtime_read_gateway(
        authority_sha256="b" * 64,
        seed_urls=("https://example.com/",),
        allowed_domains=("example.com",),
        user_agent="web-listening-bot/2.0",
        max_body_bytes=1024,
        timeout_seconds=30.0,
        budget_limit=2,
    )

    assert reader.gateway.config.pacing_interval == timedelta(seconds=1)


def test_document_rejection_occurs_before_temp_or_blob_write(
    tmp_path, monkeypatch
) -> None:
    from web_listening.blocks.document import DocumentProcessor

    transport = _Transport({"https://example.com/robots.txt": (403, b"", {})})
    reader = _reader(transport, "https://example.com")
    monkeypatch.setattr(
        "web_listening.blocks.document.settings.downloads_dir", tmp_path
    )
    processor = DocumentProcessor(read_gateway=reader)

    with pytest.raises(AccessRejectedError):
        processor.download(
            "https://example.com/private/report.pdf",
            institution="Example",
        )

    assert not list(tmp_path.iterdir())


def test_supported_target_readers_have_no_direct_http_or_browser_navigation() -> None:
    root = Path(__file__).parents[1]
    supported = [
        root / "web_listening/blocks/crawler.py",
        root / "web_listening/blocks/document.py",
        root / "web_listening/blocks/acquisition_capture.py",
        root / "web_listening/blocks/acquisition_fallback.py",
        root / "web_listening/blocks/acquisition_gateway.py",
        root / "web_listening/blocks/rescue.py",
        root / "web_listening/blocks/staged_workflow.py",
        root / "web_listening/blocks/tree_bootstrap_workflow.py",
        root / "web_listening/blocks/tree_crawler.py",
        root / "web_listening/blocks/tree_run_workflow.py",
        root / "web_listening/executors/http_wrapper.py",
        root / "web_listening/executors/browseract.py",
        root / "web_listening/executors/browseract_wrapper.py",
        root / "web_listening/executors/playwright_wrapper.py",
        root / "web_listening/executors/cloakbrowser_wrapper.py",
        root / "web_listening/cli.py",
        root / "web_listening/api/routes.py",
        root / "web_listening/mcp/tools.py",
    ]
    forbidden = (
        "import httpx",
        "follow_redirects=True",
        ".goto(",
        ".client.get(",
        ".client.stream(",
    )

    findings = {
        str(path.relative_to(root)): marker
        for path in supported
        for marker in forbidden
        if marker in path.read_text(encoding="utf-8")
    }

    assert findings == {}

    browseract_wrapper = (
        root / "web_listening/executors/browseract_wrapper.py"
    ).read_text(encoding="utf-8")
    assert '"browser", "open"' not in browseract_wrapper
    assert '"stealth-extract", url' not in browseract_wrapper


def test_cli_api_and_mcp_share_the_exact_frozen_rejection_payload() -> None:
    from web_listening import cli
    from web_listening.api import routes
    from web_listening.mcp import tools

    transport = _Transport({"https://example.com/robots.txt": (403, b"", {})})
    with pytest.raises(AccessRejectedError) as raised:
        _reader(transport, "https://example.com").read("https://example.com/private")

    expected = access_rejection_payload(raised.value)
    assert cli.access_rejection_payload(raised.value) == expected
    assert routes.access_rejection_payload(raised.value) == expected
    assert tools.access_rejection_payload(raised.value) == expected
    assert expected["reason_code"] == "robots.forbidden"


def test_cli_api_and_mcp_return_the_same_runtime_rejection_envelope(
    tmp_path,
    monkeypatch,
) -> None:
    from web_listening.api.app import create_app
    from web_listening.cli import app
    from web_listening.mcp.tools import web_listening_bootstrap_scope

    transport = _Transport({"https://example.com/robots.txt": (403, b"", {})})
    with pytest.raises(AccessRejectedError) as raised:
        _reader(transport, "https://example.com").read("https://example.com/private")
    rejection = raised.value
    expected = access_rejection_payload(rejection)
    scope_path = tmp_path / "scope.yaml"
    profile_path = tmp_path / "profile.yaml"
    scope_path.write_text("scope", encoding="utf-8")
    profile_path.write_text("profile", encoding="utf-8")

    monkeypatch.setattr(
        "web_listening.blocks.monitor_scope_planner.load_monitor_scope_plan",
        lambda path: SimpleNamespace(scope_id=1),
    )
    monkeypatch.setattr(
        "web_listening.blocks.staged_workflow.bootstrap_scope",
        lambda **kwargs: (_ for _ in ()).throw(rejection),
    )
    monkeypatch.setattr(
        "web_listening.blocks.staged_workflow.prepare_scope_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(rejection),
    )
    cli = CliRunner().invoke(
        app,
        [
            "bootstrap-scope",
            "--scope-path",
            str(scope_path),
            "--acquisition-profile-path",
            str(profile_path),
            "--json",
        ],
    )

    monkeypatch.setattr(
        "web_listening.api.routes.execute_job",
        lambda **kwargs: (_ for _ in ()).throw(rejection),
    )
    monkeypatch.setattr(
        "web_listening.api.routes._prepare_scope_execution_authority",
        lambda *args, **kwargs: (_ for _ in ()).throw(rejection),
    )
    monkeypatch.setattr(
        "web_listening.blocks.staged_workflow.prepare_scope_execution",
        lambda *args, **kwargs: (_ for _ in ()).throw(rejection),
    )
    api = TestClient(create_app()).post(
        "/api/v1/monitor-scopes/1/bootstrap",
        json={
            "scope_path": str(scope_path),
            "acquisition_profile_path": str(profile_path),
        },
    )
    mcp = web_listening_bootstrap_scope(
        str(scope_path),
        acquisition_profile_path=str(profile_path),
    )

    assert cli.exit_code == 1
    assert __import__("json").loads(cli.stdout) == expected
    assert api.status_code == 403
    assert api.json() == expected
    assert mcp == expected


def test_gateway_document_bytes_retain_sha256_without_a_second_read() -> None:
    body = b"%PDF-1.7\nissue-51"
    transport = _Transport(
        {
            "https://example.com/robots.txt": (404, b"", {}),
            "https://example.com/report.pdf": (
                200,
                body,
                {"content-type": "application/pdf"},
            ),
        }
    )

    result = _reader(transport, "https://example.com").read(
        "https://example.com/report.pdf"
    )

    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert transport.requests.count("https://example.com/report.pdf") == 1


@pytest.mark.parametrize(
    ("module_name", "function_name", "kwargs"),
    [
        (
            "web_listening.blocks.tree_bootstrap_workflow",
            "run_bootstrap",
            {
                "catalog": "example",
                "max_depth": 1,
                "max_pages": 1,
                "max_files": 0,
                "targets": [],
            },
        ),
        (
            "web_listening.blocks.tree_run_workflow",
            "run_incremental",
            {
                "catalog": "example",
                "max_depth": 1,
                "max_pages": 1,
                "max_files": 0,
            },
        ),
    ],
)
def test_legacy_tree_entrypoints_reject_before_storage(
    monkeypatch,
    module_name: str,
    function_name: str,
    kwargs: dict[str, object],
) -> None:
    module = __import__(module_name, fromlist=[function_name])
    storage_calls: list[object] = []

    def forbidden_storage(*args, **inner_kwargs):
        storage_calls.append((args, inner_kwargs))
        raise AssertionError("Storage must not be constructed")

    monkeypatch.setattr(module, "Storage", forbidden_storage)

    with pytest.raises(
        ValueError, match="governed acquisition gateway|PreparedScopeExecution"
    ):
        getattr(module, function_name)(**kwargs)

    assert storage_calls == []


def test_api_and_mcp_require_explicit_formal_execution_authority() -> None:
    from pydantic import ValidationError

    from web_listening.api.routes import BootstrapScopeRequest, RunScopeRequest
    from web_listening.mcp.tools import (
        web_listening_bootstrap_scope,
        web_listening_run_scope,
    )

    for model in (BootstrapScopeRequest, RunScopeRequest):
        with pytest.raises(ValidationError):
            model()
        parsed = model(
            scope_path="plans/scope.yaml",
            acquisition_profile_path="profiles/http.yaml",
        )
        assert parsed.scope_path == "plans/scope.yaml"
        assert parsed.acquisition_profile_path == "profiles/http.yaml"

    for function in (web_listening_bootstrap_scope, web_listening_run_scope):
        parameter = inspect.signature(function).parameters["acquisition_profile_path"]
        assert parameter.default is inspect.Parameter.empty


def test_authorized_live_canary_is_explicit_and_offline_by_default() -> None:
    canary = Path(__file__).parent / "live" / "test_authorized_access_gateway_canary.py"
    source = canary.read_text(encoding="utf-8")

    assert "WEB_LISTENING_AUTHORIZED_CANARY_URL" in source
    assert "WEB_LISTENING_AUTHORIZED_CANARY_WINDOW" in source
    assert "WEB_LISTENING_RUN_AUTHORIZED_CANARY" in source
    assert "pytest.skip" in source
    assert '"target"' in source
    assert '"observed_at"' in source
    assert '"result"' in source
    assert "TOKEN" not in source
    assert "PASSWORD" not in source


def test_legacy_cli_and_api_target_reads_are_disabled_before_storage(
    monkeypatch,
) -> None:
    from web_listening.api.app import create_app
    from web_listening.cli import app

    def forbidden_storage(*args, **kwargs):
        raise AssertionError("disabled target-read entrypoint reached Storage")

    monkeypatch.setattr("web_listening.cli._get_storage", forbidden_storage)
    runner = CliRunner()
    cli_results = (
        runner.invoke(app, ["check"]),
        runner.invoke(
            app,
            ["download-docs", "--site-id", "1", "--institution", "Example"],
        ),
    )
    assert all(result.exit_code != 0 for result in cli_results)
    assert all("governed" in result.output for result in cli_results)

    monkeypatch.setattr("web_listening.api.routes.get_storage", forbidden_storage)
    client = TestClient(create_app())
    api_results = (
        client.post("/api/v1/sites/1/check"),
        client.post(
            "/api/v1/sites/1/download-docs",
            json={"institution": "Example"},
        ),
        client.post("/api/v1/sites/1/rescue-check", json={}),
    )
    assert [response.status_code for response in api_results] == [409, 409, 409]
    assert all("governed" in response.json()["detail"] for response in api_results)
