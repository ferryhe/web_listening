from pathlib import Path

from web_listening.blocks.monitor_scope_planner import (
    build_monitor_scope,
    load_monitor_scope_plan,
    minimize_prefixes,
    monitor_scope_to_tree_target,
    render_markdown,
    render_yaml_text,
)


def test_minimize_prefixes_drops_redundant_children_but_keeps_case_variants():
    result = minimize_prefixes(
        [
            "/research/topics",
            "/research",
            "/news-and-publications",
            "/News-and-Publications",
            "/research/opportunities",
        ]
    )

    assert result == ["/research", "/news-and-publications", "/News-and-Publications"]


def test_build_monitor_scope_compiles_selected_scope(tmp_path: Path):
    classification_path = tmp_path / "classification.yaml"
    classification_path.write_text(
        """
catalog: "dev"
sites:
  - site_key: "soa"
    display_name: "SOA"
    seed_url: "https://www.soa.org/"
    homepage_url: "https://www.soa.org/"
    fetch_mode: "http"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    selection_path = tmp_path / "selection.yaml"
    selection_path.write_text(
        """
site_key: "soa"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Keep research and publication surfaces."
based_on:
  section_classification: "ignored-by-test.yaml"
selected_sections:
  - path: "/research"
    selection_reason: "Keep root."
  - path: "/research/topics"
    selection_reason: "Covered by root."
  - path: "/news-and-publications"
    selection_reason: "Keep canonical branch."
  - path: "/News-and-Publications"
    selection_reason: "Keep case alias."
rejected_sections:
  - path: "/education"
    selection_reason: "Skip exam content."
deferred_sections:
  - path: "/programs"
    selection_reason: "Review later."
excluded_categories:
  - "exam_education"
excluded_prefixes:
  - "/globalassets"
selection_notes:
  - "Test note."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    plan = build_monitor_scope(selection_path, classification_path=classification_path)

    assert plan.site_key == "soa"
    assert plan.display_name == "SOA"
    assert plan.catalog == "dev"
    assert plan.allowed_page_prefixes == ["/research", "/news-and-publications", "/News-and-Publications"]
    assert plan.allowed_file_prefixes == ["/"]
    assert plan.selected_focus_prefixes == ["/research/topics"]
    assert plan.excluded_page_prefixes == ["/education", "/globalassets"]
    assert plan.deferred_page_prefixes == ["/programs"]
    assert plan.fetch_mode == "http"
    assert plan.max_depth == 4
    assert plan.max_pages == 120
    assert plan.max_files == 40
    assert plan.based_on["section_selection"].endswith("selection.yaml")
    assert plan.based_on["section_classification"].endswith("classification.yaml")


def test_build_monitor_scope_can_match_file_scope_to_selected_pages(tmp_path: Path):
    classification_path = tmp_path / "classification.yaml"
    classification_path.write_text(
        """
catalog: "dev"
sites:
  - site_key: "soa"
    display_name: "SOA"
    seed_url: "https://www.soa.org/"
    homepage_url: "https://www.soa.org/"
    fetch_mode: "http"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    selection_path = tmp_path / "selection.yaml"
    selection_path.write_text(
        """
site_key: "soa"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Keep research and publication surfaces."
selected_sections:
  - path: "/publications"
    selection_reason: "Keep publications."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    plan = build_monitor_scope(
        selection_path,
        classification_path=classification_path,
        file_scope_mode="selected_pages",
    )

    assert plan.allowed_page_prefixes == ["/publications"]
    assert plan.allowed_file_prefixes == ["/publications"]


def test_render_outputs_include_compiled_scope_sections(tmp_path: Path):
    classification_path = tmp_path / "classification.yaml"
    classification_path.write_text(
        """
catalog: "dev"
sites:
  - site_key: "soa"
    display_name: "SOA"
    seed_url: "https://www.soa.org/"
    homepage_url: "https://www.soa.org/"
    fetch_mode: "http"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    selection_path = tmp_path / "selection.yaml"
    selection_path.write_text(
        """
site_key: "soa"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Keep research and publication surfaces."
selected_sections:
  - path: "/research"
    selection_reason: "Keep research."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    plan = build_monitor_scope(selection_path, classification_path=classification_path)
    markdown = render_markdown(plan)
    yaml_text = render_yaml_text(plan)

    assert "# Plan Monitor Scope" in markdown
    assert "Allowed Page Prefixes" in markdown
    assert "file_scope_mode" in markdown
    assert "allowed_page_prefixes" in yaml_text
    assert "selected_focus_prefixes" in yaml_text


def test_monitor_scope_round_trips_into_tree_target(tmp_path: Path):
    classification_path = tmp_path / "classification.yaml"
    classification_path.write_text(
        """
catalog: "dev"
sites:
  - site_key: "soa"
    display_name: "SOA"
    seed_url: "https://www.soa.org/"
    homepage_url: "https://www.soa.org/"
    fetch_mode: "http"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    selection_path = tmp_path / "selection.yaml"
    selection_path.write_text(
        """
site_key: "soa"
generated_at: "2026-04-07T01:20:54-04:00"
selection_mode: "manual_with_agent_assist"
review_status: "recommended_draft"
business_goal: "Keep research and publication surfaces."
selected_sections:
  - path: "/research"
    selection_reason: "Keep research."
  - path: "/news-and-publications"
    selection_reason: "Keep publications."
excluded_prefixes:
  - "/education"
selection_notes:
  - "Test note."
""".strip()
        + "\n",
        encoding="utf-8",
    )

    plan = build_monitor_scope(selection_path, classification_path=classification_path)
    scope_path = tmp_path / "monitor_scope.yaml"
    scope_path.write_text(render_yaml_text(plan), encoding="utf-8")

    loaded = load_monitor_scope_plan(scope_path)
    target = monitor_scope_to_tree_target(loaded)

    assert loaded.allowed_page_prefixes == ["/research", "/news-and-publications"]
    assert loaded.allowed_file_prefixes == ["/"]
    assert target.site_key == "soa"
    assert target.catalog == "dev"
    assert target.seed_url == "https://www.soa.org/"
    assert target.allowed_page_prefixes == ["/research", "/news-and-publications"]
    assert target.allowed_file_prefixes == ["/"]
    assert target.tree_max_depth == 4
    assert target.tree_max_pages == 120
    assert target.tree_max_files == 40
