from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_markdown, render_yaml_text
from web_listening.blocks.monitor_scope_planner import load_section_selection
from web_listening.config import settings


def build_default_yaml_path(site_key: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "plans" / f"monitor_scope_{site_key}_{report_date}.yaml"


def build_default_report_path(site_key: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"monitor_scope_{site_key}_{report_date}.md"


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
    parser.add_argument("--yaml-path", help="Where to write the compiled monitor_scope YAML.")
    parser.add_argument("--report-path", help="Where to write the Markdown report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    selection = load_section_selection(args.selection_path)
    yaml_path = Path(args.yaml_path) if args.yaml_path else build_default_yaml_path(selection.site_key)
    report_path = Path(args.report_path) if args.report_path else build_default_report_path(selection.site_key)

    plan = build_monitor_scope(
        args.selection_path,
        classification_path=args.classification_path,
        file_scope_mode=args.file_scope_mode,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_files=args.max_files,
    )

    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(render_yaml_text(plan), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(plan), encoding="utf-8")

    print(f"Wrote YAML to {yaml_path}")
    print(f"Wrote report to {report_path}")


if __name__ == "__main__":
    main()
