from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml
import pytest

from web_listening.site_skill_registry import resolve_site_skill_contract, validate_site_skill_package


ROOT = Path(__file__).parents[1]
SITES = ROOT / "web_listening/skills/sites"
FILES = {
    "SKILL.md", "manifest.json", "profiles/default.yaml",
    "scripts/recipe.py", "scripts/executor.py", "tests/verification.json",
}
KEYS = (
    "soa", "cas", "iaa", "a2ii", "iais", "iea", "ipcc", "irff", "issa", "issb",
    "oecd", "pcaf", "psi", "tnfd", "undp", "fao", "unep", "wef", "world-bank",
    "adb", "afdb", "bcbs", "bis", "caf", "fit", "fsb", "g20", "gca", "ifac",
    "ilo", "imf", "ngfs", "sif", "un-water", "unctad", "unfccc", "who", "wmo",
    "wri", "wto",
)


def _generator_module():
    path = ROOT / "tools/generate_catalog_site_skills.py"
    spec = importlib.util.spec_from_file_location("generate_catalog_site_skills", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def catalogs() -> list[dict]:
    result = []
    for name in ("dev_test_sites.json", "smoke_site_catalog.json"):
        result.extend(json.loads((ROOT / "config" / name).read_text(encoding="utf-8")))
    return result


def test_catalog_packages_are_complete_valid_and_exactly_resolvable() -> None:
    assert tuple(item["site_key"] for item in catalogs()) == KEYS
    for source in catalogs():
        package = SITES / source["site_key"] / "1.0.0"
        actual = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
        assert actual == FILES
        assert not any(path.is_symlink() for path in package.rglob("*"))
        result = validate_site_skill_package(package)
        assert result["valid"], (source["site_key"], result["diagnostics"])
        resolved = resolve_site_skill_contract(
            site_key=source["site_key"], version="1.0.0",
            package_sha256=result["package_sha256"], root=SITES,
        )
        assert resolved.manifest.site_key == source["site_key"]


def test_packages_preserve_catalog_authority() -> None:
    for source in catalogs():
        package = SITES / source["site_key"] / "1.0.0"
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        profile = yaml.safe_load((package / "profiles/default.yaml").read_text(encoding="utf-8"))
        expected_home = source["homepage_url"] if "homepage_url" in source else source["base_url"]
        expected_monitor = source["monitor_url"]
        catalog = profile["adapters"][0]["config"]
        assert catalog.get("homepage_url", catalog.get("base_url")) == expected_home
        assert catalog["monitor_url"] == expected_monitor
        assert catalog["fetch_config_json"] == source.get("fetch_config_json", {})
        expected_words = source["expected_min_words"] if "expected_min_words" in source else source["expected_min_monitor_words"]
        assert profile["quality_gates"]["min_words"] == expected_words
        assert manifest["metadata"]["js_heavy_candidate"] == source.get("js_heavy_candidate", False)
        assert manifest["metadata"]["canary_outcome"] == source.get("smoke_expectation", "pass_http")


def test_generator_check_is_clean_and_deterministic() -> None:
    command = [sys.executable, str(ROOT / "tools/generate_catalog_site_skills.py"), "--check"]
    first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    assert first.returncode == second.returncode == 0, first.stdout + first.stderr
    assert first.stdout == second.stdout


def test_catalog_parser_rejects_duplicate_keys_at_any_depth_without_echoing_values() -> None:
    generator = _generator_module()
    secret = "duplicate-value-must-not-leak"
    payload = ('[{"site_key":"safe","nested":{"api":"first","api":"' + secret + '"}}]').encode()
    with pytest.raises(ValueError) as exc_info:
        generator._parse_catalog_bytes(payload)
    assert str(exc_info.value) == "catalog JSON contains duplicate object keys"
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    "fetch_config",
    [
        {"nested": {"api_key": "must-not-leak"}},
        {"nested": [{"clientSecret": "must-not-leak"}]},
    ],
)
def test_catalog_fetch_config_rejects_nested_secret_keys_without_echoing_values(fetch_config) -> None:
    generator = _generator_module()
    with pytest.raises(ValueError) as exc_info:
        generator._validate_fetch_config(fetch_config, site_key="safe")
    assert str(exc_info.value) == "safe.fetch_config_json contains forbidden secret material"
    assert "must-not-leak" not in str(exc_info.value)


def test_catalog_fetch_config_allows_approved_browser_user_agent() -> None:
    generator = _generator_module()
    assert generator._validate_fetch_config(
        {"user_agent_profile": "browser"}, site_key="safe"
    ) == {"user_agent_profile": "browser"}


@pytest.mark.parametrize("value", ["env:CATALOG_SECRET", "sk-literal-must-not-leak"])
def test_catalog_fetch_config_rejects_secret_references_and_literals(value) -> None:
    generator = _generator_module()
    with pytest.raises(ValueError) as exc_info:
        generator._validate_fetch_config({"user_agent_profile": value}, site_key="safe")
    assert str(exc_info.value) == "safe.fetch_config_json contains unsupported catalog config"
    assert value not in str(exc_info.value)


@pytest.mark.parametrize("mode", ["--check", "--write"])
@pytest.mark.parametrize("linked_root", ["site", "package"])
def test_generator_rejects_governed_root_symlinks(tmp_path, monkeypatch, mode, linked_root) -> None:
    generator = _generator_module()
    destination = tmp_path / "sites"
    real = tmp_path / "real"
    real.mkdir()
    if linked_root == "site":
        destination.symlink_to(real, target_is_directory=True)
    else:
        destination.mkdir()
        site = destination / KEYS[0]
        site.mkdir()
        (site / generator.VERSION).symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(generator, "DESTINATION", destination)
    monkeypatch.setattr(sys, "argv", ["generate_catalog_site_skills.py", mode])
    with pytest.raises(ValueError, match="governed path must be a real directory"):
        generator.main()
    assert list(real.iterdir()) == []


@pytest.mark.parametrize("mode", ["--check", "--write"])
def test_generator_rejects_orphan_site_without_mutation(tmp_path, monkeypatch, mode, capsys) -> None:
    generator = _generator_module()
    destination = tmp_path / "sites"
    orphan = destination / "orphan-site"
    orphan.mkdir(parents=True)
    marker = orphan / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(generator, "DESTINATION", destination)
    monkeypatch.setattr(sys, "argv", ["generate_catalog_site_skills.py", mode])

    assert generator.main() == 1
    assert capsys.readouterr().out == (
        "catalog-site-skills drift=unexpected-site:orphan-site\n"
        f"catalog-site-skills mode={mode.removeprefix('--')} changed=1\n"
    )
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in destination.iterdir()) == ["orphan-site"]


@pytest.mark.parametrize("mode", ["--check", "--write"])
def test_generator_rejects_extra_version_without_mutation(tmp_path, monkeypatch, mode, capsys) -> None:
    generator = _generator_module()
    destination = tmp_path / "sites"
    extra = destination / KEYS[0] / "2.0.0"
    extra.mkdir(parents=True)
    marker = extra / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(generator, "DESTINATION", destination)
    monkeypatch.setattr(sys, "argv", ["generate_catalog_site_skills.py", mode])

    assert generator.main() == 1
    assert capsys.readouterr().out == (
        f"catalog-site-skills drift=unexpected-version:{KEYS[0]}/2.0.0\n"
        f"catalog-site-skills mode={mode.removeprefix('--')} changed=1\n"
    )
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in (destination / KEYS[0]).iterdir()) == ["2.0.0"]
