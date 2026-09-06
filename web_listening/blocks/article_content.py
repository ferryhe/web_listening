"""Versioned article reads using the existing compiled acquisition authority."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from web_listening.blocks.acquisition_fallback import GOAL_PRESET_QUALITY_GATES
from web_listening.blocks.acquisition_profile import (
    AcquisitionProfile,
    AcquisitionQualityGates,
    load_acquisition_profile,
)
from web_listening.blocks.acquisition_terminal import DEFAULT_BLOCKED_MARKERS
from web_listening.blocks.acquisition_tools import validate_http_url
from web_listening.blocks.diff import extract_links, find_document_links
from web_listening.blocks.governed_read import ROLLBACK_REQUIRED_READ_ERRORS
from web_listening.blocks.normalizer import normalize_html
from web_listening.config import settings
from web_listening.contracts.tool_result import (
    ToolResult,
    ToolResultDataQuality,
    ToolResultError,
    ToolResultQualityGates,
)

POLICY_VERSION = "article_content.v1"
READERS = ("web_http", "browser_rendered", "cloakbrowser")
_RETRYABLE_EXCEPTIONS = (TimeoutError, ConnectionError)
_TERMINAL = {"not_found", "auth_required", "permission_denied", "interaction_required"}


def runtime_data_dir() -> Path:
    return Path(settings.data_dir).absolute()


def _output_path(output_dir) -> Path:
    root = runtime_data_dir()
    path = (
        Path(output_dir).absolute()
        if output_dir is not None
        else root / "article_content"
    )
    if ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("output_dir must be inside the project data root")
    return path


@contextmanager
def _output_descriptor(output_dir):
    """Use the project's O_DIRECTORY/O_NOFOLLOW descriptor traversal policy."""
    path = _output_path(output_dir)
    root = runtime_data_dir()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not current.is_relative_to(root):
                    raise ValueError(
                        "output_dir parent outside project data root"
                    ) from None
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def write_file(output_dir, name: str, body: bytes) -> str:
    """Atomically publish one body under a pinned no-follow output directory."""
    with _output_descriptor(output_dir) as directory:
        temporary = f".article-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return name


class _ContentRefError(ValueError):
    pass


def _read_evidence(output_dir, name, digest) -> bytes:
    if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
        raise _ContentRefError("content_ref_corrupt")
    try:
        with _output_descriptor(output_dir) as directory:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise _ContentRefError("content_ref_corrupt")
                body = stream.read()
    except OSError:
        raise _ContentRefError("content_ref_corrupt") from None
    if hashlib.sha256(body).hexdigest() != digest:
        raise _ContentRefError("content_ref_hash_mismatch")
    return body


def _result(
    status, reason, attempts, *, data=None, error=None, gates=None, warnings=()
):
    has_data = status == "present"
    return ToolResult(
        ok=error is None,
        has_data=has_data,
        data_status=status,
        data_count=1 if has_data else 0,
        tool=(data or {}).get("selected_method") or "fetch_article_content",
        stop_reason=reason,
        error=error,
        attempts=attempts,
        data={**(data or {}), "attempts": attempts},
        warnings=list(warnings),
        quality_gates=gates or ToolResultQualityGates(),
        meta={
            "contract_version": "web-listening-tool-result.v1",
            "policy_version": POLICY_VERSION,
        },
    )


def _error(code, attempts, *, data=None, gates=None):
    return _result(
        "error",
        code,
        attempts,
        data=data,
        gates=gates,
        error=ToolResultError(code=code, message=code, retryable=False),
    )


