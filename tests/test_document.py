import hashlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from web_listening.blocks.document import DocumentProcessor
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
