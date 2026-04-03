from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from web_listening.blocks.crawler import Crawler
from web_listening.models import Site
from web_listening.smoke_sites import load_smoke_sites

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"


@dataclass(slots=True)
class SmokeResult:
    site_key: str
    abbreviation: str
    full_name: str
    smoke_required: bool
    smoke_expectation: str
    monitor_url: str
    final_url: str
    fetch_mode: str
    request_user_agent: str
    status_code: int | None
    word_count: int
    link_count: int
    expected_min_words: int
    js_heavy_candidate: bool
    js_markers: list[str]
    passed: bool
    outcome: str
    error: str
    notes: str


def detect_js_markers(raw_html: str, word_count: int) -> list[str]:
    html = (raw_html or "").lower()
    markers: list[str] = []
    if "__next" in html or "__next_data__" in html:
        markers.append("nextjs")
    if 'id="root"' in html or "id='root'" in html or "data-reactroot" in html:
        markers.append("react-root")
    if "gatsby" in html:
        markers.append("gatsby")
    script_count = html.count("<script")
    if script_count >= 20:
        markers.append(f"scripts={script_count}")
    if word_count < 120 and script_count >= 10:
        markers.append("low_text_high_script")
    return markers


def evaluate_success(smoke_expectation: str, word_count: int, expected_min_words: int) -> tuple[bool, str]:
    if word_count >= expected_min_words:
        return True, "ok"
    if smoke_expectation == "pass_http_limited" and word_count > 0:
        return True, "limited"
    return False, "too_little_content"


def run_smoke(entries: list[dict]) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    with Crawler() as crawler:
        for index, entry in enumerate(entries, start=1):
            site = Site(
                id=index,
                url=entry["monitor_url"],
                name=entry["abbreviation"],
                fetch_mode=entry["fetch_mode"],
                fetch_config_json=entry["fetch_config_json"],
            )
            try:
                snapshot = crawler.snapshot(site)
                word_count = int(snapshot.metadata_json.get("word_count", 0))
                passed, outcome = evaluate_success(
                    entry["smoke_expectation"],
                    word_count,
                    int(entry["expected_min_words"]),
                )
                if entry["smoke_expectation"] in {"known_blocked", "broken_upstream", "ssl_issue"}:
                    passed = False
                    outcome = "unexpected_success" if snapshot.status_code == 200 else outcome
                results.append(
                    SmokeResult(
                        site_key=entry["site_key"],
                        abbreviation=entry["abbreviation"],
                        full_name=entry["full_name"],
                        smoke_required=bool(entry["smoke_required"]),
                        smoke_expectation=entry["smoke_expectation"],
                        monitor_url=entry["monitor_url"],
                        final_url=snapshot.final_url,
                        fetch_mode=entry["fetch_mode"],
                        request_user_agent=str(snapshot.metadata_json.get("request_user_agent", "")),
                        status_code=snapshot.status_code,
                        word_count=word_count,
                        link_count=len(snapshot.links),
                        expected_min_words=int(entry["expected_min_words"]),
                        js_heavy_candidate=bool(entry["js_heavy_candidate"]),
                        js_markers=detect_js_markers(snapshot.raw_html, word_count),
                        passed=passed,
                        outcome=outcome,
                        error="",
                        notes=entry["notes"],
                    )
                )
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                expected_issue = entry["smoke_expectation"] in {"known_blocked", "broken_upstream"}
                results.append(
                    SmokeResult(
                        site_key=entry["site_key"],
                        abbreviation=entry["abbreviation"],
                        full_name=entry["full_name"],
                        smoke_required=bool(entry["smoke_required"]),
                        smoke_expectation=entry["smoke_expectation"],
                        monitor_url=entry["monitor_url"],
                        final_url=str(exc.response.url) if exc.response is not None else entry["monitor_url"],
                        fetch_mode=entry["fetch_mode"],
                        request_user_agent="",
                        status_code=status_code,
                        word_count=0,
                        link_count=0,
                        expected_min_words=int(entry["expected_min_words"]),
                        js_heavy_candidate=bool(entry["js_heavy_candidate"]),
                        js_markers=[],
                        passed=not bool(entry["smoke_required"]) and expected_issue,
                        outcome="expected_issue" if expected_issue else "http_error",
                        error=f"{type(exc).__name__}: {exc}",
                        notes=entry["notes"],
                    )
                )
            except Exception as exc:  # pragma: no cover - live failure path
                expected_issue = entry["smoke_expectation"] in {"ssl_issue", "broken_upstream", "known_blocked"}
                results.append(
                    SmokeResult(
                        site_key=entry["site_key"],
                        abbreviation=entry["abbreviation"],
                        full_name=entry["full_name"],
                        smoke_required=bool(entry["smoke_required"]),
                        smoke_expectation=entry["smoke_expectation"],
                        monitor_url=entry["monitor_url"],
                        final_url=entry["monitor_url"],
                        fetch_mode=entry["fetch_mode"],
                        request_user_agent="",
                        status_code=None,
                        word_count=0,
                        link_count=0,
                        expected_min_words=int(entry["expected_min_words"]),
                        js_heavy_candidate=bool(entry["js_heavy_candidate"]),
                        js_markers=[],
                        passed=not bool(entry["smoke_required"]) and expected_issue,
                        outcome="expected_issue" if expected_issue else "error",
                        error=f"{type(exc).__name__}: {exc}",
                        notes=entry["notes"],
                    )
                )
    return results


