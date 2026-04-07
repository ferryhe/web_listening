from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import urlsplit

from web_listening.config import settings
from web_listening.models import CrawlScope, Document


@dataclass(slots=True)
class SectionSummary:
    label: str
    page_count: int


@dataclass(slots=True)
class SourcePageSummary:
    page_url: str
    file_count: int


@dataclass(slots=True)
class SourcePageEvidence:
    page_url: str
    file_count: int
    page_title: str
    page_excerpt: str
    sample_file_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SourcePageClassification:
    page_url: str
    file_count: int
    source_category: str
    business_importance: str
    conversion_priority: str
    classification_reason: str
    page_title: str = ""
    sample_file_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BootstrapSiteEvidence:
    display_name: str
    site_key: str
    seed_url: str
    scope_id: int
    run_id: int
    pages: int
    files: int
    top_sections: list[SectionSummary] = field(default_factory=list)
    section_hubs: list[str] = field(default_factory=list)
    top_file_source_pages: list[SourcePageSummary] = field(default_factory=list)
    source_page_evidence: list[SourcePageEvidence] = field(default_factory=list)
    source_page_classifications: list[SourcePageClassification] = field(default_factory=list)
    sample_page_urls: list[str] = field(default_factory=list)
    sample_file_urls: list[str] = field(default_factory=list)
    sample_file_paths: list[str] = field(default_factory=list)
    file_type_counts: dict[str, int] = field(default_factory=dict)


def _section_bucket(url: str) -> str:
    path = (urlsplit(url).path or "/").strip("/")
    if not path:
        return "/"
    return "/" + path.split("/")[0]


def _sample(items: Iterable[str], limit: int = 5) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
        if len(result) >= limit:
            break
    return result


def _extract_title(markdown: str) -> str:
    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        return line[:160]
    return ""


