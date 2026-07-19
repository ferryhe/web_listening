from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from importlib import metadata

from web_listening.blocks.acquisition_evidence import load_acquisition_evidence, persisted_acquisition_evidence
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, load_monitor_scope_plan
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.section_discovery import render_yaml
from web_listening.blocks.storage import Storage
from web_listening.config import settings
from web_listening.models import CrawlRun, CrawlScope, Document, FileObservation, Site

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


def _artifact_base(*paths: str | Path | None) -> Path:
    absolute_parents = [Path(path).parent for path in paths if path is not None and Path(path).is_absolute()]
    if absolute_parents:
        return Path(os.path.commonpath([str(parent) for parent in absolute_parents]))
    data_dir = Path(settings.data_dir)
    return data_dir.parent if data_dir.name == "data" and not data_dir.is_absolute() else data_dir


def _relative_artifact_root(manifest_json_path: str | Path | None, artifact_base: Path) -> str:
    if manifest_json_path is None:
        return "."
    manifest_parent = Path(manifest_json_path).parent
    if not manifest_parent.is_absolute():
        manifest_parent = Path.cwd() / manifest_parent
    base = artifact_base if artifact_base.is_absolute() else Path.cwd() / artifact_base
    relative = os.path.relpath(base, manifest_parent)
    return "." if relative == "." else Path(relative).as_posix()


def _portable_path(path: str | Path | None, *, artifact_base: Path | None = None) -> str | None:
    if path is None:
        return None
    value = str(path).strip()
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        return candidate.as_posix()
    if artifact_base is not None:
        base = artifact_base if artifact_base.is_absolute() else Path.cwd() / artifact_base
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            pass
    parts = candidate.parts
    if "data" in parts:
        data_index = parts.index("data")
        return Path(*parts[data_index:]).as_posix()
    try:
        return candidate.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return candidate.name


def _portable_acquisition_evidence(evidence: dict | None, *, artifact_base: Path) -> dict | None:
    if evidence is None:
        return None
    payload = dict(evidence)
    raw_paths = evidence.get("input_paths")
    if isinstance(raw_paths, dict):
        payload["input_paths"] = {
            str(key): _portable_path(value, artifact_base=artifact_base) or ""
            for key, value in raw_paths.items()
        }
    return payload


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


def _run_manifest_state(status: str) -> str:
    normalized = (status or "").lower()
    if normalized == "completed":
        return "completed"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"queued", "running", "started"}:
        return "partial"
    return normalized or "partial"


def _item_id(scope_id: int | None, url: str | None, fallback: str) -> str:
    seed = url or fallback
    return "item-" + hashlib.sha256(f"{scope_id or 0}:{seed}".encode("utf-8")).hexdigest()[:16]


def _file_observation_item(
    observation: FileObservation,
    *,
    page_url: str | None,
    document: Document | None,
    run: CrawlRun,
    source_id: str,
    run_id_text: str,
    input_paths: list[str],
    extraction_method: str | None,
    generated_text: str,
) -> dict:
    file_url = observation.download_url or observation.discovered_url
    return {
        "item_id": _item_id(observation.scope_id, file_url, f"file-observation-{observation.id or 0}"),
        "item_type": "file_link",
        "url": file_url,
        "title": document.title if document else Path(file_url or f"file-{observation.id or 0}").name,
        "status": "new",
        "observed_at": _utc_iso(document.downloaded_at if document else None) or _utc_iso(run.finished_at) or generated_text,
        "provenance": {
            "source_id": source_id,
            "run_id": run_id_text,
            "input_artifacts": input_paths,
            "parent_item_id": _item_id(observation.scope_id, page_url, f"page-{observation.page_id}"),
            "observed_at": _utc_iso(document.downloaded_at if document else None) or _utc_iso(run.finished_at),
            "extraction_method": extraction_method,
        },
        "content_type": document.content_type if document else None,
        "http_status": None,
        "checksum": _checksum(document.sha256 if document else None),
        "metadata": {
            "document_id": document.id if document and document.id else None,
            "doc_type": document.doc_type if document else None,
            "page_url": page_url,
            "file_observation_id": observation.id or 0,
        },
    }


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
    acquisition_evidence: dict | None = None
    documents: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        if payload.get("acquisition_evidence") is None:
            payload.pop("acquisition_evidence", None)
        return payload


