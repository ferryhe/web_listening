from pathlib import Path

import pytest

from web_listening.blocks.job_orchestration import resolve_scope_plan_path
from web_listening.blocks.scope_lookup import (
    find_scope_for_plan,
    require_scope_or_raise,
    require_site_or_raise,
    resolve_scope_path_or_raise,
)
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlScope, Site


def test_require_site_or_raise_returns_site(tmp_path):
    db_path = tmp_path / "scope-lookup.db"
    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com", name="Example"))

    assert require_site_or_raise(storage, site.id) == site

    storage.close()


def test_require_site_or_raise_raises_for_missing_site(tmp_path):
    db_path = tmp_path / "scope-lookup.db"
    storage = Storage(db_path)

    with pytest.raises(LookupError, match="Site not found"):
        require_site_or_raise(storage, 999)

    storage.close()


def test_require_scope_or_raise_raises_for_missing_scope(tmp_path):
    db_path = tmp_path / "scope-lookup.db"
    storage = Storage(db_path)

    with pytest.raises(LookupError, match="Monitor scope not found"):
        require_scope_or_raise(storage, 999)

    storage.close()


def test_resolve_scope_path_or_raise_returns_matching_plan_path(tmp_path):
    db_path = tmp_path / "scope-lookup.db"
    data_dir = tmp_path
    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/research"],
            allowed_file_prefixes=["/"],
            fetch_mode="http",
        )
    )
    storage.close()

    plan_path = data_dir / "plans" / "monitor_scope_demo.yaml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"scope_fingerprint: demo\nsite_key: demo\ndisplay_name: Example\ncatalog: dev\ngenerated_at: 2026-04-14T00:00:00+00:00\nselection_review_status: approved\nselection_mode: manual\nbusiness_goal: Track research.\nseed_url: https://example.com/\nhomepage_url: https://example.com/\nfetch_mode: http\nfetch_config_json: {{}}\ntree_strategy: selected_scope\ntree_budget_profile: selected_scope_default\nfile_scope_mode: site_root\nallowed_page_prefixes:\n  - /research\nallowed_file_prefixes:\n  - /\nscope_id: {scope.id}\nselected_focus_prefixes:\n  - /research\nexcluded_page_prefixes: []\ndeferred_page_prefixes: []\nexcluded_categories: []\nmax_depth: 3\nmax_pages: 25\nmax_files: 10\nbased_on: {{}}\nselection_summary: {{}}\nnotes: []\n",
        encoding="utf-8",
    )

    storage = Storage(db_path)
    assert resolve_scope_path_or_raise(storage, scope.id, data_dir=data_dir) == resolve_scope_plan_path(
        scope.id,
        scope=scope,
        data_dir=data_dir,
    )
    storage.close()


def test_resolve_scope_path_or_raise_wraps_missing_plan_as_lookup_error(tmp_path):
    db_path = tmp_path / "scope-lookup.db"
    storage = Storage(db_path)
    site = storage.add_site(Site(url="https://example.com/", name="Example"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=site.url,
            allowed_origin="https://example.com",
            allowed_page_prefixes=["/research"],
            allowed_file_prefixes=["/"],
            fetch_mode="http",
        )
    )

    with pytest.raises(LookupError, match="monitor scope plan"):
        resolve_scope_path_or_raise(storage, scope.id, data_dir=tmp_path)

    storage.close()
