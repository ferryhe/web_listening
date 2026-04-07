from pathlib import Path
from datetime import datetime, timezone

from web_listening.blocks.document_manifest import build_scope_document_manifest, render_markdown
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Document, FileObservation, PageSnapshot, Site


def test_document_preferred_display_path_prefers_tracked_path():
    document = Document(
        site_id=1,
        title="Demo",
        url="https://example.com/files/demo.pdf",
        download_url="https://example.com/files/demo.pdf",
        local_path="data/downloads/_blobs/ab/abcdef.pdf",
        tracked_local_path="data/downloads/_tracked/example/demo/demo--abcdef12.pdf",
    )

    assert document.preferred_display_path == "data/downloads/_tracked/example/demo/demo--abcdef12.pdf"


def test_document_preferred_display_path_falls_back_to_local_path():
    document = Document(
        site_id=1,
        title="Demo",
        url="https://example.com/files/demo.pdf",
        download_url="https://example.com/files/demo.pdf",
        local_path="data/downloads/_blobs/ab/abcdef.pdf",
    )

    assert document.preferred_display_path == "data/downloads/_blobs/ab/abcdef.pdf"


def test_build_scope_document_manifest_exports_tracked_and_canonical_paths(tmp_path: Path):
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

    storage = Storage(tmp_path / "manifest.db")
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
                max_pages=120,
                max_files=40,
                fetch_mode="http",
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        scope = storage.update_crawl_scope(
            CrawlScope(
                **{
                    **scope.model_dump(),
                    "is_initialized": True,
                    "baseline_run_id": run.id,
                }
            )
        )

        page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/topics/page-a",
            depth=1,
            run_id=run.id,
        )
        snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=page.id,
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
            latest_snapshot_id=snapshot.id,
        )

        document = storage.add_document(
            Document(
                site_id=site.id,
                title="Demo PDF",
                url="https://example.com/files/demo.pdf",
                download_url="https://example.com/files/demo.pdf",
                institution="Demo",
                page_url="https://example.com/research/topics/page-a",
                downloaded_at=datetime(2026, 4, 7, 13, 0, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ab/abcdef.pdf",
                doc_type="pdf",
                sha256="abcdef123456",
                content_type="application/pdf",
            )
        )
        tracked_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/demo.pdf",
            run_id=run.id,
            latest_document_id=document.id,
            latest_sha256=document.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page.id,
                file_id=tracked_file.id,
                document_id=document.id,
                discovered_url=tracked_file.canonical_url,
                download_url=tracked_file.canonical_url,
                tracked_local_path="data/downloads/_tracked/example.com/research/topics/page-a/demo--abcdef12.pdf",
            )
        )

        manifest = build_scope_document_manifest(scope_path, storage=storage)
        markdown = render_markdown(manifest)
    finally:
        storage.close()

    assert manifest.document_count == 1
    assert manifest.documents[0]["sha256"] == "abcdef123456"
    assert manifest.documents[0]["local_path"] == "data/downloads/_blobs/ab/abcdef.pdf"
    assert manifest.documents[0]["tracked_local_path"].endswith("demo--abcdef12.pdf")
    assert manifest.documents[0]["preferred_display_path"].endswith("demo--abcdef12.pdf")
    assert manifest.documents[0]["page_url"] == "https://example.com/research/topics/page-a"
    assert manifest.documents[0]["downloaded_at"] == "2026-04-07T13:00:00+00:00"
    assert "preferred_display_path" in markdown
    assert "Downloaded at" in markdown


def test_list_scope_documents_falls_back_to_tracked_file_latest_document_for_legacy_rows(tmp_path: Path):
    storage = Storage(tmp_path / "legacy-manifest.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
            )
        )
        run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=run.id,
        )
        document = storage.add_document(
            Document(
                site_id=site.id,
                title="Legacy PDF",
                url="https://example.com/files/legacy.pdf",
                download_url="https://example.com/files/legacy.pdf",
                local_path="data/downloads/_blobs/ab/legacy.pdf",
                sha256="legacy123",
                doc_type="pdf",
            )
        )
        tracked_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/legacy.pdf",
            run_id=run.id,
            latest_document_id=document.id,
            latest_sha256=document.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page.id,
                file_id=tracked_file.id,
                discovered_url=tracked_file.canonical_url,
                download_url=tracked_file.canonical_url,
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/legacy--legacy12.pdf",
            )
        )

        documents = storage.list_scope_documents(scope.id, run_id=run.id)
    finally:
        storage.close()

    assert len(documents) == 1
    assert documents[0].sha256 == "legacy123"
    assert documents[0].preferred_display_path.endswith("legacy--legacy12.pdf")
