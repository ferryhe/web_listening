from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(help="Web Listening - monitor websites for changes")
console = Console()


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


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
    from web_listening.blocks.monitor_task import build_default_task_path, build_monitor_task, render_yaml_text
    from web_listening.config import settings

    task = build_monitor_task(
        task_name=task_name,
        site_url=site_url,
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
    console.print(Panel(f"[green]Saved monitor task:[/green] {output_path}"))


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
