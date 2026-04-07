from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from web_listening.blocks.document_manifest import (
    build_scope_document_manifest,
    render_markdown,
    render_yaml_text,
)
from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
from web_listening.blocks.storage import Storage
from web_listening.config import settings


def build_default_yaml_path(site_key: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "plans" / f"document_manifest_{site_key}_{report_date}.yaml"


def build_default_report_path(site_key: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"document_manifest_{site_key}_{report_date}.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a machine-readable manifest for downloaded scope documents.")
    parser.add_argument("--scope-path", required=True, type=Path, help="Path to monitor_scope.yaml.")
    parser.add_argument("--run-id", type=int, help="Optional crawl run id. Defaults to the scope baseline run.")
    parser.add_argument("--yaml-path", type=Path, help="Optional YAML output path.")
    parser.add_argument("--report-path", type=Path, help="Optional Markdown output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plan = load_monitor_scope_plan(args.scope_path)
    yaml_path = args.yaml_path or build_default_yaml_path(plan.site_key)
    report_path = args.report_path or build_default_report_path(plan.site_key)

    storage = Storage(settings.db_path)
    try:
        manifest = build_scope_document_manifest(args.scope_path, storage=storage, run_id=args.run_id)
    finally:
        storage.close()

    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(render_yaml_text(manifest), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_markdown(manifest), encoding="utf-8")

    print(f"Wrote YAML to {yaml_path}")
    print(f"Wrote report to {report_path}")


if __name__ == "__main__":
    main()
