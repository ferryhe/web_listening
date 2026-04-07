from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_crawler import TreeCrawler
from web_listening.config import settings
from web_listening.models import CrawlScope, Site
from web_listening.tree_defaults import PRODUCTION_TREE_LIMITS
from web_listening.tree_targets import TreeTarget, filter_tree_targets, load_tree_targets


def build_default_report_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"tree_run_{catalog}_{report_date}.md"


@dataclass(slots=True)
class RunResult:
    catalog: str
    site_key: str
    display_name: str
    seed_url: str
    scope_id: int | None
    run_id: int | None
    status: str
    pages_seen: int
    files_seen: int
    new_pages: int
    changed_pages: int
    missing_pages: int
    new_files: int
    changed_files: int
    missing_files: int
    page_failures: int
    file_failures: int
    notes: str


def find_scope(storage: Storage, target: TreeTarget) -> tuple[Site | None, CrawlScope | None]:
    expected_name = f"{target.display_name} Tree"
    matched_site = None
    for site in storage.list_sites(active_only=False):
        if site.name == expected_name and site.url == target.seed_url:
            matched_site = site
            break
    if matched_site is None:
        return None, None

    for scope in storage.list_crawl_scopes(site_id=matched_site.id):
        if (
            scope.seed_url == target.seed_url
            and scope.allowed_page_prefixes == target.allowed_page_prefixes
            and scope.allowed_file_prefixes == target.allowed_file_prefixes
        ):
            return matched_site, scope
    return matched_site, None


def run_incremental(
    *,
    catalog: str,
    max_depth: int,
    max_pages: int,
    max_files: int,
    site_keys: set[str] | None = None,
    download_files: bool = False,
) -> list[RunResult]:
    targets = filter_tree_targets(load_tree_targets(catalog), site_keys)
    storage = Storage(settings.db_path)
    processor = DocumentProcessor(storage=storage) if download_files else None
    results: list[RunResult] = []

    try:
        with TreeCrawler(storage=storage, document_processor=processor) as tree:
            for target in targets:
                site, scope = find_scope(storage, target)
                if site is None or scope is None:
                    results.append(
                        RunResult(
                            catalog=target.catalog,
                            site_key=target.site_key,
                            display_name=target.display_name,
                            seed_url=target.seed_url,
                            scope_id=scope.id if scope else None,
                            run_id=None,
                            status="missing_scope",
                            pages_seen=0,
                            files_seen=0,
                            new_pages=0,
                            changed_pages=0,
                            missing_pages=0,
                            new_files=0,
                            changed_files=0,
                            missing_files=0,
                            page_failures=0,
                            file_failures=0,
                            notes="Run bootstrap_site_tree.py first.",
                        )
                    )
                    continue

                scope = CrawlScope(
                    **{
                        **scope.model_dump(),
                        "max_depth": max_depth,
                        "max_pages": max_pages,
                        "max_files": max_files,
                        "fetch_mode": target.fetch_mode,
                        "fetch_config_json": target.fetch_config_json,
                    }
                )
                try:
                    crawl = tree.run_scope(
                        scope,
                        institution=target.display_name,
                        download_files=download_files,
                    )
                    results.append(
                        RunResult(
                            catalog=target.catalog,
                            site_key=target.site_key,
                            display_name=target.display_name,
                            seed_url=target.seed_url,
                            scope_id=crawl.scope.id,
                            run_id=crawl.run.id,
                            status=crawl.run.status,
                            pages_seen=len(crawl.pages),
                            files_seen=len(crawl.files),
                            new_pages=len(crawl.new_pages),
                            changed_pages=len(crawl.changed_pages),
                            missing_pages=len(crawl.missing_pages),
                            new_files=len(crawl.new_files),
                            changed_files=len(crawl.changed_files),
                            missing_files=len(crawl.missing_files),
                            page_failures=len(crawl.page_failures),
                            file_failures=len(crawl.file_failures),
                            notes=target.notes,
                        )
                    )
                except Exception as exc:  # pragma: no cover - live failure path
                    results.append(
                        RunResult(
                            catalog=target.catalog,
                            site_key=target.site_key,
                            display_name=target.display_name,
                            seed_url=target.seed_url,
                            scope_id=scope.id,
                            run_id=None,
                            status="failed",
                            pages_seen=0,
                            files_seen=0,
                            new_pages=0,
                            changed_pages=0,
                            missing_pages=0,
                            new_files=0,
                            changed_files=0,
                            missing_files=0,
                            page_failures=1,
                            file_failures=0,
                            notes=f"{target.notes} {type(exc).__name__}: {exc}".strip(),
                        )
                    )
    finally:
        storage.close()

    return results


