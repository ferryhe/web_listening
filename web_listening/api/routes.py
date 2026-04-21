from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from web_listening.blocks.job_orchestration import execute_job, persist_job_result, resolve_scope_plan_path
from web_listening.blocks.monitor_task import build_default_task_path, build_monitor_task, render_yaml_text
from web_listening.blocks.rescue import run_site_rescue
from web_listening.blocks.job_artifacts import (
    load_job_delivery_payload_or_raise,
    load_job_or_raise,
    load_latest_scope_manifest_artifact_or_create,
    load_latest_scope_report_artifact_or_raise,
)
from web_listening.blocks.scope_lookup import require_site_or_raise, resolve_scope_path_or_raise
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import AnalysisReport, Change, Document, Job, Site, SiteSnapshot

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


def _resolve_data_root() -> Path:
    return Path(settings.data_dir).resolve()


def _ensure_path_within_data_root(path: Path) -> Path:
    resolved = path.resolve()
    data_root = _resolve_data_root()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Path `{path}` must stay under `{data_root}`") from exc
    return resolved


def _safe_output_path(raw_path: str | None, *, default_path: Path) -> Path:
    if not raw_path:
        path = default_path
    else:
        candidate = Path(raw_path)
        path = candidate if candidate.is_absolute() else _resolve_data_root() / candidate
    if ".." in path.parts:
        raise HTTPException(status_code=422, detail="Path traversal is not allowed")
    return _ensure_path_within_data_root(path)


def _safe_input_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    candidate = Path(raw_path)
    path = candidate if candidate.is_absolute() else _resolve_data_root() / candidate
    if ".." in path.parts:
        raise HTTPException(status_code=422, detail="Path traversal is not allowed")
    resolved = _ensure_path_within_data_root(path)
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Path `{resolved}` not found")
    return resolved


def get_storage() -> Storage:
    return Storage(settings.db_path)


def _resolve_scope_path(scope_id: int) -> Path:
    storage = get_storage()
    try:
        return resolve_scope_path_or_raise(storage, scope_id, data_dir=settings.data_dir)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        storage.close()


def _read_text_if_present(path_value: str) -> str:
    path = _ensure_path_within_data_root(Path(path_value))
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Artifact `{path}` not found")
    max_bytes = 512 * 1024
    if path.stat().st_size > max_bytes:
        raise HTTPException(status_code=413, detail=f"Artifact `{path}` is too large to inline")
    return path.read_text(encoding="utf-8")


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


class CreateMonitorTaskRequest(BaseModel):
    task_name: str
    site_url: str
    task_description: str
    goal: str
    focus_topics: List[str] = Field(default_factory=list)
    must_track_prefixes: List[str] = Field(default_factory=list)
    exclude_prefixes: List[str] = Field(default_factory=list)
    prefer_file_types: List[str] = Field(default_factory=list)
    must_download_patterns: List[str] = Field(default_factory=list)
    severity_policy: Optional[List[dict[str, object]]] = None
    change_severity_rules: Optional[dict[str, str]] = None
    handoff_requirements: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    report_style: str = "briefing"
    output_path: Optional[str] = None


class BootstrapScopeRequest(BaseModel):
    download_files: bool = False
    refresh_existing: bool = False
    max_depth: Optional[int] = None
    max_pages: Optional[int] = None
    max_files: Optional[int] = None
    report_path: Optional[str] = None
    summary_path: Optional[str] = None
    include_summary: bool = False


class RunScopeRequest(BaseModel):
    download_files: bool = False
    max_depth: Optional[int] = None
    max_pages: Optional[int] = None
    max_files: Optional[int] = None
    report_path: Optional[str] = None


class ReportScopeRequest(BaseModel):
    task_path: Optional[str] = None
    run_id: Optional[int] = None
    output_path: Optional[str] = None
    output_format: str = "md"


class ArtifactEnvelope(BaseModel):
    job: Job
    artifact_path: str
    content: str
    report_payload: Optional[dict[str, object]] = None


class JobDeliveryPayload(BaseModel):
    contract_version: str
    job: dict[str, object]
    error: dict[str, object]
    artifacts: dict[str, object]
    artifact_contract: dict[str, object]
    next_action: str


class JobWebhookRegistrationRequest(BaseModel):
    target_url: str
    event_types: List[str] = Field(default_factory=lambda: ["job.completed"])
    secret_hint: str = ""
    active: bool = True


class JobWebhookRegistrationResponse(BaseModel):
    registration_id: str
    target_url: str
    event_types: List[str]
    active: bool
    delivery_mode: str
    sample_payload: JobDeliveryPayload


