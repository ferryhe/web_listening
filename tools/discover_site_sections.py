from __future__ import annotations

import argparse
from pathlib import Path

from web_listening.blocks.staged_workflow import discover_sections as staged_discover_sections


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    artifacts = staged_discover_sections(
        catalog=args.catalog,
        site_keys=site_keys,
        discovery_depth=args.max_depth,
        section_depth=args.section_depth,
        max_pages=args.max_pages,
        detect_documents=args.detect_documents,
        level3_sample_limit=max(1, args.level3_sample_limit),
        yaml_path=args.yaml_path,
        report_path=args.report_path,
    )
    print(artifacts.report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
