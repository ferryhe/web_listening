import json
from pathlib import Path

import pytest

from web_listening.smoke_sites import load_smoke_sites, validate_smoke_sites


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"


def test_smoke_site_catalog_loads():
    entries = load_smoke_sites(CONFIG_PATH)

    assert len(entries) == 37
    assert any(item["site_key"] == "un-water" for item in entries)
    assert any(item["site_key"] == "g20" for item in entries)
    iea = next(item for item in entries if item["site_key"] == "iea")
    assert iea["tree_seed_url"] == "https://www.iea.org/news"
    assert iea["tree_page_prefixes"] == ["/news"]
    assert iea["tree_budget_profile"] == ""


def test_smoke_site_catalog_contains_browser_ua_targets():
    entries = load_smoke_sites(CONFIG_PATH)
    browser_ua_targets = {
        item["site_key"]
        for item in entries
        if item["fetch_config_json"].get("user_agent_profile") == "browser"
    }

    assert browser_ua_targets == {"g20", "ilo"}


def test_validate_smoke_sites_parses_boolean_like_strings():
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    payload[0]["priority"] = "false"
    payload[0]["smoke_required"] = "0"
    payload[0]["js_heavy_candidate"] = "yes"

    entries = validate_smoke_sites(payload)

    assert entries[0]["priority"] is False
    assert entries[0]["smoke_required"] is False
    assert entries[0]["js_heavy_candidate"] is True


def test_validate_smoke_sites_accepts_optional_tree_budget_fields():
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    payload[0]["tree_strategy"] = "homepage_full"
    payload[0]["tree_budget_profile"] = "homepage_standard"
    payload[0]["tree_max_depth"] = 4
    payload[0]["tree_max_pages"] = 120
    payload[0]["tree_max_files"] = 40

    entries = validate_smoke_sites(payload)

    assert entries[0]["tree_strategy"] == "homepage_full"
    assert entries[0]["tree_budget_profile"] == "homepage_standard"
    assert entries[0]["tree_max_depth"] == 4
    assert entries[0]["tree_max_pages"] == 120
    assert entries[0]["tree_max_files"] == 40


def test_validate_smoke_sites_rejects_invalid_boolean_strings():
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    payload[0]["smoke_required"] = "maybe"

    with pytest.raises(ValueError, match="must be a boolean value"):
        validate_smoke_sites(payload)
