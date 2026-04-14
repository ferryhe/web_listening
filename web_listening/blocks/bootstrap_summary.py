from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from web_listening.blocks.monitor_scope_planner import MonitorScopePlan, load_monitor_scope_plan
from web_listening.blocks.scope_lookup import find_scope_for_plan
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, Site


@dataclass(slots=True)
class DirectorySummary:
    path: str
    pages: int = 0
    files: int = 0


@dataclass(slots=True)
class SourcePageSummary:
    page_url: str
    file_count: int
    sample_tracked_local_path: str = ""


@dataclass(slots=True)
class ScopeBootstrapSummary:
    generated_at: str
    display_name: str
    site_key: str
    catalog: str
    scope_id: int
    run_id: int
    run_status: str
    run_finished_at: str
    selected_roots: list[str] = field(default_factory=list)
    selected_focus_prefixes: list[str] = field(default_factory=list)
    page_count: int = 0
    file_count: int = 0
    coverage_page_count: int = 0
    coverage_file_count: int = 0
    truncated_by_budget: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    selected_but_low_coverage_prefixes: list[str] = field(default_factory=list)
    discovered_but_unselected_candidates: list[str] = field(default_factory=list)
    baseline_confidence: str = "unknown"
    recommended_followups: list[str] = field(default_factory=list)
    level1: list[DirectorySummary] = field(default_factory=list)
    level2: list[DirectorySummary] = field(default_factory=list)
    top_source_pages: list[SourcePageSummary] = field(default_factory=list)
    narrative: list[str] = field(default_factory=list)


