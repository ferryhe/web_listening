from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text as render_scope_yaml_text
from web_listening.blocks.monitor_task import build_monitor_task, render_yaml_text as render_task_yaml_text
from web_listening.blocks.storage import Storage
from web_listening.blocks.tracking_report import build_tracking_report, render_markdown, render_yaml_text, set_report_output_path
from web_listening.models import CrawlRun, CrawlScope, Document, FileObservation, PageSnapshot, Site


def _write_scope_inputs(tmp_path: Path) -> tuple[Path, Path]:
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
  - path: "/news"
    selection_reason: "Keep updates."
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return classification_path, selection_path


def test_build_tracking_report_includes_change_bundles_priority_queue_artifacts_and_scope_identity(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)

    monitor_task = build_monitor_task(
        task_name="demo-research-watch",
        site_url="https://example.com/",
        task_description="Track research page and file changes.",
        goal="Find new, changed, and missing research pages plus report files.",
        focus_topics=["research", "reports"],
        must_track_prefixes=["/research"],
        prefer_file_types=["pdf"],
        must_download_patterns=["report"],
        severity_policy=[
            {
                "rule_type": "prefix",
                "match_value": "/research",
                "severity": "high",
                "reason_template": "Research sections changed and require review.",
                "recommended_action": "review_research_changes",
                "weight": 40,
            },
            {
                "rule_type": "file_type",
                "match_value": "pdf",
                "severity": "high",
                "reason_template": "PDF files should be reviewed promptly.",
                "recommended_action": "open_document_first",
                "weight": 60,
            },
            {
                "rule_type": "keyword",
                "match_value": "Quarterly",
                "severity": "critical",
                "reason_template": "Quarterly updates need immediate escalation.",
                "recommended_action": "escalate_immediately",
                "weight": 80,
            },
        ],
        alert_policy={"channels": ["email"]},
        human_review_rules=["new_file", "missing_page"],
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
                allowed_page_prefixes=["/research", "/news"],
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
                files_seen=3,
                pages_changed=3,
                files_changed=3,
            )
        )

        changed_page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=bootstrap_run.id,
        )
        storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=changed_page.id,
                run_id=bootstrap_run.id,
                content_hash="hash-old",
                final_url="https://example.com/research/page-a",
            )
        )
        changed_snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=changed_page.id,
                run_id=rerun.id,
                content_hash="hash-new",
                final_url="https://example.com/research/page-a",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=rerun.id,
            latest_hash="hash-new",
            latest_snapshot_id=changed_snapshot.id,
        )

        new_page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/news/new-page",
            depth=1,
            run_id=rerun.id,
        )
        new_snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=new_page.id,
                run_id=rerun.id,
                content_hash="hash-fresh",
                final_url="https://example.com/news/new-page",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/news/new-page",
            depth=1,
            run_id=rerun.id,
            latest_hash="hash-fresh",
            latest_snapshot_id=new_snapshot.id,
        )

        missing_page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/missing-page",
            depth=1,
            run_id=bootstrap_run.id,
        )
        storage.conn.execute(
            "UPDATE tracked_pages SET is_active = 0, miss_count = 1, last_seen_run_id = ? WHERE id = ?",
            (bootstrap_run.id, missing_page.id),
        )

        old_doc = storage.add_document(
            Document(
                site_id=site.id,
                title="Research Report v1",
                url="https://example.com/files/report.pdf?version=1",
                download_url="https://example.com/files/report.pdf?version=1",
                institution="Demo",
                page_url="https://example.com/research/page-a",
                downloaded_at=datetime(2026, 4, 7, 12, 4, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ab/report-v1.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/report-v1.pdf",
                doc_type="pdf",
                sha256="sha-old",
                content_type="application/pdf",
            )
        )
        changed_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/report.pdf",
            run_id=bootstrap_run.id,
            latest_document_id=old_doc.id,
            latest_sha256=old_doc.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=bootstrap_run.id,
                page_id=changed_page.id,
                file_id=changed_file.id,
                document_id=old_doc.id,
                discovered_url=changed_file.canonical_url,
                download_url=changed_file.canonical_url,
                tracked_local_path=old_doc.tracked_local_path,
            )
        )
        new_doc = storage.add_document(
            Document(
                site_id=site.id,
                title="Research Report v2",
                url="https://example.com/files/report.pdf?version=2",
                download_url="https://example.com/files/report.pdf?version=2",
                institution="Demo",
                page_url="https://example.com/research/page-a",
                downloaded_at=datetime(2026, 4, 8, 12, 4, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ab/report-v2.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/research/page-a/report-v2.pdf",
                doc_type="pdf",
                sha256="sha-new",
                content_type="application/pdf",
            )
        )
        changed_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/report.pdf",
            run_id=rerun.id,
            latest_document_id=new_doc.id,
            latest_sha256=new_doc.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=rerun.id,
                page_id=changed_page.id,
                file_id=changed_file.id,
                document_id=new_doc.id,
                discovered_url=changed_file.canonical_url,
                download_url=changed_file.canonical_url,
                tracked_local_path=new_doc.tracked_local_path,
            )
        )

        new_file_doc = storage.add_document(
            Document(
                site_id=site.id,
                title="Quarterly Update",
                url="https://example.com/files/update.pdf",
                download_url="https://example.com/files/update.pdf",
                institution="Demo",
                page_url="https://example.com/news/new-page",
                downloaded_at=datetime(2026, 4, 8, 12, 4, 30, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/cd/update.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/news/new-page/update.pdf",
                doc_type="pdf",
                sha256="sha-update",
                content_type="application/pdf",
            )
        )
        new_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/update.pdf",
            run_id=rerun.id,
            latest_document_id=new_file_doc.id,
            latest_sha256=new_file_doc.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=rerun.id,
                page_id=new_page.id,
                file_id=new_file.id,
                document_id=new_file_doc.id,
                discovered_url=new_file.canonical_url,
                download_url=new_file.canonical_url,
                tracked_local_path=new_file_doc.tracked_local_path,
            )
        )

        missing_doc = storage.add_document(
            Document(
                site_id=site.id,
                title="Missing File",
                url="https://example.com/files/missing.pdf",
                download_url="https://example.com/files/missing.pdf",
                institution="Demo",
                page_url="https://example.com/research/missing-page",
                downloaded_at=datetime(2026, 4, 7, 11, 0, 0, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/ef/missing.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/research/missing-page/missing.pdf",
                doc_type="pdf",
                sha256="sha-missing",
                content_type="application/pdf",
            )
        )
        missing_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/missing.pdf",
            run_id=bootstrap_run.id,
            latest_document_id=missing_doc.id,
            latest_sha256=missing_doc.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=bootstrap_run.id,
                page_id=missing_page.id,
                file_id=missing_file.id,
                document_id=missing_doc.id,
                discovered_url=missing_file.canonical_url,
                download_url=missing_file.canonical_url,
                tracked_local_path=missing_doc.tracked_local_path,
            )
        )
        storage.conn.execute(
            "UPDATE tracked_files SET is_active = 0, miss_count = 1, last_seen_run_id = ? WHERE id = ?",
            (bootstrap_run.id, missing_file.id),
        )
        storage.conn.commit()

        scope_plan.allowed_page_prefixes = ["/news", "/research"]
        scope_plan.scope_id = scope.id
        scope_path = tmp_path / "monitor_scope.yaml"
        scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

        report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
        markdown = render_markdown(report)
        yaml_text = render_yaml_text(report)
    finally:
        storage.close()

    assert report.scope_id == scope.id
    assert report.scope_fingerprint == scope_plan.scope_fingerprint
    assert [item["url"] for item in report.new_pages] == ["https://example.com/news/new-page"]
    assert [item["url"] for item in report.changed_pages] == ["https://example.com/research/page-a"]
    assert [item["url"] for item in report.missing_pages] == ["https://example.com/research/missing-page"]
    assert [item["url"] for item in report.new_files] == ["https://example.com/files/update.pdf"]
    assert [item["url"] for item in report.changed_files] == ["https://example.com/files/report.pdf"]
    assert [item["url"] for item in report.missing_files] == ["https://example.com/files/missing.pdf"]
    assert report.priority_summary["highest_priority"] == "critical"
    assert report.priority_summary["severity_counts"]["critical"] == 1
    assert report.priority_summary["severity_counts"]["high"] >= 1
    assert report.review_queue[0]["change_type"] == "new_file"
    assert report.review_queue[0]["severity"] == "critical"
    assert report.review_queue[0]["entity_type"] == "file"
    assert report.review_queue[0]["entity_url"] == "https://example.com/files/update.pdf"
    assert report.review_queue[0]["recommended_action"] == "escalate_immediately"
    assert report.review_queue[0]["matched_policy"]["rule_type"] == "keyword"
    assert report.review_queue[0]["preferred_display_path"].endswith("update.pdf")
    assert report.next_action == "escalate_review_queue"
    assert report.escalation_needed is True
    assert report.review_required_count == len(report.review_queue)
    assert report.high_priority_count >= 3
    assert any(item["plane"] == "control_plane" and item["kind"] == "monitor_scope" for item in report.artifact_index)
    assert any(item["plane"] == "control_plane" and item["kind"] == "monitor_task" for item in report.artifact_index)
    assert any(item["plane"] == "status_plane" and item["kind"] == "run_metadata" for item in report.artifact_index)
    assert any(item["plane"] == "explanation_plane" and item["kind"] == "tracking_report" for item in report.artifact_index)
    assert sum(1 for item in report.artifact_index if item["plane"] == "evidence_plane" and item["kind"] == "document") == 2
    assert all("recommended_reader" in item for item in report.artifact_index)
    assert "Scope identity" in markdown
    assert "Priority Summary" in markdown
    assert "Review Queue" in markdown
    assert "Artifact Index" in markdown
    assert "control_plane" in markdown
    assert "new_pages:" in yaml_text
    assert "review_required_count:" in yaml_text
    assert "high_priority_count:" in yaml_text
    assert "matched_policy:" in yaml_text
    assert "artifact_index:" in yaml_text


