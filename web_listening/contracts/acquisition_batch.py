from __future__ import annotations

from typing import Annotated, Literal, Protocol, Sequence

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


ACQUISITION_BATCH_RESULT_VERSION_V2 = "acquisition-batch-result.v2"
_DISPOSITIONS_V2 = ("updated", "unchanged", "blocked", "failed", "unresolved")
_BLOCKED_CLASSIFICATIONS = frozenset(
    {
        "blocked",
        "blocked_redirect",
        "http_403",
        "captcha",
        "robots_rejected",
        "robots_disallowed",
        "robots_denied",
    }
)


class AcquisitionDispositionV2(StrictContractModel):
    task_id: NonEmptyString
    site_key: NonEmptyString
    requested_url: AnyHttpUrl
    disposition: Literal["updated", "unchanged", "blocked", "failed", "unresolved"]
    reason: ReasonCode
    artifact_id: NonEmptyString | None = None

    _validate_url = field_validator("requested_url", mode="before")(
        validate_http_url_without_credentials
    )

    @model_validator(mode="after")
    def validate_artifact(self) -> AcquisitionDispositionV2:
        if (self.disposition in {"updated", "unchanged"}) != (
            self.artifact_id is not None
        ):
            raise ValueError("artifact_id is required exactly for updated/unchanged")
        return self


class AcquisitionBatchCountsV2(StrictContractModel):
    requested: int = Field(ge=0)
    updated: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    blocked: int = Field(ge=0)
    failed: int = Field(ge=0)
    unresolved: int = Field(ge=0)
    valid_snapshots: int = Field(ge=0)
    failed_evidence: int = Field(ge=0)
    succeeded: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_conservation(self) -> AcquisitionBatchCountsV2:
        if self.requested != sum(getattr(self, name) for name in _DISPOSITIONS_V2):
            raise ValueError("requested count must equal all five disposition counts")
        if (
            self.succeeded is not None
            and self.succeeded != self.updated + self.unchanged
        ):
            raise ValueError("succeeded must equal updated + unchanged")
        return self


class _BatchSummaryV2(StrictContractModel):
    checked: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)


def _status_v2(counts: AcquisitionBatchCountsV2, authoritative_status: str) -> str:
    return _batch_status(
        AcquisitionBatchCounts(
            requested=counts.requested,
            succeeded=counts.updated + counts.unchanged,
            failed=counts.blocked + counts.failed,
            unresolved=counts.unresolved,
            valid_snapshots=counts.valid_snapshots,
            failed_evidence=counts.failed_evidence,
        ),
        authoritative_status=authoritative_status,
    )


class AcquisitionBatchResultV2(StrictContractModel):
    schema_version: Literal["acquisition-batch-result.v2"] = (
        ACQUISITION_BATCH_RESULT_VERSION_V2
    )
    run_id: NonEmptyString
    authoritative_status: ReasonCode
    status: Literal["succeeded", "partial", "failed", "unresolved"]
    full_success: bool
    counts: AcquisitionBatchCountsV2
    dispositions: tuple[AcquisitionDispositionV2, ...]
    summary: _BatchSummaryV2

    @model_validator(mode="after")
    def validate_summary(self) -> AcquisitionBatchResultV2:
        if len({item.task_id for item in self.dispositions}) != len(self.dispositions):
            raise ValueError("disposition task IDs must be unique")
        if self.counts.requested != len(self.dispositions) or any(
            getattr(self.counts, name)
            != sum(item.disposition == name for item in self.dispositions)
            for name in _DISPOSITIONS_V2
        ):
            raise ValueError("batch counts do not match dispositions")
        expected = _status_v2(self.counts, self.authoritative_status)
        if self.status != expected or self.full_success != (expected == "succeeded"):
            raise ValueError("batch status does not match counts/authority")
        if self.summary != _summary_v2(self.counts):
            raise ValueError("weekly summary does not match counts")
        return self


def _summary_v2(counts: AcquisitionBatchCountsV2) -> _BatchSummaryV2:
    return _BatchSummaryV2(
        checked=counts.requested - counts.unresolved,
        succeeded=counts.updated + counts.unchanged,
        failed=counts.blocked + counts.failed,
    )


def build_acquisition_batch_result_v2(
    dispositions: Sequence[AcquisitionDispositionV2 | dict],
    *,
    valid_snapshots: int = 0,
    failed_evidence: int = 0,
    authoritative_status: str = "completed",
    run_id: str = "batch",
) -> dict:
    """Pure projection of already classified terminal evidence; never discovers URLs."""
    items = tuple(
        sorted(
            (AcquisitionDispositionV2.model_validate(item) for item in dispositions),
            key=lambda item: (item.site_key, str(item.requested_url), item.task_id),
        )
    )
    counts = AcquisitionBatchCountsV2(
        requested=len(items),
        **{
            name: sum(item.disposition == name for item in items)
            for name in _DISPOSITIONS_V2
        },
        valid_snapshots=valid_snapshots,
        failed_evidence=failed_evidence,
        succeeded=sum(item.disposition in {"updated", "unchanged"} for item in items),
    )
    status = _status_v2(counts, authoritative_status)
    return AcquisitionBatchResultV2(
        run_id=run_id,
        authoritative_status=authoritative_status,
        status=status,
        full_success=status == "succeeded",
        counts=counts,
        dispositions=items,
        summary=_summary_v2(counts),
    ).model_dump(mode="json", exclude_none=True)