def _package(body, *, output_dir, site_key, limit, data, attempts, gates):
    encoded = body.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    truncated = len(encoded) > limit
    data = {**data, "sha256": digest, "truncated": truncated, "data_status": "present"}
    if truncated:
        data["truncated_preview"] = encoded[:limit].decode("utf-8", errors="ignore")
    else:
        data["full_text"] = body
    suffix = ".txt" if data["content_type"].startswith("text/") else ".html"
    name = f"{site_key}_{digest[:16]}{suffix}"
    warnings = []
    try:
        data["content_ref"] = write_file(output_dir, name, encoded)
    except OSError:
        if truncated:
            return _error("artifact_write_failed", attempts, gates=gates)
        warnings.append("artifact_write_failed")
    else:
        try:
            _read_evidence(output_dir, name, digest)
        except _ContentRefError as exc:
            return _error(str(exc), attempts, gates=gates)
    attempts[-1].update(
        {
            key: data[key]
            for key in (
                "requested_url",
                "final_url",
                "selected_method",
                "sha256",
                "content_type",
                "extraction_metadata",
                "content_ref",
            )
            if key in data
        }
    )
    result = _result(
        "present",
        "usable_data_found",
        attempts,
        data=data,
        gates=gates,
        warnings=warnings,
    )
    meta = data["extraction_metadata"]
    result.data_quality = ToolResultDataQuality(
        passed=True,
        status_code=meta["status_code"],
        word_count=meta["word_count"],
        link_count=meta["link_count"],
        document_link_count=meta["document_link_count"],
    )
    return result


def _classify(page, gates):
    body = page.raw_html or page.content_text
    normalized = normalize_html(body, base_url=page.final_url) if "<" in body else None
    text = normalized.content_text if normalized is not None else body
    metadata = dict(
        normalized.metadata if normalized is not None else page.metadata_json
    )
    if normalized is not None:
        from bs4 import BeautifulSoup

        title = BeautifulSoup(body, "lxml").title
        metadata["title"] = title.get_text(" ", strip=True) if title is not None else ""
        links = extract_links(body, page.final_url)
        metadata["link_count"] = len(links)
        metadata["document_link_count"] = len(find_document_links(links))
    markers = [
        marker
        for marker in dict.fromkeys((*DEFAULT_BLOCKED_MARKERS, *gates.blocked_markers))
        if marker.strip() and marker.casefold() in text.casefold()
    ]
    code = page.status_code
    words = len(text.split())
    meta = {
        "page_title": metadata.get("title", ""),
        "status_code": code,
        "word_count": words,
        "link_count": int(metadata.get("link_count", 0)),
        "document_link_count": int(metadata.get("document_link_count", 0)),
        "blocked_marker_hits": markers,
        "fetch_mode": page.metadata_json.get("fetch_mode", ""),
        "driver": page.metadata_json.get("runtime_id", ""),
        "access_decision_id": page.metadata_json.get("access_decision_id"),
    }
    if code in {404, 410}:
        status = "not_found"
    elif code == 401 or any(
        marker in text.casefold()
        for marker in (
            "sign in to continue",
            "log in to continue",
            "subscribe to continue",
            "paywall",
        )
    ):
        status = "auth_required"
    elif any(
        marker in text.casefold()
        for marker in ("interaction required", "click to continue")
    ):
        status = "interaction_required"
    elif code == 403 and not markers:
        status = "permission_denied"
    elif markers:
        status = "blocked"
    elif code is None or not 200 <= code < 300:
        status = "error"
    elif (
        not text.strip()
        or "\ufffd" in body
        or words < gates.min_words
        or meta["link_count"] < gates.min_links
        or meta["document_link_count"] < gates.min_document_links
    ):
        status = "failed_quality_gate"
    else:
        status = "present"
    return status, body, meta


