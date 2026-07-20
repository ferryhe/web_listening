from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path

import web_listening
from web_listening.api.app import app


def test_release_version_authorities_are_consistent() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["version"] == "1.0.0"
    assert importlib.metadata.version("web-listening") == project["version"]
    assert web_listening.__version__ == project["version"]
    assert app.version == project["version"]
