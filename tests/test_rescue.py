from datetime import datetime, timezone

from web_listening.blocks import rescue
from web_listening.models import Site, SiteSnapshot


class FakeCrawler:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def snapshot(self, site: Site) -> SiteSnapshot:
        return SiteSnapshot(
            site_id=site.id,
            captured_at=datetime.now(timezone.utc),
            content_hash="hash123",
            raw_html="<html><body><main><h1>Recovered</h1></main></body></html>",
            cleaned_html="<main><h1>Recovered</h1></main>",
            content_text="Recovered content",
            markdown="# Recovered",
            fit_markdown="# Recovered",
            metadata_json={"word_count": 120, "source_kind": "html"},
            fetch_mode=site.fetch_mode,
            final_url=site.url,
            status_code=200,
            links=["https://example.com/report.pdf"],
        )


def test_run_site_rescue_preserves_real_site_id(monkeypatch):
    monkeypatch.setattr(rescue, "Crawler", FakeCrawler)

    result = rescue.run_site_rescue(Site(id=42, url="https://example.com", name="Example"))

    assert result.resolved is True
    assert result.winning_attempt is not None
    assert result.winning_attempt.snapshot is not None
    assert result.winning_attempt.snapshot.site_id == 42


def test_build_default_site_rescue_candidates_uses_browser_user_agent_profile():
    candidates = rescue.build_default_site_rescue_candidates(
        Site(id=7, url="https://example.com", name="Example")
    )

    browser_candidate = next(candidate for candidate in candidates if candidate.strategy == "browser")
    assert browser_candidate.fetch_mode == "browser"
    assert browser_candidate.fetch_config_json["user_agent_profile"] == "browser"
