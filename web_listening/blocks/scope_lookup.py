from __future__ import annotations

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlScope, Site


def find_scope_for_plan(storage: Storage, plan: MonitorScopePlan) -> tuple[Site, CrawlScope]:
    """Return the (Site, CrawlScope) pair that matches the given monitor scope plan."""
    for site in storage.list_sites(active_only=False):
        if site.url != plan.seed_url:
            continue
        for scope in storage.list_crawl_scopes(site_id=site.id):
            if (
                scope.seed_url == plan.seed_url
                and scope.allowed_page_prefixes == plan.allowed_page_prefixes
                and scope.allowed_file_prefixes == plan.allowed_file_prefixes
            ):
                return site, scope
    raise ValueError(f"Could not find a stored crawl scope matching monitor scope for `{plan.site_key}`.")
