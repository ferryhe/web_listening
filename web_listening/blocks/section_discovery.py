from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import urlsplit

from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import extract_links, find_document_links
from web_listening.blocks.polite import PolitePacer
from web_listening.blocks.tree_crawler import (
    build_scope_from_site,
    canonicalize_tracked_url,
    is_file_url_in_scope,
    is_page_url_in_scope,
    sanitize_request_url,
)
from web_listening.models import CrawlScope, Site

_SAMPLE_LIMIT = 4


@dataclass(slots=True)
class SectionSummary:
    section_path: str
    section_level: int
    page_count: int
    page_with_docs_count: int
    doc_link_count: int
    child_section_count: int = 0
    sample_urls: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    candidate_category: str = "general_reference"


@dataclass(slots=True)
class ExpansionCandidate:
    branch_path: str
    candidate_category: str
    sampled_pages: int
    discovered_candidate_pages: int
    skipped_candidate_pages: int
    reason: str


@dataclass(slots=True)
class SiteSectionInventory:
    site_key: str
    display_name: str
    seed_url: str
    homepage_url: str
    fetch_mode: str
    allowed_page_prefixes: list[str]
    allowed_file_prefixes: list[str]
    discovery_depth: int
    section_depth: int
    max_pages: int
    pages_discovered: int
    pages_with_docs: int
    unique_document_links: int
    page_limit_mode: str = "unbounded"
    discovery_mode: str = "structure_only"
    discovery_strategy: str = "adaptive_sections"
    detect_documents: bool = False
    level3_sample_limit: int = 2
    level2_pages_discovered: int = 0
    sampled_level3_pages: int = 0
    skipped_level3_candidate_pages: int = 0
    skipped_external_pages: int = 0
    skipped_external_files: int = 0
    skipped_duplicate_pages: int = 0
    page_failures: list[str] = field(default_factory=list)
    sample_pages: list[str] = field(default_factory=list)
    sections: list[SectionSummary] = field(default_factory=list)
    expansion_candidates: list[ExpansionCandidate] = field(default_factory=list)
    notes: str = ""


@dataclass(slots=True)
class CatalogSectionInventory:
    catalog: str
    generated_at: str
    discovery_depth: int
    section_depth: int
    max_pages: int
    sites: list[SiteSectionInventory] = field(default_factory=list)
    page_limit_mode: str = "unbounded"
    discovery_mode: str = "structure_only"
    discovery_strategy: str = "adaptive_sections"
    detect_documents: bool = False
    level3_sample_limit: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _SectionAggregate:
    section_path: str
    section_level: int
    page_urls: set[str] = field(default_factory=set)
    page_urls_with_docs: set[str] = field(default_factory=set)
    doc_urls: set[str] = field(default_factory=set)
    sample_urls: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)


def _count_child_sections(section_path: str, section_paths: set[str]) -> int:
    prefix = section_path.rstrip("/")
    if not prefix:
        return 0
    child_sections: set[str] = set()
    for candidate in section_paths:
        if candidate == section_path or not candidate.startswith(prefix + "/"):
            continue
        remainder = candidate[len(prefix) + 1 :]
        if remainder and "/" not in remainder:
            child_sections.add(candidate)
    return len(child_sections)


