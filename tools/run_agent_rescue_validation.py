from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx

from web_listening.blocks.crawler import Crawler
from web_listening.models import Site
from web_listening.smoke_sites import load_smoke_sites

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke_site_catalog.json"
BLOCKED_MARKERS = (
    "access denied",
    "performing security verification",
    "request unsuccessful",
    "just a moment",
    "verification successful. waiting for",
)


@dataclass(slots=True)
class RescueAttempt:
    strategy: str
    url: str
    fetch_mode: str
    status_code: int | None
    final_url: str
    word_count: int
    link_count: int
    source_kind: str
    passed: bool
    reason: str
    head: str


@dataclass(slots=True)
class RescueResult:
    site_key: str
    abbreviation: str
    smoke_required: bool
    primary_strategy: str
    resolved_strategy: str
    resolved: bool
    attempts: list[RescueAttempt]
    notes: str


def build_candidates(entry: dict) -> list[tuple[str, str, str, dict]]:
    candidates: list[tuple[str, str, str, dict]] = [
        ("catalog", entry["monitor_url"], entry["fetch_mode"], entry["fetch_config_json"]),
    ]

    browser_config = {"wait_until": "domcontentloaded", "extra_wait_ms": 1000}
    if entry["fetch_mode"] != "browser":
        candidates.append(("browser", entry["monitor_url"], "browser", browser_config))

    origin_url = entry["homepage_url"]
    candidates.append(("sitemap", urljoin(origin_url, "/sitemap.xml"), "http", {}))
    candidates.append(("rss", urljoin(origin_url, "/rss.xml"), "http", {}))
    return candidates


def evaluate_attempt(snapshot, expected_min_words: int) -> tuple[bool, str]:
    word_count = int(snapshot.metadata_json.get("word_count", 0))
    link_count = len(snapshot.links)
    source_kind = str(snapshot.metadata_json.get("source_kind", "html"))
    head = snapshot.fit_markdown[:400].lower()

    if any(marker in head for marker in BLOCKED_MARKERS):
        return False, "blocked_interstitial"

    if snapshot.status_code and snapshot.status_code >= 400:
        return False, f"http_{snapshot.status_code}"

    if source_kind == "xml_sitemap":
        if link_count >= 5:
            return True, "sitemap_inventory"
        return False, "sitemap_too_small"

    if source_kind == "xml_feed":
        item_count = int(snapshot.metadata_json.get("item_count", 0))
        if item_count >= 3 or link_count >= 3 or word_count >= max(10, expected_min_words // 5):
            return True, "feed_inventory"
        return False, "feed_too_small"

    if word_count >= expected_min_words:
        return True, "content_ok"
    return False, "too_little_content"


def run_rescue(entries: list[dict]) -> list[RescueResult]:
    results: list[RescueResult] = []
    for index, entry in enumerate(entries, start=1):
        attempts: list[RescueAttempt] = []
        resolved_strategy = ""
        resolved = False

        for strategy, url, fetch_mode, fetch_config_json in build_candidates(entry):
            try:
                with Crawler(fetch_mode=fetch_mode) as crawler:
                    snapshot = crawler.snapshot(
                        Site(
                            id=index,
                            url=url,
                            name=entry["abbreviation"],
                            fetch_mode=fetch_mode,
                            fetch_config_json=fetch_config_json,
                        )
                    )
                passed, reason = evaluate_attempt(snapshot, int(entry["expected_min_words"]))
                attempts.append(
                    RescueAttempt(
                        strategy=strategy,
                        url=url,
                        fetch_mode=fetch_mode,
                        status_code=snapshot.status_code,
                        final_url=snapshot.final_url,
                        word_count=int(snapshot.metadata_json.get("word_count", 0)),
                        link_count=len(snapshot.links),
                        source_kind=str(snapshot.metadata_json.get("source_kind", "html")),
                        passed=passed,
                        reason=reason,
                        head=snapshot.fit_markdown[:220].replace("\n", " | "),
                    )
                )
                if passed:
                    resolved_strategy = strategy
                    resolved = True
                    break
            except httpx.HTTPStatusError as exc:
                attempts.append(
                    RescueAttempt(
                        strategy=strategy,
                        url=url,
                        fetch_mode=fetch_mode,
                        status_code=exc.response.status_code if exc.response is not None else None,
                        final_url=str(exc.response.url) if exc.response is not None else url,
                        word_count=0,
                        link_count=0,
                        source_kind="error",
                        passed=False,
                        reason=f"http_{exc.response.status_code}" if exc.response is not None else "http_error",
                        head="",
                    )
                )
            except Exception as exc:  # pragma: no cover - live failure path
                attempts.append(
                    RescueAttempt(
                        strategy=strategy,
                        url=url,
                        fetch_mode=fetch_mode,
                        status_code=None,
                        final_url=url,
                        word_count=0,
                        link_count=0,
                        source_kind="error",
                        passed=False,
                        reason=f"{type(exc).__name__}",
                        head="",
                    )
                )

        results.append(
            RescueResult(
                site_key=entry["site_key"],
                abbreviation=entry["abbreviation"],
                smoke_required=bool(entry["smoke_required"]),
                primary_strategy=entry["fetch_mode"],
                resolved_strategy=resolved_strategy,
                resolved=resolved,
                attempts=attempts,
                notes=entry["notes"],
            )
        )
    return results


def render_markdown(results: list[RescueResult]) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    resolved_total = sum(1 for item in results if item.resolved)
    unresolved = [item for item in results if not item.resolved]

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
            f"{'yes' if item.resolved else 'no'} | {item.resolved_strategy or '-'} | {len(item.attempts)} |"
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
            for attempt in item.attempts:
                lines.append(
                    f"- `{attempt.strategy}` `{attempt.fetch_mode}` `{attempt.status_code}` "
                    f"`{attempt.reason}` `{attempt.url}`"
                )
            lines.append("")

    lines.extend(["## Details", ""])
    for item in results:
        lines.append(f"### {item.abbreviation}")
        lines.append("")
        lines.append(f"- Resolved: `{'yes' if item.resolved else 'no'}`")
        lines.append(f"- Winning strategy: `{item.resolved_strategy or 'none'}`")
        for attempt in item.attempts:
            lines.append(
                f"- Attempt `{attempt.strategy}` via `{attempt.fetch_mode}`: "
                f"status=`{attempt.status_code}` words=`{attempt.word_count}` links=`{attempt.link_count}` "
                f"kind=`{attempt.source_kind}` passed=`{'yes' if attempt.passed else 'no'}` "
                f"reason=`{attempt.reason}`"
            )
            lines.append(f"- Attempt URL: `{attempt.url}`")
            lines.append(f"- Final URL: `{attempt.final_url}`")
            if attempt.head:
                lines.append(f"- Head: `{attempt.head}`")
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
