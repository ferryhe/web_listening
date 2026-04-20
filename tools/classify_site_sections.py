from __future__ import annotations

import argparse
from pathlib import Path

from web_listening.blocks.staged_workflow import classify_sections as staged_classify_sections


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    artifacts = staged_classify_sections(
        catalog=args.catalog,
        inventory_path=args.inventory_path,
        site_keys=site_keys,
        use_ai=args.use_ai,
        yaml_path=args.yaml_path,
        report_path=args.report_path,
    )
    print(artifacts.report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
