import hashlib
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from web_listening.config import settings
from web_listening.models import Document


@dataclass(slots=True)
class DownloadResult:
    local_path: Path
    doc_type: str
    sha256: str
    file_size: int
    content_type: str
    etag: str
    last_modified: str


class DocumentProcessor:
    """Download documents and record metadata.

    Content conversion (PDF → Markdown, DOCX → Markdown, etc.) is intentionally
    out of scope for this module.  Use a dedicated ``doc_to_md`` module to
    populate ``Document.content_md`` after downloading.
    """

    def __init__(self, client: httpx.Client = None, storage=None):
        self.client = client or httpx.Client(
            timeout=60,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None
        self.storage = storage

    def _build_blob_path(self, filename: str, sha256: str, content_type: str) -> Path:
        parsed_name = Path(filename)
        suffix = parsed_name.suffix
        if not suffix and content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            suffix = guessed or ""
        blob_dir = settings.downloads_dir / "_blobs" / sha256[:2]
        blob_dir.mkdir(parents=True, exist_ok=True)
        return blob_dir / f"{sha256}{suffix}"

    def download(self, url: str, institution: str, page_url: str = "") -> DownloadResult:
        """Download a document into the shared blob store and return its metadata."""
        if self.storage is not None:
            existing = self.storage.get_document_by_download_url(url)
            if existing and existing.sha256 and existing.local_path and Path(existing.local_path).exists():
                return DownloadResult(
                    local_path=Path(existing.local_path),
                    doc_type=existing.doc_type,
                    sha256=existing.sha256,
                    file_size=existing.file_size or Path(existing.local_path).stat().st_size,
                    content_type=existing.content_type,
                    etag=existing.etag,
                    last_modified=existing.last_modified,
                )

        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or "document"
        tmp_dir = settings.downloads_dir / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix="download_", suffix=".part", dir=tmp_dir)
        os.close(handle)
        temp_path = Path(temp_name)
        hasher = hashlib.sha256()
        file_size = 0

        try:
            with self.client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                etag = resp.headers.get("etag", "")
                last_modified = resp.headers.get("last-modified", "")
                with temp_path.open("wb") as output:
                    for chunk in resp.iter_bytes():
                        if not chunk:
                            continue
                        output.write(chunk)
                        hasher.update(chunk)
                        file_size += len(chunk)

            sha256 = hasher.hexdigest()
            blob = self.storage.get_blob(sha256) if self.storage is not None else None
            if blob:
                local_path = Path(blob["canonical_path"])
                if local_path.exists():
                    if temp_path.exists():
                        temp_path.unlink()
                else:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    temp_path.replace(local_path)
                if self.storage is not None:
                    self.storage.upsert_blob(
                        sha256=sha256,
                        canonical_path=str(local_path),
                        file_size=file_size,
                        content_type=content_type,
                    )
            else:
                local_path = self._build_blob_path(filename, sha256, content_type)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                if local_path.exists():
                    local_path.unlink()
                temp_path.replace(local_path)
                if self.storage is not None:
                    self.storage.upsert_blob(
                        sha256=sha256,
                        canonical_path=str(local_path),
                        file_size=file_size,
                        content_type=content_type,
                    )

            return DownloadResult(
                local_path=local_path,
                doc_type=local_path.suffix.lower().lstrip("."),
                sha256=sha256,
                file_size=file_size,
                content_type=content_type,
                etag=etag,
                last_modified=last_modified,
            )
        except Exception:
            if temp_path.exists():
                temp_path.unlink()
            raise

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
        downloaded = self.download(url, institution, page_url)

        return Document(
            site_id=site_id,
            title=title or downloaded.local_path.name,
            url=url,
            download_url=url,
            institution=institution,
            page_url=page_url,
            published_at=None,
            downloaded_at=datetime.now(timezone.utc),
            local_path=str(downloaded.local_path),
            doc_type=downloaded.doc_type,
            sha256=downloaded.sha256,
            file_size=downloaded.file_size,
            content_type=downloaded.content_type,
            etag=downloaded.etag,
            last_modified=downloaded.last_modified,
        )

    def close(self):
        if self._owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
