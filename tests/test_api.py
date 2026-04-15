from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from web_listening.api import routes
from web_listening.api.app import create_app
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, render_yaml_text as render_scope_yaml_text
from web_listening.blocks.rescue import RescueAttempt, RescueResult
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Document, Site, SiteSnapshot


def test_get_latest_snapshot_endpoint(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com", name="Example"))
    storage.add_snapshot(
        SiteSnapshot(
            site_id=site.id,
            captured_at=datetime.now(timezone.utc),
            content_hash="hash123",
            raw_html="<html><body><h1>Example</h1></body></html>",
            cleaned_html="<body><h1>Example</h1></body>",
            content_text="Example",
            markdown="# Example",
            fit_markdown="# Example",
            metadata_json={"word_count": 1},
            fetch_mode="http",
            final_url="https://example.com",
            status_code=200,
            links=["https://example.com/doc.pdf"],
        )
    )
    storage.close()

    client = TestClient(create_app())
    response = client.get(f"/api/v1/sites/{site.id}/snapshots/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_id"] == site.id
    assert payload["markdown"] == "# Example"
    assert payload["fit_markdown"] == "# Example"
    assert payload["metadata_json"]["word_count"] == 1
    assert payload["final_url"] == "https://example.com"


def test_get_latest_snapshot_endpoint_returns_404_without_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com", name="Example"))
    storage.close()

    client = TestClient(create_app())
    response = client.get(f"/api/v1/sites/{site.id}/snapshots/latest")

    assert response.status_code == 404
    assert response.json()["detail"] == "Snapshot not found"


def test_update_document_content_endpoint(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com", name="Example"))
    document = storage.add_document(
        Document(
            site_id=site.id,
            title="Report",
            url="https://example.com/report.pdf",
            download_url="https://example.com/report.pdf",
            institution="ExampleOrg",
            doc_type="pdf",
        )
    )
    storage.close()

    client = TestClient(create_app())
    response = client.patch(
        f"/api/v1/documents/{document.id}/content",
        json={
            "content_md": "# Report\n\nConverted content",
            "content_md_status": "converted",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == document.id
    assert payload["content_md"].startswith("# Report")
    assert payload["content_md_status"] == "converted"
    assert payload["content_md_updated_at"] is not None


def test_add_site_with_fetch_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/sites",
        json={
            "url": "https://example.com",
            "name": "Example",
            "tags": ["news"],
            "fetch_mode": "browser",
            "fetch_config_json": {"wait_for": "#main"},
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["fetch_mode"] == "browser"
    assert payload["fetch_config_json"]["wait_for"] == "#main"


def test_rescue_check_endpoint_returns_winning_snapshot(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com", name="Example"))
    storage.close()

    snapshot = SiteSnapshot(
        site_id=site.id,
        captured_at=datetime.now(timezone.utc),
        content_hash="hash123",
        raw_html="<html><body><main><h1>Recovered</h1></main></body></html>",
        cleaned_html="<main><h1>Recovered</h1></main>",
        content_text="Recovered",
        markdown="# Recovered",
        fit_markdown="# Recovered",
        metadata_json={"word_count": 1, "source_kind": "html"},
        fetch_mode="browser",
        final_url="https://example.com/recovered",
        status_code=200,
        links=["https://example.com/report.pdf"],
    )

    def fake_run_site_rescue(*args, **kwargs):
        return RescueResult(
            label="Example",
            primary_strategy="catalog",
            resolved_strategy="browser",
            resolved=True,
            attempts=[
                RescueAttempt(
                    strategy="catalog",
                    url="https://example.com",
                    fetch_mode="http",
                    status_code=403,
                    final_url="https://example.com",
                    request_user_agent="web-listening-bot/1.0",
                    word_count=0,
                    link_count=0,
                    source_kind="html",
                    passed=False,
                    reason="http_403",
                    head="",
                    error="HTTPStatusError: 403",
                ),
                RescueAttempt(
                    strategy="browser",
                    url="https://example.com",
                    fetch_mode="browser",
                    status_code=200,
                    final_url="https://example.com/recovered",
                    request_user_agent="Mozilla/5.0",
                    word_count=120,
                    link_count=5,
                    source_kind="html",
                    passed=True,
                    reason="content_ok",
                    head="# Recovered",
                    snapshot=snapshot,
                ),
            ],
        )

    monkeypatch.setattr(routes, "run_site_rescue", fake_run_site_rescue)

    client = TestClient(create_app())
    response = client.post(
        f"/api/v1/sites/{site.id}/rescue-check",
        json={"allow_browser": True, "allow_official_feeds": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["site_id"] == site.id
    assert payload["resolved"] is True
    assert payload["resolved_strategy"] == "browser"
    assert len(payload["attempts"]) == 2
    assert payload["winning_snapshot"]["final_url"] == "https://example.com/recovered"
    assert payload["winning_snapshot"]["fetch_mode"] == "browser"


def test_rescue_check_endpoint_validates_optional_urls(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com", name="Example"))
    storage.close()

    client = TestClient(create_app())
    response = client.post(
        f"/api/v1/sites/{site.id}/rescue-check",
        json={"sitemap_url": "ftp://example.com/sitemap.xml"},
    )

    assert response.status_code == 422
    assert "only http and https are accepted" in response.json()["detail"]


def test_create_monitor_task_endpoint_persists_completed_job(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/monitor-tasks",
        json={
            "task_name": "demo-watch",
            "site_url": "https://example.com/",
            "task_description": "Track research updates.",
            "goal": "Find new pages and files.",
            "focus_topics": ["research"],
            "severity_policy": [
                {
                    "rule_type": "prefix",
                    "match_value": "/research",
                    "severity": "high",
                    "recommended_action": "review_research_change",
                    "weight": 40,
                }
            ],
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["job_type"] == "monitor_task.create"
    assert payload["status"] == "completed"
    task_path = Path(payload["produced_artifacts"]["task_path"])
    assert task_path.exists()
    task_text = task_path.read_text(encoding="utf-8")
    assert "task_name: demo-watch" in task_text
    assert "severity_policy:" in task_text
    assert "recommended_action: review_research_change" in task_text

    job_response = client.get(f"/api/v1/jobs/{payload['job_id']}")
    assert job_response.status_code == 200
    assert job_response.json()["job_id"] == payload["job_id"]


def test_create_monitor_task_endpoint_keeps_default_structured_policy_when_policy_fields_omitted(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/monitor-tasks",
        json={
            "task_name": "default-policy-watch",
            "site_url": "https://example.com/",
            "task_description": "Track default policy behavior.",
            "goal": "Keep default structured severity policy for API-created tasks.",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    task_path = Path(payload["produced_artifacts"]["task_path"])
    task_text = task_path.read_text(encoding="utf-8")
    assert "severity_policy:" in task_text
    assert "rule_type: change_type" in task_text
    assert "match_value: new_file" in task_text
    assert "severity: high" in task_text


def test_create_monitor_task_endpoint_rejects_output_path_outside_data_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/monitor-tasks",
        json={
            "task_name": "demo-watch",
            "site_url": "https://example.com/",
            "task_description": "Track research updates.",
            "goal": "Find new pages and files.",
            "output_path": "/tmp/outside-task.yaml",
        },
    )

    assert response.status_code == 422
    assert "must stay under" in response.json()["detail"]



def test_scope_bootstrap_job_endpoint_persists_completed_job(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/research"],
            allowed_file_prefixes=["/"],
        )
    )
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
    storage.close()

    scope_path = tmp_path / "plans" / "monitor_scope_demo.yaml"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(
        f"scope_fingerprint: demo\nsite_key: demo\ndisplay_name: Example\ncatalog: dev\ngenerated_at: 2026-04-14T00:00:00+00:00\nselection_review_status: approved\nselection_mode: manual\nbusiness_goal: Track research.\nseed_url: https://example.com/\nhomepage_url: https://example.com/\nfetch_mode: http\nfetch_config_json: {{}}\ntree_strategy: selected_scope\ntree_budget_profile: selected_scope_default\nfile_scope_mode: site_root\nallowed_page_prefixes:\n  - /research\nallowed_file_prefixes:\n  - /\nscope_id: {scope.id}\nselected_focus_prefixes:\n  - /research\nexcluded_page_prefixes: []\ndeferred_page_prefixes: []\nexcluded_categories: []\nmax_depth: 3\nmax_pages: 25\nmax_files: 10\nbased_on: {{}}\nselection_summary: {{}}\nnotes: []\n",
        encoding="utf-8",
    )

    report_path = tmp_path / "reports" / "bootstrap.md"

    def fake_bootstrap_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Bootstrap\n", encoding="utf-8")
        return SimpleNamespace(results=[SimpleNamespace(scope_id=scope.id, run_id=run.id)], report_path=report_path, summary_path=None)

    monkeypatch.setattr("web_listening.blocks.staged_workflow.bootstrap_scope", fake_bootstrap_scope)

    client = TestClient(create_app())
    response = client.post(f"/api/v1/monitor-scopes/{scope.id}/bootstrap", json={})

    assert response.status_code == 201
    payload = response.json()
    assert payload["job_type"] == "scope.bootstrap"
    assert payload["status"] == "completed"
    assert payload["stage"] == "completed"
    assert payload["progress"] == 100
    assert payload["artifact_summary"]["artifact_count"] == 2
    assert payload["run_id"] == run.id
    assert payload["produced_artifacts"]["report_path"] == str(report_path)



def test_scope_bootstrap_endpoint_resolves_matching_scope_plan_by_fingerprint(tmp_path, monkeypatch):
    db_path = tmp_path / "api-fingerprint.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope_one = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/one"],
            allowed_file_prefixes=["/"],
            fetch_mode="http",
        )
    )
    scope_two = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/two"],
            allowed_file_prefixes=["/"],
            fetch_mode="http",
        )
    )
    storage.close()

    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "monitor_scope_one.yaml").write_text(
        f"scope_fingerprint: one\nsite_key: demo\ndisplay_name: Example\ncatalog: dev\ngenerated_at: 2026-04-14T00:00:00+00:00\nselection_review_status: approved\nselection_mode: manual\nbusiness_goal: Track one.\nseed_url: https://example.com/\nhomepage_url: https://example.com/\nfetch_mode: http\nfetch_config_json: {{}}\ntree_strategy: selected_scope\ntree_budget_profile: selected_scope_default\nfile_scope_mode: site_root\nallowed_page_prefixes:\n  - /one\nallowed_file_prefixes:\n  - /\nscope_id: \nselected_focus_prefixes:\nexcluded_page_prefixes: []\ndeferred_page_prefixes: []\nexcluded_categories: []\nmax_depth: 3\nmax_pages: 25\nmax_files: 10\nbased_on: {{}}\nselection_summary: {{}}\nnotes: []\n",
        encoding="utf-8",
    )
    (plans_dir / "monitor_scope_two.yaml").write_text(
        f"scope_fingerprint: two\nsite_key: demo\ndisplay_name: Example\ncatalog: dev\ngenerated_at: 2026-04-14T00:00:00+00:00\nselection_review_status: approved\nselection_mode: manual\nbusiness_goal: Track two.\nseed_url: https://example.com/\nhomepage_url: https://example.com/\nfetch_mode: http\nfetch_config_json: {{}}\ntree_strategy: selected_scope\ntree_budget_profile: selected_scope_default\nfile_scope_mode: site_root\nallowed_page_prefixes:\n  - /two\nallowed_file_prefixes:\n  - /\nscope_id: \nselected_focus_prefixes:\nexcluded_page_prefixes: []\ndeferred_page_prefixes: []\nexcluded_categories: []\nmax_depth: 3\nmax_pages: 25\nmax_files: 10\nbased_on: {{}}\nselection_summary: {{}}\nnotes: []\n",
        encoding="utf-8",
    )

    seen = {}

    def fake_bootstrap_scope(**kwargs):
        seen["scope_path"] = str(kwargs["scope_path"])
        report_path = tmp_path / "reports" / "bootstrap-fingerprint.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Bootstrap\n", encoding="utf-8")
        return SimpleNamespace(results=[SimpleNamespace(scope_id=scope_two.id, run_id=123)], report_path=report_path, summary_path=None)

    monkeypatch.setattr("web_listening.blocks.staged_workflow.bootstrap_scope", fake_bootstrap_scope)

    client = TestClient(create_app())
    response = client.post(f"/api/v1/monitor-scopes/{scope_two.id}/bootstrap", json={})

    assert response.status_code == 201
    assert seen["scope_path"].endswith("monitor_scope_two.yaml")


def test_scope_run_job_endpoint_persists_completed_job(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/research"],
            allowed_file_prefixes=["/"],
            is_initialized=True,
        )
    )
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed"))
    storage.close()

    scope_path = tmp_path / "plans" / "monitor_scope_demo.yaml"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(
        f"scope_fingerprint: demo\nsite_key: demo\ndisplay_name: Example\ncatalog: dev\ngenerated_at: 2026-04-14T00:00:00+00:00\nselection_review_status: approved\nselection_mode: manual\nbusiness_goal: Track research.\nseed_url: https://example.com/\nhomepage_url: https://example.com/\nfetch_mode: http\nfetch_config_json: {{}}\ntree_strategy: selected_scope\ntree_budget_profile: selected_scope_default\nfile_scope_mode: site_root\nallowed_page_prefixes:\n  - /research\nallowed_file_prefixes:\n  - /\nscope_id: {scope.id}\nselected_focus_prefixes:\n  - /research\nexcluded_page_prefixes: []\ndeferred_page_prefixes: []\nexcluded_categories: []\nmax_depth: 3\nmax_pages: 25\nmax_files: 10\nbased_on: {{}}\nselection_summary: {{}}\nnotes: []\n",
        encoding="utf-8",
    )

    report_path = tmp_path / "reports" / "run.md"

    def fake_run_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Run\n", encoding="utf-8")
        return SimpleNamespace(result=SimpleNamespace(scope_id=scope.id, run_id=run.id), report_path=report_path)

    monkeypatch.setattr("web_listening.blocks.staged_workflow.run_scope", fake_run_scope)

    client = TestClient(create_app())
    response = client.post(f"/api/v1/monitor-scopes/{scope.id}/run", json={})

    assert response.status_code == 201
    payload = response.json()
    assert payload["job_type"] == "scope.run"
    assert payload["status"] == "completed"
    assert payload["stage"] == "completed"
    assert payload["progress"] == 100
    assert payload["artifact_summary"]["artifact_count"] == 2
    assert payload["run_id"] == run.id
    assert payload["produced_artifacts"]["report_path"] == str(report_path)



def test_scope_report_job_and_latest_report_endpoint(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
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
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed", pages_seen=2))
    storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))
    storage.close()

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
    scope_plan.scope_id = scope.id
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    report_path = tmp_path / "reports" / "tracking_report_demo.md"

    def fake_report_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("# Demo report\n", encoding="utf-8")
        report_payload = {
            "run_id": run.id,
            "next_action": "review_low_priority_changes",
            "review_required_count": 1,
            "high_priority_count": 0,
            "artifact_index": [
                {
                    "plane": "explanation_plane",
                    "kind": "tracking_report",
                    "label": "tracking-report-contract-v3",
                    "path": str(report_path),
                    "url": "",
                    "recommended_reader": "markdown",
                }
            ],
        }
        return SimpleNamespace(report=report_payload, output_path=report_path, output_format="md")

    monkeypatch.setattr("web_listening.blocks.staged_workflow.report_scope", fake_report_scope)

    client = TestClient(create_app())
    response = client.post(f"/api/v1/monitor-scopes/{scope.id}/report", json={})

    assert response.status_code == 201
    payload = response.json()
    assert payload["job_type"] == "scope.report"
    assert payload["status"] == "completed"
    assert payload["stage"] == "completed"
    assert payload["progress"] == 100
    assert payload["artifact_summary"]["artifact_count"] >= 3
    assert payload["produced_artifacts"]["output_path"] == str(report_path)
    assert payload["produced_artifacts"]["output_format"] == "md"

    payload_response = client.get(f"/api/v1/jobs/{payload['job_id']}/payload")
    assert payload_response.status_code == 200
    delivery_payload = payload_response.json()
    assert delivery_payload["job"]["job_id"] == payload["job_id"]
    assert delivery_payload["job"]["stage"] == "completed"
    assert delivery_payload["artifacts"]["produced"]["output_path"] == str(report_path)
    assert delivery_payload["next_action"] == "read_job_artifacts"

    latest = client.get(f"/api/v1/monitor-scopes/{scope.id}/reports/latest")
    assert latest.status_code == 200
    latest_payload = latest.json()
    assert latest_payload["artifact_path"] == str(report_path)
    assert latest_payload["content"].startswith("# Demo report")
    assert latest_payload["report_payload"]["next_action"] == "review_low_priority_changes"
    report_artifact = next(item for item in latest_payload["report_payload"]["artifact_index"] if item["kind"] == "tracking_report")
    assert report_artifact["path"] == str(report_path)
    assert report_artifact["recommended_reader"] == "markdown"


def test_scope_report_endpoint_rejects_invalid_format(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
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
    storage.close()

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
    scope_plan.scope_id = scope.id
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    client = TestClient(create_app())
    response = client.post(f"/api/v1/monitor-scopes/{scope.id}/report", json={"output_format": "json"})

    assert response.status_code == 422
    assert response.json()["detail"] == "output_format must be one of: md, yaml"


def test_scope_report_job_supports_yaml_v3_contract(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
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
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="incremental", status="completed", pages_seen=1))
    storage.update_crawl_scope(CrawlScope(**{**scope.model_dump(), "baseline_run_id": run.id, "is_initialized": True}))
    storage.close()

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
    scope_plan.scope_id = scope.id
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    report_path = tmp_path / "reports" / "tracking_report_demo.yaml"

    def fake_report_scope(**kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            "next_action: attach_monitor_task\nreview_required_count: 0\nhigh_priority_count: 0\nreview_queue: []\nartifact_index:\n  - plane: explanation_plane\n    kind: tracking_report\n    label: tracking-report-contract-v3\n    path: " + str(report_path) + "\n    url: ''\n    recommended_reader: yaml\n",
            encoding="utf-8",
        )
        report_payload = {
            "run_id": run.id,
            "next_action": "attach_monitor_task",
            "review_required_count": 0,
            "high_priority_count": 0,
            "review_queue": [],
            "artifact_index": [
                {
                    "plane": "explanation_plane",
                    "kind": "tracking_report",
                    "label": "tracking-report-contract-v3",
                    "path": str(report_path),
                    "url": "",
                    "recommended_reader": "yaml",
                }
            ],
        }
        return SimpleNamespace(report=report_payload, output_path=report_path, output_format="yaml")

    monkeypatch.setattr("web_listening.blocks.staged_workflow.report_scope", fake_report_scope)

    client = TestClient(create_app())
    response = client.post(f"/api/v1/monitor-scopes/{scope.id}/report", json={"output_format": "yaml"})

    assert response.status_code == 201
    payload = response.json()
    assert payload["produced_artifacts"]["output_format"] == "yaml"

    latest = client.get(f"/api/v1/monitor-scopes/{scope.id}/reports/latest")
    assert latest.status_code == 200
    latest_payload = latest.json()
    assert "next_action: attach_monitor_task" in latest_payload["content"]
    assert "review_required_count: 0" in latest_payload["content"]
    assert "plane: explanation_plane" in latest_payload["content"]
    assert latest_payload["report_payload"]["artifact_index"][0]["recommended_reader"] == "yaml"


def test_scope_report_endpoint_rejects_task_path_outside_data_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
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
    storage.close()

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
    scope_plan.scope_id = scope.id
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_scope_yaml_text(scope_plan), encoding="utf-8")

    client = TestClient(create_app())
    response = client.post(
        f"/api/v1/monitor-scopes/{scope.id}/report",
        json={"task_path": "/tmp/not-allowed-task.yaml"},
    )

    assert response.status_code == 422
    assert "must stay under" in response.json()["detail"]


def test_job_webhook_registration_stub_returns_sample_payload(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)

    client = TestClient(create_app())
    response = client.post(
        "/api/v1/webhooks/job-deliveries",
        json={
            "target_url": "https://hooks.example.com/web-listening",
            "event_types": ["job.completed", "job.failed"],
            "secret_hint": "configured-in-prod",
            "active": True,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["registration_id"] == "job-webhook-stub"
    assert payload["delivery_mode"] == "stub"
    assert payload["event_types"] == ["job.completed", "job.failed"]
    assert payload["sample_payload"]["job"]["stage"] == "completed"
    assert payload["sample_payload"]["next_action"] == "read_job_artifacts"



def test_scope_manifest_latest_endpoint_generates_and_persists_job(tmp_path, monkeypatch):
    db_path = tmp_path / "api.db"
    monkeypatch.setattr(routes.settings, "db_path", db_path)
    monkeypatch.setattr(routes.settings, "data_dir", tmp_path)

    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/research"],
            allowed_file_prefixes=["/"],
            is_initialized=True,
            baseline_run_id=5,
        )
    )
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id, run_type="bootstrap", status="completed"))
    storage.close()

    scope_path = tmp_path / "plans" / "monitor_scope_demo.yaml"
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(
        f"scope_fingerprint: demo\nsite_key: demo\ndisplay_name: Example\ncatalog: dev\ngenerated_at: 2026-04-14T00:00:00+00:00\nselection_review_status: approved\nselection_mode: manual\nbusiness_goal: Track research.\nseed_url: https://example.com/\nhomepage_url: https://example.com/\nfetch_mode: http\nfetch_config_json: {{}}\ntree_strategy: selected_scope\ntree_budget_profile: selected_scope_default\nfile_scope_mode: site_root\nallowed_page_prefixes:\n  - /research\nallowed_file_prefixes:\n  - /\nscope_id: {scope.id}\nselected_focus_prefixes:\n  - /research\nexcluded_page_prefixes: []\ndeferred_page_prefixes: []\nexcluded_categories: []\nmax_depth: 3\nmax_pages: 25\nmax_files: 10\nbased_on: {{}}\nselection_summary: {{}}\nnotes: []\n",
        encoding="utf-8",
    )

    yaml_path = tmp_path / "reports" / "manifest.yaml"

    def fake_export_manifest(**kwargs):
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text("site_key: demo\nrun_id: 5\n", encoding="utf-8")
        report_path = tmp_path / "reports" / "manifest.md"
        report_path.write_text("# Manifest\n", encoding="utf-8")
        return SimpleNamespace(manifest=SimpleNamespace(run_id=run.id), yaml_path=yaml_path, report_path=report_path)

    monkeypatch.setattr("web_listening.blocks.staged_workflow.export_manifest", fake_export_manifest)

    client = TestClient(create_app())
    response = client.get(f"/api/v1/monitor-scopes/{scope.id}/manifest/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["job"]["job_type"] == "scope.manifest"
    assert payload["artifact_path"] == str(yaml_path)
    assert "run_id: 5" in payload["content"]
