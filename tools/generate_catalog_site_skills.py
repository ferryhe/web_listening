#!/usr/bin/env python3
"""Deterministically generate governed Site Skill packages from tracked catalogs."""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from web_listening.contracts._protocol import validate_portable_json


ROOT = Path(__file__).resolve().parents[1]
CATALOGS = (ROOT / "config/dev_test_sites.json", ROOT / "config/smoke_site_catalog.json")
DESTINATION = ROOT / "web_listening/skills/sites"
VERSION = "1.0.0"
GENERATED_AT = "2026-07-20T00:00:00Z"
SAFE_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PATHS = (
    "SKILL.md", "manifest.json", "profiles/default.yaml", "scripts/recipe.py",
    "scripts/executor.py", "tests/verification.json",
)


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()


def _yaml(value: object) -> bytes:
    return yaml.safe_dump(value, allow_unicode=False, sort_keys=False).encode()


def _parse_catalog_bytes(data: bytes) -> object:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("catalog JSON contains duplicate object keys")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=reject_duplicate_keys)


def _validate_fetch_config(value: object, *, site_key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{site_key}.fetch_config_json must be an object")
    try:
        validate_portable_json(value, location="fetch_config_json")
    except (TypeError, ValueError):
        raise ValueError(
            f"{site_key}.fetch_config_json contains forbidden secret material"
        ) from None
    if value not in ({}, {"user_agent_profile": "browser"}):
        raise ValueError(f"{site_key}.fetch_config_json contains unsupported catalog config")
    return value


def _load() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for path in CATALOGS:
        payload = _parse_catalog_bytes(path.read_bytes())
        if not isinstance(payload, list):
            raise ValueError(f"catalog root must be a list: {path.relative_to(ROOT)}")
        for row in payload:
            if not isinstance(row, dict) or not isinstance(row.get("site_key"), str):
                raise ValueError(f"catalog row must have a string site_key: {path.relative_to(ROOT)}")
            key = row["site_key"]
            if not SAFE_KEY.fullmatch(key):
                raise ValueError(f"unsafe site_key: {key!r}")
            if key in seen:
                raise ValueError(f"duplicate site_key: {key}")
            seen.add(key)
            rows.append(row)
    if len(rows) != 40:
        raise ValueError(f"expected frozen 40-site catalog, found {len(rows)}")
    return rows


def _urls(row: dict) -> dict[str, str]:
    pairs = {}
    for name in ("base_url", "homepage_url", "monitor_url", "document_url", "tree_seed_url"):
        value = row.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"{row['site_key']}.{name} must be a string")
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{row['site_key']}.{name} must be a safe HTTP(S) URL")
            pairs[name] = value
    if "monitor_url" not in pairs or not ({"homepage_url", "base_url"} & pairs.keys()):
        raise ValueError(f"{row['site_key']} is missing required catalog URLs")
    return pairs


