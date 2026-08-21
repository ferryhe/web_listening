from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


@pytest.mark.parametrize("extra", ["dev", "mcp"])
def test_mcp_extra_stays_on_qualified_1x_release_line(extra: str) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    mcp_requirements = [
        item
        for item in project["optional-dependencies"][extra]
        if item.startswith("mcp")
    ]
    assert mcp_requirements == ["mcp>=1.28.1,<2.0.0"]


def test_base_install_includes_jsonschema_non_gpl_format_validators() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    jsonschema_requirements = [
        item for item in project["dependencies"] if item.startswith("jsonschema")
    ]
    assert jsonschema_requirements == ["jsonschema[format-nongpl]>=4.23.0,<5.0.0"]


def test_ci_asserts_wheel_format_dependency_metadata() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'jsonschema[0].extras == {"format-nongpl"}' in workflow
    assert "{str(item) for item in jsonschema[0].specifier}" in workflow
    assert '">=4.23.0"' in workflow
    assert '"<5.0.0"' in workflow
