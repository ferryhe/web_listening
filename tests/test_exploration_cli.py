from __future__ import annotations

import builtins
import json
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader
from typer.testing import CliRunner

from web_listening import exploration
from web_listening.cli import app


runner = CliRunner()


def _require_browser_runtime() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                pytest.skip("Playwright Chromium is not installed; set PLAYWRIGHT_BROWSERS_PATH for the opt-in browser fixture")
    except pytest.skip.Exception:
        raise
    except Exception as exc:
        pytest.skip(f"Optional Playwright browser runtime is unavailable: {type(exc).__name__}")


def _text_pdf() -> bytes:
    text_stream = b"BT /F1 18 Tf 20 200 Td (Hello world) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(text_stream)} >>\nstream\n".encode("ascii")
        + text_stream
        + b"endstream",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n".encode("ascii"))
    body.extend(b" " * (5 * 1024 * 1024))
    body.extend(b"%%EOF\n")
    value = bytes(body)
    assert len(value) > 4 * 1024 * 1024
    assert len(PdfReader(__import__("io").BytesIO(value), strict=True).pages) == 2
    return value


class _FixtureHandler(BaseHTTPRequestHandler):
    pdf = _text_pdf()
    requests: list[tuple[str, str]] = []
    user_agents: list[str | None] = []

    def log_message(self, *_: object) -> None:
        return

    def _send(self, status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.command))
        type(self).user_agents.append(self.headers.get("User-Agent"))
        if self.path == "/redirect-page":
            self._send(302, "text/plain", b"", {"Location": "/"})
        elif self.path == "/redirect-pdf":
            self._send(302, "text/plain", b"", {"Location": "/document.pdf"})
        elif self.path == "/redirect-disallowed-page":
            self._send(302, "text/plain", b"", {"Location": "https://outside.example.test/page"})
        elif self.path == "/redirect-disallowed-file":
            self._send(302, "text/plain", b"", {"Location": "https://outside.example.test/file.pdf"})
        elif self.path == "/":
            self._send(
                200,
                "text/html; charset=utf-8",
                b"""<!doctype html><html><head><title>Fixture</title><link rel='stylesheet' href='/site.css'><script src='/app.js' defer></script></head>
<body><button id='faq'>FAQ</button><section id='faq-panel' hidden>FAQ answer</section>
<div id='page'>Page 1</div><button id='next'>Next</button><a href='/document.pdf'>PDF</a></body></html>""",
            )
        elif self.path == "/policy-frame":
            self._send(
                200,
                "text/html; charset=utf-8",
                b"<!doctype html><html><body><p>Public page content</p><iframe src='http://blocked.test/frame'></iframe></body></html>",
            )
        elif self.path == "/navigate-404":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><body><a id='next' href='/destination-404'>Next</a></body></html>")
        elif self.path == "/navigate-200":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><body><a id='next' href='/destination-200'>Next</a></body></html>")
        elif self.path == "/navigate-challenge":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><body><a id='next' href='/destination-challenge'>Next</a></body></html>")
        elif self.path == "/partial-page":
            self._send(
                206,
                "text/html; charset=utf-8",
                b"<!doctype html><html><body>Partial page content with enough words to look complete.</body></html>",
                {"Content-Range": "bytes 0-85/120"},
            )
        elif self.path == "/wordpress-menu":
            menu = "".join(
                f"<a class='menu-item menu-item-type-post_type menu-item-object-page' href='/menu/{index}'>Menu item {index}</a>"
                for index in range(120)
            )
            body = (
                "<!doctype html><html><body><nav><ul>"
                + menu
                + "</ul></nav><main><a class='page-numbers next' rel='next' href='/page/2'>Next</a></main></body></html>"
            )
            self._send(200, "text/html; charset=utf-8", body.encode("utf-8"))
        elif self.path == "/destination-404":
            self._send(404, "text/html; charset=utf-8", b"<!doctype html><html><body>Not found</body></html>")
        elif self.path == "/destination-200":
            self._send(200, "application/xhtml+xml", b"<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'><head><title>Final</title></head><body><p>Final destination content</p></body></html>")
        elif self.path == "/destination-challenge":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><head><title>Just a moment...</title></head><body>Please verify you are human to continue.</body></html>")
        elif self.path == "/app.js":
            self._send(
                200,
                "application/javascript",
                b"document.querySelector('#faq').onclick=()=>document.querySelector('#faq-panel').hidden=false;\n"
                b"document.querySelector('#next').onclick=async()=>{let r=await fetch('/api/page/2');document.querySelector('#page').textContent=await r.text()};",
            )
        elif self.path == "/site.css":
            self._send(200, "text/css", b"body { font-family: sans-serif }")
        elif self.path == "/api/page/2":
            self._send(200, "text/plain; charset=utf-8", b"Page 2")
        elif self.path == "/http-forbidden":
            if self.headers.get("Sec-Fetch-Mode") == "navigate":
                self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><body>Browser route works</body></html>")
            else:
                self._send(403, "text/html; charset=utf-8", b"<!doctype html><title>HTTP denied</title>")
        elif self.path == "/challenge":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><head><title>Just a moment...</title></head><body>Please verify you are human to continue.</body></html>")
        elif self.path == "/cloudflare-article":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><html><body>Cloudflare is a company described in this article.</body></html>")
        elif self.path == "/document.pdf":
            self._send(200, "application/pdf", type(self).pdf, {"ETag": '"fixture-pdf"'})
        elif self.path == "/mislabeled-pdf.pdf":
            self._send(200, "text/html", type(self).pdf)
        elif self.path == "/pdf-page":
            self._send(200, "application/pdf", type(self).pdf)
        elif self.path == "/pdf-page-octet":
            self._send(200, "application/octet-stream", type(self).pdf)
        elif self.path == "/json-page":
            self._send(
                200,
                "application/json; charset=utf-8",
                b'{"title":"Public insurance committee updates","summary":"New regulatory announcements and market research"}',
            )
        elif self.path == "/image-page":
            self._send(200, "image/png", b"Public insurance committee image file response.")
        elif self.path == "/plain-document":
            self._send(200, "text/plain; charset=utf-8", b"Public insurance committee report with meaningful text content.")
        elif self.path == "/json-document":
            self._send(
                200,
                "application/json; charset=utf-8",
                b'{"title":"Public insurance committee report","summary":"Meaningful regulatory text content"}',
            )
        elif self.path == "/document-copy.pdf":
            self._send(200, "application/pdf", type(self).pdf, {"ETag": '"fixture-pdf-copy"'})
        elif self.path == "/error.pdf":
            self._send(200, "text/html; charset=utf-8", b"<!doctype html><title>Proxy error</title>")
        elif self.path == "/short.pdf":
            self._send(200, "application/pdf", b"%PDF-1.4\nshort")
        elif self.path == "/signed":
            self._send(200, "application/octet-stream", b"%PDF-1.7\nmissing end marker")
        elif self.path == "/large.bin":
            self._send(200, "application/octet-stream", b"z" * (1024 * 1024))
        elif self.path == "/partial.bin":
            self._send(206, "application/octet-stream", b"0123456789abcdef", {"Content-Range": "bytes 0-15/500"})
        elif self.path == "/legacy-no-charset":
            self._send(
                200,
                "text/html",
                b"<!doctype html><html><head><meta charset='windows-1252'></head><body>caf\xe9</body></html>",
            )
        elif self.path == "/legacy-bom":
            self._send(
                200,
                "text/html",
                b"\xef\xbb\xbf<!doctype html><html><body>caf\xc3\xa9</body></html>",
            )
        elif self.path == "/explicit-header-charset":
            self._send(
                200,
                "text/html; charset=windows-1252",
                b"<!doctype html><html><head><meta charset='utf-8'></head><body>caf\xe9</body></html>",
            )
        elif self.path in {"/slow-page", "/slow-file"}:
            content_type = "text/html; charset=utf-8" if self.path == "/slow-page" else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", "3")
            self.end_headers()
            for part in (b"a", b"b", b"c"):
                time.sleep(0.4)
                try:
                    self.wfile.write(part)
                    self.wfile.flush()
                except OSError:
                    break
        elif self.path in {"/deadline-page", "/deadline-file"}:
            content_type = "text/html; charset=utf-8" if self.path == "/deadline-page" else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", "3")
            self.end_headers()
            time.sleep(0.8)
            try:
                self.wfile.write(b"a")
                self.wfile.flush()
                time.sleep(2)
                self.wfile.write(b"bc")
                self.wfile.flush()
            except OSError:
                pass
        else:
            self._send(404, "text/plain", b"not found")


@pytest.fixture()
def fixture_server():
    _FixtureHandler.requests = []
    _FixtureHandler.user_agents = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _payload(result) -> dict:
    assert result.stdout.strip()
    return json.loads(result.stdout)


def _collect_args(
    urls_file: Path,
    output_dir: Path,
    *,
    kind: str,
    method: str,
    parse_pdf: bool = False,
    user_agent: str | None = None,
) -> list[str]:
    args = [
        "collect", "--urls-file", str(urls_file), "--kind", kind, "--method", method,
        "--output-dir", str(output_dir), "--pacing-seconds", "0", "--jitter-seconds", "0",
        "--timeout-seconds", "5", "--json",
    ]
    if parse_pdf:
        args.append("--parse-pdf")
    if user_agent is not None:
        args.extend(["--user-agent", user_agent])
    return args


