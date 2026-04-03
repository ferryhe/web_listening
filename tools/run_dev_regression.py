from __future__ import annotations

import argparse
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import compute_diff, find_document_links, select_compare_text
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.dev_targets import load_dev_targets
from web_listening.models import Site

TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "dev_test_sites.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True)
class DownloadCheck:
    document_url: str
    local_path: str
    sha256: str
    file_size: int
    doc_type: str
    repeat_local_path: str
    repeat_sha256: str
    stable_sha256: bool
    stable_local_path: bool
    sha256_format_ok: bool
    content_type: str
    etag: str
    last_modified: str


@dataclass(slots=True)
class SiteRegressionResult:
    site_name: str
    monitor_url: str
    document_url: str
    monitor_status_code: int | None
    document_status_code: int | None
    monitor_final_url: str
    document_final_url: str
    first_hash: str
    second_hash: str
    stable_hash: bool
    changed: bool
    hash_basis: str
    diff_preview: str
    monitor_word_count: int
    expected_min_monitor_words: int
    document_page_word_count: int
    expected_min_document_words: int
    document_link_count: int
    expected_min_doc_links: int
    sample_downloads: list[DownloadCheck]


def load_targets() -> list[dict]:
    return load_dev_targets(TARGETS_PATH)


def looks_like_sha256(value: str) -> bool:
    return bool(SHA256_RE.fullmatch((value or "").strip()))


