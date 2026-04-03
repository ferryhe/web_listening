from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

import httpx

from web_listening.blocks.diff import compute_hash, extract_links, select_compare_text
from web_listening.blocks.normalizer import normalize_html
from web_listening.config import settings
from web_listening.models import Site, SiteSnapshot

_ALLOWED_FETCH_MODES = {"http", "browser", "auto"}


@dataclass(slots=True)
class FetchResult:
    raw_html: str
    cleaned_html: str
    content_text: str
    markdown: str
    fit_markdown: str
    metadata_json: dict
    final_url: str
    status_code: Optional[int]


def normalize_fetch_mode(mode: str | None) -> str:
    resolved = (mode or "http").strip().lower()
    if resolved not in _ALLOWED_FETCH_MODES:
        raise ValueError(f"Unsupported fetch_mode '{mode}'. Allowed: http, browser, auto.")
    if resolved == "auto":
        return "http"
    return resolved


def _snapshot_from_page(site: Site, page: FetchResult, fetch_mode: str) -> SiteSnapshot:
    links = extract_links(page.raw_html, page.final_url or site.url)
    compare_text = select_compare_text(
        fit_markdown=page.fit_markdown,
        markdown=page.markdown,
        content_text=page.content_text,
    )
    return SiteSnapshot(
        site_id=site.id,
        captured_at=datetime.now(timezone.utc),
        content_hash=compute_hash(compare_text),
        raw_html=page.raw_html,
        cleaned_html=page.cleaned_html,
        content_text=page.content_text,
        markdown=page.markdown,
        fit_markdown=page.fit_markdown,
        metadata_json=page.metadata_json,
        fetch_mode=fetch_mode,
        final_url=page.final_url,
        status_code=page.status_code,
        links=links,
    )


class HttpCrawler:
    def __init__(self, client: httpx.Client = None):
        self.client = client or httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None

    def fetch_page(self, url: str, *, fetch_config_json: Optional[dict] = None) -> FetchResult:
        resp = self.client.get(url)
        resp.raise_for_status()
        normalized = normalize_html(resp.text, base_url=str(resp.url))
        metadata = dict(normalized.metadata)
        metadata["driver"] = "http"
        return FetchResult(
            raw_html=normalized.raw_html,
            cleaned_html=normalized.cleaned_html,
            content_text=normalized.content_text,
            markdown=normalized.markdown,
            fit_markdown=normalized.fit_markdown,
            metadata_json=metadata,
            final_url=str(resp.url),
            status_code=resp.status_code,
        )

    def fetch(self, url: str) -> Tuple[str, str]:
        page = self.fetch_page(url)
        return page.raw_html, page.content_text

    def snapshot(self, site: Site) -> SiteSnapshot:
        if site.id is None:
            raise ValueError("site.id must not be None — persist the site with Storage.add_site() before snapshotting")
        page = self.fetch_page(site.url, fetch_config_json=site.fetch_config_json)
        return _snapshot_from_page(site, page, fetch_mode="http")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "HttpCrawler":
        return self

    def __exit__(self, *args) -> None:
        self.close()


class BrowserCrawler:
    def fetch_page(self, url: str, *, fetch_config_json: Optional[dict] = None) -> FetchResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Browser crawling requires Playwright. Install it with `pip install -e \".[browser]\"` "
                "and run `playwright install chromium`."
            ) from exc

        config = fetch_config_json or {}
        timeout_ms = int(config.get("timeout_ms", settings.request_timeout * 1000))
        wait_until = config.get("wait_until", "load")
        wait_for_selector = config.get("wait_for")
        extra_wait_ms = int(config.get("extra_wait_ms", 0))
        headless = bool(config.get("headless", True))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            try:
                page = browser.new_page(user_agent=settings.user_agent)
                response = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                if wait_for_selector:
                    page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
                if extra_wait_ms > 0:
                    page.wait_for_timeout(extra_wait_ms)
                html = page.content()
                final_url = page.url
                status_code = response.status if response else None
            finally:
                browser.close()

        normalized = normalize_html(html, base_url=final_url or url)
        metadata = dict(normalized.metadata)
        metadata["driver"] = "browser"
        metadata["wait_until"] = wait_until
        metadata["wait_for"] = wait_for_selector or ""
        return FetchResult(
            raw_html=normalized.raw_html,
            cleaned_html=normalized.cleaned_html,
            content_text=normalized.content_text,
            markdown=normalized.markdown,
            fit_markdown=normalized.fit_markdown,
            metadata_json=metadata,
            final_url=final_url or url,
            status_code=status_code,
        )

    def fetch(self, url: str) -> Tuple[str, str]:
        page = self.fetch_page(url)
        return page.raw_html, page.content_text

    def snapshot(self, site: Site) -> SiteSnapshot:
        if site.id is None:
            raise ValueError("site.id must not be None — persist the site with Storage.add_site() before snapshotting")
        page = self.fetch_page(site.url, fetch_config_json=site.fetch_config_json)
        return _snapshot_from_page(site, page, fetch_mode="browser")

    def close(self) -> None:
        return None

    def __enter__(self) -> "BrowserCrawler":
        return self

    def __exit__(self, *args) -> None:
        self.close()


class Crawler:
    def __init__(self, client: httpx.Client = None, fetch_mode: str = "http"):
        self.fetch_mode = normalize_fetch_mode(fetch_mode)
        self.http_crawler = HttpCrawler(client=client)
        self.browser_crawler: Optional[BrowserCrawler] = None

    def _get_driver(self, fetch_mode: str):
        mode = normalize_fetch_mode(fetch_mode)
        if mode == "browser":
            if self.browser_crawler is None:
                self.browser_crawler = BrowserCrawler()
            return self.browser_crawler
        return self.http_crawler

    def fetch(self, url: str, *, fetch_mode: Optional[str] = None, fetch_config_json: Optional[dict] = None) -> Tuple[str, str]:
        page = self.fetch_page(url, fetch_mode=fetch_mode, fetch_config_json=fetch_config_json)
        return page.raw_html, page.content_text

    def fetch_page(self, url: str, *, fetch_mode: Optional[str] = None, fetch_config_json: Optional[dict] = None) -> FetchResult:
        driver = self._get_driver(fetch_mode or self.fetch_mode)
        return driver.fetch_page(url, fetch_config_json=fetch_config_json)

    def snapshot(self, site: Site) -> SiteSnapshot:
        driver = self._get_driver(site.fetch_mode)
        return driver.snapshot(site)

    def close(self) -> None:
        self.http_crawler.close()
        if self.browser_crawler is not None:
            self.browser_crawler.close()

    def __enter__(self) -> "Crawler":
        return self

    def __exit__(self, *args) -> None:
        self.close()
