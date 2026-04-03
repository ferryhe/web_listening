from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import find_document_links
from web_listening.dev_targets import load_dev_targets
from web_listening.models import Site


@dataclass(slots=True)
class ValidationResult:
    site_name: str
    kind: str
    url: str
    final_url: str
    status_code: int | None
    word_count: int
    expected_min_words: int
    link_count: int
    doc_link_count: int
    expected_min_doc_links: int
    hash_basis: str
    sample_doc_links: list[str]
    markdown_head: str


TARGETS_PATH = Path(__file__).resolve().parents[1] / "config" / "dev_test_sites.json"


def load_targets() -> list[dict]:
    payload = load_dev_targets(TARGETS_PATH)
    targets: list[dict] = []
    for item in payload:
        targets.append(
            {
                "site_name": f"{item['site_name']} Monitor",
                "kind": "monitor",
                "url": item["monitor_url"],
                "expected_min_words": int(item["expected_min_monitor_words"]),
                "expected_min_doc_links": 0,
            }
        )
        targets.append(
            {
                "site_name": f"{item['site_name']} Documents",
                "kind": "documents",
                "url": item["document_url"],
                "expected_min_words": int(item["expected_min_document_words"]),
                "expected_min_doc_links": int(item["expected_min_doc_links"]),
            }
        )
    return targets


def validate_targets(targets: list[dict]) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    with Crawler() as crawler:
        for idx, target in enumerate(targets, start=1):
            snapshot = crawler.snapshot(Site(id=idx, url=target["url"], name=target["site_name"]))
            doc_links = find_document_links(snapshot.links)
            results.append(
                ValidationResult(
                    site_name=target["site_name"],
                    kind=target["kind"],
                    url=target["url"],
                    final_url=snapshot.final_url,
                    status_code=snapshot.status_code,
                    word_count=int(snapshot.metadata_json.get("word_count", 0)),
                    expected_min_words=int(target["expected_min_words"]),
                    link_count=len(snapshot.links),
                    doc_link_count=len(doc_links),
                    expected_min_doc_links=int(target["expected_min_doc_links"]),
                    hash_basis=str(snapshot.metadata_json.get("hash_basis", "")),
                    sample_doc_links=doc_links[:5],
                    markdown_head=snapshot.fit_markdown[:400].replace("\n", " | "),
                )
            )
    return results


def render_markdown(results: list[ValidationResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Real Site Validation",
        "",
        f"- Generated at: `{generated_at}`",
        "- Validation mode: `http`",
        "- Source: live public websites",
        "- Required live targets: `SOA`, `CAS`, `IAA`",
        "",
        "| Site | Kind | Status | Words | Expected min | Links | Doc links | Expected min |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.site_name} | {result.kind} | {result.status_code if result.status_code is not None else '-'} | "
            f"{result.word_count} | {result.expected_min_words} | {result.link_count} | "
            f"{result.doc_link_count} | {result.expected_min_doc_links} |"
        )

    lines.extend(["", "## Details", ""])
    for result in results:
        lines.append(f"### {result.site_name}")
        lines.append("")
        lines.append(f"- Requested URL: `{result.url}`")
        lines.append(f"- Final URL: `{result.final_url}`")
        lines.append(f"- Status code: `{result.status_code}`")
        lines.append(f"- Hash basis: `{result.hash_basis}`")
        lines.append(
            f"- Word count: `{result.word_count}` "
            f"(expected minimum `{result.expected_min_words}`)"
        )
        lines.append(f"- Link count: `{result.link_count}`")
        lines.append(
            f"- Document link count: `{result.doc_link_count}` "
            f"(expected minimum `{result.expected_min_doc_links}`)"
        )
        lines.append(f"- Markdown head: `{result.markdown_head}`")
        if result.sample_doc_links:
            lines.append("- Sample document links:")
            for link in result.sample_doc_links:
                lines.append(f"  - `{link}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    argparse.ArgumentParser(
        description="Validate web_listening against the required development test sites."
    ).parse_args()
    print(render_markdown(validate_targets(load_targets())))


if __name__ == "__main__":
    main()