def build_scope_document_manifest(
    scope_path: str | Path,
    *,
    storage: Storage,
    run_id: int | None = None,
    acquisition_profile_path: str | Path | None = None,
    capture_attempt_path: str | Path | None = None,
    precomputed_acquisition_evidence: dict | None = None,
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
    acquisition_evidence = persisted_acquisition_evidence(
        storage, scope_id=scope.id or 0, run_id=resolved_run_id
    ) or precomputed_acquisition_evidence
    if acquisition_evidence is None and (acquisition_profile_path is not None or capture_attempt_path is not None):
        acquisition_evidence = load_acquisition_evidence(
            profile_path=acquisition_profile_path,
            capture_attempt_path=capture_attempt_path,
        )
        acquisition_evidence["source"] = "read_only_compatibility_input"

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
        acquisition_evidence=acquisition_evidence,
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

    if manifest.acquisition_evidence:
        latest_attempt = manifest.acquisition_evidence.get("latest_attempt") or {}
        profile = manifest.acquisition_evidence.get("profile") or {}
        lines.extend([
            "",
            "## Acquisition Evidence",
            "",
            f"- Profile: `{profile.get('site_key', '-') or '-'}`",
            f"- Attempts: `{len(manifest.acquisition_evidence.get('attempts') or [])}`",
            f"- Latest adapter: `{latest_attempt.get('adapter', '-') or '-'}`",
            f"- Latest status: `{latest_attempt.get('status', '-') or '-'}`",
            f"- Recommended next adapter: `{manifest.acquisition_evidence.get('recommended_next_adapter') or '-'}`",
            f"- Next action: `{manifest.acquisition_evidence.get('next_action') or '-'}`",
        ])

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
    acquisition_profile_path: str | Path | None = None,
    capture_attempt_path: str | Path | None = None,
    precomputed_acquisition_evidence: dict | None = None,
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
    documents_by_id = {document.id: document for document in documents if document.id is not None}
    file_observations = storage.list_file_observations(scope.id or 0, run_id=resolved_run_id)
    generated = generated_at or datetime.now(timezone.utc)
    generated_text = _utc_iso(generated) or ""
    run_id_text = f"run-{resolved_run_id}"
    source_id = plan.site_key or f"scope-{scope.id or 0}"
    artifact_base = _artifact_base(
        manifest_json_path,
        yaml_path,
        report_path,
        scope_path,
    )
    artifact_root = _relative_artifact_root(manifest_json_path, artifact_base)
    portable_scope_path = _portable_path(scope_path, artifact_base=artifact_base)
    portable_acquisition_profile_path = _portable_path(acquisition_profile_path, artifact_base=artifact_base)
    portable_capture_attempt_path = _portable_path(capture_attempt_path, artifact_base=artifact_base)
    portable_yaml_path = _portable_path(yaml_path, artifact_base=artifact_base)
    portable_report_path = _portable_path(report_path, artifact_base=artifact_base)
    portable_json_path = _portable_path(manifest_json_path, artifact_base=artifact_base)
    output_paths = [path for path in [portable_json_path, portable_yaml_path, portable_report_path] if path]
    input_paths = [path for path in [portable_scope_path, portable_acquisition_profile_path, portable_capture_attempt_path] if path]
    idempotency_key = f"{source_id}|{plan.scope_fingerprint}|{resolved_run_id}"
    extraction_method = scope.fetch_mode or plan.fetch_mode or None

    discovered_items: list[dict] = []
    downloaded_assets: list[dict] = []
    seen_items: set[str] = set()
    seen_assets: set[str] = set()
    for observation in file_observations:
        tracked_file = storage.get_tracked_file(observation.file_id)
        tracked_page = storage.get_tracked_page(observation.page_id)
        document_id = observation.document_id or (tracked_file.latest_document_id if tracked_file else None)
        document = documents_by_id.get(document_id) if document_id is not None else None
        page_url = tracked_page.canonical_url if tracked_page else None
        discovered_item = _file_observation_item(
            observation,
            page_url=page_url,
            document=document,
            run=run,
            source_id=source_id,
            run_id_text=run_id_text,
            input_paths=input_paths,
            extraction_method=extraction_method,
            generated_text=generated_text,
        )
        item_id = discovered_item["item_id"]
        if item_id not in seen_items:
            discovered_items.append(discovered_item)
            seen_items.add(item_id)
        if document is None:
            continue
        asset_key = document.sha256 or str(document.id or observation.id or item_id)
        if asset_key in seen_assets:
            continue
        preferred_path = document.preferred_display_path or document.local_path
        asset_id = f"sha256-{document.sha256[:16]}" if document.sha256 else f"document-{document.id or 0}"
        downloaded_assets.append(
            {
                "asset_id": asset_id,
                "source_item_id": item_id,
                "url": document.download_url or observation.download_url or document.url,
                "local_path": _portable_path(preferred_path, artifact_base=artifact_base) or "",
                "canonical_blob_path": _portable_path(document.local_path, artifact_base=artifact_base),
                "tracked_path": _portable_path(document.tracked_local_path or observation.tracked_local_path, artifact_base=artifact_base),
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
                    "extraction_method": extraction_method,
                },
            }
        )
        seen_assets.add(asset_key)

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

    acquisition_evidence = persisted_acquisition_evidence(
        storage, scope_id=scope.id or 0, run_id=resolved_run_id
    ) or precomputed_acquisition_evidence
    if acquisition_evidence is None and (acquisition_profile_path is not None or capture_attempt_path is not None):
        acquisition_evidence = load_acquisition_evidence(
            profile_path=acquisition_profile_path,
            capture_attempt_path=capture_attempt_path,
        )
        acquisition_evidence["source"] = "read_only_compatibility_input"
    acquisition_evidence = _portable_acquisition_evidence(acquisition_evidence, artifact_base=artifact_base)
    deprecated_manifest = build_scope_document_manifest(
        scope_path,
        storage=storage,
        run_id=resolved_run_id,
        acquisition_profile_path=acquisition_profile_path,
        capture_attempt_path=capture_attempt_path,
        precomputed_acquisition_evidence=acquisition_evidence,
    ).to_dict()

    payload = {
        "schema_version": SCHEMA_VERSION,
        "manifest_id": f"manifest-{source_id}-{resolved_run_id}",
        "generated_at": generated_text,
        "producer": {
            "name": "web-listening",
            "version": _producer_version(),
            "command": command or f"web-listening export-manifest --scope-path {portable_scope_path}",
            "contract_version": SCHEMA_VERSION,
        },
        "artifact_root": artifact_root,
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
            "selection_path": _portable_path(plan.based_on.get("selection_path") if plan.based_on else None, artifact_base=artifact_base),
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
            "state": _run_manifest_state(run.status),
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
                    "sha256": None,
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
            "scope_document_manifest": deprecated_manifest,
        },
        "metadata": {},
        "extensions": {},
    }
    if acquisition_evidence is not None:
        payload["extensions"]["acquisition"] = acquisition_evidence
    return payload


def render_web_listening_manifest_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
