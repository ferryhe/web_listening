import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.models import Document


def make_client(content: bytes, content_type: str = "application/pdf") -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": content_type},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_returns_blob_metadata(tmp_path):
    pdf_bytes = b"fake-pdf-bytes"
    client = make_client(pdf_bytes)
    proc = DocumentProcessor(client=client)

    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.user_agent = "test-agent"
        mock_settings.downloads_dir = tmp_path

        result = proc.download("https://example.com/report.pdf", institution="TestOrg")

    assert result.local_path.exists()
    assert result.local_path.read_bytes() == pdf_bytes
    assert result.doc_type == "pdf"
    assert result.sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert result.file_size == len(pdf_bytes)


def test_process_returns_pending_document_without_content_md(tmp_path):
    client = make_client(b"%PDF fake")
    proc = DocumentProcessor(client=client)

    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.user_agent = "test-agent"
        mock_settings.downloads_dir = tmp_path

        doc = proc.process(
            url="https://example.com/doc.pdf",
            site_id=1,
            institution="TestOrg",
        )

    assert isinstance(doc, Document)
    assert doc.doc_type == "pdf"
    assert doc.institution == "TestOrg"
    assert doc.site_id == 1
    assert doc.content_md == ""
    assert doc.content_md_status == "pending"
    assert doc.content_md_updated_at is None
    assert doc.local_path != ""
    assert isinstance(doc.downloaded_at, datetime)


def test_processor_has_no_conversion_methods():
    assert not hasattr(DocumentProcessor, "to_markdown")
    assert not hasattr(DocumentProcessor, "_pdf_to_md")
    assert not hasattr(DocumentProcessor, "_html_to_md")


def test_download_reuses_same_sha256_and_blob_path(tmp_path):
    pdf_bytes = b"repeatable-pdf-bytes"
    client = make_client(pdf_bytes)
    storage = Storage(tmp_path / "test.db")
    proc = DocumentProcessor(client=client, storage=storage)

    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.user_agent = "test-agent"
        mock_settings.downloads_dir = tmp_path

        first_doc = proc.process(
            url="https://example.com/report.pdf",
            site_id=1,
            institution="TestOrg",
            page_url="https://example.com/reports",
        )
        saved = storage.add_document(first_doc)
        repeated = proc.download(
            "https://example.com/report.pdf",
            institution="TestOrg",
            page_url="https://example.com/reports",
        )

    assert saved.sha256 == repeated.sha256
    assert saved.local_path == str(repeated.local_path)
    assert repeated.local_path.exists()
    storage.close()


def test_materialize_tracked_view_creates_source_organized_path(tmp_path):
    pdf_bytes = b"source-view-pdf"
    client = make_client(pdf_bytes)
    proc = DocumentProcessor(client=client)

    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.user_agent = "test-agent"
        mock_settings.downloads_dir = tmp_path

        result = proc.download("https://example.com/report.pdf", institution="TestOrg")
        tracked = proc.materialize_tracked_view(
            canonical_local_path=result.local_path,
            page_url="https://example.com/research/topics/page-a",
            file_url="https://example.com/report.pdf",
            sha256=result.sha256,
            content_type=result.content_type,
        )

    assert tracked.exists()
    assert tracked.read_bytes() == pdf_bytes
    assert "_tracked" in str(tracked)
    assert "example.com" in str(tracked)
    assert "research" in str(tracked)
    assert "topics" in str(tracked)


