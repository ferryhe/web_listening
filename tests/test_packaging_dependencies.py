from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


@pytest.mark.parametrize("extra", ["dev", "mcp"])
def test_mcp_extra_stays_on_qualified_1x_release_line(extra: str) -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    mcp_requirements = [item for item in project["optional-dependencies"][extra] if item.startswith("mcp")]
    assert mcp_requirements == ["mcp>=1.28.1,<2.0.0"]
