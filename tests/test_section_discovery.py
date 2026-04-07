import httpx

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.section_discovery import CatalogSectionInventory, SectionDiscoverer, render_markdown, render_yaml


def make_section_transport():
    html_root = """
    <html>
      <body>
        <main>
          <h1>Example Home</h1>
          <a href="https://example.com/research">Research</a>
          <a href="https://example.com/about/governance/annual-reports">Annual Reports</a>
          <a href="https://example.com/news">News</a>
        </main>
      </body>
    </html>
    """
    html_research = """
    <html>
      <body>
        <main>
          <h1>Research Papers</h1>
          <a href="https://example.com/files/research-paper.pdf">Paper</a>
        </main>
      </body>
    </html>
    """
    html_governance = """
    <html>
      <body>
        <main>
          <h1>Annual Reports</h1>
          <a href="https://example.com/files/annual-report-2025.pdf">Report</a>
          <a href="https://example.com/about/governance/board">Board</a>
        </main>
      </body>
    </html>
    """
    html_news = """
    <html>
      <body>
        <main>
          <h1>Latest News</h1>
        </main>
      </body>
    </html>
    """
    html_board = """
    <html>
      <body>
        <main>
          <h1>Board of Directors</h1>
        </main>
      </body>
    </html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/":
            return httpx.Response(200, text=html_root, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/research":
            return httpx.Response(200, text=html_research, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/about/governance/annual-reports":
            return httpx.Response(200, text=html_governance, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/news":
            return httpx.Response(200, text=html_news, headers={"content-type": "text/html"}, request=request)
        if url == "https://example.com/about/governance/board":
            return httpx.Response(200, text=html_board, headers={"content-type": "text/html"}, request=request)
        return httpx.Response(404, text="not found", request=request)

    return httpx.MockTransport(handler)


def test_section_discoverer_builds_section_inventory():
    client = httpx.Client(transport=make_section_transport(), follow_redirects=True)
    crawler = Crawler(client=client)

    with SectionDiscoverer(crawler=crawler) as discoverer:
        result = discoverer.discover_target(
            site_key="demo",
            display_name="Demo",
            seed_url="https://example.com/",
            homepage_url="https://example.com/",
            fetch_mode="http",
            fetch_config_json={},
            allowed_page_prefixes=["/"],
            allowed_file_prefixes=["/"],
            discovery_depth=3,
            section_depth=3,
            max_pages=None,
        )

    assert result.pages_discovered == 5
    assert result.unique_document_links == 0
    assert result.discovery_mode == "structure_only"
    assert result.page_limit_mode == "unbounded"
    assert result.discovery_strategy == "adaptive_sections"
    assert result.level2_pages_discovered == 3
    assert result.sampled_level3_pages == 2
    by_path = {item.section_path: item for item in result.sections}
    assert by_path["/research"].candidate_category == "research_publications"
    assert by_path["/research"].doc_link_count == 0
    assert by_path["/about"].page_count == 2
    assert by_path["/about"].child_section_count == 1
    assert by_path["/about/governance/annual-reports"].candidate_category == "governance_management"


def test_render_yaml_and_markdown_include_expected_fields():
    inventory = CatalogSectionInventory(
        catalog="dev",
        generated_at="2026-04-06T00:00:00+00:00",
        discovery_depth=3,
        section_depth=3,
        max_pages=0,
        sites=[],
    )

    yaml_text = render_yaml(inventory.to_dict())
    markdown = render_markdown(inventory)

    assert "catalog: \"dev\"" in yaml_text
    assert "generated_at: \"2026-04-06T00:00:00+00:00\"" in yaml_text
    assert "page_limit_mode: \"unbounded\"" in yaml_text
    assert "discovery_strategy: \"adaptive_sections\"" in yaml_text
    assert "# Discover Site Sections" in markdown
    assert "level-2 pages" in markdown.lower()
    assert "not the number of level-1 directories" in markdown


def test_section_discoverer_can_optionally_count_document_links():
    client = httpx.Client(transport=make_section_transport(), follow_redirects=True)
    crawler = Crawler(client=client)

    with SectionDiscoverer(crawler=crawler) as discoverer:
        result = discoverer.discover_target(
            site_key="demo",
            display_name="Demo",
            seed_url="https://example.com/",
            homepage_url="https://example.com/",
            fetch_mode="http",
            fetch_config_json={},
            allowed_page_prefixes=["/"],
            allowed_file_prefixes=["/"],
            discovery_depth=3,
            section_depth=3,
            max_pages=10,
            detect_documents=True,
        )

    assert result.discovery_mode == "structure_plus_documents"
    assert result.unique_document_links == 2
    by_path = {item.section_path: item for item in result.sections}
    assert by_path["/research"].doc_link_count == 1


def test_section_discoverer_marks_branch_for_expansion_when_sample_limit_is_hit():
    client = httpx.Client(transport=make_section_transport(), follow_redirects=True)
    crawler = Crawler(client=client)

    with SectionDiscoverer(crawler=crawler) as discoverer:
        result = discoverer.discover_target(
            site_key="demo",
            display_name="Demo",
            seed_url="https://example.com/",
            homepage_url="https://example.com/",
            fetch_mode="http",
            fetch_config_json={},
            allowed_page_prefixes=["/"],
            allowed_file_prefixes=["/"],
            discovery_depth=3,
            section_depth=3,
            max_pages=None,
            level3_sample_limit=1,
        )

    assert result.sampled_level3_pages == 1
    assert result.skipped_level3_candidate_pages == 1
    assert result.expansion_candidates
    assert result.expansion_candidates[0].branch_path == "/about/governance"
