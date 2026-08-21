import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from web_listening.blocks.governed_read import (
    GovernedReadGateway,
    MockClientReadGateway,
)
from web_listening.config import settings
from web_listening.models import Document


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.stat(follow_symlinks=False)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != identity:
        return
    try:
        path.unlink()
    except OSError:
        return


def _hardlink_and_handoff(
    source: Path,
    target: Path,
    *,
    storage,
    cleanup_root: Path,
    link_function,
) -> bool:
    source_info = source.stat(follow_symlinks=False)
    identity = (source_info.st_dev, source_info.st_ino)
    try:
        link_function(source, target)
    except FileExistsError:
        return False
    except BaseException:
        try:
            if storage is None:
                _unlink_if_identity(target, identity)
            else:
                storage.remove_execution_created_path_if_identity(
                    target,
                    cleanup_root=cleanup_root,
                    expected_identity=identity,
                )
        except BaseException:
            pass
        raise
    try:
        if storage is not None:
            storage.register_execution_created_path(
                target,
                cleanup_root=cleanup_root,
                expected_identity=identity,
            )
    except BaseException:
        try:
            if storage is None:
                _unlink_if_identity(target, identity)
            else:
                storage.remove_execution_created_path_if_identity(
                    target,
                    cleanup_root=cleanup_root,
                    expected_identity=identity,
                )
        except BaseException:
            pass
        raise
    return True