def test_tracked_view_hardlink_effect_then_interrupt_removes_only_owned_target(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    storage = Storage(tmp_path / "hardlink-interrupt.db")
    storage.begin_execution_transaction()
    processor = DocumentProcessor(storage=storage)
    failure = KeyboardInterrupt("hardlink effected")
    real_link = os.link

    def link_then_interrupt(source, target, *args, **kwargs):
        real_link(source, target, *args, **kwargs)
        raise failure

    monkeypatch.setattr("web_listening.blocks.document.os.link", link_then_interrupt)
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            processor.materialize_tracked_view(
                canonical_local_path=canonical,
                page_url="https://example.com/research",
                file_url="https://example.com/report.pdf",
                sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                content_type="application/pdf",
            )

    assert caught.value is failure
    assert not tracked.exists()
    assert canonical.read_bytes() == b"canonical"
    storage.rollback_execution_transaction()
    storage.close()


@pytest.mark.parametrize("preexisting_directories", [False, True])
def test_tracked_view_rollback_respects_directory_creation_provenance(
    tmp_path: Path, preexisting_directories: bool
) -> None:
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    storage = Storage(tmp_path / "tracked-directory-provenance.db")
    processor = DocumentProcessor(storage=storage)
    digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = downloads
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research/topics",
            file_url="https://example.com/report.pdf",
            sha256=digest,
            content_type="application/pdf",
        )
        if preexisting_directories:
            tracked.parent.mkdir(parents=True)
        storage.begin_execution_transaction()
        processor.materialize_tracked_view(
            canonical_local_path=canonical,
            page_url="https://example.com/research/topics",
            file_url="https://example.com/report.pdf",
            sha256=digest,
            content_type="application/pdf",
        )
        storage.rollback_execution_transaction()

    assert not tracked.exists()
    assert tracked.parent.exists() is preexisting_directories
    assert (downloads / "_tracked").exists() is preexisting_directories
    assert canonical.read_bytes() == b"canonical"
    storage.close()


