from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import compute_diff, find_document_links, select_compare_text
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import Site

TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "dev_test_sites.json"


@dataclass(slots=True)
class DownloadCheck:
    document_url: str
    local_path: str
    sha256: str
    file_size: int
    doc_type: str


@dataclass(slots=True)
class SiteRegressionResult:
    site_name: str
    monitor_url: str
    document_url: str
    first_hash: str
    second_hash: str
    stable_hash: bool
    changed: bool
    diff_preview: str
    monitor_word_count: int
    document_page_word_count: int
    document_link_count: int
    expected_min_doc_links: int
    sample_downloads: list[DownloadCheck]


def load_targets() -> list[dict]:
    return json.loads(TARGETS_PATH.read_text(encoding="utf-8"))


def run_regression() -> list[SiteRegressionResult]:
    results: list[SiteRegressionResult] = []
    with tempfile.TemporaryDirectory(prefix="web_listening_dev_regression_") as temp_dir:
        temp_root = Path(temp_dir)
        db_path = temp_root / "regression.db"
        downloads_dir = temp_root / "downloads"
        settings.db_path = db_path
        settings.downloads_dir = downloads_dir
        storage = Storage(db_path)
        try:
            with Crawler() as crawler, DocumentProcessor(storage=storage) as processor:
                for index, target in enumerate(load_targets(), start=1):
                    site = storage.add_site(
                        Site(
                            url=target["monitor_url"],
                            name=target["site_name"],
                            tags=["dev-test"],
                        )
                    )
                    first_monitor = crawler.snapshot(site)
                    second_monitor = crawler.snapshot(site)
                    compare_left = select_compare_text(
                        fit_markdown=first_monitor.fit_markdown,
                        markdown=first_monitor.markdown,
                        content_text=first_monitor.content_text,
                    )
                    compare_right = select_compare_text(
                        fit_markdown=second_monitor.fit_markdown,
                        markdown=second_monitor.markdown,
                        content_text=second_monitor.content_text,
                    )
                    changed, diff_preview = compute_diff(compare_left, compare_right)

                    doc_site = Site(
                        id=index + 1000,
                        url=target["document_url"],
                        name=f"{target['site_name']} Documents",
                    )
                    document_snapshot = crawler.snapshot(doc_site)
                    doc_links = find_document_links(document_snapshot.links)

                    sample_downloads: list[DownloadCheck] = []
                    for document_url in doc_links[: int(target.get("sample_download_limit", 1))]:
                        document = processor.process(
                            document_url,
                            site_id=site.id,
                            institution=target["site_name"],
                            page_url=target["document_url"],
                        )
                        persisted = storage.add_document(document)
                        sample_downloads.append(
                            DownloadCheck(
                                document_url=document_url,
                                local_path=persisted.local_path,
                                sha256=persisted.sha256,
                                file_size=persisted.file_size or 0,
                                doc_type=persisted.doc_type,
                            )
                        )

                    results.append(
                        SiteRegressionResult(
                            site_name=target["site_name"],
                            monitor_url=target["monitor_url"],
                            document_url=target["document_url"],
                            first_hash=first_monitor.content_hash,
                            second_hash=second_monitor.content_hash,
                            stable_hash=first_monitor.content_hash == second_monitor.content_hash,
                            changed=changed,
                            diff_preview=diff_preview[:400],
                            monitor_word_count=int(first_monitor.metadata_json.get("word_count", 0)),
                            document_page_word_count=int(document_snapshot.metadata_json.get("word_count", 0)),
                            document_link_count=len(doc_links),
                            expected_min_doc_links=int(target.get("expected_min_doc_links", 0)),
                            sample_downloads=sample_downloads,
                        )
                    )
        finally:
            storage.close()
    return results


def render_markdown(results: list[SiteRegressionResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Dev Regression Report",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Target config: `{TARGETS_PATH}`",
        "- Regression scope: monitoring, hash stability, diff check, document discovery, sample download",
        "",
        "| Site | Monitor hash stable | Changed | Monitor words | Doc page words | Doc links | Expected min | Sample downloads |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.site_name} | {'yes' if result.stable_hash else 'no'} | "
            f"{'yes' if result.changed else 'no'} | {result.monitor_word_count} | "
            f"{result.document_page_word_count} | {result.document_link_count} | "
            f"{result.expected_min_doc_links} | {len(result.sample_downloads)} |"
        )

    lines.extend(["", "## Details", ""])
    for result in results:
        lines.append(f"### {result.site_name}")
        lines.append("")
        lines.append(f"- Monitor URL: `{result.monitor_url}`")
        lines.append(f"- Document URL: `{result.document_url}`")
        lines.append(f"- First content hash: `{result.first_hash}`")
        lines.append(f"- Second content hash: `{result.second_hash}`")
        lines.append(f"- Stable hash: `{'yes' if result.stable_hash else 'no'}`")
        lines.append(f"- Changed on immediate repeat fetch: `{'yes' if result.changed else 'no'}`")
        lines.append(f"- Monitor word count: `{result.monitor_word_count}`")
        lines.append(f"- Document page word count: `{result.document_page_word_count}`")
        lines.append(f"- Document links found: `{result.document_link_count}`")
        lines.append(f"- Expected minimum document links: `{result.expected_min_doc_links}`")
        if result.diff_preview:
            lines.append(f"- Diff preview: `{result.diff_preview}`")
        if result.sample_downloads:
            lines.append("- Sample downloads:")
            for sample in result.sample_downloads:
                lines.append(
                    f"  - `{sample.document_url}` -> `{sample.doc_type}` `{sample.file_size}` bytes "
                    f"`sha256={sample.sha256}`"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    print(render_markdown(run_regression()))


if __name__ == "__main__":
    main()
