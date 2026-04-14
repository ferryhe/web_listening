from __future__ import annotations

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, compute_scope_fingerprint
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlScope, Site


def _scope_fingerprint(scope: CrawlScope) -> str:
    return compute_scope_fingerprint(
        seed_url=scope.seed_url,
        allowed_page_prefixes=list(scope.allowed_page_prefixes),
        allowed_file_prefixes=list(scope.allowed_file_prefixes),
        fetch_mode=scope.fetch_mode,
    )


def _site_by_id(storage: Storage, site_id: int) -> Site | None:
    return storage.get_site(site_id)


def find_scope_for_plan(storage: Storage, plan: MonitorScopePlan) -> tuple[Site, CrawlScope]:
    """Return the (Site, CrawlScope) pair that matches the given monitor scope plan."""
    if plan.scope_id is not None:
        scoped = storage.get_crawl_scope(plan.scope_id)
        if scoped is None:
            raise ValueError(f"Monitor scope references missing scope_id `{plan.scope_id}`.")
        site = _site_by_id(storage, scoped.site_id)
        if site is None:
            raise ValueError(f"Stored crawl scope `{plan.scope_id}` references missing site `{scoped.site_id}`.")
        if scoped.seed_url != plan.seed_url:
            raise ValueError(
                f"Stored crawl scope `{plan.scope_id}` seed_url `{scoped.seed_url}` does not match monitor scope seed_url `{plan.seed_url}`."
            )
        if plan.scope_fingerprint and _scope_fingerprint(scoped) != plan.scope_fingerprint:
            raise ValueError(
                f"Stored crawl scope `{plan.scope_id}` does not match monitor scope fingerprint `{plan.scope_fingerprint}`."
            )
        return site, scoped

    plan_fingerprint = plan.scope_fingerprint or compute_scope_fingerprint(
        seed_url=plan.seed_url,
        allowed_page_prefixes=list(plan.allowed_page_prefixes),
        allowed_file_prefixes=list(plan.allowed_file_prefixes),
        fetch_mode=plan.fetch_mode,
    )
    for site in storage.list_sites(active_only=False):
        for scope in storage.list_crawl_scopes(site_id=site.id):
            if scope.seed_url != plan.seed_url:
                continue
            if _scope_fingerprint(scope) == plan_fingerprint:
                return site, scope
            if (
                scope.allowed_page_prefixes == plan.allowed_page_prefixes
                and scope.allowed_file_prefixes == plan.allowed_file_prefixes
                and scope.fetch_mode == plan.fetch_mode
            ):
                return site, scope
    raise ValueError(f"Could not find a stored crawl scope matching monitor scope for `{plan.site_key}`.")
