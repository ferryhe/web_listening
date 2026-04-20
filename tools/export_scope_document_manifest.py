from __future__ import annotations

import argparse
from pathlib import Path

from web_listening.blocks.staged_workflow import export_manifest as staged_export_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export a machine-readable manifest for downloaded scope documents.")
    parser.add_argument("--scope-path", required=True, type=Path, help="Path to monitor_scope.yaml.")
    parser.add_argument("--run-id", type=int, help="Optional crawl run id. Defaults to the scope baseline run.")
    parser.add_argument("--yaml-path", type=Path, help="Optional YAML output path.")
    parser.add_argument("--report-path", type=Path, help="Optional Markdown output path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifacts = staged_export_manifest(
        scope_path=args.scope_path,
        run_id=args.run_id,
        yaml_path=args.yaml_path,
        report_path=args.report_path,
    )
    print(f"Wrote YAML to {artifacts.yaml_path}")
    print(f"Wrote report to {artifacts.report_path}")


if __name__ == "__main__":
    main()
