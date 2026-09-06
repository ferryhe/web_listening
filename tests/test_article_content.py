from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.test_acquisition_fallback import FakeAdapter, make_fetch_result, make_profile
from web_listening.blocks import article_content as article
from web_listening.blocks.acquisition_profile import AcquisitionQualityGates

URL = "https://example.com/page"
BODY = "<html><body>one two three four</body></html>"


@pytest.fixture
def output(tmp_path, monkeypatch):
    monkeypatch.setattr(article.settings, "data_dir", tmp_path)
    return tmp_path / "article_content"


def reader(name, body=BODY, status=200):
    result = make_fetch_result(text=body, status_code=status, final_url=URL)
    result.raw_html = body
    return FakeAdapter(name, result)


def run(output, readers, **kwargs):
    return article._fetch_with_readers(
        URL,
        profile=make_profile(
            quality_gates=AcquisitionQualityGates(min_words=1, min_links=0)
        ),
        readers=readers,
        output_dir=output,
        site_key="example",
        **kwargs,
    )


def test_no_reviewed_profile():
    result = article.fetch_article_content(URL)
    assert result.data_status == "permission_denied"
    assert result.stop_reason == "no_reviewed_profile"


def test_f1_http_success(output):
    http, browser = reader("web_http"), reader("browser_rendered")
    result = run(output, {"web_http": http, "browser_rendered": browser})
    assert result.has_data and result.data["selected_method"] == "web_http"
    assert result.data["full_text"] == BODY
    assert result.data["sha256"] == hashlib.sha256(BODY.encode()).hexdigest()
    assert (output / result.data["content_ref"]).read_bytes() == BODY.encode()
    assert len(http.calls) == 1 and not browser.calls
    assert result.data["attempts"] == result.attempts


@pytest.mark.parametrize(
    "body,status,expected,retryable",
    [
        ("", 200, "failed_quality_gate", False),
        ("unavailable", 503, "error", True),
        ("captcha", 403, "blocked", False),
    ],
)
def test_f2_f3_fallback(output, body, status, expected, retryable):
    http, browser = reader("web_http", body, status), reader("browser_rendered")
    result = run(output, {"web_http": http, "browser_rendered": browser})
    assert result.data["selected_method"] == "browser_rendered"
    assert result.attempts[0]["data_status"] == expected
    assert result.attempts[0]["retryable"] == retryable
    assert len(http.calls) == len(browser.calls) == 1


def test_f4_f5_exhausted(output):
    result = run(
        output,
        {
            "web_http": reader("web_http", ""),
            "browser_rendered": reader("browser_rendered", "captcha"),
        },
    )
    assert result.data_status == "no_content"
    assert result.ok and result.error is None and not result.has_data
    assert result.stop_reason == "no_usable_content"
    assert len(result.attempts) == 3
    assert result.attempts[-1]["data_status"] == "not_applicable"
    assert result.attempts[-1]["reason"] == "cloakbrowser_unavailable"


@pytest.mark.parametrize(
    "exception,retryable",
    [(RuntimeError("secret"), False), (TimeoutError("secret"), True)],
)
def test_f6_runtime_exception(output, exception, retryable):
    result = run(
        output,
        {
            "web_http": FakeAdapter("web_http", exception),
            "browser_rendered": reader("browser_rendered"),
        },
    )
    assert result.has_data
    assert result.attempts[0]["data_status"] == "error"
    assert result.attempts[0]["retryable"] == retryable
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize("limit", [0, 5, len(BODY), 2000])
def test_f7_ac15_preview(output, limit):
    result = run(output, {"web_http": reader("web_http")}, inline_content_limit=limit)
    assert result.data["truncated"] == (len(BODY.encode()) > limit)
    if result.data["truncated"]:
        assert "full_text" not in result.data
        assert result.data["truncated_preview"] == BODY[:limit]
    else:
        assert result.data["full_text"] == BODY
    assert (
        hashlib.sha256((output / result.data["content_ref"]).read_bytes()).hexdigest()
        == result.data["sha256"]
    )


def test_f8_reuse(output):
    first = run(output, {"web_http": reader("web_http")})
    http = reader("web_http")
    result = run(output, {"web_http": http}, prior_attempts=[first.data])
    assert result.has_data and not http.calls
    assert result.attempts[0]["skipped"]
    assert result.attempts[0]["reason"] == "in_process_evidence_reused"