def _extract_title(page) -> str:
    headings = page.metadata_json.get("headings", []) if isinstance(page.metadata_json, dict) else []
    for heading in headings:
        value = str(heading).strip()
        if value:
            return value[:160]
    for raw_line in (page.fit_markdown or page.markdown or page.content_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        if line:
            return line[:160]
    return ""


def _append_unique(values: list[str], candidate: str, *, limit: int = _SAMPLE_LIMIT) -> None:
    value = str(candidate or "").strip()
    if not value or value in values or len(values) >= limit:
        return
    values.append(value)


def _section_prefixes(url: str, *, max_section_depth: int) -> list[str]:
    path = (urlsplit(url).path or "/").strip("/")
    if not path:
        return []
    parts = [part for part in path.split("/") if part]
    prefixes: list[str] = []
    for level in range(1, min(max_section_depth, len(parts)) + 1):
        prefixes.append("/" + "/".join(parts[:level]))
    return prefixes


def _path_level(url: str) -> int:
    path = (urlsplit(url).path or "/").strip("/")
    if not path:
        return 0
    return len([part for part in path.split("/") if part])


def _path_prefix(url: str, *, level: int) -> str:
    path = (urlsplit(url).path or "/").strip("/")
    if not path:
        return "/"
    parts = [part for part in path.split("/") if part]
    if not parts:
        return "/"
    return "/" + "/".join(parts[: min(level, len(parts))])


def _candidate_category(section_path: str, sample_titles: list[str]) -> str:
    haystack = f"{section_path} {' '.join(sample_titles)}".lower()
    if any(token in haystack for token in ("exam-req", "/exam", "exams-admissions", "syllabus", "credential", "pass mark", "vee")):
        return "exam_education"
    if any(token in haystack for token in ("research", "publication", "paper", "journal", "working-paper")):
        return "research_publications"
    if any(token in haystack for token in ("governance", "board", "leadership", "election", "bylaws", "policy", "guideline", "committee", "council", "annual-report")):
        return "governance_management"
    if any(token in haystack for token in ("finance", "financial", "budget", "statement", "accounting", "audit")):
        return "finance_reports"
    if any(token in haystack for token in ("membership", "member", "directory", "sponsorship", "advertising", "contact")):
        return "membership_operations"
    if any(token in haystack for token in ("news", "article", "announcement", "press", "congress")):
        return "news_announcements"
    return "general_reference"


def _level_sections(site: SiteSectionInventory, *, level: int) -> list[SectionSummary]:
    return [section for section in site.sections if section.section_level == level]


def render_yaml(data: dict[str, Any]) -> str:
    def _scalar(value: Any) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, (int, float)):
            return str(value)
        return json.dumps(str(value), ensure_ascii=True)

    def _emit(value: Any, indent: int) -> list[str]:
        prefix = " " * indent
        if isinstance(value, dict):
            if not value:
                return [f"{prefix}{{}}"]
            lines: list[str] = []
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    if isinstance(item, list) and not item:
                        lines.append(f"{prefix}{key}: []")
                    elif isinstance(item, dict) and not item:
                        lines.append(f"{prefix}{key}: {{}}")
                    else:
                        lines.append(f"{prefix}{key}:")
                        lines.extend(_emit(item, indent + 2))
                else:
                    lines.append(f"{prefix}{key}: {_scalar(item)}")
            return lines
        if isinstance(value, list):
            if not value:
                return [f"{prefix}[]"]
            lines = []
            for item in value:
                if isinstance(item, (dict, list)):
                    if isinstance(item, dict) and item:
                        first_key = next(iter(item))
                        first_value = item[first_key]
                        if isinstance(first_value, (dict, list)):
                            lines.append(f"{prefix}- {first_key}:")
                            lines.extend(_emit(first_value, indent + 4))
                        else:
                            lines.append(f"{prefix}- {first_key}: {_scalar(first_value)}")
                        remaining = dict(list(item.items())[1:])
                        if remaining:
                            lines.extend(_emit(remaining, indent + 2))
                    elif isinstance(item, list) and item:
                        lines.append(f"{prefix}-")
                        lines.extend(_emit(item, indent + 2))
                    else:
                        lines.append(f"{prefix}- {{}}")
                else:
                    lines.append(f"{prefix}- {_scalar(item)}")
            return lines
        return [f"{prefix}{_scalar(value)}"]

    return "\n".join(_emit(data, 0)) + "\n"


