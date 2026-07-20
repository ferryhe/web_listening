from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import base64
import hashlib
from importlib import import_module
import json
from pathlib import Path
from typing import Any

from web_listening.blocks.bootstrap_summary import render_markdown as render_bootstrap_summary_markdown
from web_listening.blocks.bootstrap_summary import summarize_monitor_scope_bootstrap
from web_listening.blocks.document_manifest import build_scope_document_manifest, build_web_listening_manifest_v1
from web_listening.blocks.document_manifest import render_markdown as render_manifest_markdown
from web_listening.blocks.document_manifest import render_web_listening_manifest_json
from web_listening.blocks.document_manifest import render_yaml_text as render_manifest_yaml
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.monitor_scope_planner import build_monitor_scope, load_monitor_scope_plan, load_section_selection
from web_listening.blocks.monitor_scope_planner import monitor_scope_to_tree_target
from web_listening.blocks.monitor_scope_planner import render_markdown as render_scope_markdown
from web_listening.blocks.monitor_scope_planner import render_yaml_text as render_scope_yaml
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.section_discovery import CatalogSectionInventory, SectionDiscoverer
from web_listening.blocks.section_discovery import render_markdown as render_discovery_markdown
from web_listening.blocks.section_discovery import render_yaml as render_discovery_yaml
from web_listening.blocks.storage import Storage
from web_listening.blocks.tracking_report import build_default_report_path as build_tracking_report_path
from web_listening.blocks.tracking_report import build_tracking_report, render_markdown as render_tracking_report_markdown
from web_listening.blocks.tracking_report import render_yaml_text as render_tracking_report_yaml
from web_listening.blocks.tracking_report import set_report_output_path
from web_listening.blocks.tree_bootstrap_workflow import BootstrapResult, build_default_report_path as build_bootstrap_report_path
from web_listening.blocks.tree_bootstrap_workflow import render_markdown as render_bootstrap_run_markdown
from web_listening.blocks.tree_bootstrap_workflow import run_bootstrap
from web_listening.blocks.tree_crawler import TreeCrawler
from web_listening.blocks.tree_run_workflow import RunResult, build_default_report_path as build_run_report_path
from web_listening.blocks.tree_run_workflow import render_markdown as render_run_markdown
from web_listening.config import settings
from web_listening.models import CrawlScope
from web_listening.tree_defaults import PRODUCTION_TREE_LIMITS
from web_listening.tree_targets import filter_tree_targets, load_tree_targets


_OPTIONAL_BROWSER_RUNTIME_ERROR = "governed optional browser runtime unavailable"


def _preflight_optional_browser_runtimes(planned, *, importer=import_module) -> None:
    """Validate only optional browser runtimes selected by the governed plan."""
    try:
        if "browser_rendered" in planned:
            sync_api = importer("playwright.sync_api")
            context = sync_api.sync_playwright()
            driver = context.start()
            try:
                executable_path = getattr(getattr(driver, "chromium", None), "executable_path", None)
                if not isinstance(executable_path, str) or not Path(executable_path).is_file():
                    raise RuntimeError(_OPTIONAL_BROWSER_RUNTIME_ERROR)
            finally:
                driver.stop()
        if "cloakbrowser" in planned:
            cloakbrowser = importer("cloakbrowser")
            launch = getattr(cloakbrowser, "launch", None)
            if not callable(launch):
                raise RuntimeError(_OPTIONAL_BROWSER_RUNTIME_ERROR)
            browser = launch(headless=True)
            close = getattr(browser, "close", None)
            if not callable(close):
                raise RuntimeError(_OPTIONAL_BROWSER_RUNTIME_ERROR)
            close()
    except Exception as exc:
        raise RuntimeError(_OPTIONAL_BROWSER_RUNTIME_ERROR) from exc


