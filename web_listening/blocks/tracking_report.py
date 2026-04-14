from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, load_monitor_scope_plan
from web_listening.blocks.monitor_task import DEFAULT_CHANGE_SEVERITY_RULES, load_monitor_task
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, FileObservation, PageSnapshot, Site, TrackedFile, TrackedPage


@dataclass(slots=True)
class TrackingReport:
    generated_at: str
    display_name: str
    site_key: str
    catalog: str
    scope_fingerprint: str = ""
    task_name: str = ""
    task_description: str = ""
    goal: str = ""
    focus_topics: list[str] = field(default_factory=list)
    prefer_file_types: list[str] = field(default_factory=list)
    selected_roots: list[str] = field(default_factory=list)
    selected_focus_prefixes: list[str] = field(default_factory=list)
    scope_id: int = 0
    run_id: int = 0
    run_type: str = ""
    run_status: str = ""
    run_started_at: str = ""
    run_finished_at: str = ""
    pages_seen: int = 0
    files_seen: int = 0
    pages_changed: int = 0
    files_changed: int = 0
    document_count: int = 0
    new_pages: list[dict[str, Any]] = field(default_factory=list)
    changed_pages: list[dict[str, Any]] = field(default_factory=list)
    missing_pages: list[dict[str, Any]] = field(default_factory=list)
    new_files: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    missing_files: list[dict[str, Any]] = field(default_factory=list)
    priority_summary: dict[str, Any] = field(default_factory=dict)
    review_queue: list[dict[str, Any]] = field(default_factory=list)
    artifact_index: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    recommended_next_actions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_key(value: str) -> str:
    """Return a filesystem-safe key: lowercase, only [a-z0-9-], no path separators or dot-dot segments."""
    key = str(value or "site").strip().lower()
    key = re.sub(r"[^a-z0-9]+", "-", key)
    key = key.strip("-") or "site"
    return key


def _find_scope_for_plan(storage: Storage, plan: MonitorScopePlan) -> tuple[Site, CrawlScope]:
    return find_scope_for_plan(storage, plan)


