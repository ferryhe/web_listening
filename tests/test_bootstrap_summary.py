from pathlib import Path

from web_listening.blocks.bootstrap_summary import render_markdown, summarize_monitor_scope_bootstrap
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, FileObservation, PageSnapshot, Site


def test_summarize_monitor_scope_bootstrap_groups_by_source_page_directories(tmp_path: Path):
    classification_path = tmp_path / "classification.yaml"
    classification_path.write_text(
        """
catalog: "dev"
sites:
  - site_key: "demo"
    display_name: "Demo"
    seed_url: "https://example.com/"
    homepage_url: "https://example.com/"
    fetch_mode: "http"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    selection_path = tmp_path / "selection.yaml"
    selection_path.write_text(
        """
site_key: "demo"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Keep research and publications."
selected_sections:
  - path: "/research"
    selection_reason: "Keep research."
  - path: "/publications"
    selection_reason: "Keep publications."
  - path: "/research/topics"
    selection_reason: "Keep research topics."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_yaml_text(plan), encoding="utf-8")

    storage = Storage(tmp_path / "summary.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/publications"],
                allowed_file_prefixes=["/"],
                max_depth=4,
                max_pages=2,
                max_files=3,
                fetch_mode="http",
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(
            CrawlRun(
                scope_id=scope.id,
                run_type="bootstrap",
                status="completed",
                pages_seen=2,
                files_seen=3,
            )
        )
        scope = storage.update_crawl_scope(
            CrawlScope(
                **{
                    **scope.model_dump(),
                    "is_initialized": True,
                    "baseline_run_id": run.id,
                }
            )
        )

        page_a = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/topics/page-a",
            depth=1,
            run_id=run.id,
        )
        snap_a = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=page_a.id,
                run_id=run.id,
                content_hash="hash-a",
                final_url="https://example.com/research/topics/page-a",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/topics/page-a",
            depth=1,
            run_id=run.id,
            latest_hash="hash-a",
            latest_snapshot_id=snap_a.id,
        )

        page_b = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/projects",
            depth=1,
            run_id=run.id,
        )
        snap_b = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=page_b.id,
                run_id=run.id,
                content_hash="hash-b",
                final_url="https://example.com/research/projects",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/projects",
            depth=1,
            run_id=run.id,
            latest_hash="hash-b",
            latest_snapshot_id=snap_b.id,
        )

        file_a = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/a.pdf",
            run_id=run.id,
            latest_document_id=1,
            latest_sha256="sha-a",
        )
        file_b = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/b.pdf",
            run_id=run.id,
            latest_document_id=2,
            latest_sha256="sha-b",
        )
        file_c = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/c.pdf",
            run_id=run.id,
            latest_document_id=3,
            latest_sha256="sha-c",
        )

        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page_a.id,
                file_id=file_a.id,
                discovered_url=file_a.canonical_url,
                download_url=file_a.canonical_url,
            )
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page_a.id,
                file_id=file_b.id,
                discovered_url=file_b.canonical_url,
                download_url=file_b.canonical_url,
            )
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page_b.id,
                file_id=file_c.id,
                discovered_url=file_c.canonical_url,
                download_url=file_c.canonical_url,
            )
        )

        summary = summarize_monitor_scope_bootstrap(scope_path, storage=storage)
        markdown = render_markdown(summary)
    finally:
        storage.close()

    level1 = {item.path: (item.pages, item.files) for item in summary.level1}
    level2 = {item.path: (item.pages, item.files) for item in summary.level2}

    assert summary.page_count == 2
    assert summary.file_count == 3
    assert level1["/research"] == (2, 3)
    assert level1["/publications"] == (0, 0)
    assert level2["/research/topics"] == (1, 2)
    assert level2["/research/projects"] == (1, 1)
    assert summary.top_source_pages[0].page_url == "https://example.com/research/topics/page-a"
    assert summary.top_source_pages[0].file_count == 2
    assert summary.coverage_page_count == 2
    assert summary.coverage_file_count == 3
    assert summary.truncated_by_budget is True
    assert any("page budget" in reason for reason in summary.truncation_reasons)
    assert any("file budget" in reason for reason in summary.truncation_reasons)
    assert summary.selected_but_low_coverage_prefixes == ["/publications"]
    assert summary.discovered_but_unselected_candidates == ["/research/projects"]
    assert summary.baseline_confidence == "low"
    assert any("/publications" in item for item in summary.recommended_followups)
    assert any("/research/projects" in item for item in summary.recommended_followups)
    assert "## Baseline Quality" in markdown
    assert "Baseline confidence" in markdown
    assert "Level-1 Coverage" in markdown
    assert "Top File Source Pages" in markdown


def test_summarize_monitor_scope_bootstrap_flags_page_only_discovered_candidates(tmp_path: Path):
    classification_path = tmp_path / "classification.yaml"
    classification_path.write_text(
        """
catalog: "dev"
sites:
  - site_key: "demo"
    display_name: "Demo"
    seed_url: "https://example.com/"
    homepage_url: "https://example.com/"
    fetch_mode: "http"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    selection_path = tmp_path / "selection.yaml"
    selection_path.write_text(
        """
site_key: "demo"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Keep research."
selected_sections:
  - path: "/research"
    selection_reason: "Keep research."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_yaml_text(plan), encoding="utf-8")

    storage = Storage(tmp_path / "summary-page-only.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research"],
                allowed_file_prefixes=["/"],
                max_depth=4,
                max_pages=20,
                max_files=20,
                fetch_mode="http",
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed", pages_seen=1, files_seen=0))
        scope = storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "is_initialized": True, "baseline_run_id": run.id}))

        page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/projects",
            depth=1,
            run_id=run.id,
        )
        snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=page.id,
                run_id=run.id,
                content_hash="hash-page-only",
                final_url="https://example.com/research/projects",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/projects",
            depth=1,
            run_id=run.id,
            latest_hash="hash-page-only",
            latest_snapshot_id=snapshot.id,
        )

        summary = summarize_monitor_scope_bootstrap(scope_path, storage=storage)
    finally:
        storage.close()

    assert "/research/projects" in summary.discovered_but_unselected_candidates
