import pytest
from pathlib import Path
from web_listening.blocks.document import DocumentProcessor


def test_html_to_md_basic():
    proc = DocumentProcessor.__new__(DocumentProcessor)
    html = "<h1>Hello</h1><p>World</p>"
    md = proc._html_to_md(html)
    assert "Hello" in md
    assert "World" in md


def test_html_to_md_links():
    proc = DocumentProcessor.__new__(DocumentProcessor)
    link_url = "https://example.com"
    html = f'<a href="{link_url}">Click here</a>'
    md = proc._html_to_md(html)
    assert "Click here" in md
    # The converted markdown should contain the original link URL somewhere
    assert link_url in md


def test_to_markdown_dispatch_html(tmp_path):
    proc = DocumentProcessor.__new__(DocumentProcessor)
    html_file = tmp_path / "test.html"
    html_file.write_text("<h1>Title</h1><p>Content</p>")
    md = proc.to_markdown(html_file)
    assert "Title" in md
    assert "Content" in md


def test_to_markdown_dispatch_text(tmp_path):
    proc = DocumentProcessor.__new__(DocumentProcessor)
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("Plain text content")
    md = proc.to_markdown(txt_file)
    assert "Plain text content" in md


def test_to_markdown_dispatch_pdf_missing(tmp_path):
    """Test PDF dispatch handles missing/invalid file gracefully."""
    proc = DocumentProcessor.__new__(DocumentProcessor)
    # Create a fake PDF (not a real PDF, so fitz will fail)
    fake_pdf = tmp_path / "test.pdf"
    fake_pdf.write_bytes(b"not a real pdf")
    result = proc.to_markdown(fake_pdf)
    # Should return error message or empty string, not raise exception
    assert isinstance(result, str)


def test_pdf_to_md_error_handling(tmp_path):
    proc = DocumentProcessor.__new__(DocumentProcessor)
    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"not a real pdf")
    result = proc._pdf_to_md(fake_pdf)
    assert isinstance(result, str)
    # Should contain error message or be empty
