import pytest
import httpx
from web_listening.blocks.crawler import BrowserCrawler, Crawler, FetchResult, normalize_fetch_mode
from web_listening.models import Site
from datetime import datetime


def make_mock_transport(html: str, status_code: int = 200):
    def handler(request):
        return httpx.Response(
            status_code=status_code,
            content=html.encode(),
            headers={"content-type": "text/html"},
        )
    return httpx.MockTransport(handler)


SAMPLE_HTML = """
<html>
<head><title>Test Page</title></head>
<body>
  <nav>Navigation</nav>
  <h1>Main Content</h1>
  <p>This is a test paragraph with important content.</p>
  <a href="https://example.com/page1">Page 1</a>
  <a href="/relative-page">Relative</a>
  <footer>Footer text</footer>
  <script>var x = 1;</script>
</body>
</html>
"""


def test_crawler_fetch_returns_html_and_text():
    transport = make_mock_transport(SAMPLE_HTML)
    client = httpx.Client(transport=transport)
    crawler = Crawler(client=client)

    html, text = crawler.fetch("https://example.com")
    assert "Main Content" in text
    assert "This is a test paragraph" in text
    # Nav and footer and script should be removed
    assert "var x = 1" not in text


def test_crawler_fetch_page_returns_normalized_artifacts():
    transport = make_mock_transport(SAMPLE_HTML)
    client = httpx.Client(transport=transport)
    crawler = Crawler(client=client)

    page = crawler.fetch_page("https://example.com")

    assert "<nav>" not in page.cleaned_html
    assert page.status_code == 200
    assert page.final_url == "https://example.com"
    assert page.markdown.startswith("# Main Content")
    assert "This is a test paragraph" in page.fit_markdown
    assert page.metadata_json["link_count"] == 2


def test_crawler_fetch_page_prefers_main_content():
    html = """
    <html>
    <body>
      <header><a href="/home">Home</a></header>
      <main>
        <h1>Actuarial Research</h1>
        <p>Important update.</p>
      </main>
      <footer>Footer</footer>
    </body>
    </html>
    """
    transport = make_mock_transport(html)
    client = httpx.Client(transport=transport)
    crawler = Crawler(client=client)

    page = crawler.fetch_page("https://example.com")

    assert "Home" not in page.fit_markdown
    assert page.markdown.startswith("# Actuarial Research")


def test_crawler_fetch_http_error():
    transport = make_mock_transport("Not Found", status_code=404)
    client = httpx.Client(transport=transport)
    crawler = Crawler(client=client)

    with pytest.raises(httpx.HTTPStatusError):
        crawler.fetch("https://example.com")


def test_crawler_snapshot_creates_snapshot():
    transport = make_mock_transport(SAMPLE_HTML)
    client = httpx.Client(transport=transport)
    crawler = Crawler(client=client)

    site = Site(id=1, url="https://example.com", name="Test")
    snapshot = crawler.snapshot(site)

    assert snapshot.site_id == 1
    assert snapshot.content_hash != ""
    assert "Main Content" in snapshot.content_text
    assert snapshot.markdown.startswith("# Main Content")
    assert snapshot.fit_markdown != ""
    assert snapshot.raw_html != ""
    assert snapshot.cleaned_html != ""
    assert snapshot.fetch_mode == "http"
    assert snapshot.final_url == "https://example.com"
    assert snapshot.status_code == 200
    assert snapshot.metadata_json["word_count"] > 0
    assert snapshot.metadata_json["hash_basis"] == "fit_markdown"
    assert snapshot.metadata_json["hash_normalization"] == "whitespace-normalized-v1"
    assert isinstance(snapshot.captured_at, datetime)
    assert isinstance(snapshot.links, list)


def test_crawler_snapshot_captures_links():
    transport = make_mock_transport(SAMPLE_HTML)
    client = httpx.Client(transport=transport)
    crawler = Crawler(client=client)

    site = Site(id=1, url="https://example.com", name="Test")
    snapshot = crawler.snapshot(site)

    # Should have absolute links
    assert any("example.com/page1" in lnk for lnk in snapshot.links)
    assert any("relative-page" in lnk for lnk in snapshot.links)


def test_crawler_context_manager():
    transport = make_mock_transport(SAMPLE_HTML)
    client = httpx.Client(transport=transport)
    with Crawler(client=client) as crawler:
        html, text = crawler.fetch("https://example.com")
    assert "Main Content" in text


def test_normalize_fetch_mode_auto_falls_back_to_http():
    assert normalize_fetch_mode("auto") == "http"


def test_crawler_snapshot_uses_browser_mode(monkeypatch):
    def fake_fetch_page(self, url: str, *, fetch_config_json=None):
        assert fetch_config_json == {"wait_for": "main"}
        return FetchResult(
            raw_html=SAMPLE_HTML,
            cleaned_html="<body><h1>Main Content</h1></body>",
            content_text="Main Content\nThis is a test paragraph with important content.",
            markdown="# Main Content\n\nThis is a test paragraph with important content.",
            fit_markdown="# Main Content\n\nThis is a test paragraph with important content.",
            metadata_json={"driver": "browser"},
            final_url=url,
            status_code=200,
        )

    monkeypatch.setattr(BrowserCrawler, "fetch_page", fake_fetch_page)

    crawler = Crawler()
    site = Site(
        id=1,
        url="https://example.com",
        name="Test",
        fetch_mode="browser",
        fetch_config_json={"wait_for": "main"},
    )

    snapshot = crawler.snapshot(site)

    assert snapshot.fetch_mode == "browser"
    assert snapshot.metadata_json["driver"] == "browser"
    assert snapshot.markdown.startswith("# Main Content")
