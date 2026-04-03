from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.rescue import RescueAttempt, build_smoke_entry_rescue_candidates, run_rescue_candidates
from web_listening.smoke_sites import load_smoke_sites

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"
EXPECTED_ISSUE_EXPECTATIONS = {"known_blocked", "broken_upstream", "ssl_issue"}


@dataclass(slots=True)
class SmokeResult:
    site_key: str
    abbreviation: str
    full_name: str
    smoke_required: bool
    smoke_expectation: str
    monitor_url: str
    primary_strategy: str
    primary_final_url: str
    primary_fetch_mode: str
    primary_request_user_agent: str
    primary_status_code: int | None
    primary_word_count: int
    primary_link_count: int
    final_url: str
    fetch_mode: str
    request_user_agent: str
    status_code: int | None
    word_count: int
    link_count: int
    source_kind: str
    expected_min_words: int
    resolved: bool
    resolved_strategy: str
    rescue_used: bool
    js_heavy_candidate: bool
    js_markers: list[str]
    passed: bool
    outcome: str
    error: str
    notes: str
    attempts: list[RescueAttempt] = field(default_factory=list)


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


def run_smoke(
    entries: list[dict],
    *,
    allow_browser: bool = True,
    allow_official_feeds: bool = True,
) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    for entry in entries:
        candidates = build_smoke_entry_rescue_candidates(entry)
        if not allow_browser:
            candidates = [candidate for candidate in candidates if candidate.strategy != "browser"]
        if not allow_official_feeds:
            candidates = [candidate for candidate in candidates if candidate.strategy not in {"sitemap", "rss"}]
        rescue_result = run_rescue_candidates(
            label=entry["abbreviation"],
            candidates=candidates,
            expected_min_words=int(entry["expected_min_words"]),
        )
        primary_attempt = rescue_result.attempts[0] if rescue_result.attempts else None
        winning_attempt = rescue_result.winning_attempt
        limited_primary_ok = bool(
            primary_attempt
            and entry["smoke_expectation"] == "pass_http_limited"
            and primary_attempt.status_code is not None
            and primary_attempt.status_code < 400
            and primary_attempt.word_count > 0
        )
        resolved = rescue_result.resolved or limited_primary_ok
        resolved_strategy = rescue_result.resolved_strategy or ("catalog" if limited_primary_ok else "")
        rescue_used = resolved and resolved_strategy not in {"", "catalog"}

        if winning_attempt is None and limited_primary_ok:
            winning_attempt = primary_attempt

        chosen_attempt = winning_attempt or primary_attempt
        expected_issue = entry["smoke_expectation"] in EXPECTED_ISSUE_EXPECTATIONS

        if resolved:
            if resolved_strategy == "catalog":
                _, outcome = evaluate_success(
                    entry["smoke_expectation"],
                    chosen_attempt.word_count if chosen_attempt is not None else 0,
                    int(entry["expected_min_words"]),
                )
            else:
                outcome = f"rescued_{resolved_strategy}"
            passed = True
        else:
            passed = not bool(entry["smoke_required"]) and expected_issue
            outcome = "expected_issue" if passed else "unresolved"

        error = ""
        if not resolved:
            errors = [attempt.error for attempt in rescue_result.attempts if attempt.error]
            if errors:
                error = errors[-1]
            elif rescue_result.attempts:
                error = rescue_result.attempts[-1].reason

        primary_raw_html = ""
        if primary_attempt and primary_attempt.snapshot is not None:
            primary_raw_html = primary_attempt.snapshot.raw_html

        results.append(
            SmokeResult(
                site_key=entry["site_key"],
                abbreviation=entry["abbreviation"],
                full_name=entry["full_name"],
                smoke_required=bool(entry["smoke_required"]),
                smoke_expectation=entry["smoke_expectation"],
                monitor_url=entry["monitor_url"],
                primary_strategy=rescue_result.primary_strategy,
                primary_final_url=primary_attempt.final_url if primary_attempt is not None else entry["monitor_url"],
                primary_fetch_mode=primary_attempt.fetch_mode if primary_attempt is not None else entry["fetch_mode"],
                primary_request_user_agent=primary_attempt.request_user_agent if primary_attempt is not None else "",
                primary_status_code=primary_attempt.status_code if primary_attempt is not None else None,
                primary_word_count=primary_attempt.word_count if primary_attempt is not None else 0,
                primary_link_count=primary_attempt.link_count if primary_attempt is not None else 0,
                final_url=chosen_attempt.final_url if chosen_attempt is not None else entry["monitor_url"],
                fetch_mode=chosen_attempt.fetch_mode if chosen_attempt is not None else entry["fetch_mode"],
                request_user_agent=chosen_attempt.request_user_agent if chosen_attempt is not None else "",
                status_code=chosen_attempt.status_code if chosen_attempt is not None else None,
                word_count=chosen_attempt.word_count if chosen_attempt is not None else 0,
                link_count=chosen_attempt.link_count if chosen_attempt is not None else 0,
                source_kind=chosen_attempt.source_kind if chosen_attempt is not None else "error",
                expected_min_words=int(entry["expected_min_words"]),
                resolved=resolved,
                resolved_strategy=resolved_strategy,
                rescue_used=rescue_used,
                attempts=rescue_result.attempts,
                js_heavy_candidate=bool(entry["js_heavy_candidate"]),
                js_markers=detect_js_markers(primary_raw_html, primary_attempt.word_count if primary_attempt else 0),
                passed=passed,
                outcome=outcome,
                error=error,
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
        f"- Resolved by rescue ladder: `{sum(1 for item in results if item.rescue_used)}`",
        "",
        "| Site | Required | Expectation | Outcome | Status | Words | Min words | Resolved by | JS |",
        "|---|---:|---|---|---:|---:|---:|---|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item.abbreviation} | {'yes' if item.smoke_required else 'no'} | {item.smoke_expectation} | "
            f"{item.outcome} | {item.status_code if item.status_code is not None else '-'} | "
            f"{item.word_count} | {item.expected_min_words} | "
            f"{item.resolved_strategy or ('expected-issue' if item.outcome == 'expected_issue' else '-')} | "
            f"{'yes' if item.js_heavy_candidate else 'no'} |"
        )

    lines.extend(["", "## Details", ""])
    for item in results:
        lines.append(f"### {item.abbreviation}")
        lines.append("")
        lines.append(f"- Full name: `{item.full_name}`")
        lines.append(f"- Required: `{'yes' if item.smoke_required else 'no'}`")
        lines.append(f"- Expectation: `{item.smoke_expectation}`")
        lines.append(f"- Outcome: `{item.outcome}`")
        lines.append(f"- Resolved: `{'yes' if item.resolved else 'no'}`")
        lines.append(f"- Resolved strategy: `{item.resolved_strategy or 'none'}`")
        lines.append(f"- Monitor URL: `{item.monitor_url}`")
        lines.append(f"- Primary strategy: `{item.primary_strategy}`")
        lines.append(f"- Primary final URL: `{item.primary_final_url}`")
        lines.append(f"- Primary fetch mode: `{item.primary_fetch_mode}`")
        if item.primary_request_user_agent:
            lines.append(f"- Primary request user agent: `{item.primary_request_user_agent}`")
        lines.append(f"- Primary status code: `{item.primary_status_code}`")
        lines.append(f"- Primary word count: `{item.primary_word_count}`")
        lines.append(f"- Primary link count: `{item.primary_link_count}`")
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
        lines.append(f"- Source kind: `{item.source_kind}`")
        if item.rescue_used:
            lines.append("- Rescue ladder: `yes`")
        lines.append(f"- JS-heavy candidate: `{'yes' if item.js_heavy_candidate else 'no'}`")
        if item.js_markers:
            lines.append(f"- JS markers: `{', '.join(item.js_markers)}`")
        if item.attempts:
            lines.append(f"- Attempts tried: `{len(item.attempts)}`")
            for attempt in item.attempts:
                lines.append(
                    f"- Attempt `{attempt.strategy}` via `{attempt.fetch_mode}`: "
                    f"status=`{attempt.status_code}` words=`{attempt.word_count}` links=`{attempt.link_count}` "
                    f"kind=`{attempt.source_kind}` passed=`{'yes' if attempt.passed else 'no'}` "
                    f"reason=`{attempt.reason}`"
                )
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
    parser.add_argument(
        "--primary-only",
        action="store_true",
        help="Disable browser and official feed rescue so only the catalog target is checked.",
    )
    args = parser.parse_args()

    entries = load_smoke_sites(CATALOG_PATH)
    if args.site_key:
        requested = {value.strip().lower() for value in args.site_key if value.strip()}
        entries = [item for item in entries if item["site_key"] in requested]
    results = run_smoke(
        entries,
        allow_browser=not args.primary_only,
        allow_official_feeds=not args.primary_only,
    )
    print(render_markdown(results))

    required_failures = [item for item in results if item.smoke_required and not item.passed]
    if required_failures and not args.report_only:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
