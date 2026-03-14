from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(help="Web Listening - monitor websites for changes")
console = Console()


def _get_storage():
    from web_listening.blocks.storage import Storage
    from web_listening.config import settings
    return Storage(settings.db_path)


@app.command("add-site")
def add_site(
    url: str = typer.Argument(..., help="URL to monitor"),
    name: str = typer.Option("", "--name", "-n", help="Friendly name"),
    tags: str = typer.Option("", "--tags", "-t", help="Comma-separated tags"),
):
    """Add a website to monitor."""
    from web_listening.models import Site

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    storage = _get_storage()
    site = storage.add_site(Site(url=url, name=name or url, tags=tag_list))
    storage.close()
    console.print(Panel(f"[green]Added site:[/green] [bold]{site.name}[/bold] (id={site.id})\n{site.url}"))


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
    table.add_column("Tags")
    table.add_column("Last Checked")
    table.add_column("Active")

    for site in sites:
        table.add_row(
            str(site.id),
            site.name,
            site.url,
            ", ".join(site.tags),
            str(site.last_checked_at)[:19] if site.last_checked_at else "-",
            "✓" if site.is_active else "✗",
        )
    console.print(table)


@app.command("check")
def check(
    site_id: Optional[int] = typer.Option(None, "--site-id", help="Check specific site"),
):
    """Check sites for changes."""
    from web_listening.blocks.crawler import Crawler
    from web_listening.blocks.diff import compute_diff, find_document_links, find_new_links
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
                    storage._update_site_checked(site.id)
                    console.print(f"  [green]First snapshot captured[/green]")
                    continue

                # Check content change
                has_changed, diff_snippet = compute_diff(old_snap.content_text, new_snap.content_text)
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
                storage._update_site_checked(site.id)

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

    with DocumentProcessor() as proc:
        for doc_url in urls_to_download:
            console.print(f"Downloading: {doc_url}")
            try:
                doc = proc.process(doc_url, site_id=site_id, institution=institution, page_url=site.url)
                saved = storage.add_document(doc)
                console.print(f"  [green]Saved as id={saved.id}[/green] → {saved.local_path}")
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
        f"Period: {period_start.date()} → {period_end.date()}\n"
        f"Changes: {saved.change_count}\n\n"
        + saved.summary_md,
        title="Analysis",
    ))


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port"),
):
    """Start the FastAPI server."""
    import uvicorn
    from web_listening.api.app import app as fastapi_app  # noqa: F401

    uvicorn.run("web_listening.api.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
