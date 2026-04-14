from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from web_listening.cli import app
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text as render_scope_yaml_text
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import CrawlRun, CrawlScope, Document, FileObservation, PageSnapshot, Site


runner = CliRunner()


def test_add_site_rejects_invalid_fetch_config_json():
    result = runner.invoke(
        app,
        [
            "add-site",
            "https://example.com",
            "--fetch-config",
            '{"broken":',
        ],
    )

    assert result.exit_code != 0
    assert "Invalid JSON for --fetch-config" in result.output


def test_create_monitor_task_writes_yaml_artifact(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    result = runner.invoke(
        app,
        [
            "create-monitor-task",
            "--task-name",
            "demo-watch",
            "--site-url",
            "https://example.com/",
            "--task-description",
            "Track research updates.",
            "--goal",
            "Find new pages and files.",
            "--focus-topics",
            "research,reports",
            "--must-track-prefixes",
            "/research,/publications",
            "--prefer-file-types",
            "pdf,docx",
        ],
    )

    assert result.exit_code == 0
    assert "Saved monitor task:" in result.output
    output_path = tmp_path / "plans"
    written = list(output_path.glob("monitor_task_demo-watch_*.yaml"))
    assert len(written) == 1
    assert "task_name: demo-watch" in written[0].read_text(encoding="utf-8")


def test_create_monitor_task_honors_explicit_output_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    explicit_output = tmp_path / "custom" / "monitor-task.yaml"

    result = runner.invoke(
        app,
        [
            "create-monitor-task",
            "--task-name",
            "demo-watch",
            "--site-url",
            "https://example.com/",
            "--task-description",
            "Track research updates.",
            "--goal",
            "Find new pages and files.",
            "--output",
            str(explicit_output),
        ],
    )

    assert result.exit_code == 0
    assert explicit_output.exists()
    assert "task_name: demo-watch" in explicit_output.read_text(encoding="utf-8")


def test_export_tracking_report_writes_markdown_artifact(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "cli-tracking.db")

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

    task_path = tmp_path / "monitor_task.yaml"
    task_path.write_text(
        """
task_name: demo-watch
site_url: https://example.com/
task_description: Track research updates.
goal: Find changed research pages.
focus_topics:
  - research
report_style: briefing
change_severity_rules:
  new_page: medium
  changed_page: medium
  missing_page: medium
  new_file: high
  changed_file: medium
  missing_file: medium
handoff_requirements: []
notes: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    storage = Storage(settings.db_path)
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
            CrawlRun(
                scope_id=scope.id,
                run_type="incremental",
                status="completed",
                started_at=datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc),
                finished_at=datetime(2026, 4, 8, 12, 3, 0, tzinfo=timezone.utc),
                pages_seen=2,
                files_seen=1,
                pages_changed=1,
                files_changed=1,
            )
        )
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))
        page = storage.upsert_tracked_page(scope_id=scope.id, canonical_url="https://example.com/research/page-a", depth=1, run_id=run.id)
        snapshot = storage.add_page_snapshot(PageSnapshot(scope_id=scope.id, page_id=page.id, run_id=run.id, content_hash="hash-a", final_url="https://example.com/research/page-a"))
        storage.upsert_tracked_page(scope_id=scope.id, canonical_url="https://example.com/research/page-a", depth=1, run_id=run.id, latest_hash="hash-a", latest_snapshot_id=snapshot.id)
        document = storage.add_document(
            Document(
                site_id=site.id,
                title="Research Report",
                url="https://example.com/files/report.pdf",
                download_url="https://example.com/files/report.pdf",
                page_url="https://example.com/research/page-a",
                downloaded_at=datetime(2026, 4, 8, 12, 2, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ab/report.pdf",
                sha256="abc123",
                doc_type="pdf",
            )
        )
        tracked_file = storage.upsert_tracked_file(scope_id=scope.id, canonical_url="https://example.com/files/report.pdf", run_id=run.id, latest_document_id=document.id, latest_sha256=document.sha256)
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page.id,
                file_id=tracked_file.id,
                document_id=document.id,
                discovered_url=tracked_file.canonical_url,
                download_url=tracked_file.canonical_url,
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/report--abc12345.pdf",
            )
        )
    finally:
        storage.close()

    result = runner.invoke(
        app,
        [
            "export-tracking-report",
            "--scope-path",
            str(scope_path),
            "--task-path",
            str(task_path),
            "--run-id",
            str(run.id),
        ],
    )

    assert result.exit_code == 0
    assert "Saved tracking report:" in result.output
    written = list((tmp_path / "reports").glob("tracking_report_demo_*.md"))
    assert len(written) == 1
    markdown = written[0].read_text(encoding="utf-8")
    assert "# Tracking Report" in markdown
    assert "demo-watch" in markdown
    assert "Research Report" in markdown


def test_export_tracking_report_honors_explicit_output_path(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "explicit-report.db")

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

    storage = Storage(settings.db_path)
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
        run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed", pages_seen=1))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))
    finally:
        storage.close()

    explicit_output = tmp_path / "custom" / "tracking-report.yaml"
    result = runner.invoke(
        app,
        [
            "export-tracking-report",
            "--scope-path",
            str(scope_path),
            "--run-id",
            str(run.id),
            "--format",
            "yaml",
            "--output",
            str(explicit_output),
        ],
    )

    assert result.exit_code == 0
    assert explicit_output.exists()
    assert "site_key: demo" in explicit_output.read_text(encoding="utf-8")
