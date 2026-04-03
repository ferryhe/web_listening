from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from web_listening.blocks.rescue import RescueResult, build_smoke_entry_rescue_candidates, run_rescue_candidates
from web_listening.smoke_sites import load_smoke_sites

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"


@dataclass(slots=True)
class CatalogRescueResult:
    site_key: str
    abbreviation: str
    smoke_required: bool
    rescue_result: RescueResult
    notes: str


def run_rescue(entries: list[dict]) -> list[CatalogRescueResult]:
    results: list[CatalogRescueResult] = []
    for entry in entries:
        results.append(
            CatalogRescueResult(
                site_key=entry["site_key"],
                abbreviation=entry["abbreviation"],
                smoke_required=bool(entry["smoke_required"]),
                rescue_result=run_rescue_candidates(
                    label=entry["abbreviation"],
                    candidates=build_smoke_entry_rescue_candidates(entry),
                    expected_min_words=int(entry["expected_min_words"]),
                ),
                notes=entry["notes"],
            )
        )
    return results


def render_markdown(results: list[CatalogRescueResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    resolved_total = sum(1 for item in results if item.rescue_result.resolved)
    unresolved = [item for item in results if not item.rescue_result.resolved]

    lines = [
        "# Agent Rescue Validation",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Catalog path: `{CATALOG_PATH}`",
        f"- Sites checked: `{len(results)}`",
        f"- Sites resolved by catalog-or-agent strategy: `{resolved_total}/{len(results)}`",
        f"- Unresolved sites: `{len(unresolved)}`",
        "",
        "| Site | Required | Resolved | Winning strategy | Attempts |",
        "|---|---:|---:|---|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item.abbreviation} | {'yes' if item.smoke_required else 'no'} | "
            f"{'yes' if item.rescue_result.resolved else 'no'} | "
            f"{item.rescue_result.resolved_strategy or '-'} | {len(item.rescue_result.attempts)} |"
        )

    lines.extend(["", "## Unresolved Sites", ""])
    if not unresolved:
        lines.append("- None")
    else:
        for item in unresolved:
            lines.append(f"### {item.abbreviation}")
            lines.append("")
            if item.notes:
                lines.append(f"- Notes: `{item.notes}`")
            for attempt in item.rescue_result.attempts:
                lines.append(
                    f"- `{attempt.strategy}` `{attempt.fetch_mode}` `{attempt.status_code}` "
                    f"`{attempt.reason}` `{attempt.url}`"
                )
            lines.append("")

    lines.extend(["## Details", ""])
    for item in results:
        lines.append(f"### {item.abbreviation}")
        lines.append("")
        lines.append(f"- Resolved: `{'yes' if item.rescue_result.resolved else 'no'}`")
        lines.append(f"- Winning strategy: `{item.rescue_result.resolved_strategy or 'none'}`")
        for attempt in item.rescue_result.attempts:
            lines.append(
                f"- Attempt `{attempt.strategy}` via `{attempt.fetch_mode}`: "
                f"status=`{attempt.status_code}` words=`{attempt.word_count}` links=`{attempt.link_count}` "
                f"kind=`{attempt.source_kind}` passed=`{'yes' if attempt.passed else 'no'}` "
                f"reason=`{attempt.reason}`"
            )
            lines.append(f"- Attempt URL: `{attempt.url}`")
            lines.append(f"- Final URL: `{attempt.final_url}`")
            if attempt.request_user_agent:
                lines.append(f"- Request user agent: `{attempt.request_user_agent}`")
            if attempt.head:
                lines.append(f"- Head: `{attempt.head}`")
            if attempt.error:
                lines.append(f"- Error: `{attempt.error}`")
        if item.notes:
            lines.append(f"- Notes: `{item.notes}`")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Try agent-style rescue strategies for smoke targets that fail the primary monitor path."
    )
    parser.add_argument("--site-key", action="append", help="Limit the run to one or more site keys.")
    args = parser.parse_args()

    entries = load_smoke_sites(CATALOG_PATH)
    if args.site_key:
        requested = {value.strip().lower() for value in args.site_key if value.strip()}
        entries = [item for item in entries if item["site_key"] in requested]

    print(render_markdown(run_rescue(entries)))


if __name__ == "__main__":
    main()
