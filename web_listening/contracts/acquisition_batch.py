from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from web_listening.contracts._protocol import (
    NonEmptyString,
    StrictContractModel,
    validate_http_url_without_credentials,
)


ACQUISITION_BATCH_RESULT_VERSION = "acquisition-batch-result.v1"
ReasonCode = Annotated[
    str,
    Field(min_length=1, pattern=r"^[a-z][a-z0-9_.:-]{0,127}$"),
]


class AcquisitionDisposition(StrictContractModel):
    task_id: NonEmptyString
    requested_url: AnyHttpUrl
    disposition: Literal["succeeded", "failed", "unresolved"]
    reason: ReasonCode
    artifact_id: NonEmptyString | None = None

    _validate_url = field_validator("requested_url", mode="before")(
        validate_http_url_without_credentials
    )

    @model_validator(mode="after")
    def validate_artifact_disposition(self) -> AcquisitionDisposition:
        if self.disposition != "succeeded" and self.artifact_id is not None:
            raise ValueError("only succeeded dispositions may carry an artifact")
        return self


class AcquisitionBatchCounts(StrictContractModel):
    requested: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    unresolved: int = Field(ge=0)
    valid_snapshots: int = Field(ge=0)
    failed_evidence: int = Field(ge=0)


class AcquisitionBatchResult(StrictContractModel):
    schema_version: Literal["acquisition-batch-result.v1"] = (
        ACQUISITION_BATCH_RESULT_VERSION
    )
    run_id: NonEmptyString
    authoritative_status: ReasonCode
    status: Literal["succeeded", "partial", "failed", "unresolved"]
    full_success: bool
    counts: AcquisitionBatchCounts
    dispositions: tuple[AcquisitionDisposition, ...]

    @model_validator(mode="after")
    def validate_summary(self) -> AcquisitionBatchResult:
        dispositions = self.dispositions
        if len({item.task_id for item in dispositions}) != len(dispositions):
            raise ValueError("disposition task IDs must be unique")
        measured = {
            name: sum(item.disposition == name for item in dispositions)
            for name in ("succeeded", "failed", "unresolved")
        }
        if (
            self.counts.requested != len(dispositions)
            or self.counts.succeeded != measured["succeeded"]
            or self.counts.failed != measured["failed"]
            or self.counts.unresolved != measured["unresolved"]
        ):
            raise ValueError("batch counts do not match dispositions")
        expected_status = _batch_status(
            self.counts, authoritative_status=self.authoritative_status
        )
        if self.status != expected_status:
            raise ValueError("batch status does not match counts")
        if self.full_success != (expected_status == "succeeded"):
            raise ValueError("full_success does not match batch status")
        return self


def _batch_status(counts: AcquisitionBatchCounts, *, authoritative_status: str) -> str:
    if (
        authoritative_status in {"completed", "succeeded"}
        and counts.requested > 0
        and counts.succeeded == counts.requested
    ):
        return "succeeded"
    if counts.succeeded > 0 or counts.valid_snapshots > 0:
        return "partial"
    if counts.failed > 0:
        return "failed"
    return "unresolved"


class _ScopeRunResult(Protocol):
    site_key: str
    seed_url: str
    run_id: int | None
    status: str
    pages_seen: int
    files_seen: int
    page_failures: int
    file_failures: int


_INITIAL_REJECTION_CLASSIFICATIONS = frozenset(
    {
        "blocked",
        "blocked_redirect",
        "empty_content",
        "executor_error",
        "failed_quality_gate",
        "http_403",
        "http_status_rejected",
        "integrity_error",
        "lineage_mismatch",
        "not_found",
        "protocol_error",
        "timeout",
        "unsafe_redirect",
    }
)


class _InitialAcquisitionOutcome(Protocol):
    classification: str
    attempt_records: tuple[object, ...]


def acquisition_batch_result_from_initial_rejection(
    *,
    site_key: str,
    requested_url: str,
    outcome: _InitialAcquisitionOutcome,
) -> dict[str, object]:
    """Project a governed initial-seed rejection without exposing executor detail."""
    classification = str(outcome.classification).strip().lower()
    if classification not in _INITIAL_REJECTION_CLASSIFICATIONS:
        classification = "acquisition_failed"
    item = AcquisitionDisposition(
        task_id=site_key,
        requested_url=requested_url,
        disposition="failed",
        reason=f"capture.{classification}",
        artifact_id=None,
    )
    attempts = tuple(outcome.attempt_records or ())
    counts = AcquisitionBatchCounts(
        requested=1,
        succeeded=0,
        failed=1,
        unresolved=0,
        valid_snapshots=0,
        failed_evidence=max(1, len(attempts)),
    )
    projected = AcquisitionBatchResult(
        run_id=f"scope-admission-{site_key}",
        authoritative_status="failed",
        status="failed",
        full_success=False,
        counts=counts,
        dispositions=(item,),
    )
    return projected.model_dump(mode="json")


def acquisition_batch_result_from_scope_run(
    result: _ScopeRunResult,
) -> dict[str, object]:
    """Project one governed scope invocation into the canonical batch contract."""
    failed_evidence = result.page_failures + result.file_failures
    if result.status in {"queued", "running", "cancelled"}:
        disposition = "unresolved"
        reason = f"scope.{result.status}"
    elif result.status != "completed":
        disposition = "failed"
        reason = "scope.run_failed"
    elif failed_evidence:
        disposition = "failed"
        reason = "scope.acquisition_failed"
    else:
        disposition = "succeeded"
        reason = "scope.completed"

    item = AcquisitionDisposition(
        task_id=result.site_key,
        requested_url=result.seed_url,
        disposition=disposition,
        reason=reason,
        artifact_id=None,
    )
    counts = AcquisitionBatchCounts(
        requested=1,
        succeeded=int(disposition == "succeeded"),
        failed=int(disposition == "failed"),
        unresolved=int(disposition == "unresolved"),
        valid_snapshots=result.pages_seen + result.files_seen,
        failed_evidence=failed_evidence,
    )
    status = _batch_status(counts, authoritative_status=result.status)
    projected = AcquisitionBatchResult(
        run_id=f"scope-run-{result.run_id}",
        authoritative_status=result.status,
        status=status,
        full_success=status == "succeeded",
        counts=counts,
        dispositions=(item,),
    )
    return projected.model_dump(mode="json")


__all__ = [
    "ACQUISITION_BATCH_RESULT_VERSION",
    "AcquisitionBatchCounts",
    "AcquisitionBatchResult",
    "AcquisitionDisposition",
    "acquisition_batch_result_from_initial_rejection",
    "acquisition_batch_result_from_scope_run",
]