def _serialize_report_payload(report: object) -> Optional[dict[str, object]]:
    if hasattr(report, "to_dict") and callable(getattr(report, "to_dict")):
        payload = report.to_dict()
        return payload if isinstance(payload, dict) else None
    if hasattr(report, "model_dump") and callable(getattr(report, "model_dump")):
        payload = report.model_dump(mode="json")
        return payload if isinstance(payload, dict) else None
    if isinstance(report, dict):
        return report
    return None


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
        return require_site_or_raise(storage, site_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        storage.close()


@router.get("/sites/{site_id}/snapshots/latest", response_model=SiteSnapshot)
def get_latest_snapshot(site_id: int):
    storage = get_storage()
    try:
        require_site_or_raise(storage, site_id)
        snapshot = storage.get_latest_snapshot(site_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        return snapshot
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
        site = require_site_or_raise(storage, site_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
        require_site_or_raise(storage, site_id)
        storage.deactivate_site(site_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        storage.close()


@router.post("/monitor-tasks", response_model=Job, status_code=201)
def create_monitor_task(body: CreateMonitorTaskRequest):
    from web_listening.blocks.monitor_task import build_default_task_path, build_monitor_task, render_yaml_text

    _validate_url(body.site_url)
    started = datetime.now(timezone.utc)
    task = build_monitor_task(
        task_name=body.task_name,
        site_url=body.site_url.strip(),
        task_description=body.task_description,
        goal=body.goal,
        focus_topics=body.focus_topics,
        must_track_prefixes=body.must_track_prefixes,
        exclude_prefixes=body.exclude_prefixes,
        prefer_file_types=body.prefer_file_types,
        must_download_patterns=body.must_download_patterns,
        severity_policy=body.severity_policy,
        change_severity_rules=body.change_severity_rules,
        handoff_requirements=body.handoff_requirements,
        notes=body.notes,
        report_style=body.report_style,
    )
    output_path = _safe_output_path(
        body.output_path,
        default_path=build_default_task_path(task.task_name, data_dir=settings.data_dir),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_yaml_text(task), encoding="utf-8")
    return persist_job_result(
        job_type="monitor_task.create",
        produced_artifacts={
            "task_path": str(output_path),
            "task_name": task.task_name,
            "site_url": task.site_url,
        },
        accepted_at=started,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: int):
    try:
        return load_job_or_raise(db_path=settings.db_path, job_id=job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/payload", response_model=JobDeliveryPayload)
def get_job_payload(job_id: int):
    try:
        return JobDeliveryPayload(**load_job_delivery_payload_or_raise(db_path=settings.db_path, job_id=job_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/webhooks/job-deliveries", response_model=JobWebhookRegistrationResponse, status_code=201)
def register_job_webhook(body: JobWebhookRegistrationRequest):
    _validate_url(body.target_url)
    sample_job = Job(
        job_id=0,
        job_type="scope.run",
        status="completed",
        stage="completed",
        stage_message="Sample completion payload.",
        progress=100,
        produced_artifacts={"report_path": "data/reports/sample.md"},
        artifact_summary={"artifact_count": 1, "artifact_keys": ["report_path"], "path_keys": ["report_path"], "has_artifacts": True},
    )
    return JobWebhookRegistrationResponse(
        registration_id="job-webhook-stub",
        target_url=body.target_url,
        event_types=body.event_types or ["job.completed"],
        active=body.active,
        delivery_mode="stub",
        sample_payload=JobDeliveryPayload(**sample_job.to_delivery_payload()),
    )


@router.post("/monitor-scopes/{scope_id}/bootstrap", response_model=Job, status_code=201)
def bootstrap_monitor_scope(scope_id: int, body: BootstrapScopeRequest):
    from web_listening.blocks.staged_workflow import bootstrap_scope as staged_bootstrap_scope

    def _runner(progress):
        progress.update(
            stage="loading_scope",
            stage_message="Resolving scope plan for bootstrap.",
            progress=10,
        )
        scope_path = _resolve_scope_path(scope_id)
        progress.update(
            status="running",
            stage="executing_workflow",
            stage_message="Running bootstrap workflow.",
            progress=45,
        )
        artifacts = staged_bootstrap_scope(
            scope_path=scope_path,
            download_files=body.download_files,
            refresh_existing=body.refresh_existing,
            max_depth=body.max_depth,
            max_pages=body.max_pages,
            max_files=body.max_files,
            report_path=body.report_path,
            summary_path=body.summary_path,
            include_summary=body.include_summary,
        )
        progress.update(
            stage="writing_artifacts",
            stage_message="Persisting bootstrap artifacts.",
            progress=90,
        )
        first = artifacts.results[0] if artifacts.results else None
        produced_artifacts = {
            "scope_path": str(scope_path),
            "report_path": str(artifacts.report_path),
            **({"summary_path": str(artifacts.summary_path)} if artifacts.summary_path else {}),
        }
        return {
            "scope_id": first.scope_id if first else scope_id,
            "run_id": first.run_id if first else None,
            "produced_artifacts": produced_artifacts,
        }

    try:
        return execute_job(job_type="scope.bootstrap", scope_id=scope_id, runner=_runner)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/monitor-scopes/{scope_id}/run", response_model=Job, status_code=201)
def run_monitor_scope(scope_id: int, body: RunScopeRequest):
    from web_listening.blocks.staged_workflow import run_scope as staged_run_scope

    def _runner(progress):
        progress.update(
            stage="loading_scope",
            stage_message="Resolving scope plan for incremental run.",
            progress=10,
        )
        scope_path = _resolve_scope_path(scope_id)
        progress.update(
            status="running",
            stage="executing_workflow",
            stage_message="Running incremental workflow.",
            progress=50,
        )
        artifacts = staged_run_scope(
            scope_path=scope_path,
            download_files=body.download_files,
            max_depth=body.max_depth,
            max_pages=body.max_pages,
            max_files=body.max_files,
            report_path=body.report_path,
        )
        progress.update(
            stage="writing_artifacts",
            stage_message="Persisting run artifacts.",
            progress=90,
        )
        return {
            "scope_id": artifacts.result.scope_id or scope_id,
            "run_id": artifacts.result.run_id,
            "produced_artifacts": {
                "scope_path": str(scope_path),
                "report_path": str(artifacts.report_path),
            },
        }

    try:
        return execute_job(job_type="scope.run", scope_id=scope_id, runner=_runner)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/monitor-scopes/{scope_id}/report", response_model=Job, status_code=201)
def report_monitor_scope(scope_id: int, body: ReportScopeRequest):
    from web_listening.blocks.staged_workflow import report_scope as staged_report_scope

    normalized_format = (body.output_format or "md").strip().lower()
    if normalized_format not in {"md", "yaml"}:
        raise HTTPException(status_code=422, detail="output_format must be one of: md, yaml")

    def _runner(progress):
        progress.update(
            stage="loading_scope",
            stage_message="Resolving scope plan and report inputs.",
            progress=10,
        )
        scope_path = _resolve_scope_path(scope_id)
        resolved_task_path = str(_safe_input_path(body.task_path)) if body.task_path else None
        resolved_output_path = (
            str(_safe_output_path(body.output_path, default_path=settings.data_dir / "reports" / f"tracking_report_scope_{scope_id}.{normalized_format}"))
            if body.output_path
            else None
        )
        progress.update(
            status="running",
            stage="executing_workflow",
            stage_message="Building tracking report.",
            progress=55,
        )
        artifacts = staged_report_scope(
            scope_path=scope_path,
            task_path=resolved_task_path,
            run_id=body.run_id,
            output_path=resolved_output_path,
            output_format=normalized_format,
        )
        progress.update(
            stage="writing_artifacts",
            stage_message="Persisting tracking report artifacts.",
            progress=90,
        )
        serialized_report_payload = _serialize_report_payload(artifacts.report)
        report_run_id = getattr(artifacts.report, "run_id", None)
        if report_run_id is None and isinstance(serialized_report_payload, dict):
            report_run_id = serialized_report_payload.get("run_id")
        return {
            "scope_id": scope_id,
            "run_id": report_run_id,
            "produced_artifacts": {
                "scope_path": str(scope_path),
                "task_path": resolved_task_path or "",
                "output_path": str(artifacts.output_path),
                "output_format": artifacts.output_format,
                "report_payload": serialized_report_payload,
            },
        }

    try:
        return execute_job(job_type="scope.report", scope_id=scope_id, runner=_runner)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/monitor-scopes/{scope_id}/reports/latest", response_model=ArtifactEnvelope)
def get_latest_scope_report(scope_id: int):
    try:
        envelope = load_latest_scope_report_artifact_or_raise(db_path=settings.db_path, scope_id=scope_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ArtifactEnvelope(
        job=envelope.job,
        artifact_path=envelope.artifact_path,
        content=envelope.content,
        report_payload=envelope.report_payload,
    )


@router.get("/monitor-scopes/{scope_id}/manifest/latest", response_model=ArtifactEnvelope)
def get_latest_scope_manifest(scope_id: int):
    from web_listening.blocks.staged_workflow import export_manifest as staged_export_manifest

    try:
        envelope = load_latest_scope_manifest_artifact_or_create(
            db_path=settings.db_path,
            scope_id=scope_id,
            resolve_scope_path=_resolve_scope_path,
            export_manifest=staged_export_manifest,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ArtifactEnvelope(
        job=envelope.job,
        artifact_path=envelope.artifact_path,
        content=envelope.content,
        report_payload=envelope.report_payload,
    )


@router.post("/sites/{site_id}/check")
def check_site(site_id: int, background_tasks: BackgroundTasks):
    storage = get_storage()
    try:
        require_site_or_raise(storage, site_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        storage.close()

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
    try:
        require_site_or_raise(storage, site_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        storage.close()

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