def test_exploration_command_help_and_json_parser_error():
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    assert all(command in root.stdout for command in ("read-page", "download-file", "extract-pdf", "collect", "list-exploration-tools"))
    for command in ("read-page", "download-file", "extract-pdf", "collect", "list-exploration-tools"):
        help_result = runner.invoke(app, [command, "--help"])
        assert help_result.exit_code == 0
        assert "--json" in help_result.stdout
    page_help = runner.invoke(app, ["read-page", "--help"])
    assert "--browser-channel" in page_help.stdout and "--headed" in page_help.stdout
    collect_help = runner.invoke(app, ["collect", "--help"])
    assert "--urls-file" in collect_help.stdout and "--manifest" in collect_help.stdout
    assert "--max-requests" in collect_help.stdout
    assert "Route-observed" in collect_help.stdout
    assert "--user-agent" in page_help.stdout
    assert "--user-agent" in runner.invoke(app, ["download-file", "--help"]).stdout
    assert "--user-agent" in collect_help.stdout
    catalog = runner.invoke(app, ["list-exploration-tools", "--json"])
    assert catalog.exit_code == 0
    catalog_payload = _payload(catalog)
    assert catalog_payload["contract_version"] == "exploration-tools.v1"
    assert "list-acquisition-tools" in catalog_payload["formal_scope_catalog"]
    assert catalog_payload["http_request_identity"]["default"] == exploration.DEFAULT_HTTP_USER_AGENT
    assert {item["name"] for item in catalog_payload["tools"]} == {
        "read-page", "download-file", "extract-pdf", "collect"
    }
    formal_catalog = runner.invoke(app, ["list-acquisition-tools", "--json"])
    assert formal_catalog.exit_code == 0
    assert _payload(formal_catalog)["contract_version"] == "acquisition-tools.v1"
    parser_cases = [
        (["read-page", "--json"], "--url"),
        (["download-file", "--json"], "--url"),
        (["extract-pdf", "--json"], "--input"),
        (["collect", "--json"], "--urls-file"),
        (["list-exploration-tools", "--json", "--not-an-option"], "--not-an-option"),
    ]
    for args, expected_message in parser_cases:
        invalid = runner.invoke(app, args)
        assert invalid.exit_code != 0
        payload = _payload(invalid)
        assert payload["tool"] == args[0]
        assert payload["error"]["code"] == "parser.invalid"
        assert expected_message in payload["error"]["message"]
        assert payload["next_action"] == "correct_arguments"
        assert payload["meta"]["contract_version"] == "web-listening-tool-result.v1"