def test_build_tracking_report_without_task_uses_scope_context_only(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
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
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(
            CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed", pages_seen=1, files_seen=0)
        )
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))

        report = build_tracking_report(scope_path, storage=storage, run_id=run.id)
        markdown = render_markdown(report)
    finally:
        storage.close()

    assert report.task_name == ""
    assert report.goal == "Keep research and publication updates."
    assert report.priority_summary == {}
    assert report.review_queue == []
    assert report.next_action == "attach_monitor_task"
    assert report.escalation_needed is False
    assert report.review_required_count == 0
    assert report.high_priority_count == 0
    assert "Task Context" not in markdown
    assert "Scope Context" in markdown


def test_build_tracking_report_rejects_run_from_different_scope(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-mismatch.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope_a = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        scope_b = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/other",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/other"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        bad_run = storage.add_crawl_run(CrawlRun(scope_id=scope_b.id, run_type="incremental", status="completed", pages_seen=99))
        storage.update_crawl_scope(CrawlScope(**{**scope_a.model_dump(), "baseline_run_id": bad_run.id, "is_initialized": True}))

        try:
            build_tracking_report(scope_path, storage=storage, run_id=bad_run.id)
            raised = False
        except ValueError as exc:
            raised = True
            assert "belongs to scope" in str(exc)
    finally:
        storage.close()

    assert raised is True


def test_build_tracking_report_rejects_task_for_different_site(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    wrong_task = build_monitor_task(
        task_name="other-site-watch",
        site_url="https://another.example.com/",
        task_description="Track another site.",
        goal="Should not attach to the demo site.",
    )
    task_path = tmp_path / "monitor_task.yaml"
    task_path.write_text(render_task_yaml_text(wrong_task), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-task-mismatch.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))

        try:
            build_tracking_report(scope_path, storage=storage, run_id=run.id, task_path=task_path)
            raised = False
        except ValueError as exc:
            raised = True
            assert "does not match monitor scope seed_url" in str(exc)
    finally:
        storage.close()

    assert raised is True


def test_build_tracking_report_failed_run_sets_status_driven_next_action(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-failed-run.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        run = storage.add_crawl_run(
            CrawlRun(scope_id=scope.id, run_type="incremental", status="failed", error_message="network timeout")
        )
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))

        report = build_tracking_report(scope_path, storage=storage, run_id=run.id)
    finally:
        storage.close()

    assert report.run_status == "failed"
    assert report.next_action == "investigate_failed_run"
    assert report.escalation_needed is True
    assert report.review_required_count == 0
    assert report.high_priority_count == 0


