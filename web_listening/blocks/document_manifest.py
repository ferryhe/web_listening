from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, load_monitor_scope_plan
from web_listening.blocks.section_discovery import render_yaml
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Document, Site


@dataclass(slots=True)
class ScopeDocumentManifest:
    generated_at: str
    display_name: str
    site_key: str
    catalog: str
    scope_id: int
    run_id: int
    run_status: str
    run_finished_at: str
    document_count: int = 0
    documents: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _find_scope_for_plan(storage: Storage, plan: MonitorScopePlan) -> tuple[Site, CrawlScope]:
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


def build_scope_document_manifest(
    scope_path: str | Path,
    *,
    storage: Storage,
    run_id: int | None = None,
) -> ScopeDocumentManifest:
    plan = load_monitor_scope_plan(scope_path)
    site, scope = _find_scope_for_plan(storage, plan)
    resolved_run_id = run_id or scope.baseline_run_id
    if resolved_run_id is None:
        raise ValueError(f"Scope `{scope.id}` does not have a baseline run yet.")

    run = storage.get_crawl_run(resolved_run_id)
    if run is None:
        raise ValueError(f"Could not find crawl run `{resolved_run_id}`.")

    documents = storage.list_scope_documents(scope.id, run_id=resolved_run_id)
    document_rows = [_document_row(document) for document in documents]
    notes = [
        "preferred_display_path uses tracked_local_path when present and falls back to local_path otherwise.",
        "tracked_local_path is the source-oriented browsing path; local_path remains the canonical SHA256 blob path.",
    ]

    return ScopeDocumentManifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        display_name=plan.display_name or site.name,
        site_key=plan.site_key,
        catalog=plan.catalog,
        scope_id=scope.id or 0,
        run_id=resolved_run_id,
        run_status=run.status,
        run_finished_at=run.finished_at.isoformat() if run.finished_at else "",
        document_count=len(document_rows),
        documents=document_rows,
        notes=notes,
    )


def _document_row(document: Document) -> dict:
    return {
        "document_id": document.id or 0,
        "title": document.title,
        "sha256": document.sha256,
        "downloaded_at": document.downloaded_at.isoformat() if document.downloaded_at else "",
        "local_path": document.local_path,
        "tracked_local_path": document.tracked_local_path,
        "preferred_display_path": document.preferred_display_path,
        "page_url": document.page_url,
        "download_url": document.download_url,
        "doc_type": document.doc_type,
        "content_type": document.content_type,
    }


def render_yaml_text(manifest: ScopeDocumentManifest) -> str:
    return render_yaml(manifest.to_dict())


def render_markdown(manifest: ScopeDocumentManifest) -> str:
    lines = [
        "# Scope Document Manifest",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{manifest.generated_at}`",
        f"- Site: `{manifest.display_name}` (`{manifest.site_key}`)",
        f"- Catalog: `{manifest.catalog}`",
        f"- Scope run: scope_id=`{manifest.scope_id}`, run_id=`{manifest.run_id}`, status=`{manifest.run_status}`, finished_at=`{manifest.run_finished_at}`",
        f"- Downloaded document observations: `{manifest.document_count}`",
        "",
        "## Manifest Columns",
        "",
        "- `sha256`: canonical content hash used for dedupe.",
        "- `local_path`: canonical SHA256 blob path.",
        "- `tracked_local_path`: source-oriented browsing path under `_tracked` when available.",
        "- `preferred_display_path`: tracked path first, then canonical blob path.",
        "- `page_url`: source page where the file was discovered.",
        "- `downloaded_at`: when this downloaded document record was stored.",
        "",
        "## Documents",
        "",
        "| SHA256 | Downloaded at | Preferred path | Source page | Download URL |",
        "|---|---|---|---|---|",
    ]
    for row in manifest.documents:
        lines.append(
            f"| {row['sha256'] or '-'} | {row['downloaded_at'] or '-'} | {row['preferred_display_path'] or '-'} | {row['page_url'] or '-'} | {row['download_url'] or '-'} |"
        )

    if manifest.notes:
        lines.extend(["", "## Notes", ""])
        for note in manifest.notes:
            lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)
