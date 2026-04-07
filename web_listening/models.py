from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


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
