from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from web_listening.blocks.acquisition_contract import (
    acquisition_contract_error_result,
)
from web_listening.cli import app
from web_listening.contracts.site_diagnostic import canonical_json


runner = CliRunner()
BUNDLE = Path("contracts/acquisition-manifest.v1")


def test_validate_acquisition_contract_help_is_stable() -> None:
    result = runner.invoke(app, ["validate-acquisition-contract", "--help"])

    assert result.exit_code == 0, result.output
    assert "--bundle-path" in result.output
    assert "--json" in result.output
    assert "offline" in result.output.casefold()


def test_readme_inventory_includes_acquisition_contract_validator() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )

    governance = readme.split("### Governance and acquisition", 1)[1].split(
        "### Jobs and delivery", 1
    )[0]
    assert "`validate-acquisition-contract`" in governance


def test_validate_acquisition_contract_json_is_canonical_and_repeatable() -> None:
    args = [
        "validate-acquisition-contract",
        "--bundle-path",
        str(BUNDLE),
        "--json",
    ]
    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    payload = json.loads(first.output)
    assert payload["valid"] is True
    assert payload["reason_code"] == "contract.valid"
    assert first.output == canonical_json(payload) + "\n"
    assert second.output == first.output


def test_validate_acquisition_contract_human_result() -> None:
    result = runner.invoke(
        app,
        ["validate-acquisition-contract", "--bundle-path", str(BUNDLE)],
    )

    assert result.exit_code == 0, result.output
    assert "Valid acquisition contract bundle" in result.output
    assert "acquisition-manifest.v1" in result.output


def test_validate_acquisition_contract_invalid_json_has_stable_reason(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("schema.json", "fixture.json", "producer-confirmation.json"):
        (bundle / name).write_text("{\n", encoding="utf-8", newline="\n")
    result = runner.invoke(
        app,
        [
            "validate-acquisition-contract",
            "--bundle-path",
            str(bundle),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload == acquisition_contract_error_result("bundle.invalid_json")
    assert result.output == canonical_json(payload) + "\n"


def test_validate_acquisition_contract_json_parser_failure_is_canonical() -> None:
    result = runner.invoke(app, ["validate-acquisition-contract", "--json"])

    assert result.exit_code != 0
    payload = acquisition_contract_error_result("bundle.argument_invalid")
    assert result.output == canonical_json(payload) + "\n"


def test_validate_acquisition_contract_json_missing_path_is_canonical() -> None:
    result = runner.invoke(
        app,
        [
            "validate-acquisition-contract",
            "--bundle-path",
            "does-not-exist",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = acquisition_contract_error_result("bundle.path_invalid")
    assert result.output == canonical_json(payload) + "\n"


def test_validate_acquisition_contract_human_parser_failure_uses_typer() -> None:
    result = runner.invoke(app, ["validate-acquisition-contract"])

    assert result.exit_code == 2
    assert "Usage" in result.output
    assert "acquisition-contract-validation.v1" not in result.output
