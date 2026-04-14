from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(help="Web Listening - monitor websites for changes")
console = Console(width=200, soft_wrap=True)


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _validate_http_url(value: str, *, field_name: str) -> str:
    normalized_value = (value or "").strip()
    parsed = urlparse(normalized_value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise typer.BadParameter(f"{field_name} must be a valid http or https URL")
    return normalized_value


def _get_storage():
    from web_listening.blocks.storage import Storage
    from web_listening.config import settings
    return Storage(settings.db_path)


@app.command("add-site")
def add_site(
    url: str = typer.Argument(..., help="URL to monitor"),
    name: str = typer.Option("", "--name", "-n", help="Friendly name"),
    tags: str = typer.Option("", "--tags", "-t", help="Comma-separated tags"),
    fetch_mode: str = typer.Option("http", "--fetch-mode", help="Fetch mode: http, browser, auto"),
    fetch_config: str = typer.Option("", "--fetch-config", help="Fetch config as JSON"),
):
    """Add a website to monitor."""
    from web_listening.models import Site

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    if fetch_config:
        try:
            fetch_config_json = json.loads(fetch_config)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Invalid JSON for --fetch-config: {exc.msg}") from exc
    else:
        fetch_config_json = {}
    storage = _get_storage()
    site = storage.add_site(
        Site(
            url=url,
            name=name or url,
            tags=tag_list,
            fetch_mode=fetch_mode,
            fetch_config_json=fetch_config_json,
        )
    )
    storage.close()
    console.print(
        Panel(
            f"[green]Added site:[/green] [bold]{site.name}[/bold] (id={site.id})\n"
            f"{site.url}\nmode={site.fetch_mode}"
        )
    )


@app.command("list-sites")
def list_sites(all_sites: bool = typer.Option(False, "--all", help="Include inactive sites")):
    """List all monitored sites."""
    storage = _get_storage()
    sites = storage.list_sites(active_only=not all_sites)
    storage.close()

    table = Table(title="Monitored Sites")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("URL", style="blue")
    table.add_column("Fetch Mode")
    table.add_column("Tags")
    table.add_column("Last Checked")
    table.add_column("Active")

    for site in sites:
        table.add_row(
            str(site.id),
            site.name,
            site.url,
            site.fetch_mode,
            ", ".join(site.tags),
            str(site.last_checked_at)[:19] if site.last_checked_at else "-",
            "yes" if site.is_active else "no",
        )
    console.print(table)


@app.command("check")
def check(
    site_id: Optional[int] = typer.Option(None, "--site-id", help="Check specific site"),
):
    """Check sites for changes."""
    from web_listening.blocks.crawler import Crawler
    from web_listening.blocks.diff import compute_diff, find_document_links, find_new_links, select_compare_text
    from web_listening.models import Change

    storage = _get_storage()
    sites = [storage.get_site(site_id)] if site_id else storage.list_sites()
    sites = [s for s in sites if s is not None]

    if not sites:
        console.print("[yellow]No sites to check.[/yellow]")
        storage.close()
        return

    with Crawler() as crawler:
        for site in sites:
            console.print(f"Checking [bold]{site.name}[/bold] ({site.url})...")
            try:
                new_snap = crawler.snapshot(site)
                old_snap = storage.get_latest_snapshot(site.id)

                changes_detected = []

                if old_snap is None:
                    # First snapshot
                    storage.add_snapshot(new_snap)
                    storage.update_site_checked(site.id)
                    console.print(f"  [green]First snapshot captured[/green]")
                    continue

                # Check content change
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
                    change = storage.add_change(Change(
                        site_id=site.id,
                        detected_at=datetime.now(timezone.utc),
                        change_type="new_content",
                        summary=f"Content changed on {site.name}",
                        diff_snippet=diff_snippet,
                    ))
                    changes_detected.append(change)

                # Check new links
                new_links = find_new_links(old_snap.links, new_snap.links)
                if new_links:
                    change = storage.add_change(Change(
                        site_id=site.id,
                        detected_at=datetime.now(timezone.utc),
                        change_type="new_links",
                        summary=f"{len(new_links)} new links found on {site.name}",
                        diff_snippet="\n".join(new_links[:10]),
                    ))
                    changes_detected.append(change)

                # Check new document links
                doc_links = find_document_links(new_links)
                if doc_links:
                    change = storage.add_change(Change(
                        site_id=site.id,
                        detected_at=datetime.now(timezone.utc),
                        change_type="new_document",
                        summary=f"{len(doc_links)} new document links on {site.name}",
                        diff_snippet="\n".join(doc_links[:10]),
                    ))
                    changes_detected.append(change)

                storage.add_snapshot(new_snap)
                storage.update_site_checked(site.id)

                if changes_detected:
                    console.print(f"  [yellow]{len(changes_detected)} change(s) detected[/yellow]")
                else:
                    console.print(f"  [green]No changes[/green]")

            except Exception as e:
                console.print(f"  [red]Error: {e}[/red]")

    storage.close()


@app.command("list-changes")
def list_changes(
    site_id: Optional[int] = typer.Option(None, "--site-id"),
    since: Optional[str] = typer.Option(None, "--since", help="ISO date, e.g. 2024-01-01"),
):
    """Show recorded changes."""
    from dateutil import parser as dtparser

    storage = _get_storage()
    since_dt = dtparser.parse(since) if since else None
    changes = storage.list_changes(site_id=site_id, since=since_dt)
    storage.close()

    table = Table(title="Changes")
    table.add_column("ID", style="cyan")
    table.add_column("Site ID")
    table.add_column("Type", style="yellow")
    table.add_column("Detected At")
    table.add_column("Summary")

    for c in changes:
        table.add_row(
            str(c.id),
            str(c.site_id),
            c.change_type,
            str(c.detected_at)[:19] if c.detected_at else "-",
            c.summary[:80],
        )
    console.print(table)


@app.command("download-docs")
def download_docs(
    site_id: int = typer.Option(..., "--site-id"),
    institution: str = typer.Option(..., "--institution"),
    url: Optional[str] = typer.Option(None, "--url", help="Specific document URL"),
):
    """Download documents from a site."""
    from web_listening.blocks.document import DocumentProcessor

    storage = _get_storage()
    site = storage.get_site(site_id)
    if not site:
        console.print(f"[red]Site {site_id} not found[/red]")
        storage.close()
        return

    urls_to_download = []
    if url:
        urls_to_download = [url]
    else:
        snap = storage.get_latest_snapshot(site_id)
        if snap:
            from web_listening.blocks.diff import find_document_links
            urls_to_download = find_document_links(snap.links)

    if not urls_to_download:
        console.print("[yellow]No document URLs found.[/yellow]")
        storage.close()
        return

    with DocumentProcessor(storage=storage) as proc:
        for doc_url in urls_to_download:
            console.print(f"Downloading: {doc_url}")
            try:
                doc = proc.process(doc_url, site_id=site_id, institution=institution, page_url=site.url)
                saved = storage.add_document(doc)
                console.print(f"  [green]Saved as id={saved.id}[/green] -> {saved.local_path}")
            except Exception as e:
                console.print(f"  [red]Error: {e}[/red]")

    storage.close()


@app.command("list-docs")
def list_docs(institution: Optional[str] = typer.Option(None, "--institution")):
    """List downloaded documents."""
    storage = _get_storage()
    docs = storage.list_documents(institution=institution)
    storage.close()

    table = Table(title="Documents")
    table.add_column("ID", style="cyan")
    table.add_column("Institution")
    table.add_column("Title")
    table.add_column("Type")
    table.add_column("Downloaded At")
    table.add_column("URL", style="blue")

    for d in docs:
        table.add_row(
            str(d.id),
            d.institution,
            d.title[:40],
            d.doc_type,
            str(d.downloaded_at)[:19] if d.downloaded_at else "-",
            d.url[:60],
        )
    console.print(table)


@app.command("analyze")
def analyze(
    since: Optional[str] = typer.Option(None, "--since", help="ISO date for period start"),
):
    """Run AI analysis on recent changes."""
    from web_listening.blocks.analyzer import Analyzer
    from dateutil import parser as dtparser

    storage = _get_storage()
    period_end = datetime.now(timezone.utc)
    period_start = dtparser.parse(since) if since else datetime(
        period_end.year, period_end.month, period_end.day, tzinfo=timezone.utc
    )
    # default to last 7 days
    if not since:
        from datetime import timedelta
        period_start = period_end - timedelta(days=7)

    changes = storage.list_changes(since=period_start)
    analyzer = Analyzer()
    report = analyzer.analyze_changes(changes, period_start, period_end)
    saved = storage.add_analysis(report)
    storage.close()

    console.print(Panel(
        f"[bold]Analysis Report[/bold] (id={saved.id})\n"
        f"Period: {period_start.date()} -> {period_end.date()}\n"
        f"Changes: {saved.change_count}\n\n"
        + saved.summary_md,
        title="Analysis",
    ))


@app.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """Start the FastAPI server."""
    import uvicorn

    uvicorn.run("web_listening.api.app:app", host=host, port=port, reload=False)


@app.command("discover")
def discover(
    catalog: str = typer.Option("dev", "--catalog", help="Target catalog: dev, smoke, or all."),
    site_key: list[str] = typer.Option(None, "--site-key", help="Limit discovery to one or more site keys."),
    max_depth: int = typer.Option(3, "--max-depth", help="Shallow discovery depth."),
    section_depth: int = typer.Option(3, "--section-depth", help="Section aggregation depth."),
    max_pages: Optional[int] = typer.Option(None, "--max-pages", help="Optional page safety cap."),
    detect_documents: bool = typer.Option(False, "--detect-documents", help="Also count document links during discovery."),
    level3_sample_limit: int = typer.Option(2, "--level3-sample-limit", help="Level-3 sampling limit per level-2 branch."),
    yaml_path: str = typer.Option("", "--yaml-path", help="Optional section inventory YAML output path."),
    report_path: str = typer.Option("", "--report-path", help="Optional section inventory Markdown report path."),
):
    """Discover staged workflow site sections and write inventory artifacts."""
    from web_listening.blocks.staged_workflow import discover_sections

    artifacts = discover_sections(
        catalog=catalog,
        site_keys={value.strip().lower() for value in site_key or [] if value.strip()} or None,
        discovery_depth=max_depth,
        section_depth=section_depth,
        max_pages=max_pages,
        detect_documents=detect_documents,
        level3_sample_limit=level3_sample_limit,
        yaml_path=yaml_path or None,
        report_path=report_path or None,
    )
    console.print(
        f"Saved section inventory\n"
        f"YAML: {artifacts.yaml_path}\n"
        f"Report: {artifacts.report_path}"
    )


@app.command("classify")
def classify(
    catalog: str = typer.Option("dev", "--catalog", help="Target catalog: dev, smoke, or all."),
    inventory_path: str = typer.Option("", "--inventory-path", help="Optional section inventory YAML path."),
    site_key: list[str] = typer.Option(None, "--site-key", help="Limit classification to one or more site keys."),
    use_ai: bool = typer.Option(False, "--use-ai", help="Use AI-assisted classification when configured."),
    yaml_path: str = typer.Option("", "--yaml-path", help="Optional section classification YAML output path."),
    report_path: str = typer.Option("", "--report-path", help="Optional section classification Markdown report path."),
):
    """Classify discovered sections and write classification artifacts."""
    from web_listening.blocks.staged_workflow import classify_sections

    artifacts = classify_sections(
        catalog=catalog,
        inventory_path=inventory_path or None,
        site_keys={value.strip().lower() for value in site_key or [] if value.strip()} or None,
        use_ai=use_ai,
        yaml_path=yaml_path or None,
        report_path=report_path or None,
    )
    console.print(
        f"Saved section classification\n"
        f"Inventory: {artifacts.inventory_path}\n"
        f"YAML: {artifacts.yaml_path}\n"
        f"Report: {artifacts.report_path}"
    )


@app.command("select")
def select(
    selection_path: str = typer.Option(..., "--selection-path", help="Path to a reviewed section selection artifact."),
):
    """Inspect a reviewed selection artifact and expose the chosen path clearly."""
    from web_listening.blocks.staged_workflow import inspect_selection

    summary = inspect_selection(selection_path=selection_path)
    console.print(
        f"Selection artifact ready\n"
        f"Path: {summary.selection_path}\n"
        f"site_key={summary.site_key} selected={summary.selected_sections} rejected={summary.rejected_sections} deferred={summary.deferred_sections}\n"
        f"review_status={summary.review_status}\n"
        f"business_goal={summary.business_goal or '-'}"
    )


@app.command("plan-scope")
def plan_scope(
    selection_path: str = typer.Option(..., "--selection-path", help="Path to section_selection.yaml."),
    classification_path: str = typer.Option("", "--classification-path", help="Optional override for section classification YAML."),
    file_scope_mode: str = typer.Option("site_root", "--file-scope-mode", help="File scope mode: site_root or selected_pages."),
    max_depth: Optional[int] = typer.Option(None, "--max-depth", help="Optional max_depth override."),
    max_pages: Optional[int] = typer.Option(None, "--max-pages", help="Optional max_pages override."),
    max_files: Optional[int] = typer.Option(None, "--max-files", help="Optional max_files override."),
    yaml_path: str = typer.Option("", "--yaml-path", help="Optional monitor scope YAML output path."),
    report_path: str = typer.Option("", "--report-path", help="Optional monitor scope Markdown report path."),
):
    """Compile a reviewed selection into a monitor scope artifact."""
    from web_listening.blocks.staged_workflow import plan_scope as staged_plan_scope

    artifacts = staged_plan_scope(
        selection_path=selection_path,
        classification_path=classification_path or None,
        file_scope_mode=file_scope_mode,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        yaml_path=yaml_path or None,
        report_path=report_path or None,
    )
    console.print(
        f"Saved monitor scope\n"
        f"Selection: {artifacts.selection_path}\n"
        f"YAML: {artifacts.yaml_path}\n"
        f"Report: {artifacts.report_path}"
    )


@app.command("bootstrap-scope")
def bootstrap_scope(
    scope_path: str = typer.Option(..., "--scope-path", help="Path to monitor_scope.yaml."),
    download_files: bool = typer.Option(False, "--download-files", help="Download accepted files during bootstrap."),
    refresh_existing: bool = typer.Option(False, "--refresh-existing", help="Refresh already initialized scopes."),
    max_depth: Optional[int] = typer.Option(None, "--max-depth", help="Optional max_depth override."),
    max_pages: Optional[int] = typer.Option(None, "--max-pages", help="Optional max_pages override."),
    max_files: Optional[int] = typer.Option(None, "--max-files", help="Optional max_files override."),
    report_path: str = typer.Option("", "--report-path", help="Optional bootstrap Markdown report path."),
    summary_path: str = typer.Option("", "--summary-path", help="Optional bootstrap summary Markdown path."),
    include_summary: bool = typer.Option(False, "--include-summary", help="Also export bootstrap scope summary markdown."),
):
    """Bootstrap a stored monitor scope into the tracking database."""
    from web_listening.blocks.job_orchestration import persist_job_result
    from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
    from web_listening.blocks.staged_workflow import bootstrap_scope as staged_bootstrap_scope

    plan = load_monitor_scope_plan(scope_path)
    started = datetime.now(timezone.utc)
    artifacts = staged_bootstrap_scope(
        scope_path=scope_path,
        download_files=download_files,
        refresh_existing=refresh_existing,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        report_path=report_path or None,
        summary_path=summary_path or None,
        include_summary=include_summary,
    )
    first = artifacts.results[0] if artifacts.results else None
    job = persist_job_result(
        job_type="scope.bootstrap",
        scope_id=first.scope_id if first else plan.scope_id,
        run_id=first.run_id if first else None,
        produced_artifacts={
            "scope_path": str(scope_path),
            "report_path": str(artifacts.report_path),
            **({"summary_path": str(artifacts.summary_path)} if artifacts.summary_path else {}),
        },
        accepted_at=started,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )
    extra_summary = f"\nSummary: {artifacts.summary_path}" if artifacts.summary_path else ""
    console.print(
        f"Bootstrap scope finished\n"
        f"Job ID: {job.job_id}\n"
        f"Scope: {scope_path}\n"
        f"Report: {artifacts.report_path}{extra_summary}\n"
        f"status={first.status if first else '-'} scope_id={first.scope_id if first else '-'} run_id={first.run_id if first else '-'}"
    )


@app.command("run-scope")
def run_scope(
    scope_path: str = typer.Option(..., "--scope-path", help="Path to monitor_scope.yaml."),
    download_files: bool = typer.Option(False, "--download-files", help="Download accepted files during incremental run."),
    max_depth: Optional[int] = typer.Option(None, "--max-depth", help="Optional max_depth override."),
    max_pages: Optional[int] = typer.Option(None, "--max-pages", help="Optional max_pages override."),
    max_files: Optional[int] = typer.Option(None, "--max-files", help="Optional max_files override."),
    report_path: str = typer.Option("", "--report-path", help="Optional incremental run report path."),
):
    """Run an initialized monitor scope incrementally."""
    from web_listening.blocks.job_orchestration import persist_job_result
    from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
    from web_listening.blocks.staged_workflow import run_scope as staged_run_scope

    plan = load_monitor_scope_plan(scope_path)
    started = datetime.now(timezone.utc)
    artifacts = staged_run_scope(
        scope_path=scope_path,
        download_files=download_files,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        report_path=report_path or None,
    )
    job = persist_job_result(
        job_type="scope.run",
        scope_id=artifacts.result.scope_id or plan.scope_id,
        run_id=artifacts.result.run_id,
        produced_artifacts={
            "scope_path": str(scope_path),
            "report_path": str(artifacts.report_path),
        },
        accepted_at=started,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )
    console.print(
        f"Run scope finished\n"
        f"Job ID: {job.job_id}\n"
        f"Scope: {scope_path}\n"
        f"Report: {artifacts.report_path}\n"
        f"status={artifacts.result.status} scope_id={artifacts.result.scope_id} run_id={artifacts.result.run_id}"
    )


@app.command("report-scope")
def report_scope(
    scope_path: str = typer.Option(..., "--scope-path", help="Path to monitor_scope.yaml."),
    task_path: str = typer.Option("", "--task-path", help="Optional monitor_task.yaml path."),
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Specific run id, defaults to baseline run."),
    output: str = typer.Option("", "--output", help="Optional explicit output path."),
    output_format: str = typer.Option("md", "--format", help="Output format: md or yaml."),
):
    """Export a tracking report for one monitor scope."""
    from web_listening.blocks.job_orchestration import persist_job_result
    from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
    from web_listening.blocks.staged_workflow import report_scope as staged_report_scope

    normalized_format = (output_format or "md").strip().lower()
    if normalized_format not in {"md", "yaml"}:
        raise typer.BadParameter("--format must be one of: md, yaml")

    plan = load_monitor_scope_plan(scope_path)
    started = datetime.now(timezone.utc)
    artifacts = staged_report_scope(
        scope_path=scope_path,
        task_path=task_path or None,
        run_id=run_id,
        output_path=output or None,
        output_format=normalized_format,
    )
    job = persist_job_result(
        job_type="scope.report",
        scope_id=plan.scope_id,
        run_id=artifacts.report.run_id,
        produced_artifacts={
            "scope_path": str(scope_path),
            "task_path": str(task_path) if task_path else "",
            "output_path": str(artifacts.output_path),
            "output_format": artifacts.output_format,
        },
        accepted_at=started,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )
    console.print(Panel(
        f"[green]Saved scope report[/green]\n"
        f"Job ID: {job.job_id}\n"
        f"Path: {artifacts.output_path}\n"
        f"Format: {artifacts.output_format}"
    ))


@app.command("export-manifest")
def export_manifest(
    scope_path: str = typer.Option(..., "--scope-path", help="Path to monitor_scope.yaml."),
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Specific run id, defaults to baseline run."),
    yaml_path: str = typer.Option("", "--yaml-path", help="Optional manifest YAML output path."),
    report_path: str = typer.Option("", "--report-path", help="Optional manifest Markdown report path."),
):
    """Export scope document manifest artifacts."""
    from web_listening.blocks.job_orchestration import persist_job_result
    from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
    from web_listening.blocks.staged_workflow import export_manifest as staged_export_manifest

    plan = load_monitor_scope_plan(scope_path)
    started = datetime.now(timezone.utc)
    artifacts = staged_export_manifest(
        scope_path=scope_path,
        run_id=run_id,
        yaml_path=yaml_path or None,
        report_path=report_path or None,
    )
    job = persist_job_result(
        job_type="scope.manifest",
        scope_id=plan.scope_id,
        run_id=artifacts.manifest.run_id,
        produced_artifacts={
            "scope_path": str(scope_path),
            "yaml_path": str(artifacts.yaml_path),
            "report_path": str(artifacts.report_path),
        },
        accepted_at=started,
        started_at=started,
        finished_at=datetime.now(timezone.utc),
    )
    console.print(Panel(
        f"[green]Saved scope manifest[/green]\n"
        f"Job ID: {job.job_id}\n"
        f"YAML: {artifacts.yaml_path}\n"
        f"Report: {artifacts.report_path}"
    ))


@app.command("list-jobs")
def list_jobs(
    scope_id: Optional[int] = typer.Option(None, "--scope-id", help="Limit to one stored crawl scope."),
    job_type: str = typer.Option("", "--job-type", help="Limit to one job type."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum jobs to display."),
):
    """List persisted staged-workflow jobs."""
    storage = _get_storage()
    try:
        jobs = storage.list_jobs(scope_id=scope_id, job_type=job_type or None, limit=limit)
    finally:
        storage.close()

    table = Table(title="Jobs")
    table.add_column("Job ID", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column("Scope ID")
    table.add_column("Run ID")
    table.add_column("Progress")
    table.add_column("Finished")

    for job in jobs:
        table.add_row(
            str(job.job_id),
            job.job_type,
            job.status,
            job.stage,
            str(job.scope_id) if job.scope_id is not None else "-",
            str(job.run_id) if job.run_id is not None else "-",
            f"{job.progress}%",
            str(job.finished_at)[:19] if job.finished_at else "-",
        )
    console.print(table)


@app.command("get-job")
def get_job(
    job_id: int = typer.Argument(..., help="Persisted job id."),
):
    """Show one persisted staged-workflow job and its artifact contract."""
    storage = _get_storage()
    try:
        job = storage.get_job(job_id)
    finally:
        storage.close()

    if job is None:
        raise typer.BadParameter(f"Job {job_id} not found")

    artifact_lines = [f"- {key}: {value}" for key, value in sorted(job.produced_artifacts.items())] or ["- <none>"]
    summary_lines = [f"- {key}: {value}" for key, value in sorted(job.artifact_summary.items())] or ["- <none>"]
    console.print(
        Panel(
            "\n".join(
                [
                    f"job_id={job.job_id}",
                    f"job_type={job.job_type}",
                    f"status={job.status}",
                    f"stage={job.stage}",
                    f"stage_message={job.stage_message or '-'}",
                    f"scope_id={job.scope_id if job.scope_id is not None else '-'}",
                    f"run_id={job.run_id if job.run_id is not None else '-'}",
                    f"progress={job.progress}",
                    f"error={job.error or '-'}",
                    f"error_code={job.error_code or '-'}",
                    f"retryable={job.is_retryable}",
                    f"next_action={job.next_recommended_action()}",
                    "artifact_summary:",
                    *summary_lines,
                    "artifacts:",
                    *artifact_lines,
                ]
            )
        )
    )


@app.command("create-monitor-task")
def create_monitor_task(
    task_name: str = typer.Option(..., "--task-name", help="Stable task name"),
    site_url: str = typer.Option(..., "--site-url", help="Target site URL"),
    task_description: str = typer.Option(..., "--task-description", help="Human-readable task description"),
    goal: str = typer.Option(..., "--goal", help="Primary monitoring goal"),
    focus_topics: str = typer.Option("", "--focus-topics", help="Comma-separated focus topics"),
    must_track_prefixes: str = typer.Option("", "--must-track-prefixes", help="Comma-separated path prefixes that should be tracked"),
    exclude_prefixes: str = typer.Option("", "--exclude-prefixes", help="Comma-separated excluded prefixes"),
    prefer_file_types: str = typer.Option("", "--prefer-file-types", help="Comma-separated preferred file types"),
    must_download_patterns: str = typer.Option("", "--must-download-patterns", help="Comma-separated required download patterns"),
    handoff_requirements: str = typer.Option("", "--handoff-requirements", help="Comma-separated downstream handoff requirements"),
    notes: str = typer.Option("", "--notes", help="Comma-separated task notes"),
    report_style: str = typer.Option("briefing", "--report-style", help="Report style name"),
    output: str = typer.Option("", "--output", help="Optional explicit output YAML path"),
):
    """Create a first-class monitor task artifact for agent/human workflows."""
    from web_listening.blocks.job_orchestration import persist_job_result
    from web_listening.blocks.monitor_task import build_default_task_path, build_monitor_task, render_yaml_text
    from web_listening.config import settings

    started = datetime.now(timezone.utc)
    normalized_site_url = _validate_http_url(site_url, field_name="site_url")
    task = build_monitor_task(
        task_name=task_name,
        site_url=normalized_site_url,
        task_description=task_description,
        goal=goal,
        focus_topics=_csv_list(focus_topics),
        must_track_prefixes=_csv_list(must_track_prefixes),
        exclude_prefixes=_csv_list(exclude_prefixes),
        prefer_file_types=_csv_list(prefer_file_types),
        must_download_patterns=_csv_list(must_download_patterns),
        handoff_requirements=_csv_list(handoff_requirements),
        notes=_csv_list(notes),
        report_style=report_style,
    )
    output_path = Path(output) if output else build_default_task_path(task_name, data_dir=settings.data_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_yaml_text(task), encoding="utf-8")
    job = persist_job_result(
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
    console.print(Panel(f"[green]Saved monitor task:[/green] {output_path}\nJob ID: {job.job_id}"))


@app.command("export-tracking-report")
def export_tracking_report(
    scope_path: str = typer.Option(..., "--scope-path", help="Path to monitor_scope.yaml"),
    task_path: str = typer.Option("", "--task-path", help="Optional path to monitor_task.yaml"),
    run_id: Optional[int] = typer.Option(None, "--run-id", help="Specific run id, defaults to baseline run"),
    output: str = typer.Option("", "--output", help="Optional explicit output report path"),
    output_format: str = typer.Option("md", "--format", help="Output format: md or yaml"),
):
    """Export a unified tracking report from a scope run and optional task artifact."""
    from web_listening.blocks.tracking_report import (
        build_default_report_path,
        build_tracking_report,
        render_markdown as render_tracking_markdown,
        render_yaml_text as render_tracking_yaml,
    )
    from web_listening.config import settings

    normalized_format = (output_format or "md").strip().lower()
    if normalized_format not in {"md", "yaml"}:
        raise typer.BadParameter("--format must be one of: md, yaml")

    storage = _get_storage()
    try:
        report = build_tracking_report(
            scope_path,
            storage=storage,
            run_id=run_id,
            task_path=task_path or None,
        )
    finally:
        storage.close()

    output_path = (
        Path(output)
        if output
        else build_default_report_path(report.site_key, format=normalized_format, data_dir=settings.data_dir)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = render_tracking_yaml(report) if normalized_format == "yaml" else render_tracking_markdown(report)
    output_path.write_text(payload, encoding="utf-8")
    console.print(Panel(f"[green]Saved tracking report:[/green] {output_path}"))


if __name__ == "__main__":
    app()
