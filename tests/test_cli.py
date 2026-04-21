import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from web_listening.cli import app
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text as render_scope_yaml_text
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import CrawlRun, CrawlScope, Document, FileObservation, Job, PageSnapshot, Site


runner = CliRunner()


def test_cli_help_registers_staged_workflow_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command_name in [
        "discover",
        "classify",
        "select",
        "plan-scope",
        "bootstrap-scope",
        "run-scope",
        "report-scope",
        "export-manifest",
        "list-jobs",
        "get-job",
        "create-monitor-task",
        "export-tracking-report",
    ]:
        assert command_name in result.output


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


def test_discover_command_reports_saved_paths(tmp_path: Path, monkeypatch):
    yaml_path = tmp_path / "plans" / "inventory.yaml"
    report_path = tmp_path / "reports" / "inventory.md"

    def fake_discover_sections(**kwargs):
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text("catalog: dev\n", encoding="utf-8")
        report_path.write_text("# Inventory\n", encoding="utf-8")
        return SimpleNamespace(yaml_path=yaml_path, report_path=report_path)

    monkeypatch.setattr("web_listening.blocks.staged_workflow.discover_sections", fake_discover_sections)

    result = runner.invoke(
        app,
        [
            "discover",
            "--catalog",
            "dev",
            "--yaml-path",
            str(yaml_path),
            "--report-path",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    normalized_output = result.output.replace("\n", "")
    assert "Saved section inventory" in result.output
    assert "YAML:" in result.output
    assert str(yaml_path).replace("\n", "") in normalized_output
    assert str(report_path).replace("\n", "") in normalized_output


def test_classify_command_reports_saved_paths(tmp_path: Path, monkeypatch):
    inventory_path = tmp_path / "plans" / "inventory.yaml"
    yaml_path = tmp_path / "plans" / "classification.yaml"
    report_path = tmp_path / "reports" / "classification.md"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text("catalog: dev\n", encoding="utf-8")

    def fake_classify_sections(**kwargs):
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text("catalog: dev\n", encoding="utf-8")
        report_path.write_text("# Classification\n", encoding="utf-8")
        return SimpleNamespace(inventory_path=inventory_path, yaml_path=yaml_path, report_path=report_path)

    monkeypatch.setattr("web_listening.blocks.staged_workflow.classify_sections", fake_classify_sections)

    result = runner.invoke(
        app,
        [
            "classify",
            "--catalog",
            "dev",
            "--inventory-path",
            str(inventory_path),
            "--yaml-path",
            str(yaml_path),
            "--report-path",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    normalized_output = result.output.replace("\n", "")
    assert "Saved section classification" in result.output
    assert "Inventory:" in result.output
    assert str(inventory_path).replace("\n", "") in normalized_output
    assert str(yaml_path).replace("\n", "") in normalized_output
    assert str(report_path).replace("\n", "") in normalized_output


def test_select_command_exposes_selection_artifact_path(tmp_path: Path):
    selection_path = tmp_path / "section_selection_demo.yaml"
    selection_path.write_text(
        """
site_key: "demo"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Track research."
selected_sections:
  - path: "/research"
    selection_reason: "Keep research."
deferred_sections:
  - path: "/news"
    selection_reason: "Review later."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["select", "--selection-path", str(selection_path)])

    assert result.exit_code == 0
    normalized_output = result.output.replace("\n", "")
    assert "Selection artifact ready" in result.output
    assert str(selection_path).replace("\n", "") in normalized_output
    assert "site_key=demo" in result.output
    assert "selected=1" in result.output


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

    json_result = runner.invoke(
        app,
        [
            "create-monitor-task",
            "--task-name",
            "demo-watch-json",
            "--site-url",
            "https://example.com/",
            "--task-description",
            "Track research updates.",
            "--goal",
            "Find new pages and files.",
            "--json",
        ],
    )

    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.output)
    assert json_payload["contract_version"] == "job_delivery.v1"
    assert json_payload["job"]["job_type"] == "monitor_task.create"
    assert json_payload["artifact_contract"]["primary_kind"] == "monitor_task"
    assert json_payload["artifact_contract"]["primary_key"] == "task_path"


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


def test_create_monitor_task_rejects_invalid_site_url(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    result = runner.invoke(
        app,
        [
            "create-monitor-task",
            "--task-name",
            "demo-watch",
            "--site-url",
            "notaurl",
            "--task-description",
            "Track research updates.",
            "--goal",
            "Find new pages and files.",
        ],
    )

    assert result.exit_code != 0
    assert "site_url must be a valid http or https URL" in result.output


def test_list_jobs_and_get_job_commands_render_persisted_job(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "cli-jobs.db")

    storage = Storage(settings.db_path)
    try:
        job = storage.add_job(
            Job(
                job_type="scope.report",
                status="completed",
                stage="completed",
                stage_message="Tracking report ready.",
                progress=100,
                scope_id=7,
                run_id=11,
                produced_artifacts={"output_path": str(tmp_path / "report.md")},
                artifact_summary={"artifact_count": 1, "artifact_keys": ["output_path"]},
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
        )
    finally:
        storage.close()

    list_result = runner.invoke(app, ["list-jobs", "--scope-id", "7"])
    assert list_result.exit_code == 0
    assert "scope.report" in list_result.output
    assert "completed" in list_result.output
    assert str(job.job_id) in list_result.output

    get_result = runner.invoke(app, ["get-job", str(job.job_id)])
    assert get_result.exit_code == 0
    assert f"job_id={job.job_id}" in get_result.output
    assert "stage=completed" in get_result.output
    assert "next_action=read_job_artifacts" in get_result.output
    assert "output_path" in get_result.output

    json_result = runner.invoke(app, ["get-job", str(job.job_id), "--json"])
    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.output)
    assert json_payload["contract_version"] == "job_delivery.v1"
    assert json_payload["job"]["job_id"] == job.job_id
    assert json_payload["artifact_contract"]["contract_version"] == "artifact_contract.v1"
    assert json_payload["artifact_contract"]["primary_kind"] == "tracking_report"
    assert json_payload["artifact_contract"]["primary_path"] == str(tmp_path / "report.md")
    assert json_payload["artifact_contract"]["path_map"]["output_path"] == str(tmp_path / "report.md")


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


def test_report_scope_rejects_invalid_format(tmp_path: Path):
    result = runner.invoke(
        app,
        [
            "report-scope",
            "--scope-path",
            str(tmp_path / "missing-scope.yaml"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code != 0
    assert "--format must be one of: md, yaml" in result.output


def test_report_scope_command_supports_json_delivery_output(tmp_path: Path, monkeypatch):
    report_path = tmp_path / "reports" / "tracking_report_demo.md"
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(
        """
scope_fingerprint: demo
site_key: demo
display_name: Demo
catalog: dev
generated_at: 2026-04-14T00:00:00+00:00
selection_review_status: approved
selection_mode: manual
business_goal: Track research.
seed_url: https://example.com/
homepage_url: https://example.com/
fetch_mode: http
fetch_config_json: {}
tree_strategy: selected_scope
tree_budget_profile: selected_scope_default
file_scope_mode: site_root
allowed_page_prefixes:
  - /research
allowed_file_prefixes:
  - /
scope_id: 12
selected_focus_prefixes:
  - /research
excluded_page_prefixes: []
deferred_page_prefixes: []
excluded_categories: []
max_depth: 3
max_pages: 25
max_files: 10
based_on: {}
selection_summary: {}
notes: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def fake_report_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Scope Report\n", encoding="utf-8")
        return SimpleNamespace(
            output_path=report_path,
            output_format="md",
            report=SimpleNamespace(run_id=34),
        )

    monkeypatch.setattr("web_listening.blocks.staged_workflow.report_scope", fake_report_scope)

    json_result = runner.invoke(
        app,
        [
            "report-scope",
            "--scope-path",
            str(scope_path),
            "--output",
            str(report_path),
            "--json",
        ],
    )

    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.output)
    assert json_payload["contract_version"] == "job_delivery.v1"
    assert json_payload["job"]["job_type"] == "scope.report"
    assert json_payload["artifact_contract"]["primary_kind"] == "tracking_report"
    assert json_payload["artifact_contract"]["primary_path"] == str(report_path)


def test_bootstrap_scope_command_reports_saved_paths(tmp_path: Path, monkeypatch):
    report_path = tmp_path / "reports" / "bootstrap.md"
    summary_path = tmp_path / "reports" / "bootstrap-summary.md"
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(
        """
scope_fingerprint: demo
site_key: demo
display_name: Demo
catalog: dev
generated_at: 2026-04-14T00:00:00+00:00
selection_review_status: approved
selection_mode: manual
business_goal: Track research.
seed_url: https://example.com/
homepage_url: https://example.com/
fetch_mode: http
fetch_config_json: {}
tree_strategy: selected_scope
tree_budget_profile: selected_scope_default
file_scope_mode: site_root
allowed_page_prefixes:
  - /research
allowed_file_prefixes:
  - /
scope_id: 7
selected_focus_prefixes:
  - /research
excluded_page_prefixes: []
deferred_page_prefixes: []
excluded_categories: []
max_depth: 3
max_pages: 25
max_files: 10
based_on: {}
selection_summary: {}
notes: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def fake_bootstrap_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Bootstrap\n", encoding="utf-8")
        summary_path.write_text("# Summary\n", encoding="utf-8")
        return SimpleNamespace(
            report_path=report_path,
            summary_path=summary_path,
            results=[SimpleNamespace(status="completed", scope_id=7, run_id=11)],
        )

    monkeypatch.setattr("web_listening.blocks.staged_workflow.bootstrap_scope", fake_bootstrap_scope)

    result = runner.invoke(
        app,
        [
            "bootstrap-scope",
            "--scope-path",
            str(scope_path),
            "--report-path",
            str(report_path),
            "--summary-path",
            str(summary_path),
            "--include-summary",
        ],
    )

    assert result.exit_code == 0
    normalized_output = result.output.replace("\n", "")
    assert "Bootstrap scope finished" in result.output
    assert str(report_path).replace("\n", "") in normalized_output
    assert str(summary_path).replace("\n", "") in normalized_output
    assert "scope_id=7" in result.output
    assert "run_id=11" in result.output

    json_result = runner.invoke(
        app,
        [
            "bootstrap-scope",
            "--scope-path",
            str(scope_path),
            "--report-path",
            str(report_path),
            "--summary-path",
            str(summary_path),
            "--include-summary",
            "--json",
        ],
    )

    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.output)
    assert json_payload["contract_version"] == "job_delivery.v1"
    assert json_payload["job"]["job_type"] == "scope.bootstrap"
    assert json_payload["artifact_contract"]["primary_path"] == str(report_path)
    assert json_payload["artifact_contract"]["path_map"]["summary_path"] == str(summary_path)



def test_run_scope_command_reports_saved_paths(tmp_path: Path, monkeypatch):
    report_path = tmp_path / "reports" / "run.md"
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(
        """
scope_fingerprint: demo
site_key: demo
display_name: Demo
catalog: dev
generated_at: 2026-04-14T00:00:00+00:00
selection_review_status: approved
selection_mode: manual
business_goal: Track research.
seed_url: https://example.com/
homepage_url: https://example.com/
fetch_mode: http
fetch_config_json: {}
tree_strategy: selected_scope
tree_budget_profile: selected_scope_default
file_scope_mode: site_root
allowed_page_prefixes:
  - /research
allowed_file_prefixes:
  - /
scope_id: 5
selected_focus_prefixes:
  - /research
excluded_page_prefixes: []
deferred_page_prefixes: []
excluded_categories: []
max_depth: 3
max_pages: 25
max_files: 10
based_on: {}
selection_summary: {}
notes: []
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def fake_run_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Run\n", encoding="utf-8")
        return SimpleNamespace(
            report_path=report_path,
            result=SimpleNamespace(status="completed", scope_id=5, run_id=9),
        )

    monkeypatch.setattr("web_listening.blocks.staged_workflow.run_scope", fake_run_scope)

    result = runner.invoke(
        app,
        [
            "run-scope",
            "--scope-path",
            str(scope_path),
            "--report-path",
            str(report_path),
        ],
    )

    assert result.exit_code == 0
    assert "Run scope finished" in result.output
    assert report_path.name in result.output
    assert "scope_id=5" in result.output
    assert "run_id=9" in result.output

    json_result = runner.invoke(
        app,
        [
            "run-scope",
            "--scope-path",
            str(scope_path),
            "--report-path",
            str(report_path),
            "--json",
        ],
    )

    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.output)
    assert json_payload["contract_version"] == "job_delivery.v1"
    assert json_payload["job"]["job_type"] == "scope.run"
    assert json_payload["artifact_contract"]["primary_path"] == str(report_path)
    assert json_payload["artifact_contract"]["path_map"]["report_path"] == str(report_path)



def test_export_manifest_writes_yaml_and_markdown_artifacts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", tmp_path / "manifest.db")

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
        run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed", pages_seen=1, files_seen=1))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))
        page = storage.upsert_tracked_page(scope_id=scope.id, canonical_url="https://example.com/research/page-a", depth=1, run_id=run.id)
        document = storage.add_document(
            Document(
                site_id=site.id,
                title="Research Report",
                url="https://example.com/files/report.pdf",
                download_url="https://example.com/files/report.pdf",
                page_url="https://example.com/research/page-a",
                downloaded_at=datetime(2026, 4, 8, 12, 2, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ab/report.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/report--abc12345.pdf",
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
                tracked_local_path=document.tracked_local_path,
            )
        )
    finally:
        storage.close()

    result = runner.invoke(
        app,
        [
            "export-manifest",
            "--scope-path",
            str(scope_path),
            "--run-id",
            str(run.id),
        ],
    )

    assert result.exit_code == 0
    assert "Saved scope manifest" in result.output
    written_yaml = list((tmp_path / "plans").glob("document_manifest_demo_*.yaml"))
    written_md = list((tmp_path / "reports").glob("document_manifest_demo_*.md"))
    assert len(written_yaml) == 1
    assert len(written_md) == 1
    assert "Research Report" in written_yaml[0].read_text(encoding="utf-8")
    assert "Scope Document Manifest" in written_md[0].read_text(encoding="utf-8")

    json_result = runner.invoke(
        app,
        [
            "export-manifest",
            "--scope-path",
            str(scope_path),
            "--run-id",
            str(run.id),
            "--json",
        ],
    )

    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.output)
    assert json_payload["contract_version"] == "job_delivery.v1"
    assert json_payload["job"]["job_type"] == "scope.manifest"
    assert json_payload["artifact_contract"]["primary_kind"] == "yaml_artifact"
    assert json_payload["artifact_contract"]["path_map"]["yaml_path"].endswith(".yaml")
