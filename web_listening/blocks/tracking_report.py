from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, load_monitor_scope_plan
from web_listening.blocks.monitor_task import load_monitor_task
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Site


@dataclass(slots=True)
class TrackingReport:
    generated_at: str
    display_name: str
    site_key: str
    catalog: str
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


def _build_recommended_next_actions(*, run: CrawlRun, document_count: int, has_task: bool) -> list[str]:
    actions: list[str] = []
    if run.status != "completed":
        actions.append("Inspect the failed run logs or rerun the scope before trusting any downstream interpretation.")
    if run.pages_changed or run.files_changed:
        actions.append("Review the changed pages/files first and decide whether the baseline should be updated after validation.")
    if document_count > 0:
        actions.append("Open the preferred_display_path entries first; they are the best human/agent browsing paths for downloaded files.")
    if document_count == 0 and run.files_seen > 0:
        actions.append("The run saw file URLs but no persisted documents were linked into the manifest; verify download_files settings and file acceptance rules.")
    if has_task:
        actions.append("Compare the observed changes against the task goal and focus topics before escalating to downstream agents or alerts.")
    else:
        actions.append("Attach a monitor task artifact on the next run so future reports can explain why this scope matters.")
    return actions


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

    goal = task.goal if task is not None else plan.business_goal
    notes = list(plan.notes)
    if task is not None:
        notes.extend(task.notes)

    return TrackingReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        display_name=plan.display_name or site.name,
        site_key=plan.site_key,
        catalog=plan.catalog,
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
        documents=document_rows,
        recommended_next_actions=_build_recommended_next_actions(
            run=run,
            document_count=len(document_rows),
            has_task=task is not None,
        ),
        notes=notes,
    )


def render_yaml_text(report: TrackingReport) -> str:
    return yaml.safe_dump(report.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False)


def render_markdown(report: TrackingReport) -> str:
    lines = [
        "# Tracking Report",
        "",
        "## Overview",
        "",
        f"- Generated at: `{report.generated_at}`",
        f"- Site: `{report.display_name}` (`{report.site_key}`)",
        f"- Catalog: `{report.catalog}`",
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

    lines.extend(["", "## Recommended Next Actions", ""])
    for action in report.recommended_next_actions:
        lines.append(f"- {action}")

    if report.notes:
        lines.extend(["", "## Notes", ""])
        for note in report.notes:
            lines.append(f"- {note}")

    lines.append("")
    return "\n".join(lines)