class SectionDiscoverer:
    def __init__(self, *, crawler: Crawler | None = None):
        self.crawler = crawler or Crawler()
        self._owns_crawler = crawler is None

    def close(self) -> None:
        if self._owns_crawler:
            self.crawler.close()

    def __enter__(self) -> "SectionDiscoverer":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def discover_target(
        self,
        *,
        site_key: str,
        display_name: str,
        seed_url: str,
        homepage_url: str,
        fetch_mode: str,
        fetch_config_json: dict,
        allowed_page_prefixes: list[str],
        allowed_file_prefixes: list[str],
        discovery_depth: int,
        section_depth: int,
        max_pages: int | None = None,
        detect_documents: bool = False,
        level3_sample_limit: int = 2,
        notes: str = "",
    ) -> SiteSectionInventory:
        site = Site(
            url=seed_url,
            name=display_name,
            fetch_mode=fetch_mode,
            fetch_config_json=fetch_config_json,
        )
        scope = build_scope_from_site(
            site,
            max_depth=discovery_depth,
            max_pages=max_pages or 0,
            max_files=0,
            allowed_page_prefixes=allowed_page_prefixes,
            allowed_file_prefixes=allowed_file_prefixes,
        )
        return self.discover_scope(
            scope,
            site_key=site_key,
            display_name=display_name,
            homepage_url=homepage_url,
            section_depth=section_depth,
            detect_documents=detect_documents,
            level3_sample_limit=level3_sample_limit,
            notes=notes,
        )

    def discover_scope(
        self,
        scope: CrawlScope,
        *,
        site_key: str,
        display_name: str,
        homepage_url: str,
        section_depth: int,
        detect_documents: bool = False,
        level3_sample_limit: int = 2,
        notes: str = "",
    ) -> SiteSectionInventory:
        pacer = PolitePacer.from_config(scope.fetch_config_json)
        primary_pages: deque[tuple[str, int]] = deque([(scope.seed_url, 0)])
        sampled_level3_queue: deque[tuple[str, int]] = deque()
        queued_page_urls = {canonicalize_tracked_url(scope.seed_url)}
        processed_page_urls: set[str] = set()
        page_failures: list[str] = []
        sample_pages: list[str] = []
        unique_document_links: set[str] = set()
        section_map: dict[str, _SectionAggregate] = {}
        level3_candidate_urls: dict[str, set[str]] = defaultdict(set)
        level3_sampled_counts: dict[str, int] = defaultdict(int)
        pages_with_docs = 0
        level2_pages_discovered = 0
        sampled_level3_pages = 0
        skipped_level3_candidate_pages = 0
        skipped_external_pages = 0
        skipped_external_files = 0
        skipped_duplicate_pages = 0

        while (primary_pages or sampled_level3_queue) and (scope.max_pages <= 0 or len(processed_page_urls) < scope.max_pages):
            is_sample_phase = not primary_pages
            queued_url, depth = (primary_pages.popleft() if primary_pages else sampled_level3_queue.popleft())
            request_url = sanitize_request_url(queued_url) or queued_url
            try:
                pacer.wait_for_request("page")
                page = self.crawler.fetch_page(
                    request_url,
                    fetch_mode=scope.fetch_mode,
                    fetch_config_json=scope.fetch_config_json,
                )
            except Exception as exc:  # pragma: no cover - live failure path
                page_failures.append(f"{request_url}: {type(exc).__name__}: {exc}")
                continue

            canonical_page_url = canonicalize_tracked_url(page.final_url or request_url)
            if not canonical_page_url:
                continue
            if not is_page_url_in_scope(scope, canonical_page_url):
                skipped_external_pages += 1
                continue
            if canonical_page_url in processed_page_urls:
                skipped_duplicate_pages += 1
                continue

            processed_page_urls.add(canonical_page_url)
            page_level = _path_level(canonical_page_url)
            if page_level <= 2:
                level2_pages_discovered += 1
            elif is_sample_phase:
                sampled_level3_pages += 1
            _append_unique(sample_pages, canonical_page_url)
            page_links = extract_links(page.raw_html, page.final_url or request_url)
            document_like_links = set(find_document_links(page_links))
            in_scope_document_links: list[str] = []

            for link in page_links:
                canonical_link = canonicalize_tracked_url(link)
                if not canonical_link:
                    continue

                if link in document_like_links:
                    if detect_documents:
                        if not is_file_url_in_scope(scope, canonical_link):
                            skipped_external_files += 1
                            continue
                        in_scope_document_links.append(canonical_link)
                        unique_document_links.add(canonical_link)
                    continue

                if not is_page_url_in_scope(scope, canonical_link):
                    skipped_external_pages += 1
                    continue
                if depth >= scope.max_depth:
                    continue
                if canonical_link in queued_page_urls or canonical_link in processed_page_urls:
                    skipped_duplicate_pages += 1
                    continue
                next_depth = depth + 1
                next_level = _path_level(canonical_link)
                if next_level <= 2:
                    primary_pages.append((sanitize_request_url(link) or link, next_depth))
                    queued_page_urls.add(canonical_link)
                    continue

                branch_path = _path_prefix(canonical_link, level=2)
                branch_candidates = level3_candidate_urls[branch_path]
                if canonical_link in branch_candidates:
                    continue
                branch_candidates.add(canonical_link)
                if level3_sampled_counts[branch_path] < level3_sample_limit:
                    sampled_level3_queue.append((sanitize_request_url(link) or link, next_depth))
                    queued_page_urls.add(canonical_link)
                    level3_sampled_counts[branch_path] += 1
                else:
                    skipped_level3_candidate_pages += 1

            page_title = _extract_title(page)
            if in_scope_document_links:
                pages_with_docs += 1

            for section_path in _section_prefixes(canonical_page_url, max_section_depth=section_depth):
                aggregate = section_map.get(section_path)
                if aggregate is None:
                    aggregate = _SectionAggregate(
                        section_path=section_path,
                        section_level=max(1, section_path.count("/")),
                    )
                    section_map[section_path] = aggregate
                aggregate.page_urls.add(canonical_page_url)
                if in_scope_document_links:
                    aggregate.page_urls_with_docs.add(canonical_page_url)
                aggregate.doc_urls.update(in_scope_document_links)
                _append_unique(aggregate.sample_urls, canonical_page_url)
                _append_unique(aggregate.sample_titles, page_title)

        sections = [
            SectionSummary(
                section_path=item.section_path,
                section_level=item.section_level,
                page_count=len(item.page_urls),
                child_section_count=0,
                page_with_docs_count=len(item.page_urls_with_docs),
                doc_link_count=len(item.doc_urls),
                sample_urls=item.sample_urls,
                sample_titles=item.sample_titles,
                candidate_category=_candidate_category(item.section_path, item.sample_titles),
            )
            for item in section_map.values()
        ]
        section_paths = {item.section_path for item in sections}
        for item in sections:
            item.child_section_count = _count_child_sections(item.section_path, section_paths)
        sections.sort(key=lambda item: (item.section_level, -item.child_section_count, -item.page_count, item.section_path))
        section_by_path = {item.section_path: item for item in sections}

        expansion_candidates: list[ExpansionCandidate] = []
        for branch_path, candidates in sorted(level3_candidate_urls.items()):
            sampled_count = min(level3_sampled_counts.get(branch_path, 0), len(candidates))
            skipped_count = max(len(candidates) - sampled_count, 0)
            branch_section = section_by_path.get(branch_path)
            candidate_category = (
                branch_section.candidate_category
                if branch_section is not None
                else _candidate_category(branch_path, [])
            )
            if candidate_category == "exam_education":
                continue
            if skipped_count <= 0 and candidate_category not in {"research_publications", "finance_reports"}:
                continue
            if skipped_count > 0:
                reason = (
                    f"Discovered `{len(candidates)}` third-level candidate pages under `{branch_path}` but only sampled "
                    f"`{sampled_count}`; expand this branch before final scope selection."
                )
            else:
                reason = (
                    f"`{branch_path}` looks like a high-value `{candidate_category}` branch; keep it available for targeted third-level expansion."
                )
            expansion_candidates.append(
                ExpansionCandidate(
                    branch_path=branch_path,
                    candidate_category=candidate_category,
                    sampled_pages=sampled_count,
                    discovered_candidate_pages=len(candidates),
                    skipped_candidate_pages=skipped_count,
                    reason=reason,
                )
            )
        expansion_candidates.sort(
            key=lambda item: (-item.skipped_candidate_pages, -item.discovered_candidate_pages, item.branch_path)
        )

        return SiteSectionInventory(
            site_key=site_key,
            display_name=display_name,
            seed_url=scope.seed_url,
            homepage_url=homepage_url,
            fetch_mode=scope.fetch_mode,
            allowed_page_prefixes=scope.allowed_page_prefixes,
            allowed_file_prefixes=scope.allowed_file_prefixes,
            discovery_depth=scope.max_depth,
            section_depth=section_depth,
            max_pages=scope.max_pages,
            page_limit_mode="unbounded" if scope.max_pages <= 0 else "bounded",
            discovery_mode="structure_only" if not detect_documents else "structure_plus_documents",
            discovery_strategy="adaptive_sections",
            detect_documents=detect_documents,
            level3_sample_limit=level3_sample_limit,
            pages_discovered=len(processed_page_urls),
            pages_with_docs=pages_with_docs,
            unique_document_links=len(unique_document_links),
            level2_pages_discovered=level2_pages_discovered,
            sampled_level3_pages=sampled_level3_pages,
            skipped_level3_candidate_pages=skipped_level3_candidate_pages,
            skipped_external_pages=skipped_external_pages,
            skipped_external_files=skipped_external_files,
            skipped_duplicate_pages=skipped_duplicate_pages,
            page_failures=page_failures,
            sample_pages=sample_pages,
            sections=sections,
            expansion_candidates=expansion_candidates,
            notes=notes,
        )