def test_reuse_redirected_attempt(output):
    page = make_fetch_result(text=BODY, status_code=200, final_url=URL + "/redirected")
    page.raw_html = BODY
    first = run(output, {"web_http": FakeAdapter("web_http", page)})
    http = reader("web_http")
    second = run(output, {"web_http": http}, prior_attempts=first.attempts)
    assert second.has_data and not http.calls
    assert second.data["requested_url"] == URL
    assert second.data["final_url"] == URL + "/redirected"
    assert second.data["sha256"] == first.data["sha256"]
    assert second.attempts[0]["reason"] == "in_process_evidence_reused"


@pytest.mark.parametrize(
    "missing", [("sha256",), ("content_ref",), ("sha256", "content_ref")]
)
def test_partial_prior_attempt_invokes_reader(output, missing):
    first = run(output, {"web_http": reader("web_http")})
    evidence = {
        key: value for key, value in first.attempts[0].items() if key not in missing
    }
    http = reader("web_http")
    second = run(output, {"web_http": http}, prior_attempts=[evidence])
    assert second.has_data and http.calls == [{"url": URL, "config": None}]
    assert not second.attempts[0]["skipped"]


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "auth_required"),
        (404, "not_found"),
        (410, "not_found"),
        (403, "permission_denied"),
    ],
)
def test_f9_terminal(output, status, expected):
    browser = reader("browser_rendered")
    result = run(
        output,
        {"web_http": reader("web_http", BODY, status), "browser_rendered": browser},
    )
    assert result.data_status == expected
    assert result.ok and result.error is None and not result.has_data
    assert not browser.calls


def test_f10_containment(output):
    http = reader("web_http")
    with pytest.raises(ValueError, match="output_dir"):
        run(output.parent.parent / "outside", {"web_http": http})
    assert not http.calls