def test_tracked_view_copy_effect_then_interrupt_cleans_partial_publication(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    storage = Storage(tmp_path / "copy-interrupt.db")
    storage.begin_execution_transaction()
    processor = DocumentProcessor(storage=storage)
    failure = SystemExit(23)
    real_copy = shutil.copy2
    monkeypatch.setattr(
        "web_listening.blocks.document.os.link",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cross-device")),
    )

    def copy_then_interrupt(source, target, *args, **kwargs):
        real_copy(source, target, *args, **kwargs)
        raise failure

    monkeypatch.setattr(
        "web_listening.blocks.document.shutil.copy2", copy_then_interrupt
    )
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        with pytest.raises(SystemExit) as caught:
            processor.materialize_tracked_view(
                canonical_local_path=canonical,
                page_url="https://example.com/research",
                file_url="https://example.com/report.pdf",
                sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                content_type="application/pdf",
            )

    assert caught.value is failure
    assert not tracked.exists()
    assert [
        path for path in (tmp_path / "downloads").rglob("*") if path.is_file()
    ] == []
    assert canonical.read_bytes() == b"canonical"
    storage.rollback_execution_transaction()
    storage.close()


def test_tracked_view_journal_handoff_interrupt_cleans_local_owned_target(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    storage = Storage(tmp_path / "handoff-interrupt.db")
    storage.begin_execution_transaction()
    processor = DocumentProcessor(storage=storage)
    failure = KeyboardInterrupt("journal handoff effected")
    real_register = storage.register_execution_created_path

    def register_then_interrupt(*args, **kwargs):
        real_register(*args, **kwargs)
        raise failure

    monkeypatch.setattr(
        storage, "register_execution_created_path", register_then_interrupt
    )
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            processor.materialize_tracked_view(
                canonical_local_path=canonical,
                page_url="https://example.com/research",
                file_url="https://example.com/report.pdf",
                sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                content_type="application/pdf",
            )

    assert caught.value is failure
    assert not tracked.exists()
    storage.rollback_execution_transaction()
    assert storage._execution_created_paths == []
    storage.close()


def test_tracked_view_replacement_before_real_journal_handoff_preserves_replacement(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    storage = Storage(tmp_path / "handoff-replacement.db")
    storage.begin_execution_transaction()
    processor = DocumentProcessor(storage=storage)
    replacement = b"replacement before register"
    replacement_identity = None
    real_register = storage.register_execution_created_path

    def replace_then_register(path, *, cleanup_root, expected_identity=None):
        nonlocal replacement_identity
        if expected_identity is None:
            raise AssertionError("publisher must supply its exact inode identity")
        Path(path).unlink()
        Path(path).write_bytes(replacement)
        info = Path(path).stat(follow_symlinks=False)
        replacement_identity = (info.st_dev, info.st_ino)
        return real_register(
            path,
            cleanup_root=cleanup_root,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(
        storage, "register_execution_created_path", replace_then_register
    )
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        with pytest.raises(ValueError, match="identity"):
            processor.materialize_tracked_view(
                canonical_local_path=canonical,
                page_url="https://example.com/research",
                file_url="https://example.com/report.pdf",
                sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                content_type="application/pdf",
            )

    current = tracked.stat(follow_symlinks=False)
    assert tracked.read_bytes() == replacement
    assert (current.st_dev, current.st_ino) == replacement_identity
    storage.rollback_execution_transaction()
    storage.close()


def test_tracked_view_interrupt_preserves_replacement_inode(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    processor = DocumentProcessor()
    failure = SystemExit(29)
    replacement = b"replacement"
    replacement_identity = None
    real_link = os.link

    def link_replace_then_interrupt(source, target, *args, **kwargs):
        nonlocal replacement_identity
        real_link(source, target, *args, **kwargs)
        Path(target).unlink()
        Path(target).write_bytes(replacement)
        info = Path(target).stat(follow_symlinks=False)
        replacement_identity = (info.st_dev, info.st_ino)
        raise failure

    monkeypatch.setattr(
        "web_listening.blocks.document.os.link", link_replace_then_interrupt
    )
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        with pytest.raises(SystemExit) as caught:
            processor.materialize_tracked_view(
                canonical_local_path=canonical,
                page_url="https://example.com/research",
                file_url="https://example.com/report.pdf",
                sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                content_type="application/pdf",
            )

    current = tracked.stat(follow_symlinks=False)
    assert caught.value is failure
    assert tracked.read_bytes() == replacement
    assert (current.st_dev, current.st_ino) == replacement_identity


def test_tracked_view_preexisting_inode_is_never_claimed_or_removed(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    processor = DocumentProcessor()
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        tracked.parent.mkdir(parents=True)
        tracked.write_bytes(b"preexisting")
        before = tracked.stat(follow_symlinks=False)
        monkeypatch.setattr(
            "web_listening.blocks.document.os.link",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("preexisting target must not be published")
            ),
        )
        result = processor.materialize_tracked_view(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )

    after = tracked.stat(follow_symlinks=False)
    assert result == tracked
    assert tracked.read_bytes() == b"preexisting"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_tracked_view_copy_fallback_interrupt_preserves_replacement_inode(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical.pdf"
    canonical.write_bytes(b"canonical")
    processor = DocumentProcessor()
    failure = KeyboardInterrupt("copy fallback target replaced")
    replacement = b"copy replacement"
    replacement_identity = None
    real_link = os.link
    link_calls = 0

    def fallback_then_replace(source, target, *args, **kwargs):
        nonlocal link_calls, replacement_identity
        link_calls += 1
        if link_calls == 1:
            raise OSError("cross-device")
        real_link(source, target, *args, **kwargs)
        Path(target).unlink()
        Path(target).write_bytes(replacement)
        info = Path(target).stat(follow_symlinks=False)
        replacement_identity = (info.st_dev, info.st_ino)
        raise failure

    monkeypatch.setattr("web_listening.blocks.document.os.link", fallback_then_replace)
    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.downloads_dir = tmp_path / "downloads"
        tracked = processor._build_tracked_view_path(
            canonical_local_path=canonical,
            page_url="https://example.com/research",
            file_url="https://example.com/report.pdf",
            sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
            content_type="application/pdf",
        )
        with pytest.raises(KeyboardInterrupt) as caught:
            processor.materialize_tracked_view(
                canonical_local_path=canonical,
                page_url="https://example.com/research",
                file_url="https://example.com/report.pdf",
                sha256=hashlib.sha256(canonical.read_bytes()).hexdigest(),
                content_type="application/pdf",
            )

    current = tracked.stat(follow_symlinks=False)
    assert caught.value is failure
    assert tracked.read_bytes() == replacement
    assert (current.st_dev, current.st_ino) == replacement_identity
    assert link_calls == 2
