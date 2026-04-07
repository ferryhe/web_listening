from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from web_listening.blocks.section_discovery import (
    CatalogSectionInventory,
    ExpansionCandidate,
    SectionSummary,
    SiteSectionInventory,
    render_yaml,
)
from web_listening.config import settings

_IMPORTANCE_RANK = {"high": 0, "medium": 1, "low": 2}
_CONVERSION_RANK = {"high": 0, "medium": 1, "low": 2, "skip": 3}
_ALLOWED_CATEGORIES = {
    "exam_education",
    "research_publications",
    "governance_management",
    "finance_reports",
    "membership_operations",
    "news_announcements",
    "archive_reference",
    "general_reference",
}
_HIGH_VALUE_CATEGORIES = {"research_publications", "finance_reports"}
_PROJECT_DEPRIORITIZED_CATEGORIES = {"exam_education", "governance_management"}


@dataclass(slots=True)
class ClassifiedSection:
    section_path: str
    section_level: int
    page_count: int
    page_with_docs_count: int
    doc_link_count: int
    child_section_count: int = 0
    sample_urls: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    candidate_category: str = "general_reference"
    source_category: str = "general_reference"
    business_importance: str = "medium"
    conversion_priority: str = "medium"
    classification_reason: str = ""


@dataclass(slots=True)
class Level2BranchPriority:
    branch_path: str
    page_count: int
    child_section_count: int
    sampled_level3_sections: int
    skipped_candidate_pages: int
    dominant_category: str
    business_importance: str
    expansion_priority: str
    reasoning: str
    supporting_sections: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ClassifiedSiteSections:
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
    notes: str = ""
    category_counts: dict[str, int] = field(default_factory=dict)
    importance_counts: dict[str, int] = field(default_factory=dict)
    conversion_counts: dict[str, int] = field(default_factory=dict)
    expansion_candidates: list[ExpansionCandidate] = field(default_factory=list)
    level2_priorities: list[Level2BranchPriority] = field(default_factory=list)
    sections: list[ClassifiedSection] = field(default_factory=list)


@dataclass(slots=True)
class CatalogSectionClassification:
    catalog: str
    inventory_generated_at: str
    classified_at: str
    discovery_depth: int
    section_depth: int
    max_pages: int
    classification_mode: str
    page_limit_mode: str = "unbounded"
    discovery_mode: str = "structure_only"
    discovery_strategy: str = "adaptive_sections"
    detect_documents: bool = False
    level3_sample_limit: int = 2
    inventory_path: str = ""
    sites: list[ClassifiedSiteSections] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_section(item: dict[str, Any]) -> SectionSummary:
    return SectionSummary(
        section_path=str(item.get("section_path", "")).strip() or "/",
        section_level=int(item.get("section_level", 1) or 1),
        page_count=int(item.get("page_count", 0) or 0),
        page_with_docs_count=int(item.get("page_with_docs_count", 0) or 0),
        doc_link_count=int(item.get("doc_link_count", 0) or 0),
        child_section_count=int(item.get("child_section_count", 0) or 0),
        sample_urls=_as_list_of_strings(item.get("sample_urls")),
        sample_titles=_as_list_of_strings(item.get("sample_titles")),
        candidate_category=str(item.get("candidate_category", "general_reference")).strip() or "general_reference",
    )


def _parse_expansion_candidate(item: dict[str, Any]) -> ExpansionCandidate:
    return ExpansionCandidate(
        branch_path=str(item.get("branch_path", "")).strip() or "/",
        candidate_category=str(item.get("candidate_category", "general_reference")).strip() or "general_reference",
        sampled_pages=int(item.get("sampled_pages", 0) or 0),
        discovered_candidate_pages=int(item.get("discovered_candidate_pages", 0) or 0),
        skipped_candidate_pages=int(item.get("skipped_candidate_pages", 0) or 0),
        reason=str(item.get("reason", "")).strip(),
    )


