import pytest
import httpx
from pathlib import Path
from unittest.mock import MagicMock, patch

from web_listening.blocks.document import DocumentProcessor
from web_listening.models import Document


def test_download_saves_file(tmp_path):
    """download() writes the response bytes to <institution>/<filename>."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.content = b"fake-pdf-bytes"
    mock_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_resp

    proc = DocumentProcessor(client=mock_client)

    with patch("web_listening.blocks.document.settings") as mock_settings:
        mock_settings.user_agent = "test-agent"
        mock_settings.downloads_dir = tmp_path

        result = proc.download("https://example.com/report.pdf", institution="TestOrg")

    assert result.exists()
    assert result.read_bytes() == b"fake-pdf-bytes"
    assert result.parent.name == "TestOrg"
    assert result.name == "report.pdf"


def test_process_returns_document_without_content_md(tmp_path):
    """process() returns a Document with empty content_md (no conversion)."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_resp = MagicMock()
    mock_resp.content = b"%PDF fake"
    mock_resp.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_resp

    proc = DocumentProcessor(client=mock_client)

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
    assert doc.content_md == ""  # conversion is done by external doc_to_md module
    assert doc.local_path != ""


def test_processor_has_no_conversion_methods():
    """Ensure to_markdown / _pdf_to_md / _html_to_md no longer exist."""
    assert not hasattr(DocumentProcessor, "to_markdown")
    assert not hasattr(DocumentProcessor, "_pdf_to_md")
    assert not hasattr(DocumentProcessor, "_html_to_md")
