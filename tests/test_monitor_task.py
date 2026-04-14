from pathlib import Path

from web_listening.blocks.monitor_task import build_monitor_task, load_monitor_task, render_yaml_text
from web_listening.models import MonitorTask


def test_monitor_task_normalizes_list_fields_from_string_inputs():
    task = MonitorTask(
        task_name="research-watch",
        site_url="https://example.com/",
        task_description="Track research updates.",
        goal="Track research pages and downloadable reports.",
        focus_topics="research, reports, publications",
        must_track_prefixes="/research, /publications",
        exclude_prefixes="/contact, /about",
        prefer_file_types="pdf, docx",
        must_download_patterns="annual, report",
        handoff_requirements="yaml, markdown",
    )

    assert task.focus_topics == ["research", "reports", "publications"]
    assert task.must_track_prefixes == ["/research", "/publications"]
    assert task.exclude_prefixes == ["/contact", "/about"]
    assert task.prefer_file_types == ["pdf", "docx"]
    assert task.must_download_patterns == ["annual", "report"]
    assert task.handoff_requirements == ["yaml", "markdown"]


def test_monitor_task_defaults_report_style_and_severity_rules():
    task = MonitorTask(
        task_name="policy-watch",
        site_url="https://example.com/",
        task_description="Track policy changes.",
        goal="Watch policy notices.",
    )

    assert task.report_style == "briefing"
    assert task.change_severity_rules["new_file"] == "high"
    assert task.change_severity_rules["changed_file"] == "medium"
    assert task.change_severity_rules["changed_page"] == "medium"


def test_build_and_load_monitor_task_round_trip(tmp_path: Path):
    task = build_monitor_task(
        task_name="association-research-watch",
        site_url="https://example.com/",
        task_description="Monitor research and publication changes for an agent skill.",
        goal="Build a reusable agent-readable monitoring task.",
        focus_topics=["research", "publications"],
        must_track_prefixes=["/research", "/publications"],
        exclude_prefixes=["/contact"],
        prefer_file_types=["pdf"],
        must_download_patterns=["report"],
        report_style="briefing",
        notes=["Prioritize report-like files."],
    )
    task_path = tmp_path / "monitor_task.yaml"
    task_path.write_text(render_yaml_text(task), encoding="utf-8")

    loaded = load_monitor_task(task_path)

    assert loaded.task_name == "association-research-watch"
    assert loaded.goal == "Build a reusable agent-readable monitoring task."
    assert loaded.focus_topics == ["research", "publications"]
    assert loaded.must_track_prefixes == ["/research", "/publications"]
    assert loaded.prefer_file_types == ["pdf"]
    assert loaded.notes == ["Prioritize report-like files."]


def test_render_yaml_text_includes_key_fields():
    task = build_monitor_task(
        task_name="regulator-watch",
        site_url="https://example.com/",
        task_description="Watch regulator announcements.",
        goal="Find new announcements and files.",
        focus_topics=["announcements"],
    )

    yaml_text = render_yaml_text(task)

    assert "task_name: regulator-watch" in yaml_text
    assert "site_url: https://example.com/" in yaml_text
    assert "report_style: briefing" in yaml_text
    assert 'focus_topics:' in yaml_text
    assert 'change_severity_rules:' in yaml_text