def _parse_site(item: dict[str, Any]) -> SiteSectionInventory:
    sections = [_parse_section(section) for section in item.get("sections", []) if isinstance(section, dict)]
    expansion_candidates = [
        _parse_expansion_candidate(candidate)
        for candidate in item.get("expansion_candidates", [])
        if isinstance(candidate, dict)
    ]
    return SiteSectionInventory(
        site_key=str(item.get("site_key", "")).strip(),
        display_name=str(item.get("display_name", "")).strip(),
        seed_url=str(item.get("seed_url", "")).strip(),
        homepage_url=str(item.get("homepage_url", "")).strip(),
        fetch_mode=str(item.get("fetch_mode", "http")).strip() or "http",
        allowed_page_prefixes=_as_list_of_strings(item.get("allowed_page_prefixes")),
        allowed_file_prefixes=_as_list_of_strings(item.get("allowed_file_prefixes")),
        discovery_depth=int(item.get("discovery_depth", 0) or 0),
        section_depth=int(item.get("section_depth", 0) or 0),
        max_pages=int(item.get("max_pages", 0) or 0),
        pages_discovered=int(item.get("pages_discovered", 0) or 0),
        pages_with_docs=int(item.get("pages_with_docs", 0) or 0),
        unique_document_links=int(item.get("unique_document_links", 0) or 0),
        page_limit_mode=str(item.get("page_limit_mode", "unbounded")).strip() or "unbounded",
        discovery_mode=str(item.get("discovery_mode", "structure_only")).strip() or "structure_only",
        discovery_strategy=str(item.get("discovery_strategy", "adaptive_sections")).strip() or "adaptive_sections",
        detect_documents=bool(item.get("detect_documents", False)),
        level3_sample_limit=int(item.get("level3_sample_limit", 2) or 2),
        level2_pages_discovered=int(item.get("level2_pages_discovered", 0) or 0),
        sampled_level3_pages=int(item.get("sampled_level3_pages", 0) or 0),
        skipped_level3_candidate_pages=int(item.get("skipped_level3_candidate_pages", 0) or 0),
        skipped_external_pages=int(item.get("skipped_external_pages", 0) or 0),
        skipped_external_files=int(item.get("skipped_external_files", 0) or 0),
        skipped_duplicate_pages=int(item.get("skipped_duplicate_pages", 0) or 0),
        page_failures=_as_list_of_strings(item.get("page_failures")),
        sample_pages=_as_list_of_strings(item.get("sample_pages")),
        expansion_candidates=expansion_candidates,
        sections=sections,
        notes=str(item.get("notes", "")).strip(),
    )


def load_section_inventory(path: str | Path) -> CatalogSectionInventory:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    sites = [_parse_site(item) for item in payload.get("sites", []) if isinstance(item, dict)]
    return CatalogSectionInventory(
        catalog=str(payload.get("catalog", "")).strip(),
        generated_at=str(payload.get("generated_at", "")).strip(),
        discovery_depth=int(payload.get("discovery_depth", 0) or 0),
        section_depth=int(payload.get("section_depth", 0) or 0),
        max_pages=int(payload.get("max_pages", 0) or 0),
        page_limit_mode=str(payload.get("page_limit_mode", "unbounded")).strip() or "unbounded",
        discovery_mode=str(payload.get("discovery_mode", "structure_only")).strip() or "structure_only",
        discovery_strategy=str(payload.get("discovery_strategy", "adaptive_sections")).strip() or "adaptive_sections",
        detect_documents=bool(payload.get("detect_documents", False)),
        level3_sample_limit=int(payload.get("level3_sample_limit", 2) or 2),
        sites=sites,
    )


