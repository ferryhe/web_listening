from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import pytest
import yaml


pytestmark = [pytest.mark.live, pytest.mark.catalog_canary]
ROOT = Path(__file__).parents[2]


def _host_is_allowed(host: str, allowed_domains: list[str]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in allowed_domains)


def _observe_canary(
    url: str,
    allowed_domains: list[str],
    *,
    browser_user_agent: bool,
    client: httpx.Client | None = None,
) -> str:
    headers = {"User-Agent": "Mozilla/5.0"} if browser_user_agent else {}
    active_client = client or httpx.Client()
    current = url
    response = None
    try:
        for _ in range(6):
            parsed = urlsplit(current)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or not _host_is_allowed(parsed.hostname, allowed_domains)
            ):
                return "unsafe_redirect"
            response = active_client.get(
                current, timeout=30, follow_redirects=False, headers=headers
            )
            if not response.is_redirect:
                break
            location = response.headers.get("Location")
            if not location:
                return "http_error"
            current = urljoin(str(response.url), location)
        else:
            return "http_error"
    except httpx.HTTPError:
        return "request_error"
    finally:
        if client is None:
            active_client.close()
    assert response is not None
    if response.is_success:
        return "pass_http"
    if response.status_code in {401, 403}:
        return "known_blocked"
    return "http_error"


def test_canary_does_not_request_unsafe_redirect_target() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/steal"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observed = _observe_canary(
            "https://example.com/start", ["example.com"], browser_user_agent=False, client=client
        )
    assert observed == "unsafe_redirect"
    assert requested == ["https://example.com/start"]


def test_canary_follows_same_domain_relative_redirect() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        observed = _observe_canary(
            "https://example.com/start", ["example.com"], browser_user_agent=False, client=client
        )
    assert observed == "pass_http"
    assert requested == ["https://example.com/start", "https://example.com/final"]


@pytest.mark.parametrize(
    ("host", "allowed", "accepted"),
    [
        ("example.com", ["example.com"], True),
        ("www.example.com", ["example.com"], True),
        ("example.com.evil.test", ["example.com"], False),
    ],
)
def test_host_allowlist_is_boundary_aware(host: str, allowed: list[str], accepted: bool) -> None:
    assert _host_is_allowed(host, allowed) is accepted


@pytest.mark.skipif(os.getenv("WEB_LISTENING_CATALOG_CANARY") != "1", reason="catalog canary is opt-in")
def test_selected_catalog_canaries_report_configured_classification() -> None:
    selected = os.getenv("WEB_LISTENING_CATALOG_CANARY_SITE")
    keys = [selected] if selected else [item["site_key"] for name in ("dev_test_sites.json", "smoke_site_catalog.json")
            for item in json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))]
    for key in keys:
        package = ROOT / "web_listening/skills/sites" / str(key) / "1.0.0"
        assert package.is_dir(), f"unknown catalog canary site: {key}"
        profile = yaml.safe_load((package / "profiles/default.yaml").read_text(encoding="utf-8"))
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        catalog = profile["adapters"][0]["config"]
        expected = manifest["metadata"]["canary_outcome"]
        observed = _observe_canary(
            catalog["monitor_url"],
            profile["safety"]["allowed_domains"],
            browser_user_agent=catalog["fetch_config_json"].get("user_agent_profile") == "browser",
        )
        if expected.startswith("pass_http"):
            assert observed == "pass_http", f"{key}: expected {expected}, observed {observed}"
        elif expected == "known_blocked":
            assert observed in {"known_blocked", "unsafe_redirect"}, (
                f"{key}: expected {expected}, observed {observed}"
            )
        elif expected in {"ssl_issue", "broken_upstream"}:
            assert observed in {"request_error", "http_error"}, (
                f"{key}: expected {expected}, observed {observed}"
            )
        else:
            pytest.fail(f"{key}: unsupported canary classification {expected!r}")
