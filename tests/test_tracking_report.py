from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text as render_scope_yaml_text
from web_listening.blocks.monitor_task import build_monitor_task, render_yaml_text as render_task_yaml_text
from web_listening.blocks.storage import Storage
from web_listening.blocks.tracking_report import build_tracking_report, render_markdown, render_yaml_text
from web_listening.models import CrawlRun, CrawlScope, Document, FileObservation, PageSnapshot, Site


def test_build_tracking_report_includes_task_run_and_document_context(tmp_path: Path):
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
business_goal: "Keep research and publication updates."
selected_sections:
  - path: "/research"
    selection_reason: "Keep research."
""".strip()
        + "\n",
        encoding="utf-8",
    )
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    monitor_task = build_monitor_task(
        task_name="demo-research-watch",
        site_url="https://example.com/",
        task_description="Track research page and file changes.",
        goal="Find new or changed research pages and downloaded reports.",
        focus_topics=["research", "reports"],
        must_track_prefixes=["/research"],
        prefer_file_types=["pdf"],
        must_download_patterns=["report"],
        notes=["Escalate new files quickly."],
    )
    task_path = tmp_path / "monitor_task.yaml"
    task_path.write_text(render_task_yaml_text(monitor_task), encoding="utf-8")

    storage = Storage(tmp_path / "tracking.db")
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
        bootstrap_run = storage.add_crawl_run(
            CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed")
        )
        scope = storage.update_crawl_scope(
            CrawlScope(
                **{
                    **scope.model_dump(),
                    "is_initialized": True,
                    "baseline_run_id": bootstrap_run.id,
                }
            )
        )
        rerun = storage.add_crawl_run(
            CrawlRun(
                scope_id=scope.id,
                run_type="incremental",
                status="completed",
                started_at=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 4, 8, 12, 5, 0, tzinfo=timezone.utc),
                pages_seen=3,
                files_seen=2,
                pages_changed=1,
                files_changed=1,
            )
        )

        page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=rerun.id,
        )
        snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=page.id,
                run_id=rerun.id,
                content_hash="hash-a",
                final_url="https://example.com/research/page-a",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=rerun.id,
            latest_hash="hash-a",
            latest_snapshot_id=snapshot.id,
        )

        document = storage.add_document(
            Document(
                site_id=site.id,
                title="Research Report",
                url="https://example.com/files/report.pdf",
                download_url="https://example.com/files/report.pdf",
                institution="Demo",
                page_url="https://example.com/research/page-a",
                downloaded_at=datetime(2026, 4, 8, 12, 4, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ab/report.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/report--abc12345.pdf",
                doc_type="pdf",
                sha256="abc123",
                content_type="application/pdf",
            )
        )
        tracked_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/report.pdf",
            run_id=rerun.id,
            latest_document_id=document.id,
            latest_sha256=document.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=rerun.id,
                page_id=page.id,
                file_id=tracked_file.id,
                document_id=document.id,
                discovered_url=tracked_file.canonical_url,
                download_url=tracked_file.canonical_url,
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/report--abc12345.pdf",
            )
        )

        report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
        markdown = render_markdown(report)
        yaml_text = render_yaml_text(report)
    finally:
        storage.close()

    assert report.task_name == "demo-research-watch"
    assert report.goal == "Find new or changed research pages and downloaded reports."
    assert report.run_id == rerun.id
    assert report.run_type == "incremental"
    assert report.pages_seen == 3
    assert report.files_seen == 2
    assert report.pages_changed == 1
    assert report.files_changed == 1
    assert report.document_count == 1
    assert report.documents[0]["title"] == "Research Report"
    assert report.documents[0]["preferred_display_path"].endswith("report--abc12345.pdf")
    assert report.recommended_next_actions
    assert "demo-research-watch" in markdown
    assert "Recommended Next Actions" in markdown
    assert "Research Report" in markdown
    assert "task_name: demo-research-watch" in yaml_text


def test_build_tracking_report_without_task_uses_scope_context_only(tmp_path: Path):
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
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-no-task.db")
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
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(
            CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed", pages_seen=1, files_seen=0)
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

        report = build_tracking_report(scope_path, storage=storage, run_id=run.id)
        markdown = render_markdown(report)
    finally:
        storage.close()

    assert report.task_name == ""
    assert report.goal == "Keep research."
    assert report.document_count == 0
    assert "Task Context" not in markdown
    assert "Scope Context" in markdown
