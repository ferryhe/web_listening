from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import json
import re
from typing import Any, Protocol

from web_listening.blocks.acquisition_profile import (
    AdapterId,
    AcquisitionProfile,
    AcquisitionQualityGates,
    CaptureAttempt,
    recommend_next_adapter,
)
from web_listening.blocks.crawler import (
    FetchResult,
)
from web_listening.executors.http_wrapper import HttpAcquisitionAdapter
from web_listening.executors.playwright_wrapper import BrowserAcquisitionAdapter
from web_listening.executors.cloakbrowser_wrapper import (
    CloakBrowserAcquisitionAdapter as _CloakBrowserAcquisitionAdapter,
)


@dataclass(slots=True)
class CaptureEvaluation:
    passed: bool
    failure_reason: str
    blocked_marker: str
    word_count: int
    link_count: int
    document_link_count: int
    status_code: int | None
    final_url: str


class AcquisitionAdapter(Protocol):
    adapter_id: AdapterId

    def capture(self, url: str, *, config: dict[str, Any] | None = None) -> FetchResult:
        ...


class CloakBrowserAcquisitionAdapter(_CloakBrowserAcquisitionAdapter):
    """Compatibility export retaining the historical monkeypatch seam."""

    def __init__(self):
        super().__init__(importer=lambda name: import_module(name))


def build_builtin_adapters() -> dict[AdapterId, AcquisitionAdapter]:
    return {
        "web_http": HttpAcquisitionAdapter(),
        "browser_rendered": BrowserAcquisitionAdapter(),
        "cloakbrowser": CloakBrowserAcquisitionAdapter(),
    }


def evaluate_fetch_result(
    adapter: AdapterId,
    url: str,
    result: FetchResult,
    quality_gates: AcquisitionQualityGates,
) -> CaptureAttempt:
    evaluation = _evaluate_result(result, quality_gates)
    status = "passed" if evaluation.passed else "failed_quality_gate"
    if evaluation.blocked_marker:
        status = "blocked"
    metadata = dict(result.metadata_json or {})
    metadata.update(_content_preview_metadata(result))
    return CaptureAttempt(
        adapter=adapter,
        status=status,
        url=url,
        final_url=evaluation.final_url,
        status_code=evaluation.status_code,
        word_count=evaluation.word_count,
        link_count=evaluation.link_count,
        document_link_count=evaluation.document_link_count,
        failure_reason=evaluation.failure_reason,
        metadata=metadata,
    )


def evaluate_capture_attempt(
    attempt: CaptureAttempt,
    quality_gates: AcquisitionQualityGates,
) -> CaptureAttempt:
    if attempt.status == "error":
        return attempt
    if attempt.status == "blocked" or _has_blocked_failure_reason(attempt.failure_reason):
        return attempt.model_copy(update={"status": "blocked"})

    blocked_marker = _find_blocked_marker(
        _metadata_text(attempt.metadata),
        quality_gates.blocked_markers,
    )
    failure_reasons = _quality_failure_reasons(
        status_code=attempt.status_code,
        word_count=attempt.word_count,
        link_count=attempt.link_count,
        document_link_count=attempt.document_link_count,
        blocked_marker=blocked_marker,
        quality_gates=quality_gates,
    )
    status = "passed"
    if failure_reasons:
        status = "blocked" if blocked_marker else "failed_quality_gate"
    return attempt.model_copy(
        update={
            "status": status,
            "failure_reason": "; ".join(failure_reasons),
        }
    )


