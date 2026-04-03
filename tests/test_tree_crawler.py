from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_crawler import (
    TreeCrawler,
    build_scope_from_site,
    canonicalize_tracked_url,
    is_file_url_in_scope,
    is_page_url_in_scope,
    sanitize_request_url,
)
from web_listening.models import CrawlScope, CrawlRun, Site


def make_tree_transport():
    html_root = """
    <html>
      <body>
        <main>
          <h1>Section Home</h1>
          <a href="https://example.com/section/page-a?utm_source=test">Page A</a>
          <a href="https://example.com/files/report.pdf">Report</a>
          <a href="https://other.example.com/out">Outside</a>
        </main>
      </body>
    </html>
    """
    html_page_a = """
    <html>
      <body>
        <main>
          <h1>Page A</h1>
          <a href="/section/page-b">Page B</a>
          <a href="https://example.com/files/report.pdf#download">Report Again</a>
        </main>
      </body>
    </html>
    """
    html_page_b = """
    <html>
      <body>
        <main>
          <h1>Page B</h1>
          <p>Stable content.</p>
        </main>
      </body>
    </html>
    """
    pdf_bytes = b"%PDF tree test"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/section":
            return httpx.Response(200, text=html_root, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/section/page-a":
            return httpx.Response(200, text=html_page_a, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/section/page-b":
            return httpx.Response(200, text=html_page_b, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/files/report.pdf":
            return httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"}, request=request)
        return httpx.Response(404, text="not found", request=request)

    return httpx.MockTransport(handler)


def test_canonicalize_tracked_url_drops_fragment_and_tracking_query():
    url = "https://Example.com/section/page-a/?utm_source=test&b=2#a"
    assert canonicalize_tracked_url(url) == "https://example.com/section/page-a?b=2"


def test_sanitize_request_url_keeps_slash_but_drops_tracking_query():
    url = "https://Example.com/section/page-a/?utm_source=test&b=2#a"
    assert sanitize_request_url(url) == "https://example.com/section/page-a/?b=2"


def test_scope_checks_keep_pages_narrow_but_files_wide():
    scope = CrawlScope(
        site_id=1,
        seed_url="https://example.com/section",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/"],
        max_depth=3,
    )

    assert is_page_url_in_scope(scope, "https://example.com/section/page-a")
    assert not is_page_url_in_scope(scope, "https://example.com/files/report.pdf")
    assert is_file_url_in_scope(scope, "https://example.com/files/report.pdf")
    assert not is_file_url_in_scope(scope, "https://other.com/files/report.pdf")


def test_build_scope_from_site_defaults_to_root_page_scope():
    site = Site(id=1, url="https://example.com/section", name="Example")
    scope = build_scope_from_site(site)

    assert scope.allowed_origin == "https://example.com"
    assert scope.allowed_page_prefixes == ["/"]
    assert scope.allowed_file_prefixes == ["/"]
    assert scope.max_depth == 3


def test_tree_crawler_bootstrap_tracks_pages_files_and_edges(tmp_path):
    storage = Storage(tmp_path / "tree.db")
    transport = make_tree_transport()
    client = httpx.Client(transport=transport, follow_redirects=True)
    crawler = Crawler(client=client)
    processor = DocumentProcessor(client=client, storage=storage)
    site = storage.add_site(Site(url="https://example.com/section", name="Example"))
    scope = CrawlScope(
        site_id=site.id,
        seed_url="https://example.com/section",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/"],
        max_depth=2,
        max_pages=10,
        max_files=5,
        fetch_mode="http",
    )

    with patch("web_listening.blocks.document.settings") as mock_doc_settings:
        mock_doc_settings.user_agent = "test-agent"
        mock_doc_settings.downloads_dir = tmp_path / "downloads"
        with TreeCrawler(storage=storage, crawler=crawler, document_processor=processor) as tree:
            result = tree.bootstrap_scope(scope, institution="Example", download_files=True)

    assert result.scope.is_initialized is True
    assert result.run.status == "completed"
    assert len(result.pages) == 3
    assert len(result.files) == 1
    assert result.off_prefix_same_origin_files == 1
    assert result.skipped_external_pages >= 1
    assert result.skipped_duplicate_files >= 1

    tracked_pages = storage.list_tracked_pages(result.scope.id)
    tracked_files = storage.list_tracked_files(result.scope.id)
    edges = storage.list_page_edges(result.scope.id, run_id=result.run.id)

    assert len(tracked_pages) == 3
    assert len(tracked_files) == 1
    assert len(edges) == 2
    assert tracked_files[0].latest_sha256 != ""

    docs = storage.list_documents(site_id=site.id)
    assert len(docs) == 1
    assert docs[0].sha256 == tracked_files[0].latest_sha256
    assert Path(docs[0].local_path).exists()

    observations = storage.list_file_observations(result.scope.id, run_id=result.run.id)
    assert len(observations) == 1

    storage.close()


def test_storage_scope_and_run_round_trip(tmp_path):
    storage = Storage(tmp_path / "scope.db")
    site = storage.add_site(Site(url="https://example.com", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url="https://example.com",
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/"],
            allowed_file_prefixes=["/"],
        )
    )
    run = storage.add_crawl_run(
        CrawlRun(
            scope_id=scope.id,
            run_type="bootstrap",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
    )
    updated = storage.update_crawl_run(run.id, status="completed", finished_at=datetime.now(timezone.utc))

    assert scope.id is not None
    assert updated.status == "completed"
    assert storage.get_crawl_scope(scope.id).seed_url == "https://example.com"
    assert len(storage.list_crawl_scopes(site_id=site.id)) == 1

    storage.close()


def test_tree_crawler_preserves_seed_trailing_slash(tmp_path):
    html_root = """
    <html>
      <body>
        <main>
          <h1>Slash Sensitive Root</h1>
          <a href="/section/page-a/">Page A</a>
        </main>
      </body>
    </html>
    """
    html_page = """
    <html>
      <body>
        <main>
          <h1>Page A</h1>
        </main>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/section/":
            return httpx.Response(200, text=html_root, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/section/page-a/":
            return httpx.Response(200, text=html_page, headers={"content-type": "text/html"}, request=request)
        return httpx.Response(404, text="not found", request=request)

    storage = Storage(tmp_path / "slash.db")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    crawler = Crawler(client=client)
    site = storage.add_site(Site(url="https://example.com/section/", name="Example Slash"))
    scope = build_scope_from_site(site, allowed_page_prefixes=["/section"], allowed_file_prefixes=["/"], max_depth=2)

    with TreeCrawler(storage=storage, crawler=crawler) as tree:
        result = tree.bootstrap_scope(scope, institution="Example Slash", download_files=False)

    assert result.run.status == "completed"
    assert len(result.pages) == 2
    assert not result.page_failures

    storage.close()
