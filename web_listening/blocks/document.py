import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from web_listening.config import settings
from web_listening.models import Document


class DocumentProcessor:
    def __init__(self, client: httpx.Client = None):
        self.client = client or httpx.Client(
            timeout=60,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None

    def download(self, url: str, institution: str, page_url: str = "") -> Path:
        """Download a document to the institution's directory."""
        resp = self.client.get(url)
        resp.raise_for_status()

        path = urlparse(url).path
        filename = os.path.basename(path) or "document"

        inst_dir = settings.downloads_dir / institution
        inst_dir.mkdir(parents=True, exist_ok=True)
        local_path = inst_dir / filename
        local_path.write_bytes(resp.content)
        return local_path

    def to_markdown(self, local_path: Path) -> str:
        """Convert document to markdown text."""
        suffix = local_path.suffix.lower()
        if suffix == ".pdf":
            return self._pdf_to_md(local_path)
        elif suffix in (".html", ".htm"):
            return self._html_to_md(local_path.read_text(errors="ignore"))
        else:
            try:
                return local_path.read_text(errors="ignore")
            except Exception:
                return ""

    def _pdf_to_md(self, path: Path) -> str:
        try:
            import fitz  # pymupdf

            doc = fitz.open(str(path))
            pages = []
            for page in doc:
                pages.append(page.get_text())
            doc.close()
            return "\n\n".join(pages)
        except Exception as e:
            return f"[PDF conversion error: {e}]"

    def _html_to_md(self, html: str) -> str:
        try:
            from markdownify import markdownify

            return markdownify(html)
        except Exception as e:
            return f"[HTML conversion error: {e}]"

    def process(
        self,
        url: str,
        site_id: int,
        institution: str,
        page_url: str = "",
        title: str = "",
    ) -> Document:
        local_path = self.download(url, institution, page_url)
        content_md = self.to_markdown(local_path)
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
            content_md=content_md,
        )

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