def _fetch_with_readers(
    url,
    *,
    profile,
    readers,
    output_dir=None,
    site_key=None,
    inline_content_limit=2000,
    quality_gates=None,
    prior_attempts=None,
):
    """Internal injection seam: production readers are compiled executors only."""
    output = _output_path(output_dir)
    with _output_descriptor(output):
        pass
    key = site_key or profile.site_key
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("site_key must be a safe filename component")
    if type(inline_content_limit) is not int or inline_content_limit < 0:
        raise ValueError("inline_content_limit must be a non-negative integer")
    quality = quality_gates or profile.quality_gates
    if not isinstance(quality, AcquisitionQualityGates):
        quality = AcquisitionQualityGates.model_validate(quality)
    gates = ToolResultQualityGates(
        requested=quality.model_dump(), effective=quality.model_dump()
    )
    attempts = []
    base = {"requested_url": url, "final_url": None, "selected_method": None}
    for tool in READERS:
        adapter = readers.get(tool)
        attempt = {
            "tool": tool,
            "data_status": "not_applicable",
            "stop_reason": "",
            "skipped": False,
            "reason": "",
            "retryable": False,
        }
        attempts.append(attempt)
        disabled = any(
            item.adapter == tool and not item.enabled for item in profile.adapters
        )
        if disabled:
            reason = (
                "cloakbrowser_unavailable"
                if tool == "cloakbrowser"
                else "reader_disabled"
            )
            attempt.update(skipped=True, reason=reason, stop_reason=reason)
            continue
        if adapter is None or (
            tool == "cloakbrowser" and not profile.safety.permits_cloakbrowser
        ):
            reason = (
                "cloakbrowser_unavailable"
                if tool == "cloakbrowser"
                else "reader_unavailable"
            )
            attempt.update(skipped=True, reason=reason, stop_reason=reason)
            continue
        reused = None
        for evidence in prior_attempts or ():
            if (
                evidence.get("data_status") in {"present", "artifact_only"}
                and evidence.get("requested_url") == url
                and evidence.get("selected_method", evidence.get("tool")) == tool
                and evidence.get("sha256")
                and evidence.get("content_ref")
            ):
                try:
                    body = _read_evidence(
                        output, evidence.get("content_ref"), evidence.get("sha256")
                    ).decode("utf-8")
                except _ContentRefError as exc:
                    return _error(str(exc), attempts, gates=gates)
                from web_listening.blocks.crawler import FetchResult

                reused = FetchResult(
                    raw_html=body,
                    cleaned_html="",
                    content_text=body,
                    markdown="",
                    fit_markdown="",
                    final_url=evidence["final_url"],
                    status_code=evidence.get("extraction_metadata", {}).get(
                        "status_code"
                    ),
                    metadata_json={
                        **evidence.get("extraction_metadata", {}),
                        "content_type": evidence.get("content_type", "text/html"),
                    },
                )
                attempt.update(skipped=True, reason="in_process_evidence_reused")
                break
        try:
            page = reused if reused is not None else adapter.capture(url)
        except ROLLBACK_REQUIRED_READ_ERRORS:
            raise
        except _ReaderFailure as exc:
            code = exc.code
            status = {
                401: "auth_required",
                404: "not_found",
                410: "not_found",
                403: "permission_denied",
            }.get(exc.status_code)
            if status is None:
                if code in {"interaction_required", "requires_interaction"}:
                    status = "interaction_required"
                elif code in {"auth_required", "login_required", "paywall"}:
                    status = "auth_required"
                elif code == "permission_denied":
                    status = "permission_denied"
                elif code in {
                    "missing_runtime",
                    "browser_authority_required",
                    "unsupported_content_kind",
                }:
                    status = "not_applicable"
                elif code in {"blocked", "blocked_redirect"}:
                    status = "blocked"
                elif code == "empty_content":
                    status = "failed_quality_gate"
                else:
                    status = "error"
            attempt.update(
                data_status=status,
                reason=code,
                stop_reason=code,
                skipped=status == "not_applicable",
                retryable=exc.retryable,
            )
            if status in _TERMINAL:
                return _result(status, status, attempts, data=base, gates=gates)
            if code in {
                "capture_identity_mismatch",
                "capture_hash_mismatch",
                "unsafe_redirect",
            }:
                return _error(code, attempts, gates=gates)
            continue
        except Exception as exc:  # noqa: BLE001 - AC-6 reader isolation.
            attempt.update(
                data_status="error",
                reason="reader_runtime_error",
                stop_reason="reader_runtime_error",
                retryable=isinstance(exc, _RETRYABLE_EXCEPTIONS),
            )
            continue
        status, body, metadata = _classify(page, quality)
        attempt.update(
            data_status=status,
            reason=attempt["reason"] or status,
            stop_reason=status,
            retryable=status == "error"
            and (page.status_code is None or page.status_code >= 500),
        )
        if status in _TERMINAL:
            return _result(status, status, attempts, data=base, gates=gates)
        if status != "present":
            continue
        metadata["fetch_mode"] = tool
        metadata["driver"] = metadata["driver"] or (
            "governed_http" if tool == "web_http" else tool
        )
        data = {
            "requested_url": url,
            "final_url": page.final_url or url,
            "selected_method": tool,
            "content_type": page.metadata_json.get("content_type", "text/html"),
            "extraction_metadata": metadata,
        }
        return _package(
            body,
            output_dir=output,
            site_key=key,
            limit=inline_content_limit,
            data=data,
            attempts=attempts,
            gates=gates,
        )
    if any(item["data_status"] == "error" for item in attempts):
        return _error("readers_failed", attempts, data=base, gates=gates)
    return _result("no_content", "no_usable_content", attempts, data=base, gates=gates)