def _normalize_category(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in _ALLOWED_CATEGORIES else "general_reference"


def _normalize_importance(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"high", "medium", "low"} else "medium"


def _normalize_conversion_priority(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"high", "medium", "low", "skip"} else "medium"


def _level_sections(site: ClassifiedSiteSections, *, level: int) -> list[ClassifiedSection]:
    return [section for section in site.sections if section.section_level == level]


class SectionClassifier:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
        return self._client

    def classify_inventory(
        self,
        inventory: CatalogSectionInventory,
        *,
        inventory_path: str = "",
        use_ai: bool = False,
        site_keys: set[str] | None = None,
    ) -> CatalogSectionClassification:
        filtered_sites = [
            site
            for site in inventory.sites
            if not site_keys or site.site_key.lower() in site_keys
        ]
        classification_mode = "heuristic+ai" if use_ai and settings.openai_api_key else "heuristic"
        sites = [self.classify_site(site, use_ai=use_ai) for site in filtered_sites]
        return CatalogSectionClassification(
            catalog=inventory.catalog,
            inventory_generated_at=inventory.generated_at,
            classified_at=datetime.now(timezone.utc).isoformat(),
            discovery_depth=inventory.discovery_depth,
            section_depth=inventory.section_depth,
            max_pages=inventory.max_pages,
            classification_mode=classification_mode,
            page_limit_mode=inventory.page_limit_mode,
            discovery_mode=inventory.discovery_mode,
            discovery_strategy=inventory.discovery_strategy,
            detect_documents=inventory.detect_documents,
            level3_sample_limit=inventory.level3_sample_limit,
            inventory_path=inventory_path,
            sites=sites,
        )

    def classify_site(self, site: SiteSectionInventory, *, use_ai: bool = False) -> ClassifiedSiteSections:
        heuristic_sections = [self._heuristic_classification(section) for section in site.sections]
        sections = heuristic_sections

        if use_ai and settings.openai_api_key and site.sections:
            ai_sections = self._classify_site_with_ai(site)
            if ai_sections:
                heuristic_by_path = {item.section_path: item for item in heuristic_sections}
                ai_by_path = {item.section_path: item for item in ai_sections}
                merged: list[ClassifiedSection] = []
                for original in site.sections:
                    heuristic = heuristic_by_path[original.section_path]
                    ai_item = ai_by_path.get(original.section_path)
                    if ai_item is None:
                        merged.append(heuristic)
                        continue
                    if heuristic.section_path in {"/about"}:
                        merged.append(heuristic)
                        continue
                    if heuristic.source_category not in {"general_reference", "archive_reference"}:
                        if ai_item.source_category == heuristic.source_category and ai_item.classification_reason:
                            merged.append(
                                ClassifiedSection(
                                    section_path=heuristic.section_path,
                                    section_level=heuristic.section_level,
                                    page_count=heuristic.page_count,
                                    page_with_docs_count=heuristic.page_with_docs_count,
                                    doc_link_count=heuristic.doc_link_count,
                                    child_section_count=heuristic.child_section_count,
                                    sample_urls=heuristic.sample_urls,
                                    sample_titles=heuristic.sample_titles,
                                    candidate_category=heuristic.candidate_category,
                                    source_category=heuristic.source_category,
                                    business_importance=heuristic.business_importance,
                                    conversion_priority=heuristic.conversion_priority,
                                    classification_reason=ai_item.classification_reason,
                                )
                            )
                        else:
                            merged.append(heuristic)
                        continue
                    if ai_item.source_category == "general_reference":
                        merged.append(heuristic)
                        continue
                    merged.append(ai_item)
                sections = merged

        level2_priorities = self._build_level2_priorities(site, sections)
        sections.sort(
            key=lambda item: (
                _IMPORTANCE_RANK.get(item.business_importance, 9),
                _CONVERSION_RANK.get(item.conversion_priority, 9),
                -item.doc_link_count,
                -item.page_count,
                item.section_path,
            )
        )
        return ClassifiedSiteSections(
            site_key=site.site_key,
            display_name=site.display_name,
            seed_url=site.seed_url,
            homepage_url=site.homepage_url,
            fetch_mode=site.fetch_mode,
            allowed_page_prefixes=site.allowed_page_prefixes,
            allowed_file_prefixes=site.allowed_file_prefixes,
            discovery_depth=site.discovery_depth,
            section_depth=site.section_depth,
            max_pages=site.max_pages,
            pages_discovered=site.pages_discovered,
            pages_with_docs=site.pages_with_docs,
            unique_document_links=site.unique_document_links,
            page_limit_mode=site.page_limit_mode,
            discovery_mode=site.discovery_mode,
            discovery_strategy=site.discovery_strategy,
            detect_documents=site.detect_documents,
            level3_sample_limit=site.level3_sample_limit,
            level2_pages_discovered=site.level2_pages_discovered,
            sampled_level3_pages=site.sampled_level3_pages,
            skipped_level3_candidate_pages=site.skipped_level3_candidate_pages,
            skipped_external_pages=site.skipped_external_pages,
            skipped_external_files=site.skipped_external_files,
            skipped_duplicate_pages=site.skipped_duplicate_pages,
            page_failures=site.page_failures,
            sample_pages=site.sample_pages,
            notes=site.notes,
            category_counts=dict(Counter(section.source_category for section in sections)),
            importance_counts=dict(Counter(section.business_importance for section in sections)),
            conversion_counts=dict(Counter(section.conversion_priority for section in sections)),
            expansion_candidates=site.expansion_candidates,
            level2_priorities=level2_priorities,
            sections=sections,
        )

    def _build_level2_priorities(
        self,
        site: SiteSectionInventory,
        sections: list[ClassifiedSection],
    ) -> list[Level2BranchPriority]:
        level2_sections = [section for section in sections if section.section_level == 2]
        if not level2_sections:
            return []

        sections_by_path = {section.section_path: section for section in sections}
        expansion_by_branch = {candidate.branch_path: candidate for candidate in site.expansion_candidates}
        priorities: list[Level2BranchPriority] = []

        for branch in level2_sections:
            descendants = [
                item
                for item in sections
                if item.section_path == branch.section_path or item.section_path.startswith(branch.section_path + "/")
            ]
            descendant_categories = [
                item.source_category
                for item in descendants
                if item.source_category not in {"general_reference", "archive_reference"}
            ]
            if descendant_categories:
                dominant_category = Counter(descendant_categories).most_common(1)[0][0]
            else:
                dominant_category = branch.source_category

            high_descendants = sum(1 for item in descendants if item.business_importance == "high")
            medium_descendants = sum(1 for item in descendants if item.business_importance == "medium")
            sampled_level3_sections = sum(1 for item in descendants if item.section_level == 3)
            expansion_candidate = expansion_by_branch.get(branch.section_path)
            skipped_candidate_pages = expansion_candidate.skipped_candidate_pages if expansion_candidate else 0

            importance = branch.business_importance
            if dominant_category in _PROJECT_DEPRIORITIZED_CATEGORIES:
                importance = "low"
            elif dominant_category in _HIGH_VALUE_CATEGORIES and (high_descendants > 0 or skipped_candidate_pages >= 10):
                importance = "high"
            elif dominant_category in _HIGH_VALUE_CATEGORIES and (medium_descendants > 0 or branch.child_section_count > 0):
                importance = "medium"
            elif dominant_category in {"membership_operations", "news_announcements"} and skipped_candidate_pages == 0:
                importance = "low"

            if dominant_category in _PROJECT_DEPRIORITIZED_CATEGORIES:
                expansion_priority = "low"
            elif dominant_category in _HIGH_VALUE_CATEGORIES and skipped_candidate_pages >= 20:
                expansion_priority = "high"
            elif skipped_candidate_pages > 0 or sampled_level3_sections > 0:
                expansion_priority = "medium"
            else:
                expansion_priority = "low"

            supporting_sections = [
                item.section_path
                for item in descendants
                if item.section_path != branch.section_path
            ][:5]
            reason_parts = [
                f"`{branch.section_path}` looks primarily `{dominant_category}`",
                f"sampled_level3_sections=`{sampled_level3_sections}`",
            ]
            if skipped_candidate_pages > 0:
                reason_parts.append(f"unsampled_candidates=`{skipped_candidate_pages}`")
            if high_descendants > 0:
                reason_parts.append(f"high_signal_children=`{high_descendants}`")
            if dominant_category in _PROJECT_DEPRIORITIZED_CATEGORIES:
                reason_parts.append("project_default=`deprioritize_exam_and_governance`")
            reasoning = "; ".join(reason_parts) + "."

            priorities.append(
                Level2BranchPriority(
                    branch_path=branch.section_path,
                    page_count=branch.page_count,
                    child_section_count=branch.child_section_count,
                    sampled_level3_sections=sampled_level3_sections,
                    skipped_candidate_pages=skipped_candidate_pages,
                    dominant_category=dominant_category,
                    business_importance=importance,
                    expansion_priority=expansion_priority,
                    reasoning=reasoning,
                    supporting_sections=supporting_sections,
                )
            )

        priorities.sort(
            key=lambda item: (
                _IMPORTANCE_RANK.get(item.business_importance, 9),
                _CONVERSION_RANK.get(item.expansion_priority, 9),
                -item.skipped_candidate_pages,
                -item.child_section_count,
                item.branch_path,
            )
        )
        return priorities

    def _heuristic_classification(self, section: SectionSummary) -> ClassifiedSection:
        section_path = section.section_path.lower()
        url_hints = " ".join(urlsplit(url).path for url in section.sample_urls).lower()
        title_hints = " ".join(section.sample_titles).lower()
        haystack = " ".join([section_path, url_hints, title_hints]).lower()

        category = _normalize_category(section.candidate_category)
        importance = "medium"
        conversion = "medium"
        reason = "Stable site section with mixed signals; keep under review until monitoring intent narrows the scope."

        exam_tokens = (
            "exam-req",
            "/exam",
            "exam ",
            "syllabus",
            "candidate",
            "credential",
            "admissions",
            "education",
            "vee",
            "pass mark",
            "study note",
            "modules",
        )
        research_tokens = (
            "research",
            "publication",
            "publications",
            "journal",
            "paper",
            "working paper",
            "monograph",
            "white paper",
            "research institute",
        )
        governance_tokens = (
            "governance",
            "board",
            "leadership",
            "election",
            "elections",
            "policy",
            "policies",
            "guideline",
            "guidelines",
            "bylaws",
            "council",
            "committee",
            "annual-report",
            "annual reports",
            "governing",
            "statute",
        )
        finance_tokens = (
            "finances",
            "finance",
            "financial",
            "budget",
            "budgets",
            "statement",
            "statements",
            "audited",
            "audit",
            "accounts",
            "treasury",
        )
        membership_tokens = (
            "membership",
            "member",
            "dues",
            "directory",
            "contact",
            "advertising",
            "sponsorship",
            "career",
        )
        news_tokens = (
            "news",
            "announcement",
            "press",
            "event",
            "conference",
            "congress",
            "webinar",
            "calendar",
        )
        archive_tokens = (
            "archive",
            "history",
            "historical",
            "past presidents",
            "past-president",
            "legacy",
        )

        if section_path == "/about":
            category = "general_reference"
            importance = "medium"
            conversion = "low"
            reason = "This umbrella about section mixes several sub-areas; classify and monitor its children more specifically than the root."
        elif any(token in haystack for token in exam_tokens):
            category = "exam_education"
            importance = "low"
            conversion = "skip"
            reason = "This section is exam or credential related, but `exam_education` is outside this project's default monitoring scope."
        elif any(token in haystack for token in research_tokens):
            category = "research_publications"
            importance = "high"
            conversion = "high" if section.doc_link_count > 0 else "medium"
            reason = (
                f"Research or publication language dominates this section, with `{section.doc_link_count}` linked documents already visible."
                if section.doc_link_count > 0
                else "Research or publication language dominates this section, so it is likely to produce substantive materials later."
            )
        elif any(token in haystack for token in governance_tokens):
            category = "governance_management"
            importance = "low"
            conversion = "skip"
            reason = "This section is governance or management related, but `governance_management` is outside this project's default monitoring scope."
        elif any(token in haystack for token in finance_tokens):
            category = "finance_reports"
            importance = "medium"
            conversion = "medium" if section.doc_link_count > 0 else "low"
            reason = (
                "Financial reporting signals suggest recurring statements or budgets; important for recordkeeping but not always the business core."
            )
        elif any(token in haystack for token in membership_tokens):
            category = "membership_operations"
            importance = "low"
            conversion = "low" if section.doc_link_count > 0 else "skip"
            reason = "Membership, directory, or contact signals suggest routine operations rather than core monitored business content."
        elif any(token in haystack for token in news_tokens):
            category = "news_announcements"
            importance = "medium" if section.doc_link_count > 0 else "low"
            conversion = "low" if section.doc_link_count > 0 else "skip"
            reason = "News and announcement sections matter for awareness, but they usually need later intent-based filtering."
        elif any(token in haystack for token in archive_tokens):
            category = "archive_reference"
            importance = "low"
            conversion = "skip"
            reason = "Archive or historical signals suggest reference material that should be preserved but not converted by default."
        else:
            if category == "exam_education":
                importance = "low"
                conversion = "skip"
                reason = "The discovered section looks education or exam related, but this project does not prioritize `exam_education` by default."
            elif category == "research_publications":
                importance = "high"
                conversion = "high" if section.doc_link_count > 0 else "medium"
                reason = "The discovered section looks like a publication hub, which often contains business-relevant source material."
            elif category == "governance_management":
                importance = "low"
                conversion = "skip"
                reason = "Governance signals are present, but this project does not prioritize `governance_management` by default."
            elif category == "membership_operations":
                importance = "low"
                conversion = "low" if section.doc_link_count > 0 else "skip"
                reason = "The discovered section looks operational rather than business-core."
            elif category == "news_announcements":
                importance = "low"
                conversion = "low" if section.doc_link_count > 0 else "skip"
                reason = "The discovered section looks news-like and should be filtered later by monitoring intent."
            else:
                importance = "low" if section.doc_link_count == 0 else "medium"
                conversion = "skip" if section.doc_link_count == 0 else "low"
                reason = (
                    "General navigation section with no visible document signal yet."
                    if section.doc_link_count == 0
                    else "General section with some visible documents; keep as evidence, then decide later during scope selection."
                )

        return ClassifiedSection(
            section_path=section.section_path,
            section_level=section.section_level,
            page_count=section.page_count,
            page_with_docs_count=section.page_with_docs_count,
            doc_link_count=section.doc_link_count,
            child_section_count=section.child_section_count,
            sample_urls=section.sample_urls,
            sample_titles=section.sample_titles,
            candidate_category=section.candidate_category,
            source_category=category,
            business_importance=importance,
            conversion_priority=conversion,
            classification_reason=reason,
        )

    def _classify_site_with_ai(self, site: SiteSectionInventory) -> list[ClassifiedSection]:
        prompt_sections = []
        for section in site.sections:
            prompt_sections.append(
                {
                    "section_path": section.section_path,
                    "section_level": section.section_level,
                    "page_count": section.page_count,
                    "pages_with_docs_count": section.page_with_docs_count,
                    "doc_link_count": section.doc_link_count,
                    "child_section_count": section.child_section_count,
                    "sample_urls": section.sample_urls[:3],
                    "sample_titles": section.sample_titles[:3],
                    "candidate_category": section.candidate_category,
                }
            )

        prompt = (
            "You are classifying website sections for a monitoring planner.\n"
            "Return JSON with one top-level key named `sections`.\n"
            "Each item must contain: section_path, source_category, business_importance, conversion_priority, classification_reason.\n"
            f"Allowed source_category values: {', '.join(sorted(_ALLOWED_CATEGORIES))}.\n"
            "Allowed business_importance values: high, medium, low.\n"
            "Allowed conversion_priority values: high, medium, low, skip.\n"
            "Judge business importance from section purpose, not raw document count alone.\n"
            "Use `high` conversion only when future new or changed files from this section would likely deserve doc_to_md conversion.\n"
            "Keep classification_reason to one concise sentence.\n\n"
            "Be conservative: default to medium or low unless the section clearly maps to exam materials, formal governance policies, finance reports, or research publications.\n"
            "Do not mark membership, contact, directory, or broad about pages as high unless the evidence clearly shows formal policy or document-heavy monitoring value.\n\n"
            "For this project, `exam_education` and `governance_management` are generally out of scope and should default to low importance with low or skip conversion unless explicitly requested.\n\n"
            f"Site: {site.display_name}\n"
            f"Seed URL: {site.seed_url}\n"
            "Sections:\n"
            f"{json.dumps(prompt_sections, ensure_ascii=True)}"
        )

        try:
            response = self.client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=2000,
            )
            content = (response.choices[0].message.content or "").strip()
            start = content.find("{")
            end = content.rfind("}")
            payload = json.loads(content[start : end + 1] if start >= 0 and end >= 0 else content)
            raw_sections = payload.get("sections", [])
        except Exception:
            return []

        by_path = {section.section_path: section for section in site.sections}
        classifications: list[ClassifiedSection] = []
        for raw in raw_sections:
            if not isinstance(raw, dict):
                continue
            section_path = str(raw.get("section_path", "")).strip()
            source = by_path.get(section_path)
            if source is None:
                continue
            classifications.append(
                ClassifiedSection(
                    section_path=source.section_path,
                    section_level=source.section_level,
                    page_count=source.page_count,
                    page_with_docs_count=source.page_with_docs_count,
                    doc_link_count=source.doc_link_count,
                    child_section_count=source.child_section_count,
                    sample_urls=source.sample_urls,
                    sample_titles=source.sample_titles,
                    candidate_category=source.candidate_category,
                    source_category=_normalize_category(str(raw.get("source_category", "general_reference"))),
                    business_importance=_normalize_importance(str(raw.get("business_importance", "medium"))),
                    conversion_priority=_normalize_conversion_priority(
                        str(raw.get("conversion_priority", "medium"))
                    ),
                    classification_reason=str(raw.get("classification_reason", "")).strip()
                    or "AI classification produced no explicit reason.",
                )
            )
        return classifications


