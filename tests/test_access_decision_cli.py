from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from web_listening.cli import app
from web_listening.contracts.site_diagnostic import canonical_json


runner = CliRunner()
ERROR_JSON = (
    canonical_json(
        {
            "schema_version": "access-rejection-error.v1",
            "outcome": "error",
            "reason_code": "contract.invalid",
            "message": "access contract validation failed",
            "retryable": False,
            "evidence": None,
        }
    )
    + "\n"
)


def test_validate_access_contract_help_is_additive() -> None:
    result = runner.invoke(app, ["validate-access-contract", "--help"])
    assert result.exit_code == 0, result.output
    assert "--path" in result.output
    assert "--json" in result.output
    assert "offline" in result.output.casefold()


def test_validate_access_contract_json_emits_canonical_contract() -> None:
    result = runner.invoke(
        app,
        [
            "validate-access-contract",
            "--path",
            "docs/testing/fixtures/access-decision-v1.sample.json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "access-decision.v1"
    assert result.output == canonical_json(payload) + "\n"


def test_validate_access_contract_json_emits_standalone_shared_envelope() -> None:
    result = runner.invoke(
        app,
        [
            "validate-access-contract",
            "--path",
            "docs/testing/fixtures/access-rejection-error-v1.sample.json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "access-rejection-error.v1"
    assert payload["reason_code"] == "robots.timeout"
    assert result.output == canonical_json(payload) + "\n"


def test_validate_access_contract_human_envelope_omits_empty_identity_rows() -> None:
    result = runner.invoke(
        app,
        [
            "validate-access-contract",
            "--path",
            "docs/testing/fixtures/access-rejection-error-v1.sample.json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Valid access contract" in result.output
    assert "Schema: access-rejection-error.v1" in result.output
    assert "ID:" not in result.output
    assert "Digest:" not in result.output


def test_validate_access_contract_invalid_json_uses_shared_error_envelope() -> None:
    result = runner.invoke(
        app,
        [
            "validate-access-contract",
            "--path",
            "docs/testing/fixtures/access-policy-v1.duplicate-key.invalid.json",
            "--json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload == {
        "schema_version": "access-rejection-error.v1",
        "outcome": "error",
        "reason_code": "contract.invalid",
        "message": "access contract validation failed",
        "retryable": False,
        "evidence": None,
    }


def test_validate_access_contract_extreme_number_uses_exact_json_envelope() -> None:
    fixture = Path(
        "docs/testing/fixtures/access-decision-v1.numeric-overflow.invalid.json"
    )
    assert fixture.is_file()
    result = runner.invoke(
        app,
        ["validate-access-contract", "--path", str(fixture), "--json"],
    )

    assert result.exit_code == 1
    assert result.output == ERROR_JSON


def test_validate_access_contract_deep_json_uses_exact_envelope(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "deep-access-contract.json"
    fixture.write_text(
        "[" * 5_000 + '{"schema_version":"access-policy.v1"}' + "]" * 5_000,
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["validate-access-contract", "--path", str(fixture), "--json"],
    )

    assert result.exit_code == 1
    assert result.output == ERROR_JSON
    assert result.output.count("\n") == 1


@pytest.mark.parametrize(
    "fixture_name",
    [
        "access-decision-v1.proxy-authority.invalid.json",
        "access-decision-v1.overlapping-userinfo.invalid.json",
        "access-decision-v1.nested-sensitive-url.invalid.json",
        "access-decision-v1.nfkc-query.invalid.json",
        "access-decision-v1.namespaced-secret.invalid.json",
        "access-decision-v1.pipe-network-userinfo.invalid.json",
        "access-policy-v1.encoded-nested-userinfo.invalid.json",
        "access-policy-v1.encoded-sensitive-text.invalid.json",
        "access-policy-v1.namespaced-secret.invalid.json",
        "access-policy-v1.network-authority.invalid.json",
        "access-policy-v1.nested-sensitive-text.invalid.json",
        "access-policy-v1.pipe-network-userinfo.invalid.json",
        "access-policy-v1.uri-userinfo.invalid.json",
    ],
)
def test_sensitive_fixtures_emit_only_the_exact_json_envelope(
    fixture_name: str,
) -> None:
    result = runner.invoke(
        app,
        [
            "validate-access-contract",
            "--path",
            f"docs/testing/fixtures/{fixture_name}",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert result.output == ERROR_JSON


@pytest.mark.parametrize(
    "fixture_name",
    [
        "access-rejection-error-v1.tampered.invalid.json",
        "access-rejection-error-v1.cache-key.invalid.json",
        "access-rejection-error-v1.freshness.invalid.json",
        "access-rejection-error-v1.matrix.invalid.json",
        "access-rejection-error-v1.partial-policy.invalid.json",
    ],
)
def test_invalid_standalone_envelopes_emit_only_the_exact_json_error(
    fixture_name: str,
) -> None:
    result = runner.invoke(
        app,
        [
            "validate-access-contract",
            "--path",
            f"docs/testing/fixtures/{fixture_name}",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert result.output == ERROR_JSON


@pytest.mark.parametrize(
    "args",
    [
        ["validate-access-contract", "--json"],
        ["validate-access-contract", "--path", "--json"],
        [
            "validate-access-contract",
            "--path",
            "does-not-exist.json",
            "--json",
        ],
    ],
)
def test_validate_access_contract_json_parser_failures_use_exact_envelope(
    args: list[str],
) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code != 0
    assert result.output == ERROR_JSON


def test_validate_access_contract_json_directory_failure_uses_exact_envelope(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        ["validate-access-contract", "--path", str(tmp_path), "--json"],
    )

    assert result.exit_code != 0
    assert result.output == ERROR_JSON


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="portable unreadable-file check requires non-root POSIX permissions",
)
def test_validate_access_contract_json_unreadable_failure_uses_exact_envelope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unreadable.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0)
    try:
        result = runner.invoke(
            app,
            ["validate-access-contract", "--path", str(path), "--json"],
        )
    finally:
        path.chmod(0o600)

    assert result.exit_code != 0
    assert result.output == ERROR_JSON


def test_validate_access_contract_human_parser_failure_remains_typer_usage() -> None:
    result = runner.invoke(app, ["validate-access-contract"])

    assert result.exit_code == 2
    assert "Usage" in result.output
    assert result.output != ERROR_JSON