class _ReaderFailure(RuntimeError):
    def __init__(self, code, status_code=None, retryable=False):
        self.code, self.status_code, self.retryable = code, status_code, retryable
        super().__init__(code)


class _CompiledReader:
    """Adapt one exact compiled step; never build an independent target reader."""

    def __init__(self, gateway, step):
        self.gateway, self.step = gateway, step

    def capture(self, url):
        from web_listening.blocks.crawler import FetchResult
        from web_listening.contracts import CaptureResult

        request = self.gateway._request(
            url, "article-content", "article-content", self.step, "page"
        )
        result = self.gateway.registry.execute(request)
        lineage = (
            "request_id",
            "site_key",
            "site_skill_id",
            "site_skill_version",
            "site_skill_digest",
            "recipe_id",
            "run_id",
            "scope_id",
            "executor_id",
        )
        if not isinstance(result, CaptureResult) or any(
            getattr(result, key) != getattr(request, key) for key in lineage
        ):
            raise _ReaderFailure("capture_identity_mismatch")
        if result.state != "succeeded":
            raise _ReaderFailure(
                result.error.code, result.status_code, result.error.retryable
            )
        final_url = str(result.final_url or request.url)
        if self.gateway._origin(final_url) != self.gateway._origin(url):
            raise _ReaderFailure("unsafe_redirect")
        body = result.content.text
        if body is None:
            raise _ReaderFailure("empty_content")
        if result.content.sha256 != hashlib.sha256(body.encode("utf-8")).hexdigest():
            raise _ReaderFailure("capture_hash_mismatch")
        return FetchResult(
            raw_html=body,
            cleaned_html="",
            content_text=body,
            markdown="",
            fit_markdown="",
            final_url=final_url,
            status_code=result.status_code,
            metadata_json={
                **dict(result.metadata),
                "content_type": result.content.media_type,
            },
        )


def _readers_from_gateway(gateway):
    readers = {}
    for step in gateway.plan.steps:
        tool = step["executor_id"]
        if tool in READERS and tool not in readers:
            readers[tool] = _CompiledReader(gateway, step)
    return readers


