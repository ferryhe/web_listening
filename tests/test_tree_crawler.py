import base64
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from web_listening.blocks.crawler import Crawler, FetchResult, HttpCrawler
from web_listening.blocks.acquisition_gateway import AcquisitionOutcome, GovernedAcquisitionGateway
from web_listening.blocks.staged_workflow import _compile_acquisition_gateway
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.storage import Storage
from web_listening.contracts import CaptureContent, CaptureResult
from web_listening.executors.http_wrapper import HttpAcquisitionAdapter
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


def make_root_entry_transport():
    html_root = """
    <html>
      <body>
        <main>
          <h1>Home</h1>
          <a href="https://example.com/research/page-a">Research A</a>
          <a href="https://example.com/education/page-b">Education B</a>
        </main>
      </body>
    </html>
    """
    html_research = """
    <html>
      <body>
        <main>
          <h1>Research A</h1>
          <a href="https://example.com/research/page-c">Research C</a>
        </main>
      </body>
    </html>
    """
    html_research_c = """
    <html>
      <body>
        <main>
          <h1>Research C</h1>
        </main>
      </body>
    </html>
    """
    html_education = """
    <html>
      <body>
        <main>
          <h1>Education B</h1>
        </main>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/":
            return httpx.Response(200, text=html_root, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/research/page-a":
            return httpx.Response(200, text=html_research, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/research/page-c":
            return httpx.Response(200, text=html_research_c, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/education/page-b":
            return httpx.Response(200, text=html_education, headers={"content-type": "text/html"}, request=request)
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
    assert all(item.tracked_local_path for item in observations)
    assert all(Path(item.tracked_local_path).exists() for item in observations)
    assert all("_tracked" in item.tracked_local_path for item in observations)


def test_tree_crawler_uses_out_of_scope_seed_as_entrypoint_only(tmp_path):
    storage = Storage(tmp_path / "tree.db")
    client = httpx.Client(transport=make_root_entry_transport(), follow_redirects=True)
    crawler = Crawler(client=client)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = CrawlScope(
        site_id=site.id,
        seed_url="https://example.com/",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/research"],
        allowed_file_prefixes=["/"],
        max_depth=3,
        max_pages=10,
        max_files=5,
        fetch_mode="http",
    )

    with TreeCrawler(storage=storage, crawler=crawler) as tree:
        result = tree.bootstrap_scope(scope, institution="Example", download_files=False)

    tracked_pages = storage.list_tracked_pages(result.scope.id)
    tracked_urls = {item.canonical_url for item in tracked_pages}

    assert result.run.status == "completed"
    assert len(result.pages) == 2
    assert tracked_urls == {
        "https://example.com/research/page-a",
        "https://example.com/research/page-c",
    }
    assert "https://example.com/" not in tracked_urls

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


def test_governed_tree_budget_bounds_requested_pages_and_readmits_results(tmp_path):
    storage = Storage(tmp_path / "governed-budget.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Governed"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/files"], max_depth=2, max_pages=2, max_files=1,
        fetch_mode="http",
    )

    class Gateway:
        def __init__(self):
            self.urls = []

        def acquire(self, url, *, run_id, scope_id, content_kind="page"):
            self.urls.append(url)
            if len(self.urls) == 1:
                html = (
                    '<a href="/section/a">A</a><a href="/section/b">B</a>'
                    '<a href="https://evil.example/out">out</a>'
                    '<a href="/private/secret.pdf">bad doc</a>'
                )
                page = FetchResult(html, html, html, html, html, {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            return AcquisitionOutcome(None, None, None, "timeout", ("timeout",), False)

        def close(self):
            return None

    gateway = Gateway()
    with TreeCrawler(storage=storage, acquisition_gateway=gateway) as tree:
        result = tree.bootstrap_scope(scope, download_files=False)

    assert gateway.urls == ["https://example.com/section", "https://example.com/section/a"]
    assert [page.canonical_url for page in result.pages] == ["https://example.com/section"]
    assert result.skipped_external_pages == 1
    assert result.skipped_external_files == 1
    assert len(storage.list_tracked_pages(result.scope.id)) == 1
    storage.close()


def test_incremental_failures_only_mark_exact_confirmed_not_found_missing(tmp_path):
    storage = Storage(tmp_path / "governed-missing.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Governed Missing"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/"], max_depth=1, max_pages=3, max_files=1,
        fetch_mode="http",
    )

    class Gateway:
        phase = "bootstrap"

        def acquire(self, url, *, run_id, scope_id, content_kind="page"):
            if url.endswith("/section"):
                html = '<a href="/section/a">A</a><a href="/section/b">B</a>'
                page = FetchResult(html, html, html, html, html, {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            if self.phase == "bootstrap":
                page = FetchResult("ok", "ok", "ok", "ok", "ok", {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            classification = "not_found" if url.endswith("/a") else "timeout"
            result = SimpleNamespace(final_url=url) if classification == "not_found" else None
            return AcquisitionOutcome(None, result, None, classification, (classification,), classification == "not_found")

        def close(self):
            return None

    gateway = Gateway()
    tree = TreeCrawler(storage=storage, acquisition_gateway=gateway)
    bootstrap = tree.bootstrap_scope(scope, download_files=False)
    gateway.phase = "incremental"
    incremental = tree.run_scope(bootstrap.scope, download_files=False)

    assert incremental.missing_pages == ["https://example.com/section/a"]
    assert "https://example.com/section/b" not in incremental.missing_pages
    assert len(incremental.page_failures) == 2
    tree.close()
    storage.close()


def test_incremental_out_of_prefix_same_origin_404_redirect_is_not_missing(tmp_path):
    storage = Storage(tmp_path / "redirected-missing.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Redirected Missing"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/"], max_depth=1, max_pages=2, max_files=1,
        fetch_mode="http",
    )

    class Gateway:
        phase = "bootstrap"

        def acquire(self, url, *, run_id, scope_id, content_kind="page"):
            if url.endswith("/section"):
                html = '<a href="/section/a">A</a>'
                page = FetchResult(html, html, html, html, html, {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            if self.phase == "bootstrap":
                page = FetchResult("ok", "ok", "ok", "ok", "ok", {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            result = SimpleNamespace(final_url="https://example.com/elsewhere/missing", status_code=404)
            return AcquisitionOutcome(None, result, None, "not_found", ("not_found",), True)

        def close(self):
            return None

    gateway = Gateway()
    tree = TreeCrawler(storage=storage, acquisition_gateway=gateway)
    bootstrap = tree.bootstrap_scope(scope, download_files=False)
    gateway.phase = "incremental"
    incremental = tree.run_scope(bootstrap.scope, download_files=False)

    assert incremental.missing_pages == []
    assert len(incremental.page_failures) == 1
    tree.close()
    storage.close()


@pytest.mark.parametrize("rejection", ["blocked", "out_of_scope_final"])
def test_governed_document_rejection_consumes_budget_before_storage(tmp_path, rejection):
    storage = Storage(tmp_path / f"governed-file-{rejection}.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Governed Files"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/files"], max_depth=1, max_pages=2, max_files=1,
        fetch_mode="http",
    )

    class Processor:
        def __init__(self):
            self.processed = []

        def process(self, url, **kwargs):
            self.processed.append(url)
            raise AssertionError("rejected governed file reached processor")

        def close(self):
            return None

    class Gateway:
        def __init__(self):
            self.calls = []

        def acquire(self, url, *, run_id, scope_id, content_kind="page"):
            self.calls.append((url, content_kind))
            if content_kind == "page":
                html = '<a href="/files/a.pdf">A</a><a href="/files/b.pdf">B</a>'
                page = FetchResult(html, html, html, html, html, {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            if rejection == "blocked":
                return AcquisitionOutcome(None, None, None, "blocked", ("blocked",), False)
            page = FetchResult("pdf", "pdf", "pdf", "pdf", "pdf", {},
                               "https://evil.example/files/a.pdf", 200)
            return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)

        def close(self):
            return None

    gateway = Gateway()
    processor = Processor()
    with TreeCrawler(
        storage=storage, acquisition_gateway=gateway, document_processor=processor
    ) as tree:
        result = tree.bootstrap_scope(scope, download_files=True)

    assert gateway.calls == [
        ("https://example.com/section", "page"),
        ("https://example.com/files/a.pdf", "document"),
    ]
    assert processor.processed == []
    assert result.files == []
    assert storage.list_tracked_files(result.scope.id) == []
    storage.close()


@pytest.mark.parametrize(
    ("encoded", "digest"),
    [
        ("%%%not-base64%%%", hashlib.sha256(b"anything").hexdigest()),
        (base64.b64encode(b"actual bytes").decode(), hashlib.sha256(b"other bytes").hexdigest()),
    ],
)
def test_governed_document_integrity_failure_creates_no_file_state_without_downloads(
    tmp_path, encoded, digest,
):
    storage = Storage(tmp_path / "governed-integrity.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Integrity"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/files"], max_depth=1, max_pages=1, max_files=1,
        fetch_mode="http",
    )
    plan = SimpleNamespace(
        mode="governed",
        steps=({
            "position": 0, "executor_id": "web_http", "executor_version": "1.0.0",
            "recipe_id": "recipe", "script_sha256": "a" * 64, "config": {},
        },),
        acquisition_fingerprint="b" * 64, site_key="demo", site_skill_id="skill",
        site_skill_version="1.0.0", site_skill_package_sha256="a" * 64,
        quality_gates={"min_words": 1, "min_links": 0, "min_document_links": 0,
                       "blocked_markers": ()},
    )

    class Registry:
        def execute(self, request):
            now = datetime.now(timezone.utc)
            lineage = {field: getattr(request, field) for field in (
                "request_id", "site_key", "site_skill_id", "site_skill_version",
                "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id",
            )}
            if request.metadata["content_kind"] == "page":
                content = CaptureContent(
                    media_type="text/html", text='<p>visible</p><a href="/files/report.pdf">R</a>',
                )
            else:
                content = CaptureContent(
                    media_type="application/pdf", text=encoded, sha256=digest,
                    metadata={"representation": "base64", "sha256_scope": "decoded-bytes"},
                )
            return CaptureResult(
                **lineage, state="succeeded", started_at=now, finished_at=now,
                final_url=request.url, status_code=200, content=content,
            )

    gateway = GovernedAcquisitionGateway(plan, Registry())
    with TreeCrawler(storage=storage, acquisition_gateway=gateway) as tree:
        result = tree.bootstrap_scope(scope, download_files=False)

    assert result.files == []
    assert storage.list_tracked_files(result.scope.id) == []
    assert storage.list_file_observations(result.scope.id) == []
    assert storage.list_documents(site_id=site.id) == []
    assert storage.list_changes(site_id=site.id) == []
    assert storage.conn.execute("SELECT COUNT(*) FROM document_blobs").fetchone()[0] == 0
    storage.close()


def test_governed_document_persists_admitted_bytes_without_second_fetch(tmp_path):
    storage = Storage(tmp_path / "governed-document.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Governed Document"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/files"], max_depth=1, max_pages=2, max_files=1,
        fetch_mode="http",
    )
    admitted_text = "governed document bytes"
    admitted_bytes = admitted_text.encode("utf-8")
    admitted_sha = hashlib.sha256(admitted_bytes).hexdigest()
    final_url = "https://example.com/files/final.pdf"

    class Processor(DocumentProcessor):
        def process(self, url, **kwargs):  # pragma: no cover - regression guard
            raise AssertionError("governed persistence performed a second fetch")

    class Gateway:
        def __init__(self):
            self.calls = []

        def acquire(self, url, *, run_id, scope_id, content_kind="page"):
            self.calls.append((url, content_kind))
            if content_kind == "page":
                html = '<a href="/files/original.pdf">A</a><a href="/files/ignored.pdf">B</a>'
                page = FetchResult(html, html, html, html, html, {}, url, 200)
                return AcquisitionOutcome(None, None, page, "accepted", ("accepted",), True)
            now = datetime.now(timezone.utc)
            capture = CaptureResult(
                request_id="request", site_key="demo", site_skill_id="skill",
                site_skill_version="1.0.0", site_skill_digest="a" * 64,
                recipe_id="recipe", run_id=run_id, scope_id=scope_id,
                executor_id="web_http", state="succeeded", started_at=now,
                finished_at=now, final_url=final_url, status_code=200,
                content=CaptureContent(media_type="application/pdf", text=admitted_text,
                                       sha256=admitted_sha),
            )
            page = FetchResult(admitted_text, admitted_text, admitted_text, admitted_text,
                               admitted_text, {}, final_url, 200)
            return AcquisitionOutcome(None, capture, page, "accepted", ("accepted",), True)

        def close(self):
            return None

    gateway = Gateway()
    processor = Processor(storage=storage)
    with patch("web_listening.blocks.document.settings") as doc_settings:
        doc_settings.downloads_dir = tmp_path / "downloads"
        with TreeCrawler(storage=storage, acquisition_gateway=gateway,
                         document_processor=processor) as tree:
            result = tree.bootstrap_scope(scope, download_files=True)

    assert gateway.calls[0] == ("https://example.com/section", "page")
    assert len(gateway.calls) == 2
    assert gateway.calls[1][0] in {
        "https://example.com/files/original.pdf", "https://example.com/files/ignored.pdf"
    }
    assert gateway.calls[1][1] == "document"
    assert [item.canonical_url for item in result.files] == [final_url]
    document = storage.list_documents(site_id=site.id)[0]
    assert document.download_url == final_url
    assert document.sha256 == admitted_sha
    assert Path(document.local_path).read_bytes() == admitted_bytes
    assert storage.list_file_observations(result.scope.id)[0].download_url == final_url
    storage.close()


def test_governed_document_type_matches_blob_suffix_for_parameterized_media_type(tmp_path):
    storage = Storage(tmp_path / "governed-document-type.db")
    site = storage.add_site(Site(url="https://example.com/section", name="Document Type"))
    payload = b"%PDF governed"
    digest = hashlib.sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    capture = CaptureResult(
        request_id="request", site_key="demo", site_skill_id="skill",
        site_skill_version="1.0.0", site_skill_digest="a" * 64,
        recipe_id="recipe", run_id="run", scope_id="scope",
        executor_id="web_http", state="succeeded", started_at=now,
        finished_at=now, final_url="https://example.com/files/report", status_code=200,
        content=CaptureContent(
            media_type="application/pdf; version=1.7", text=base64.b64encode(payload).decode(),
            sha256=digest,
            metadata={"representation": "base64", "sha256_scope": "decoded-bytes"},
        ),
    )
    processor = DocumentProcessor(storage=storage)

    with patch("web_listening.blocks.document.settings") as doc_settings:
        doc_settings.downloads_dir = tmp_path / "downloads"
        with TreeCrawler(storage=storage, acquisition_gateway=SimpleNamespace(close=lambda: None),
                         document_processor=processor) as tree:
            document = tree._document_from_capture(
                capture, site_id=site.id, institution="", page_url=site.url,
                file_url="https://example.com/files/report",
            )

    assert Path(document.local_path).suffix == ".pdf"
    assert document.doc_type == "pdf"
    storage.close()


def test_real_governed_http_document_preserves_non_utf8_response_bytes(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "governed-http-document.db")
    site = storage.add_site(Site(url="https://example.com/section", name="HTTP Bytes"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/files"], max_depth=1, max_pages=1, max_files=1,
        fetch_mode="http",
    )
    payload = b"%PDF-1.7\n\xff\xfe\x00\x80binary\n%%EOF"
    digest = hashlib.sha256(payload).hexdigest()
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/section":
            return httpx.Response(
                200, request=request,
                text='<p>visible page words</p><a href="/files/report.pdf">report</a>',
                headers={"content-type": "text/html; charset=utf-8"},
            )
        return httpx.Response(
            200, request=request, content=payload,
            headers={"content-type": "application/pdf; version=1.7"},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True)
    adapter = HttpAcquisitionAdapter(HttpCrawler(client=client))
    step = {
        "position": 0, "executor_id": "web_http", "executor_version": "1.0.0",
        "recipe_id": "recipe", "script_sha256": "a" * 64, "config": {},
    }
    compiled = SimpleNamespace(
        mode="governed", steps=(step,), acquisition_fingerprint="b" * 64,
        site_key="demo", site_skill_id="skill", site_skill_version="1.0.0",
        site_skill_package_sha256="a" * 64,
        quality_gates={"min_words": 1, "min_links": 0, "min_document_links": 0,
                       "blocked_markers": ()},
    )
    monkeypatch.setattr("web_listening.blocks.acquisition_profile.load_acquisition_profile", lambda *a, **k: object())
    monkeypatch.setattr("web_listening.site_skill_registry.resolve_site_skill_contract", lambda **k: object())
    monkeypatch.setattr("web_listening.blocks.acquisition_execution_plan.compile_acquisition_execution_plan",
                        lambda *a: compiled)
    monkeypatch.setattr("web_listening.executors.http_wrapper.HttpAcquisitionAdapter", lambda: adapter)
    plan = SimpleNamespace(site_key="demo", based_on={"acquisition_profile_id": "profile"})
    gateway = _compile_acquisition_gateway(plan, acquisition_profile_path="profile.yaml")

    class Processor(DocumentProcessor):
        def process(self, url, **kwargs):  # pragma: no cover - no refetch guard
            raise AssertionError("governed document was refetched")

    processor = Processor(storage=storage)
    with patch("web_listening.blocks.document.settings") as doc_settings:
        doc_settings.downloads_dir = tmp_path / "downloads"
        with TreeCrawler(storage=storage, acquisition_gateway=gateway,
                         document_processor=processor) as tree:
            result = tree.bootstrap_scope(scope, download_files=True)

    document = storage.list_documents(site_id=site.id)[0]
    assert requests == ["https://example.com/section", "https://example.com/files/report.pdf"]
    assert Path(document.local_path).read_bytes() == payload
    assert document.content_type == "application/pdf; version=1.7"
    assert document.sha256 == digest
    assert result.files[0].latest_sha256 == digest
    client.close()
    storage.close()


def test_narrower_incremental_depth_does_not_infer_deeper_pages_missing(tmp_path):
    storage = Storage(tmp_path / "depth-coverage.db")
    transport = make_tree_transport()
    client = httpx.Client(transport=transport, follow_redirects=True)
    crawler = Crawler(client=client)
    site = storage.add_site(Site(url="https://example.com/section", name="Depth Coverage"))
    scope = CrawlScope(
        site_id=site.id, seed_url="https://example.com/section",
        allowed_origin="https://example.com", allowed_page_prefixes=["/section"],
        allowed_file_prefixes=["/"], max_depth=2, max_pages=10, max_files=1,
        fetch_mode="http",
    )

    with TreeCrawler(storage=storage, crawler=crawler) as tree:
        bootstrap = tree.bootstrap_scope(scope, download_files=False)
        shallower = CrawlScope(**{**bootstrap.scope.model_dump(), "max_depth": 1})
        incremental = tree.run_scope(shallower, download_files=False)

    assert "https://example.com/section/page-b" not in incremental.missing_pages
    storage.close()


def test_governed_blob_publication_cleans_temp_after_publish_failure(tmp_path, monkeypatch):
    destination = tmp_path / "blobs" / "payload.bin"
    payload = b"complete governed bytes"
    digest = hashlib.sha256(payload).hexdigest()

    def fail_publish(*args, **kwargs):
        raise OSError("injected publish failure")

    monkeypatch.setattr("web_listening.blocks.tree_crawler.os.link", fail_publish)
    with pytest.raises(OSError, match="injected publish failure"):
        TreeCrawler._publish_governed_blob(destination, payload, digest)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_tree_crawler_close_attempts_every_resource_and_is_idempotent():
    closed = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def close(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} close failed")

    tree = TreeCrawler(
        storage=SimpleNamespace(), crawler=Resource("crawler", fail=True),
        acquisition_gateway=Resource("gateway"), document_processor=Resource("document"),
    )
    tree._owns_crawler = True

    with pytest.raises(RuntimeError, match="crawler close failed"):
        tree.close()
    tree.close()

    assert closed == ["crawler", "gateway", "document"]