def render_markdown(
    classification: CatalogSectionClassification,
    *,
    top_sections_per_site: int = 14,
) -> str:
    total_sites = len(classification.sites)
    total_sections = sum(len(site.sections) for site in classification.sites)
    total_pages = sum(site.pages_discovered for site in classification.sites)
    total_level2_branches = sum(len(site.level2_priorities) for site in classification.sites)
    total_high_importance = sum(site.importance_counts.get("high", 0) for site in classification.sites)
    total_high_conversion = sum(site.conversion_counts.get("high", 0) for site in classification.sites)
    total_level1_sections = sum(len(_level_sections(site, level=1)) for site in classification.sites)
    total_high_importance_branches = sum(
        sum(1 for branch in site.level2_priorities if branch.business_importance == "high")
        for site in classification.sites
    )
    level_2_sections = sum(sum(1 for section in site.sections if section.section_level == 2) for site in classification.sites)
    level_3_sections = sum(sum(1 for section in site.sections if section.section_level == 3) for site in classification.sites)
    page_limit_text = "unbounded within depth" if classification.max_pages <= 0 else str(classification.max_pages)

    lines = [
        "# Classify Site Sections",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{classification.classified_at}`",
        f"- Catalog: `{classification.catalog}`",
        f"- Input inventory time: `{classification.inventory_generated_at}`",
        f"- Classification mode: `{classification.classification_mode}`",
        f"- Discovery mode: `{classification.discovery_mode}`",
        f"- Discovery strategy: `{classification.discovery_strategy}`",
        f"- Sites classified: `{total_sites}`",
        f"- Discovery coverage: pages=`{total_pages}`, sections=`{total_sections}`, level_1_sections=`{total_level1_sections}`, level_2_sections=`{level_2_sections}`, level_3_sections=`{level_3_sections}`",
        f"- Level-2 branch priorities: branches=`{total_level2_branches}`, high_importance_branches=`{total_high_importance_branches}`",
        f"- Discovery page limit: `{page_limit_text}`",
        f"- Priority totals: high_importance=`{total_high_importance}`, high_conversion=`{total_high_conversion}`",
        "- Operational note: this is a planning artifact for later scope selection, not the final monitoring scope.",
        "- Terminology note: `level_2_pages_discovered` from the input inventory is a page count, not the number of top-level directories.",
        "",
        "## Site Summary",
        "",
        "| Site | Pages | Level-1 sections | Level-2 branches | High-importance branches | Expansion branches | Watch candidates |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]

    for site in classification.sites:
        watch_candidates = ", ".join(
            f"{branch.branch_path} [{branch.dominant_category}]"
            for branch in site.level2_priorities[:3]
        ) or "-"
        high_importance_branches = sum(1 for branch in site.level2_priorities if branch.business_importance == "high")
        expansion_branches = sum(1 for branch in site.level2_priorities if branch.expansion_priority in {"high", "medium"})
        level_1_sections = _level_sections(site, level=1)
        lines.append(
            f"| {site.display_name} | {site.pages_discovered} | {len(level_1_sections)} | {len(site.level2_priorities)} | "
            f"{high_importance_branches} | {expansion_branches} | {watch_candidates} |"
        )

    lines.extend(["", "## Level-2 Branch Priority", ""])
    for site in classification.sites:
        level_1_sections = sorted(
            _level_sections(site, level=1),
            key=lambda item: (_IMPORTANCE_RANK.get(item.business_importance, 9), -item.child_section_count, -item.page_count, item.section_path),
        )
        lines.append(f"### {site.display_name}")
        lines.append("")
        lines.append(f"- Site key: `{site.site_key}`")
        lines.append(f"- Seed URL: `{site.seed_url}`")
        lines.append(f"- Discovery input: pages=`{site.pages_discovered}`, sections=`{len(site.sections)}`")
        lines.append(f"- Level-1 sections discovered: `{len(level_1_sections)}`")
        lines.append(f"- Discovery mode: `{site.discovery_mode}`")
        lines.append(f"- Discovery strategy: `{site.discovery_strategy}`")
        lines.append(f"- Page limit mode: `{site.page_limit_mode}`")
        lines.append(f"- Level-2 pages discovered: `{site.level2_pages_discovered}`")
        lines.append(f"- Sampled level-3 pages: `{site.sampled_level3_pages}`")
        lines.append(f"- Unsampled level-3 candidates: `{site.skipped_level3_candidate_pages}`")
        lines.append("- Clarification: `Level-2 pages discovered` comes from the discovery inventory and counts pages, not top-level directories.")
        if site.category_counts:
            category_line = ", ".join(f"`{key}`={value}" for key, value in sorted(site.category_counts.items()))
            lines.append(f"- Category mix: {category_line}")
        if site.importance_counts:
            importance_line = ", ".join(f"`{key}`={value}" for key, value in sorted(site.importance_counts.items()))
            lines.append(f"- Importance mix: {importance_line}")
        if site.conversion_counts:
            conversion_line = ", ".join(f"`{key}`={value}" for key, value in sorted(site.conversion_counts.items()))
            lines.append(f"- Conversion mix: {conversion_line}")
        lines.append("")
        lines.append("#### Level-1 Overview")
        lines.append("")
        lines.extend(
            [
                "| Section | Pages | Child sections | source_category | business_importance | conversion_priority |",
                "|---|---:|---:|---|---|---|",
            ]
        )
        for section in level_1_sections:
            lines.append(
                f"| {section.section_path} | {section.page_count} | {section.child_section_count} | "
                f"{section.source_category} | {section.business_importance} | {section.conversion_priority} |"
            )
        lines.append("")
        lines.extend(
            [
                "| Branch | Pages | Child sections | Sampled level-3 | Unsampled candidates | dominant_category | business_importance | expansion_priority | reasoning |",
                "|---|---:|---:|---:|---:|---|---|---|---|",
            ]
        )
        for branch in site.level2_priorities[:top_sections_per_site]:
            lines.append(
                f"| {branch.branch_path} | {branch.page_count} | {branch.child_section_count} | {branch.sampled_level3_sections} | "
                f"{branch.skipped_candidate_pages} | {branch.dominant_category} | {branch.business_importance} | "
                f"{branch.expansion_priority} | {branch.reasoning} |"
            )
        lines.append("")
        lines.append("#### Section Classification")
        lines.append("")
        lines.extend(
            [
                "| Section | Level | Pages | Child sections | source_category | business_importance | conversion_priority | classification_reason |",
                "|---|---:|---:|---:|---|---|---|---|",
            ]
        )
        for section in site.sections[:top_sections_per_site]:
            lines.append(
                f"| {section.section_path} | {section.section_level} | {section.page_count} | {section.child_section_count} | "
                f"{section.source_category} | {section.business_importance} | {section.conversion_priority} | "
                f"{section.classification_reason} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_yaml_text(classification: CatalogSectionClassification) -> str:
    return render_yaml(classification.to_dict())
