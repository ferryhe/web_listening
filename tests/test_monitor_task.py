from pathlib import Path

from web_listening.blocks.monitor_task import build_monitor_task, load_monitor_task, render_yaml_text
from web_listening.models import MonitorTask


def test_monitor_task_normalizes_list_and_policy_fields_from_string_inputs():
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
        run_schedule='{"cadence": "daily", "timezone": "UTC"}',
        baseline_expectations='{"expected_pages": 12}',
        file_policy='{"download": true, "extensions": ["pdf"]}',
        report_policy='{"include_artifact_index": true}',
        alert_policy='{"channels": ["email"]}',
        human_review_rules="new_file, missing_page",
        handoff_requirements="yaml, markdown",
    )

    assert task.focus_topics == ["research", "reports", "publications"]
    assert task.must_track_prefixes == ["/research", "/publications"]
    assert task.exclude_prefixes == ["/contact", "/about"]
    assert task.prefer_file_types == ["pdf", "docx"]
    assert task.must_download_patterns == ["annual", "report"]
    assert task.run_schedule == {"cadence": "daily", "timezone": "UTC"}
    assert task.baseline_expectations == {"expected_pages": 12}
    assert task.file_policy == {"download": True, "extensions": ["pdf"]}
    assert task.report_policy == {"include_artifact_index": True}
    assert task.alert_policy == {"channels": ["email"]}
    assert task.human_review_rules == ["new_file", "missing_page"]
    assert task.handoff_requirements == ["yaml", "markdown"]


def test_monitor_task_defaults_report_style_and_phase_two_policy_fields():
    task = MonitorTask(
        task_name="policy-watch",
        site_url="https://example.com/",
        task_description="Track policy changes.",
        goal="Watch policy notices.",
    )

    assert task.report_style == "briefing"
    assert task.run_schedule == {}
    assert task.baseline_expectations == {}
    assert task.file_policy == {}
    assert task.report_policy == {}
    assert task.alert_policy == {}
    assert task.human_review_rules == []
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
        run_schedule={"cadence": "daily"},
        baseline_expectations={"expected_pages": 8},
        file_policy={"download": True},
        report_style="briefing",
        report_policy={"include_review_queue": True},
        alert_policy={"channels": ["slack"]},
        human_review_rules=["new_file"],
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
    assert loaded.run_schedule == {"cadence": "daily"}
    assert loaded.baseline_expectations == {"expected_pages": 8}
    assert loaded.file_policy == {"download": True}
    assert loaded.report_policy == {"include_review_queue": True}
    assert loaded.alert_policy == {"channels": ["slack"]}
    assert loaded.human_review_rules == ["new_file"]
    assert loaded.notes == ["Prioritize report-like files."]


def test_render_yaml_text_includes_phase_two_contract_fields():
    task = build_monitor_task(
        task_name="regulator-watch",
        site_url="https://example.com/",
        task_description="Watch regulator announcements.",
        goal="Find new announcements and files.",
        focus_topics=["announcements"],
        run_schedule={"cadence": "weekly"},
        report_policy={"style": "briefing"},
        human_review_rules=["new_file"],
    )

    yaml_text = render_yaml_text(task)

    assert "task_name: regulator-watch" in yaml_text
    assert "site_url: https://example.com/" in yaml_text
    assert "report_style: briefing" in yaml_text
    assert "run_schedule:" in yaml_text
    assert "report_policy:" in yaml_text
    assert "human_review_rules:" in yaml_text
    assert "change_severity_rules:" in yaml_text


def test_monitor_task_supports_structured_severity_policy_and_legacy_yaml_defaults(tmp_path: Path):
    task = build_monitor_task(
        task_name="delivery-v3-watch",
        site_url="https://example.com/",
        task_description="Track delivery-v3 review severity.",
        goal="Escalate important report files.",
        severity_policy=[
            {
                "rule_type": "prefix",
                "match_value": "/research/urgent",
                "severity": "critical",
                "reason_template": "Urgent research path changed.",
                "recommended_action": "escalate_immediately",
                "weight": 90,
            }
        ],
    )
    task_path = tmp_path / "monitor_task_v3.yaml"
    task_path.write_text(render_yaml_text(task), encoding="utf-8")

    loaded = load_monitor_task(task_path)

    assert loaded.severity_policy[0]["rule_type"] == "prefix"
    assert loaded.severity_policy[0]["match_value"] == "/research/urgent"
    assert loaded.severity_policy[0]["severity"] == "critical"
    assert loaded.severity_policy[0]["recommended_action"] == "escalate_immediately"

    legacy_path = tmp_path / "legacy_monitor_task.yaml"
    legacy_path.write_text(
        """
task_name: legacy-watch
site_url: https://example.com/
task_description: Old task file without structured severity policy.
goal: Keep legacy task files working.
change_severity_rules:
  new_file: critical
""".strip()
        + "\n",
        encoding="utf-8",
    )

    legacy = load_monitor_task(legacy_path)

    assert legacy.severity_policy == []
    assert legacy.change_severity_rules["new_file"] == "critical"
