from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from web_listening.blocks.storage import Storage
from web_listening.blocks.rescue import run_site_rescue
from web_listening.config import settings
from web_listening.models import AnalysisReport, Change, Document, Site, SiteSnapshot

router = APIRouter()

_ALLOWED_SCHEMES = {"http", "https"}


def _validate_url(url: str) -> None:
    """Raise HTTP 422 if *url* is not a valid http/https URL."""
    scheme = urlparse(url).scheme
    if scheme not in _ALLOWED_SCHEMES:
        raise HTTPException(
            status_code=422,
            detail=f"URL scheme '{scheme}' is not allowed; only http and https are accepted.",
        )


def get_storage() -> Storage:
    return Storage(settings.db_path)


# ── Request bodies ──────────────────────────────────────────────────────────

class AddSiteRequest(BaseModel):
    url: str
    name: str = ""
    tags: List[str] = Field(default_factory=list)
    fetch_mode: str = "http"
    fetch_config_json: dict = Field(default_factory=dict)


class AnalyzeRequest(BaseModel):
    since_date: Optional[str] = None


class DownloadDocsRequest(BaseModel):
    institution: str
    url: Optional[str] = None


class UpdateDocumentContentRequest(BaseModel):
    content_md: str
    content_md_status: str = "converted"


class RescueCheckRequest(BaseModel):
    expected_min_words: int = 50
    min_inventory_links: int = 5
    allow_browser: bool = True
    allow_official_feeds: bool = True
    sitemap_url: Optional[str] = None
    rss_url: Optional[str] = None
    browser_fetch_config: dict = Field(default_factory=dict)


class RescueAttemptResponse(BaseModel):
    strategy: str
    url: str
    fetch_mode: str
    status_code: Optional[int] = None
    final_url: str = ""
    request_user_agent: str = ""
    word_count: int = 0
    link_count: int = 0
    source_kind: str = ""
    passed: bool = False
    reason: str = ""
    head: str = ""
    error: str = ""


class RescueCheckResponse(BaseModel):
    site_id: int
    site_name: str
    monitor_url: str
    primary_strategy: str
    resolved_strategy: str = ""
    resolved: bool
    attempts: List[RescueAttemptResponse]
    winning_snapshot: Optional[SiteSnapshot] = None


# ── Sites ───────────────────────────────────────────────────────────────────

@router.get("/sites", response_model=List[Site])
def list_sites():
    storage = get_storage()
    try:
        return storage.list_sites()
    finally:
        storage.close()


@router.post("/sites", response_model=Site, status_code=201)
def add_site(body: AddSiteRequest):
    _validate_url(body.url)
    storage = get_storage()
    try:
        site = storage.add_site(
            Site(
                url=body.url,
                name=body.name or body.url,
                tags=body.tags,
                fetch_mode=body.fetch_mode,
                fetch_config_json=body.fetch_config_json,
            )
        )
        return site
    finally:
        storage.close()


@router.get("/sites/{site_id}", response_model=Site)
def get_site(site_id: int):
    storage = get_storage()
    try:
        site = storage.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
        return site
    finally:
        storage.close()


