from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


def _parse_string_list(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in value.split(",") if item.strip()]
        else:
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
            if parsed is None:
                return []
            return [str(parsed).strip()] if str(parsed).strip() else []
        return parsed
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [] if value is None else [str(value).strip()]


class Site(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    url: str
    name: str = ""
    tags: List[str] = Field(default_factory=list)
    fetch_mode: str = "http"
    fetch_config_json: dict = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    is_active: bool = True

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [t.strip() for t in v.split(",") if t.strip()]
        return v or []

    @field_validator("fetch_mode", mode="before")
    @classmethod
    def parse_fetch_mode(cls, v):
        mode = (v or "http").strip().lower()
        if mode not in {"http", "browser", "auto"}:
            raise ValueError("fetch_mode must be one of: http, browser, auto")
        return mode

    @field_validator("fetch_config_json", mode="before")
    @classmethod
    def parse_fetch_config_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v or {}


class SiteSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    site_id: int
    captured_at: Optional[datetime] = None
    content_hash: str
    raw_html: str = ""
    cleaned_html: str = ""
    content_text: str = ""
    markdown: str = ""
    fit_markdown: str = ""
    metadata_json: dict = Field(default_factory=dict)
    fetch_mode: str = "http"
    final_url: str = ""
    status_code: Optional[int] = None
    links: List[str] = Field(default_factory=list)

    @field_validator("metadata_json", mode="before")
    @classmethod
    def parse_metadata_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v or {}

    @field_validator("links", mode="before")
    @classmethod
    def parse_links(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v or []


class Change(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    site_id: int
    detected_at: Optional[datetime] = None
    change_type: str  # new_content | new_links | new_document
    summary: str = ""
    diff_snippet: str = ""


class Document(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    site_id: int
    title: str = ""
    url: str
    download_url: str
    institution: str = ""
    page_url: str = ""
    published_at: Optional[datetime] = None
    downloaded_at: Optional[datetime] = None
    local_path: str = ""
    doc_type: str = ""
    sha256: str = ""
    file_size: Optional[int] = None
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""
    content_md: str = ""
    content_md_status: str = "pending"
    content_md_updated_at: Optional[datetime] = None
    tracked_local_path: str = ""

    @computed_field(return_type=str)
    @property
    def preferred_display_path(self) -> str:
        return self.tracked_local_path or self.local_path


class AnalysisReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    period_start: datetime
    period_end: datetime
    generated_at: Optional[datetime] = None
    site_ids: List[int] = Field(default_factory=list)
    summary_md: str = ""
    change_count: int = 0

    @field_validator("site_ids", mode="before")
    @classmethod
    def parse_site_ids(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v or []


class Job(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: Optional[int] = None
    job_type: str
    status: str = "queued"
    stage: str = "accepted"
    stage_message: str = ""
    progress: int = 0
    scope_id: Optional[int] = None
    run_id: Optional[int] = None
    produced_artifacts: dict[str, object] = Field(default_factory=dict)
    artifact_summary: dict[str, object] = Field(default_factory=dict)
    error: str = ""
    error_code: str = ""
    error_detail: dict[str, object] = Field(default_factory=dict)
    is_retryable: bool = False
    accepted_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @field_validator("produced_artifacts", "artifact_summary", "error_detail", mode="before")
    @classmethod
    def parse_job_dict_payloads(cls, value):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return value if isinstance(value, dict) else {}

    def next_recommended_action(self) -> str:
        if self.status in {"queued", "running"}:
            return "poll_job_status"
        if self.status == "failed":
            return "inspect_job_error"
        if self.produced_artifacts:
            return "read_job_artifacts"
        return "inspect_job_record"

    def to_delivery_payload(self) -> dict[str, object]:
        return {
            "job": {
                "job_id": self.job_id,
                "job_type": self.job_type,
                "status": self.status,
                "stage": self.stage,
                "stage_message": self.stage_message,
                "progress": self.progress,
                "scope_id": self.scope_id,
                "run_id": self.run_id,
                "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            },
            "error": {
                "message": self.error,
                "code": self.error_code,
                "detail": self.error_detail,
                "is_retryable": self.is_retryable,
            },
            "artifacts": {
                "produced": self.produced_artifacts,
                "summary": self.artifact_summary,
            },
            "next_action": self.next_recommended_action(),
        }


class MonitorTask(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_name: str
    site_url: str
    task_description: str
    goal: str
    focus_topics: List[str] = Field(default_factory=list)
    must_track_prefixes: List[str] = Field(default_factory=list)
    exclude_prefixes: List[str] = Field(default_factory=list)
    prefer_file_types: List[str] = Field(default_factory=list)
    must_download_patterns: List[str] = Field(default_factory=list)
    run_schedule: dict[str, object] = Field(default_factory=dict)
    baseline_expectations: dict[str, object] = Field(default_factory=dict)
    file_policy: dict[str, object] = Field(default_factory=dict)
    report_style: str = "briefing"
    report_policy: dict[str, object] = Field(default_factory=dict)
    change_severity_rules: dict[str, str] = Field(
        default_factory=lambda: {
            "new_page": "medium",
            "changed_page": "medium",
            "missing_page": "medium",
            "new_file": "high",
            "changed_file": "medium",
            "missing_file": "medium",
        }
    )
    alert_policy: dict[str, object] = Field(default_factory=dict)
    human_review_rules: List[str] = Field(default_factory=list)
    handoff_requirements: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

    @field_validator(
        "run_schedule",
        "baseline_expectations",
        "file_policy",
        "report_policy",
        "alert_policy",
        mode="before",
    )
    @classmethod
    def parse_monitor_task_dicts(cls, value):
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return value or {}

    @field_validator(
        "focus_topics",
        "must_track_prefixes",
        "exclude_prefixes",
        "prefer_file_types",
        "must_download_patterns",
        "human_review_rules",
        "handoff_requirements",
        "notes",
        mode="before",
    )
    @classmethod
    def parse_monitor_task_lists(cls, value):
        return _parse_string_list(value)


class CrawlScope(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    site_id: int
    seed_url: str
    allowed_origin: str = ""
    allowed_page_prefixes: List[str] = Field(default_factory=list)
    allowed_file_prefixes: List[str] = Field(default_factory=list)
    max_depth: int = 3
    max_pages: int = 100
    max_files: int = 20
    follow_files: bool = True
    fetch_mode: str = "http"
    fetch_config_json: dict = Field(default_factory=dict)
    is_initialized: bool = False
    baseline_run_id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("allowed_page_prefixes", "allowed_file_prefixes", mode="before")
    @classmethod
    def parse_prefix_lists(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [v] if v else []
        return v or []

    @field_validator("fetch_mode", mode="before")
    @classmethod
    def parse_scope_fetch_mode(cls, v):
        mode = (v or "http").strip().lower()
        if mode not in {"http", "browser", "auto"}:
            raise ValueError("fetch_mode must be one of: http, browser, auto")
        return mode

    @field_validator("fetch_config_json", mode="before")
    @classmethod
    def parse_scope_fetch_config_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v or {}


class CrawlRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    scope_id: int
    run_type: str = "bootstrap"
    status: str = "queued"
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    pages_seen: int = 0
    files_seen: int = 0
    pages_changed: int = 0
    files_changed: int = 0
    error_message: str = ""


class TrackedPage(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    scope_id: int
    canonical_url: str
    depth: int = 0
    first_seen_run_id: Optional[int] = None
    last_seen_run_id: Optional[int] = None
    miss_count: int = 0
    is_active: bool = True
    latest_snapshot_id: Optional[int] = None
    latest_hash: str = ""


class PageSnapshot(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    scope_id: int
    page_id: int
    run_id: int
    captured_at: Optional[datetime] = None
    content_hash: str
    raw_html: str = ""
    cleaned_html: str = ""
    content_text: str = ""
    markdown: str = ""
    fit_markdown: str = ""
    metadata_json: dict = Field(default_factory=dict)
    fetch_mode: str = "http"
    final_url: str = ""
    status_code: Optional[int] = None
    links: List[str] = Field(default_factory=list)

    @field_validator("metadata_json", mode="before")
    @classmethod
    def parse_page_metadata_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v or {}

    @field_validator("links", mode="before")
    @classmethod
    def parse_page_links(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v or []


class PageEdge(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    scope_id: int
    run_id: int
    from_page_id: int
    to_page_id: int


class TrackedFile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    scope_id: int
    canonical_url: str
    first_seen_run_id: Optional[int] = None
    last_seen_run_id: Optional[int] = None
    miss_count: int = 0
    is_active: bool = True
    latest_document_id: Optional[int] = None
    latest_sha256: str = ""


class FileObservation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    scope_id: int
    run_id: int
    page_id: int
    file_id: int
    document_id: Optional[int] = None
    discovered_url: str
    download_url: str
    tracked_local_path: str = ""