def _clean_excerpt(markdown: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", (markdown or "").strip())
    return text[:limit]


class TreeBootstrapExplainer:
    def __init__(self, storage):
        self.storage = storage
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

    def build_site_evidence(
        self,
        *,
        display_name: str,
        site_key: str,
        scope: CrawlScope,
    ) -> BootstrapSiteEvidence:
        if scope.id is None or scope.baseline_run_id is None:
            raise ValueError("Scope must be initialized with a baseline_run_id")

        run = self.storage.get_crawl_run(scope.baseline_run_id)
        if run is None:
            raise ValueError(f"Missing baseline run {scope.baseline_run_id} for scope {scope.id}")

        tracked_pages = self.storage.list_tracked_pages(scope.id)
        pages_by_id = {page.id: page for page in tracked_pages if page.id is not None}
        tracked_files = self.storage.list_tracked_files(scope.id)
        files_by_id = {tracked_file.id: tracked_file for tracked_file in tracked_files if tracked_file.id is not None}

        page_snapshots = self.storage.list_page_snapshots_for_run(scope.id, run.id)
        snapshots_by_page_id = {snapshot.page_id: snapshot for snapshot in page_snapshots}
        baseline_pages = [pages_by_id[snapshot.page_id] for snapshot in page_snapshots if snapshot.page_id in pages_by_id]

        section_counts = Counter(_section_bucket(page.canonical_url) for page in baseline_pages)
        top_sections = [
            SectionSummary(label=label, page_count=count)
            for label, count in section_counts.most_common(5)
        ]

        section_hubs = _sample(
            page.canonical_url
            for page in sorted(baseline_pages, key=lambda item: (item.depth, item.canonical_url))
            if page.depth <= 1
        )
        sample_page_urls = _sample(
            page.canonical_url
            for page in sorted(baseline_pages, key=lambda item: (item.depth, item.canonical_url))
        )

        observations = self.storage.list_file_observations(scope.id, run_id=run.id)
        source_counts = Counter(
            pages_by_id[observation.page_id].canonical_url
            for observation in observations
            if observation.page_id in pages_by_id
        )
        top_file_source_pages = [
            SourcePageSummary(page_url=page_url, file_count=count)
            for page_url, count in source_counts.most_common(5)
        ]

        document_map: dict[int, Document] = {}
        documents: list[Document] = []
        for observation in observations:
            tracked_file = files_by_id.get(observation.file_id)
            if tracked_file is None or tracked_file.latest_document_id is None:
                continue
            document = self.storage.get_document(tracked_file.latest_document_id)
            if document is not None:
                document_map[observation.file_id] = document
                documents.append(document)

        source_page_evidence: list[SourcePageEvidence] = []
        observations_by_page_id: dict[int, list] = {}
        for observation in observations:
            observations_by_page_id.setdefault(observation.page_id, []).append(observation)
        for page_id, page_observations in observations_by_page_id.items():
            tracked_page = pages_by_id.get(page_id)
            snapshot = snapshots_by_page_id.get(page_id)
            if tracked_page is None or snapshot is None:
                continue
            markdown = snapshot.fit_markdown or snapshot.markdown or snapshot.content_text or ""
            source_page_evidence.append(
                SourcePageEvidence(
                    page_url=tracked_page.canonical_url,
                    file_count=len(page_observations),
                    page_title=_extract_title(markdown),
                    page_excerpt=_clean_excerpt(markdown),
                    sample_file_urls=_sample(
                        (
                            document_map[observation.file_id].download_url
                            for observation in page_observations
                            if observation.file_id in document_map
                        )
                    ),
                )
            )
        source_page_evidence.sort(key=lambda item: (-item.file_count, item.page_url))

        file_type_counts = dict(
            Counter((document.doc_type or Path(document.local_path).suffix.lower().lstrip(".") or "unknown") for document in documents)
        )
        sample_file_urls = _sample(document.download_url for document in documents)
        sample_file_paths = _sample(document.local_path for document in documents)

        return BootstrapSiteEvidence(
            display_name=display_name,
            site_key=site_key,
            seed_url=scope.seed_url,
            scope_id=scope.id,
            run_id=run.id,
            pages=run.pages_seen,
            files=run.files_seen,
            top_sections=top_sections,
            section_hubs=section_hubs,
            top_file_source_pages=top_file_source_pages,
            source_page_evidence=source_page_evidence,
            sample_page_urls=sample_page_urls,
            sample_file_urls=sample_file_urls,
            sample_file_paths=sample_file_paths,
            file_type_counts=file_type_counts,
        )

    def classify_site_sources(self, evidence: BootstrapSiteEvidence) -> list[SourcePageClassification]:
        if not evidence.source_page_evidence:
            return []
        heuristic_items = [self._heuristic_classification(item) for item in evidence.source_page_evidence]
        if not settings.openai_api_key:
            return heuristic_items

        ai_items = self._classify_site_sources_with_ai(evidence)
        if not ai_items:
            return heuristic_items

        heuristics_by_url = {item.page_url: item for item in heuristic_items}
        ai_by_url = {item.page_url: item for item in ai_items}
        merged: list[SourcePageClassification] = []
        for source in evidence.source_page_evidence:
            heuristic = heuristics_by_url[source.page_url]
            ai_item = ai_by_url.get(source.page_url)
            if ai_item is None or heuristic.source_category != "general_reference":
                merged.append(heuristic)
                continue
            merged.append(ai_item)
        return merged

    def _heuristic_classification(self, source: SourcePageEvidence) -> SourcePageClassification:
        page_path = urlsplit(source.page_url).path.lower()
        url_title_haystack = " ".join(
            [
                page_path,
                source.page_title,
            ]
        ).lower()
        excerpt_haystack = (source.page_excerpt or "").lower()
        category = "general_reference"
        importance = "medium"
        conversion_priority = "medium"
        reason = "General site content with supporting files; convert only when future file changes look material."

        governance_tokens = (
            "governance",
            "board",
            "leadership",
            "election",
            "elections",
            "bylaws",
            "policy",
            "policies",
            "guideline",
            "guidelines",
            "statute",
            "professionalism",
            "annual-report",
            "annual reports",
            "annual report",
            "council",
            "committee-meetings",
        )
        finance_tokens = (
            "finances",
            "financial",
            "budget",
            "budgets",
            "statement",
            "statements",
            "audited",
            "audit",
            "accounting",
        )
        research_tokens = (
            "research",
            "publication",
            "publications",
            "journal",
            "paper series",
            "white paper",
            "monograph",
            "working paper",
            "actuarial-review",
        )
        membership_tokens = (
            "membership",
            "member",
            "dues",
            "sponsorship",
            "advertising",
            "partners",
            "directory",
            "contact",
        )
        exam_tokens = (
            "exam-req",
            "/education/exam",
            "/exam",
            "exams-admissions",
            "exam ",
            "exam-",
            "syllabus",
            "study materials",
            "credential",
            "pass marks",
            "asa-req",
            "asa pathway",
            "vee",
            "admissions",
        )
        news_tokens = ("news", "announcement", "press", "call for nominations")

        if any(token in url_title_haystack for token in exam_tokens):
            category = "exam_education"
            importance = "low"
            conversion_priority = "skip"
            reason = "This source page is exam or education related, but `exam_education` is outside this project's default monitoring scope."
        elif any(token in url_title_haystack for token in governance_tokens):
            category = "governance_management"
            importance = "low"
            conversion_priority = "skip"
            reason = "This source page is governance or management related, but `governance_management` is outside this project's default monitoring scope."
        elif any(token in url_title_haystack for token in finance_tokens):
            category = "finance_reports"
            importance = "high"
            conversion_priority = "high"
            reason = "This source page centers official financial statements, budgets, or accounting materials that are worth converting when files change."
        elif any(token in url_title_haystack for token in research_tokens):
            category = "research_publications"
            importance = "high" if source.file_count >= 3 else "medium"
            conversion_priority = "high" if source.file_count >= 3 else "medium"
            reason = "This source page appears to host research or publication materials, which are good candidates for later conversion and analysis."
        elif any(token in url_title_haystack for token in membership_tokens):
            category = "membership_operations"
            importance = "medium" if source.file_count >= 2 else "low"
            conversion_priority = "medium" if source.file_count >= 2 else "low"
            reason = "This source page is mostly operational or membership content, so keep the files but convert only when the updates look important."
        elif any(token in url_title_haystack for token in news_tokens):
            category = "news_announcements"
            importance = "medium"
            conversion_priority = "low"
            reason = "This source page is announcement-oriented. Monitor new files, but do not convert all baseline documents by default."
        elif any(token in excerpt_haystack for token in ("research", "paper", "publication", "journal", "white paper")):
            category = "research_publications"
            importance = "medium"
            conversion_priority = "medium"
            reason = "The page excerpt suggests a research or publication hub, so these files are worth selective conversion."
        elif any(token in excerpt_haystack for token in ("archive", "historical", "1998", "1999", "2000", "2001", "2002")):
            category = "archive_reference"
            importance = "low"
            conversion_priority = "skip"
            reason = "This source page looks archival. Keep downloads and evidence, but avoid bulk conversion unless a later change makes a file relevant."

        return SourcePageClassification(
            page_url=source.page_url,
            file_count=source.file_count,
            page_title=source.page_title,
            source_category=category,
            business_importance=importance,
            conversion_priority=conversion_priority,
            classification_reason=reason,
            sample_file_urls=source.sample_file_urls,
        )

    def _classify_site_sources_with_ai(self, evidence: BootstrapSiteEvidence) -> list[SourcePageClassification]:
        allowed_categories = [
            "exam_education",
            "governance_management",
            "finance_reports",
            "research_publications",
            "membership_operations",
            "news_announcements",
            "archive_reference",
            "general_reference",
        ]
        prompt_lines = []
        for item in evidence.source_page_evidence:
            prompt_lines.append(
                json.dumps(
                    {
                        "page_url": item.page_url,
                        "file_count": item.file_count,
                        "page_title": item.page_title,
                        "page_excerpt": item.page_excerpt,
                        "sample_file_urls": item.sample_file_urls[:5],
                    },
                    ensure_ascii=True,
                )
            )
        prompt = (
            "You are classifying document source pages from a monitored website baseline.\n"
            "Return strict JSON with one top-level key named `classifications` whose value is an array.\n"
            "Each array item must contain: page_url, source_category, business_importance, conversion_priority, classification_reason.\n"
            f"Allowed source_category values: {', '.join(allowed_categories)}.\n"
            "Allowed business_importance values: high, medium, low.\n"
            "Allowed conversion_priority values: high, medium, low, skip.\n"
            "Judge business importance from page purpose, not from raw file count alone.\n"
            "For baseline conversion, only use `high` when a source page is likely to produce substantively important documents.\n"
            "For this project, `exam_education` and `governance_management` are generally out of scope and should default to low importance with low or skip conversion unless explicitly requested.\n"
            "Keep classification_reason to one concise sentence.\n\n"
            f"Site: {evidence.display_name}\n"
            "Source pages:\n"
            + "\n".join(prompt_lines)
        )

        try:
            response = self.client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=1600,
            )
            content = (response.choices[0].message.content or "").strip()
            start = content.find("{")
            end = content.rfind("}")
            payload = json.loads(content[start : end + 1] if start >= 0 and end >= 0 else content)
            raw_items = payload.get("classifications", [])
            by_url = {item.page_url: item for item in evidence.source_page_evidence}
            classifications: list[SourcePageClassification] = []
            for raw in raw_items:
                page_url = str(raw.get("page_url", "")).strip()
                source = by_url.get(page_url)
                if source is None:
                    continue
                classifications.append(
                    SourcePageClassification(
                        page_url=page_url,
                        file_count=source.file_count,
                        page_title=source.page_title,
                        source_category=self._normalize_category(str(raw.get("source_category", "general_reference"))),
                        business_importance=self._normalize_importance(str(raw.get("business_importance", "medium"))),
                        conversion_priority=self._normalize_conversion_priority(
                            str(raw.get("conversion_priority", "medium"))
                        ),
                        classification_reason=str(raw.get("classification_reason", "")).strip()
                        or "AI classification produced no explicit reason.",
                        sample_file_urls=source.sample_file_urls,
                    )
                )
            if classifications:
                classifications.sort(key=lambda item: (-item.file_count, item.page_url))
                return classifications
        except Exception:
            return []
        return []

    def _normalize_category(self, value: str) -> str:
        allowed = {
            "exam_education",
            "governance_management",
            "finance_reports",
            "research_publications",
            "membership_operations",
            "news_announcements",
            "archive_reference",
            "general_reference",
        }
        normalized = (value or "").strip().lower()
        return normalized if normalized in allowed else "general_reference"

    def _normalize_importance(self, value: str) -> str:
        allowed = {"high", "medium", "low"}
        normalized = (value or "").strip().lower()
        return normalized if normalized in allowed else "medium"

    def _normalize_conversion_priority(self, value: str) -> str:
        allowed = {"high", "medium", "low", "skip"}
        normalized = (value or "").strip().lower()
        return normalized if normalized in allowed else "medium"

    def render_markdown(
        self,
        evidences: list[BootstrapSiteEvidence],
        *,
        catalog: str,
        ai_summary_md: str = "",
    ) -> str:
        generated_at = datetime.now(timezone.utc).isoformat()
        total_pages = sum(item.pages for item in evidences)
        total_files = sum(item.files for item in evidences)

        lines = [
            "# Explain Tree Bootstrap",
            "",
            "## Final Conclusion",
            "",
            f"- Conclusion time: `{generated_at}`",
            f"- Catalog: `{catalog}`",
            f"- Sites explained: `{len(evidences)}`",
            f"- Baseline inventory: pages=`{total_pages}`, files=`{total_files}`",
            "- Interpretation mode: `first-run baseline explanation`",
            "- Operational note: this report explains what the initial site tree contains; it is not a change report.",
            "",
        ]

        if ai_summary_md.strip():
            lines.extend(["## AI Interpretation", "", ai_summary_md.strip(), ""])

        lines.extend(
            [
                "## Evidence Summary",
                "",
                "| Site | Scope ID | Run ID | Pages | Files | Top sections | Top file source page |",
                "|---|---:|---:|---:|---:|---|---|",
            ]
        )
        for evidence in evidences:
            top_sections = ", ".join(f"{item.label} ({item.page_count})" for item in evidence.top_sections[:3]) or "-"
            top_source = (
                f"{evidence.top_file_source_pages[0].page_url} ({evidence.top_file_source_pages[0].file_count})"
                if evidence.top_file_source_pages
                else "-"
            )
            lines.append(
                f"| {evidence.display_name} | {evidence.scope_id} | {evidence.run_id} | {evidence.pages} | {evidence.files} | "
                f"{top_sections} | {top_source} |"
            )

        lines.extend(["", "## Source Classification", ""])
        lines.extend(
            [
                "| Site | Source page | Files | source_category | business_importance | conversion_priority | classification_reason |",
                "|---|---|---:|---|---|---|---|",
            ]
        )
        classification_rows = 0
        for evidence in evidences:
            for item in evidence.source_page_classifications:
                classification_rows += 1
                lines.append(
                    f"| {evidence.display_name} | {item.page_url} | {item.file_count} | {item.source_category} | "
                    f"{item.business_importance} | {item.conversion_priority} | {item.classification_reason} |"
                )
        if classification_rows == 0:
            lines.append("| - | - | 0 | - | - | - | - |")

        lines.extend(["", "## Site Notes", ""])
        for evidence in evidences:
            lines.append(f"### {evidence.display_name}")
            lines.append("")
            lines.append(f"- Site key: `{evidence.site_key}`")
            lines.append(f"- Seed URL: `{evidence.seed_url}`")
            lines.append(f"- Baseline scope/run: `scope_id={evidence.scope_id}`, `run_id={evidence.run_id}`")
            lines.append(f"- Inventory size: pages=`{evidence.pages}`, files=`{evidence.files}`")
            if evidence.top_sections:
                lines.append(
                    "- Top sections: "
                    + ", ".join(f"`{item.label}` ({item.page_count})" for item in evidence.top_sections)
                )
            if evidence.section_hubs:
                lines.append("- Section hubs to watch: " + ", ".join(f"`{url}`" for url in evidence.section_hubs))
            if evidence.top_file_source_pages:
                lines.append(
                    "- Document-rich source pages: "
                    + ", ".join(f"`{item.page_url}` ({item.file_count})" for item in evidence.top_file_source_pages)
                )
            if evidence.source_page_classifications:
                lines.append("- Classified source pages:")
                for item in evidence.source_page_classifications:
                    lines.append(
                        f"  - `{item.page_url}` -> "
                        f"`{item.source_category}` / importance=`{item.business_importance}` / conversion=`{item.conversion_priority}`; "
                        f"{item.classification_reason}"
                    )
            if evidence.file_type_counts:
                file_types = ", ".join(f"`{kind}`={count}" for kind, count in sorted(evidence.file_type_counts.items()))
                lines.append(f"- File type mix: {file_types}")
            if evidence.sample_file_urls:
                lines.append("- Sample file URLs: " + ", ".join(f"`{url}`" for url in evidence.sample_file_urls))
            if evidence.sample_file_paths:
                lines.append("- Sample local paths: " + ", ".join(f"`{path}`" for path in evidence.sample_file_paths))
            lines.append(
                "- `doc_to_md` suggestion: convert only files from stable document-rich source pages or future new/changed files, "
                "not the whole baseline by default."
            )
            lines.append("")

        return "\n".join(lines)

    def generate_ai_summary(self, evidences: list[BootstrapSiteEvidence], *, catalog: str) -> str:
        if not settings.openai_api_key:
            return ""

        evidence_lines: list[str] = []
        for evidence in evidences:
            evidence_lines.append(
                f"- {evidence.display_name}: pages={evidence.pages}, files={evidence.files}, "
                f"top_sections={', '.join(f'{item.label}:{item.page_count}' for item in evidence.top_sections[:5]) or 'none'}, "
                f"doc_source_pages={', '.join(f'{item.page_url}:{item.file_count}' for item in evidence.top_file_source_pages[:5]) or 'none'}, "
                f"classified_sources={', '.join(f'{item.page_url}:{item.source_category}:{item.business_importance}:{item.conversion_priority}' for item in evidence.source_page_classifications[:8]) or 'none'}, "
                f"sample_files={', '.join(evidence.sample_file_urls[:5]) or 'none'}"
            )

        prompt = (
            "You are helping explain a first-run website tree baseline to a human and an agent.\n"
            "This is not a change report. Explain what the monitored site tree currently contains, "
            "which sections look most important, where documents appear concentrated, which source categories matter most, "
            "and which areas should be prioritized later for change tracking or doc_to_md conversion.\n"
            "Keep the output concise markdown with one short overall conclusion and one short bullet per site.\n\n"
            f"Catalog: {catalog}\n"
            f"Generated at: {datetime.now(timezone.utc).isoformat()}\n"
            "Evidence:\n"
            + "\n".join(evidence_lines)
        )

        try:
            response = self.client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=1200,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            return f"_AI explanation unavailable: {exc}_"