def fetch_article_content(
    url,
    *,
    profile=None,
    profile_path=None,
    site_key=None,
    goal_preset="page_text",
    quality_gates=None,
    safety=None,
    allowed_domains=None,
    inline_content_limit=2000,
    output_dir=None,
    scope_path=None,
    prior_attempts=None,
) -> ToolResult:
    """Read one reviewed scope seed using article_content.v1.

    ``scope_path`` supplies the existing MonitorScopePlan authority; a profile
    alone cannot replace its reviewed Site Skill bindings. ``prior_attempts``
    accepts verified result data retained by the caller during this invocation.
    """
    from web_listening.blocks import staged_workflow
    from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan
    from web_listening.contracts.access_decision import canonicalize_access_url
    from web_listening.mcp.tools import _has_inline_safety_override

    canonicalize_access_url(url)
    url = validate_http_url(url)
    if type(inline_content_limit) is not int or inline_content_limit < 0:
        raise ValueError("inline_content_limit must be a non-negative integer")
    _output_path(output_dir)
    if goal_preset not in GOAL_PRESET_QUALITY_GATES:
        raise ValueError("unknown goal_preset")
    if profile is not None and profile_path is not None:
        raise ValueError("profile and profile_path are mutually exclusive")
    if profile_path and _has_inline_safety_override(
        safety=safety, allowed_domains=allowed_domains
    ):
        raise ValueError("profile_path forbids inline safety overrides")
    if profile is None and profile_path is None:
        return _result(
            "permission_denied", "no_reviewed_profile", [], data={"requested_url": url}
        )
    resolved = (
        load_acquisition_profile(profile_path, strict=True)
        if profile_path
        else AcquisitionProfile.model_validate(
            (
                profile.model_dump()
                if isinstance(profile, AcquisitionProfile)
                else profile
            ),
            strict=True,
        )
    )
    if site_key is not None and site_key != resolved.site_key:
        raise ValueError("site_key must match reviewed profile")
    if safety or allowed_domains is not None:
        override = dict(safety or {})
        if allowed_domains is not None:
            override["allowed_domains"] = allowed_domains
        from web_listening.blocks.acquisition_profile import AcquisitionSafetyPolicy

        candidate = AcquisitionSafetyPolicy.model_validate(
            {**resolved.safety.model_dump(), **override}
        )
        if (
            not set(candidate.allowed_domains) <= set(resolved.safety.allowed_domains)
            or candidate.permits_cloakbrowser
            and not resolved.safety.permits_cloakbrowser
        ):
            raise ValueError("inline safety cannot enlarge reviewed authority")
        resolved = resolved.model_copy(update={"safety": candidate})
    if scope_path is None:
        return _result(
            "permission_denied", "no_reviewed_scope", [], data={"requested_url": url}
        )
    scope = load_monitor_scope_plan(Path(scope_path))
    if scope.seed_url != url or scope.selection_review_status != "approved":
        return _result(
            "permission_denied",
            "url_outside_reviewed_scope",
            [],
            data={"requested_url": url},
        )
    requested = dict(quality_gates or {})
    if goal_preset != "page_text":
        requested = {**GOAL_PRESET_QUALITY_GATES[goal_preset], **requested}
    effective = {**resolved.quality_gates.model_dump(), **requested}
    for key in ("min_words", "min_links", "min_document_links"):
        effective[key] = max(effective[key], getattr(resolved.quality_gates, key))
    effective["blocked_markers"] = list(
        dict.fromkeys(
            [*resolved.quality_gates.blocked_markers, *effective["blocked_markers"]]
        )
    )
    gates = AcquisitionQualityGates.model_validate(effective)
    # Reuse the staged compiler, including its profile/skill/origin/budget checks.
    # CloakBrowser has no admitted target executor before #68. Removing an
    # unavailable optional step narrows the reviewed plan; it grants no reader.
    wired = {"web_http", "browser_rendered"}
    enabled = {item.adapter for item in resolved.adapters if item.enabled}
    if resolved.default_adapter not in wired:
        return _result(
            "permission_denied",
            "default_reader_unavailable",
            [],
            data={"requested_url": url},
        )
    compiled_profile = resolved.model_copy(
        update={
            "fallback_order": [
                tool
                for tool in resolved.fallback_order
                if tool in wired and tool in enabled
            ],
        }
    )
    gateway = staged_workflow._compile_acquisition_gateway(
        scope, acquisition_profile=compiled_profile
    )
    try:
        result = _fetch_with_readers(
            url,
            profile=resolved,
            readers=_readers_from_gateway(gateway),
            site_key=site_key,
            quality_gates=gates,
            output_dir=output_dir,
            inline_content_limit=inline_content_limit,
            prior_attempts=prior_attempts,
        )
        result.quality_gates.requested = requested
        return result
    finally:
        gateway.close()


__all__ = ["POLICY_VERSION", "fetch_article_content"]
