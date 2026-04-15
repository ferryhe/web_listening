from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from web_listening.blocks.bootstrap_summary import render_markdown as render_bootstrap_summary_markdown
from web_listening.blocks.bootstrap_summary import summarize_monitor_scope_bootstrap
from web_listening.blocks.document_manifest import build_scope_document_manifest, render_markdown as render_manifest_markdown
from web_listening.blocks.document_manifest import render_yaml_text as render_manifest_yaml
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, load_monitor_scope_plan, load_section_selection
from web_listening.blocks.monitor_scope_planner import monitor_scope_to_tree_target
from web_listening.blocks.monitor_scope_planner import render_markdown as render_scope_markdown
from web_listening.blocks.monitor_scope_planner import render_yaml_text as render_scope_yaml
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.storage import Storage
from web_listening.blocks.tracking_report import build_default_report_path as build_tracking_report_path
from web_listening.blocks.tracking_report import build_tracking_report, render_markdown as render_tracking_report_markdown
from web_listening.blocks.tracking_report import render_yaml_text as render_tracking_report_yaml
from web_listening.blocks.tracking_report import set_report_output_path
from web_listening.blocks.tree_crawler import TreeCrawler
from web_listening.config import settings
from web_listening.models import CrawlScope
from web_listening.tree_defaults import PRODUCTION_TREE_LIMITS
from tools.bootstrap_site_tree import BootstrapResult, build_default_report_path as build_bootstrap_report_path
from tools.bootstrap_site_tree import render_markdown as render_bootstrap_run_markdown
from tools.bootstrap_site_tree import run_bootstrap
from tools.classify_site_sections import build_default_inventory_path, build_default_report_path as build_classification_report_path
from tools.classify_site_sections import build_default_yaml_path as build_classification_yaml_path
from tools.discover_site_sections import build_default_report_path as build_discovery_report_path
from tools.discover_site_sections import build_default_yaml_path as build_discovery_yaml_path
from tools.discover_site_sections import discover_sections as build_section_inventory
from tools.export_scope_document_manifest import build_default_report_path as build_manifest_report_path
from tools.export_scope_document_manifest import build_default_yaml_path as build_manifest_yaml_path
from tools.plan_monitor_scope import build_default_report_path as build_scope_report_path
from tools.plan_monitor_scope import build_default_yaml_path as build_scope_yaml_path
from tools.run_site_tree import RunResult, build_default_report_path as build_run_report_path
from tools.run_site_tree import render_markdown as render_run_markdown


@dataclass(slots=True)
class DiscoveryArtifacts:
    inventory: Any
    yaml_path: Path
    report_path: Path


@dataclass(slots=True)
class ClassificationArtifacts:
    classification: Any
    inventory_path: Path
    yaml_path: Path
    report_path: Path


@dataclass(slots=True)
class SelectionSummary:
    selection_path: Path
    site_key: str
    selected_sections: int
    rejected_sections: int
    deferred_sections: int
    review_status: str
    business_goal: str


@dataclass(slots=True)
class ScopePlanArtifacts:
    plan: Any
    selection_path: Path
    yaml_path: Path
    report_path: Path


@dataclass(slots=True)
class ScopeBootstrapArtifacts:
    plan: Any
    results: list[BootstrapResult]
    report_path: Path
    summary_path: Path | None = None


@dataclass(slots=True)
class ScopeRunArtifacts:
    plan: Any
    result: RunResult
    report_path: Path


@dataclass(slots=True)
class ScopeReportArtifacts:
    report: Any
    output_path: Path
    output_format: str


@dataclass(slots=True)
class ManifestArtifacts:
    manifest: Any
    yaml_path: Path
    report_path: Path


