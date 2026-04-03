import importlib.util
from pathlib import Path
import sys

from web_listening.blocks.rescue import RescueAttempt, RescueResult


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "run_smoke_site_catalog.py"
SPEC = importlib.util.spec_from_file_location("run_smoke_site_catalog", MODULE_PATH)
smoke_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = smoke_module
SPEC.loader.exec_module(smoke_module)


def make_entry(smoke_expectation: str = "pass_http") -> dict:
    return {
        "site_key": "demo",
        "abbreviation": "DEMO",
        "full_name": "Demo Site",
        "smoke_required": True,
        "smoke_expectation": smoke_expectation,
        "monitor_url": "https://example.com",
        "homepage_url": "https://example.com",
        "fetch_mode": "http",
        "fetch_config_json": {},
        "expected_min_words": 100,
        "js_heavy_candidate": False,
        "notes": "",
    }


def test_run_smoke_marks_browser_rescue_as_pass(monkeypatch):
    def fake_run_rescue_candidates(**kwargs):
        return RescueResult(
            label="DEMO",
            primary_strategy="catalog",
            resolved_strategy="browser",
            resolved=True,
            attempts=[
                RescueAttempt(
                    strategy="catalog",
                    url="https://example.com",
                    fetch_mode="http",
                    status_code=403,
                    final_url="https://example.com",
                    request_user_agent="web-listening-bot/1.0",
                    word_count=0,
                    link_count=0,
                    source_kind="html",
                    passed=False,
                    reason="http_403",
                    head="",
                    error="HTTPStatusError: 403",
                ),
                RescueAttempt(
                    strategy="browser",
                    url="https://example.com",
                    fetch_mode="browser",
                    status_code=200,
                    final_url="https://example.com/app",
                    request_user_agent="Mozilla/5.0",
                    word_count=220,
                    link_count=12,
                    source_kind="html",
                    passed=True,
                    reason="content_ok",
                    head="# Demo",
                ),
            ],
        )

    monkeypatch.setattr(smoke_module, "run_rescue_candidates", fake_run_rescue_candidates)

    results = smoke_module.run_smoke([make_entry()])

    assert len(results) == 1
    result = results[0]
    assert result.passed is True
    assert result.resolved is True
    assert result.rescue_used is True
    assert result.resolved_strategy == "browser"
    assert result.outcome == "rescued_browser"
    assert result.fetch_mode == "browser"


def test_run_smoke_allows_limited_catalog_result(monkeypatch):
    def fake_run_rescue_candidates(**kwargs):
        return RescueResult(
            label="DEMO",
            primary_strategy="catalog",
            resolved_strategy="",
            resolved=False,
            attempts=[
                RescueAttempt(
                    strategy="catalog",
                    url="https://example.com",
                    fetch_mode="http",
                    status_code=200,
                    final_url="https://example.com",
                    request_user_agent="web-listening-bot/1.0",
                    word_count=24,
                    link_count=3,
                    source_kind="html",
                    passed=False,
                    reason="too_little_content",
                    head="# Demo",
                )
            ],
        )

    monkeypatch.setattr(smoke_module, "run_rescue_candidates", fake_run_rescue_candidates)

    results = smoke_module.run_smoke([make_entry(smoke_expectation="pass_http_limited")])

    assert len(results) == 1
    result = results[0]
    assert result.passed is True
    assert result.resolved is True
    assert result.resolved_strategy == "catalog"
    assert result.outcome == "limited"
    assert result.fetch_mode == "http"
