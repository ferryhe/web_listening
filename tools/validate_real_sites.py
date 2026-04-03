from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import find_document_links
from web_listening.models import Site


@dataclass(slots=True)
class ValidationResult:
    site_name: str
    url: str
    final_url: str
    status_code: int | None
    word_count: int
    link_count: int
    doc_link_count: int
    sample_doc_links: list[str]
    markdown_head: str


DEFAULT_TARGETS = [
    ("SOA Home", "https://www.soa.org/"),
    ("SOA Publications", "https://www.soa.org/publications/publications-landing/"),
    ("CAS Home", "https://www.casact.org/"),
    ("CAS Annual Reports", "https://www.casact.org/about/governance/annual-reports"),
]


def validate_targets(targets: list[tuple[str, str]]) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    with Crawler() as crawler:
        for idx, (site_name, url) in enumerate(targets, start=1):
            snapshot = crawler.snapshot(Site(id=idx, url=url, name=site_name))
            doc_links = find_document_links(snapshot.links)
            results.append(
                ValidationResult(
                    site_name=site_name,
                    url=url,
                    final_url=snapshot.final_url,
                    status_code=snapshot.status_code,
                    word_count=int(snapshot.metadata_json.get("word_count", 0)),
                    link_count=len(snapshot.links),
                    doc_link_count=len(doc_links),
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
        "",
        "| Site | URL | Status | Words | Links | Doc links |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.site_name} | {result.final_url or result.url} | "
            f"{result.status_code if result.status_code is not None else '-'} | "
            f"{result.word_count} | {result.link_count} | {result.doc_link_count} |"
        )

    lines.extend(["", "## Details", ""])
    for result in results:
        lines.append(f"### {result.site_name}")
        lines.append("")
        lines.append(f"- Requested URL: `{result.url}`")
        lines.append(f"- Final URL: `{result.final_url}`")
        lines.append(f"- Status code: `{result.status_code}`")
        lines.append(f"- Word count: `{result.word_count}`")
        lines.append(f"- Link count: `{result.link_count}`")
        lines.append(f"- Document link count: `{result.doc_link_count}`")
        lines.append(f"- Markdown head: `{result.markdown_head}`")
        if result.sample_doc_links:
            lines.append("- Sample document links:")
            for link in result.sample_doc_links:
                lines.append(f"  - `{link}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate web_listening against real public sites.")
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Custom target in the format Name|URL. May be repeated.",
    )
    args = parser.parse_args()

    if args.target:
        targets = []
        for raw in args.target:
            if "|" not in raw:
                raise SystemExit(f"Invalid --target '{raw}'. Expected Name|URL.")
            name, url = raw.split("|", 1)
            targets.append((name.strip(), url.strip()))
    else:
        targets = DEFAULT_TARGETS

    print(render_markdown(validate_targets(targets)))


if __name__ == "__main__":
    main()