@router.get("/sites/{site_id}/snapshots/latest", response_model=SiteSnapshot)
def get_latest_snapshot(site_id: int):
    storage = get_storage()
    try:
        site = storage.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
        snapshot = storage.get_latest_snapshot(site_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return snapshot
    finally:
        storage.close()


@router.post("/sites/{site_id}/rescue-check", response_model=RescueCheckResponse)
def rescue_check_site(site_id: int, body: RescueCheckRequest):
    if body.sitemap_url is not None:
        _validate_url(body.sitemap_url)
    if body.rss_url is not None:
        _validate_url(body.rss_url)

    storage = get_storage()
    try:
        site = storage.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
    finally:
        storage.close()

    result = run_site_rescue(
        site,
        expected_min_words=body.expected_min_words,
        min_inventory_links=body.min_inventory_links,
        allow_browser=body.allow_browser,
        allow_official_feeds=body.allow_official_feeds,
        sitemap_url=body.sitemap_url,
        rss_url=body.rss_url,
        browser_fetch_config=body.browser_fetch_config,
    )
    attempts = [
        RescueAttemptResponse(
            strategy=attempt.strategy,
            url=attempt.url,
            fetch_mode=attempt.fetch_mode,
            status_code=attempt.status_code,
            final_url=attempt.final_url,
            request_user_agent=attempt.request_user_agent,
            word_count=attempt.word_count,
            link_count=attempt.link_count,
            source_kind=attempt.source_kind,
            passed=attempt.passed,
            reason=attempt.reason,
            head=attempt.head,
            error=attempt.error,
        )
        for attempt in result.attempts
    ]
    winning_attempt = result.winning_attempt
    return RescueCheckResponse(
        site_id=site.id,
        site_name=site.name,
        monitor_url=site.url,
        primary_strategy=result.primary_strategy,
        resolved_strategy=result.resolved_strategy,
        resolved=result.resolved,
        attempts=attempts,
        winning_snapshot=winning_attempt.snapshot if winning_attempt is not None else None,
    )


@router.delete("/sites/{site_id}", status_code=204)
def deactivate_site(site_id: int):
    storage = get_storage()
    try:
        site = storage.get_site(site_id)
        if not site:
            raise HTTPException(status_code=404, detail="Site not found")
        storage.deactivate_site(site_id)
    finally:
        storage.close()


@router.post("/sites/{site_id}/check")
def check_site(site_id: int, background_tasks: BackgroundTasks):
    storage = get_storage()
    site = storage.get_site(site_id)
    storage.close()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    background_tasks.add_task(_do_check, site_id)
    return {"status": "check queued", "site_id": site_id}


def _do_check(site_id: int):
    from web_listening.blocks.crawler import Crawler
    from web_listening.blocks.diff import compute_diff, find_document_links, find_new_links, select_compare_text
    from web_listening.models import Change

    storage = get_storage()
    site = storage.get_site(site_id)
    if not site:
        storage.close()
        return

    try:
        with Crawler() as crawler:
            new_snap = crawler.snapshot(site)
            old_snap = storage.get_latest_snapshot(site.id)

            if old_snap:
                has_changed, diff_snippet = compute_diff(
                    select_compare_text(
                        fit_markdown=old_snap.fit_markdown,
                        markdown=old_snap.markdown,
                        content_text=old_snap.content_text,
                    ),
                    select_compare_text(
                        fit_markdown=new_snap.fit_markdown,
                        markdown=new_snap.markdown,
                        content_text=new_snap.content_text,
                    ),
                )
                if has_changed:
                    storage.add_change(Change(
                        site_id=site.id,
                        detected_at=datetime.now(timezone.utc),
                        change_type="new_content",
                        summary=f"Content changed on {site.name}",
                        diff_snippet=diff_snippet,
                    ))

                new_links = find_new_links(old_snap.links, new_snap.links)
                if new_links:
                    storage.add_change(Change(
                        site_id=site.id,
                        detected_at=datetime.now(timezone.utc),
                        change_type="new_links",
                        summary=f"{len(new_links)} new links found",
                        diff_snippet="\n".join(new_links[:10]),
                    ))

                doc_links = find_document_links(new_links)
                if doc_links:
                    storage.add_change(Change(
                        site_id=site.id,
                        detected_at=datetime.now(timezone.utc),
                        change_type="new_document",
                        summary=f"{len(doc_links)} new document links",
                        diff_snippet="\n".join(doc_links[:10]),
                    ))

            storage.add_snapshot(new_snap)
            storage.update_site_checked(site.id)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Error during background check for site %s: %s", site_id, exc)
    finally:
        storage.close()


# ── Changes ─────────────────────────────────────────────────────────────────

@router.get("/changes", response_model=List[Change])
def list_changes(site_id: Optional[int] = None, since: Optional[str] = None):
    from dateutil import parser as dtparser

    storage = get_storage()
    try:
        since_dt = dtparser.parse(since) if since else None
        return storage.list_changes(site_id=site_id, since=since_dt)
    finally:
        storage.close()


# ── Documents ────────────────────────────────────────────────────────────────

@router.get("/documents", response_model=List[Document])
def list_documents(institution: Optional[str] = None, site_id: Optional[int] = None):
    storage = get_storage()
    try:
        return storage.list_documents(site_id=site_id, institution=institution)
    finally:
        storage.close()


@router.patch("/documents/{document_id}/content", response_model=Document)
def update_document_content(document_id: int, body: UpdateDocumentContentRequest):
    storage = get_storage()
    try:
        document = storage.update_document_content_md(
            document_id,
            content_md=body.content_md,
            content_md_status=body.content_md_status,
        )
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        return document
    finally:
        storage.close()


@router.post("/sites/{site_id}/download-docs")
def download_docs_for_site(site_id: int, body: DownloadDocsRequest, background_tasks: BackgroundTasks):
    if body.url is not None:
        _validate_url(body.url)
    storage = get_storage()
    site = storage.get_site(site_id)
    storage.close()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    background_tasks.add_task(_do_download, site_id, body.institution, body.url)
    return {"status": "download queued", "site_id": site_id}


def _do_download(site_id: int, institution: str, url: Optional[str]):
    from web_listening.blocks.document import DocumentProcessor
    from web_listening.blocks.diff import find_document_links

    storage = get_storage()
    site = storage.get_site(site_id)
    if not site:
        storage.close()
        return

    urls_to_download = []
    if url:
        urls_to_download = [url]
    else:
        snap = storage.get_latest_snapshot(site_id)
        if snap:
            urls_to_download = find_document_links(snap.links)

    try:
        with DocumentProcessor(storage=storage) as proc:
            for doc_url in urls_to_download:
                try:
                    doc = proc.process(doc_url, site_id=site_id, institution=institution, page_url=site.url)
                    storage.add_document(doc)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error("Failed to download %s: %s", doc_url, exc)
    finally:
        storage.close()


# ── Analysis ─────────────────────────────────────────────────────────────────

@router.post("/analyze", response_model=AnalysisReport)
def run_analysis(body: AnalyzeRequest):
    from web_listening.blocks.analyzer import Analyzer
    from dateutil import parser as dtparser

    storage = get_storage()
    try:
        period_end = datetime.now(timezone.utc)
        period_start = dtparser.parse(body.since_date) if body.since_date else period_end - timedelta(days=7)

        changes = storage.list_changes(since=period_start)
        analyzer = Analyzer()
        report = analyzer.analyze_changes(changes, period_start, period_end)
        return storage.add_analysis(report)
    finally:
        storage.close()


@router.get("/analyses", response_model=List[AnalysisReport])
def list_analyses():
    storage = get_storage()
    try:
        return storage.list_analyses()
    finally:
        storage.close()