def _portable_json(value):
    """Convert adapter metadata containers into plain JSON-compatible values."""
    def plain(item):
        if isinstance(item, dict):
            return {str(key): plain(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [plain(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)

    normalized = plain(value)
    if not isinstance(normalized, dict):
        raise TypeError("adapter metadata must be a mapping")
    # Nested governed contract models freeze JSON containers before the parent
    # revalidates them. Canonical strings retain their complete portable value.
    return {
        key: (json.dumps(child, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
              if isinstance(child, (dict, list)) else child)
        for key, child in normalized.items()
    }


def _compile_acquisition_gateway(plan, *, acquisition_profile_path=None, site_skill_root=None):
    """Resolve all governed authority before Storage (and therefore mutation) exists."""
    from web_listening.blocks.acquisition_execution_plan import compile_acquisition_execution_plan
    from web_listening.blocks.acquisition_gateway import GovernedAcquisitionGateway
    from web_listening.blocks.acquisition_profile import load_acquisition_profile
    from web_listening.executors.registry import ExecutorRegistry, default_preview_registry
    from datetime import datetime, timezone
    from web_listening.executors.http_wrapper import HttpAcquisitionAdapter
    from web_listening.executors.playwright_wrapper import BrowserAcquisitionAdapter
    from web_listening.executors.cloakbrowser_wrapper import CloakBrowserAcquisitionAdapter
    from web_listening.executors.browseract import BrowserActExecutor
    from web_listening.executors.subprocess_runner import SubprocessLimits
    from web_listening.executors.wrapper_protocol import result_from_fetch
    from web_listening.blocks.crawler import resolve_request_headers
    from web_listening.contracts import CaptureContent, CaptureResult
    from web_listening.site_skill_registry import resolve_site_skill_contract

    bindings = {"acquisition_profile_id", "site_skill_version", "site_skill_package_sha256",
                "site_skill_recipe_id", "site_skill_script_sha256", "executor_version"}
    based_on = getattr(plan, "based_on", {})
    if set(based_on).intersection(bindings) != bindings:
        raise ValueError("formal scope execution requires complete governed acquisition bindings")
    if acquisition_profile_path is None:
        raise ValueError("governed scope requires --acquisition-profile-path")
    profile = load_acquisition_profile(acquisition_profile_path, strict=True)
    skill = resolve_site_skill_contract(
        site_key=plan.site_key, version=str(based_on.get("site_skill_version", "")),
        package_sha256=str(based_on.get("site_skill_package_sha256", "")),
        root=site_skill_root)
    preview = default_preview_registry()
    compiled = compile_acquisition_execution_plan(plan, profile, skill, preview)
    if compiled.mode != "governed" or not compiled.steps:
        raise ValueError("formal scope execution requires a governed non-empty acquisition plan")

    class _Executor:
        def __init__(self, executor_id, adapter):
            self.executor_id, self._adapter = executor_id, adapter
            self._closed = False

        def execute(self, request):
            started = datetime.now(timezone.utc)
            if (
                self.executor_id == "web_http"
                and request.metadata.get("content_kind") == "document"
            ):
                crawler = getattr(self._adapter, "crawler", None)
                client = getattr(crawler, "client", None)
                if client is None:
                    raise RuntimeError("governed HTTP document executor is not byte-capable")
                response = client.get(
                    str(request.url), headers=resolve_request_headers(dict(request.config))
                )
                payload = response.content
                digest = hashlib.sha256(payload).hexdigest()
                return CaptureResult(
                    **request.model_dump(include={
                        "site_key", "site_skill_id", "site_skill_version",
                        "site_skill_digest", "recipe_id", "run_id", "scope_id",
                        "request_id", "executor_id",
                    }),
                    state="succeeded", started_at=started,
                    finished_at=datetime.now(timezone.utc), final_url=str(response.url),
                    status_code=response.status_code,
                    content=CaptureContent(
                        media_type=response.headers.get("content-type", "application/octet-stream"),
                        text=base64.b64encode(payload).decode("ascii"), sha256=digest,
                        metadata={"representation": "base64", "sha256_scope": "decoded-bytes"},
                    ),
                )
            page = self._adapter.capture(str(request.url), config=dict(request.config))
            page = replace(page, metadata_json=_portable_json(page.metadata_json))
            return result_from_fetch(request, page, started)

        def close(self):
            if self._closed:
                return
            self._closed = True
            crawler = getattr(self._adapter, "crawler", None)
            close = getattr(crawler, "close", None)
            if close is not None:
                close()

    adapters = {"web_http": HttpAcquisitionAdapter, "browser_rendered": BrowserAcquisitionAdapter,
                "cloakbrowser": CloakBrowserAcquisitionAdapter}
    planned = {str(step["executor_id"]) for step in compiled.steps}
    unavailable = planned - adapters.keys() - {"browseract"}
    if unavailable:
        raise ValueError(f"governed executor runtime unavailable: {', '.join(sorted(unavailable))}")
    _preflight_optional_browser_runtimes(planned)
    executors = {}
    try:
        for executor_id in planned:
            if executor_id == "browseract":
                step = next(item for item in compiled.steps if item["executor_id"] == executor_id)
                config = dict(step.get("config", {}))
                executable = config.pop("executable", None)
                if not isinstance(executable, str) or not executable:
                    raise ValueError("governed browseract runtime requires an executable")
                runtime = BrowserActExecutor(
                    executable, limits=SubprocessLimits(**dict(step["limits"])),
                )

                class _BrowserActRuntime:
                    executor_id = "browseract"

                    def execute(self, request):
                        return runtime.execute(request.model_copy(update={"config": config}))

                    def close(self):
                        close = getattr(runtime, "close", None)
                        if close is not None:
                            close()

                executors[executor_id] = _BrowserActRuntime()
            else:
                executors[executor_id] = _Executor(executor_id, adapters[executor_id]())
        registry = ExecutorRegistry(
            executors,
            metadata={executor_id: preview.metadata[executor_id] for executor_id in planned},
        )
        return GovernedAcquisitionGateway(compiled, registry)
    except BaseException:
        for executor in executors.values():
            try:
                executor.close()
            except BaseException:
                pass
        raise


def _safe_key(value: str) -> str:
    key = str(value or "site").strip().lower()
    key = key.replace("/", "-").replace("\\", "-")
    key = key.replace("..", "-")
    key = "-".join(part for part in key.split() if part)
    key = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in key)
    while "--" in key:
        key = key.replace("--", "-")
    return key.strip("-") or "site"


def _dated_output_path(*, folder: str, stem: str, suffix: str, now: datetime | None = None) -> Path:
    if now is None:
        moment = datetime.now().astimezone()
    else:
        local_tz = datetime.now().astimezone().tzinfo
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            now = now.replace(tzinfo=local_tz)
        moment = now.astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / folder / f"{stem}_{report_date}.{suffix}"


def build_default_inventory_path(catalog: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="plans", stem=f"section_inventory_{_safe_key(catalog)}", suffix="yaml", now=now)


def build_default_discovery_yaml_path(catalog: str, now: datetime | None = None) -> Path:
    return build_default_inventory_path(catalog, now)


def build_default_discovery_report_path(catalog: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="reports", stem=f"section_inventory_{_safe_key(catalog)}", suffix="md", now=now)


def build_default_classification_yaml_path(catalog: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="plans", stem=f"section_classification_{_safe_key(catalog)}", suffix="yaml", now=now)


def build_default_classification_report_path(catalog: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="reports", stem=f"section_classification_{_safe_key(catalog)}", suffix="md", now=now)


def build_default_scope_yaml_path(site_key: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="plans", stem=f"monitor_scope_{_safe_key(site_key)}", suffix="yaml", now=now)


def build_default_scope_report_path(site_key: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="reports", stem=f"monitor_scope_{_safe_key(site_key)}", suffix="md", now=now)


def build_default_manifest_yaml_path(site_key: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="plans", stem=f"document_manifest_{_safe_key(site_key)}", suffix="yaml", now=now)


def build_default_manifest_report_path(site_key: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="reports", stem=f"document_manifest_{_safe_key(site_key)}", suffix="md", now=now)


def build_default_manifest_json_path(site_key: str, now: datetime | None = None) -> Path:
    return _dated_output_path(folder="manifests", stem=f"web_listening_manifest_{_safe_key(site_key)}", suffix="json", now=now)


def build_section_inventory(
    *,
    catalog: str,
    site_keys: set[str] | None = None,
    discovery_depth: int = 3,
    section_depth: int = 3,
    max_pages: int | None = None,
    detect_documents: bool = False,
    level3_sample_limit: int = 2,
) -> CatalogSectionInventory:
    targets = filter_tree_targets(load_tree_targets(catalog), site_keys)
    generated_at = datetime.now(timezone.utc).isoformat()
    sites = []

    with SectionDiscoverer() as discoverer:
        for target in targets:
            sites.append(
                discoverer.discover_target(
                    site_key=target.site_key,
                    display_name=target.display_name,
                    seed_url=target.seed_url,
                    homepage_url=target.homepage_url,
                    fetch_mode=target.fetch_mode,
                    fetch_config_json=target.fetch_config_json,
                    allowed_page_prefixes=target.allowed_page_prefixes,
                    allowed_file_prefixes=target.allowed_file_prefixes,
                    discovery_depth=discovery_depth,
                    section_depth=section_depth,
                    max_pages=max_pages,
                    detect_documents=detect_documents,
                    level3_sample_limit=level3_sample_limit,
                    notes=target.notes,
                )
            )

    return CatalogSectionInventory(
        catalog=catalog,
        generated_at=generated_at,
        discovery_depth=discovery_depth,
        section_depth=section_depth,
        max_pages=max_pages or 0,
        page_limit_mode="unbounded" if max_pages is None else "bounded",
        discovery_mode="structure_only" if not detect_documents else "structure_plus_documents",
        discovery_strategy="adaptive_sections",
        detect_documents=detect_documents,
        level3_sample_limit=level3_sample_limit,
        sites=sites,
    )


def _first_defined(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


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
    manifest_json: dict
    manifest_json_path: Path


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
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_default_discovery_yaml_path(catalog)
    resolved_report_path = Path(report_path) if report_path else build_default_discovery_report_path(catalog)
    resolved_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)

    resolved_yaml_path.write_text(render_discovery_yaml(inventory.to_dict()), encoding="utf-8")
    resolved_report_path.write_text(render_discovery_markdown(inventory), encoding="utf-8")
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
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_default_classification_yaml_path(catalog)
    resolved_report_path = Path(report_path) if report_path else build_default_classification_report_path(catalog)
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
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_default_scope_yaml_path(selection.site_key)
    resolved_report_path = Path(report_path) if report_path else build_default_scope_report_path(selection.site_key)
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
    acquisition_profile_path: str | Path | None = None,
    site_skill_root: str | Path | None = None,
) -> ScopeBootstrapArtifacts:
    plan = load_monitor_scope_plan(scope_path)
    report_catalog = f"scope_{plan.site_key}"
    effective_max_depth = _first_defined(max_depth, plan.max_depth, PRODUCTION_TREE_LIMITS.max_depth)
    effective_max_pages = _first_defined(max_pages, plan.max_pages, PRODUCTION_TREE_LIMITS.max_pages)
    effective_max_files = _first_defined(max_files, plan.max_files, PRODUCTION_TREE_LIMITS.max_files)
    effective_plan = replace(
        plan, max_depth=effective_max_depth, max_pages=effective_max_pages,
        max_files=effective_max_files,
    )
    target = monitor_scope_to_tree_target(effective_plan)
    acquisition_gateway = _compile_acquisition_gateway(
        effective_plan, acquisition_profile_path=acquisition_profile_path,
        site_skill_root=site_skill_root)
    results = run_bootstrap(
        catalog=plan.catalog or "scope",
        max_depth=effective_max_depth,
        max_pages=effective_max_pages,
        max_files=effective_max_files,
        download_files=download_files,
        refresh_existing=refresh_existing,
        targets=[target],
        acquisition_gateway=acquisition_gateway,
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
    acquisition_profile_path: str | Path | None = None,
    site_skill_root: str | Path | None = None,
) -> ScopeRunArtifacts:
    plan = load_monitor_scope_plan(scope_path)
    effective_max_depth = _first_defined(max_depth, plan.max_depth)
    effective_max_pages = _first_defined(max_pages, plan.max_pages)
    effective_max_files = _first_defined(max_files, plan.max_files)
    effective_plan = replace(
        plan,
        max_depth=_first_defined(effective_max_depth, plan.max_depth),
        max_pages=_first_defined(effective_max_pages, plan.max_pages),
        max_files=_first_defined(effective_max_files, plan.max_files),
    )
    acquisition_gateway = _compile_acquisition_gateway(
        effective_plan, acquisition_profile_path=acquisition_profile_path,
        site_skill_root=site_skill_root)
    storage = None
    processor = None
    try:
        storage = Storage(settings.db_path)
        processor = DocumentProcessor(storage=storage) if download_files else None
        _, stored_scope = find_scope_for_plan(storage, plan)
        scoped_run = CrawlScope(
            **{
                **stored_scope.model_dump(),
                "max_depth": _first_defined(effective_max_depth, stored_scope.max_depth),
                "max_pages": _first_defined(effective_max_pages, stored_scope.max_pages),
                "max_files": _first_defined(effective_max_files, stored_scope.max_files),
            }
        )
        tree_kwargs = {
            "storage": storage,
            "document_processor": processor,
            "acquisition_gateway": acquisition_gateway,
        }
        with TreeCrawler(**tree_kwargs) as tree:
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
        cleanup_failures = []
        active_failure = __import__("sys").exc_info()[0] is not None
        for resource in (acquisition_gateway, processor, storage):
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    close()
                except BaseException as exc:
                    cleanup_failures.append(exc)
        if cleanup_failures and not active_failure:
            raise cleanup_failures[0]

    resolved_report_path = Path(report_path) if report_path else build_run_report_path(f"scope_{plan.site_key}")
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.write_text(
        render_run_markdown(
            [result],
            catalog=f"scope_{plan.site_key}",
            max_depth=effective_max_depth,
            max_pages=effective_max_pages,
            max_files=effective_max_files,
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
    acquisition_profile_path: str | Path | None = None,
    capture_attempt_path: str | Path | None = None,
) -> ScopeReportArtifacts:
    normalized_format = (output_format or "md").strip().lower()
    if normalized_format not in {"md", "yaml"}:
        raise ValueError("output_format must be one of: md, yaml")
    storage = Storage(settings.db_path)
    try:
        report = build_tracking_report(
            scope_path,
            storage=storage,
            run_id=run_id,
            task_path=task_path,
            acquisition_profile_path=acquisition_profile_path,
            capture_attempt_path=capture_attempt_path,
        )
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
    manifest_json_path: str | Path | None = None,
    acquisition_profile_path: str | Path | None = None,
    capture_attempt_path: str | Path | None = None,
) -> ManifestArtifacts:
    plan = load_monitor_scope_plan(scope_path)
    storage = Storage(settings.db_path)
    try:
        manifest = build_scope_document_manifest(
            scope_path,
            storage=storage,
            run_id=run_id,
            acquisition_profile_path=acquisition_profile_path,
            capture_attempt_path=capture_attempt_path,
        )
    finally:
        storage.close()
    resolved_yaml_path = Path(yaml_path) if yaml_path else build_default_manifest_yaml_path(plan.site_key)
    resolved_report_path = Path(report_path) if report_path else build_default_manifest_report_path(plan.site_key)
    resolved_json_path = Path(manifest_json_path) if manifest_json_path else build_default_manifest_json_path(plan.site_key)
    resolved_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_report_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_json_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_yaml_path.write_text(render_manifest_yaml(manifest), encoding="utf-8")
    resolved_report_path.write_text(render_manifest_markdown(manifest), encoding="utf-8")

    storage = Storage(settings.db_path)
    try:
        manifest_json = build_web_listening_manifest_v1(
            scope_path,
            storage=storage,
            run_id=run_id,
            yaml_path=resolved_yaml_path,
            report_path=resolved_report_path,
            manifest_json_path=resolved_json_path,
            acquisition_profile_path=acquisition_profile_path,
            capture_attempt_path=capture_attempt_path,
            precomputed_acquisition_evidence=manifest.acquisition_evidence,
        )
    finally:
        storage.close()
    resolved_json_path.write_text(render_web_listening_manifest_json(manifest_json), encoding="utf-8")
    return ManifestArtifacts(
        manifest=manifest,
        yaml_path=resolved_yaml_path,
        report_path=resolved_report_path,
        manifest_json=manifest_json,
        manifest_json_path=resolved_json_path,
    )
