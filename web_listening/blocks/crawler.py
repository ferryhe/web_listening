from bs4 import BeautifulSoup
from datetime import datetime, timezone
from typing import Tuple

import httpx

from web_listening.blocks.diff import compute_hash, extract_links
from web_listening.config import settings
from web_listening.models import Site, SiteSnapshot


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
        resp = self.client.get(url)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return html, text

    def snapshot(self, site: Site) -> SiteSnapshot:
        if site.id is None:
            raise ValueError("site.id must not be None — persist the site with Storage.add_site() before snapshotting")
        html, text = self.fetch(site.url)
        links = extract_links(html, site.url)
        return SiteSnapshot(
            site_id=site.id,
            captured_at=datetime.now(timezone.utc),
            content_hash=compute_hash(text),
            content_text=text,
            links=links,
        )

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
