from pathlib import Path

from web_listening.blocks.section_classifier import SectionClassifier, load_section_inventory, render_markdown, render_yaml_text
from web_listening.blocks.section_discovery import CatalogSectionInventory, ExpansionCandidate, SectionSummary, SiteSectionInventory, render_yaml


def make_inventory() -> CatalogSectionInventory:
    return CatalogSectionInventory(
        catalog="dev",
        generated_at="2026-04-06T00:00:00+00:00",
        discovery_depth=2,
        section_depth=3,
        max_pages=20,
        sites=[
            SiteSectionInventory(
                site_key="demo",
                display_name="Demo",
                seed_url="https://example.com/",
                homepage_url="https://example.com/",
                fetch_mode="http",
                allowed_page_prefixes=["/"],
                allowed_file_prefixes=["/"],
                discovery_depth=2,
                section_depth=3,
                max_pages=20,
                pages_discovered=8,
                pages_with_docs=4,
                unique_document_links=18,
                level2_pages_discovered=3,
                sampled_level3_pages=2,
                skipped_level3_candidate_pages=6,
                expansion_candidates=[
                    ExpansionCandidate(
                        branch_path="/education/exam-req",
                        candidate_category="exam_education",
                        sampled_pages=2,
                        discovered_candidate_pages=8,
                        skipped_candidate_pages=6,
                        reason="Expand exam branch.",
                    )
                ],
                sections=[
                    SectionSummary(
                        section_path="/education/exam-req",
                        section_level=2,
                        page_count=2,
                        page_with_docs_count=1,
                        doc_link_count=12,
                        sample_urls=["https://example.com/education/exam-req/syllabus"],
                        sample_titles=["Exam syllabus"],
                        candidate_category="exam_education",
                    ),
                    SectionSummary(
                        section_path="/guidelines-and-policies",
                        section_level=1,
                        page_count=1,
                        page_with_docs_count=1,
                        doc_link_count=4,
                        sample_urls=["https://example.com/guidelines-and-policies"],
                        sample_titles=["Guidelines and policies"],
                        candidate_category="governance_management",
                    ),
                    SectionSummary(
                        section_path="/about/membership",
                        section_level=2,
                        page_count=2,
                        page_with_docs_count=0,
                        doc_link_count=0,
                        sample_urls=["https://example.com/about/membership"],
                        sample_titles=["Membership requirements"],
                        candidate_category="membership_operations",
                    ),
                ],
            )
        ],
    )


def test_load_section_inventory_round_trips_yaml(tmp_path: Path):
    inventory = make_inventory()
    path = tmp_path / "inventory.yaml"
    path.write_text(render_yaml(inventory.to_dict()), encoding="utf-8")

    loaded = load_section_inventory(path)

    assert loaded.catalog == "dev"
    assert loaded.sites[0].site_key == "demo"
    assert loaded.sites[0].sections[0].section_path == "/education/exam-req"


def test_heuristic_classification_sets_business_fields():
    classifier = SectionClassifier()
    site = classifier.classify_site(make_inventory().sites[0], use_ai=False)
    by_path = {section.section_path: section for section in site.sections}

    assert by_path["/education/exam-req"].source_category == "exam_education"
    assert by_path["/education/exam-req"].business_importance == "low"
    assert by_path["/education/exam-req"].conversion_priority == "skip"

    assert by_path["/guidelines-and-policies"].source_category == "governance_management"
    assert by_path["/guidelines-and-policies"].business_importance == "low"
    assert by_path["/guidelines-and-policies"].conversion_priority == "skip"

    assert by_path["/about/membership"].source_category == "membership_operations"
    assert by_path["/about/membership"].business_importance == "low"
    assert by_path["/about/membership"].conversion_priority == "skip"
    priorities = {branch.branch_path: branch for branch in site.level2_priorities}
    assert priorities["/education/exam-req"].business_importance == "low"
    assert priorities["/education/exam-req"].expansion_priority == "low"
    assert priorities["/about/membership"].business_importance == "low"