def aggregate_batch_result_v2(
    results: Sequence[AcquisitionBatchResultV2 | AcquisitionBatchResult | dict]
    | AcquisitionBatchResultV2 | AcquisitionBatchResult | dict,
) -> dict:
    """Union v1/v2 results without I/O or mutation.

    Identical records collapse. Conflicting evidence for a task is rejected, never
    resolved by input order. v1 lacks change evidence and cannot infer v2 success.
    Evidence counters are counted once per distinct source run and task set.
    """
    import json

    items: dict[str, AcquisitionDispositionV2] = {}
    sources: dict[tuple, AcquisitionBatchResultV2] = {}
    if isinstance(results, (dict, AcquisitionBatchResult, AcquisitionBatchResultV2)):
        results = [results]
    for raw in results:
        if isinstance(raw, AcquisitionBatchResult) or (
            isinstance(raw, dict)
            and raw.get("schema_version") == ACQUISITION_BATCH_RESULT_VERSION
        ):
            legacy = (
                AcquisitionBatchResult.model_validate(raw)
                if isinstance(raw, AcquisitionBatchResult)
                else AcquisitionBatchResult.model_validate_json(json.dumps(raw))
            )
            raw = build_acquisition_batch_result_v2(
                [
                    {
                        "task_id": item.task_id,
                        # Scope v1 producers use the site key as task identity.
                        "site_key": item.task_id,
                        "requested_url": str(item.requested_url),
                        "disposition": (
                            "blocked"
                            if item.disposition == "failed"
                            and item.reason.removeprefix("capture.")
                            in _BLOCKED_CLASSIFICATIONS
                            else "failed" if item.disposition == "failed"
                            else "unresolved"
                        ),
                        "reason": item.reason,
                    }
                    for item in legacy.dispositions
                ],
                run_id=legacy.run_id,
                authoritative_status=legacy.authoritative_status,
                valid_snapshots=legacy.counts.valid_snapshots,
                failed_evidence=legacy.counts.failed_evidence,
            )
        result = (
            AcquisitionBatchResultV2.model_validate(raw)
            if isinstance(raw, AcquisitionBatchResultV2)
            else AcquisitionBatchResultV2.model_validate_json(json.dumps(raw))
        )
        for item in result.dispositions:
            if item.task_id in items and items[item.task_id] != item:
                raise ValueError(f"conflicting task_id data: {item.task_id}")
            items[item.task_id] = item
        key = (
            result.run_id,
            tuple(sorted(item.task_id for item in result.dispositions)),
        )
        if key in sources and (
            sources[key].counts.model_dump(exclude={"succeeded"})
            != result.counts.model_dump(exclude={"succeeded"})
            or sources[key].authoritative_status != result.authoritative_status
        ):
            raise ValueError("conflicting evidence counts for source run")
        sources[key] = result
    authority = (
        "completed"
        if all(
            result.authoritative_status in {"completed", "succeeded"}
            for result in sources.values()
        )
        else "partial"
    )
    return build_acquisition_batch_result_v2(
        tuple(items.values()),
        run_id="aggregate",
        authoritative_status=authority,
        valid_snapshots=sum(
            result.counts.valid_snapshots for result in sources.values()
        ),
        failed_evidence=sum(
            result.counts.failed_evidence for result in sources.values()
        ),
    )


aggregate_batch_result = aggregate_batch_result_v2


def acquisition_batch_result_v2_from_scope_run(
    run,
    *,
    site_key: str,
    requested_url: str,
    artifact_id: str | None = None,
    classification: str | None = None,
    page_failures: int = 0,
    file_failures: int = 0,
) -> dict:
    """Project a CrawlRun's persisted counters and explicit terminal classification.

    A successful empty run is failed evidence, not unchanged or in-flight. Artifact
    identity must come from the caller's real run artifact, never a discovered URL.
    """
    seen = run.pages_seen + run.files_seen
    changes = run.pages_changed + run.files_changed
    failures = page_failures + file_failures
    if run.status in {"queued", "running", "cancelled"}:
        disposition, reason = "unresolved", f"scope.{run.status}"
    elif classification in _BLOCKED_CLASSIFICATIONS:
        disposition, reason = "blocked", f"capture.{classification}"
    elif classification or run.status not in {"completed", "succeeded"} or failures:
        disposition, reason = "failed", (
            f"capture.{classification}" if classification else "scope.run_failed"
        )
    elif changes > 0:
        disposition, reason = "updated", "scope.changed"
    elif seen > 0:
        disposition, reason = "unchanged", "scope.unchanged"
    else:
        disposition, reason = "failed", "scope.no_observations"
    return build_acquisition_batch_result_v2(
        [
            AcquisitionDispositionV2(
                task_id=site_key,
                site_key=site_key,
                requested_url=requested_url,
                disposition=disposition,
                reason=reason,
                artifact_id=(
                    artifact_id if disposition in {"updated", "unchanged"} else None
                ),
            )
        ],
        run_id=f"scope-run-{run.id}",
        authoritative_status=run.status,
        valid_snapshots=seen,
        failed_evidence=max(failures, int(disposition in {"blocked", "failed"})),
    )


__all__ += [
    "ACQUISITION_BATCH_RESULT_VERSION_V2",
    "AcquisitionDispositionV2",
    "AcquisitionBatchCountsV2",
    "AcquisitionBatchResultV2",
    "build_acquisition_batch_result_v2",
    "aggregate_batch_result_v2",
    "aggregate_batch_result",
    "acquisition_batch_result_v2_from_scope_run",
]
