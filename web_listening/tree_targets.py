from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from web_listening.dev_targets import load_dev_targets
from web_listening.smoke_sites import load_smoke_sites


DEV_TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "dev_test_sites.json"
SMOKE_TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"


@dataclass(slots=True)
class TreeTarget:
    catalog: str
    site_key: str
    display_name: str
    seed_url: str
    homepage_url: str
    fetch_mode: str
    fetch_config_json: dict
    allowed_page_prefixes: list[str]
    allowed_file_prefixes: list[str]
    tree_strategy: str = ""
    tree_budget_profile: str = ""
    tree_max_depth: int | None = None
    tree_max_pages: int | None = None
    tree_max_files: int | None = None
    notes: str = ""


def load_dev_tree_targets(path: Path = DEV_TARGETS_PATH) -> list[TreeTarget]:
    targets: list[TreeTarget] = []
    for item in load_dev_targets(path):
        targets.append(
            TreeTarget(
                catalog="dev",
                site_key=item["site_key"],
                display_name=item["site_name"],
                seed_url=item["monitor_url"],
                homepage_url=item["base_url"],
                fetch_mode="http",
                fetch_config_json={},
                allowed_page_prefixes=["/"],
                allowed_file_prefixes=["/"],
                tree_strategy="homepage_full",
                tree_budget_profile="production_default",
                notes="Development target derived from monitor_url.",
            )
        )
    return targets


def load_smoke_tree_targets(path: Path = SMOKE_TARGETS_PATH) -> list[TreeTarget]:
    targets: list[TreeTarget] = []
    for item in load_smoke_sites(path):
        seed_url = item.get("tree_seed_url") or item.get("monitor_url") or item["homepage_url"]
        targets.append(
            TreeTarget(
                catalog="smoke",
                site_key=item["site_key"],
                display_name=item["abbreviation"],
                seed_url=seed_url,
                homepage_url=item["homepage_url"],
                fetch_mode=item["fetch_mode"],
                fetch_config_json=item["fetch_config_json"],
                allowed_page_prefixes=item.get("tree_page_prefixes") or ["/"],
                allowed_file_prefixes=item.get("tree_file_prefixes") or ["/"],
                tree_strategy=item.get("tree_strategy", ""),
                tree_budget_profile=item.get("tree_budget_profile", ""),
                tree_max_depth=item.get("tree_max_depth"),
                tree_max_pages=item.get("tree_max_pages"),
                tree_max_files=item.get("tree_max_files"),
                notes=item.get("notes", ""),
            )
        )
    return targets


def load_tree_targets(catalog: str) -> list[TreeTarget]:
    normalized = (catalog or "").strip().lower()
    if normalized == "dev":
        return load_dev_tree_targets()
    if normalized == "smoke":
        return load_smoke_tree_targets()
    if normalized == "all":
        return load_dev_tree_targets() + load_smoke_tree_targets()
    raise ValueError("catalog must be one of: dev, smoke, all")


def filter_tree_targets(targets: list[TreeTarget], site_keys: set[str] | None = None) -> list[TreeTarget]:
    if not site_keys:
        return targets
    wanted = {value.strip().lower() for value in site_keys if value.strip()}
    return [target for target in targets if target.site_key in wanted]
