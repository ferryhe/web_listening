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
DEFAULT_REPORT_PATH = settings.data_dir / "reports" / "dev_daily_latest.md"


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


def render_markdown(results: list[DailyResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Dev Daily Monitor",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Target config: `{TARGETS_PATH}`",
        f"- Database path: `{settings.db_path}`",
        f"- Downloads path: `{settings.downloads_dir}`",
        "- Target set: `SOA`, `CAS`, `IAA`",
        "",
        "| Site | Kind | Initialized today | Changed | New links | New docs | Words | Doc links | Downloads |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.site_name} | {result.kind} | {'yes' if result.initialized_today else 'no'} | "
            f"{'yes' if result.changed else 'no'} | {result.new_link_count} | {result.new_doc_count} | "
            f"{result.word_count} | {result.doc_link_count} | {len(result.sample_downloads)} |"
        )

    lines.extend(["", "## Details", ""])
    for result in results:
        lines.append(f"### {result.site_name} {result.kind.title()}")
        lines.append("")
        lines.append(f"- Requested URL: `{result.url}`")
        lines.append(f"- Final URL: `{result.final_url}`")
        lines.append(f"- Status code: `{result.status_code}`")
        lines.append(f"- Initialized today: `{'yes' if result.initialized_today else 'no'}`")
        lines.append(f"- Changed against previous stored snapshot: `{'yes' if result.changed else 'no'}`")
        lines.append(f"- New links: `{result.new_link_count}`")
        lines.append(f"- New document links: `{result.new_doc_count}`")
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
        default=DEFAULT_REPORT_PATH,
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
