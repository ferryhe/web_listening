import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from web_listening.config import settings
from web_listening.models import Document


class DocumentProcessor:
    """Download documents and record metadata.

    Content conversion (PDF → Markdown, DOCX → Markdown, etc.) is intentionally
    out of scope for this module.  Use a dedicated ``doc_to_md`` module to
    populate ``Document.content_md`` after downloading.
    """

    def __init__(self, client: httpx.Client = None):
        self.client = client or httpx.Client(
            timeout=60,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None

    def download(self, url: str, institution: str, page_url: str = "") -> Path:
        """Download a document into ``<downloads_dir>/<institution>/`` and return its path."""
        resp = self.client.get(url)
        resp.raise_for_status()

        path = urlparse(url).path
        filename = os.path.basename(path) or "document"

        inst_dir = settings.downloads_dir / institution
        inst_dir.mkdir(parents=True, exist_ok=True)
        local_path = inst_dir / filename
        local_path.write_bytes(resp.content)
        return local_path

    def process(
        self,
        url: str,
        site_id: int,
        institution: str,
        page_url: str = "",
        title: str = "",
    ) -> Document:
        """Download *url* and return a :class:`Document` record.

        ``content_md`` is left empty; fill it with an external ``doc_to_md``
        module when text extraction is required.
        """
        local_path = self.download(url, institution, page_url)
        suffix = local_path.suffix.lower().lstrip(".")

        return Document(
            site_id=site_id,
            title=title or local_path.name,
            url=url,
            download_url=url,
            institution=institution,
            page_url=page_url,
            published_at=None,
            downloaded_at=datetime.now(timezone.utc),
            local_path=str(local_path),
            doc_type=suffix,
        )

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