def _files(row: dict) -> dict[str, bytes]:
    key = row["site_key"]
    urls = _urls(row)
    domains = sorted({urlsplit(value).hostname.lower().rstrip(".") for value in urls.values()})
    fetch_config = _validate_fetch_config(row.get("fetch_config_json", {}), site_key=key)
    min_words = row.get("expected_min_words", row.get("expected_min_monitor_words"))
    if isinstance(min_words, bool) or not isinstance(min_words, int) or min_words < 0:
        raise ValueError(f"{key} has an unsafe quality threshold")
    outcome = row.get("smoke_expectation", "pass_http")
    if not isinstance(outcome, str) or not outcome:
        raise ValueError(f"{key} has an invalid canary outcome")
    catalog = {**urls, "fetch_config_json": fetch_config}
    profile = {
        "allowed_domains": domains,
        "schema_version": "acquisition-profile.v1",
        "profile_id": f"{key}-catalog-http",
        "site_key": key,
        "generated_at": GENERATED_AT,
        "strategy": "catalog-http",
        "default_adapter": "web_http",
        "fallback_order": [],
        "quality_gates": {"min_words": min_words, "min_links": 0, "min_document_links": 0,
                          "require_status_ok": True,
                          "blocked_markers": ["access denied", "captcha", "cloudflare", "enable javascript", "forbidden"]},
        "safety": {"allowed_domains": domains, "allow_stealth_browser": False,
                   "require_authorized_access": False},
        "adapters": [{"adapter": "web_http", "enabled": True,
                      "reason": "Catalog-governed HTTP acquisition.", "config": catalog, "safety": {}}],
        "recipe_mappings": [{"adapter": "web_http", "recipe_id": "catalog-http"}],
        "resource_limits": {"timeout_seconds": 30.0, "stdout_bytes": 4194304, "stderr_bytes": 65536},
        "adapter_resource_limits": {},
        "notes": [f"Catalog canary classification: {outcome}.",
                  f"JavaScript-heavy candidate: {str(bool(row.get('js_heavy_candidate', False))).lower()}."]
    }
    manifest = {
        "schema_version": "site-skill.v1", "skill_id": f"{key}-catalog", "site_key": key,
        "version": VERSION, "status": "active", "generated_at": GENERATED_AT,
        "runtime_requirements": [{"requirement_id": "python-312", "description": "Python 3.12 runtime.", "optional": False}],
        "secret_policy": {"allow_secret_references": False, "forbid_secret_values": True,
                          "allowed_reference_schemes": []},
        "allowed_domains": domains, "default_executor_id": "web_http", "default_recipe_id": "catalog-http",
        "executors": [{"executor_id": "web_http", "enabled": True, "config": {}, "script_path": "scripts/executor.py"}],
        "recipes": [{"recipe_id": "catalog-http", "enabled": True, "executor_id": "web_http",
                     "profile_ref": "profiles/default.yaml", "entrypoint": "scripts/recipe.py",
                     "output_contract": "capture-result.v1", "required_capabilities": ["http_get"],
                     "verification_rules": [{"rule_id": "status-and-quality", "description": "Require safe HTTP content meeting catalog quality gates."}]}],
        "metadata": {"canary_outcome": outcome, "js_heavy_candidate": bool(row.get("js_heavy_candidate", False)),
                     "source_catalog": "dev" if "base_url" in row else "smoke"},
    }
    return {
        "SKILL.md": f"# {key} Catalog Site Skill\n\nDeterministic governed HTTP package generated from the tracked site catalogs.\n".encode(),
        "manifest.json": _json(manifest), "profiles/default.yaml": _yaml(profile),
        "scripts/recipe.py": b'"""Static catalog HTTP recipe declaration; never imported by the registry."""\n\nENTRYPOINT = "catalog_http"\n',
        "scripts/executor.py": b'"""Static catalog HTTP executor declaration; never imported by the registry."""\n\nEXECUTOR_ID = "web_http"\n',
        "tests/verification.json": _json({"implemented_rule_ids": ["status-and-quality"]}),
    }


def _reject_unsafe_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"governed path must be a real directory: {path.name}")


def _validate_governed_roots(keys: list[str]) -> None:
    _reject_unsafe_directory(DESTINATION)
    for key in keys:
        site = DESTINATION / key
        package = site / VERSION
        _reject_unsafe_directory(site)
        _reject_unsafe_directory(package)
        for parent in (package / "profiles", package / "scripts", package / "tests"):
            _reject_unsafe_directory(parent)


def _identity_drift(keys: list[str]) -> list[str]:
    if not DESTINATION.exists():
        return []
    allowed_sites = set(keys) | {"example-news"}
    drift: list[str] = []
    for site in sorted(DESTINATION.iterdir(), key=lambda path: path.name):
        if site.name not in allowed_sites:
            drift.append(f"unexpected-site:{site.name}")
            continue
        try:
            info = site.lstat()
        except FileNotFoundError:
            drift.append(f"unsafe-site:{site.name}")
            continue
        if site.is_symlink() or not stat.S_ISDIR(info.st_mode):
            drift.append(f"unsafe-site:{site.name}")
            continue
        for version in sorted(site.iterdir(), key=lambda path: path.name):
            if version.name != VERSION:
                drift.append(f"unexpected-version:{site.name}/{version.name}")
            elif version.is_symlink() or not version.is_dir():
                drift.append(f"unsafe-version:{site.name}/{version.name}")
    return drift


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = {row["site_key"]: _files(row) for row in _load()}
    _validate_governed_roots(list(expected))
    identity_drift = _identity_drift(list(expected))
    if identity_drift:
        for item in identity_drift:
            print(f"catalog-site-skills drift={item}")
        print(f"catalog-site-skills mode={'write' if args.write else 'check'} changed={len(identity_drift)}")
        return 1
    changed: list[str] = []
    for key, files in expected.items():
        package = DESTINATION / key / VERSION
        actual = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()} if package.exists() else set()
        for relative in sorted(actual - set(PATHS)):
            changed.append(f"unexpected:{key}/{relative}")
            if args.write:
                (package / relative).unlink()
        for relative in PATHS:
            path, content = package / relative, files[relative]
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                changed.append(f"stale:{key}/{relative}")
                if args.write:
                    _validate_governed_roots([key])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path.is_symlink():
                        path.unlink()
                    path.write_bytes(content)
    print(f"catalog-site-skills mode={'write' if args.write else 'check'} changed={len(changed)}")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"catalog-site-skills error: {exc}", file=sys.stderr)
        raise SystemExit(2)
