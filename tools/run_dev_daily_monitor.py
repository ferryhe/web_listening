from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import compute_diff, find_document_links, find_new_links, select_compare_text
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.dev_targets import load_dev_targets
from web_listening.models import Change, Site


TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "dev_test_sites.json"


def build_default_report_path(now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"dev_daily_{report_date}.md"


@dataclass(slots=True)
class DailyTarget:
    site_key: str
    site_name: str
    kind: str
    url: str
    expected_min_words: int
    expected_min_doc_links: int
    sample_download_limit: int


@dataclass(slots=True)
class DownloadSample:
    url: str
    sha256: str
    file_size: int
    local_path: str


@dataclass(slots=True)
class DailyResult:
    site_key: str
    site_name: str
    kind: str
    url: str
    final_url: str
    status_code: int | None
    initialized_today: bool
    changed: bool
    new_link_count: int
    new_doc_count: int
    word_count: int
    page_link_count: int
    content_link_count: int
    doc_link_count: int
    hash_basis: str
    content_hash: str
    change_summaries: list[str]
    sample_downloads: list[DownloadSample]


def load_daily_targets() -> list[DailyTarget]:
    targets: list[DailyTarget] = []
    for item in load_dev_targets(TARGETS_PATH):
        targets.append(
            DailyTarget(
                site_key=item["site_key"],
                site_name=item["site_name"],
                kind="monitor",
                url=item["monitor_url"],
                expected_min_words=int(item["expected_min_monitor_words"]),
                expected_min_doc_links=0,
                sample_download_limit=0,
            )
        )
        targets.append(
            DailyTarget(
                site_key=item["site_key"],
                site_name=item["site_name"],
                kind="documents",
                url=item["document_url"],
                expected_min_words=int(item["expected_min_document_words"]),
                expected_min_doc_links=int(item["expected_min_doc_links"]),
                sample_download_limit=int(item["sample_download_limit"]),
            )
        )
    return targets


def ensure_site(storage: Storage, target: DailyTarget) -> Site:
    expected_name = f"{target.site_name} {target.kind.title()}"
    expected_tags = ["dev-target", target.site_key, target.kind]
    for site in storage.list_sites(active_only=False):
        if site.url == target.url and site.name == expected_name:
            return site
    return storage.add_site(
        Site(
            url=target.url,
            name=expected_name,
            tags=expected_tags,
        )
    )


def run_daily_monitor(
    *,
    report_path: Path | None = None,
    download_samples: bool = False,
    download_limit_override: int | None = None,
) -> str:
    storage = Storage(settings.db_path)
    results: list[DailyResult] = []

    try:
        with Crawler() as crawler, DocumentProcessor(storage=storage) as processor:
            for target in load_daily_targets():
                site = ensure_site(storage, target)
                previous = storage.get_latest_snapshot(site.id)
                snapshot = crawler.snapshot(site)
                doc_links = find_document_links(snapshot.links)

                initialized_today = previous is None
                changed = False
                new_links: list[str] = []
                new_docs: list[str] = []
                change_summaries: list[str] = []

                if previous is not None:
                    changed, diff_snippet = compute_diff(
                        select_compare_text(
                            fit_markdown=previous.fit_markdown,
                            markdown=previous.markdown,
                            content_text=previous.content_text,
                        ),
                        select_compare_text(
                            fit_markdown=snapshot.fit_markdown,
                            markdown=snapshot.markdown,
                            content_text=snapshot.content_text,
                        ),
                    )
                    if changed:
                        storage.add_change(
                            Change(
                                site_id=site.id,
                                detected_at=datetime.now(timezone.utc),
                                change_type="new_content",
                                summary=f"Content changed on {site.name}",
                                diff_snippet=diff_snippet,
                            )
                        )
                        change_summaries.append("content_changed")

                    new_links = find_new_links(previous.links, snapshot.links)
                    if new_links:
                        storage.add_change(
                            Change(
                                site_id=site.id,
                                detected_at=datetime.now(timezone.utc),
                                change_type="new_links",
                                summary=f"{len(new_links)} new links found on {site.name}",
                                diff_snippet="\n".join(new_links[:10]),
                            )
                        )
                        change_summaries.append(f"new_links={len(new_links)}")

                    new_docs = find_document_links(new_links)
                    if new_docs:
                        storage.add_change(
                            Change(
                                site_id=site.id,
                                detected_at=datetime.now(timezone.utc),
                                change_type="new_document",
                                summary=f"{len(new_docs)} new document links on {site.name}",
                                diff_snippet="\n".join(new_docs[:10]),
                            )
                        )
                        change_summaries.append(f"new_documents={len(new_docs)}")

                storage.add_snapshot(snapshot)
                storage.update_site_checked(site.id)

                sample_downloads: list[DownloadSample] = []
                if download_samples and target.kind == "documents":
                    limit = download_limit_override if download_limit_override is not None else target.sample_download_limit
                    for document_url in doc_links[: max(0, limit)]:
                        doc = storage.add_document(
                            processor.process(
                                document_url,
                                site_id=site.id,
                                institution=target.site_name,
                                page_url=target.url,
                            )
                        )
                        sample_downloads.append(
                            DownloadSample(
                                url=document_url,
                                sha256=doc.sha256,
                                file_size=doc.file_size or 0,
                                local_path=doc.local_path,
                            )
                        )

                results.append(
                    DailyResult(
                        site_key=target.site_key,
                        site_name=target.site_name,
                        kind=target.kind,
                        url=target.url,
                        final_url=snapshot.final_url,
                        status_code=snapshot.status_code,
                        initialized_today=initialized_today,
                        changed=changed,
                        new_link_count=len(new_links),
                        new_doc_count=len(new_docs),
                        word_count=int(snapshot.metadata_json.get("word_count", 0)),
                        page_link_count=len(snapshot.links),
                        content_link_count=int(snapshot.metadata_json.get("link_count", 0)),
                        doc_link_count=len(doc_links),
                        hash_basis=str(snapshot.metadata_json.get("hash_basis", "")),
                        content_hash=snapshot.content_hash,
                        change_summaries=change_summaries,
                        sample_downloads=sample_downloads,
                    )
                )
    finally:
        storage.close()

    markdown = render_markdown(results)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(markdown, encoding="utf-8")
    return markdown


def build_final_conclusion(results: list[DailyResult], generated_at: str) -> list[str]:
    monitored_sites = len({result.site_key for result in results})
    monitor_pages = [result for result in results if result.kind == "monitor"]
    document_pages = [result for result in results if result.kind == "documents"]
    reachable_count = sum(1 for result in results if (result.status_code or 0) < 400)
    changed_results = [result for result in results if result.changed]
    new_link_results = [result for result in results if result.new_link_count > 0]
    new_doc_results = [result for result in results if result.new_doc_count > 0]
    stable_results = [
        result
        for result in results
        if not result.changed and result.new_link_count == 0 and result.new_doc_count == 0
    ]

    changed_labels = ", ".join(
        f"{result.site_name} {result.kind}" for result in changed_results
    ) or "none"
    new_link_labels = ", ".join(
        f"{result.site_name} {result.kind}" for result in new_link_results
    ) or "none"
    new_doc_labels = ", ".join(
        f"{result.site_name} {result.kind}" for result in new_doc_results
    ) or "none"
    stable_labels = ", ".join(
        f"{result.site_name} {result.kind}" for result in stable_results
    ) or "none"

    total_page_links = sum(result.page_link_count for result in results)
    total_content_links = sum(result.content_link_count for result in results)
    total_visible_doc_links = sum(result.doc_link_count for result in results)
    total_new_links = sum(result.new_link_count for result in results)
    total_new_docs = sum(result.new_doc_count for result in results)
    total_downloads = sum(len(result.sample_downloads) for result in results)
    if new_link_results:
        agent_takeaway = (
            f"Prioritize the pages with new links: `{new_link_labels}`. "
            f"New document links in this run: `{total_new_docs}` on `{new_doc_labels}`. "
            f"`{total_downloads}` sample downloads still resolved cleanly."
        )
    else:
        agent_takeaway = (
            f"No new content, links, or document links were discovered in this run, "
            f"and `{total_downloads}` sample downloads still resolved cleanly. "
            f"The current agent-readable evidence surface is stable, with CAS and IAA "
            f"document pages still the richest sources."
        )

    return [
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{generated_at}`",
        (
            f"- Monitoring depth: `{monitored_sites}` institutions, `{len(results)}` entry pages "
            f"(`{len(monitor_pages)}` monitor + `{len(document_pages)}` document pages), "
            f"mode=`single-page snapshot, non-recursive`."
        ),
        (
            f"- Inventory totals: `{total_page_links}` extracted page links, "
            f"`{total_content_links}` content-area links, "
            f"`{total_visible_doc_links}` visible document links, "
            f"`{total_downloads}` sample documents verified."
        ),
        (
            f"- Change totals: `{len(changed_results)}` pages with new content, "
            f"`{total_new_links}` new links, `{total_new_docs}` new document links/files."
        ),
        (
            f"- Final result: `{reachable_count}/{len(results)}` target pages were reachable. "
            f"Content changed on `{len(changed_results)}` pages: `{changed_labels}`. "
            f"Pages with newly discovered links: `{new_link_labels}`. "
            f"Pages with newly discovered files: `{new_doc_labels}`. "
            f"Stable pages with no detected changes: `{stable_labels}`."
        ),
        (
            f"- Agent takeaway: `{agent_takeaway}`"
        ),
        "",
    ]


def render_markdown(results: list[DailyResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Dev Daily Monitor",
        "",
    ]
    lines.extend(build_final_conclusion(results, generated_at))
    lines.extend(
        [
        f"- Generated at: `{generated_at}`",
        f"- Target config: `{TARGETS_PATH}`",
        f"- Database path: `{settings.db_path}`",
        f"- Downloads path: `{settings.downloads_dir}`",
        f"- Default report file: `{build_default_report_path()}`",
        "- Target set: `SOA`, `CAS`, `IAA`",
        "",
        "| Site | Kind | Status | Content changed | New links | New files | Page links | Content links | Doc links | Words | Downloads |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for result in results:
        lines.append(
            f"| {result.site_name} | {result.kind} | {result.status_code} | "
            f"{'yes' if result.changed else 'no'} | {result.new_link_count} | {result.new_doc_count} | "
            f"{result.page_link_count} | {result.content_link_count} | {result.doc_link_count} | "
            f"{result.word_count} | {len(result.sample_downloads)} |"
        )

    lines.extend(["", "## Details", ""])
    for result in results:
        lines.append(f"### {result.site_name} {result.kind.title()}")
        lines.append("")
        lines.append(f"- Requested URL: `{result.url}`")
        lines.append(f"- Final URL: `{result.final_url}`")
        lines.append(f"- Status code: `{result.status_code}`")
        lines.append(f"- Initialized today: `{'yes' if result.initialized_today else 'no'}`")
        lines.append("- Monitoring depth: `entry page only; no recursive crawl in this report`")
        lines.append(f"- Changed against previous stored snapshot: `{'yes' if result.changed else 'no'}`")
        lines.append(f"- New links: `{result.new_link_count}`")
        lines.append(f"- New document links: `{result.new_doc_count}`")
        lines.append(f"- Extracted page links: `{result.page_link_count}`")
        lines.append(f"- Content-area links: `{result.content_link_count}`")
        lines.append(f"- Word count: `{result.word_count}`")
        lines.append(f"- Document links currently visible: `{result.doc_link_count}`")
        lines.append(f"- Hash basis: `{result.hash_basis}`")
        lines.append(f"- Content hash: `{result.content_hash}`")
        if result.change_summaries:
            lines.append(f"- Changes recorded: `{', '.join(result.change_summaries)}`")
        if result.sample_downloads:
            lines.append("- Sample downloads:")
            for sample in result.sample_downloads:
                lines.append(
                    f"  - `{sample.url}` -> sha256=`{sample.sha256}` size=`{sample.file_size}` "
                    f"path=`{sample.local_path}`"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist daily SOA/CAS/IAA monitoring snapshots into the main database."
    )
    parser.add_argument(
        "--download-samples",
        action="store_true",
        help="Download sample documents from the document pages and persist them in the shared blob store.",
    )
    parser.add_argument(
        "--download-limit-override",
        type=int,
        help="Override the per-target sample download limit from config/dev_test_sites.json.",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=build_default_report_path(),
        help="Path to write the Markdown report.",
    )
    args = parser.parse_args()

    markdown = run_daily_monitor(
        report_path=args.report_path,
        download_samples=args.download_samples,
        download_limit_override=args.download_limit_override,
    )
    print(markdown)


if __name__ == "__main__":
    main()