def run_capture_attempt(
    url: str,
    adapter: AcquisitionAdapter,
    profile: AcquisitionProfile,
    prior_attempts: list[CaptureAttempt] | None = None,
) -> CaptureAttempt:
    attempts = list(prior_attempts or [])
    adapter_id = adapter.adapter_id
    _validate_capture_safety(adapter_id, profile)
    config = _adapter_config(profile, adapter_id)
    try:
        result = adapter.capture(url, config=config)
        attempt = evaluate_fetch_result(adapter_id, url, result, profile.quality_gates)
    except Exception as exc:
        attempt = CaptureAttempt(
            adapter=adapter_id,
            status="error",
            url=url,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
    attempts.append(attempt)
    recommended_next_adapter = recommend_next_adapter(profile, attempts)
    return attempt.model_copy(update={"recommended_next_adapter": recommended_next_adapter})


def _evaluate_result(
    result: FetchResult,
    quality_gates: AcquisitionQualityGates,
) -> CaptureEvaluation:
    text = _best_content_text(result)
    metadata = result.metadata_json or {}
    word_count = _word_count(text)
    # The current FetchResult does not carry extracted links directly, so PR2
    # uses crawler-provided metadata counts until richer evidence is integrated.
    link_count = _metadata_count(metadata, "link_count")
    document_link_count = _metadata_count(metadata, "document_link_count")
    blocked_marker = _find_blocked_marker(
        "\n".join([result.fit_markdown, result.markdown, result.content_text, _metadata_text(metadata)]),
        quality_gates.blocked_markers,
    )
    failure_reasons = _quality_failure_reasons(
        status_code=result.status_code,
        word_count=word_count,
        link_count=link_count,
        document_link_count=document_link_count,
        blocked_marker=blocked_marker,
        quality_gates=quality_gates,
    )
    return CaptureEvaluation(
        passed=not failure_reasons,
        failure_reason="; ".join(failure_reasons),
        blocked_marker=blocked_marker,
        word_count=word_count,
        link_count=link_count,
        document_link_count=document_link_count,
        status_code=result.status_code,
        final_url=result.final_url,
    )


def _quality_failure_reasons(
    *,
    status_code: int | None,
    word_count: int,
    link_count: int,
    document_link_count: int,
    blocked_marker: str,
    quality_gates: AcquisitionQualityGates,
) -> list[str]:
    reasons = []
    if blocked_marker:
        reasons.append(f"blocked marker found: {blocked_marker}")
    if quality_gates.require_status_ok and not _status_is_ok(status_code):
        reasons.append(f"status_code {status_code} is not OK")
    if word_count < quality_gates.min_words:
        reasons.append(f"word_count {word_count} < min_words {quality_gates.min_words}")
    if link_count < quality_gates.min_links:
        reasons.append(f"link_count {link_count} < min_links {quality_gates.min_links}")
    if document_link_count < quality_gates.min_document_links:
        reasons.append(
            f"document_link_count {document_link_count} "
            f"< min_document_links {quality_gates.min_document_links}"
        )
    return reasons


def _adapter_config(profile: AcquisitionProfile, adapter_id: AdapterId) -> dict[str, Any] | None:
    for adapter in profile.adapters:
        if adapter.adapter == adapter_id:
            return dict(adapter.config)
    return None


def _validate_capture_safety(adapter_id: AdapterId, profile: AcquisitionProfile) -> None:
    if adapter_id != "cloakbrowser":
        return
    if not profile.safety.permits_cloakbrowser:
        raise PermissionError(
            "CloakBrowser capture requires safety.allow_stealth_browser=true "
            "and safety.require_authorized_access=true in the active acquisition profile."
        )


def _status_is_ok(status_code: int | None) -> bool:
    return status_code is not None and 200 <= status_code < 300


def _best_content_text(result: FetchResult) -> str:
    return result.fit_markdown or result.markdown or result.content_text or ""


def _content_preview_metadata(result: FetchResult) -> dict[str, str]:
    metadata: dict[str, str] = {}
    content_text_preview = result.content_text[:2_000].strip()
    if content_text_preview:
        metadata["content_text_preview"] = content_text_preview
    markdown_preview = (result.fit_markdown or result.markdown)[:2_000].strip()
    if markdown_preview:
        metadata["markdown_preview"] = markdown_preview
    return metadata


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _metadata_count(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key, 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _find_blocked_marker(text: str, markers: list[str]) -> str:
    haystack = text.casefold()
    for marker in markers:
        normalized_marker = marker.strip()
        if normalized_marker and normalized_marker.casefold() in haystack:
            return normalized_marker.casefold()
    return ""


def _has_blocked_failure_reason(failure_reason: str) -> bool:
    return "blocked marker" in failure_reason.casefold()


def _metadata_text(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, sort_keys=True, default=str)
