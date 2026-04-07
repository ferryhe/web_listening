from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.section_discovery import (
    CatalogSectionInventory,
    SectionDiscoverer,
    render_markdown,
    render_yaml,
)
from web_listening.config import settings
from web_listening.tree_targets import filter_tree_targets, load_tree_targets


def build_default_yaml_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "plans" / f"section_inventory_{catalog}_{report_date}.yaml"


def build_default_report_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"section_inventory_{catalog}_{report_date}.md"


def discover_sections(
    *,
    catalog: str,
    site_keys: set[str] | None = None,
    discovery_depth: int = 3,
    section_depth: int = 3,
    max_pages: int | None = None,
    detect_documents: bool = False,
    level3_sample_limit: int = 2,
) -> CatalogSectionInventory:
    targets = filter_tree_targets(load_tree_targets(catalog), site_keys)
    generated_at = datetime.now(timezone.utc).isoformat()
    sites = []

    with SectionDiscoverer() as discoverer:
        for target in targets:
            sites.append(
                discoverer.discover_target(
                    site_key=target.site_key,
                    display_name=target.display_name,
                    seed_url=target.seed_url,
                    homepage_url=target.homepage_url,
                    fetch_mode=target.fetch_mode,
                    fetch_config_json=target.fetch_config_json,
                    allowed_page_prefixes=target.allowed_page_prefixes,
                    allowed_file_prefixes=target.allowed_file_prefixes,
                    discovery_depth=discovery_depth,
                    section_depth=section_depth,
                    max_pages=max_pages,
                    detect_documents=detect_documents,
                    level3_sample_limit=level3_sample_limit,
                    notes=target.notes,
                )
            )

    return CatalogSectionInventory(
        catalog=catalog,
        generated_at=generated_at,
        discovery_depth=discovery_depth,
        section_depth=section_depth,
        max_pages=max_pages or 0,
        page_limit_mode="unbounded" if not max_pages else "bounded",
        discovery_mode="structure_only" if not detect_documents else "structure_plus_documents",
        discovery_strategy="adaptive_sections",
        detect_documents=detect_documents,
        level3_sample_limit=level3_sample_limit,
        sites=sites,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover shallow second- and third-level site sections for planning targeted monitoring scopes."
    )
    parser.add_argument("--catalog", choices=("dev", "smoke", "all"), default="dev")
    parser.add_argument("--site-key", action="append", help="Limit discovery to one or more site keys.")
    parser.add_argument("--max-depth", type=int, default=3, help="Shallow BFS page depth for discovery.")
    parser.add_argument("--section-depth", type=int, default=3, help="Max path prefix depth to aggregate into sections.")
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Optional emergency safety stop for HTML pages per site. Omit it for depth-bounded full structure discovery.",
    )
    parser.add_argument(
        "--detect-documents",
        action="store_true",
        help="Also count in-scope document links during discovery. The default structure-first mode skips document detection.",
    )
    parser.add_argument(
        "--level3-sample-limit",
        type=int,
        default=2,
        help="How many deeper candidate pages to sample under each level-2 branch during adaptive discovery.",
    )
    parser.add_argument("--yaml-path", type=Path, help="Optional YAML output path.")
    parser.add_argument("--report-path", type=Path, help="Optional Markdown output path.")
    args = parser.parse_args()

    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    inventory = discover_sections(
        catalog=args.catalog,
        site_keys=site_keys,
        discovery_depth=args.max_depth,
        section_depth=args.section_depth,
        max_pages=args.max_pages,
        detect_documents=args.detect_documents,
        level3_sample_limit=max(1, args.level3_sample_limit),
    )

    yaml_path = args.yaml_path or build_default_yaml_path(args.catalog)
    report_path = args.report_path or build_default_report_path(args.catalog)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    yaml_text = render_yaml(inventory.to_dict())
    markdown = render_markdown(inventory)
    yaml_path.write_text(yaml_text, encoding="utf-8")
    report_path.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
