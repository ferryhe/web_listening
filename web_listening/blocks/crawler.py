from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple

import httpx

from web_listening.blocks.diff import compute_hash, extract_links, select_compare_text
from web_listening.blocks.normalizer import normalize_html
from web_listening.config import settings
from web_listening.models import Site, SiteSnapshot


@dataclass(slots=True)
class FetchResult:
    raw_html: str
    cleaned_html: str
    content_text: str
    markdown: str
    fit_markdown: str
    metadata_json: dict
    final_url: str
    status_code: int


class Crawler:
    def __init__(self, client: httpx.Client = None):
        self.client = client or httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None

    def fetch(self, url: str) -> Tuple[str, str]:
        """Returns (raw_html, text_content)."""
        page = self.fetch_page(url)
        return page.raw_html, page.content_text

    def fetch_page(self, url: str) -> FetchResult:
        """Fetch *url* and return normalized page artifacts."""
        resp = self.client.get(url)
        resp.raise_for_status()
        normalized = normalize_html(resp.text, base_url=str(resp.url))
        return FetchResult(
            raw_html=normalized.raw_html,
            cleaned_html=normalized.cleaned_html,
            content_text=normalized.content_text,
            markdown=normalized.markdown,
            fit_markdown=normalized.fit_markdown,
            metadata_json=normalized.metadata,
            final_url=str(resp.url),
            status_code=resp.status_code,
        )

    def snapshot(self, site: Site) -> SiteSnapshot:
        if site.id is None:
            raise ValueError("site.id must not be None — persist the site with Storage.add_site() before snapshotting")
        page = self.fetch_page(site.url)
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
            fetch_mode="http",
            final_url=page.final_url,
            status_code=page.status_code,
            links=links,
        )

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
