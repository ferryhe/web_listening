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
          <a href="https://example.com/files/deep-report.pdf">Deep Report</a>
        </main>
      </body>
    </html>
    """
    pdf_bytes = b"%PDF tree test"
    pdf_deep_bytes = b"%PDF deep tree test"

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
        if url == "https://example.com/files/deep-report.pdf":
            return httpx.Response(200, content=pdf_deep_bytes, headers={"content-type": "application/pdf"}, request=request)
        return httpx.Response(404, text="not found", request=request)

    return httpx.MockTransport(handler)


def make_incremental_tree_transport():
    state = {"phase": "bootstrap"}
    html_root_bootstrap = """
    <html>
      <body>
        <main>
          <h1>Section Home</h1>
          <a href="https://example.com/section/page-a">Page A</a>
          <a href="https://example.com/files/report.pdf">Report</a>
        </main>
      </body>
    </html>
    """
    html_page_a_bootstrap = """
    <html>
      <body>
        <main>
          <h1>Page A</h1>
          <p>Original page A content.</p>
          <a href="/section/page-b">Page B</a>
          <a href="https://example.com/files/report.pdf">Report</a>
        </main>
      </body>
    </html>
    """
    html_page_b_bootstrap = """
    <html>
      <body>
        <main>
          <h1>Page B</h1>
          <p>Original page B content.</p>
        </main>
      </body>
    </html>
    """
    html_root_incremental = """
    <html>
      <body>
        <main>
          <h1>Section Home</h1>
          <a href="https://example.com/section/page-a">Page A</a>
          <a href="https://example.com/section/page-c">Page C</a>
          <a href="https://example.com/files/report.pdf">Report</a>
          <a href="https://example.com/files/new-report.pdf">New Report</a>
        </main>
      </body>
    </html>
    """
    html_page_a_incremental = """
    <html>
      <body>
        <main>
          <h1>Page A</h1>
          <p>Updated page A content with a meaningful change.</p>
          <a href="https://example.com/files/report.pdf">Report</a>
        </main>
      </body>
    </html>
    """
    html_page_c_incremental = """
    <html>
      <body>
        <main>
          <h1>Page C</h1>
          <p>Brand new child page.</p>
        </main>
      </body>
    </html>
    """
    pdf_bootstrap = b"%PDF report v1"
    pdf_incremental = b"%PDF report v2"
    pdf_new = b"%PDF new report"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if state["phase"] == "bootstrap":
            if url == "https://example.com/section":
                return httpx.Response(200, text=html_root_bootstrap, headers={"content-type": "text/html"}, request=request)
            if url == "https://example.com/section/page-a":
                return httpx.Response(200, text=html_page_a_bootstrap, headers={"content-type": "text/html"}, request=request)
            if url == "https://example.com/section/page-b":
                return httpx.Response(200, text=html_page_b_bootstrap, headers={"content-type": "text/html"}, request=request)
            if url == "https://example.com/files/report.pdf":
                return httpx.Response(200, content=pdf_bootstrap, headers={"content-type": "application/pdf"}, request=request)
        else:
            if url == "https://example.com/section":
                return httpx.Response(200, text=html_root_incremental, headers={"content-type": "text/html"}, request=request)
            if url == "https://example.com/section/page-a":
                return httpx.Response(200, text=html_page_a_incremental, headers={"content-type": "text/html"}, request=request)
            if url == "https://example.com/section/page-c":
                return httpx.Response(200, text=html_page_c_incremental, headers={"content-type": "text/html"}, request=request)
            if url == "https://example.com/files/report.pdf":
                return httpx.Response(200, content=pdf_incremental, headers={"content-type": "application/pdf"}, request=request)
            if url == "https://example.com/files/new-report.pdf":
                return httpx.Response(200, content=pdf_new, headers={"content-type": "application/pdf"}, request=request)
        return httpx.Response(404, text="not found", request=request)

    return state, httpx.MockTransport(handler)


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
    assert len(result.files) == 2
    assert result.off_prefix_same_origin_files == 2
    assert result.skipped_external_pages >= 1
    assert result.skipped_duplicate_files >= 1

    tracked_pages = storage.list_tracked_pages(result.scope.id)
    tracked_files = storage.list_tracked_files(result.scope.id)
    edges = storage.list_page_edges(result.scope.id, run_id=result.run.id)

    assert len(tracked_pages) == 3
    assert len(tracked_files) == 2
    assert len(edges) == 2
    assert all(item.latest_sha256 != "" for item in tracked_files)

    docs = storage.list_documents(site_id=site.id)
    assert len(docs) == 2
    assert {doc.sha256 for doc in docs} == {item.latest_sha256 for item in tracked_files}
    assert all(Path(doc.local_path).exists() for doc in docs)

    observations = storage.list_file_observations(result.scope.id, run_id=result.run.id)
    assert len(observations) == 2

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


def test_tree_crawler_incremental_reports_new_changed_and_missing_items(tmp_path):
    storage = Storage(tmp_path / "incremental.db")
    state, transport = make_incremental_tree_transport()
    client = httpx.Client(transport=transport, follow_redirects=True)
    crawler = Crawler(client=client)
    processor = DocumentProcessor(client=client, storage=storage)
    site = storage.add_site(Site(url="https://example.com/section", name="Example Incremental"))
    scope = CrawlScope(
        site_id=site.id,
        seed_url="https://example.com/section",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/"],
        max_depth=2,
        max_pages=10,
        max_files=10,
        fetch_mode="http",
    )

    with patch("web_listening.blocks.document.settings") as mock_doc_settings:
        mock_doc_settings.user_agent = "test-agent"
        mock_doc_settings.downloads_dir = tmp_path / "downloads"
        with TreeCrawler(storage=storage, crawler=crawler, document_processor=processor) as tree:
            bootstrap = tree.bootstrap_scope(scope, institution="Example Incremental", download_files=True)
            state["phase"] = "incremental"
            incremental = tree.run_scope(bootstrap.scope, institution="Example Incremental", download_files=True)

    assert bootstrap.run.status == "completed"
    assert incremental.run.status == "completed"
    assert "https://example.com/section/page-c" in incremental.new_pages
    assert "https://example.com/section/page-a" in incremental.changed_pages
    assert "https://example.com/section/page-b" in incremental.missing_pages
    assert "https://example.com/files/new-report.pdf" in incremental.new_files
    assert "https://example.com/files/report.pdf" in incremental.changed_files
    assert incremental.missing_files == []
    assert incremental.run.pages_changed >= 3
    assert incremental.run.files_changed >= 2

    storage.close()


def test_tree_crawler_bootstrap_tolerates_file_download_failures(tmp_path):
    html_root = """
    <html>
      <body>
        <main>
          <h1>File Failure Root</h1>
          <a href="https://example.com/files/missing.pdf">Broken file</a>
        </main>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/root":
            return httpx.Response(200, text=html_root, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/files/missing.pdf":
            return httpx.Response(404, text="missing", request=request)
        return httpx.Response(404, text="not found", request=request)

    storage = Storage(tmp_path / "file-failure.db")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    crawler = Crawler(client=client)
    processor = DocumentProcessor(client=client, storage=storage)
    site = storage.add_site(Site(url="https://example.com/root", name="File Failure"))
    scope = CrawlScope(
        site_id=site.id,
        seed_url="https://example.com/root",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
        max_depth=1,
        max_pages=5,
        max_files=5,
        fetch_mode="http",
    )

    with patch("web_listening.blocks.document.settings") as mock_doc_settings:
        mock_doc_settings.user_agent = "test-agent"
        mock_doc_settings.downloads_dir = tmp_path / "downloads"
        with TreeCrawler(storage=storage, crawler=crawler, document_processor=processor) as tree:
            result = tree.bootstrap_scope(scope, institution="File Failure", download_files=True)

    assert result.run.status == "completed"
    assert result.pages
    assert result.files == []
    assert len(result.file_failures) == 1

    storage.close()


def test_tree_crawler_skips_pages_that_redirect_outside_scope(tmp_path):
    html_root = """
    <html>
      <body>
        <main>
          <h1>Root</h1>
          <a href="https://example.com/go">Go</a>
        </main>
      </body>
    </html>
    """
    html_login = """
    <html>
      <body>
        <main>
          <h1>Login</h1>
        </main>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/root":
            return httpx.Response(200, text=html_root, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/go":
            return httpx.Response(302, headers={"location": "https://secure.example.com/login"}, request=request)
        if url == "https://secure.example.com/login":
            return httpx.Response(200, text=html_login, headers={"content-type": "text/html"}, request=request)
        return httpx.Response(404, text="not found", request=request)

    storage = Storage(tmp_path / "redirect.db")
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    crawler = Crawler(client=client)
    site = storage.add_site(Site(url="https://example.com/root", name="Redirect Example"))
    scope = CrawlScope(
        site_id=site.id,
        seed_url="https://example.com/root",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/"],
        allowed_file_prefixes=["/"],
        max_depth=2,
        max_pages=10,
        max_files=5,
        fetch_mode="http",
    )

    with TreeCrawler(storage=storage, crawler=crawler) as tree:
        result = tree.bootstrap_scope(scope, institution="Redirect Example", download_files=False)

    tracked_pages = storage.list_tracked_pages(result.scope.id)

    assert result.run.status == "completed"
    assert len(tracked_pages) == 1
    assert tracked_pages[0].canonical_url == "https://example.com/root"
    assert result.skipped_external_pages >= 1

    storage.close()
