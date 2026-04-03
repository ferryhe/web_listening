import pytest
from web_listening.blocks.diff import (
    canonicalize_text_for_hash,
    compute_hash,
    compute_diff,
    extract_links,
    find_new_links,
    find_document_links,
    select_compare_artifact,
    select_compare_text,
)


def test_compute_hash_same_content():
    assert compute_hash("hello") == compute_hash("hello")


def test_compute_hash_different_content():
    assert compute_hash("hello") != compute_hash("world")


def test_compute_hash_ignores_whitespace_only_differences():
    left = "Hello   world\n\nLine two"
    right = "Hello world\r\n\r\n\r\nLine two   "
    assert compute_hash(left) == compute_hash(right)


def test_canonicalize_text_for_hash_collapses_blank_lines():
    text = "A\n\n\nB\n   \nC"
    assert canonicalize_text_for_hash(text) == "A\n\nB\n\nC"


def test_compute_diff_no_change():
    changed, snippet = compute_diff("hello", "hello")
    assert changed is False
    assert snippet == ""


def test_compute_diff_with_change():
    changed, snippet = compute_diff("old content", "new content")
    assert changed is True
    assert len(snippet) > 0


def test_select_compare_text_prefers_fit_markdown():
    selected = select_compare_text(
        fit_markdown="# Fit",
        markdown="# Markdown",
        content_text="plain text",
    )
    assert selected == "# Fit"


def test_select_compare_text_falls_back_to_content_text():
    selected = select_compare_text(
        fit_markdown="",
        markdown="",
        content_text="plain text",
    )
    assert selected == "plain text"


def test_select_compare_artifact_returns_source_name():
    source, selected = select_compare_artifact(
        fit_markdown="",
        markdown="# Markdown",
        content_text="plain text",
    )
    assert source == "markdown"
    assert selected == "# Markdown"


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


def test_extract_links_from_sitemap_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset>
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/b</loc></url>
    </urlset>
    """
    links = extract_links(xml, "https://example.com")
    assert links == ["https://example.com/a", "https://example.com/b"]


def test_extract_links_from_rss_xml():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <item><title>One</title><link>https://example.com/one</link></item>
        <item><title>Two</title><link>https://example.com/two</link></item>
      </channel>
    </rss>
    """
    links = extract_links(xml, "https://example.com")
    assert links == ["https://example.com/one", "https://example.com/two"]


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