def test_build_tracking_report_low_priority_only_sets_review_action_and_report_path(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)

    monitor_task = build_monitor_task(
        task_name="demo-low-priority-watch",
        site_url="https://example.com/",
        task_description="Track research changes with low-priority defaults.",
        goal="Review routine page updates.",
    )
    task_path = tmp_path / "monitor_task.yaml"
    task_path.write_text(render_task_yaml_text(monitor_task), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-low-priority.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        bootstrap_run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": bootstrap_run.id, "is_initialized": True}))
        rerun = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed", pages_seen=1))

        changed_page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=bootstrap_run.id,
        )
        storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=changed_page.id,
                run_id=bootstrap_run.id,
                content_hash="hash-old",
                final_url="https://example.com/research/page-a",
            )
        )
        changed_snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=changed_page.id,
                run_id=rerun.id,
                content_hash="hash-new",
                final_url="https://example.com/research/page-a",
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=rerun.id,
            latest_hash="hash-new",
            latest_snapshot_id=changed_snapshot.id,
        )

        scope_plan.allowed_page_prefixes = ["/news", "/research"]
        scope_plan.scope_id = scope.id
        scope_path = tmp_path / "monitor_scope.yaml"
        scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

        report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
        set_report_output_path(report, tmp_path / "reports" / "tracking_report_demo.md")
        yaml_report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
        set_report_output_path(yaml_report, tmp_path / "reports" / "tracking_report_demo.yml")
    finally:
        storage.close()

    assert report.next_action == "review_low_priority_changes"
    assert report.escalation_needed is False
    assert report.review_required_count == 1
    assert report.high_priority_count == 0
    report_artifact = next(item for item in report.artifact_index if item["kind"] == "tracking_report")
    assert report_artifact["path"].endswith("tracking_report_demo.md")
    assert report_artifact["recommended_reader"] == "markdown"
    yaml_report_artifact = next(item for item in yaml_report.artifact_index if item["kind"] == "tracking_report")
    assert yaml_report_artifact["path"].endswith("tracking_report_demo.yml")
    assert yaml_report_artifact["recommended_reader"] == "yaml"


