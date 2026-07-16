from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from web_listening.contracts._protocol import NonEmptyString, StrictContractModel
from web_listening.contracts.capture import CaptureRequest, CaptureResult


class AcquisitionAttempt(StrictContractModel):
    schema_version: Literal["acquisition-attempt.v2"] = "acquisition-attempt.v2"
    attempt_id: NonEmptyString
    request: CaptureRequest
    result: CaptureResult
    accepted: bool
    acceptance_reason: str = ""

    @model_validator(mode="after")
    def validate_consistency_and_acceptance(self) -> AcquisitionAttempt:
        immutable_fields = (
            "request_id",
            "executor_id",
            "site_key",
            "site_skill_id",
            "site_skill_version",
            "site_skill_digest",
            "recipe_id",
            "run_id",
            "scope_id",
        )
        for field_name in immutable_fields:
            if getattr(self.result, field_name) != getattr(self.request, field_name):
                raise ValueError(f"result.{field_name} must match request.{field_name}")
        if self.result.started_at < self.request.requested_at:
            raise ValueError("result.started_at must not precede request.requested_at")
        if self.accepted and self.result.state != "succeeded":
            raise ValueError("accepted attempt requires a succeeded result")
        if not self.accepted and not self.acceptance_reason.strip():
            raise ValueError("rejected attempt requires acceptance_reason")
        return self


__all__ = ["AcquisitionAttempt"]
