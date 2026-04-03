import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.storage import Storage
from web_listening.models import Document, SiteSnapshot


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "run_dev_daily_monitor.py"
SPEC = importlib.util.spec_from_file_location("run_dev_daily_monitor", MODULE_PATH)
daily_module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = daily_module
SPEC.loader.exec_module(daily_module)


class FakeCrawler:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def snapshot(self, site):
        is_docs = site.url.endswith("/docs")
        return SiteSnapshot(
            site_id=site.id,
            captured_at=datetime.now(timezone.utc),
            content_hash=f"hash-{site.url}",
            raw_html="<html><body><main><h1>Example</h1></main></body></html>",
            cleaned_html="<main><h1>Example</h1></main>",
            content_text="Example content",
            markdown="# Example",
            fit_markdown="# Example",
            metadata_json={"word_count": 123 if not is_docs else 45, "hash_basis": "fit_markdown"},
            fetch_mode="http",
            final_url=site.url,
            status_code=200,
            links=["https://example.com/file.pdf"] if is_docs else ["https://example.com/next"],
        )


class FakeProcessor:
    def __init__(self, storage=None):
        self.storage = storage

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def process(self, url, site_id, institution, page_url=""):
        downloads_dir = daily_module.settings.downloads_dir
        downloads_dir.mkdir(parents=True, exist_ok=True)
        local_path = downloads_dir / "sample.pdf"
        local_path.write_bytes(b"%PDF-1.4 sample")
        return Document(
            site_id=site_id,
            title="sample.pdf",
            url=url,
            download_url=url,
            institution=institution,
            page_url=page_url,
            downloaded_at=datetime.now(timezone.utc),
            local_path=str(local_path),
            doc_type="pdf",
            sha256="abc123" * 10 + "ab",
            file_size=len(b"%PDF-1.4 sample"),
            content_type="application/pdf",
            content_md_status="pending",
        )


def test_run_dev_daily_monitor_persists_baseline_and_samples(tmp_path, monkeypatch):
    db_path = tmp_path / "daily.db"
    downloads_dir = tmp_path / "downloads"
    report_path = tmp_path / "report.md"

    monkeypatch.setattr(daily_module.settings, "db_path", db_path)
    monkeypatch.setattr(daily_module.settings, "downloads_dir", downloads_dir)
    monkeypatch.setattr(
        daily_module,
        "load_daily_targets",
        lambda: [
            daily_module.DailyTarget(
                site_key="soa",
                site_name="SOA",
                kind="monitor",
                url="https://example.com/monitor",
                expected_min_words=100,
                expected_min_doc_links=0,
                sample_download_limit=0,
            ),
            daily_module.DailyTarget(
                site_key="soa",
                site_name="SOA",
                kind="documents",
                url="https://example.com/docs",
                expected_min_words=50,
                expected_min_doc_links=1,
                sample_download_limit=1,
            ),
        ],
    )
    monkeypatch.setattr(daily_module, "Crawler", FakeCrawler)
    monkeypatch.setattr(daily_module, "DocumentProcessor", FakeProcessor)

    first_markdown = daily_module.run_daily_monitor(
        report_path=report_path,
        download_samples=True,
    )
    second_markdown = daily_module.run_daily_monitor(
        report_path=report_path,
        download_samples=True,
    )

    assert "Initialized today: `yes`" in first_markdown
    assert "Initialized today: `no`" in second_markdown
    assert report_path.exists()

    storage = Storage(db_path)
    try:
        assert len(storage.list_sites(active_only=False)) == 2
        assert storage.get_latest_snapshot(1) is not None
        assert storage.get_latest_snapshot(2) is not None
        assert len(storage.list_documents(site_id=2)) == 1
        assert storage.list_changes() == []
    finally:
        storage.close()