def test_build_tracking_report_handles_bad_severity_policy_config_with_fallback(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)

    monitor_task = build_monitor_task(
        task_name="demo-bad-policy-watch",
        site_url="https://example.com/",
        task_description="Track research file changes.",
        goal="Keep bad severity policy config from breaking report generation.",
        severity_policy=[
            {
                "rule_type": "keyword",
                "match_value": "Quarterly",
                "severity": "critical",
                "weight": "not-a-number",
                "reason_template": "bad {missing_key}",
                "recommended_action": "escalate_immediately",
            }
        ],
        change_severity_rules={"new_file": "high"},
    )
    task_path = tmp_path / "monitor_task_bad_policy.yaml"
    task_path.write_text(render_task_yaml_text(monitor_task), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-bad-policy.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        bootstrap_run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": bootstrap_run.id, "is_initialized": True}))
        rerun = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed", files_seen=1))

        new_page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/news/new-page",
            depth=1,
            run_id=rerun.id,
        )
        storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=new_page.id,
                run_id=rerun.id,
                content_hash="hash-fresh",
                final_url="https://example.com/news/new-page",
            )
        )
        new_file_doc = storage.add_document(
            Document(
                site_id=site.id,
                title="Quarterly Update",
                url="https://example.com/files/update.pdf",
                download_url="https://example.com/files/update.pdf",
                institution="Demo",
                page_url="https://example.com/news/new-page",
                downloaded_at=datetime(2026, 4, 8, 12, 4, 30, tzinfo=timezone.utc),
                local_path="data/downloads/_blobs/cd/update.pdf",
                tracked_local_path="data/downloads/_tracked/example.com/news/new-page/update.pdf",
                doc_type="pdf",
                sha256="sha-update",
                content_type="application/pdf",
            )
        )
        new_file = storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url="https://example.com/files/update.pdf",
            run_id=rerun.id,
            latest_document_id=new_file_doc.id,
            latest_sha256=new_file_doc.sha256,
        )
        storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=rerun.id,
                page_id=new_page.id,
                file_id=new_file.id,
                document_id=new_file_doc.id,
                discovered_url=new_file.canonical_url,
                download_url=new_file.canonical_url,
                tracked_local_path=new_file_doc.tracked_local_path,
            )
        )
        storage.conn.commit()

        scope_plan.allowed_page_prefixes = ["/news", "/research"]
        scope_plan.scope_id = scope.id
        scope_path = tmp_path / "monitor_scope.yaml"
        scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

        report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
    finally:
        storage.close()

    assert report.review_queue[0]["severity"] == "critical"
    assert report.review_queue[0]["policy_weight"] == 0
    assert report.review_queue[0]["reason"] == "new_file matched severity policy `keyword` at `critical`."