def evaluate_result(result: SiteRegressionResult) -> list[str]:
    issues: list[str] = []
    if result.monitor_status_code != 200:
        issues.append(f"monitor_status={result.monitor_status_code}")
    if result.document_status_code != 200:
        issues.append(f"document_status={result.document_status_code}")
    if not result.stable_hash:
        issues.append("monitor_hash_unstable")
    if result.changed:
        issues.append("repeat_fetch_reported_change")
    if result.monitor_word_count < result.expected_min_monitor_words:
        issues.append(
            f"monitor_word_count={result.monitor_word_count} < expected_min={result.expected_min_monitor_words}"
        )
    if result.document_page_word_count < result.expected_min_document_words:
        issues.append(
            "document_page_word_count="
            f"{result.document_page_word_count} < expected_min={result.expected_min_document_words}"
        )
    if result.document_link_count < result.expected_min_doc_links:
        issues.append(
            f"document_link_count={result.document_link_count} < expected_min={result.expected_min_doc_links}"
        )
    if not result.sample_downloads:
        issues.append("no_sample_downloads")
    for sample in result.sample_downloads:
        if sample.file_size <= 0:
            issues.append(f"download_empty:{sample.document_url}")
        if not sample.sha256_format_ok:
            issues.append(f"download_sha256_invalid:{sample.document_url}")
        if not sample.stable_sha256:
            issues.append(f"download_sha256_unstable:{sample.document_url}")
        if not sample.stable_local_path:
            issues.append(f"download_blob_path_unstable:{sample.document_url}")
    return issues


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
                        repeat_download = processor.download(
                            document_url,
                            institution=target["site_name"],
                            page_url=target["document_url"],
                        )
                        sample_downloads.append(
                            DownloadCheck(
                                document_url=document_url,
                                local_path=persisted.local_path,
                                sha256=persisted.sha256,
                                file_size=persisted.file_size or 0,
                                doc_type=persisted.doc_type,
                                repeat_local_path=str(repeat_download.local_path),
                                repeat_sha256=repeat_download.sha256,
                                stable_sha256=persisted.sha256 == repeat_download.sha256,
                                stable_local_path=persisted.local_path == str(repeat_download.local_path),
                                sha256_format_ok=looks_like_sha256(persisted.sha256),
                                content_type=persisted.content_type,
                                etag=persisted.etag,
                                last_modified=persisted.last_modified,
                            )
                        )

                    results.append(
                        SiteRegressionResult(
                            site_name=target["site_name"],
                            monitor_url=target["monitor_url"],
                            document_url=target["document_url"],
                            monitor_status_code=first_monitor.status_code,
                            document_status_code=document_snapshot.status_code,
                            monitor_final_url=first_monitor.final_url,
                            document_final_url=document_snapshot.final_url,
                            first_hash=first_monitor.content_hash,
                            second_hash=second_monitor.content_hash,
                            stable_hash=first_monitor.content_hash == second_monitor.content_hash,
                            changed=changed,
                            hash_basis=str(first_monitor.metadata_json.get("hash_basis", "")),
                            diff_preview=diff_preview[:400],
                            monitor_word_count=int(first_monitor.metadata_json.get("word_count", 0)),
                            expected_min_monitor_words=int(target.get("expected_min_monitor_words", 0)),
                            document_page_word_count=int(document_snapshot.metadata_json.get("word_count", 0)),
                            expected_min_document_words=int(target.get("expected_min_document_words", 0)),
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
        "- Required live targets: `SOA`, `CAS`, `IAA`",
        "- Regression scope: monitoring, hash stability, diff check, document discovery, sample download, repeat-download SHA validation",
        "",
        "| Site | Passed | Hash stable | Changed | Monitor words | Doc words | Doc links | Downloads |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        issues = evaluate_result(result)
        lines.append(
            f"| {result.site_name} | {'yes' if not issues else 'no'} | {'yes' if result.stable_hash else 'no'} | "
            f"{'yes' if result.changed else 'no'} | {result.monitor_word_count} | "
            f"{result.document_page_word_count} | {result.document_link_count} | {len(result.sample_downloads)} |"
        )

    lines.extend(["", "## Details", ""])
    for result in results:
        issues = evaluate_result(result)
        lines.append(f"### {result.site_name}")
        lines.append("")
        lines.append(f"- Regression passed: `{'yes' if not issues else 'no'}`")
        lines.append(f"- Monitor URL: `{result.monitor_url}`")
        lines.append(f"- Monitor final URL: `{result.monitor_final_url}`")
        lines.append(f"- Monitor status code: `{result.monitor_status_code}`")
        lines.append(f"- Document URL: `{result.document_url}`")
        lines.append(f"- Document final URL: `{result.document_final_url}`")
        lines.append(f"- Document status code: `{result.document_status_code}`")
        lines.append(f"- Hash basis: `{result.hash_basis}`")
        lines.append(f"- First content hash: `{result.first_hash}`")
        lines.append(f"- Second content hash: `{result.second_hash}`")
        lines.append(f"- Stable hash: `{'yes' if result.stable_hash else 'no'}`")
        lines.append(f"- Changed on immediate repeat fetch: `{'yes' if result.changed else 'no'}`")
        lines.append(
            f"- Monitor word count: `{result.monitor_word_count}` "
            f"(expected minimum `{result.expected_min_monitor_words}`)"
        )
        lines.append(
            f"- Document page word count: `{result.document_page_word_count}` "
            f"(expected minimum `{result.expected_min_document_words}`)"
        )
        lines.append(
            f"- Document links found: `{result.document_link_count}` "
            f"(expected minimum `{result.expected_min_doc_links}`)"
        )
        lines.append(f"- Expected minimum document links: `{result.expected_min_doc_links}`")
        if result.diff_preview:
            lines.append(f"- Diff preview: `{result.diff_preview}`")
        if issues:
            lines.append("- Regression issues:")
            for issue in issues:
                lines.append(f"  - `{issue}`")
        if result.sample_downloads:
            lines.append("- Sample downloads:")
            for sample in result.sample_downloads:
                lines.append(
                    f"  - `{sample.document_url}` -> `{sample.doc_type}` `{sample.file_size}` bytes "
                    f"`sha256={sample.sha256}` repeat=`{sample.repeat_sha256}` "
                    f"sha-stable=`{'yes' if sample.stable_sha256 else 'no'}` "
                    f"path-stable=`{'yes' if sample.stable_local_path else 'no'}`"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run live regression checks against the required development test sites."
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0 and only emit the Markdown report.",
    )
    args = parser.parse_args()

    results = run_regression()
    print(render_markdown(results))

    has_failures = any(evaluate_result(result) for result in results)
    if has_failures and not args.report_only:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
