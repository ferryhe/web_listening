from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from web_listening.models import MonitorTask


DEFAULT_CHANGE_SEVERITY_RULES = {
    "new_page": "medium",
    "changed_page": "medium",
    "missing_page": "medium",
    "new_file": "high",
    "changed_file": "medium",
    "missing_file": "medium",
}


def build_default_task_path(site_key: str, now: datetime | None = None, *, data_dir: str | Path = "data") -> Path:
    moment = now or datetime.now(timezone.utc)
    task_date = moment.date().isoformat()
    normalized_site_key = str(site_key or "site").strip().lower().replace(" ", "-")
    return Path(data_dir) / "plans" / f"monitor_task_{normalized_site_key}_{task_date}.yaml"


def build_monitor_task(
    *,
    task_name: str,
    site_url: str,
    task_description: str,
    goal: str,
    focus_topics: list[str] | None = None,
    must_track_prefixes: list[str] | None = None,
    exclude_prefixes: list[str] | None = None,
    prefer_file_types: list[str] | None = None,
    must_download_patterns: list[str] | None = None,
    report_style: str = "briefing",
    change_severity_rules: dict[str, str] | None = None,
    handoff_requirements: list[str] | None = None,
    notes: list[str] | None = None,
) -> MonitorTask:
    rules = {**DEFAULT_CHANGE_SEVERITY_RULES, **(change_severity_rules or {})}
    return MonitorTask(
        task_name=task_name,
        site_url=site_url,
        task_description=task_description,
        goal=goal,
        focus_topics=focus_topics or [],
        must_track_prefixes=must_track_prefixes or [],
        exclude_prefixes=exclude_prefixes or [],
        prefer_file_types=prefer_file_types or [],
        must_download_patterns=must_download_patterns or [],
        report_style=report_style,
        change_severity_rules=rules,
        handoff_requirements=handoff_requirements or [],
        notes=notes or [],
    )


def load_monitor_task(path: str | Path) -> MonitorTask:
    payload: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return MonitorTask(**payload)


def render_yaml_text(task: MonitorTask) -> str:
    return yaml.safe_dump(
        task.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