def test_build_tracking_report_matches_change_type_rules_case_insensitively(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)

    monitor_task = build_monitor_task(
        task_name="demo-change-type-case-watch",
        site_url="https://example.com/",
        task_description="Track case-insensitive change-type rules.",
        goal="Ensure change_type structured policies are not fragile to case.",
        severity_policy=[
            {
                "rule_type": "change_type",
                "match_value": "Changed_Page",
                "severity": "critical",
                "recommended_action": "escalate_case_insensitive",
                "weight": 25,
            }
        ],
    )
    task_path = tmp_path / "monitor_task_case_policy.yaml"
    task_path.write_text(render_task_yaml_text(monitor_task), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-case-policy.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        bootstrap_run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": bootstrap_run.id, "is_initialized": True}))
        rerun = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed", pages_seen=1))

        changed_page = storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=bootstrap_run.id,
        )
        storage.add_page_snapshot(PageSnapshot(scope_id=scope.id, page_id=changed_page.id, run_id=bootstrap_run.id, content_hash="hash-old", final_url="https://example.com/research/page-a"))
        changed_snapshot = storage.add_page_snapshot(PageSnapshot(scope_id=scope.id, page_id=changed_page.id, run_id=rerun.id, content_hash="hash-new", final_url="https://example.com/research/page-a"))
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url="https://example.com/research/page-a",
            depth=1,
            run_id=rerun.id,
            latest_hash="hash-new",
            latest_snapshot_id=changed_snapshot.id,
        )

        scope_plan.allowed_page_prefixes = ["/news", "/research"]
        scope_plan.scope_id = scope.id
        scope_path = tmp_path / "monitor_scope.yaml"
        scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

        report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
    finally:
        storage.close()

    assert report.review_queue[0]["change_type"] == "changed_page"
    assert report.review_queue[0]["severity"] == "critical"
    assert report.review_queue[0]["recommended_action"] == "escalate_case_insensitive"


def test_build_tracking_report_sorts_same_severity_by_policy_weight_then_entity_url(tmp_path: Path):
    classification_path, selection_path = _write_scope_inputs(tmp_path)
    scope_plan = build_monitor_scope(selection_path, classification_path=classification_path)

    monitor_task = build_monitor_task(
        task_name="demo-weighted-policy-watch",
        site_url="https://example.com/",
        task_description="Track policy-sorted page changes.",
        goal="Sort same-severity queue entries by policy weight, then URL.",
        severity_policy=[
            {
                "rule_type": "prefix",
                "match_value": "/research/a",
                "severity": "high",
                "recommended_action": "review_a",
                "weight": 10,
            },
            {
                "rule_type": "prefix",
                "match_value": "/research/b",
                "severity": "high",
                "recommended_action": "review_b",
                "weight": 50,
            },
            {
                "rule_type": "change_type",
                "match_value": "changed_page",
                "severity": "high",
                "recommended_action": "review_generic",
                "weight": 10,
            },
        ],
    )
    task_path = tmp_path / "monitor_task_weighted.yaml"
    task_path.write_text(render_task_yaml_text(monitor_task), encoding="utf-8")

    storage = Storage(tmp_path / "tracking-weighted-policy.db")
    try:
        site = storage.add_site(Site(url="https://example.com/", name="Demo Tree"))
        scope = storage.add_crawl_scope(
            CrawlScope(
                site_id=site.id,
                seed_url="https://example.com/",
                allowed_origin="https://example.com",
                allowed_page_prefixes=["/research", "/news"],
                allowed_file_prefixes=["/"],
                fetch_mode="http",
                is_initialized=True,
            )
        )
        bootstrap_run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
        storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": bootstrap_run.id, "is_initialized": True}))
        rerun = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed", pages_seen=3))

        for slug in ("a-page", "b-page", "c-page"):
            page = storage.upsert_tracked_page(
                scope_id=scope.id,
                canonical_url=f"https://example.com/research/{slug}",
                depth=1,
                run_id=bootstrap_run.id,
            )
            storage.add_page_snapshot(
                PageSnapshot(
                    scope_id=scope.id,
                    page_id=page.id,
                    run_id=bootstrap_run.id,
                    content_hash=f"old-{slug}",
                    final_url=f"https://example.com/research/{slug}",
                )
            )
            changed_snapshot = storage.add_page_snapshot(
                PageSnapshot(
                    scope_id=scope.id,
                    page_id=page.id,
                    run_id=rerun.id,
                    content_hash=f"new-{slug}",
                    final_url=f"https://example.com/research/{slug}",
                )
            )
            storage.upsert_tracked_page(
                scope_id=scope.id,
                canonical_url=f"https://example.com/research/{slug}",
                depth=1,
                run_id=rerun.id,
                latest_hash=f"new-{slug}",
                latest_snapshot_id=changed_snapshot.id,
            )

        scope_plan.allowed_page_prefixes = ["/news", "/research"]
        scope_plan.scope_id = scope.id
        scope_path = tmp_path / "monitor_scope.yaml"
        scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

        report = build_tracking_report(scope_path, storage=storage, run_id=rerun.id, task_path=task_path)
    finally:
        storage.close()

    changed_page_items = [item for item in report.review_queue if item["change_type"] == "changed_page"]
    assert [item["entity_url"] for item in changed_page_items] == [
        "https://example.com/research/b-page",
        "https://example.com/research/a-page",
        "https://example.com/research/c-page",
    ]
