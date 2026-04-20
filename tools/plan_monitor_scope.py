from __future__ import annotations

import argparse
from pathlib import Path

from web_listening.blocks.staged_workflow import plan_scope as staged_plan_scope


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile a section selection artifact into a monitor scope YAML.")
    parser.add_argument("--selection-path", required=True, help="Path to section_selection.yaml.")
    parser.add_argument(
        "--classification-path",
        help="Optional override for the section classification YAML. Defaults to selection.based_on.section_classification.",
    )
    parser.add_argument(
        "--file-scope-mode",
        choices=("site_root", "selected_pages"),
        default="site_root",
        help="How broad file download prefixes should be.",
    )
    parser.add_argument("--max-depth", type=int, help="Optional override for max_depth.")
    parser.add_argument("--max-pages", type=int, help="Optional override for max_pages.")
    parser.add_argument("--max-files", type=int, help="Optional override for max_files.")
    parser.add_argument("--yaml-path", type=Path, help="Where to write the compiled monitor_scope YAML.")
    parser.add_argument("--report-path", type=Path, help="Where to write the Markdown report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifacts = staged_plan_scope(
        selection_path=args.selection_path,
        classification_path=args.classification_path,
        file_scope_mode=args.file_scope_mode,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_files=args.max_files,
        yaml_path=args.yaml_path,
        report_path=args.report_path,
    )
    print(f"Wrote YAML to {artifacts.yaml_path}")
    print(f"Wrote report to {artifacts.report_path}")


if __name__ == "__main__":
    main()
