from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel

from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import AnalysisReport, Change, Document, Site

router = APIRouter()


def get_storage() -> Storage:
    return Storage(settings.db_path)


# ── Request bodies ──────────────────────────────────────────────────────────

class AddSiteRequest(BaseModel):
    url: str
    name: str = ""
    tags: List[str] = []


class AnalyzeRequest(BaseModel):
    since_date: Optional[str] = None


class DownloadDocsRequest(BaseModel):
    institution: str
    url: Optional[str] = None


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
    storage = get_storage()
    try:
        site = storage.add_site(Site(url=body.url, name=body.name or body.url, tags=body.tags))
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
    from web_listening.blocks.diff import compute_diff, find_document_links, find_new_links
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
                has_changed, diff_snippet = compute_diff(old_snap.content_text, new_snap.content_text)
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
            storage._update_site_checked(site.id)
    except Exception:
        pass
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


@router.post("/sites/{site_id}/download-docs")
def download_docs_for_site(site_id: int, body: DownloadDocsRequest, background_tasks: BackgroundTasks):
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
        with DocumentProcessor() as proc:
            for doc_url in urls_to_download:
                try:
                    doc = proc.process(doc_url, site_id=site_id, institution=institution, page_url=site.url)
                    storage.add_document(doc)
                except Exception:
                    pass
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
