import json
from pathlib import Path

import pytest

from web_listening.dev_targets import REQUIRED_DEV_SITE_KEYS, load_dev_targets, validate_dev_targets


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "dev_test_sites.json"


def test_dev_target_config_includes_required_sites():
    targets = load_dev_targets(CONFIG_PATH)
    site_keys = {item["site_key"] for item in targets}

    assert site_keys == set(REQUIRED_DEV_SITE_KEYS)
    assert any(item["base_url"] == "https://actuaries.org/" for item in targets)


def test_validate_dev_targets_rejects_missing_required_site():
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    filtered = [item for item in payload if item["site_key"] != "iaa"]

    with pytest.raises(ValueError, match="missing required sites"):
        validate_dev_targets(filtered)