def _build_recommended_next_actions(*, run: CrawlRun, document_count: int, has_task: bool, review_queue: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    if run.status != "completed":
        actions.append("Inspect the failed run logs or rerun the scope before trusting any downstream interpretation.")
    if review_queue:
        actions.append("Work the review queue in priority order; it is already sorted using the monitor task severity rules when available.")
    if document_count > 0:
        actions.append("Open the preferred_display_path entries first; they are the best human/agent browsing paths for downloaded files.")
    if document_count == 0 and run.files_seen > 0:
        actions.append("The run saw file URLs but no persisted documents were linked into the manifest; verify download_files settings and file acceptance rules.")
    if has_task:
        actions.append("Compare the observed changes against the task goal, policy fields, and focus topics before escalating to downstream agents or alerts.")
    else:
        actions.append("Attach a monitor task artifact on the next run so future reports can explain why this scope matters.")
    return actions


def _severity_rank(severity: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get((severity or "").lower(), 4)


def _build_page_change_bundles(
    storage: Storage,
    scope_id: int,
    run_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tracked_pages = storage.list_tracked_pages(scope_id)
    current_snapshots = {snapshot.page_id: snapshot for snapshot in storage.list_page_snapshots_for_run(scope_id, run_id)}
    scope_snapshots = storage.list_scope_page_snapshots(scope_id)
    previous_snapshot_by_page: dict[int, PageSnapshot] = {}
    for snapshot in scope_snapshots:
        if snapshot.run_id >= run_id:
            continue
        previous_snapshot_by_page[snapshot.page_id] = snapshot
    new_pages: list[dict[str, Any]] = []
    changed_pages: list[dict[str, Any]] = []
    missing_pages: list[dict[str, Any]] = []

    for tracked_page in tracked_pages:
        snapshot = current_snapshots.get(tracked_page.id)
        if tracked_page.first_seen_run_id == run_id and snapshot is not None:
            new_pages.append(
                {
                    "url": tracked_page.canonical_url,
                    "page_id": tracked_page.id or 0,
                    "depth": tracked_page.depth,
                    "snapshot_id": snapshot.id or 0,
                    "content_hash": snapshot.content_hash,
                }
            )
            continue
        if tracked_page.last_seen_run_id == run_id and snapshot is not None:
            previous_snapshot = previous_snapshot_by_page.get(tracked_page.id or 0)
            if previous_snapshot is not None and previous_snapshot.content_hash != snapshot.content_hash:
                changed_pages.append(
                    {
                        "url": tracked_page.canonical_url,
                        "page_id": tracked_page.id or 0,
                        "depth": tracked_page.depth,
                        "snapshot_id": snapshot.id or 0,
                        "content_hash": snapshot.content_hash,
                        "previous_content_hash": previous_snapshot.content_hash,
                    }
                )
        if tracked_page.last_seen_run_id and tracked_page.last_seen_run_id < run_id and (tracked_page.miss_count > 0 or not tracked_page.is_active):
            missing_pages.append(
                {
                    "url": tracked_page.canonical_url,
                    "page_id": tracked_page.id or 0,
                    "depth": tracked_page.depth,
                    "last_seen_run_id": tracked_page.last_seen_run_id,
                    "miss_count": tracked_page.miss_count,
                }
            )

    return sorted(new_pages, key=lambda item: item["url"]), sorted(changed_pages, key=lambda item: item["url"]), sorted(missing_pages, key=lambda item: item["url"])


def _build_file_change_bundles(
    storage: Storage,
    scope_id: int,
    run_id: int,
    documents_by_id: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    tracked_files = storage.list_tracked_files(scope_id)
    observations = storage.list_file_observations(scope_id)
    observations_by_file: dict[int, list[FileObservation]] = {}
    for observation in observations:
        observations_by_file.setdefault(observation.file_id, []).append(observation)

    new_files: list[dict[str, Any]] = []
    changed_files: list[dict[str, Any]] = []
    missing_files: list[dict[str, Any]] = []

    for tracked_file in tracked_files:
        run_observations = [item for item in observations_by_file.get(tracked_file.id or 0, []) if item.run_id == run_id]
        current_observation = run_observations[-1] if run_observations else None
        current_document = documents_by_id.get(current_observation.document_id) if current_observation and current_observation.document_id else None
        if current_document is None and current_observation and current_observation.document_id:
            persisted_current = storage.get_document(current_observation.document_id)
            if persisted_current is not None:
                current_document = {
                    "document_id": persisted_current.id or 0,
                    "sha256": persisted_current.sha256,
                    "page_url": persisted_current.page_url,
                    "preferred_display_path": persisted_current.preferred_display_path,
                }
        current_row = {
            "url": tracked_file.canonical_url,
            "file_id": tracked_file.id or 0,
            "sha256": tracked_file.latest_sha256,
            "document_id": current_observation.document_id if current_observation and current_observation.document_id else 0,
            "page_url": current_document.get("page_url", "") if current_document else "",
            "preferred_display_path": current_document.get("preferred_display_path", "") if current_document else (current_observation.tracked_local_path if current_observation else ""),
        }
        if tracked_file.first_seen_run_id == run_id and current_observation is not None:
            new_files.append(current_row)
            continue
        if tracked_file.last_seen_run_id == run_id and current_observation is not None:
            previous_observations = [item for item in observations_by_file.get(tracked_file.id or 0, []) if item.run_id < run_id]
            previous_observation = previous_observations[-1] if previous_observations else None
            previous_sha = ""
            if previous_observation and previous_observation.document_id:
                previous_document = storage.get_document(previous_observation.document_id)
                previous_sha = previous_document.sha256 if previous_document is not None else ""
            if previous_sha and tracked_file.latest_sha256 and previous_sha != tracked_file.latest_sha256:
                changed_files.append(
                    {
                        **current_row,
                        "previous_sha256": previous_sha,
                    }
                )
        if tracked_file.last_seen_run_id and tracked_file.last_seen_run_id < run_id and (tracked_file.miss_count > 0 or not tracked_file.is_active):
            missing_files.append(
                {
                    "url": tracked_file.canonical_url,
                    "file_id": tracked_file.id or 0,
                    "last_seen_run_id": tracked_file.last_seen_run_id,
                    "miss_count": tracked_file.miss_count,
                }
            )

    return sorted(new_files, key=lambda item: item["url"]), sorted(changed_files, key=lambda item: item["url"]), sorted(missing_files, key=lambda item: item["url"])


def _build_priority_summary(
    *,
    task: Any,
    new_pages: list[dict[str, Any]],
    changed_pages: list[dict[str, Any]],
    missing_pages: list[dict[str, Any]],
    new_files: list[dict[str, Any]],
    changed_files: list[dict[str, Any]],
    missing_files: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if task is None:
        return {}, []

    rules = {**DEFAULT_CHANGE_SEVERITY_RULES, **dict(task.change_severity_rules)}
    queue: list[dict[str, Any]] = []
    bundles = [
        ("new_page", new_pages, "page"),
        ("changed_page", changed_pages, "page"),
        ("missing_page", missing_pages, "page"),
        ("new_file", new_files, "file"),
        ("changed_file", changed_files, "file"),
        ("missing_file", missing_files, "file"),
    ]
    for change_type, items, artifact_type in bundles:
        severity = rules.get(change_type, "medium")
        for item in items:
            queue.append(
                {
                    "severity": severity,
                    "change_type": change_type,
                    "artifact_type": artifact_type,
                    "url": item.get("url", ""),
                    "preferred_display_path": item.get("preferred_display_path", ""),
                    "reason": f"{change_type} matched monitor task severity `{severity}`.",
                }
            )
    queue.sort(key=lambda item: (_severity_rank(item["severity"]), item["change_type"], item["url"]))
    severity_counts: dict[str, int] = {}
    for item in queue:
        severity_counts[item["severity"]] = severity_counts.get(item["severity"], 0) + 1
    highest_priority = queue[0]["severity"] if queue else "none"
    return {
        "highest_priority": highest_priority,
        "severity_counts": severity_counts,
        "review_item_count": len(queue),
    }, queue


def _build_artifact_index(
    *,
    scope_path: str | Path,
    task_path: str | Path | None,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    artifacts = [
        {
            "kind": "monitor_scope",
            "label": Path(scope_path).name,
            "path": str(scope_path),
        }
    ]
    if task_path is not None:
        artifacts.append(
            {
                "kind": "monitor_task",
                "label": Path(task_path).name,
                "path": str(task_path),
            }
        )
    for document in documents:
        artifacts.append(
            {
                "kind": "document",
                "label": document.get("title", "") or document.get("download_url", ""),
                "path": document.get("preferred_display_path", "") or document.get("tracked_local_path", "") or document.get("local_path", ""),
                "url": document.get("download_url", ""),
            }
        )
    return artifacts


def build_default_report_path(site_key: str, *, format: str = "md", now: datetime | None = None, data_dir: str | Path = "data") -> Path:
    moment = now or datetime.now(timezone.utc)
    report_date = moment.date().isoformat()
    normalized_site_key = _safe_key(site_key)
    suffix = ".yaml" if format == "yaml" else ".md"
    return Path(data_dir) / "reports" / f"tracking_report_{normalized_site_key}_{report_date}{suffix}"


def build_tracking_report(
    scope_path: str | Path,
    *,
    storage: Storage,
    run_id: int | None = None,
    task_path: str | Path | None = None,
) -> TrackingReport:
    plan = load_monitor_scope_plan(scope_path)
    site, scope = _find_scope_for_plan(storage, plan)
    resolved_run_id = run_id or scope.baseline_run_id
    if resolved_run_id is None:
        raise ValueError(f"Scope `{scope.id}` does not have a baseline run yet.")

    run = storage.get_crawl_run(resolved_run_id)
    if run is None:
        raise ValueError(f"Could not find crawl run `{resolved_run_id}`.")
    if run.scope_id != scope.id:
        raise ValueError(
            f"Crawl run `{resolved_run_id}` belongs to scope `{run.scope_id}`, not monitor scope `{scope.id}`."
        )

    task = load_monitor_task(task_path) if task_path is not None else None
    if task is not None and task.site_url.rstrip("/") != plan.seed_url.rstrip("/"):
        raise ValueError(
            f"Monitor task site_url `{task.site_url}` does not match monitor scope seed_url `{plan.seed_url}`."
        )
    documents = storage.list_scope_documents(scope.id, run_id=resolved_run_id)
    document_rows = [
        {
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
        for document in documents
    ]
    documents_by_id = {row["document_id"]: row for row in document_rows}
    new_pages, changed_pages, missing_pages = _build_page_change_bundles(storage, scope.id, resolved_run_id)
    new_files, changed_files, missing_files = _build_file_change_bundles(storage, scope.id, resolved_run_id, documents_by_id)
    priority_summary, review_queue = _build_priority_summary(
        task=task,
        new_pages=new_pages,
        changed_pages=changed_pages,
        missing_pages=missing_pages,
        new_files=new_files,
        changed_files=changed_files,
        missing_files=missing_files,
    )

    goal = task.goal if task is not None else plan.business_goal
    notes = list(plan.notes)
    if task is not None:
        notes.extend(task.notes)

    return TrackingReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        display_name=plan.display_name or site.name,
        site_key=plan.site_key,
        catalog=plan.catalog,
        scope_fingerprint=plan.scope_fingerprint,
        task_name=task.task_name if task is not None else "",
        task_description=task.task_description if task is not None else "",
        goal=goal,
        focus_topics=list(task.focus_topics) if task is not None else [],
        prefer_file_types=list(task.prefer_file_types) if task is not None else [],
        selected_roots=list(plan.allowed_page_prefixes),
        selected_focus_prefixes=list(plan.selected_focus_prefixes),
        scope_id=scope.id or 0,
        run_id=resolved_run_id,
        run_type=run.run_type,
        run_status=run.status,
        run_started_at=run.started_at.isoformat() if run.started_at else "",
        run_finished_at=run.finished_at.isoformat() if run.finished_at else "",
        pages_seen=run.pages_seen,
        files_seen=run.files_seen,
        pages_changed=run.pages_changed,
        files_changed=run.files_changed,
        document_count=len(document_rows),
        new_pages=new_pages,
        changed_pages=changed_pages,
        missing_pages=missing_pages,
        new_files=new_files,
        changed_files=changed_files,
        missing_files=missing_files,
        priority_summary=priority_summary,
        review_queue=review_queue,
        artifact_index=_build_artifact_index(scope_path=scope_path, task_path=task_path, documents=document_rows),
        documents=document_rows,
        recommended_next_actions=_build_recommended_next_actions(
            run=run,
            document_count=len(document_rows),
            has_task=task is not None,
            review_queue=review_queue,
        ),
        notes=notes,
    )


def render_yaml_text(report: TrackingReport) -> str:
    return yaml.safe_dump(report.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False)


def _append_change_table(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(["", f"## {title}", ""])
    lines.append("| URL | Details |")
    lines.append("|---|---|")
    if not rows:
        lines.append("| - | - |")
        return
    for row in rows:
        details: list[str] = []
        for key in ("content_hash", "previous_content_hash", "sha256", "previous_sha256", "preferred_display_path", "miss_count"):
            if row.get(key):
                details.append(f"{key}={row[key]}")
        lines.append(f"| {row.get('url', '-') or '-'} | {'; '.join(details) or '-'} |")


def render_markdown(report: TrackingReport) -> str:
    lines = [
        "# Tracking Report",
        "",
        "## Overview",
        "",
        f"- Generated at: `{report.generated_at}`",
        f"- Site: `{report.display_name}` (`{report.site_key}`)",
        f"- Catalog: `{report.catalog}`",
        f"- Scope identity: scope_id=`{report.scope_id}`, fingerprint=`{report.scope_fingerprint}`",
        f"- Run: scope_id=`{report.scope_id}`, run_id=`{report.run_id}`, type=`{report.run_type}`, status=`{report.run_status}`",
        f"- Summary: pages_seen=`{report.pages_seen}`, files_seen=`{report.files_seen}`, pages_changed=`{report.pages_changed}`, files_changed=`{report.files_changed}`, documents=`{report.document_count}`",
    ]
    if report.goal:
        lines.append(f"- Goal: {report.goal}")

    if report.task_name:
        lines.extend([
            "",
            "## Task Context",
            "",
            f"- Task name: `{report.task_name}`",
            f"- Description: {report.task_description}",
        ])
        if report.focus_topics:
            lines.append(f"- Focus topics: `{', '.join(report.focus_topics)}`")
        if report.prefer_file_types:
            lines.append(f"- Preferred file types: `{', '.join(report.prefer_file_types)}`")

    lines.extend([
        "",
        "## Scope Context",
        "",
        f"- Selected roots: `{', '.join(report.selected_roots) or '-'}`",
        f"- Selected focus prefixes: `{', '.join(report.selected_focus_prefixes) or '-'}`",
        f"- Run started at: `{report.run_started_at or '-'}`",
        f"- Run finished at: `{report.run_finished_at or '-'}`",
    ])

    if report.priority_summary:
        lines.extend([
            "",
            "## Priority Summary",
            "",
            f"- Highest priority: `{report.priority_summary.get('highest_priority', 'none')}`",
            f"- Review items: `{report.priority_summary.get('review_item_count', 0)}`",
            f"- Severity counts: `{report.priority_summary.get('severity_counts', {})}`",
        ])

    _append_change_table(lines, "New Pages", report.new_pages)
    _append_change_table(lines, "Changed Pages", report.changed_pages)
    _append_change_table(lines, "Missing Pages", report.missing_pages)
    _append_change_table(lines, "New Files", report.new_files)
    _append_change_table(lines, "Changed Files", report.changed_files)
    _append_change_table(lines, "Missing Files", report.missing_files)

    lines.extend([
        "",
        "## Review Queue",
        "",
        "| Severity | Change | URL | Reason |",
        "|---|---|---|---|",
    ])
    if report.review_queue:
        for item in report.review_queue:
            lines.append(
                f"| {item['severity']} | {item['change_type']} | {item['url'] or '-'} | {item['reason']} |"
            )
    else:
        lines.append("| - | - | - | - |")

    lines.extend([
        "",
        "## Downloaded Documents",
        "",
        "| Title | Downloaded at | Preferred path | Source page | Download URL |",
        "|---|---|---|---|---|",
    ])
    for row in report.documents:
        lines.append(
            f"| {row['title'] or '-'} | {row['downloaded_at'] or '-'} | {row['preferred_display_path'] or '-'} | {row['page_url'] or '-'} | {row['download_url'] or '-'} |"
        )
    if not report.documents:
        lines.append("| - | - | - | - | - |")

    lines.extend([
        "",
        "## Artifact Index",
        "",
        "| Kind | Label | Path | URL |",
        "|---|---|---|---|",
    ])
    for item in report.artifact_index:
        lines.append(
            f"| {item.get('kind', '-')} | {item.get('label', '-') or '-'} | {item.get('path', '-') or '-'} | {item.get('url', '-') or '-'} |"
        )
    if not report.artifact_index:
        lines.append("| - | - | - | - |")

    lines.extend(["", "## Recommended Next Actions", ""])
    for action in report.recommended_next_actions:
        lines.append(f"- {action}")

    if report.notes:
        lines.extend(["", "## Notes", ""])
        for note in report.notes:
            lines.append(f"- {note}")

    lines.append("")
    return "\n".join(lines)