def publish_execution_file(
    source: str | Path,
    target: str | Path,
    *,
    storage=None,
    cleanup_root: str | Path,
    allow_copy_fallback: bool,
    link_function=None,
) -> bool:
    """Publish exclusively and hand ownership to Storage before returning."""
    source_path = Path(source)
    target_path = Path(target)
    link = link_function or os.link
    try:
        return _hardlink_and_handoff(
            source_path,
            target_path,
            storage=storage,
            cleanup_root=Path(cleanup_root),
            link_function=link,
        )
    except OSError:
        if not allow_copy_fallback:
            raise

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", dir=target_path.parent
    )
    temporary_path = Path(temporary_name)
    temporary_info = os.fstat(descriptor)
    temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
    os.close(descriptor)
    failure_active = False
    try:
        shutil.copy2(source_path, temporary_path)
        return _hardlink_and_handoff(
            temporary_path,
            target_path,
            storage=storage,
            cleanup_root=Path(cleanup_root),
            link_function=link,
        )
    except BaseException:
        failure_active = True
        raise
    finally:
        try:
            _unlink_if_identity(temporary_path, temporary_identity)
        except BaseException:
            if not failure_active:
                raise


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

    def __init__(
        self,
        *,
        read_gateway: GovernedReadGateway | None = None,
        client=None,
        storage=None,
    ):
        if client is not None and read_gateway is not None:
            raise ValueError("provide either read_gateway or offline mock client")
        self.read_gateway = read_gateway or (
            MockClientReadGateway(
                client,
                user_agent=settings.user_agent,
                max_body_bytes=64 * 1024 * 1024,
            )
            if client is not None
            else None
        )
        self.storage = storage

    def _build_blob_path(self, filename: str, sha256: str, content_type: str) -> Path:
        parsed_name = Path(filename)
        suffix = parsed_name.suffix
        if not suffix and content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            suffix = guessed or ""
        blob_dir = settings.downloads_dir / "_blobs" / sha256[:2]
        return blob_dir / f"{sha256}{suffix}"

    def _sanitize_component(self, value: str, *, fallback: str) -> str:
        normalized = str(value or "").strip()
        normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", normalized).strip(" .")
        if not normalized:
            normalized = fallback
        return normalized[:80]

    def _build_tracked_view_path(
        self,
        *,
        canonical_local_path: Path,
        page_url: str,
        file_url: str,
        sha256: str,
        content_type: str = "",
    ) -> Path:
        page_parts = urlparse(page_url or "")
        file_parts = urlparse(file_url or "")
        host = page_parts.netloc or file_parts.netloc or "unknown-host"
        host = self._sanitize_component(host.lower(), fallback="unknown-host")

        page_segments = [
            self._sanitize_component(part, fallback="segment")
            for part in (page_parts.path or "/").split("/")
            if part
        ]
        if not page_segments:
            page_segments = ["_root"]

        file_name = os.path.basename(file_parts.path or "") or canonical_local_path.name
        parsed_name = Path(file_name)
        suffix = parsed_name.suffix or canonical_local_path.suffix
        if not suffix and content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            suffix = guessed or ""
        stem = parsed_name.stem if parsed_name.suffix else parsed_name.name
        stem = self._sanitize_component(stem, fallback="document")
        final_name = f"{stem}--{sha256[:8]}{suffix}"

        return (
            settings.downloads_dir
            / "_tracked"
            / host
            / Path(*page_segments)
            / final_name
        )

    def materialize_tracked_view(
        self,
        *,
        canonical_local_path: str | Path,
        page_url: str,
        file_url: str,
        sha256: str,
        content_type: str = "",
    ) -> Path:
        canonical_path = Path(canonical_local_path)
        tracked_path = self._build_tracked_view_path(
            canonical_local_path=canonical_path,
            page_url=page_url,
            file_url=file_url,
            sha256=sha256,
            content_type=content_type,
        )
        if self.storage is None:
            tracked_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.storage.ensure_execution_artifact_directory(
                tracked_path.parent,
                cleanup_root=settings.downloads_dir,
            )
        if tracked_path.exists():
            return tracked_path
        publish_execution_file(
            canonical_path,
            tracked_path,
            storage=self.storage,
            cleanup_root=settings.downloads_dir,
            allow_copy_fallback=True,
        )
        return tracked_path

    def download(
        self,
        url: str,
        institution: str,
        page_url: str = "",
        request_headers: dict | None = None,
        force_download: bool = False,
    ) -> DownloadResult:
        """Download a document into the shared blob store and return its metadata."""
        if self.storage is not None and not force_download:
            existing = self.storage.get_document_by_download_url(url)
            if (
                existing
                and existing.sha256
                and existing.local_path
                and Path(existing.local_path).exists()
            ):
                return DownloadResult(
                    local_path=Path(existing.local_path),
                    doc_type=existing.doc_type,
                    sha256=existing.sha256,
                    file_size=existing.file_size
                    or Path(existing.local_path).stat().st_size,
                    content_type=existing.content_type,
                    etag=existing.etag,
                    last_modified=existing.last_modified,
                )

        if self.read_gateway is None:
            raise RuntimeError("document target reads require a governed AccessGateway")
        if request_headers:
            raise ValueError(
                "per-request headers cannot replace the gateway's frozen identity"
            )

        governed = self.read_gateway.read(url)
        if not 200 <= governed.status_code < 300:
            raise RuntimeError(
                f"governed document request returned HTTP {governed.status_code}"
            )
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or "document"
        tmp_dir = settings.downloads_dir / "_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix="download_", suffix=".part", dir=tmp_dir
        )
        os.close(handle)
        temp_path = Path(temp_name)
        hasher = hashlib.sha256()
        file_size = 0

        try:
            content_type = governed.content_type
            etag = governed.etag
            last_modified = governed.last_modified
            with temp_path.open("wb") as output:
                output.write(governed.body)
            hasher.update(governed.body)
            file_size = len(governed.body)

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
        request_headers: dict | None = None,
        force_download: bool = False,
    ) -> Document:
        """Download *url* and return a :class:`Document` record.

        ``content_md`` is left empty; fill it with an external ``doc_to_md``
        module when text extraction is required.
        """
        downloaded = self.download(
            url,
            institution,
            page_url,
            request_headers=request_headers,
            force_download=force_download,
        )

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
            content_md_status="pending",
        )

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