def test_render_outputs_include_requested_fields():
    classifier = SectionClassifier()
    classification = classifier.classify_inventory(make_inventory(), inventory_path="data/plans/demo.yaml", use_ai=False)

    yaml_text = render_yaml_text(classification)
    markdown = render_markdown(classification)

    assert "source_category" in yaml_text
    assert "business_importance" in yaml_text
    assert "conversion_priority" in yaml_text
    assert "classification_reason" in yaml_text
    assert "level2_priorities" in yaml_text
    assert "# Classify Site Sections" in markdown
    assert "planning artifact" in markdown.lower()
    assert "Level-2 Branch Priority" in markdown
    assert "Level-1 Overview" in markdown
    assert "not the number of top-level directories" in markdown


def test_ai_refinement_does_not_overwrite_specific_heuristics():
    classifier = SectionClassifier()
    site = make_inventory().sites[0]

    def fake_ai(_site):
        return [
            classifier._heuristic_classification(site.sections[0]),
            classifier._heuristic_classification(site.sections[1]),
            type(classifier._heuristic_classification(site.sections[2]))(
                section_path="/about/membership",
                section_level=2,
                page_count=2,
                page_with_docs_count=0,
                doc_link_count=0,
                sample_urls=["https://example.com/about/membership"],
                sample_titles=["Membership requirements"],
                candidate_category="membership_operations",
                source_category="membership_operations",
                business_importance="high",
                conversion_priority="high",
                classification_reason="Over-eager AI classification.",
            ),
        ]

    classifier._classify_site_with_ai = fake_ai
    classified = classifier.classify_site(site, use_ai=True)
    by_path = {section.section_path: section for section in classified.sections}

    assert by_path["/about/membership"].business_importance == "low"
    assert by_path["/about/membership"].conversion_priority == "skip"


def test_about_root_stays_general_reference():
    classifier = SectionClassifier()
    site = make_inventory().sites[0]
    site.sections.append(
        SectionSummary(
            section_path="/about",
            section_level=1,
            page_count=5,
            page_with_docs_count=2,
            doc_link_count=6,
            sample_urls=["https://example.com/about"],
            sample_titles=["About Example Org", "Working Papers"],
            candidate_category="research_publications",
        )
    )

    classified = classifier.classify_site(site, use_ai=False)
    by_path = {section.section_path: section for section in classified.sections}

    assert by_path["/about"].source_category == "general_reference"
    assert by_path["/about"].business_importance == "medium"


def test_ai_refinement_does_not_override_about_root():
    classifier = SectionClassifier()
    site = make_inventory().sites[0]
    site.sections.append(
        SectionSummary(
            section_path="/about",
            section_level=1,
            page_count=5,
            page_with_docs_count=2,
            doc_link_count=6,
            sample_urls=["https://example.com/about"],
            sample_titles=["About Example Org", "Working Papers"],
            candidate_category="research_publications",
        )
    )

    def fake_ai(_site):
        sections = []
        for section in site.sections:
            classified = classifier._heuristic_classification(section)
            if section.section_path == "/about":
                classified = type(classified)(
                    section_path=classified.section_path,
                    section_level=classified.section_level,
                    page_count=classified.page_count,
                    page_with_docs_count=classified.page_with_docs_count,
                    doc_link_count=classified.doc_link_count,
                    sample_urls=classified.sample_urls,
                    sample_titles=classified.sample_titles,
                    candidate_category=classified.candidate_category,
                    source_category="governance_management",
                    business_importance="high",
                    conversion_priority="high",
                    classification_reason="Over-eager AI classification for root about.",
                )
            sections.append(classified)
        return sections

    classifier._classify_site_with_ai = fake_ai
    classified = classifier.classify_site(site, use_ai=True)
    by_path = {section.section_path: section for section in classified.sections}

    assert by_path["/about"].source_category == "general_reference"
    assert by_path["/about"].business_importance == "medium"
