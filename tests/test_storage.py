import pytest
from datetime import datetime, timezone

from web_listening.blocks.storage import Storage
from web_listening.models import Site, SiteSnapshot, Change, Document, AnalysisReport


@pytest.fixture
def storage(tmp_path):
    db_path = tmp_path / "test.db"
    s = Storage(db_path)
    yield s
    s.close()


def test_add_and_get_site(storage):
    site = Site(url="https://example.com", name="Example", tags=["news"])
    saved = storage.add_site(site)
    assert saved.id is not None
    assert saved.url == "https://example.com"
    assert saved.name == "Example"
    assert "news" in saved.tags

    retrieved = storage.get_site(saved.id)
    assert retrieved is not None
    assert retrieved.id == saved.id
    assert retrieved.url == saved.url


def test_get_site_not_found(storage):
    result = storage.get_site(9999)
    assert result is None


def test_list_sites(storage):
    storage.add_site(Site(url="https://a.com", name="A"))
    storage.add_site(Site(url="https://b.com", name="B"))
    sites = storage.list_sites()
    assert len(sites) == 2


def test_list_sites_active_only(storage):
    s = storage.add_site(Site(url="https://a.com", name="A"))
    storage.add_site(Site(url="https://b.com", name="B"))
    storage.deactivate_site(s.id)
    active = storage.list_sites(active_only=True)
    assert len(active) == 1
    assert active[0].url == "https://b.com"


def test_add_and_get_snapshot(storage):
    site = storage.add_site(Site(url="https://example.com", name="Test"))
    snap = SiteSnapshot(
        site_id=site.id,
        captured_at=datetime.now(timezone.utc),
        content_hash="abc123",
        raw_html="<html><body><h1>Hello world</h1></body></html>",
        cleaned_html="<body><h1>Hello world</h1></body>",
        content_text="Hello world",
        markdown="# Hello world",
        fit_markdown="# Hello world",
        metadata_json={"word_count": 2},
        fetch_mode="http",
        final_url="https://example.com/final",
        status_code=200,
        links=["https://example.com/page1"],
    )
    saved = storage.add_snapshot(snap)
    assert saved.id is not None
    assert saved.content_hash == "abc123"
    assert saved.markdown == "# Hello world"
    assert saved.metadata_json["word_count"] == 2

    latest = storage.get_latest_snapshot(site.id)
    assert latest is not None
    assert latest.content_hash == "abc123"
    assert latest.cleaned_html == "<body><h1>Hello world</h1></body>"
    assert latest.fit_markdown == "# Hello world"
    assert latest.final_url == "https://example.com/final"
    assert latest.status_code == 200
    assert "https://example.com/page1" in latest.links


def test_get_latest_snapshot_none(storage):
    result = storage.get_latest_snapshot(9999)
    assert result is None


def test_add_and_list_changes(storage):
    site = storage.add_site(Site(url="https://example.com", name="Test"))
    change = Change(
        site_id=site.id,
        detected_at=datetime.now(timezone.utc),
        change_type="new_content",
        summary="Content changed",
        diff_snippet="--- old\n+++ new",
    )
    saved = storage.add_change(change)
    assert saved.id is not None
    assert saved.change_type == "new_content"

    changes = storage.list_changes(site_id=site.id)
    assert len(changes) == 1
    assert changes[0].summary == "Content changed"


def test_list_changes_since_filter(storage):
    site = storage.add_site(Site(url="https://example.com", name="Test"))
    from datetime import timedelta

    old_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new_time = datetime.now(timezone.utc)

    storage.add_change(Change(
        site_id=site.id,
        detected_at=old_time,
        change_type="new_content",
        summary="Old change",
    ))
    storage.add_change(Change(
        site_id=site.id,
        detected_at=new_time,
        change_type="new_content",
        summary="Recent change",
    ))

    since = datetime(2021, 1, 1, tzinfo=timezone.utc)
    changes = storage.list_changes(since=since)
    assert all(c.summary == "Recent change" for c in changes)


def test_add_and_list_documents(storage):
    site = storage.add_site(Site(url="https://example.com", name="Test"))
    doc = Document(
        site_id=site.id,
        title="Annual Report",
        url="https://example.com/report.pdf",
        download_url="https://example.com/report.pdf",
        institution="ExampleOrg",
        page_url="https://example.com/reports",
        doc_type="pdf",
        content_md="# Annual Report",
        content_md_status="converted",
        content_md_updated_at=datetime.now(timezone.utc),
    )
    saved = storage.add_document(doc)
    assert saved.id is not None
    assert saved.title == "Annual Report"
    assert saved.content_md_status == "converted"

    docs = storage.list_documents(site_id=site.id)
    assert len(docs) == 1
    assert docs[0].content_md_status == "converted"
    docs_by_inst = storage.list_documents(institution="ExampleOrg")
    assert len(docs_by_inst) == 1
    docs_other = storage.list_documents(institution="Other")
    assert len(docs_other) == 0


def test_update_document_content_md(storage):
    site = storage.add_site(Site(url="https://example.com", name="Test"))
    doc = storage.add_document(
        Document(
            site_id=site.id,
            title="Annual Report",
            url="https://example.com/report.pdf",
            download_url="https://example.com/report.pdf",
            institution="ExampleOrg",
            doc_type="pdf",
        )
    )

    updated = storage.update_document_content_md(
        doc.id,
        content_md="# Annual Report\n\nConverted",
        content_md_status="converted",
    )

    assert updated is not None
    assert updated.content_md.startswith("# Annual Report")
    assert updated.content_md_status == "converted"
    assert updated.content_md_updated_at is not None


def test_add_and_list_analyses(storage):
    report = AnalysisReport(
        period_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        period_end=datetime(2024, 1, 7, tzinfo=timezone.utc),
        generated_at=datetime.now(timezone.utc),
        site_ids=[1, 2, 3],
        summary_md="## Summary\n\nNo significant changes.",
        change_count=5,
    )
    saved = storage.add_analysis(report)
    assert saved.id is not None
    assert saved.change_count == 5
    assert 1 in saved.site_ids

    analyses = storage.list_analyses()
    assert len(analyses) == 1
    assert analyses[0].summary_md == "## Summary\n\nNo significant changes."
