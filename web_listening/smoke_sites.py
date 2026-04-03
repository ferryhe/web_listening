from __future__ import annotations

import json
from pathlib import Path

ALLOWED_SMOKE_EXPECTATIONS = {
    "pass_http",
    "pass_http_browser_ua",
    "pass_http_limited",
    "known_blocked",
    "broken_upstream",
    "ssl_issue",
}

REQUIRED_SMOKE_SITE_FIELDS = {
    "site_key",
    "abbreviation",
    "full_name",
    "priority",
    "homepage_url",
    "monitor_url",
    "fetch_mode",
    "fetch_config_json",
    "smoke_required",
    "smoke_expectation",
    "expected_min_words",
    "js_heavy_candidate",
    "website_status",
    "source_name",
    "source_row",
    "notes",
}


def validate_smoke_sites(payload: list[dict]) -> list[dict]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("Smoke site catalog must be a non-empty list.")

    validated: list[dict] = []
    seen_keys: set[str] = set()

    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each smoke site entry must be an object.")

        missing = sorted(REQUIRED_SMOKE_SITE_FIELDS - set(item))
        if missing:
            raise ValueError(f"Smoke site entry is missing required fields: {', '.join(missing)}")

        site_key = str(item["site_key"]).strip().lower()
        if not site_key:
            raise ValueError("Smoke site site_key must not be empty.")
        if site_key in seen_keys:
            raise ValueError(f"Duplicate smoke site site_key: {site_key}")
        seen_keys.add(site_key)

        for field_name in ("homepage_url", "monitor_url"):
            value = str(item[field_name]).strip()
            if not value.startswith("https://"):
                raise ValueError(f"{site_key}.{field_name} must start with https://")

        smoke_expectation = str(item["smoke_expectation"]).strip()
        if smoke_expectation not in ALLOWED_SMOKE_EXPECTATIONS:
            raise ValueError(
                f"{site_key}.smoke_expectation must be one of: {', '.join(sorted(ALLOWED_SMOKE_EXPECTATIONS))}"
            )

        fetch_mode = str(item["fetch_mode"]).strip().lower()
        if fetch_mode not in {"http", "browser", "auto"}:
            raise ValueError(f"{site_key}.fetch_mode must be one of: http, browser, auto")

        fetch_config_json = item["fetch_config_json"]
        if not isinstance(fetch_config_json, dict):
            raise ValueError(f"{site_key}.fetch_config_json must be an object")

        validated.append(
            {
                "site_key": site_key,
                "abbreviation": str(item["abbreviation"]).strip(),
                "full_name": str(item["full_name"]).strip(),
                "priority": bool(item["priority"]),
                "homepage_url": str(item["homepage_url"]).strip(),
                "monitor_url": str(item["monitor_url"]).strip(),
                "fetch_mode": fetch_mode,
                "fetch_config_json": fetch_config_json,
                "smoke_required": bool(item["smoke_required"]),
                "smoke_expectation": smoke_expectation,
                "expected_min_words": int(item["expected_min_words"]),
                "js_heavy_candidate": bool(item["js_heavy_candidate"]),
                "website_status": str(item["website_status"]).strip(),
                "source_name": str(item["source_name"]).strip(),
                "source_row": int(item["source_row"]),
                "notes": str(item["notes"]).strip(),
            }
        )

    return validated


def load_smoke_sites(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_smoke_sites(payload)
