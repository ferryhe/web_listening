from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import web_listening
from web_listening.api.app import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_version_authorities_are_consistent() -> None:
    project = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert project["version"] == "3.3.0"
    assert importlib.metadata.version("web-listening") == project["version"]
    assert web_listening.__version__ == project["version"]
    assert app.version == project["version"]
    assert app.openapi()["info"]["version"] == project["version"]
    assert Path(web_listening.__file__).resolve().is_file()
    assert Path(sys.executable).is_file()


def test_stale_and_missing_distribution_metadata_are_distinct(tmp_path: Path) -> None:
    source_root = PROJECT_ROOT
    probe = """
import importlib.metadata as metadata
import json
import sys

mode = sys.argv[1]
original_version = metadata.version

if mode == "stale":
    def version(name):
        return "1.0.0" if name == "web-listening" else original_version(name)
else:
    def version(name):
        if name == "web-listening":
            raise metadata.PackageNotFoundError(name)
        return original_version(name)

metadata.version = version

import web_listening
from web_listening.api.app import app

try:
    distribution_version = metadata.version("web-listening")
except metadata.PackageNotFoundError:
    distribution_version = None
print(json.dumps({
    "distribution_version": distribution_version,
    "package_version": web_listening.__version__,
    "app_version": app.version,
    "openapi_version": app.openapi()["info"]["version"],
}))
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root)

    for mode, expected in (("stale", "1.0.0"), ("missing", "0+unknown")):
        result = subprocess.run(
            [sys.executable, "-c", probe, mode],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        observed = json.loads(result.stdout)
        if mode == "missing":
            assert observed["distribution_version"] is None
        else:
            assert observed["distribution_version"] == expected
        assert observed["package_version"] == expected
        assert observed["app_version"] == expected
        assert observed["openapi_version"] == expected
