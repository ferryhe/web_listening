from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_crawler import TreeCrawler, build_scope_from_site
from web_listening.models import Site
from web_listening.smoke_sites import load_smoke_sites

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"


@dataclass(slots=True)
class TreeValidationResult:
    site_key: str
    abbreviation: str
    smoke_required: bool
    homepage_url: str
    monitor_url: str
    fetch_mode: str
    root_status: str
    pages: int
    files: int
    child_pages: int
    page_failures: int
    skipped_external_pages: int
    skipped_external_files: int
    off_prefix_same_origin_files: int
    outcome: str
    notes: str


def classify_outcome(result: TreeValidationResult) -> str:
    if result.pages == 0:
        return "blocked_root"
    if result.child_pages == 0:
        return "root_only"
    if result.page_failures >= max(10, result.pages * 3):
        return "unstable_tree"
    if result.pages < 3:
        return "thin_tree"
    return "ok"


def run_validation(
    *,
    max_depth: int,
    max_pages: int,
    max_files: int,
    site_keys: set[str] | None = None,
    download_files: bool = False,
) -> list[TreeValidationResult]:
    entries = load_smoke_sites(CATALOG_PATH)
    if site_keys:
        entries = [item for item in entries if item["site_key"] in site_keys]

    results: list[TreeValidationResult] = []
    with tempfile.TemporaryDirectory(prefix="web_listening_tree_validation_") as temp_dir:
        temp_root = Path(temp_dir)
        storage = Storage(temp_root / "tree_validation.db")
        try:
            with TreeCrawler(storage=storage) as tree:
                for entry in entries:
                    seed_url = entry.get("tree_seed_url") or entry.get("monitor_url") or entry["homepage_url"]
                    site = storage.add_site(
                        Site(
                            url=seed_url,
                            name=entry["abbreviation"],
                            fetch_mode=entry["fetch_mode"],
                            fetch_config_json=entry["fetch_config_json"],
                        )
                    )
                    scope = build_scope_from_site(
                        site,
                        max_depth=max_depth,
                        max_pages=max_pages,
                        max_files=max_files,
                        allowed_page_prefixes=entry.get("tree_page_prefixes") or ["/"],
                        allowed_file_prefixes=entry.get("tree_file_prefixes") or ["/"],
                    )
                    try:
                        crawl = tree.bootstrap_scope(
                            scope,
                            institution=entry["abbreviation"],
                            download_files=download_files,
                        )
                        result = TreeValidationResult(
                            site_key=entry["site_key"],
                            abbreviation=entry["abbreviation"],
                            smoke_required=bool(entry["smoke_required"]),
                            homepage_url=entry["homepage_url"],
                            monitor_url=seed_url,
                            fetch_mode=entry["fetch_mode"],
                            root_status=crawl.run.status,
                            pages=len(crawl.pages),
                            files=len(crawl.files),
                            child_pages=max(0, len(crawl.pages) - 1),
                            page_failures=len(crawl.page_failures),
                            skipped_external_pages=crawl.skipped_external_pages,
                            skipped_external_files=crawl.skipped_external_files,
                            off_prefix_same_origin_files=crawl.off_prefix_same_origin_files,
                            outcome="",
                            notes=entry["notes"],
                        )
                    except Exception as exc:  # pragma: no cover - live failure path
                        result = TreeValidationResult(
                            site_key=entry["site_key"],
                            abbreviation=entry["abbreviation"],
                            smoke_required=bool(entry["smoke_required"]),
                            homepage_url=entry["homepage_url"],
                            monitor_url=seed_url,
                            fetch_mode=entry["fetch_mode"],
                            root_status="failed",
                            pages=0,
                            files=0,
                            child_pages=0,
                            page_failures=1,
                            skipped_external_pages=0,
                            skipped_external_files=0,
                            off_prefix_same_origin_files=0,
                            outcome="",
                            notes=f"{entry['notes']} {type(exc).__name__}: {exc}".strip(),
                        )
                    result.outcome = classify_outcome(result)
                    results.append(result)
        finally:
            storage.close()
    return results


def render_markdown(results: list[TreeValidationResult], *, max_depth: int, max_pages: int, max_files: int, download_files: bool) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    not_ok = [item for item in results if item.outcome != "ok"]
    lines = [
        "# Tree Catalog Validation",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Catalog path: `{CATALOG_PATH}`",
        f"- Max depth: `{max_depth}`",
        f"- Max pages per scope: `{max_pages}`",
        f"- Max files per scope: `{max_files}`",
        f"- Download files: `{'yes' if download_files else 'no'}`",
        f"- Sites checked: `{len(results)}`",
        f"- Sites not meeting current tree expectation: `{len(not_ok)}`",
        "",
        "| Site | Required | Outcome | Pages | Child pages | Files | Failures |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item.abbreviation} | {'yes' if item.smoke_required else 'no'} | {item.outcome} | "
            f"{item.pages} | {item.child_pages} | {item.files} | {item.page_failures} |"
        )

    lines.extend(["", "## Sites Not Meeting Current Tree Expectation", ""])
    if not not_ok:
        lines.append("- None")
    else:
        for item in not_ok:
            lines.append(f"### {item.abbreviation}")
            lines.append("")
            lines.append(f"- Homepage URL: `{item.homepage_url}`")
            lines.append(f"- Monitor URL: `{item.monitor_url}`")
            lines.append(f"- Outcome: `{item.outcome}`")
            lines.append(f"- Required smoke target: `{'yes' if item.smoke_required else 'no'}`")
            lines.append(f"- Pages discovered: `{item.pages}`")
            lines.append(f"- Child pages discovered: `{item.child_pages}`")
            lines.append(f"- File links accepted: `{item.files}`")
            lines.append(f"- Page failures: `{item.page_failures}`")
            lines.append(f"- Skipped external pages: `{item.skipped_external_pages}`")
            lines.append(f"- Skipped external files: `{item.skipped_external_files}`")
            lines.append(f"- Off-prefix same-origin files: `{item.off_prefix_same_origin_files}`")
            if item.notes:
                lines.append(f"- Notes: `{item.notes}`")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run bounded recursive tree validation against the curated smoke site catalog."
    )
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--max-files", type=int, default=1)
    parser.add_argument("--download-files", action="store_true")
    parser.add_argument("--site-key", action="append", help="Limit the run to one or more site keys.")
    args = parser.parse_args()

    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    results = run_validation(
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_files=args.max_files,
        site_keys=site_keys,
        download_files=args.download_files,
    )
    print(
        render_markdown(
            results,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
            max_files=args.max_files,
            download_files=args.download_files,
        )
    )


if __name__ == "__main__":
    main()
