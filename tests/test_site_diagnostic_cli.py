from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from web_listening.blocks.site_diagnostic import load_site_diagnostic
from web_listening.cli import app


runner = CliRunner()


def test_diagnose_site_json_writes_the_stable_artifact(monkeypatch, tmp_path: Path) -> None:
    fixture = load_site_diagnostic("docs/testing/fixtures/site-diagnostic-v1.sample.json")
    monkeypatch.setattr(
        "web_listening.blocks.site_diagnostic.diagnose_site",
        lambda **kwargs: fixture,
    )
    output = tmp_path / "site-diagnostic.json"

    result = runner.invoke(app, [
        "diagnose-site",
        "--url", "https://example.com",
        "--site-key", "example",
        "--allowed-domain", "example.com",
        "--allowed-document-origin", "https://example.com",
        "--output", str(output),
        "--json",
    ])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "site-diagnostic.v1"
    assert payload["artifact_sha256"] == fixture.artifact_sha256
    assert load_site_diagnostic(output).artifact_sha256 == fixture.artifact_sha256

    second = runner.invoke(app, [
        "diagnose-site", "--url", "https://example.com", "--site-key", "example",
        "--allowed-domain", "example.com", "--allowed-document-origin", "https://example.com",
        "--output", str(output), "--json",
    ])
    assert second.exit_code == 0, second.output


def test_default_output_sanitizes_site_key_and_uses_diagnostic_id(monkeypatch, tmp_path: Path) -> None:
    fixture = load_site_diagnostic("docs/testing/fixtures/site-diagnostic-v1.sample.json")
    monkeypatch.setattr("web_listening.blocks.site_diagnostic.diagnose_site", lambda **kwargs: fixture)
    monkeypatch.setattr("web_listening.config.settings.data_dir", tmp_path)

    result = runner.invoke(app, [
        "diagnose-site", "--url", "https://example.com", "--site-key", "../unsafe name",
        "--allowed-domain", "example.com", "--allowed-document-origin", "https://example.com",
    ])
    assert result.exit_code == 0, result.output
    files = list((tmp_path / "plans").glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path / "plans"
    assert ".." not in files[0].name
    assert fixture.diagnostic_id in files[0].name


def test_diagnose_site_help_is_additive() -> None:
    result = runner.invoke(app, ["diagnose-site", "--help"])
    assert result.exit_code == 0
    assert "--allowed-document" in result.output
    assert "robots.txt first" in result.output