def test_browser_reads_external_script_xhr_faq_and_ajax_page(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    discovery = runner.invoke(
        app,
        [
            "read-page",
            "--url",
            fixture_server,
            "--method",
            "browser",
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--json",
        ],
    )
    assert discovery.exit_code == 0, discovery.stdout + discovery.stderr
    discovery_data = _payload(discovery)["data"]
    assert discovery_data["interaction_candidates_source"] == "rendered_dom"
    faq_control = next(item for item in discovery_data["interaction_candidates"] if item["name"] == "FAQ")
    next_control = next(item for item in discovery_data["interaction_candidates"] if item["name"] == "Next")
    actions = [
        {"type": "click", "selector": faq_control["selector"]},
        {"type": "wait-text", "text": "FAQ answer"},
        {"type": "click", "selector": next_control["selector"]},
        {"type": "wait-text", "text": "Page 2"},
    ]
    result = runner.invoke(
        app,
        [
            "read-page",
            "--url",
            fixture_server,
            "--method",
            "browser",
            "--actions-json",
            json.dumps(actions),
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["meta"]["contract_version"] == "web-listening-tool-result.v1"
    assert "FAQ answer" in payload["data"]["text"]
    assert payload["data_quality"]["passed"] is True
    page = json.loads(Path(payload["artifacts"]["page_result"]).read_text(encoding="utf-8"))
    html = Path(payload["artifacts"]["page_html"]).read_text(encoding="utf-8")
    assert "Page 2" in html and "FAQ answer" in html
    assert page["text"].find("FAQ answer") < page["text"].find("Page 2")
    assert len(page["actions"]) == 4
    assert page["actions"][0]["action"] == {"type": "click", "selector": faq_control["selector"]}
    assert all(item["status"] == "completed" for item in page["actions"])
    assert {"script", "stylesheet", "fetch"}.issubset({item["resource_type"] for item in page["requests"]})
    assert all(item["status"] == 200 for item in page["requests"] if item["resource_type"] in {"script", "stylesheet", "fetch"})
    assert any(item["requested_url"].endswith("/api/page/2") for item in page["requests"])
    assert page["html_sha256"] == __import__("hashlib").sha256(html.encode("utf-8")).hexdigest()
    assert page["interaction_candidates_source"] == "rendered_dom"
    assert page["interaction_visibility_note"].startswith("Candidates come from DOM attributes")


@pytest.mark.parametrize(
    "path,expected_status,expected_content_type,expected_text,error_code",
    [
        ("navigate-200", 200, "application/xhtml+xml", "Final destination content", None),
        ("navigate-404", 404, "text/html", "Not found", "http.status"),
    ],
)
def test_browser_result_uses_final_main_frame_navigation_response(
    fixture_server, tmp_path: Path, path: str, expected_status: int,
    expected_content_type: str, expected_text: str, error_code: str | None,
):
    _require_browser_runtime()
    result = runner.invoke(
        app,
        [
            "read-page",
            "--url",
            f"{fixture_server}/{path}",
            "--method",
            "browser",
            "--actions-json",
            json.dumps([{"type": "click", "selector": "#next"}]),
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--json",
        ],
    )
    payload = _payload(result)
    final_document = [
        item for item in payload["attempts"]
        if item["resource_type"] == "document" and item["requested_url"].endswith("destination-" + path[-3:])
    ]
    assert final_document
    assert final_document[-1]["status"] == expected_status
    if error_code:
        assert result.exit_code != 0
        assert payload["error"]["code"] == error_code
        return
    assert result.exit_code == 0, result.stdout + result.stderr
    assert payload["data"]["status"] == expected_status
    assert payload["data"]["content_type"] == expected_content_type
    assert payload["data"]["final_url"].endswith("destination-200")
    assert expected_text in payload["data"]["text"]


def test_browser_classifies_challenge_after_main_frame_navigation(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    result = runner.invoke(
        app,
        [
            "read-page",
            "--url",
            f"{fixture_server}/navigate-challenge",
            "--method",
            "browser",
            "--actions-json",
            json.dumps([{"type": "click", "selector": "#next"}]),
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["data"]["final_url"].endswith("destination-challenge")
    assert payload["data"]["status"] == 200
    assert payload["data"]["content_type"].startswith("text/html")
    assert payload["data"]["classification"] == "blocked"
    assert payload["data_status"] == "blocked"
    assert payload["data_quality"]["passed"] is False
    assert "Please verify you are human" in payload["data"]["text"]


def test_http_read_and_action_validation_are_structured(fixture_server, tmp_path: Path):
    result = runner.invoke(
        app,
        ["read-page", "--url", fixture_server, "--output-dir", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["data"]["status"] == 200
    assert payload["data"]["interaction_candidates_source"] == "static_dom"
    assert Path(payload["artifacts"]["page_html"]).exists()
    invalid = runner.invoke(
        app,
        ["read-page", "--url", fixture_server, "--method", "browser", "--actions-json", "[{\"type\":\"eval\",\"code\":\"bad\"}]", "--json"],
    )
    assert invalid.exit_code != 0
    invalid_payload = _payload(invalid)
    assert invalid_payload["error"]["code"] == "actions.invalid"
    assert invalid_payload["next_action"] == "correct_actions"
    assert invalid_payload["data"]["browser_channel"] == "bundled"
    assert invalid_payload["data"]["browser_version"] is None
    assert invalid_payload["data"]["headless"] is True
    assert exploration._allowed_domains("https://target.test", ["other.test"]) == [
        "target.test",
        "other.test",
    ]


@pytest.mark.parametrize(
    ("path", "expected_content_type", "expected_pdf_signature"),
    [
        ("pdf-page", "application/pdf", True),
        ("pdf-page-octet", "application/octet-stream", True),
        ("json-page", "application/json", False),
        ("image-page", "image/png", False),
    ],
)
@pytest.mark.parametrize("command", ["read-page", "collect"])
def test_http_non_html_response_is_not_reported_as_html_success(
    fixture_server, tmp_path: Path, path: str, expected_content_type: str,
    expected_pdf_signature: bool, command: str,
):
    url = f"{fixture_server}/{path}"
    if command == "read-page":
        result = runner.invoke(
            app,
            ["read-page", "--url", url, "--output-dir", str(tmp_path), "--json"],
        )
        assert result.exit_code != 0
        payload = _payload(result)
        assert payload["error"]["code"] == "page.non_html_response"
        assert payload["data_quality"]["passed"] is False
        assert payload["data_quality"]["status_code"] == 200
        assert payload["next_action"] == "use_download_file"
        assert payload["data"]["pdf_signature"] is expected_pdf_signature
        assert payload["data"]["content_type"].startswith(expected_content_type)
        assert payload["attempts"][0]["status"] == 200
        assert payload["artifacts"] == {}
        return

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(url + "\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [*_collect_args(urls_file, tmp_path / "out", kind="page", method="http")],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["data"]["success_count"] == 0
    assert payload["data"]["failed_count"] == 1
    item = payload["data"]["items"][0]
    assert item["outcome"] == "failed"
    assert item["quality_passed"] is False
    row = json.loads(Path(payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()[0])
    assert row["outcome"] == "failed"
    stage = row["stages"][0]
    assert stage["error_code"] == "page.non_html_response"
    stage_payload = json.loads(Path(stage["result_ref"]).read_text(encoding="utf-8"))
    assert stage_payload["data_quality"]["passed"] is False
    assert stage_payload["next_action"] == "use_download_file"
    assert stage_payload["data"]["pdf_signature"] is expected_pdf_signature


@pytest.mark.parametrize(
    ("command", "path", "expected_next_action"),
    [
        ("read-page", "redirect-disallowed-page", "inspect_request_ledger_and_allow_required_public_navigation_domain"),
        ("download-file", "redirect-disallowed-file", "correct_url_or_allowed_domains"),
    ],
)
def test_http_redirect_denial_records_target_without_requesting_it(
    fixture_server, tmp_path: Path, command: str, path: str, expected_next_action: str
):
    url = f"{fixture_server}/{path}"
    args = (
        ["read-page", "--url", url, "--output-dir", str(tmp_path), "--json"]
        if command == "read-page"
        else ["download-file", "--url", url, "--output-dir", str(tmp_path), "--pacing-seconds", "0", "--jitter-seconds", "0", "--json"]
    )
    result = runner.invoke(app, args)

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "navigation.domain_not_allowed"
    assert payload["next_action"] == expected_next_action
    assert len(payload["attempts"]) == 1
    attempt = payload["attempts"][0]
    assert attempt["requested_url"] == url
    assert attempt["status"] == 302
    assert attempt["redirect_target_url"] == "https://outside.example.test/" + (
        "page" if command == "read-page" else "file.pdf"
    )
    assert _FixtureHandler.requests == [("/" + path, "GET")]


@pytest.mark.parametrize(
    ("path", "expected_html"),
    [
        ("legacy-no-charset", "<meta charset='windows-1252'>"),
        ("legacy-bom", "<!doctype html>"),
        ("explicit-header-charset", "<meta charset='utf-8'>"),
    ],
)
def test_http_page_decoding_uses_header_bom_or_meta_encoding(
    fixture_server, tmp_path: Path, path: str, expected_html: str
):
    result = runner.invoke(
        app,
        ["read-page", "--url", f"{fixture_server}/{path}", "--output-dir", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert "café" in payload["data"]["text"]
    html = Path(payload["artifacts"]["page_html"]).read_text(encoding="utf-8")
    assert "café" in html
    assert expected_html in html
    assert payload["data"]["html_sha256"] == __import__("hashlib").sha256(html.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("method", ["http", "browser"])
@pytest.mark.parametrize("command", ["read-page", "collect"])
def test_partial_page_response_is_not_reported_as_complete(
    fixture_server, tmp_path: Path, method: str, command: str
):
    if method == "browser":
        _require_browser_runtime()
    url = f"{fixture_server}/partial-page"
    if command == "read-page":
        result = runner.invoke(
            app,
            [
                "read-page", "--url", url, "--method", method, "--output-dir", str(tmp_path),
                "--timeout-seconds", "5", "--json",
            ],
        )
        assert result.exit_code != 0
        payload = _payload(result)
        assert payload["error"]["code"] == "page.partial_response"
        assert payload["data_quality"]["passed"] is False
        assert payload["data_quality"]["status_code"] == 206
        assert payload["next_action"] == "retry_for_complete_page_response"
        assert payload["data"]["content_range"] == "bytes 0-85/120"
        assert payload["attempts"][0]["status"] == 206
        assert payload["attempts"][0]["content_range"] == "bytes 0-85/120"
        assert payload["artifacts"] == {}
        if method == "browser":
            assert payload["data"]["browser_channel"] == "bundled"
            assert payload["data"]["browser_version"]
        return

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(url + "\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method=method),
            "--timeout-seconds", "5",
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["data"]["success_count"] == 0
    assert payload["data"]["failed_count"] == 1
    assert payload["data"]["items"][0]["outcome"] == "failed"
    assert payload["data"]["items"][0]["next_action"] == "retry_for_complete_page_response"
    row = json.loads(Path(payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()[0])
    stage = row["stages"][0]
    stage_payload = json.loads(Path(stage["result_ref"]).read_text(encoding="utf-8"))
    assert stage["error_code"] == "page.partial_response"
    assert stage_payload["data_quality"]["passed"] is False
    assert stage_payload["data"]["content_range"] == "bytes 0-85/120"
    assert stage_payload["attempts"][0]["content_range"] == "bytes 0-85/120"
    assert stage_payload["artifacts"] == {}
    if method == "browser":
        assert stage_payload["data"]["browser_version"]


@pytest.mark.parametrize("path,expected_content_type", [
    ("plain-document", "text/plain"),
    ("json-document", "application/json"),
])
@pytest.mark.parametrize("command", ["read-page", "collect"])
def test_browser_non_html_document_is_not_reported_as_html_success(
    fixture_server, tmp_path: Path, path: str, expected_content_type: str, command: str
):
    _require_browser_runtime()
    url = f"{fixture_server}/{path}"
    if command == "read-page":
        result = runner.invoke(
            app,
            [
                "read-page", "--url", url, "--method", "browser", "--browser-channel", "bundled",
                "--timeout-seconds", "5", "--output-dir", str(tmp_path), "--json",
            ],
        )
        assert result.exit_code != 0, result.stdout + result.stderr
        payload = _payload(result)
        assert payload["error"]["code"] == "page.non_html_response"
        assert payload["data_quality"]["passed"] is False
        assert payload["next_action"] == "use_download_file"
        assert payload["data"]["content_type"].startswith(expected_content_type)
        assert payload["attempts"][0]["status"] == 200
        assert payload["attempts"][0]["content_type"].startswith(expected_content_type)
        assert payload["data"]["browser_channel"] == "bundled"
        assert payload["data"]["browser_version"]
        assert payload["data"]["headless"] is True
        assert payload["artifacts"] == {}
        return

    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(url + "\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [*_collect_args(urls_file, tmp_path / "out", kind="page", method="browser")],
    )
    assert result.exit_code != 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["data"]["success_count"] == 0
    assert payload["data"]["failed_count"] == 1
    item = payload["data"]["items"][0]
    assert item["outcome"] == "failed"
    assert item["quality_passed"] is False
    row = json.loads(Path(payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()[0])
    stage = row["stages"][0]
    stage_payload = json.loads(Path(stage["result_ref"]).read_text(encoding="utf-8"))
    assert stage["error_code"] == "page.non_html_response"
    assert stage_payload["data_quality"]["passed"] is False
    assert stage_payload["next_action"] == "use_download_file"
    assert stage_payload["data"]["content_type"].startswith(expected_content_type)
    assert stage_payload["attempts"][0]["content_type"].startswith(expected_content_type)
    assert stage_payload["artifacts"] == {}
    assert stage_payload["data"]["browser_channel"] == "bundled"
    assert stage_payload["data"]["browser_version"]
    assert stage_payload["data"]["headless"] is True


@pytest.mark.parametrize("command", ["download-file", "collect"])
def test_valid_pdf_signature_overrides_mislabeled_html_mime_and_collect_parses(
    fixture_server, tmp_path: Path, command: str
):
    url = f"{fixture_server}/mislabeled-pdf.pdf"
    if command == "download-file":
        result = runner.invoke(
            app,
            [
                "download-file", "--url", url, "--output-dir", str(tmp_path),
                "--pacing-seconds", "0", "--jitter-seconds", "0", "--json",
            ],
        )
    else:
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(url + "\n", encoding="utf-8")
        result = runner.invoke(
            app,
            [*_collect_args(urls_file, tmp_path / "out", kind="file", method="http", parse_pdf=True)],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    expected_hash = __import__("hashlib").sha256(_FixtureHandler.pdf).hexdigest()
    if command == "download-file":
        artifact_path = Path(payload["artifacts"]["file"])
        assert payload["data"]["sha256"] == expected_hash
        assert payload["data"]["size_bytes"] == len(_FixtureHandler.pdf)
        assert payload["data"]["mime_type"].startswith("text/html")
        assert payload["attempts"][0]["content_type"].startswith("text/html")
        source_metadata_path = Path(payload["artifacts"]["source_metadata"])
    else:
        assert payload["data"]["success_count"] == 1
        assert payload["data"]["failed_count"] == 0
        row = json.loads(Path(payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()[0])
        download_result = json.loads(Path(row["stages"][0]["result_ref"]).read_text(encoding="utf-8"))
        parse_result = json.loads(Path(row["stages"][1]["result_ref"]).read_text(encoding="utf-8"))
        artifact_path = Path(download_result["artifacts"]["file"])
        assert download_result["data"]["sha256"] == expected_hash
        assert download_result["data"]["mime_type"].startswith("text/html")
        assert parse_result["data"]["page_count"] == 2
        assert parse_result["data"]["source_url"] == url
        source_metadata_path = Path(download_result["artifacts"]["source_metadata"])
    assert artifact_path.read_bytes() == _FixtureHandler.pdf
    source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
    assert source_metadata["source_url"] == url
    assert source_metadata["sha256"] == expected_hash
    assert source_metadata["mime_type"].startswith("text/html")


def test_interaction_candidates_ignore_wordpress_page_menu_items_and_keep_next(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    result = runner.invoke(
        app,
        [
            "read-page", "--url", f"{fixture_server}/wordpress-menu", "--method", "browser",
            "--output-dir", str(tmp_path), "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["data"]["interaction_candidates_source"] == "rendered_dom"
    candidates = payload["data"]["interaction_candidates"]
    assert payload["data"]["interaction_candidate_count"] == 1
    assert payload["data"]["interaction_candidates_truncated"] is False
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "pagination_link"
    assert candidates[0]["name"] == "Next"
    assert candidates[0]["selector"]
    assert candidates[0]["url"].endswith("/page/2")


def test_http_page_user_agent_default_and_override_survive_redirects(fixture_server, tmp_path: Path):
    default = runner.invoke(
        app,
        [
            "read-page", "--url", f"{fixture_server}/redirect-page",
            "--output-dir", str(tmp_path / "default"), "--json",
        ],
    )
    assert default.exit_code == 0, default.stdout + default.stderr
    default_payload = _payload(default)
    expected_default = exploration.DEFAULT_HTTP_USER_AGENT
    assert default_payload["data"]["user_agent"] == expected_default
    assert _FixtureHandler.user_agents == [expected_default, expected_default]
    assert [attempt["user_agent"] for attempt in default_payload["attempts"]] == [expected_default, expected_default]

    _FixtureHandler.requests = []
    _FixtureHandler.user_agents = []
    custom = "web-listening-evaluation/1.0"
    result = runner.invoke(
        app,
        [
            "read-page", "--url", f"{fixture_server}/redirect-page",
            "--user-agent", custom, "--output-dir", str(tmp_path / "custom"), "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["data"]["user_agent"] == custom
    assert _FixtureHandler.user_agents == [custom, custom]
    assert [attempt["user_agent"] for attempt in payload["attempts"]] == [custom, custom]


@pytest.mark.parametrize("command", ["read-page", "download-file", "collect"])
def test_invalid_http_user_agent_fails_before_target_io(fixture_server, tmp_path: Path, command: str):
    invalid = "web-listening/1.0\r\nInjected: true"
    output_dir = tmp_path / command
    if command == "read-page":
        args = [command, "--url", fixture_server, "--output-dir", str(output_dir), "--json"]
    elif command == "download-file":
        args = [command, "--url", f"{fixture_server}/document.pdf", "--output-dir", str(output_dir), "--pacing-seconds", "0", "--jitter-seconds", "0", "--json"]
    else:
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
        args = _collect_args(urls_file, output_dir, kind="page", method="http")
    result = runner.invoke(app, [*args, "--user-agent", invalid])
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "user_agent.invalid"
    assert payload["next_action"] == "correct_user_agent"
    assert payload["attempts"] == []
    assert _FixtureHandler.requests == []
    assert not output_dir.exists()


@pytest.mark.parametrize("command", ["read-page", "collect"])
def test_browser_rejects_custom_http_user_agent_before_runtime_or_target_io(
    fixture_server, tmp_path: Path, command: str
):
    output_dir = tmp_path / "out"
    if command == "read-page":
        args = [command, "--url", fixture_server, "--method", "browser", "--output-dir", str(output_dir), "--json"]
    else:
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
        args = _collect_args(urls_file, output_dir, kind="page", method="browser")
    result = runner.invoke(app, [*args, "--user-agent", "web-listening-evaluation/1.0"])
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "user_agent.unsupported"
    assert payload["next_action"] == "remove_user_agent_for_browser_mode"
    assert payload["attempts"] == []
    if command == "read-page":
        assert payload["data"]["browser_version"] is None
    assert _FixtureHandler.requests == []


def test_exploration_runtime_and_input_errors_have_specific_next_actions(tmp_path: Path, monkeypatch):
    missing_input = runner.invoke(
        app,
        ["extract-pdf", "--input", str(tmp_path / "missing.pdf"), "--output-dir", str(tmp_path), "--json"],
    )
    assert missing_input.exit_code != 0
    missing_payload = _payload(missing_input)
    assert missing_payload["error"]["code"] == "input.not_found"
    assert missing_payload["next_action"] == "correct_input_path"

    valid_path = tmp_path / "present.pdf"
    valid_path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        exploration,
        "version",
        lambda _name: (_ for _ in ()).throw(exploration.PackageNotFoundError("pypdf")),
    )
    missing_runtime = runner.invoke(
        app,
        ["extract-pdf", "--input", str(valid_path), "--output-dir", str(tmp_path), "--json"],
    )
    assert missing_runtime.exit_code != 0
    runtime_payload = _payload(missing_runtime)
    assert runtime_payload["error"]["code"] == "pdf.runtime_missing"
    assert runtime_payload["next_action"] == "install_pdf_runtime"


def test_browser_runtime_error_action_and_channel_options_are_specific(tmp_path: Path, monkeypatch):
    captured: dict[str, Any] = {}

    def missing_browser(**kwargs):
        captured.update(kwargs)
        raise exploration.ExplorationError("browser.channel_unavailable", "not installed")

    monkeypatch.setattr(exploration, "_browser_capture", missing_browser)
    result = runner.invoke(
        app,
        [
            "read-page", "--url", "https://example.test", "--method", "browser",
            "--browser-channel", "msedge", "--headed", "--output-dir", str(tmp_path), "--json",
        ],
    )
    assert result.exit_code != 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["error"]["code"] == "browser.channel_unavailable"
    assert payload["next_action"] == "install_or_select_browser_channel"
    assert captured["browser_channel"] == "msedge"
    assert captured["headed"] is True


def test_browser_runtime_missing_error_keeps_requested_provenance(tmp_path: Path, monkeypatch):
    original_import = builtins.__import__

    def import_without_playwright(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError("optional Playwright package is unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_playwright)
    result = runner.invoke(
        app,
        [
            "read-page", "--url", "https://example.test", "--method", "browser",
            "--browser-channel", "msedge", "--headed", "--output-dir", str(tmp_path), "--json",
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "browser.runtime_missing"
    assert payload["next_action"] == "install_browser_runtime"
    assert payload["data"]["browser_channel"] == "msedge"
    assert payload["data"]["browser_version"] is None
    assert payload["data"]["headless"] is False


def test_browser_output_failure_keeps_actual_runtime_provenance(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    output_file = tmp_path / "output-file"
    output_file.write_text("not a directory", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "read-page", "--url", fixture_server, "--method", "browser",
            "--browser-channel", "bundled", "--headed", "--output-dir", str(output_file),
            "--timeout-seconds", "5", "--json",
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "read.failed"
    assert payload["data"]["browser_channel"] == "bundled"
    assert payload["data"]["browser_version"]
    assert payload["data"]["headless"] is False


@pytest.mark.parametrize(
    "action",
    [
        {"type": []},
        {"type": {}},
        {"type": "wait-selector", "selector": "#target", "state": []},
        {"type": "wait-selector", "selector": "#target", "state": {}},
    ],
)
def test_unhashable_action_type_and_state_are_structured_errors_without_target_reads(
    fixture_server, tmp_path: Path, action: dict
):
    result = runner.invoke(
        app,
        [
            "read-page",
            "--url",
            fixture_server,
            "--method",
            "browser",
            "--actions-json",
            json.dumps([action]),
            "--output-dir",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "actions.invalid"
    assert payload["attempts"] == []
    assert _FixtureHandler.requests == []


@pytest.mark.parametrize("command", ["read-page", "collect"])
@pytest.mark.parametrize(
    ("action_type", "selector"),
    [
        ("click", ""),
        ("click", " \t "),
        ("wait-selector", ""),
        ("wait-selector", " \t "),
        ("scroll", ""),
        ("scroll", " \t "),
    ],
)
def test_blank_browser_action_selectors_fail_before_capture_or_target_io(
    fixture_server, tmp_path: Path, monkeypatch, command: str, action_type: str, selector: str
):
    browser_capture_calls: list[dict[str, Any]] = []

    def unexpected_browser_capture(**kwargs):
        browser_capture_calls.append(kwargs)
        raise RuntimeError("browser capture must not start for invalid actions")

    monkeypatch.setattr(exploration, "_browser_capture", unexpected_browser_capture)
    action = {"type": action_type, "selector": selector}
    if command == "read-page":
        args = [
            "read-page", "--url", fixture_server, "--method", "browser",
            "--actions-json", json.dumps([action]), "--output-dir", str(tmp_path), "--json",
        ]
    else:
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
        args = [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method="browser"),
            "--actions-json", json.dumps([action]),
        ]

    result = runner.invoke(app, args)

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "actions.invalid"
    assert payload["next_action"] == "correct_actions"
    assert payload["attempts"] == []
    assert browser_capture_calls == []
    assert _FixtureHandler.requests == []


def test_browser_action_selector_validation_preserves_nonblank_text():
    selector = "  #target  "
    actions = exploration._load_actions(json.dumps([{"type": "click", "selector": selector}]), None)
    assert actions[0]["selector"] == selector


@pytest.mark.parametrize("command", ["read-page", "collect"])
@pytest.mark.parametrize("text", ["", " \t "])
def test_blank_wait_text_fails_before_browser_capture_or_target_io(
    fixture_server, tmp_path: Path, monkeypatch, command: str, text: str
):
    browser_capture_calls: list[dict[str, Any]] = []

    def unexpected_browser_capture(**kwargs):
        browser_capture_calls.append(kwargs)
        raise RuntimeError("browser capture must not start for invalid actions")

    monkeypatch.setattr(exploration, "_browser_capture", unexpected_browser_capture)
    actions = json.dumps([{"type": "wait-text", "text": text}])
    if command == "read-page":
        args = [
            "read-page", "--url", fixture_server, "--method", "browser", "--actions-json", actions,
            "--output-dir", str(tmp_path), "--json",
        ]
    else:
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
        args = [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method="browser"),
            "--actions-json", actions,
        ]

    result = runner.invoke(app, args)

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "actions.invalid"
    assert payload["next_action"] == "correct_actions"
    assert payload["attempts"] == []
    assert browser_capture_calls == []
    assert _FixtureHandler.requests == []


def test_actions_file_accepts_utf8_bom(tmp_path: Path):
    actions_file = tmp_path / "actions.json"
    action = {"type": "wait-text", "text": "  FAQ answer  "}
    actions_file.write_bytes(b"\xef\xbb\xbf" + json.dumps([action]).encode("utf-8"))
    assert exploration._load_actions("", actions_file) == [action]


@pytest.mark.parametrize("command", ["read-page", "collect"])
def test_actions_file_with_non_utf8_bytes_fails_before_target_io(
    fixture_server, tmp_path: Path, monkeypatch, command: str
):
    actions_file = tmp_path / "actions.json"
    actions_file.write_bytes(b"\xff\xfe\x00\x00")
    browser_capture_calls: list[dict[str, Any]] = []

    def unexpected_browser_capture(**kwargs):
        browser_capture_calls.append(kwargs)
        raise RuntimeError("browser capture must not start for invalid actions")

    monkeypatch.setattr(exploration, "_browser_capture", unexpected_browser_capture)
    if command == "read-page":
        args = [
            "read-page", "--url", fixture_server, "--method", "browser", "--actions-file", str(actions_file),
            "--output-dir", str(tmp_path), "--json",
        ]
    else:
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
        args = [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method="browser"),
            "--actions-file", str(actions_file),
        ]

    result = runner.invoke(app, args)

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "actions.invalid"
    assert payload["next_action"] == "correct_actions"
    assert payload["attempts"] == []
    assert browser_capture_calls == []
    assert _FixtureHandler.requests == []


def test_http_403_is_preserved_and_browser_retry_is_explicit(fixture_server, tmp_path: Path):
    url = f"{fixture_server}/http-forbidden"
    http = runner.invoke(
        app,
        ["read-page", "--url", url, "--output-dir", str(tmp_path), "--json"],
    )
    assert http.exit_code != 0
    http_payload = _payload(http)
    assert http_payload["error"]["code"] == "http.status"
    assert http_payload["attempts"][0]["status"] == 403
    assert "does not establish" in http_payload["error"]["message"]
    assert http_payload["next_action"] == "retry_with_browser"


def test_browser_can_retry_http_403_target(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    url = f"{fixture_server}/http-forbidden"
    browser = runner.invoke(
        app,
        [
            "read-page",
            "--url",
            url,
            "--method",
            "browser",
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "5",
            "--json",
        ],
    )
    assert browser.exit_code == 0, browser.stdout + browser.stderr
    browser_payload = _payload(browser)
    assert "Browser route works" in browser_payload["data"]["text"]
    assert browser_payload["data"]["browser_channel"] == "bundled"
    assert browser_payload["data"]["headless"] is True


def test_collect_mixed_items_continue_and_append_manifest_rows(fixture_server, tmp_path: Path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n{fixture_server}/http-forbidden\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    custom_user_agent = "web-listening-collect-test/1.0"
    args = _collect_args(
        urls_file,
        output_dir,
        kind="page",
        method="http",
        user_agent=custom_user_agent,
    )

    first = runner.invoke(app, args)
    assert first.exit_code != 0
    first_payload = _payload(first)
    assert first_payload["ok"] is True
    assert first_payload["data_status"] == "partial"
    assert first_payload["data"]["requested_count"] == 2
    assert first_payload["data"]["success_count"] == 1
    assert first_payload["data"]["failed_count"] == 1
    assert first_payload["data"]["user_agent"] == custom_user_agent
    assert _FixtureHandler.user_agents == [custom_user_agent, custom_user_agent]
    assert first_payload["next_action"] == "retry_with_browser"
    failed_url = f"{fixture_server}/http-forbidden"
    assert first_payload["data"]["failed_urls"] == [failed_url]
    failed_path = Path(first_payload["artifacts"]["failed_urls"])
    assert failed_path.read_text(encoding="utf-8") == failed_url + "\n"
    manifest = Path(first_payload["artifacts"]["manifest"])
    rows_first = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert len(rows_first) == 2
    assert [row["user_agent"] for row in rows_first] == [custom_user_agent, custom_user_agent]
    first_stage = json.loads(Path(rows_first[0]["stages"][0]["result_ref"]).read_text(encoding="utf-8"))
    assert first_stage["data"]["user_agent"] == custom_user_agent
    assert first_stage["attempts"][0]["user_agent"] == custom_user_agent
    assert [row["outcome"] for row in rows_first] == ["success", "failed"]
    assert rows_first[1]["stages"][0]["error_code"] == "http.status"
    assert all(Path(row["result_ref"]).is_file() for row in rows_first)
    assert all(Path(row["stages"][0]["result_ref"]).is_file() for row in rows_first)
    assert _payload(runner.invoke(app, ["read-page", "--url", failed_url, "--output-dir", str(output_dir), "--json"]))["error"]["code"] == "http.status"

    second = runner.invoke(app, args)
    assert second.exit_code != 0
    second_payload = _payload(second)
    rows_all = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert len(rows_all) == 4
    assert len({row["invocation_id"] for row in rows_all}) == 2
    assert len({row["batch_id"] for row in rows_all}) == 2
    assert second_payload["data"]["requested_count"] == 2
    assert [row["url"] for row in rows_all] == [f"{fixture_server}/", failed_url] * 2


def test_collect_adds_line_boundary_before_appending_to_manifest_without_final_lf(fixture_server, tmp_path: Path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
    manifest = tmp_path / "manifest.jsonl"
    old_line = json.dumps({"prior": "row"}, separators=(",", ":")).encode("utf-8")
    manifest.write_bytes(old_line)

    result = runner.invoke(
        app,
        [*_collect_args(urls_file, tmp_path / "out", kind="page", method="http"), "--manifest", str(manifest)],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    manifest_bytes = manifest.read_bytes()
    assert manifest_bytes.startswith(old_line + b"\n")
    rows = [json.loads(line) for line in manifest_bytes.splitlines()]
    assert rows[0] == {"prior": "row"}
    assert len(rows) == 2
    assert rows[1]["url"] == f"{fixture_server}/"


def test_collect_passes_existing_actions_to_each_browser_page(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
    actions_file = tmp_path / "actions.json"
    actions_file.write_text(
        json.dumps([
            {"type": "click", "selector": "button#faq"},
            {"type": "wait-text", "text": "FAQ answer"},
        ]),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method="browser"),
            "--actions-file", str(actions_file),
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    row = json.loads(Path(payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()[0])
    assert row["max_requests"] == 100
    page_result = json.loads(Path(row["stages"][0]["result_ref"]).read_text(encoding="utf-8"))
    assert "FAQ answer" in page_result["data"]["text"]
    assert [step["status"] for step in page_result["data"]["actions"]] == ["completed", "completed"]


def test_collect_rejects_bad_actions_before_any_target_request(fixture_server, tmp_path: Path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method="browser"),
            "--actions-json", '[{"type":"eval"}]',
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "actions.invalid"
    assert payload["next_action"] == "correct_actions"
    assert payload["attempts"] == []
    assert _FixtureHandler.requests == []


def test_browser_request_ledger_preserves_domain_policy_block(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    result = runner.invoke(
        app,
        [
            "read-page", "--url", f"{fixture_server}/policy-frame", "--method", "browser",
            "--output-dir", str(tmp_path), "--timeout-seconds", "5", "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    blocked = next(
        attempt for attempt in payload["attempts"]
        if attempt["requested_url"] == "http://blocked.test/frame"
    )
    assert blocked["policy_block"] == "navigation_domain_not_allowed"
    assert "transport_failure" in blocked
    assert payload["next_action"] == "inspect_request_ledger_and_allow_required_public_navigation_domain"
    assert payload["data_quality"]["passed"] is True


def test_browser_request_budget_has_specific_recovery(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    result = runner.invoke(
        app,
        [
            "read-page", "--url", fixture_server, "--method", "browser", "--max-requests", "1",
            "--output-dir", str(tmp_path), "--timeout-seconds", "5", "--json",
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "browser.request_limit"
    assert payload["data"]["request_limit_hit"] is True
    assert payload["next_action"] == "retry_single_read_page_with_max_requests_or_reduce_page_load"


def test_browser_action_navigation_request_budget_failure_is_classified(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    result = runner.invoke(
        app,
        [
            "read-page", "--url", f"{fixture_server}/navigate-200", "--method", "browser",
            "--max-requests", "1", "--browser-channel", "bundled", "--headed",
            "--actions-json", '[{"type":"click","selector":"#next"}]',
            "--output-dir", str(tmp_path), "--timeout-seconds", "5", "--json",
        ],
    )
    assert result.exit_code != 0, result.stdout + result.stderr
    payload = _payload(result)
    assert payload["error"]["code"] == "browser.request_limit"
    assert payload["data"]["request_limit_hit"] is True
    assert payload["next_action"] == "retry_single_read_page_with_max_requests_or_reduce_page_load"
    redirected = next(
        attempt for attempt in payload["attempts"]
        if attempt["requested_url"].endswith("/destination-200")
    )
    assert redirected["policy_block"] == "request_limit"
    assert payload["data"]["browser_channel"] == "bundled"
    assert payload["data"]["browser_version"]
    assert payload["data"]["headless"] is False


def test_collect_browser_action_failure_keeps_browser_provenance(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
    actions_file = tmp_path / "actions.json"
    actions_file.write_text(
        json.dumps([{"type": "wait-selector", "selector": "#missing-control"}]),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            *_collect_args(urls_file, tmp_path / "out", kind="page", method="browser"),
            "--actions-file", str(actions_file),
        ],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["data"]["failed_count"] == 1
    assert payload["next_action"] == "retry_within_timeout_limit"
    row = json.loads(Path(payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()[0])
    stage = row["stages"][0]
    stage_payload = json.loads(Path(stage["result_ref"]).read_text(encoding="utf-8"))
    assert stage_payload["error"]["code"] == "browser.action_timeout"
    assert stage_payload["data"]["browser_channel"] == "bundled"
    assert stage_payload["data"]["browser_version"]
    assert stage_payload["data"]["headless"] is True
    assert stage["browser_channel"] == "bundled"
    assert stage["browser_version"] == stage_payload["data"]["browser_version"]
    assert stage["headless"] is True
    assert row["browser_version"] == stage["browser_version"]
    assert row["browser_channel"] == stage["browser_channel"]
    assert row["headless"] is True


def test_collect_request_limit_failed_urls_retry_with_higher_budget(fixture_server, tmp_path: Path):
    _require_browser_runtime()
    urls_file = tmp_path / "urls.txt"
    url = f"{fixture_server}/navigate-200"
    urls_file.write_text(url + "\n", encoding="utf-8")
    actions_file = tmp_path / "actions.json"
    actions_file.write_text(
        json.dumps([{"type": "click", "selector": "#next"}]),
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    first = runner.invoke(
        app,
        [
            *_collect_args(urls_file, output_dir, kind="page", method="browser"),
            "--max-requests", "1", "--actions-file", str(actions_file),
        ],
    )
    assert first.exit_code != 0, first.stdout + first.stderr
    first_payload = _payload(first)
    assert first_payload["data"]["items"][0]["max_requests"] == 1
    assert first_payload["data"]["failed_items"][0]["next_action"] == (
        "retry_collect_failed_urls_with_higher_max_requests_or_reduce_page_load"
    )
    assert first_payload["next_action"] == "retry_collect_failed_urls_with_higher_max_requests_or_reduce_page_load"
    failed_urls = Path(first_payload["artifacts"]["failed_urls"])
    manifest = Path(first_payload["artifacts"]["manifest"])
    first_row = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
    assert first_row["max_requests"] == 1
    assert first_row["stages"][0]["max_requests"] == 1
    first_stage = json.loads(Path(first_row["stages"][0]["result_ref"]).read_text(encoding="utf-8"))
    assert first_stage["data"]["request_limit_hit"] is True

    retry = runner.invoke(
        app,
        [
            *_collect_args(failed_urls, output_dir, kind="page", method="browser"),
            "--max-requests", "10", "--actions-file", str(actions_file),
        ],
    )
    assert retry.exit_code == 0, retry.stdout + retry.stderr
    retry_payload = _payload(retry)
    assert retry_payload["data"]["success_count"] == 1
    assert retry_payload["data"]["items"][0]["max_requests"] == 10
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert [row["url"] for row in rows] == [url, url]
    assert [row["max_requests"] for row in rows] == [1, 10]
    retry_stage = json.loads(Path(rows[1]["stages"][0]["result_ref"]).read_text(encoding="utf-8"))
    assert retry_stage["data"]["request_count"] <= 10
    assert "Final destination content" in retry_stage["data"]["text"]


@pytest.mark.parametrize("max_requests", ["0", "201"])
def test_collect_rejects_out_of_range_request_budget_before_target_io(
    fixture_server, tmp_path: Path, max_requests: str
):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            *_collect_args(urls_file, output_dir, kind="page", method="http"),
            "--max-requests", max_requests,
        ],
    )
    assert result.exit_code != 0
    assert _FixtureHandler.requests == []
    assert not output_dir.exists()


def test_collect_validates_all_urls_before_any_target_request(fixture_server, tmp_path: Path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\nftp://example.test/file\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    result = runner.invoke(app, _collect_args(urls_file, output_dir, kind="page", method="http"))
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "collect.url_invalid"
    assert payload["next_action"] == "correct_invalid_urls_before_retrying"
    assert payload["attempts"] == []
    assert _FixtureHandler.requests == []
    assert not output_dir.exists()


def test_collect_failed_http_urls_retry_in_browser_without_replacing_history(
    fixture_server, tmp_path: Path
):
    _require_browser_runtime()
    urls_file = tmp_path / "urls.txt"
    failed_url = f"{fixture_server}/http-forbidden"
    urls_file.write_text(failed_url + "\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    http = runner.invoke(app, _collect_args(urls_file, output_dir, kind="page", method="http"))
    assert http.exit_code != 0
    http_payload = _payload(http)
    assert http_payload["data"]["failed_urls"] == [failed_url]
    manifest = Path(http_payload["artifacts"]["manifest"])
    failed_urls = Path(http_payload["artifacts"]["failed_urls"])

    browser_args = _collect_args(failed_urls, output_dir, kind="page", method="browser")
    browser_args.extend(["--browser-channel", "bundled"])
    browser = runner.invoke(app, browser_args)
    assert browser.exit_code == 0, browser.stdout + browser.stderr
    browser_payload = _payload(browser)
    assert browser_payload["data_status"] == "present"
    assert browser_payload["data"]["success_count"] == 1
    rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["url"] == rows[1]["url"] == failed_url
    assert rows[0]["method"] == "http" and rows[1]["method"] == "browser"
    assert rows[0]["invocation_id"] != rows[1]["invocation_id"]
    assert rows[0]["stages"][0]["error_code"] == "http.status"
    assert rows[1]["stages"][0]["quality_passed"] is True
    assert "Browser route works" in json.loads(Path(rows[1]["stages"][0]["result_ref"]).read_text(encoding="utf-8"))["data"]["text"]


def test_collect_file_download_then_pdf_parse_preserves_source_and_reuses_artifacts(
    fixture_server, tmp_path: Path
):
    urls_file = tmp_path / "pdf-urls.txt"
    pdf_urls = [f"{fixture_server}/document.pdf", f"{fixture_server}/document-copy.pdf"]
    urls_file.write_text("\n".join(pdf_urls) + "\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    args = _collect_args(urls_file, output_dir, kind="file", method="http", parse_pdf=True)

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stdout + first.stderr
    first_payload = _payload(first)
    assert first_payload["data"]["requested_count"] == 2
    assert first_payload["data"]["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT
    rows_first = [json.loads(line) for line in Path(first_payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()]
    assert all(row["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT for row in rows_first)
    assert all([stage["tool"] for stage in row["stages"]] == ["download-file", "extract-pdf"] for row in rows_first)
    first_downloads = [json.loads(Path(row["stages"][0]["result_ref"]).read_text(encoding="utf-8")) for row in rows_first]
    first_parses = [json.loads(Path(row["stages"][1]["result_ref"]).read_text(encoding="utf-8")) for row in rows_first]
    assert [item["data"]["artifact_reused"] for item in first_downloads] == [False, True]
    assert all(item["attempts"][0]["pacing_wait_seconds"] == 0 for item in first_downloads)
    assert [item["data"]["source_url"] for item in first_parses] == pdf_urls
    assert [item["data"]["page_count"] for item in first_parses] == [2, 2]
    assert all(item["data"]["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT for item in first_downloads)
    assert all(item["attempts"][0]["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT for item in first_downloads)
    assert all(
        json.loads(Path(item["artifacts"]["source_metadata"]).read_text(encoding="utf-8"))["user_agent"]
        == exploration.DEFAULT_HTTP_USER_AGENT
        for item in first_downloads
    )
    assert _FixtureHandler.user_agents == [exploration.DEFAULT_HTTP_USER_AGENT] * 2
    first_file = first_downloads[0]["artifacts"]["file"]
    assert first_downloads[1]["artifacts"]["file"] == first_file

    second = runner.invoke(app, args)
    assert second.exit_code == 0
    second_payload = _payload(second)
    rows = [json.loads(line) for line in Path(second_payload["artifacts"]["manifest"]).read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    second_downloads = [json.loads(Path(row["stages"][0]["result_ref"]).read_text(encoding="utf-8")) for row in rows[2:]]
    second_parses = [json.loads(Path(row["stages"][1]["result_ref"]).read_text(encoding="utf-8")) for row in rows[2:]]
    assert all(item["data"]["artifact_reused"] is True for item in second_downloads)
    assert all(item["artifacts"]["file"] == first_file for item in second_downloads)
    assert all(item["data"]["artifact_reused"] is True for item in second_parses)


def test_collect_rejects_parse_pdf_for_pages_before_network(fixture_server, tmp_path: Path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text(f"{fixture_server}/\n", encoding="utf-8")
    result = runner.invoke(
        app,
        [*_collect_args(urls_file, tmp_path / "out", kind="page", method="http"), "--parse-pdf"],
    )
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "collect.parse_pdf_invalid"
    assert payload["attempts"] == []
    assert _FixtureHandler.requests == []


@pytest.mark.parametrize("command,path,error_code", [
    ("read-page", "/slow-page", "http.timeout"),
    ("download-file", "/slow-file", "download.timeout"),
])
def test_http_operations_enforce_overall_wall_clock_timeout(
    fixture_server, tmp_path: Path, command: str, path: str, error_code: str
):
    started = time.monotonic()
    if command == "read-page":
        args = [command, "--url", f"{fixture_server}{path}", "--output-dir", str(tmp_path), "--timeout-seconds", "1", "--json"]
    else:
        args = [command, "--url", f"{fixture_server}{path}", "--output-dir", str(tmp_path), "--timeout-seconds", "1", "--pacing-seconds", "0", "--jitter-seconds", "0", "--json"]
    result = runner.invoke(app, args)
    elapsed = time.monotonic() - started
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == error_code
    assert elapsed < 2.5
    if command == "download-file":
        assert not list((tmp_path / "downloads").glob("*.part"))
        assert not list((tmp_path / "downloads").glob(".download-*.part"))


@pytest.mark.parametrize(
    ("command", "path", "error_code"),
    [
        ("read-page", "/deadline-page", "http.timeout"),
        ("download-file", "/deadline-file", "download.timeout"),
    ],
)
def test_http_body_idle_timeout_cannot_extend_overall_deadline(
    fixture_server, tmp_path: Path, command: str, path: str, error_code: str
):
    started = time.monotonic()
    args = [
        command,
        "--url",
        f"{fixture_server}{path}",
        "--output-dir",
        str(tmp_path),
        "--timeout-seconds",
        "1",
        "--json",
    ]
    if command == "download-file":
        args.extend(["--pacing-seconds", "0", "--jitter-seconds", "0"])
    result = runner.invoke(app, args)
    elapsed = time.monotonic() - started

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == error_code
    assert payload["attempts"][0]["status"] == 200
    assert elapsed < 1.6
    if command == "download-file":
        assert not list((tmp_path / "downloads").glob("*.part"))
        assert not list((tmp_path / "downloads").glob(".download-*.part"))


def test_download_pacing_consumes_overall_deadline_before_request(fixture_server, tmp_path: Path):
    started = time.monotonic()
    result = runner.invoke(
        app,
        [
            "download-file",
            "--url",
            f"{fixture_server}/document.pdf",
            "--output-dir",
            str(tmp_path),
            "--timeout-seconds",
            "1",
            "--pacing-seconds",
            "2",
            "--jitter-seconds",
            "0",
            "--json",
        ],
    )
    elapsed = time.monotonic() - started

    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["error"]["code"] == "download.timeout"
    assert payload["attempts"] == []
    assert elapsed < 1.6
    assert _FixtureHandler.requests == []


def test_http_200_challenge_is_blocked_but_plain_cloudflare_mention_is_not(
    fixture_server, tmp_path: Path
):
    challenge = runner.invoke(
        app,
        ["read-page", "--url", f"{fixture_server}/challenge", "--output-dir", str(tmp_path), "--json"],
    )
    assert challenge.exit_code == 0
    blocked = _payload(challenge)
    assert blocked["data_status"] == "blocked"
    assert blocked["data_quality"]["passed"] is False
    assert blocked["data_quality"]["blocked"] is True
    assert blocked["stop_reason"] == "blocked"
    assert blocked["next_action"] == "retry_with_browser"
    html = Path(blocked["artifacts"]["page_html"]).read_text(encoding="utf-8")
    assert "Please verify you are human" in html
    assert blocked["attempts"][0]["status"] == 200

    ordinary = runner.invoke(
        app,
        ["read-page", "--url", f"{fixture_server}/cloudflare-article", "--output-dir", str(tmp_path), "--json"],
    )
    assert ordinary.exit_code == 0
    accepted = _payload(ordinary)
    assert accepted["data_status"] == "present"
    assert accepted["data_quality"]["passed"] is True


def test_download_large_pdf_hash_source_sidecar_and_idempotent_rerun(fixture_server, tmp_path: Path):
    args = [
        "download-file",
        "--url",
        f"{fixture_server}/document.pdf",
        "--output-dir",
        str(tmp_path),
        "--pacing-seconds",
        "0",
        "--jitter-seconds",
        "0",
        "--json",
    ]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.stdout + first.stderr
    first_payload = _payload(first)
    assert first_payload["data"]["artifact_reused"] is False
    artifact = Path(first_payload["artifacts"]["file"])
    source_sidecar = Path(first_payload["artifacts"]["source_metadata"])
    assert artifact.stat().st_size > 4 * 1024 * 1024
    assert first_payload["data"]["sha256"] == __import__("hashlib").sha256(artifact.read_bytes()).hexdigest()
    assert first_payload["attempts"][0]["requested_at"] == first_payload["data"]["request_started_at"]
    assert first_payload["data"]["mime_type"] == "application/pdf"
    assert json.loads(source_sidecar.read_text(encoding="utf-8"))["source_url"].endswith("/document.pdf")
    assert first_payload["data"]["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT
    assert json.loads(source_sidecar.read_text(encoding="utf-8"))["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT
    assert first_payload["attempts"][0]["user_agent"] == exploration.DEFAULT_HTTP_USER_AGENT
    second = runner.invoke(app, args)
    assert second.exit_code == 0
    second_payload = _payload(second)
    assert second_payload["artifacts"]["file"] == str(artifact)
    assert second_payload["data"]["artifact_reused"] is True
    assert len(list((tmp_path / "downloads").glob("*.pdf"))) == 1
    assert PdfReader(str(artifact), strict=True).pages[0].extract_text().strip() == "Hello world"


def test_download_custom_user_agent_survives_redirect_and_is_recorded(fixture_server, tmp_path: Path):
    custom = "web-listening-evaluation/1.0"
    result = runner.invoke(
        app,
        [
            "download-file", "--url", f"{fixture_server}/redirect-pdf",
            "--user-agent", custom, "--output-dir", str(tmp_path),
            "--pacing-seconds", "0", "--jitter-seconds", "0", "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    sidecar = json.loads(Path(payload["artifacts"]["source_metadata"]).read_text(encoding="utf-8"))
    assert _FixtureHandler.user_agents == [custom, custom]
    assert [attempt["user_agent"] for attempt in payload["attempts"]] == [custom, custom]
    assert payload["data"]["user_agent"] == custom
    assert sidecar["user_agent"] == custom
    assert sidecar["redirects"][0]["to_url"].endswith("/document.pdf")


def test_download_reports_pdf_error_pages_truncation_limits_and_cleans_part_files(fixture_server, tmp_path: Path):
    base = ["download-file", "--output-dir", str(tmp_path), "--pacing-seconds", "0", "--jitter-seconds", "0", "--json"]
    html_result = runner.invoke(app, [*base, "--url", f"{fixture_server}/error.pdf"])
    assert html_result.exit_code != 0
    assert _payload(html_result)["error"]["code"] == "download.html_response"
    truncated = runner.invoke(app, [*base, "--url", f"{fixture_server}/short.pdf"])
    assert truncated.exit_code != 0
    assert _payload(truncated)["error"]["code"] == "download.pdf_truncated"
    signed = runner.invoke(app, [*base, "--url", f"{fixture_server}/signed"])
    assert signed.exit_code != 0
    assert _payload(signed)["error"]["code"] == "download.pdf_truncated"
    partial = runner.invoke(app, [*base, "--url", f"{fixture_server}/partial.bin"])
    assert partial.exit_code != 0
    assert _payload(partial)["error"]["code"] == "download.partial_response"
    oversized = runner.invoke(app, [*base, "--url", f"{fixture_server}/document.pdf", "--max-bytes", "1024"])
    assert oversized.exit_code != 0
    assert _payload(oversized)["error"]["code"] == "download.too_large"
    assert not list((tmp_path / "downloads").glob("*.part"))
    assert not list((tmp_path / "downloads").glob(".download-*.part"))
    assert not list((tmp_path / "downloads").glob("*.bin"))


def test_download_pacing_timestamp_is_after_wait(fixture_server, tmp_path: Path):
    before = time.monotonic()
    result = exploration.download_file(
        url=f"{fixture_server}/large.bin",
        output_dir=tmp_path,
        allowed_domains=None,
        timeout_seconds=10,
        max_bytes=2 * 1024 * 1024,
        pacing_seconds=0.05,
        jitter_seconds=0,
    )
    elapsed = time.monotonic() - before
    assert result.ok
    assert elapsed >= 0.05
    request_start = result.attempts[0]["requested_at"]
    assert request_start == result.data["request_started_at"]


def test_pdf_extracts_pages_inherits_download_url_and_reuses_deterministic_output(fixture_server, tmp_path: Path):
    downloaded = runner.invoke(
        app,
        ["download-file", "--url", f"{fixture_server}/document.pdf", "--output-dir", str(tmp_path), "--pacing-seconds", "0", "--jitter-seconds", "0", "--json"],
    )
    source = _payload(downloaded)
    result = runner.invoke(
        app,
        ["extract-pdf", "--input", source["artifacts"]["file"], "--output-dir", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = _payload(result)
    output_path = Path(payload["artifacts"]["page_text"])
    extracted = json.loads(output_path.read_text(encoding="utf-8"))
    assert extracted["source_url"] == f"{fixture_server}/document.pdf"
    assert extracted["source_sha256"] == source["data"]["sha256"]
    assert extracted["page_count"] == 2
    assert extracted["empty_text_page_count"] == 1
    assert extracted["page_error_count"] == 0
    assert extracted["status"] == "present"
    assert extracted["pages"][0]["text"].strip() == "Hello world"
    repeated = runner.invoke(
        app,
        ["extract-pdf", "--input", source["artifacts"]["file"], "--output-dir", str(tmp_path), "--json"],
    )
    assert repeated.exit_code == 0
    assert _payload(repeated)["data"]["artifact_reused"] is True
    assert len(list((tmp_path / "extracted").glob("*.json"))) == 1
    source_b = "https://mirror.example.gov/reports/annual.pdf"
    overridden = runner.invoke(
        app,
        [
            "extract-pdf",
            "--input",
            source["artifacts"]["file"],
            "--source-url",
            source_b,
            "--output-dir",
            str(tmp_path),
            "--json",
        ],
    )
    assert overridden.exit_code == 0, overridden.stdout + overridden.stderr
    override_payload = _payload(overridden)
    assert override_payload["data"]["source_url"] == source_b
    override_document = json.loads(
        Path(override_payload["artifacts"]["page_text"]).read_text(encoding="utf-8")
    )
    assert override_document["pages"] == extracted["pages"]
    assert override_payload["artifacts"]["page_text"] != payload["artifacts"]["page_text"]
    assert len(list((tmp_path / "extracted").glob("*.json"))) == 2


def test_pdf_truncation_and_page_errors_are_not_reported_as_complete(tmp_path: Path, monkeypatch):
    valid = tmp_path / "valid.pdf"
    valid.write_bytes(_text_pdf())
    truncated = tmp_path / "truncated.pdf"
    truncated.write_bytes(valid.read_bytes()[:-16])
    bad = exploration.extract_pdf(input_path=truncated, output_dir=tmp_path, source_url=None)
    assert bad.error is not None and bad.error.code == "pdf.truncated"
    oversized = exploration.extract_pdf(
        input_path=valid, output_dir=tmp_path, source_url=None, max_bytes=1024
    )
    assert oversized.error is not None and oversized.error.code == "pdf.too_large"

    import pypdf

    class BrokenPage:
        def extract_text(self):
            raise RuntimeError("fixture error")

    class BrokenReader:
        is_encrypted = False
        metadata = {}
        pages = [BrokenPage()]

    monkeypatch.setattr(pypdf, "PdfReader", lambda *_args, **_kwargs: BrokenReader())
    partial = exploration.extract_pdf(input_path=valid, output_dir=tmp_path / "partial", source_url=None)
    assert partial.ok
    assert partial.data_status == "partial"
    assert partial.data_quality.passed is False
    assert partial.data_quality.failure_reasons == ["one_or_more_pages_failed_text_extraction"]
    assert partial.data["page_error_count"] == 1
    assert partial.data["page_count"] == 1


@pytest.mark.parametrize(
    "failure,browser_channel,headed,expected_launch",
    [
        (RuntimeError("browser fixture failure"), "bundled", False, {"headless": True}),
        (RuntimeError("browser fixture failure"), "chromium", False, {"headless": True, "channel": "chromium"}),
        (RuntimeError("browser fixture failure"), "msedge", True, {"headless": False, "channel": "msedge"}),
        (KeyboardInterrupt(), "bundled", False, {"headless": True}),
    ],
)
def test_browser_failure_cancellation_and_launch_options(monkeypatch, failure, browser_channel, headed, expected_launch):
    calls: list[str] = []
    launch_options: list[dict[str, Any]] = []

    class FakePage:
        def set_default_timeout(self, *_args):
            pass

        def on(self, *_args):
            pass

        def goto(self, *_args, **_kwargs):
            raise failure

        def close(self):
            calls.append("page")

    class FakeContext:
        def route(self, *_args):
            pass

        def new_page(self):
            return FakePage()

        def close(self):
            calls.append("context")

    class FakeBrowser:
        def new_context(self):
            return FakeContext()

        def close(self):
            calls.append("browser")

    class FakeChromium:
        def launch(self, **kwargs):
            launch_options.append(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            calls.append("playwright")

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.TimeoutError = type("PlaywrightTimeoutError", (TimeoutError,), {})
    sync_api_module.sync_playwright = lambda: FakeStarter()
    playwright_module.sync_api = sync_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            exploration._browser_capture(
                url="https://example.test", domains=["example.test"], actions=[], wait_until="load",
                timeout_seconds=1, max_requests=10, max_dom_bytes=1024,
                browser_channel=browser_channel, headed=headed,
            )
    else:
        with pytest.raises(exploration.ExplorationError):
            exploration._browser_capture(
                url="https://example.test", domains=["example.test"], actions=[], wait_until="load",
                timeout_seconds=1, max_requests=10, max_dom_bytes=1024,
                browser_channel=browser_channel, headed=headed,
            )
    assert calls == ["page", "context", "browser", "playwright"]
    assert len(launch_options) == 1
    assert {key: value for key, value in launch_options[0].items() if key != "timeout"} == expected_launch
    assert 1 <= launch_options[0]["timeout"] <= 1000


def _install_fake_browser_modules(monkeypatch, starter, timeout_error):
    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.TimeoutError = timeout_error
    sync_api_module.sync_playwright = lambda: starter
    playwright_module.sync_api = sync_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api_module)


def test_browser_launch_and_navigation_share_one_deadline(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(exploration.time, "monotonic", lambda: clock[0])
    timeout_error = type("TimeoutError", (TimeoutError,), {})
    launch_options: list[dict[str, Any]] = []
    navigation_timeouts: list[int] = []
    cleanup: list[str] = []

    class FakePage:
        def set_default_timeout(self, *_args):
            pass

        def on(self, *_args):
            pass

        def goto(self, *_args, **kwargs):
            navigation_timeouts.append(kwargs["timeout"])
            raise timeout_error("navigation timed out")

        def close(self):
            cleanup.append("page")

    class FakeContext:
        def route(self, *_args):
            pass

        def new_page(self):
            return FakePage()

        def close(self):
            cleanup.append("context")

    class FakeBrowser:
        def new_context(self):
            return FakeContext()

        def close(self):
            cleanup.append("browser")

    class FakeChromium:
        def launch(self, **kwargs):
            launch_options.append(kwargs)
            clock[0] += 3
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            cleanup.append("playwright")

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    _install_fake_browser_modules(monkeypatch, FakeStarter(), timeout_error)

    with pytest.raises(exploration.ExplorationError) as captured:
        exploration._browser_capture(
            url="https://example.test", domains=["example.test"], actions=[], wait_until="load",
            timeout_seconds=10, max_requests=10, max_dom_bytes=1024,
        )

    assert captured.value.code == "browser.timeout"
    assert launch_options[0]["timeout"] == 10_000
    assert navigation_timeouts == [7_000]
    assert cleanup == ["page", "context", "browser", "playwright"]


def test_browser_launch_timeout_is_not_reported_as_missing_binary(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(exploration.time, "monotonic", lambda: clock[0])
    timeout_error = type("TimeoutError", (TimeoutError,), {})
    launch_options: list[dict[str, Any]] = []
    cleanup: list[str] = []

    class FakeChromium:
        def launch(self, **kwargs):
            launch_options.append(kwargs)
            clock[0] += 1
            raise timeout_error("launch timed out")

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            cleanup.append("playwright")

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    _install_fake_browser_modules(monkeypatch, FakeStarter(), timeout_error)

    with pytest.raises(exploration.ExplorationError) as captured:
        exploration._browser_capture(
            url="https://example.test", domains=["example.test"], actions=[], wait_until="load",
            timeout_seconds=10, max_requests=10, max_dom_bytes=1024,
        )

    assert captured.value.code == "browser.timeout"
    assert launch_options[0]["timeout"] == 10_000
    assert cleanup == ["playwright"]


def test_browser_wait_larger_than_remaining_budget_is_recorded_as_timeout(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(exploration.time, "monotonic", lambda: clock[0])
    timeout_error = type("TimeoutError", (TimeoutError,), {})
    waited_ms: list[int] = []
    response = types.SimpleNamespace(status=200, url="https://example.test", headers={"content-type": "text/html"})

    class FakePage:
        url = "https://example.test"

        def set_default_timeout(self, *_args):
            pass

        def on(self, *_args):
            pass

        def goto(self, *_args, **_kwargs):
            return response

        def wait_for_timeout(self, milliseconds):
            waited_ms.append(milliseconds)
            clock[0] += milliseconds / 1000

        def title(self):
            return "fixture"

        def content(self):
            return "<html><body>fixture</body></html>"

        def close(self):
            pass

    class FakeContext:
        def route(self, *_args):
            pass

        def new_page(self):
            return FakePage()

        def close(self):
            pass

    class FakeBrowser:
        def new_context(self):
            return FakeContext()

        def close(self):
            pass

    class FakeChromium:
        def launch(self, **_kwargs):
            clock[0] += 8
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def stop(self):
            pass

    class FakeStarter:
        def start(self):
            return FakePlaywright()

    _install_fake_browser_modules(monkeypatch, FakeStarter(), timeout_error)

    with pytest.raises(exploration.ExplorationError) as captured:
        exploration._browser_capture(
            url="https://example.test", domains=["example.test"],
            actions=[{"type": "wait", "seconds": 5}], wait_until="load",
            timeout_seconds=10, max_requests=10, max_dom_bytes=1024,
        )

    assert captured.value.code == "browser.timeout"
    assert waited_ms == [2_000]
    action = captured.value.data["actions"][0]
    assert action["status"] == "timeout"
    assert action["timeout_reason"] == "overall_deadline"
    assert action["wait_requested_ms"] == 5_000
    assert action["waited_ms"] == 2_000