def test_symlink_parent_rejected(output):
    output.symlink_to(output.parent, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        run(output, {"web_http": reader("web_http")})


@pytest.mark.parametrize(
    "corruption,code",
    [("delete", "content_ref_corrupt"), ("replace", "content_ref_hash_mismatch")],
)
def test_ac15_corruption(output, monkeypatch, corruption, code):
    original = article.write_file

    def corrupt(*args, **kwargs):
        name = original(*args, **kwargs)
        path = output / name
        if corruption == "delete":
            path.unlink()
        else:
            path.write_text("corrupt")
        return name

    monkeypatch.setattr(article, "write_file", corrupt)
    result = run(output, {"web_http": reader("web_http")})
    assert result.error.code == code and not result.has_data


def test_write_failure_inline_or_error(output, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("sensitive path")

    monkeypatch.setattr(article, "write_file", fail)
    inline = run(output, {"web_http": reader("web_http")})
    assert inline.has_data and inline.data["full_text"] == BODY
    assert "content_ref" not in inline.data
    large = run(output, {"web_http": reader("web_http")}, inline_content_limit=1)
    assert large.error.code == "artifact_write_failed" and not large.error.retryable


def test_ac16_fixture(output):
    contract = json.loads(
        (Path(__file__).parent / "fixtures/article_content/climate_92.json").read_text()
    )
    result = run(output, {"web_http": reader("web_http")}, inline_content_limit=5)
    assert set(contract["required_data_keys"]) <= result.data.keys()
    assert set(contract["required_attempt_keys"]) <= result.attempts[0].keys()


def test_scoped_public_entrypoint_uses_compiler(output, monkeypatch):
    from dataclasses import replace

    from tests.test_governed_browser_reader import _mixed_gateway_plan
    from tests.test_prepared_scope_execution import _scope_plan
    from web_listening.blocks import staged_workflow
    from web_listening.blocks.acquisition_gateway import GovernedAcquisitionGateway
    from web_listening.blocks.monitor_scope_planner import render_yaml_text
    from web_listening.contracts import CaptureContent, CaptureResult
    from web_listening.executors.registry import (
        ExecutorRegistry,
        default_preview_registry,
    )

    class Executor:
        executor_id = "web_http"
        calls = 0

        def execute(self, request):
            self.calls += 1
            return CaptureResult(
                **request.model_dump(
                    include={
                        "request_id",
                        "site_key",
                        "site_skill_id",
                        "site_skill_version",
                        "site_skill_digest",
                        "recipe_id",
                        "run_id",
                        "scope_id",
                        "executor_id",
                    }
                ),
                state="succeeded",
                started_at=request.requested_at,
                finished_at=request.requested_at,
                final_url=URL,
                status_code=200,
                content=CaptureContent(
                    text=BODY,
                    media_type="text/html",
                    sha256=hashlib.sha256(BODY.encode()).hexdigest(),
                ),
            )

    executor = Executor()
    plan = _mixed_gateway_plan(
        http_timeout=5, browser_timeout=5, http_bytes=4096, browser_bytes=4096
    )
    plan = replace(plan, steps=(plan.steps[0],))
    gateway = GovernedAcquisitionGateway(
        plan,
        ExecutorRegistry(
            {"web_http": executor},
            metadata={"web_http": default_preview_registry().metadata["web_http"]},
        ),
    )
    scope = output.parent / "scope.yaml"
    scope.write_text(render_yaml_text(_scope_plan(seed_url=URL)))
    calls = []

    def compile(plan, **kwargs):
        calls.append((plan, kwargs))
        return gateway

    monkeypatch.setattr(staged_workflow, "_compile_acquisition_gateway", compile)
    result = article.fetch_article_content(
        URL,
        profile=make_profile(
            quality_gates=AcquisitionQualityGates(min_words=1, min_links=0)
        ),
        scope_path=scope,
        output_dir=output,
        quality_gates={"min_words": 1, "min_links": 0},
    )
    assert result.has_data and executor.calls == 1 and len(calls) == 1
    assert gateway._closed


def test_public_wrapper_parity_and_cli_validation(output):
    from typer.testing import CliRunner

    from web_listening.cli import app
    from web_listening.mcp.tools import web_listening_fetch_article_content

    expected = article.fetch_article_content(URL).model_dump(mode="json")
    assert web_listening_fetch_article_content(URL) == expected
    result = CliRunner().invoke(app, ["fetch-article-content", "--url", URL])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == expected
    invalid = CliRunner().invoke(
        app, ["fetch-article-content", "--url", "https://user:secret@example.com/"]
    )
    assert invalid.exit_code == 2 and "secret" not in invalid.stdout


@pytest.mark.parametrize(
    "scenario,status,method,reads",
    [
        ("success", "present", "web_http", 1),
        ("fallback", "present", "browser_rendered", 2),
        ("all_empty", "no_content", None, 2),
        ("unrenderable", "no_content", None, 2),
        ("not_found", "not_found", None, 1),
    ],
)
def test_ac13_compiled_loopback(output, scenario, status, method, reads):
    from tests.fixtures.article_content.loopback import (
        BODY as SERVED,
    )
    from tests.fixtures.article_content.loopback import (
        loopback_authority,
    )

    with loopback_authority(output.parent / "fixture", scenario) as (kwargs, seen):
        result = article.fetch_article_content(**kwargs, inline_content_limit=100_000)
        assert result.data_status == status
        assert result.data["selected_method"] == method
        assert seen.count("/news") == reads
        if method:
            assert result.data["full_text"] == SERVED
            assert result.data["sha256"] == hashlib.sha256(SERVED.encode()).hexdigest()
        if scenario == "unrenderable":
            assert result.attempts[0]["data_status"] == "failed_quality_gate"


def test_ac14_compiled_loopback_wrapper_parity(output):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from tests.fixtures.article_content.loopback import loopback_authority
    from web_listening.api.app import create_app
    from web_listening.cli import app
    from web_listening.mcp.tools import web_listening_fetch_article_content

    with loopback_authority(output.parent / "fixture") as (kwargs, _):
        expected = article.fetch_article_content(**kwargs).model_dump(mode="json")
        assert web_listening_fetch_article_content(**kwargs) == expected
        args = ["fetch-article-content"]
        for key, value in kwargs.items():
            args.extend(["--" + key.replace("_", "-"), value])
        cli = CliRunner().invoke(app, args)
        assert cli.exit_code == 0, cli.stdout
        assert json.loads(cli.stdout) == expected
        response = TestClient(create_app()).post(
            "/api/v1/acquisition/article-content/fetch", json=kwargs
        )
        assert response.status_code == 200
        assert response.json() == expected


def test_reuse_rechecks_current_quality(output):
    first = run(output, {"web_http": reader("web_http")})
    http = reader("web_http")
    result = run(
        output,
        {"web_http": http},
        prior_attempts=[first.data],
        quality_gates={"min_words": 500, "min_links": 0},
    )
    assert not result.has_data
    assert not http.calls
    assert result.attempts[0]["skipped"]
    assert result.attempts[0]["data_status"] == "failed_quality_gate"


def test_no_profile_sensitive_url_is_not_echoed():
    from web_listening.mcp.tools import web_listening_fetch_article_content

    result = web_listening_fetch_article_content(
        "https://example.com/?api_key=secret-value"
    )
    assert "secret-value" not in json.dumps(result)
    assert result["error"]["code"] == "invalid_acquisition_request"


def test_optional_cloak_profile_does_not_prevent_http(output):
    from tests.fixtures.article_content.loopback import loopback_authority
    from web_listening.blocks.acquisition_profile import (
        AcquisitionAdapterConfig,
        AcquisitionRecipeMapping,
        AcquisitionSafetyPolicy,
        load_acquisition_profile,
    )

    with loopback_authority(output.parent / "fixture") as (kwargs, _):
        profile = load_acquisition_profile(kwargs.pop("profile_path"))
        profile = profile.model_copy(
            update={
                "fallback_order": ["browser_rendered", "cloakbrowser"],
                "adapters": profile.adapters
                + [AcquisitionAdapterConfig(adapter="cloakbrowser")],
                "recipe_mappings": profile.recipe_mappings
                + [
                    AcquisitionRecipeMapping(
                        adapter="cloakbrowser", recipe_id="optional-cloak"
                    )
                ],
                "safety": AcquisitionSafetyPolicy(
                    allowed_domains=["example.com"],
                    allow_stealth_browser=True,
                    require_authorized_access=True,
                ),
            }
        )
        result = article.fetch_article_content(**kwargs, profile=profile)
        assert result.has_data and result.data["selected_method"] == "web_http"


def test_attempt_list_carries_reusable_evidence(output):
    first = run(output, {"web_http": reader("web_http")})
    http = reader("web_http")
    second = run(output, {"web_http": http}, prior_attempts=first.attempts)
    assert second.has_data and not http.calls
    assert second.attempts[0]["reason"] == "in_process_evidence_reused"


def test_disabled_reader_is_recorded_without_invocation(output):
    from web_listening.blocks.acquisition_profile import AcquisitionAdapterConfig

    http, browser = reader("web_http"), reader("browser_rendered")
    profile = make_profile(
        quality_gates=AcquisitionQualityGates(min_words=1, min_links=0),
        adapters=[
            AcquisitionAdapterConfig(adapter="web_http", enabled=False),
            AcquisitionAdapterConfig(adapter="browser_rendered"),
        ],
    )
    result = article._fetch_with_readers(
        URL,
        profile=profile,
        readers={"web_http": http, "browser_rendered": browser},
        output_dir=output,
    )
    assert result.data["selected_method"] == "browser_rendered" and not http.calls
    assert result.attempts[0]["data_status"] == "not_applicable"
    assert result.attempts[0]["reason"] == "reader_disabled"


def test_shared_interstitial_markers_trigger_fallback(output):
    result = run(
        output,
        {
            "web_http": reader("web_http", "Just a moment"),
            "browser_rendered": reader("browser_rendered"),
        },
    )
    assert result.data["selected_method"] == "browser_rendered"
    assert result.attempts[0]["data_status"] == "blocked"


def test_structured_interaction_requirement_is_terminal(output):
    browser = reader("browser_rendered")
    result = run(
        output,
        {
            "web_http": FakeAdapter(
                "web_http", article._ReaderFailure("interaction_required")
            ),
            "browser_rendered": browser,
        },
    )
    assert result.data_status == "interaction_required" and not browser.calls


def test_extraction_metadata_title(output):
    body = "<html><head><title>Official announcement</title></head><body>one two three four</body></html>"
    result = run(output, {"web_http": reader("web_http", body)})
    assert result.data["extraction_metadata"]["page_title"] == "Official announcement"


def test_extraction_document_count_drives_quality_gate(output):
    body = (
        '<html><body>one two three four <a href="/report.pdf">Report</a></body></html>'
    )
    result = run(
        output,
        {"web_http": reader("web_http", body)},
        quality_gates={"min_words": 1, "min_links": 1, "min_document_links": 1},
    )
    assert result.has_data
    assert result.data["extraction_metadata"]["document_link_count"] == 1
    assert result.data["extraction_metadata"]["link_count"] == 1
