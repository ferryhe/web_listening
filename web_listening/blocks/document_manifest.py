from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from importlib import metadata

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, load_monitor_scope_plan
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.section_discovery import render_yaml
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Document, Site

SCHEMA_VERSION = "web-listening-manifest.v1"


def _utc_iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _portable_path(path: str | Path | None) -> str | None:
    if path is None:
        return None
    value = str(path).strip()
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return candidate.as_posix()
    parts = candidate.parts
    if "data" in parts:
        data_index = parts.index("data")
        return Path(*parts[data_index:]).as_posix()
    try:
        return candidate.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return candidate.name


def _sha256_file(path: str | Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_size(path: str | Path | None) -> int | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate.stat().st_size


def _checksum(value: str | None) -> dict[str, str] | None:
    return {"algorithm": "sha256", "value": value} if value else None


def _producer_version() -> str | None:
    for package_name in ("web-listening", "web_listening"):
        try:
            return metadata.version(package_name)
        except metadata.PackageNotFoundError:
            continue
    return None


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


def build_scope_document_manifest(
    scope_path: str | Path,
    *,
    storage: Storage,
    run_id: int | None = None,
) -> ScopeDocumentManifest:
    plan = load_monitor_scope_plan(scope_path)
    site, scope = find_scope_for_plan(storage, plan)
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


def build_web_listening_manifest_v1(
    scope_path: str | Path,
    *,
    storage: Storage,
    run_id: int | None = None,
    yaml_path: str | Path | None = None,
    report_path: str | Path | None = None,
    manifest_json_path: str | Path | None = None,
    generated_at: datetime | None = None,
    command: str | None = None,
) -> dict:
    """Build the reviewed web-listening-manifest.v1 JSON payload.

    The existing ScopeDocumentManifest YAML/Markdown exports remain compatibility
    artifacts. This function builds the stable downstream handoff envelope.
    """
    plan = load_monitor_scope_plan(scope_path)
    site, scope = find_scope_for_plan(storage, plan)
    resolved_run_id = run_id or scope.baseline_run_id
    if resolved_run_id is None:
        raise ValueError(f"Scope `{scope.id}` does not have a baseline run yet.")
    run = storage.get_crawl_run(resolved_run_id)
    if run is None:
        raise ValueError(f"Could not find crawl run `{resolved_run_id}`.")

    documents = storage.list_scope_documents(scope.id or 0, run_id=resolved_run_id)
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = _utc_iso(generated) or ""
    run_id_text = f"run-{resolved_run_id}"
    source_id = plan.site_key or f"scope-{scope.id or 0}"
    portable_scope_path = _portable_path(scope_path)
    portable_yaml_path = _portable_path(yaml_path)
    portable_report_path = _portable_path(report_path)
    portable_json_path = _portable_path(manifest_json_path)
    output_paths = [path for path in [portable_json_path, portable_yaml_path, portable_report_path] if path]
    input_paths = [path for path in [portable_scope_path] if path]
    idempotency_key = f"{source_id}|{plan.scope_fingerprint}|{resolved_run_id}"

    discovered_items: list[dict] = []
    downloaded_assets: list[dict] = []
    seen_items: set[str] = set()
    for document in documents:
        item_url = document.page_url or document.download_url or document.url
        item_seed = item_url or f"document-{document.id or 0}"
        item_id = "item-" + hashlib.sha256(f"{scope.id}:{item_seed}".encode("utf-8")).hexdigest()[:16]
        if item_id not in seen_items:
            discovered_items.append(
                {
                    "item_id": item_id,
                    "item_type": "file_link" if document.download_url else "page",
                    "url": item_url,
                    "title": document.title or None,
                    "status": "new",
                    "observed_at": _utc_iso(document.downloaded_at) or _utc_iso(run.finished_at) or generated_text,
                    "provenance": {
                        "source_id": source_id,
                        "run_id": run_id_text,
                        "input_artifacts": input_paths,
                        "parent_item_id": None,
                        "observed_at": _utc_iso(document.downloaded_at) or _utc_iso(run.finished_at),
                        "extraction_method": scope.fetch_mode or plan.fetch_mode or None,
                    },
                    "content_type": document.content_type or None,
                    "http_status": None,
                    "checksum": _checksum(document.sha256),
                    "metadata": {
                        "document_id": document.id or 0,
                        "doc_type": document.doc_type or None,
                    },
                }
            )
            seen_items.add(item_id)
        preferred_path = document.preferred_display_path or document.local_path
        asset_id = f"sha256-{document.sha256[:16]}" if document.sha256 else f"document-{document.id or 0}"
        downloaded_assets.append(
            {
                "asset_id": asset_id,
                "source_item_id": item_id,
                "url": document.download_url or document.url,
                "local_path": _portable_path(preferred_path) or "",
                "canonical_blob_path": _portable_path(document.local_path),
                "tracked_path": _portable_path(document.tracked_local_path),
                "filename": Path(preferred_path or document.download_url or document.url or f"document-{document.id or 0}").name,
                "media_type": document.content_type or None,
                "bytes": _file_size(preferred_path) or _file_size(document.local_path),
                "checksum": _checksum(document.sha256),
                "status": "downloaded",
                "provenance": {
                    "source_id": source_id,
                    "run_id": run_id_text,
                    "input_artifacts": input_paths,
                    "parent_item_id": item_id,
                    "observed_at": _utc_iso(document.downloaded_at) or _utc_iso(run.finished_at),
                    "extraction_method": scope.fetch_mode or plan.fetch_mode or None,
                },
            }
        )

    artifact_provenance = {
        "source_id": source_id,
        "run_id": run_id_text,
        "input_artifacts": input_paths,
        "parent_item_id": None,
        "observed_at": _utc_iso(run.finished_at),
        "extraction_method": "export_manifest",
    }
    compatibility_exports = []
    if portable_yaml_path:
        compatibility_exports.append(
            {
                "artifact_id": "document-manifest-yaml",
                "kind": "document_manifest_yaml",
                "path": portable_yaml_path,
                "media_type": "application/x-yaml",
                "sha256": _sha256_file(yaml_path),
                "created_at": generated_text,
                "provenance": artifact_provenance,
            }
        )
    if portable_report_path:
        compatibility_exports.append(
            {
                "artifact_id": "document-manifest-markdown",
                "kind": "document_manifest_markdown",
                "path": portable_report_path,
                "media_type": "text/markdown",
                "sha256": _sha256_file(report_path),
                "created_at": generated_text,
                "provenance": artifact_provenance,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": f"manifest-{source_id}-{resolved_run_id}",
        "generated_at": generated_text,
        "producer": {
            "name": "web-listening",
            "version": _producer_version(),
            "command": command or f"web-listening export-manifest --scope-path {portable_scope_path}",
            "contract_version": SCHEMA_VERSION,
        },
        "artifact_root": ".",
        "run": {
            "run_id": run_id_text,
            "run_type": "export_manifest",
            "started_at": _utc_iso(run.started_at),
            "completed_at": _utc_iso(run.finished_at),
            "input_paths": input_paths,
            "output_paths": output_paths,
            "idempotency_key": idempotency_key,
            "parent_run_id": str(resolved_run_id),
            "scope_path": portable_scope_path,
            "selection_path": _portable_path(plan.based_on.get("selection_path") if plan.based_on else None),
            "parameters": {
                "scope_fingerprint": plan.scope_fingerprint,
                "fetch_mode": scope.fetch_mode or plan.fetch_mode,
                "max_depth": scope.max_depth,
                "max_pages": scope.max_pages,
                "max_files": scope.max_files,
            },
        },
        "job": None,
        "source": {
            "source_id": source_id,
            "site_url": plan.homepage_url or plan.seed_url or site.url,
            "site_name": plan.display_name or site.name,
            "scope_profile": plan.tree_budget_profile or plan.file_scope_mode or None,
            "tree_seed_url": scope.seed_url or plan.seed_url or None,
            "tree_page_prefixes": scope.allowed_page_prefixes or plan.allowed_page_prefixes,
            "tree_file_prefixes": scope.allowed_file_prefixes or plan.allowed_file_prefixes,
            "catalog_key": plan.catalog or None,
        },
        "status": {
            "state": "completed" if run.status == "completed" else "partial",
            "stage": "export_manifest",
            "counts": {
                "discovered_items": len(discovered_items),
                "downloaded_assets": len(downloaded_assets),
                "changed_items": run.pages_changed + run.files_changed,
                "warnings": 0,
                "errors": 1 if run.error_message else 0,
            },
            "message": "Manifest exported for downstream document conversion.",
        },
        "artifacts": {
            "reports": [entry for entry in compatibility_exports if entry["kind"] == "document_manifest_markdown"],
            "structured_exports": [
                {
                    "artifact_id": "web-listening-manifest-json",
                    "kind": "web_listening_manifest_json",
                    "path": portable_json_path,
                    "media_type": "application/json",
                    "sha256": _sha256_file(manifest_json_path),
                    "created_at": generated_text,
                    "provenance": artifact_provenance,
                }
            ]
            if portable_json_path
            else [],
            "compatibility_exports": compatibility_exports,
        },
        "discovered_items": discovered_items,
        "downloaded_assets": downloaded_assets,
        "provenance": {
            "source_id": source_id,
            "run_id": run_id_text,
            "input_artifacts": input_paths,
            "parent_item_id": None,
            "observed_at": _utc_iso(run.finished_at),
            "extraction_method": "export_manifest",
        },
        "errors": [
            {
                "error_id": f"run-{resolved_run_id}-error",
                "severity": "error",
                "stage": run.run_type,
                "message": run.error_message,
                "item_id": None,
                "url": None,
                "retryable": True,
            }
        ]
        if run.error_message
        else [],
        "deprecated": {
            "scope_document_manifest": build_scope_document_manifest(scope_path, storage=storage, run_id=resolved_run_id).to_dict(),
        },
        "metadata": {},
        "extensions": {},
    }


def render_web_listening_manifest_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
