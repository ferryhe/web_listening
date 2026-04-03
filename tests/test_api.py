from datetime import datetime, timezone

from fastapi.testclient import TestClient

from web_listening.api import routes
from web_listening.api.app import create_app
from web_listening.blocks.rescue import RescueAttempt, RescueResult
from web_listening.blocks.storage import Storage
from web_listening.models import Document, Site, SiteSnapshot


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
