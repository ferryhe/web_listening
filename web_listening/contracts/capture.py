from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from web_listening.contracts._protocol import (
    ExecutorId,
    JsonObject,
    NonEmptyString,
    Sha256,
    SkillVersion,
    StrictContractModel,
    require_aware_timestamp,
    validate_artifact_path,
    validate_http_url_without_credentials,
    validate_portable_json_field,
)


class CaptureLineage(StrictContractModel):
    site_key: NonEmptyString
    site_skill_id: NonEmptyString
    site_skill_version: SkillVersion
    site_skill_digest: Sha256
    recipe_id: NonEmptyString
    run_id: NonEmptyString
    scope_id: NonEmptyString


class CaptureRequest(CaptureLineage):
    schema_version: Literal["capture-request.v1"] = "capture-request.v1"
    request_id: NonEmptyString
    executor_id: ExecutorId
    url: AnyHttpUrl
    requested_at: datetime
    config: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)

    _validate_url = field_validator("url", mode="before")(
        validate_http_url_without_credentials
    )
    _validate_requested_at = field_validator("requested_at")(require_aware_timestamp)
    _validate_json = field_validator("config", "metadata")(
        validate_portable_json_field
    )


class CaptureError(StrictContractModel):
    code: NonEmptyString
    message: NonEmptyString
    retryable: bool = False
    metadata: JsonObject = Field(default_factory=dict)

    _validate_metadata = field_validator("metadata")(validate_portable_json_field)


class CaptureContent(StrictContractModel):
    media_type: NonEmptyString
    text: str | None = None
    artifact_path: str | None = None
    sha256: Sha256 | None = None
    metadata: JsonObject = Field(default_factory=dict)

    _validate_artifact_path = field_validator("artifact_path")(validate_artifact_path)
    _validate_metadata = field_validator("metadata")(validate_portable_json_field)

    @model_validator(mode="after")
    def require_content_pointer(self) -> CaptureContent:
        if self.text is None and self.artifact_path is None:
            raise ValueError("content requires text or artifact_path")
        return self


class CaptureResult(CaptureLineage):
    schema_version: Literal["capture-result.v1"] = "capture-result.v1"
    request_id: NonEmptyString
    executor_id: ExecutorId
    state: Literal["succeeded", "failed"]
    started_at: datetime
    finished_at: datetime
    final_url: AnyHttpUrl | None = None
    status_code: int | None = Field(default=None, ge=100, le=599)
    content: CaptureContent | None = None
    error: CaptureError | None = None
    metadata: JsonObject = Field(default_factory=dict)

    _validate_url = field_validator("final_url", mode="before")(
        validate_http_url_without_credentials
    )
    _validate_started_at = field_validator("started_at")(require_aware_timestamp)
    _validate_finished_at = field_validator("finished_at")(require_aware_timestamp)
    _validate_metadata = field_validator("metadata")(validate_portable_json_field)

    @model_validator(mode="after")
    def validate_state_and_timing(self) -> CaptureResult:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.state == "succeeded":
            if self.content is None or self.error is not None:
                raise ValueError("succeeded result requires content and forbids error")
        elif self.error is None or self.content is not None:
            raise ValueError("failed result requires error and forbids content")
        return self


__all__ = [
    "CaptureContent",
    "CaptureError",
    "CaptureLineage",
    "CaptureRequest",
    "CaptureResult",
]
