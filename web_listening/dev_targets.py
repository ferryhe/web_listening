from __future__ import annotations

import json
from pathlib import Path

REQUIRED_DEV_SITE_KEYS = ("soa", "cas", "iaa")
REQUIRED_TARGET_FIELDS = {
    "site_key",
    "site_name",
    "base_url",
    "monitor_url",
    "document_url",
    "expected_min_doc_links",
    "expected_min_monitor_words",
    "expected_min_document_words",
    "sample_download_limit",
}


def validate_dev_targets(payload: list[dict]) -> list[dict]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("Development target config must be a non-empty list.")

    seen_keys: set[str] = set()
    validated: list[dict] = []

    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each development target must be an object.")

        missing = sorted(REQUIRED_TARGET_FIELDS - set(item))
        if missing:
            raise ValueError(f"Development target is missing required fields: {', '.join(missing)}")

        site_key = str(item["site_key"]).strip().lower()
        if not site_key:
            raise ValueError("Development target site_key must not be empty.")
        if site_key in seen_keys:
            raise ValueError(f"Duplicate development target site_key: {site_key}")
        seen_keys.add(site_key)

        for field_name in ("base_url", "monitor_url", "document_url"):
            value = str(item[field_name]).strip()
            if not value.startswith("https://"):
                raise ValueError(f"{site_key}.{field_name} must start with https://")

        validated_item = dict(item)
        validated_item["site_key"] = site_key
        validated_item["site_name"] = str(item["site_name"]).strip()
        for field_name in (
            "expected_min_doc_links",
            "expected_min_monitor_words",
            "expected_min_document_words",
            "sample_download_limit",
        ):
            validated_item[field_name] = int(item[field_name])
            if validated_item[field_name] < 0:
                raise ValueError(f"{site_key}.{field_name} must be >= 0")

        validated.append(validated_item)

    missing_required = sorted(set(REQUIRED_DEV_SITE_KEYS) - seen_keys)
    if missing_required:
        raise ValueError(
            "Development target config is missing required sites: "
            + ", ".join(missing_required)
        )

    return validated


def load_dev_targets(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_dev_targets(payload)
