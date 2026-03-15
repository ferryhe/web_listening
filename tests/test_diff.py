import pytest
from web_listening.blocks.diff import (
    compute_hash,
    compute_diff,
    extract_links,
    find_new_links,
    find_document_links,
)


def test_compute_hash_same_content():
    assert compute_hash("hello") == compute_hash("hello")


def test_compute_hash_different_content():
    assert compute_hash("hello") != compute_hash("world")


def test_compute_diff_no_change():
    changed, snippet = compute_diff("hello", "hello")
    assert changed is False
    assert snippet == ""


def test_compute_diff_with_change():
    changed, snippet = compute_diff("old content", "new content")
    assert changed is True
    assert len(snippet) > 0


def test_extract_links_basic():
    html = '<html><body><a href="https://example.com/page">Link</a></body></html>'
    links = extract_links(html, "https://example.com")
    assert "https://example.com/page" in links


def test_extract_links_relative():
    html = '<html><body><a href="/about">About</a></body></html>'
    links = extract_links(html, "https://example.com")
    assert "https://example.com/about" in links


def test_extract_links_filters_non_http():
    html = '<html><body><a href="mailto:test@example.com">Email</a><a href="https://ok.com">OK</a></body></html>'
    links = extract_links(html, "https://example.com")
    assert all(l.startswith("http") for l in links)


def test_find_new_links():
    old = ["https://a.com", "https://b.com"]
    new = ["https://a.com", "https://b.com", "https://c.com"]
    assert find_new_links(old, new) == ["https://c.com"]


def test_find_new_links_no_new():
    old = ["https://a.com"]
    new = ["https://a.com"]
    assert find_new_links(old, new) == []


def test_find_document_links():
    links = [
        "https://example.com/report.pdf",
        "https://example.com/data.xlsx",
        "https://example.com/page.html",
        "https://example.com/doc.docx",
    ]
    docs = find_document_links(links)
    assert "https://example.com/report.pdf" in docs
    assert "https://example.com/data.xlsx" in docs
    assert "https://example.com/doc.docx" in docs
    assert "https://example.com/page.html" not in docs


def test_find_document_links_empty():
    assert find_document_links([]) == []
