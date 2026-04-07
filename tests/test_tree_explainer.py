from web_listening.blocks.tree_explainer import (
    BootstrapSiteEvidence,
    SectionSummary,
    SourcePageClassification,
    SourcePageEvidence,
    SourcePageSummary,
    TreeBootstrapExplainer,
)


class DummyStorage:
    pass


def test_render_markdown_includes_evidence_sections():
    explainer = TreeBootstrapExplainer(DummyStorage())
    markdown = explainer.render_markdown(
        [
            BootstrapSiteEvidence(
                display_name="Demo",
                site_key="demo",
                seed_url="https://example.com",
                scope_id=1,
                run_id=2,
                pages=12,
                files=4,
                top_sections=[SectionSummary(label="/news", page_count=5)],
                section_hubs=["https://example.com/news"],
                top_file_source_pages=[SourcePageSummary(page_url="https://example.com/reports", file_count=4)],
                source_page_classifications=[
                    SourcePageClassification(
                        page_url="https://example.com/reports",
                        file_count=4,
                        page_title="Reports",
                        source_category="research_publications",
                        business_importance="high",
                        conversion_priority="high",
                        classification_reason="This page looks like a research reports hub.",
                    )
                ],
                sample_page_urls=["https://example.com/news"],
                sample_file_urls=["https://example.com/files/report.pdf"],
                sample_file_paths=["data/downloads/_blobs/aa/example.pdf"],
                file_type_counts={"pdf": 4},
            )
        ],
        catalog="dev",
        ai_summary_md="Overall baseline looks document-rich.",
    )

    assert "# Explain Tree Bootstrap" in markdown
    assert "Overall baseline looks document-rich." in markdown
    assert "Document-rich source pages" in markdown
    assert "`https://example.com/files/report.pdf`" in markdown
    assert "classification_reason" in markdown
    assert "This page looks like a research reports hub." in markdown


def test_heuristic_classification_deprioritizes_exam_pages():
    explainer = TreeBootstrapExplainer(DummyStorage())
    classification = explainer._heuristic_classification(
        SourcePageEvidence(
            page_url="https://example.com/education/exam-req/syllabus",
            file_count=12,
            page_title="Exam syllabus",
            page_excerpt="Candidate syllabus, study materials, and education requirements.",
            sample_file_urls=["https://example.com/files/syllabus.pdf"],
        )
    )

    assert classification.source_category == "exam_education"
    assert classification.business_importance == "low"
    assert classification.conversion_priority == "skip"


def test_heuristic_classification_prefers_governance_url_signals_over_excerpt_noise():
    explainer = TreeBootstrapExplainer(DummyStorage())
    classification = explainer._heuristic_classification(
        SourcePageEvidence(
            page_url="https://example.com/about/governance/annual-reports",
            file_count=12,
            page_title="Annual Reports",
            page_excerpt="Our annual report covers continuing education, research, outreach, and member activity.",
            sample_file_urls=["https://example.com/files/annual-report-2025.pdf"],
        )
    )

    assert classification.source_category == "governance_management"
    assert classification.business_importance == "low"
    assert classification.conversion_priority == "skip"


def test_heuristic_classification_marks_research_hubs_from_url_title():
    explainer = TreeBootstrapExplainer(DummyStorage())
    classification = explainer._heuristic_classification(
        SourcePageEvidence(
            page_url="https://example.com/biasandinsuranceresearch",
            file_count=14,
            page_title="Research Paper Series on Bias and Insurance",
            page_excerpt="The organization has produced research examining bias in insurance pricing.",
            sample_file_urls=["https://example.com/files/research-paper.pdf"],
        )
    )

    assert classification.source_category == "research_publications"
    assert classification.business_importance == "high"
    assert classification.conversion_priority == "high"


def test_heuristic_classification_keeps_exam_paths_above_finance_keywords():
    explainer = TreeBootstrapExplainer(DummyStorage())
    classification = explainer._heuristic_classification(
        SourcePageEvidence(
            page_url="https://example.com/exam/exam-6u-regulation-and-financial-reporting-us",
            file_count=2,
            page_title="Exam 6U Regulation and Financial Reporting",
            page_excerpt="Exam registration and candidate instructions.",
            sample_file_urls=["https://example.com/files/financial-reporting-sample-questions.xlsx"],
        )
    )

    assert classification.source_category == "exam_education"
    assert classification.business_importance == "low"
    assert classification.conversion_priority == "skip"
