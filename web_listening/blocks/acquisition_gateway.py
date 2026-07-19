"""Acquisition boundary used by the parent-owned tree crawler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web_listening.blocks.crawler import Crawler, FetchResult
from web_listening.blocks.diff import extract_links, find_document_links
from web_listening.contracts import (
    AcquisitionAttempt as ContractAcquisitionAttempt,
    CaptureContent,
    CaptureError,
    CaptureRequest,
    CaptureResult,
)
from web_listening.executors.registry import ExecutorRegistry
from web_listening.models import AcquisitionAttempt

if TYPE_CHECKING:
    from web_listening.blocks.acquisition_execution_plan import AcquisitionExecutionPlan


@dataclass(frozen=True, slots=True)
class AcquisitionOutcome:
    request: CaptureRequest | None
    result: CaptureResult | None
    page: FetchResult | None
    classification: str
    attempts: tuple[str, ...]
    coverage_complete: bool
    attempt_records: tuple[AcquisitionAttempt, ...] = ()
    attempt_inline_artifacts: tuple[tuple[Mapping[str, Any], ...], ...] = ()

    @property
    def accepted(self) -> bool:
        return self.classification == "accepted" and self.page is not None

    @property
    def accepted_attempt(self) -> AcquisitionAttempt | None:
        matches = [item for item in self.attempt_records if item.accepted]
        return matches[0] if len(matches) == 1 else None


class AcquisitionGateway(Protocol):
    def acquire(
        self, url: str, *, run_id: str, scope_id: str, content_kind: str = "page"
    ) -> AcquisitionOutcome: ...

    def close(self) -> None: ...


class LegacyCrawlerGateway:
    """Runtime fallback retaining the full authority of the legacy crawler choice."""

    def __init__(self, crawler: Crawler, *, fetch_mode: str, fetch_config_json: dict):
        self.crawler = crawler
        self.fetch_mode = fetch_mode
        self.fetch_config_json = dict(fetch_config_json or {})

    def acquire(
        self, url: str, *, run_id: str, scope_id: str, content_kind: str = "page"
    ) -> AcquisitionOutcome:
        requested_at = datetime.now(timezone.utc)
        executor_id = _legacy_executor_id(self.fetch_mode)
        identity = json.dumps({"run_id": run_id, "scope_id": scope_id, "url": url,
                               "content_kind": content_kind, "mode": "legacy_runtime",
                               "fetch_mode": self.fetch_mode,
                               "fetch_config_json": self.fetch_config_json},
                              sort_keys=True, separators=(",", ":"))
        request_id = hashlib.sha256(identity.encode()).hexdigest()
        sentinel_digest = "0" * 64
        request = _execution_request(
            request_id=request_id, site_key="legacy-runtime",
            site_skill_id="absent", site_skill_version="0.0.0",
            site_skill_digest=sentinel_digest, recipe_id="legacy-runtime",
            run_id=run_id, scope_id=scope_id, executor_id=executor_id, url=url,
            requested_at=requested_at, config=self.fetch_config_json,
            metadata={"authority_mode": "legacy_runtime", "content_kind": content_kind,
                      "fallback_position": 0, "profile_id": None,
                      "legacy_fetch_mode": self.fetch_mode,
                      "legacy_executor_label": f"legacy_{self.fetch_mode}",
                      "site_skill_lineage": "absent",
                      "executor_version": "legacy-runtime"},
        )
        started_at = datetime.now(timezone.utc)
        page = None
        error = ""
        final_url = None
        status_code = None
        try:
            page = self.crawler.fetch_page(
                url, fetch_mode=self.fetch_mode, fetch_config_json=self.fetch_config_json
            )
            final_url, status_code = page.final_url, page.status_code
        except Exception:
            # Runtime exception text is not governed authority and may contain
            # credentials or local diagnostics. Persist only the stable class.
            error = "executor_error"
        finished_at = datetime.now(timezone.utc)
        classification = "executor_error" if page is None else ("not_found" if page.status_code in {404, 410} else "accepted")
        lineage = {field: getattr(request, field) for field in (
            "request_id", "site_key", "site_skill_id", "site_skill_version",
            "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id",
        )}
        result = CaptureResult(
            **lineage, state="failed" if page is None else "succeeded",
            started_at=started_at, finished_at=finished_at, final_url=final_url,
            status_code=status_code,
            content=(CaptureContent(media_type="text/html", text=page.raw_html,
                                    metadata={"content_kind": content_kind})
                     if page is not None else None),
            error=(CaptureError(code="executor_error", message=error)
                   if page is None else None),
            metadata={"authority_mode": "legacy_runtime", "content_kind": content_kind,
                      "legacy_fetch_mode": self.fetch_mode,
                      "legacy_executor_label": f"legacy_{self.fetch_mode}",
                      "acquisition_classification": classification,
                      "acquisition_validation": {"status_code": status_code}},
        )
        attempt = AcquisitionAttempt(
            attempt_id=request_id, request_id=request_id, scope_id=_numeric_id(scope_id), run_id=_numeric_id(run_id),
            position=0, content_kind=content_kind, site_skill_id="absent",
            site_skill_version="0.0.0", site_skill_package_sha256=sentinel_digest,
            recipe_id="legacy-runtime", executor_id=executor_id,
            executor_version="legacy-runtime", requested_url=url, final_url=final_url,
            requested_at=requested_at, started_at=started_at, finished_at=finished_at,
            classification=classification, accepted=classification == "accepted", reason=classification,
            validation={"status_code": status_code}, authority_mode="legacy_runtime",
        )
        contract = ContractAcquisitionAttempt.model_validate_json(json.dumps({
            "attempt_id": request_id,
            "request": _canonical_request(request).model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "accepted": classification == "accepted", "acceptance_reason": classification,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        attempt.canonical_json = json.dumps(
            redact_persisted_value(contract.model_dump(mode="json")),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        if page is None:
            return AcquisitionOutcome(request, result, None, classification, (classification,), False, (attempt,))
        return AcquisitionOutcome(request, result, page, classification, (classification,), True, (attempt,))

    def close(self) -> None:
        return None


class GovernedAcquisitionGateway:
    """Executes only immutable plan steps and normalizes their capture results."""

    def __init__(self, plan: "AcquisitionExecutionPlan", registry: ExecutorRegistry):
        if plan.mode != "governed" or not plan.steps:
            raise ValueError("governed gateway requires a governed execution plan")
        metadata = getattr(registry, "metadata", None)
        if metadata is not None:
            for step in plan.steps:
                runtime = metadata.get(step["executor_id"])
                if runtime is None or runtime.version != step["executor_version"]:
                    raise ValueError("governed executor runtime does not match the frozen plan")
        self.plan, self.registry = plan, registry
        self._closed = False

    def acquire(
        self, url: str, *, run_id: str, scope_id: str, content_kind: str = "page"
    ) -> AcquisitionOutcome:
        attempts: list[AcquisitionAttempt] = []
        attempt_artifacts: list[tuple[Mapping[str, Any], ...]] = []
        last_request: CaptureRequest | None = None
        last_result: CaptureResult | None = None
        for step in self.plan.steps:
            request = self._request(url, run_id, scope_id, step, content_kind)
            last_request = request
            try:
                result = self.registry.execute(request)
            except Exception as exc:
                result = self._failed_result(
                    request, "executor_exception", _redact_diagnostic_text(str(exc)))
                last_result = result
                attempts.append(self._attempt(request, step, content_kind, "executor_error", result,
                                              reason="executor_error"))
                attempt_artifacts.append(())
                continue
            last_result = result
            classification, terminal, page, validation = self._classify(request, result, content_kind)
            attempts.append(self._attempt(request, step, content_kind, classification, result,
                                          validation=validation))
            inline = result.metadata.get("inline_artifacts", ()) if isinstance(result, CaptureResult) else ()
            attempt_artifacts.append(tuple(inline) if isinstance(inline, (list, tuple)) else ())
            if classification == "accepted":
                return AcquisitionOutcome(request, result, page, classification,
                                          tuple(item.classification for item in attempts), True, tuple(attempts),
                                          tuple(attempt_artifacts))
            if terminal:
                return AcquisitionOutcome(
                    request, result, None, classification,
                    tuple(item.classification for item in attempts), classification == "not_found", tuple(attempts),
                    tuple(attempt_artifacts),
                )
        classification = attempts[-1].classification if attempts else "executor_error"
        return AcquisitionOutcome(last_request, last_result, None, classification,
                                  tuple(item.classification for item in attempts), False, tuple(attempts),
                                  tuple(attempt_artifacts))

    def _attempt(self, request, step, content_kind, classification, result, *, reason="",
                 validation=None):
        lineage = ("request_id", "site_key", "site_skill_id", "site_skill_version",
                   "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id")
        if (not isinstance(result, CaptureResult)
                or any(getattr(request, field) != getattr(result, field) for field in lineage)):
            result = self._failed_result(
                request, classification, classification,
                metadata={"protocol_classification": classification},
            )
        validation = validation or {
            "decision": classification, "status_code": getattr(result, "status_code", None)}
        attempt = AcquisitionAttempt(
            attempt_id=request.request_id, request_id=request.request_id,
            scope_id=_numeric_id(request.scope_id), run_id=_numeric_id(request.run_id), position=int(step["position"]),
            content_kind=content_kind, profile_id=getattr(self.plan, "profile_id", None),
            site_skill_id=request.site_skill_id, site_skill_version=request.site_skill_version,
            site_skill_package_sha256=request.site_skill_digest, recipe_id=request.recipe_id,
            script_sha256=step.get("script_sha256"), executor_id=request.executor_id,
            executor_version=step["executor_version"], requested_url=str(request.url),
            final_url=(str(result.final_url) if getattr(result, "final_url", None) is not None else None),
            requested_at=request.requested_at, started_at=getattr(result, "started_at", None),
            finished_at=getattr(result, "finished_at", datetime.now(timezone.utc)),
            acquisition_fingerprint=self.plan.acquisition_fingerprint,
            classification=classification, accepted=classification == "accepted",
            reason=reason or classification, validation=validation,
        )
        canonical_result = result.model_copy(update={"metadata": {
            **dict(result.metadata),
            "acquisition_classification": classification,
            "acquisition_validation": validation,
        }})
        canonical_result_payload = canonical_result.model_dump(mode="json")
        canonical_result_payload = _redact_marker_values(
            canonical_result_payload, self.plan.quality_gates.get("blocked_markers", ()))
        contract = ContractAcquisitionAttempt.model_validate_json(json.dumps({
            "attempt_id": request.request_id,
            "request": _canonical_request(request).model_dump(mode="json"),
            "result": canonical_result_payload,
            "accepted": classification == "accepted",
            "acceptance_reason": reason or classification,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        attempt.canonical_json = json.dumps(
            redact_persisted_value(contract.model_dump(mode="json")),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return attempt

    @staticmethod
    def _failed_result(request: CaptureRequest, code: str, message: str,
                       *, metadata: Mapping[str, Any] | None = None) -> CaptureResult:
        now = max(datetime.now(timezone.utc), request.requested_at)
        lineage = {field: getattr(request, field) for field in (
            "request_id", "site_key", "site_skill_id", "site_skill_version",
            "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id",
        )}
        return CaptureResult(
            **lineage, state="failed", started_at=now, finished_at=now,
            error=CaptureError(code=code, message=message), metadata=dict(metadata or {}),
        )

    def _request(self, url: str, run_id: str, scope_id: str, step, content_kind: str) -> CaptureRequest:
        identity = json.dumps({
            "plan": self.plan.acquisition_fingerprint, "run_id": run_id, "scope_id": scope_id,
            "url": url, "content_kind": content_kind, "position": step["position"],
            "executor_id": step["executor_id"],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return _execution_request(
            request_id=hashlib.sha256(identity.encode()).hexdigest(),
            site_key=self.plan.site_key, site_skill_id=self.plan.site_skill_id or "",
            site_skill_version=self.plan.site_skill_version or "",
            site_skill_digest=self.plan.site_skill_package_sha256 or "",
            recipe_id=step["recipe_id"], run_id=run_id, scope_id=scope_id,
            executor_id=step["executor_id"], url=url, requested_at=datetime.now(timezone.utc),
            config=dict(step.get("config", {})),
            metadata=canonicalize_persisted_value({
                "acquisition_fingerprint": self.plan.acquisition_fingerprint,
                "scope_fingerprint": self.plan.scope_fingerprint,
                "profile_id": self.plan.profile_id,
                "authority_mode": "governed",
                "content_kind": content_kind,
                "fallback_position": step["position"],
                "executor_version": step["executor_version"],
                "entrypoint": step.get("entrypoint"),
                "script_sha256": step["script_sha256"],
                "required_capabilities": list(step.get("required_capabilities", ())),
                "executor_capabilities": list(step.get("executor_capabilities", ())),
                "requires_authorized_access": bool(step.get("requires_authorized_access", False)),
                "verification_rules": list(step.get("verification_rules", ())),
                "resource_limits": dict(step.get("limits", {})),
                "quality_gates": dict(self.plan.quality_gates),
                "scope_budgets": dict(self.plan.scope_budgets),
            }),
        )


    def _classify(
        self, request: CaptureRequest, result: CaptureResult, content_kind: str
    ) -> tuple[str, bool, FetchResult | None, dict[str, Any]]:
        def decision(name, terminal=False, page=None, **extra):
            return name, terminal, page, {
                "decision": name, "status_code": getattr(result, "status_code", None), **extra}
        if not isinstance(result, CaptureResult):
            return decision("protocol_error", True)
        lineage = ("request_id", "site_key", "site_skill_id", "site_skill_version",
                   "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id")
        if any(getattr(request, field) != getattr(result, field) for field in lineage):
            return decision("lineage_mismatch", True)
        if result.state != "succeeded":
            code = (result.error.code if result.error else "executor_error").lower()
            if any(
                marker in code for marker in ("protocol", "identity", "lineage", "integrity", "runtime_mismatch")
            ):
                return decision("integrity_error", True)
            if "timeout" in code:
                return decision("timeout")
            if "block" in code:
                return decision("blocked")
            return decision("executor_error")
        if self._origin(str(result.final_url or request.url)) != self._origin(str(request.url)):
            return decision("unsafe_redirect", True)
        if result.status_code in {404, 410}:
            return decision("not_found", True)
        if result.status_code is None or not 200 <= result.status_code < 300 or result.content is None:
            return decision("failed_quality_gate", failed_rules=["successful_content"])
        if content_kind == "document":
            metadata = result.content.metadata
            if (
                result.content.text is None
                or result.content.sha256 is None
                or metadata.get("representation") != "base64"
                or metadata.get("sha256_scope") != "decoded-bytes"
            ):
                return decision("failed_quality_gate", failed_rules=["document_representation"])
            try:
                payload = base64.b64decode(result.content.text, validate=True)
            except (binascii.Error, ValueError):
                return decision("integrity_error", True, failed_rules=["document_base64"])
            if hashlib.sha256(payload).hexdigest() != result.content.sha256:
                return decision("integrity_error", True, failed_rules=["document_sha256"])
            return decision("accepted", page=FetchResult(
                raw_html="", cleaned_html="", content_text="", markdown="",
                fit_markdown="", metadata_json={},
                final_url=str(result.final_url or request.url), status_code=result.status_code,
            ))
        page = self._page(result)
        links = extract_links(page.raw_html, page.final_url or str(request.url))
        document_links = find_document_links(links)
        gates = self.plan.quality_gates
        measurements = {"word_count": len(page.content_text.split()), "link_count": len(links),
                        "document_link_count": len(document_links)}
        failed_rules = [name for name, measured, minimum in (
            ("min_words", measurements["word_count"], gates.get("min_words", 0)),
            ("min_links", measurements["link_count"], gates.get("min_links", 0)),
            ("min_document_links", measurements["document_link_count"],
             gates.get("min_document_links", 0)),
        ) if measured < minimum]
        lowered = page.content_text.casefold()
        markers = tuple(gates.get("blocked_markers", ()))
        marker_matched = any(str(marker).casefold() in lowered for marker in markers)
        evidence = {"measurements": measurements, "failed_rules": failed_rules,
                    "blocked_marker": {"matched": marker_matched,
                                       "configured_count": len(markers)}}
        if failed_rules:
            return decision("failed_quality_gate", **evidence)
        if marker_matched:
            return decision("blocked", **evidence)
        return decision("accepted", page=page, **evidence)

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port

    @staticmethod
    def _page(result: CaptureResult) -> FetchResult:
        text = result.content.text if result.content else ""
        from web_listening.blocks.normalizer import normalize_html

        page = normalize_html(text or "", base_url=str(result.final_url or ""))
        untrusted = _redact_json(dict(result.metadata))
        untrusted.pop("inline_artifacts", None)
        for field in ("raw_html", "cleaned_html", "content_text", "markdown", "fit_markdown"):
            untrusted.pop(field, None)
        return FetchResult(
            raw_html=page.raw_html, cleaned_html=page.cleaned_html,
            content_text=page.content_text, markdown=page.markdown,
            fit_markdown=page.fit_markdown,
            metadata_json={**page.metadata, **untrusted},
            final_url=str(result.final_url or ""), status_code=result.status_code,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        for executor in getattr(self.registry, "executors", {}).values():
            close = getattr(executor, "close", None)
            if close is not None:
                try:
                    close()
                except BaseException as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]


def legacy_document_runtime_attempt(
    *, url: str, run_id: int, scope_id: int, fetch_mode: str,
    started_at: datetime, finished_at: datetime, classification: str,
    error_message: str = "",
) -> AcquisitionAttempt:
    """Build truthful typed lineage for the live legacy document downloader."""
    identity = json.dumps({"run_id": run_id, "scope_id": scope_id, "url": url,
                           "content_kind": "document", "mode": "legacy_runtime_document"},
                          sort_keys=True, separators=(",", ":"))
    request_id = hashlib.sha256(identity.encode()).hexdigest()
    sentinel_digest = "0" * 64
    executor_id = _legacy_executor_id(fetch_mode)
    request = _execution_request(
        request_id=request_id, site_key="legacy-runtime", site_skill_id="absent",
        site_skill_version="0.0.0", site_skill_digest=sentinel_digest,
        recipe_id="legacy-runtime", run_id=str(run_id), scope_id=str(scope_id),
        executor_id=executor_id, url=url, requested_at=started_at, config={},
        metadata={"authority_mode": "legacy_runtime", "content_kind": "document",
                  "fallback_position": 0, "profile_id": None,
                  "legacy_fetch_mode": fetch_mode,
                  "legacy_executor_label": f"legacy_{fetch_mode}_document",
                  "site_skill_lineage": "absent", "executor_version": "legacy-runtime"},
    )
    accepted = classification == "accepted"
    lineage = {field: getattr(request, field) for field in (
        "request_id", "site_key", "site_skill_id", "site_skill_version",
        "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id")}
    result = CaptureResult(
        **lineage, state="succeeded" if accepted else "failed",
        started_at=started_at, finished_at=finished_at, final_url=url if accepted else None,
        content=(CaptureContent(
            media_type="application/octet-stream", text="",
            metadata={"content_kind": "document", "representation": "legacy_parent_persisted"},
        ) if accepted else None),
        error=None if accepted else CaptureError(code=classification, message=error_message or classification),
        metadata={"authority_mode": "legacy_runtime", "content_kind": "document",
                  "legacy_fetch_mode": fetch_mode,
                  "legacy_executor_label": f"legacy_{fetch_mode}_document",
                  "acquisition_classification": classification,
                  "acquisition_validation": {"decision": classification}},
    )
    contract = ContractAcquisitionAttempt.model_validate_json(json.dumps({
        "attempt_id": request_id,
        "request": _canonical_request(request).model_dump(mode="json"),
        "result": result.model_dump(mode="json"),
        "accepted": accepted, "acceptance_reason": classification,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return AcquisitionAttempt(
        attempt_id=request_id, request_id=request_id, scope_id=scope_id, run_id=run_id,
        position=0, content_kind="document", site_skill_id="absent",
        site_skill_version="0.0.0", site_skill_package_sha256=sentinel_digest,
        recipe_id="legacy-runtime", executor_id=executor_id, executor_version="legacy-runtime",
        requested_url=url, final_url=url if accepted else None, requested_at=started_at,
        started_at=started_at, finished_at=finished_at, classification=classification,
        accepted=accepted, reason=classification, validation={"decision": classification},
        canonical_json=json.dumps(redact_persisted_value(contract.model_dump(mode="json")),
                                  sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        authority_mode="legacy_runtime",
    )


_SECRET_KEYS = ("apikey", "authorization", "cookie", "credential", "password", "secret", "token", "argv")
_HOST_PATH_KEYS = ("executable", "executable_path", "binary_path", "host_path")


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_secret_key(value: str) -> bool:
    compact = _normalized_key(value)
    return any(marker in compact for marker in _SECRET_KEYS)


def _redact_text(value: str) -> str:
    import re
    value = re.sub(
        r"https?://[^\s<>\"']+",
        lambda match: _redact_url(match.group(0)),
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"(?i)(bearer|basic)\s+[^\s,;]+", r"\1 [REDACTED]", value)
    value = re.sub(r"(?i)(token|password|secret|api[_-]?key|cookie|authorization)\s*[:=]\s*[^\s,;&#<>\"']+", r"\1=[REDACTED]", value)
    value = re.sub(
        r"(?i)([?&#](?:access[_-]?token|token|api[_-]?key|password|authorization|cookie|secret|credential)(?:=|/))[^&#\s<>\"']*",
        r"\1[REDACTED]", value,
    )
    return value


def _redact_diagnostic_text(value: str) -> str:
    import re
    value = _redact_text(value)
    value = re.sub(
        r"(?<![A-Za-z0-9./:])(?:/[A-Za-z0-9_.-]+){2,}(?:/[A-Za-z0-9_.-]+)?",
        "[HOST_PATH_REDACTED]", value,
    )
    return re.sub(
        r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/](?:[^\s,;:\"']+[\\/])*[^\s,;:\"']+)",
        "[HOST_PATH_REDACTED]", value,
    )[:4096]


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _redact_text(value)
    if not parsed.scheme or not parsed.netloc:
        return _redact_text(value)
    query = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        query.append((name, "[REDACTED]" if _is_secret_key(name) else _redact_text(item)))
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    # Userinfo is credential material; remove the whole authority prefix so the
    # redacted value remains a valid credential-free URL contract value.
    fragment = []
    for name, item in parse_qsl(parsed.fragment, keep_blank_values=True):
        fragment.append(("[REDACTED]", "[REDACTED]") if _is_secret_key(name)
                        else (name, _redact_text(item)))
    rendered_fragment = urlencode(fragment) if fragment else _redact_text(parsed.fragment)
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), rendered_fragment))


def _redact_json(value: Any, key: str = "") -> Any:
    if _is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        redacted = {}
        hidden = 0
        for item_key, item_value in value.items():
            item_key = str(item_key)
            if item_key == "inline_artifacts":
                continue
            if _is_secret_key(item_key):
                replacement = "redacted_field" if hidden == 0 else f"redacted_field_{hidden}"
                hidden += 1
                redacted[replacement] = "[REDACTED]"
            else:
                redacted[item_key] = _redact_json(item_value, item_key)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        if _normalized_key(key) in {_normalized_key(item) for item in _HOST_PATH_KEYS} and (
                value.startswith(("/", "\\")) or (len(value) > 2 and value[1:3] in {":/", ":\\"})):
            return "[HOST_PATH_REDACTED]"
        if _normalized_key(key) in {"message", "diagnostic", "errormessage"}:
            return _redact_diagnostic_text(value)
        return _redact_url(value)
    return value


def _canonical_attempt(attempt: AcquisitionAttempt) -> str:
    payload = attempt.model_dump(mode="json", exclude={"canonical_json", "artifacts"})
    return json.dumps(_redact_json(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def redact_persisted_value(value: Any) -> Any:
    """Apply the single structural policy used by persisted and exported evidence."""
    return _redact_json(value)


def canonicalize_persisted_value(value: Any) -> Any:
    """Normalize persistence data, redacting semantic secrets without dropping safe authority."""
    sanitized = _redact_json(value)
    if isinstance(sanitized, dict):
        quality = sanitized.get("quality_gates")
        if isinstance(quality, dict) and "blocked_markers" in quality:
            markers = quality["blocked_markers"]
            quality["blocked_markers"] = ["[REDACTED]"] * len(markers) if isinstance(markers, list) else []
    return sanitized


def _redact_marker_values(value: Any, markers: Sequence[Any]) -> Any:
    """Remove configured blocked-marker literals from canonical evidence values."""
    if isinstance(value, Mapping):
        return {str(key): _redact_marker_values(item, markers) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_marker_values(item, markers) for item in value]
    if isinstance(value, str):
        import re
        for marker in markers:
            if str(marker):
                value = re.sub(re.escape(str(marker)), "[BLOCKED_MARKER]", value,
                               flags=re.IGNORECASE)
    return value


def _execution_request(**values: Any) -> CaptureRequest:
    """Build trusted executor input without applying the persistence-only JSON policy."""
    executable_config = values.get("config", {})
    executable_metadata = values.get("metadata", {})
    portable = {**values, "config": canonicalize_persisted_value(executable_config),
                "metadata": canonicalize_persisted_value(executable_metadata)}
    validated = CaptureRequest.model_validate(portable)
    if portable["config"] == executable_config:
        return validated
    execution_values = dict(validated.__dict__)
    execution_values.update(config=executable_config)
    return CaptureRequest.model_construct(**execution_values)


def _canonical_request(request: CaptureRequest) -> CaptureRequest:
    payload = request.model_dump(mode="python")
    payload["config"] = canonicalize_persisted_value(payload.get("config", {}))
    payload["metadata"] = canonicalize_persisted_value(payload.get("metadata", {}))
    return CaptureRequest.model_validate(payload)


def canonical_redacted_attempt(attempt: AcquisitionAttempt) -> str:
    return _canonical_attempt(attempt)


def _numeric_id(value: str) -> int:
    tail = str(value).rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _legacy_executor_id(fetch_mode: str) -> str:
    return "browser_rendered" if fetch_mode.casefold() in {
        "browser", "rendered", "playwright", "browser_rendered"
    } else "web_http"


__all__ = ["AcquisitionGateway", "AcquisitionOutcome", "GovernedAcquisitionGateway",
           "LegacyCrawlerGateway", "canonical_redacted_attempt", "legacy_document_runtime_attempt",
           "redact_persisted_value"]