def render_markdown(
    inventory: CatalogSectionInventory,
    *,
    top_sections_per_site: int = 12,
) -> str:
    total_pages = sum(site.pages_discovered for site in inventory.sites)
    total_level2_pages = sum(site.level2_pages_discovered for site in inventory.sites)
    total_sampled_level3_pages = sum(site.sampled_level3_pages for site in inventory.sites)
    total_expansion_candidates = sum(len(site.expansion_candidates) for site in inventory.sites)
    total_sections = sum(len(site.sections) for site in inventory.sites)
    total_level_1 = sum(len(_level_sections(site, level=1)) for site in inventory.sites)
    total_level_2 = sum(sum(1 for section in site.sections if section.section_level == 2) for site in inventory.sites)
    total_level_3 = sum(sum(1 for section in site.sections if section.section_level == 3) for site in inventory.sites)
    depth_limit_text = "unbounded within depth" if inventory.max_pages <= 0 else str(inventory.max_pages)
    lines = [
        "# Discover Site Sections",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{inventory.generated_at}`",
        f"- Catalog: `{inventory.catalog}`",
        f"- Targets discovered: `{len(inventory.sites)}`",
        f"- Discovery mode: `{inventory.discovery_mode}`",
        f"- Discovery strategy: `{inventory.discovery_strategy}`",
        f"- Discovery limits: depth=`{inventory.discovery_depth}`, section_depth=`{inventory.section_depth}`, page_limit=`{depth_limit_text}`",
        f"- Inventory totals: pages=`{total_pages}`, level_1_sections=`{total_level_1}`, level_2_pages=`{total_level2_pages}`, sampled_level_3_pages=`{total_sampled_level3_pages}`, sections=`{total_sections}`, level_2_sections=`{total_level_2}`, level_3_sections=`{total_level_3}`",
        f"- Expansion signals: candidate_branches=`{total_expansion_candidates}`, level3_sample_limit=`{inventory.level3_sample_limit}`",
        "- Operational note: this planning artifact covers all reachable level-2 pages first, then samples level-3 branches for later targeted expansion.",
        "- Terminology note: `level_2_pages` counts discovered HTML pages at path depth 2; it is not the number of level-1 directories.",
        "",
        "## Site Summary",
        "",
        "| Site | Pages | Level-1 sections | Level-2 pages | Sampled level-3 pages | Expansion candidates | Top branches |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for site in inventory.sites:
        top_sections = ", ".join(
            f"{section.section_path} [{section.candidate_category}] children={section.child_section_count}"
            for section in sorted(site.sections, key=lambda item: (-item.child_section_count, -item.page_count, item.section_path))[:3]
        ) or "-"
        level_1_sections = _level_sections(site, level=1)
        lines.append(
            f"| {site.display_name} | {site.pages_discovered} | {len(level_1_sections)} | {site.level2_pages_discovered} | {site.sampled_level3_pages} | {len(site.expansion_candidates)} | {top_sections} |"
        )

    lines.extend(["", "## Section Inventory", ""])
    for site in inventory.sites:
        level_1_sections = sorted(
            _level_sections(site, level=1),
            key=lambda item: (-item.child_section_count, -item.page_count, item.section_path),
        )
        lines.append(f"### {site.display_name}")
        lines.append("")
        lines.append(f"- Site key: `{site.site_key}`")
        lines.append(f"- Seed URL: `{site.seed_url}`")
        lines.append(f"- Pages discovered: `{site.pages_discovered}`")
        lines.append(f"- Level-1 sections discovered: `{len(level_1_sections)}`")
        lines.append(f"- Discovery mode: `{site.discovery_mode}`")
        lines.append(f"- Discovery strategy: `{site.discovery_strategy}`")
        lines.append(f"- Page limit mode: `{site.page_limit_mode}`")
        lines.append(f"- Level-2 pages discovered: `{site.level2_pages_discovered}`")
        lines.append(f"- Sampled level-3 pages: `{site.sampled_level3_pages}`")
        lines.append(f"- Level-3 sample limit per branch: `{site.level3_sample_limit}`")
        lines.append("- Clarification: `Level-2 pages discovered` is a page count, not a count of level-1 directories.")
        lines.append(f"- Allowed page prefixes: `{', '.join(site.allowed_page_prefixes)}`")
        if site.sample_pages:
            lines.append("- Sample pages: " + ", ".join(f"`{url}`" for url in site.sample_pages))
        if site.page_failures:
            lines.append("- Page failures: " + ", ".join(f"`{item}`" for item in site.page_failures[:3]))
        if site.expansion_candidates:
            lines.append(
                "- Expansion candidates: "
                + ", ".join(f"`{item.branch_path}` ({item.discovered_candidate_pages} candidates)" for item in site.expansion_candidates[:5])
            )
        lines.append("")
        lines.extend(
            [
                "#### Level-1 Overview",
                "",
                "| Section | Pages | Child sections | Candidate category | Sample pages |",
                "|---|---:|---:|---|---|",
            ]
        )
        for section in level_1_sections:
            sample_pages = ", ".join(section.sample_urls[:2]) or "-"
            lines.append(
                f"| {section.section_path} | {section.page_count} | {section.child_section_count} | "
                f"{section.candidate_category} | {sample_pages} |"
            )
        lines.append("")
        lines.extend(
            [
                "| Section | Level | Pages | Child sections | Candidate category | Sample pages |",
                "|---|---:|---:|---:|---|---|",
            ]
        )
        for section in sorted(site.sections, key=lambda item: (-item.child_section_count, -item.page_count, item.section_path))[:top_sections_per_site]:
            sample_pages = ", ".join(section.sample_urls[:2]) or "-"
            lines.append(
                f"| {section.section_path} | {section.section_level} | {section.page_count} | {section.child_section_count} | "
                f"{section.candidate_category} | {sample_pages} |"
            )
        lines.append("")
    return "\n".join(lines)