def discover_sections(
    *,
    catalog: str,
    site_keys: set[str] | None = None,
    discovery_depth: int = 3,
    section_depth: int = 3,
    max_pages: int | None = None,
    detect_documents: bool = False,
    level3_sample_limit: int = 2,
    yaml_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> DiscoveryArtifacts:
    inventory = build_section_inventory(
        catalog=catalog,
        site_keys=site_keys,
        discovery_depth=discovery_depth,
        section_depth=section_depth,
        max_pages=max_pages,
        detect_documents=detect_documents,
        level3_sample_limit=max(1, level3_sample_limit),
    )
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_discovery_yaml_path(catalog)
    resolved_report_path = Path(report_path) if report_path else build_discovery_report_path(catalog)
    resolved_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    from web_listening.blocks.section_discovery import render_markdown, render_yaml

    resolved_yaml_path.write_text(render_yaml(inventory.to_dict()), encoding="utf-8")
    resolved_report_path.write_text(render_markdown(inventory), encoding="utf-8")
    return DiscoveryArtifacts(inventory=inventory, yaml_path=resolved_yaml_path, report_path=resolved_report_path)


def classify_sections(
    *,
    catalog: str,
    inventory_path: str | Path | None = None,
    site_keys: set[str] | None = None,
    use_ai: bool = False,
    yaml_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> ClassificationArtifacts:
    from web_listening.blocks.section_classifier import SectionClassifier, load_section_inventory, render_markdown, render_yaml_text

    resolved_inventory_path = Path(inventory_path) if inventory_path else build_default_inventory_path(catalog)
    inventory = load_section_inventory(resolved_inventory_path)
    classifier = SectionClassifier()
    classification = classifier.classify_inventory(
        inventory,
        inventory_path=str(resolved_inventory_path),
        use_ai=use_ai,
        site_keys=site_keys,
    )
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_classification_yaml_path(catalog)
    resolved_report_path = Path(report_path) if report_path else build_classification_report_path(catalog)
    resolved_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_yaml_path.write_text(render_yaml_text(classification), encoding="utf-8")
    resolved_report_path.write_text(render_markdown(classification), encoding="utf-8")
    return ClassificationArtifacts(
        classification=classification,
        inventory_path=resolved_inventory_path,
        yaml_path=resolved_yaml_path,
        report_path=resolved_report_path,
    )


def inspect_selection(*, selection_path: str | Path) -> SelectionSummary:
    resolved_selection_path = Path(selection_path)
    selection = load_section_selection(resolved_selection_path)
    return SelectionSummary(
        selection_path=resolved_selection_path,
        site_key=selection.site_key,
        selected_sections=len(selection.selected_sections),
        rejected_sections=len(selection.rejected_sections),
        deferred_sections=len(selection.deferred_sections),
        review_status=selection.review_status,
        business_goal=selection.business_goal,
    )


def plan_scope(
    *,
    selection_path: str | Path,
    classification_path: str | Path | None = None,
    file_scope_mode: str = "site_root",
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
    yaml_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> ScopePlanArtifacts:
    resolved_selection_path = Path(selection_path)
    selection = load_section_selection(resolved_selection_path)
    plan = build_monitor_scope(
        resolved_selection_path,
        classification_path=classification_path,
        file_scope_mode=file_scope_mode,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
    )
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_scope_yaml_path(selection.site_key)
    resolved_report_path = Path(report_path) if report_path else build_scope_report_path(selection.site_key)
    resolved_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_yaml_path.write_text(render_scope_yaml(plan), encoding="utf-8")
    resolved_report_path.write_text(render_scope_markdown(plan), encoding="utf-8")
    return ScopePlanArtifacts(
        plan=plan,
        selection_path=resolved_selection_path,
        yaml_path=resolved_yaml_path,
        report_path=resolved_report_path,
    )


def bootstrap_scope(
    *,
    scope_path: str | Path,
    download_files: bool = False,
    refresh_existing: bool = False,
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
    report_path: str | Path | None = None,
    summary_path: str | Path | None = None,
    include_summary: bool = False,
) -> ScopeBootstrapArtifacts:
    plan = load_monitor_scope_plan(scope_path)
    report_catalog = f"scope_{plan.site_key}"
    effective_max_depth = max_depth or plan.max_depth or PRODUCTION_TREE_LIMITS.max_depth
    effective_max_pages = max_pages or plan.max_pages or PRODUCTION_TREE_LIMITS.max_pages
    effective_max_files = max_files or plan.max_files or PRODUCTION_TREE_LIMITS.max_files
    results = run_bootstrap(
        catalog=plan.catalog or "scope",
        max_depth=effective_max_depth,
        max_pages=effective_max_pages,
        max_files=effective_max_files,
        download_files=download_files,
        refresh_existing=refresh_existing,
        targets=[monitor_scope_to_tree_target(plan)],
    )
    markdown = render_bootstrap_run_markdown(
        results,
        catalog=report_catalog,
        max_depth=effective_max_depth,
        max_pages=effective_max_pages,
        max_files=effective_max_files,
        download_files=download_files,
        refresh_existing=refresh_existing,
    )
    resolved_report_path = Path(report_path) if report_path else build_bootstrap_report_path(report_catalog)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.write_text(markdown, encoding="utf-8")

    resolved_summary_path: Path | None = None
    if include_summary:
        resolved_summary_path = Path(summary_path) if summary_path else settings.data_dir / "reports" / f"bootstrap_scope_summary_{plan.site_key}_{datetime.now().astimezone().date().isoformat()}.md"
        storage = Storage(settings.db_path)
        try:
            summary = summarize_monitor_scope_bootstrap(scope_path, storage=storage, run_id=results[0].run_id if results else None)
        finally:
            storage.close()
        resolved_summary_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_summary_path.write_text(render_bootstrap_summary_markdown(summary), encoding="utf-8")

    return ScopeBootstrapArtifacts(plan=plan, results=results, report_path=resolved_report_path, summary_path=resolved_summary_path)


def run_scope(
    *,
    scope_path: str | Path,
    download_files: bool = False,
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
    report_path: str | Path | None = None,
) -> ScopeRunArtifacts:
    plan = load_monitor_scope_plan(scope_path)
    storage = Storage(settings.db_path)
    try:
        _, stored_scope = find_scope_for_plan(storage, plan)
        scoped_run = CrawlScope(
            **{
                **stored_scope.model_dump(),
                "max_depth": max_depth or plan.max_depth or stored_scope.max_depth,
                "max_pages": max_pages or plan.max_pages or stored_scope.max_pages,
                "max_files": max_files or plan.max_files or stored_scope.max_files,
            }
        )
        with TreeCrawler(storage=storage) as tree:
            crawl = tree.run_scope(scoped_run, institution=plan.display_name, download_files=download_files)
        result = RunResult(
            catalog=plan.catalog,
            site_key=plan.site_key,
            display_name=plan.display_name,
            seed_url=plan.seed_url,
            scope_id=crawl.scope.id,
            run_id=crawl.run.id,
            status=crawl.run.status,
            pages_seen=len(crawl.pages),
            files_seen=len(crawl.files),
            new_pages=len(crawl.new_pages),
            changed_pages=len(crawl.changed_pages),
            missing_pages=len(crawl.missing_pages),
            new_files=len(crawl.new_files),
            changed_files=len(crawl.changed_files),
            missing_files=len(crawl.missing_files),
            page_failures=len(crawl.page_failures),
            file_failures=len(crawl.file_failures),
            notes="",
        )
    finally:
        storage.close()

    resolved_report_path = Path(report_path) if report_path else build_run_report_path(f"scope_{plan.site_key}")
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.write_text(
        render_run_markdown(
            [result],
            catalog=f"scope_{plan.site_key}",
            max_depth=max_depth or plan.max_depth,
            max_pages=max_pages or plan.max_pages,
            max_files=max_files or plan.max_files,
            download_files=download_files,
        ),
        encoding="utf-8",
    )
    return ScopeRunArtifacts(plan=plan, result=result, report_path=resolved_report_path)


def report_scope(
    *,
    scope_path: str | Path,
    task_path: str | Path | None = None,
    run_id: int | None = None,
    output_path: str | Path | None = None,
    output_format: str = "md",
) -> ScopeReportArtifacts:
    normalized_format = (output_format or "md").strip().lower()
    if normalized_format not in {"md", "yaml"}:
        raise ValueError("output_format must be one of: md, yaml")
    storage = Storage(settings.db_path)
    try:
        report = build_tracking_report(scope_path, storage=storage, run_id=run_id, task_path=task_path)
    finally:
        storage.close()
    resolved_output_path = Path(output_path) if output_path else build_tracking_report_path(report.site_key, format=normalized_format, data_dir=settings.data_dir)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
    report = set_report_output_path(report, resolved_output_path)
    payload = render_tracking_report_yaml(report) if normalized_format == "yaml" else render_tracking_report_markdown(report)
    resolved_output_path.write_text(payload, encoding="utf-8")
    return ScopeReportArtifacts(report=report, output_path=resolved_output_path, output_format=normalized_format)


def export_manifest(
    *,
    scope_path: str | Path,
    run_id: int | None = None,
    yaml_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> ManifestArtifacts:
    plan = load_monitor_scope_plan(scope_path)
    storage = Storage(settings.db_path)
    try:
        manifest = build_scope_document_manifest(scope_path, storage=storage, run_id=run_id)
    finally:
        storage.close()
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_manifest_yaml_path(plan.site_key)
    resolved_report_path = Path(report_path) if report_path else build_manifest_report_path(plan.site_key)
    resolved_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_yaml_path.write_text(render_manifest_yaml(manifest), encoding="utf-8")
    resolved_report_path.write_text(render_manifest_markdown(manifest), encoding="utf-8")
    return ManifestArtifacts(manifest=manifest, yaml_path=resolved_yaml_path, report_path=resolved_report_path)
