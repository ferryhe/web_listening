"""Controlled HTTP fixture. Only this transport opens sockets, to 127.0.0.1.

The reviewed example.com identity is mapped to the loopback server by an
injected test transport. Production URL/SSRF policy is never relaxed.
"""

from __future__ import annotations

import http.client
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import yaml

from web_listening.blocks.access_gateway import AccessGateway, AccessGatewayConfig
from web_listening.blocks.acquisition_profile import (
    AcquisitionAdapterConfig,
    AcquisitionQualityGates,
    AcquisitionRecipeMapping,
)
from web_listening.blocks.governed_read import GovernedReadGateway
from web_listening.blocks.monitor_scope_planner import render_yaml_text
from web_listening.blocks.site_diagnostic import RawHttpResponse, normalize_http_url
from web_listening.contracts import SiteSkillExecutor, SiteSkillRecipe, VerificationRule
from web_listening.contracts.site_diagnostic import DiagnosticIdentity, canonical_sha256
from web_listening.executors.playwright_wrapper import (
    prepare_browser_acquisition_adapter,
)
from web_listening.site_skill_registry import ResolvedSiteSkill

BODY = (
    "<html><head><title>Article</title></head><body><p>"
    + "Governed article evidence. " * 200
    + "</p></body></html>"
)
URL = "https://example.com/news"


@contextmanager
def loopback_authority(directory: Path, scenario="success"):
    # Reuse the producer's reviewed static Site Skill fixture and real compiler.
    from tests.test_acquisition_execution_plan import inputs

    scope, profile, skill, _ = inputs()
    manifest = skill.manifest.model_copy(
        update={
            "executors": skill.manifest.executors
            + (SiteSkillExecutor(executor_id="browser_rendered"),),
            "recipes": skill.manifest.recipes
            + (
                SiteSkillRecipe(
                    recipe_id="news-browser",
                    executor_id="browser_rendered",
                    profile_ref="profiles/default.yaml",
                    entrypoint="scripts/recipe.py",
                    required_capabilities=("browser_render",),
                    verification_rules=(
                        VerificationRule(
                            rule_id="status-ok", description="Successful status."
                        ),
                    ),
                ),
            ),
        }
    )
    skill = ResolvedSiteSkill(manifest, skill.package_sha256, skill.script_sha256)
    profile = profile.model_copy(
        update={
            "fallback_order": ["browser_rendered"],
            "adapters": profile.adapters
            + [AcquisitionAdapterConfig(adapter="browser_rendered")],
            "recipe_mappings": profile.recipe_mappings
            + [
                AcquisitionRecipeMapping(
                    adapter="browser_rendered", recipe_id="news-browser"
                )
            ],
            "quality_gates": AcquisitionQualityGates(min_words=4, min_links=0),
        }
    )
    scope = replace(scope, max_pages=4, max_files=0)
    directory.mkdir(parents=True, exist_ok=True)
    scope_path, profile_path = directory / "scope.yaml", directory / "profile.yaml"
    scope_path.write_text(render_yaml_text(scope), encoding="utf-8")
    profile_path.write_text(
        yaml.safe_dump(profile.model_dump(mode="json")), encoding="utf-8"
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/robots.txt" or scenario == "not_found":
                status, body = 404, b""
            elif scenario == "all_empty":
                status, body = 200, b""
            elif scenario == "unrenderable":
                status, body = 200, b"<html><body>empty</body></html>"
            elif scenario == "fallback" and seen.count("/news") == 1:
                status, body = 200, b""
            else:
                status, body = 200, BODY.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Transport:
        def request(self, url, *, user_agent, identity_sha256, timeout_seconds=None):
            assert urlsplit(url).hostname == "example.com"
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            try:
                connection.request(
                    "GET", urlsplit(url).path, headers={"User-Agent": user_agent}
                )
                response = connection.getresponse()
                return RawHttpResponse(
                    response.status, dict(response.getheaders()), [response.read()]
                )
            finally:
                connection.close()

    readers = []

    def build(**kwargs):
        visible = {
            "identity_id": "article-loopback",
            "product_token": "web-listening-bot",
            "user_agent": "web-listening-bot/1.0",
        }
        identity = DiagnosticIdentity(
            **visible, identity_sha256=canonical_sha256(visible)
        )
        gateway = AccessGateway(
            AccessGatewayConfig(
                identity=identity,
                allowed_origins=frozenset({normalize_http_url(URL)[1]}),
                diagnostic_artifact_sha256=kwargs["authority_sha256"],
                pacing_interval=timedelta(0),
                budget_limit=kwargs["budget_limit"],
            ),
            transport=Transport(),
            clock=lambda: datetime(2026, 9, 7, tzinfo=UTC),
        )
        reader = GovernedReadGateway(gateway, max_body_bytes=kwargs["max_body_bytes"])
        readers.append(reader)
        return reader

    class Session:
        runtime_id = "loopback-fixture"
        runtime_version = "1.0.0"

        def open(self):
            pass

        def render(self, response, **kwargs):
            return response.body.decode("utf-8")

        def close(self):
            pass

    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "web_listening.site_skill_registry.resolve_site_skill_contract",
                    return_value=skill,
                )
            )
            stack.enter_context(
                patch(
                    "web_listening.blocks.governed_read.build_runtime_read_gateway",
                    side_effect=build,
                )
            )
            stack.enter_context(
                patch(
                    "web_listening.executors.playwright_wrapper.prepare_browser_acquisition_adapter",
                    side_effect=lambda plan, step, gateway: prepare_browser_acquisition_adapter(
                        plan, step, gateway, session_factory=Session
                    ),
                )
            )
            yield {
                "url": URL,
                "profile_path": str(profile_path),
                "scope_path": str(scope_path),
                "output_dir": str(directory / "content"),
            }, seen
    finally:
        for reader in readers:
            reader.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