def render_markdown(results: list[SmokeResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    required_total = sum(1 for item in results if item.smoke_required)
    required_passed = sum(1 for item in results if item.smoke_required and item.passed)
    optional_expected_issues = sum(
        1 for item in results if (not item.smoke_required) and item.outcome == "expected_issue"
    )
    lines = [
        "# Smoke Site Catalog Report",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Catalog path: `{CATALOG_PATH}`",
        f"- Sites checked: `{len(results)}`",
        f"- Required smoke targets passed: `{required_passed}/{required_total}`",
        f"- Optional expected issues: `{optional_expected_issues}`",
        "",
        "| Site | Required | Expectation | Outcome | Status | Words | Min words | JS |",
        "|---|---:|---|---|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item.abbreviation} | {'yes' if item.smoke_required else 'no'} | {item.smoke_expectation} | "
            f"{item.outcome} | {item.status_code if item.status_code is not None else '-'} | "
            f"{item.word_count} | {item.expected_min_words} | {'yes' if item.js_heavy_candidate else 'no'} |"
        )

    lines.extend(["", "## Details", ""])
    for item in results:
        lines.append(f"### {item.abbreviation}")
        lines.append("")
        lines.append(f"- Full name: `{item.full_name}`")
        lines.append(f"- Required: `{'yes' if item.smoke_required else 'no'}`")
        lines.append(f"- Expectation: `{item.smoke_expectation}`")
        lines.append(f"- Outcome: `{item.outcome}`")
        lines.append(f"- Monitor URL: `{item.monitor_url}`")
        lines.append(f"- Final URL: `{item.final_url}`")
        lines.append(f"- Fetch mode: `{item.fetch_mode}`")
        if item.request_user_agent:
            lines.append(f"- Request user agent: `{item.request_user_agent}`")
        lines.append(f"- Status code: `{item.status_code}`")
        lines.append(
            f"- Word count: `{item.word_count}` "
            f"(expected minimum `{item.expected_min_words}`)"
        )
        lines.append(f"- Link count: `{item.link_count}`")
        lines.append(f"- JS-heavy candidate: `{'yes' if item.js_heavy_candidate else 'no'}`")
        if item.js_markers:
            lines.append(f"- JS markers: `{', '.join(item.js_markers)}`")
        if item.error:
            lines.append(f"- Error: `{item.error}`")
        if item.notes:
            lines.append(f"- Notes: `{item.notes}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run live smoke checks for the curated site catalog."
    )
    parser.add_argument(
        "--site-key",
        action="append",
        help="Limit the run to one or more site keys from config/smoke_site_catalog.json.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit 0 and only print the Markdown report.",
    )
    args = parser.parse_args()

    entries = load_smoke_sites(CATALOG_PATH)
    if args.site_key:
        requested = {value.strip().lower() for value in args.site_key if value.strip()}
        entries = [item for item in entries if item["site_key"] in requested]
    results = run_smoke(entries)
    print(render_markdown(results))

    required_failures = [item for item in results if item.smoke_required and not item.passed]
    if required_failures and not args.report_only:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
