from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from web_listening.blocks.crawler import Crawler
from web_listening.models import Site, SiteSnapshot

BLOCKED_MARKERS = (
    "access denied",
    "performing security verification",
    "request unsuccessful",
    "just a moment",
    "verification successful. waiting for",
)


@dataclass(slots=True)
class RescueCandidate:
    strategy: str
    url: str
    fetch_mode: str = "http"
    fetch_config_json: dict = field(default_factory=dict)


@dataclass(slots=True)
class RescueAttempt:
    strategy: str
    url: str
    fetch_mode: str
    status_code: int | None
    final_url: str
    request_user_agent: str
    word_count: int
    link_count: int
    source_kind: str
    passed: bool
    reason: str
    head: str
    error: str = ""
    snapshot: SiteSnapshot | None = None


@dataclass(slots=True)
class RescueResult:
    label: str
    primary_strategy: str
    resolved_strategy: str
    resolved: bool
    attempts: list[RescueAttempt]

    @property
    def winning_attempt(self) -> RescueAttempt | None:
        for attempt in self.attempts:
            if attempt.passed:
                return attempt
        return None


def build_default_site_rescue_candidates(
    site: Site,
    *,
    allow_browser: bool = True,
    allow_official_feeds: bool = True,
    sitemap_url: Optional[str] = None,
    rss_url: Optional[str] = None,
    browser_fetch_config: Optional[dict] = None,
) -> list[RescueCandidate]:
    candidates = [
        RescueCandidate(
            strategy="catalog",
            url=site.url,
            fetch_mode=site.fetch_mode,
            fetch_config_json=site.fetch_config_json,
        )
    ]

    if allow_browser and site.fetch_mode != "browser":
        candidates.append(
            RescueCandidate(
                strategy="browser",
                url=site.url,
                fetch_mode="browser",
                fetch_config_json=browser_fetch_config
                or {"wait_until": "domcontentloaded", "extra_wait_ms": 1000},
            )
        )

    if allow_official_feeds:
        root_url = _site_root_url(site.url)
        candidates.append(
            RescueCandidate(
                strategy="sitemap",
                url=sitemap_url or urljoin(root_url, "/sitemap.xml"),
            )
        )
        candidates.append(
            RescueCandidate(
                strategy="rss",
                url=rss_url or urljoin(root_url, "/rss.xml"),
            )
        )

    return candidates


def build_smoke_entry_rescue_candidates(entry: dict) -> list[RescueCandidate]:
    site = Site(
        url=entry["monitor_url"],
        name=entry["abbreviation"],
        fetch_mode=entry["fetch_mode"],
        fetch_config_json=entry["fetch_config_json"],
    )
    return build_default_site_rescue_candidates(
        site,
        sitemap_url=urljoin(entry["homepage_url"], "/sitemap.xml"),
        rss_url=urljoin(entry["homepage_url"], "/rss.xml"),
    )


def evaluate_snapshot(
    snapshot: SiteSnapshot,
    *,
    expected_min_words: int = 50,
    min_inventory_links: int = 5,
    blocked_markers: tuple[str, ...] = BLOCKED_MARKERS,
) -> tuple[bool, str]:
    word_count = int(snapshot.metadata_json.get("word_count", 0))
    link_count = len(snapshot.links)
    source_kind = str(snapshot.metadata_json.get("source_kind", "html"))
    head = snapshot.fit_markdown[:400].lower()

    if any(marker in head for marker in blocked_markers):
        return False, "blocked_interstitial"

    if snapshot.status_code and snapshot.status_code >= 400:
        return False, f"http_{snapshot.status_code}"

    if source_kind == "xml_sitemap":
        if link_count >= min_inventory_links:
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


def run_rescue_candidates(
    *,
    label: str,
    candidates: list[RescueCandidate],
    expected_min_words: int = 50,
    min_inventory_links: int = 5,
) -> RescueResult:
    attempts: list[RescueAttempt] = []
    resolved = False
    resolved_strategy = ""

    with Crawler() as crawler:
        for index, candidate in enumerate(candidates, start=1):
            try:
                snapshot = crawler.snapshot(
                    Site(
                        id=index,
                        url=candidate.url,
                        name=label,
                        fetch_mode=candidate.fetch_mode,
                        fetch_config_json=candidate.fetch_config_json,
                    )
                )
                passed, reason = evaluate_snapshot(
                    snapshot,
                    expected_min_words=expected_min_words,
                    min_inventory_links=min_inventory_links,
                )
                attempts.append(
                    RescueAttempt(
                        strategy=candidate.strategy,
                        url=candidate.url,
                        fetch_mode=candidate.fetch_mode,
                        status_code=snapshot.status_code,
                        final_url=snapshot.final_url,
                        request_user_agent=str(snapshot.metadata_json.get("request_user_agent", "")),
                        word_count=int(snapshot.metadata_json.get("word_count", 0)),
                        link_count=len(snapshot.links),
                        source_kind=str(snapshot.metadata_json.get("source_kind", "html")),
                        passed=passed,
                        reason=reason,
                        head=snapshot.fit_markdown[:220].replace("\n", " | "),
                        snapshot=snapshot,
                    )
                )
                if passed:
                    resolved = True
                    resolved_strategy = candidate.strategy
                    break
            except httpx.HTTPStatusError as exc:
                attempts.append(
                    RescueAttempt(
                        strategy=candidate.strategy,
                        url=candidate.url,
                        fetch_mode=candidate.fetch_mode,
                        status_code=exc.response.status_code if exc.response is not None else None,
                        final_url=str(exc.response.url) if exc.response is not None else candidate.url,
                        request_user_agent="",
                        word_count=0,
                        link_count=0,
                        source_kind="error",
                        passed=False,
                        reason=f"http_{exc.response.status_code}" if exc.response is not None else "http_error",
                        head="",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            except Exception as exc:  # pragma: no cover - live failure path
                attempts.append(
                    RescueAttempt(
                        strategy=candidate.strategy,
                        url=candidate.url,
                        fetch_mode=candidate.fetch_mode,
                        status_code=None,
                        final_url=candidate.url,
                        request_user_agent="",
                        word_count=0,
                        link_count=0,
                        source_kind="error",
                        passed=False,
                        reason=type(exc).__name__,
                        head="",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

    return RescueResult(
        label=label,
        primary_strategy=candidates[0].strategy if candidates else "catalog",
        resolved_strategy=resolved_strategy,
        resolved=resolved,
        attempts=attempts,
    )


def run_site_rescue(
    site: Site,
    *,
    expected_min_words: int = 50,
    min_inventory_links: int = 5,
    allow_browser: bool = True,
    allow_official_feeds: bool = True,
    sitemap_url: Optional[str] = None,
    rss_url: Optional[str] = None,
    browser_fetch_config: Optional[dict] = None,
) -> RescueResult:
    return run_rescue_candidates(
        label=site.name or site.url,
        candidates=build_default_site_rescue_candidates(
            site,
            allow_browser=allow_browser,
            allow_official_feeds=allow_official_feeds,
            sitemap_url=sitemap_url,
            rss_url=rss_url,
            browser_fetch_config=browser_fetch_config,
        ),
        expected_min_words=expected_min_words,
        min_inventory_links=min_inventory_links,
    )


def _site_root_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"
