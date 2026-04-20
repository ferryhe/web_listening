from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan, monitor_scope_to_tree_target
from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_crawler import TreeCrawler, build_scope_from_site
from web_listening.config import settings
from web_listening.models import CrawlScope, Site
from web_listening.tree_defaults import PRODUCTION_TREE_LIMITS
from web_listening.tree_targets import TreeTarget, filter_tree_targets, load_tree_targets


def _safe_key(value: str) -> str:
    key = str(value or "catalog").strip().lower()
    key = key.replace("/", "-").replace("\\", "-")
    key = key.replace("..", "-")
    key = "-".join(part for part in key.split() if part)
    key = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in key)
    while "--" in key:
        key = key.replace("--", "-")
    return key.strip("-") or "catalog"


def build_default_report_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"tree_bootstrap_{_safe_key(catalog)}_{report_date}.md"


@dataclass(slots=True)
class BootstrapResult:
    catalog: str
    site_key: str
    display_name: str
    seed_url: str
    scope_id: int | None
    run_id: int | None
    status: str
    skipped_reason: str
    pages: int
    files: int
    child_pages: int
    page_failures: int
    file_failures: int
    skipped_external_pages: int
    skipped_external_files: int
    off_prefix_same_origin_files: int
    notes: str


def ensure_tree_site(storage: Storage, target: TreeTarget) -> Site:
    expected_name = f"{target.display_name} Tree"
    expected_tags = ["tree-target", target.catalog, target.site_key]
    for site in storage.list_sites(active_only=False):
        if site.name == expected_name and site.url == target.seed_url:
            return site
    return storage.add_site(
        Site(
            url=target.seed_url,
            name=expected_name,
            tags=expected_tags,
            fetch_mode=target.fetch_mode,
            fetch_config_json=target.fetch_config_json,
        )
    )


def ensure_tree_scope(
    storage: Storage,
    *,
    site: Site,
    target: TreeTarget,
    max_depth: int,
    max_pages: int,
    max_files: int,
) -> CrawlScope:
    desired = build_scope_from_site(
        site,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        allowed_page_prefixes=target.allowed_page_prefixes,
        allowed_file_prefixes=target.allowed_file_prefixes,
    )
    for scope in storage.list_crawl_scopes(site_id=site.id):
        if (
            scope.seed_url == desired.seed_url
            and scope.allowed_page_prefixes == desired.allowed_page_prefixes
            and scope.allowed_file_prefixes == desired.allowed_file_prefixes
        ):
            return CrawlScope(
                **{
                    **scope.model_dump(),
                    "max_depth": max_depth,
                    "max_pages": max_pages,
                    "max_files": max_files,
                    "fetch_mode": target.fetch_mode,
                    "fetch_config_json": target.fetch_config_json,
                }
            )
    return desired


def run_bootstrap(
    *,
    catalog: str,
    max_depth: int,
    max_pages: int,
    max_files: int,
    site_keys: set[str] | None = None,
    download_files: bool = False,
    refresh_existing: bool = False,
    targets: list[TreeTarget] | None = None,
) -> list[BootstrapResult]:
    resolved_targets = targets if targets is not None else filter_tree_targets(load_tree_targets(catalog), site_keys)
    storage = Storage(settings.db_path)
    processor = DocumentProcessor(storage=storage) if download_files else None
    results: list[BootstrapResult] = []

    try:
        with TreeCrawler(storage=storage, document_processor=processor) as tree:
            for target in resolved_targets:
                effective_max_depth = target.tree_max_depth or max_depth
                effective_max_pages = target.tree_max_pages or max_pages
                effective_max_files = target.tree_max_files or max_files
                site = ensure_tree_site(storage, target)
                scope = ensure_tree_scope(
                    storage,
                    site=site,
                    target=target,
                    max_depth=effective_max_depth,
                    max_pages=effective_max_pages,
                    max_files=effective_max_files,
                )
                if scope.id is not None and scope.is_initialized and not refresh_existing:
                    results.append(
                        BootstrapResult(
                            catalog=target.catalog,
                            site_key=target.site_key,
                            display_name=target.display_name,
                            seed_url=target.seed_url,
                            scope_id=scope.id,
                            run_id=scope.baseline_run_id,
                            status="skipped",
                            skipped_reason="already_initialized",
                            pages=0,
                            files=0,
                            child_pages=0,
                            page_failures=0,
                            file_failures=0,
                            skipped_external_pages=0,
                            skipped_external_files=0,
                            off_prefix_same_origin_files=0,
                            notes=target.notes,
                        )
                    )
                    continue

                try:
                    crawl = tree.bootstrap_scope(
                        scope,
                        institution=target.display_name,
                        download_files=download_files,
                    )
                    results.append(
                        BootstrapResult(
                            catalog=target.catalog,
                            site_key=target.site_key,
                            display_name=target.display_name,
                            seed_url=target.seed_url,
                            scope_id=crawl.scope.id,
                            run_id=crawl.run.id,
                            status=crawl.run.status,
                            skipped_reason="",
                            pages=len(crawl.pages),
                            files=len(crawl.files),
                            child_pages=max(0, len(crawl.pages) - 1),
                            page_failures=len(crawl.page_failures),
                            file_failures=len(crawl.file_failures),
                            skipped_external_pages=crawl.skipped_external_pages,
                            skipped_external_files=crawl.skipped_external_files,
                            off_prefix_same_origin_files=crawl.off_prefix_same_origin_files,
                            notes=target.notes,
                        )
                    )
                except Exception as exc:  # pragma: no cover - live failure path
                    results.append(
                        BootstrapResult(
                            catalog=target.catalog,
                            site_key=target.site_key,
                            display_name=target.display_name,
                            seed_url=target.seed_url,
                            scope_id=scope.id,
                            run_id=None,
                            status="failed",
                            skipped_reason="",
                            pages=0,
                            files=0,
                            child_pages=0,
                            page_failures=1,
                            file_failures=0,
                            skipped_external_pages=0,
                            skipped_external_files=0,
                            off_prefix_same_origin_files=0,
                            notes=f"{target.notes} {type(exc).__name__}: {exc}".strip(),
                        )
                    )
    finally:
        storage.close()

    return results


