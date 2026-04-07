from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from web_listening.blocks.section_classifier import (
    SectionClassifier,
    load_section_inventory,
    render_markdown,
    render_yaml_text,
)
from web_listening.config import settings


def build_default_inventory_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "plans" / f"section_inventory_{catalog}_{report_date}.yaml"


def build_default_yaml_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "plans" / f"section_classification_{catalog}_{report_date}.yaml"


def build_default_report_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"section_classification_{catalog}_{report_date}.md"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify discovered second- and third-level site sections before targeted tree monitoring."
    )
    parser.add_argument("--catalog", choices=("dev", "smoke", "all"), default="dev")
    parser.add_argument("--site-key", action="append", help="Limit classification to one or more site keys.")
    parser.add_argument("--inventory-path", type=Path, help="Optional input section inventory YAML path.")
    parser.add_argument("--yaml-path", type=Path, help="Optional classified YAML output path.")
    parser.add_argument("--report-path", type=Path, help="Optional classified Markdown output path.")
    parser.add_argument(
        "--use-ai",
        action="store_true",
        help="Use OpenAI to refine section categories and reasons when WL_OPENAI_API_KEY is set.",
    )
    args = parser.parse_args()

    inventory_path = args.inventory_path or build_default_inventory_path(args.catalog)
    inventory = load_section_inventory(inventory_path)
    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None

    classifier = SectionClassifier()
    classification = classifier.classify_inventory(
        inventory,
        inventory_path=str(inventory_path),
        use_ai=args.use_ai,
        site_keys=site_keys,
    )

    yaml_path = args.yaml_path or build_default_yaml_path(args.catalog)
    report_path = args.report_path or build_default_report_path(args.catalog)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    yaml_path.write_text(render_yaml_text(classification), encoding="utf-8")
    markdown = render_markdown(classification)
    report_path.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