def render_markdown(
    results: list[RunResult],
    *,
    catalog: str,
    max_depth: int,
    max_pages: int,
    max_files: int,
    download_files: bool,
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    completed = [item for item in results if item.status == "completed"]
    missing_scope = [item for item in results if item.status == "missing_scope"]
    failed = [item for item in results if item.status == "failed"]
    total_new_pages = sum(item.new_pages for item in completed)
    total_changed_pages = sum(item.changed_pages for item in completed)
    total_missing_pages = sum(item.missing_pages for item in completed)
    total_new_files = sum(item.new_files for item in completed)
    total_changed_files = sum(item.changed_files for item in completed)
    total_missing_files = sum(item.missing_files for item in completed)

    lines = [
        "# Run Site Tree",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{generated_at}`",
        (
            f"- Scope: catalog=`{catalog}`, targets=`{len(results)}`, max_depth=`{max_depth}`, "
            f"max_pages=`{max_pages}`, max_files=`{max_files}`, download_files=`{'yes' if download_files else 'no'}`."
        ),
        (
            f"- Final result: completed=`{len(completed)}`, missing_scope=`{len(missing_scope)}`, failed=`{len(failed)}`."
        ),
        (
            f"- Change totals: new_pages=`{total_new_pages}`, changed_pages=`{total_changed_pages}`, "
            f"missing_pages=`{total_missing_pages}`, new_files=`{total_new_files}`, "
            f"changed_files=`{total_changed_files}`, missing_files=`{total_missing_files}`."
        ),
        "",
        f"- Generated at: `{generated_at}`",
        f"- Database path: `{settings.db_path}`",
        "",
        "| Site | Catalog | Status | Pages seen | Files seen | New pages | Changed pages | Missing pages | New files | Changed files | Missing files | File failures |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item.display_name} | {item.catalog} | {item.status} | {item.pages_seen} | {item.files_seen} | "
            f"{item.new_pages} | {item.changed_pages} | {item.missing_pages} | {item.new_files} | "
            f"{item.changed_files} | {item.missing_files} | {item.file_failures} |"
        )

    lines.extend(["", "## Details", ""])
    for item in results:
        lines.append(f"### {item.display_name}")
        lines.append("")
        lines.append(f"- Site key: `{item.site_key}`")
        lines.append(f"- Seed URL: `{item.seed_url}`")
        lines.append(f"- Status: `{item.status}`")
        lines.append(f"- Scope ID: `{item.scope_id}`")
        lines.append(f"- Run ID: `{item.run_id}`")
        lines.append(f"- Pages seen this run: `{item.pages_seen}`")
        lines.append(f"- Files seen this run: `{item.files_seen}`")
        lines.append(f"- New pages: `{item.new_pages}`")
        lines.append(f"- Changed pages: `{item.changed_pages}`")
        lines.append(f"- Missing pages: `{item.missing_pages}`")
        lines.append(f"- New files: `{item.new_files}`")
        lines.append(f"- Changed files: `{item.changed_files}`")
        lines.append(f"- Missing files: `{item.missing_files}`")
        lines.append(f"- Page failures: `{item.page_failures}`")
        lines.append(f"- File failures: `{item.file_failures}`")
        if item.notes:
            lines.append(f"- Notes: `{item.notes}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run incremental bounded recursive tree monitoring against initialized scopes."
    )
    parser.add_argument("--catalog", choices=("dev", "smoke", "all"), default="dev")
    parser.add_argument("--max-depth", type=int, default=PRODUCTION_TREE_LIMITS.max_depth)
    parser.add_argument("--max-pages", type=int, default=PRODUCTION_TREE_LIMITS.max_pages)
    parser.add_argument("--max-files", type=int, default=PRODUCTION_TREE_LIMITS.max_files)
    parser.add_argument("--download-files", action="store_true")
    parser.add_argument("--site-key", action="append", help="Limit the run to one or more site keys.")
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional Markdown output path. Defaults to data/reports/tree_run_<catalog>_YYYY-MM-DD.md",
    )
    args = parser.parse_args()

    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    results = run_incremental(
        catalog=args.catalog,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_files=args.max_files,
        site_keys=site_keys,
        download_files=args.download_files,
    )
    markdown = render_markdown(
        results,
        catalog=args.catalog,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        max_files=args.max_files,
        download_files=args.download_files,
    )
    report_path = args.report_path or build_default_report_path(args.catalog)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