def render_markdown(
    results: list[BootstrapResult],
    *,
    catalog: str,
    max_depth: int,
    max_pages: int,
    max_files: int,
    download_files: bool,
    refresh_existing: bool,
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    completed = [item for item in results if item.status == "completed"]
    skipped = [item for item in results if item.status == "skipped"]
    failed = [item for item in results if item.status == "failed"]
    total_pages = sum(item.pages for item in completed)
    total_files = sum(item.files for item in completed)

    lines = [
        "# Bootstrap Site Tree",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{generated_at}`",
        (
            f"- Scope: catalog=`{catalog}`, targets=`{len(results)}`, max_depth=`{max_depth}`, "
            f"max_pages=`{max_pages}`, max_files=`{max_files}`, download_files=`{'yes' if download_files else 'no'}`."
        ),
        (
            f"- Final result: completed=`{len(completed)}`, skipped=`{len(skipped)}`, failed=`{len(failed)}`. "
            f"Bootstrap inventory discovered `{total_pages}` pages and `{total_files}` accepted files."
        ),
        (
            f"- Operational note: bootstrap establishes the baseline only. Re-run later with `web-listening run-scope` "
            f"to detect new pages, changed content, new files, and missing items against this baseline."
        ),
        "",
        f"- Generated at: `{generated_at}`",
        f"- Database path: `{settings.db_path}`",
        f"- Refresh existing scopes: `{'yes' if refresh_existing else 'no'}`",
        "",
        "| Site | Catalog | Status | Pages | Child pages | Files | Page failures | File failures | Scope ID | Run ID |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]

    for item in results:
        lines.append(
            f"| {item.display_name} | {item.catalog} | {item.status} | {item.pages} | {item.child_pages} | "
            f"{item.files} | {item.page_failures} | {item.file_failures} | {item.scope_id or 0} | {item.run_id or 0} |"
        )

    lines.extend(["", "## Details", ""])
    for item in results:
        lines.append(f"### {item.display_name}")
        lines.append("")
        lines.append(f"- Site key: `{item.site_key}`")
        lines.append(f"- Seed URL: `{item.seed_url}`")
        lines.append(f"- Status: `{item.status}`")
        if item.skipped_reason:
            lines.append(f"- Skipped reason: `{item.skipped_reason}`")
        lines.append(f"- Scope ID: `{item.scope_id}`")
        lines.append(f"- Run ID: `{item.run_id}`")
        lines.append(f"- Pages discovered: `{item.pages}`")
        lines.append(f"- Child pages discovered: `{item.child_pages}`")
        lines.append(f"- Accepted files: `{item.files}`")
        lines.append(f"- Page failures: `{item.page_failures}`")
        lines.append(f"- File failures: `{item.file_failures}`")
        lines.append(f"- Skipped external pages: `{item.skipped_external_pages}`")
        lines.append(f"- Skipped external files: `{item.skipped_external_files}`")
        lines.append(f"- Off-prefix same-origin files: `{item.off_prefix_same_origin_files}`")
        if item.notes:
            lines.append(f"- Notes: `{item.notes}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bootstrap bounded recursive tree monitoring into the main database."
    )
    parser.add_argument("--catalog", choices=("dev", "smoke", "all"), default="dev")
    parser.add_argument("--scope-path", type=Path, help="Optional monitor_scope YAML path for a single targeted bootstrap.")
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-files", type=int, default=None)
    parser.add_argument("--download-files", action="store_true")
    parser.add_argument("--refresh-existing", action="store_true")
    parser.add_argument("--site-key", action="append", help="Limit the run to one or more site keys.")
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional Markdown output path. Defaults to data/reports/tree_bootstrap_<catalog>_YYYY-MM-DD.md",
    )
    args = parser.parse_args()

    if args.scope_path and args.site_key:
        parser.error("--site-key cannot be used together with --scope-path")

    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    targets: list[TreeTarget] | None = None
    report_catalog = args.catalog
    max_depth = args.max_depth or PRODUCTION_TREE_LIMITS.max_depth
    max_pages = args.max_pages or PRODUCTION_TREE_LIMITS.max_pages
    max_files = args.max_files or PRODUCTION_TREE_LIMITS.max_files

    if args.scope_path:
        scope_plan = load_monitor_scope_plan(args.scope_path)
        targets = [monitor_scope_to_tree_target(scope_plan)]
        report_catalog = f"scope_{scope_plan.site_key}"
        if args.max_depth is None:
            max_depth = scope_plan.max_depth
        if args.max_pages is None:
            max_pages = scope_plan.max_pages
        if args.max_files is None:
            max_files = scope_plan.max_files

    results = run_bootstrap(
        catalog=args.catalog,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        site_keys=site_keys,
        download_files=args.download_files,
        refresh_existing=args.refresh_existing,
        targets=targets,
    )
    markdown = render_markdown(
        results,
        catalog=report_catalog,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        download_files=args.download_files,
        refresh_existing=args.refresh_existing,
    )
    report_path = args.report_path or build_default_report_path(report_catalog)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
