from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class Site(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    url: str
    name: str = ""
    tags: List[str] = []
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
    metadata_json: dict = {}
    fetch_mode: str = "http"
    final_url: str = ""
    status_code: Optional[int] = None
    links: List[str] = []

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


class AnalysisReport(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    period_start: datetime
    period_end: datetime
    generated_at: Optional[datetime] = None
    site_ids: List[int] = []
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