def _normalize_prefix(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    while normalized != "/" and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized or "/"


def _path_levels(url: str) -> tuple[str, str]:
    path = (urlsplit(url).path or "/").strip("/")
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "/", "/"
    level1 = "/" + parts[0]
    level2 = "/" + "/".join(parts[:2]) if len(parts) >= 2 else level1
    return level1, level2


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = _normalize_prefix(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _covers(prefix: str, candidate: str) -> bool:
    normalized_prefix = _normalize_prefix(prefix)
    normalized_candidate = _normalize_prefix(candidate)
    if not normalized_prefix or not normalized_candidate:
        return False
    if normalized_prefix == "/":
        return True
    return normalized_candidate == normalized_prefix or normalized_candidate.startswith(normalized_prefix + "/")


def _count_for_prefix(prefix: str, page_urls: Iterable[str], file_source_urls: Iterable[str]) -> tuple[int, int]:
    page_count = 0
    file_count = 0
    for url in page_urls:
        if _covers(prefix, urlsplit(url).path or "/"):
            page_count += 1
    for url in file_source_urls:
        if _covers(prefix, urlsplit(url).path or "/"):
            file_count += 1
    return page_count, file_count


def _compute_truncation(scope: CrawlScope, run: CrawlRun, page_count: int, file_count: int) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    pages_seen = max(run.pages_seen or 0, page_count)
    files_seen = max(run.files_seen or 0, file_count)
    if scope.max_pages > 0 and pages_seen >= scope.max_pages:
        reasons.append(f"Reached page budget (`{pages_seen}` seen / max `{scope.max_pages}`).")
    if scope.max_files > 0 and files_seen >= scope.max_files:
        reasons.append(f"Reached file budget (`{files_seen}` seen / max `{scope.max_files}`).")
    return bool(reasons), reasons


def _compute_selected_low_coverage(
    selected_roots: list[str],
    selected_focus_prefixes: list[str],
    page_urls: list[str],
    file_source_urls: list[str],
) -> list[str]:
    low_coverage: list[str] = []
    for prefix in selected_roots + selected_focus_prefixes:
        page_count, file_count = _count_for_prefix(prefix, page_urls, file_source_urls)
        if page_count == 0 and file_count == 0:
            low_coverage.append(prefix)
    return _dedupe(low_coverage)


def _compute_unselected_candidates(selected_roots: list[str], selected_focus_prefixes: list[str], observed_level2: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    for path in observed_level2:
        if any(_covers(prefix, path) for prefix in selected_focus_prefixes):
            continue
        if not any(_covers(root, path) for root in selected_roots):
            continue
        candidates.append(path)
    return _dedupe(candidates)


def _compute_followups(
    *,
    truncated_by_budget: bool,
    truncation_reasons: list[str],
    selected_but_low_coverage_prefixes: list[str],
    discovered_but_unselected_candidates: list[str],
) -> list[str]:
    followups: list[str] = []
    if truncated_by_budget:
        followups.append(
            "Review crawl budget before trusting this baseline: " + "; ".join(truncation_reasons)
        )
    for prefix in selected_but_low_coverage_prefixes:
        followups.append(f"Recheck selected prefix `{prefix}` because the bootstrap run captured no pages or files there.")
    for prefix in discovered_but_unselected_candidates:
        followups.append(f"Review discovered branch `{prefix}` as a possible focus-prefix follow-up.")
    if not followups:
        followups.append("No immediate follow-up required; captured baseline evidence looks consistent with the selected scope.")
    return followups


def _baseline_confidence(
    *,
    truncated_by_budget: bool,
    selected_but_low_coverage_prefixes: list[str],
    discovered_but_unselected_candidates: list[str],
    page_count: int,
    file_count: int,
) -> str:
    if page_count == 0:
        return "low"
    if truncated_by_budget or selected_but_low_coverage_prefixes:
        return "low"
    if discovered_but_unselected_candidates or file_count == 0:
        return "medium"
    return "high"


def _render_directory_counts(paths: list[str], page_counts: Counter[str], file_counts: Counter[str]) -> list[DirectorySummary]:
    rows = [DirectorySummary(path=path, pages=page_counts.get(path, 0), files=file_counts.get(path, 0)) for path in paths]
    return rows


def _narrative_for_summary(
    display_name: str,
    page_count: int,
    file_count: int,
    level1_rows: list[DirectorySummary],
    level2_rows: list[DirectorySummary],
) -> list[str]:
    narrative: list[str] = []
    if level1_rows:
        top_pages = max(level1_rows, key=lambda item: (item.pages, item.files, item.path))
        if top_pages.pages > 0:
            narrative.append(
                f"`{display_name}` 这次基线主要页面密度集中在 `{top_pages.path}`，共抓到 `{top_pages.pages}` 个页面。"
            )
        top_files_l1 = max(level1_rows, key=lambda item: (item.files, item.pages, item.path))
        if top_files_l1.files > 0:
            narrative.append(
                f"按来源页一级目录看，文件最集中的是 `{top_files_l1.path}`，共发现 `{top_files_l1.files}` 个文件。"
            )
    heavy_l2 = [item for item in level2_rows if item.files > 0]
    if heavy_l2:
        top_files_l2 = max(heavy_l2, key=lambda item: (item.files, item.pages, item.path))
        narrative.append(
            f"按来源页二级目录看，文件最重的分支是 `{top_files_l2.path}`，共有 `{top_files_l2.files}` 个文件。"
        )
    narrative.append(
        f"本次总结基于 bootstrap run 的实际页面和文件来源页聚合，不是按静态 asset URL 路径聚合；总计页面 `{page_count}`、文件 `{file_count}`。"
    )
    return narrative


def summarize_monitor_scope_bootstrap(
    scope_path: str | Path,
    *,
    storage: Storage,
    run_id: int | None = None,
) -> ScopeBootstrapSummary:
    plan = load_monitor_scope_plan(scope_path)
    site, scope = find_scope_for_plan(storage, plan)
    resolved_run_id = run_id or scope.baseline_run_id
    if resolved_run_id is None:
        raise ValueError(f"Scope `{scope.id}` does not have a baseline run yet.")

    run = storage.get_crawl_run(resolved_run_id)
    if run is None:
        raise ValueError(f"Could not find crawl run `{resolved_run_id}`.")

    tracked_pages = {page.id: page for page in storage.list_tracked_pages(scope.id)}
    snapshots = storage.list_page_snapshots_for_run(scope.id, resolved_run_id)
    page_urls = [tracked_pages[snapshot.page_id].canonical_url for snapshot in snapshots if snapshot.page_id in tracked_pages]
    page_level1 = Counter()
    page_level2 = Counter()
    for url in page_urls:
        level1, level2 = _path_levels(url)
        page_level1[level1] += 1
        page_level2[level2] += 1

    file_observations = storage.list_file_observations(scope.id, run_id=resolved_run_id)
    file_level1 = Counter()
    file_level2 = Counter()
    source_pages = Counter()
    source_page_paths: dict[str, str] = {}
    file_source_urls: list[str] = []
    for observation in file_observations:
        page = tracked_pages.get(observation.page_id)
        if page is None:
            continue
        file_source_urls.append(page.canonical_url)
        level1, level2 = _path_levels(page.canonical_url)
        file_level1[level1] += 1
        file_level2[level2] += 1
        source_pages[page.canonical_url] += 1
        if observation.tracked_local_path and page.canonical_url not in source_page_paths:
            source_page_paths[page.canonical_url] = observation.tracked_local_path

    selected_roots = _dedupe(plan.allowed_page_prefixes)
    level1_paths = _dedupe(selected_roots + list(page_level1.keys()))
    focus_l2_paths = _dedupe(plan.selected_focus_prefixes)
    observed_l2_paths = [
        path
        for path, count in sorted(
            {**page_level2, **file_level2}.items(),
            key=lambda item: (-max(page_level2.get(item[0], 0), file_level2.get(item[0], 0)), item[0]),
        )
        if page_level2.get(path, 0) > 0 or file_level2.get(path, 0) > 0
    ]
    level2_paths = _dedupe(focus_l2_paths + observed_l2_paths)

    level1_rows = _render_directory_counts(level1_paths, page_level1, file_level1)
    level2_rows = _render_directory_counts(level2_paths, page_level2, file_level2)
    truncated_by_budget, truncation_reasons = _compute_truncation(scope, run, len(page_urls), len(file_observations))
    selected_but_low_coverage_prefixes = _compute_selected_low_coverage(
        selected_roots,
        focus_l2_paths,
        page_urls,
        file_source_urls,
    )
    discovered_but_unselected_candidates = _compute_unselected_candidates(
        selected_roots,
        focus_l2_paths,
        [row.path for row in level2_rows if row.pages > 0 or row.files > 0],
    )
    baseline_confidence = _baseline_confidence(
        truncated_by_budget=truncated_by_budget,
        selected_but_low_coverage_prefixes=selected_but_low_coverage_prefixes,
        discovered_but_unselected_candidates=discovered_but_unselected_candidates,
        page_count=len(page_urls),
        file_count=len(file_observations),
    )
    recommended_followups = _compute_followups(
        truncated_by_budget=truncated_by_budget,
        truncation_reasons=truncation_reasons,
        selected_but_low_coverage_prefixes=selected_but_low_coverage_prefixes,
        discovered_but_unselected_candidates=discovered_but_unselected_candidates,
    )
    top_source_pages = [
        SourcePageSummary(
            page_url=url,
            file_count=count,
            sample_tracked_local_path=source_page_paths.get(url, ""),
        )
        for url, count in source_pages.most_common(12)
    ]

    return ScopeBootstrapSummary(
        generated_at=datetime.now(timezone.utc).isoformat(),
        display_name=plan.display_name or site.name,
        site_key=plan.site_key,
        catalog=plan.catalog,
        scope_id=scope.id or 0,
        run_id=resolved_run_id,
        run_status=run.status,
        run_finished_at=run.finished_at.isoformat() if run.finished_at else "",
        selected_roots=selected_roots,
        selected_focus_prefixes=focus_l2_paths,
        page_count=len(page_urls),
        file_count=len(file_observations),
        coverage_page_count=len(page_urls),
        coverage_file_count=len(file_observations),
        truncated_by_budget=truncated_by_budget,
        truncation_reasons=truncation_reasons,
        selected_but_low_coverage_prefixes=selected_but_low_coverage_prefixes,
        discovered_but_unselected_candidates=discovered_but_unselected_candidates,
        baseline_confidence=baseline_confidence,
        recommended_followups=recommended_followups,
        level1=level1_rows,
        level2=level2_rows,
        top_source_pages=top_source_pages,
        narrative=_narrative_for_summary(
            plan.display_name or site.name,
            len(page_urls),
            len(file_observations),
            level1_rows,
            level2_rows,
        ),
    )


def render_markdown(summary: ScopeBootstrapSummary) -> str:
    lines = [
        "# Bootstrap Scope Summary",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{summary.generated_at}`",
        f"- Site: `{summary.display_name}` (`{summary.site_key}`)",
        f"- Catalog: `{summary.catalog}`",
        f"- Bootstrap run: scope_id=`{summary.scope_id}`, run_id=`{summary.run_id}`, status=`{summary.run_status}`, finished_at=`{summary.run_finished_at}`",
        f"- Totals: pages=`{summary.page_count}`, files=`{summary.file_count}`",
    ]
    for item in summary.narrative:
        lines.append(f"- {item}")

    lines.extend(
        [
            "",
            "## Baseline Quality",
            "",
            f"- Coverage page count: `{summary.coverage_page_count}`",
            f"- Coverage file count: `{summary.coverage_file_count}`",
            f"- Baseline confidence: `{summary.baseline_confidence}`",
            f"- Truncated by budget: `{'yes' if summary.truncated_by_budget else 'no'}`",
        ]
    )
    if summary.truncation_reasons:
        lines.append(f"- Truncation reasons: `{'; '.join(summary.truncation_reasons)}`")
    else:
        lines.append("- Truncation reasons: `-`")
    lines.append(
        f"- Selected but low coverage prefixes: `{', '.join(summary.selected_but_low_coverage_prefixes) or '-'}`"
    )
    lines.append(
        f"- Discovered but unselected candidates: `{', '.join(summary.discovered_but_unselected_candidates) or '-'}`"
    )
    lines.append("- Recommended followups:")
    for item in summary.recommended_followups:
        lines.append(f"  - {item}")

    lines.extend(["", "## Selected Roots", ""])
    for path in summary.selected_roots:
        lines.append(f"- `{path}`")

    if summary.selected_focus_prefixes:
        lines.extend(["", "## Selected Focus Branches", ""])
        for path in summary.selected_focus_prefixes:
            lines.append(f"- `{path}`")

    lines.extend(
        [
            "",
            "## Level-1 Coverage",
            "",
            "| Level-1 directory | Pages | Files from source pages |",
            "|---|---:|---:|",
        ]
    )
    for row in summary.level1:
        lines.append(f"| {row.path} | {row.pages} | {row.files} |")

    lines.extend(
        [
            "",
            "## Level-2 File Distribution",
            "",
            "| Level-2 directory | Pages | Files from source pages |",
            "|---|---:|---:|",
        ]
    )
    for row in summary.level2:
        lines.append(f"| {row.path} | {row.pages} | {row.files} |")

    if summary.top_source_pages:
        lines.extend(
            [
                "",
                "## Top File Source Pages",
                "",
                "| Source page | Files | Sample tracked path |",
                "|---|---:|---|",
            ]
        )
        for row in summary.top_source_pages:
            lines.append(f"| {row.page_url} | {row.file_count} | {row.sample_tracked_local_path or '-'} |")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- `Files from source pages` means files are grouped by the page where the crawler discovered them, not by the static asset URL path.",
            "- This makes the summary easier to read from a business-section perspective.",
            "",
        ]
    )
    return "\n".join(lines)
