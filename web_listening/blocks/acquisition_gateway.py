"""Acquisition boundary used by the parent-owned tree crawler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import json
from typing import Protocol, TYPE_CHECKING
from urllib.parse import urlsplit

from web_listening.blocks.crawler import Crawler, FetchResult
from web_listening.blocks.diff import extract_links, find_document_links
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.registry import ExecutorRegistry

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

    @property
    def accepted(self) -> bool:
        return self.classification == "accepted" and self.page is not None


class AcquisitionGateway(Protocol):
    def acquire(
        self, url: str, *, run_id: str, scope_id: str, content_kind: str = "page"
    ) -> AcquisitionOutcome: ...

    def close(self) -> None: ...


class LegacyCrawlerGateway:
    """Compatibility boundary retaining the historical deterministic driver choice."""

    def __init__(self, crawler: Crawler, *, fetch_mode: str, fetch_config_json: dict):
        self.crawler = crawler
        self.fetch_mode = fetch_mode
        self.fetch_config_json = dict(fetch_config_json or {})

    def acquire(
        self, url: str, *, run_id: str, scope_id: str, content_kind: str = "page"
    ) -> AcquisitionOutcome:
        try:
            page = self.crawler.fetch_page(
                url, fetch_mode=self.fetch_mode, fetch_config_json=self.fetch_config_json
            )
        except Exception as exc:
            return AcquisitionOutcome(None, None, None, "executor_error", (type(exc).__name__,), False)
        classification = "not_found" if page.status_code in {404, 410} else "accepted"
        return AcquisitionOutcome(None, None, page, classification, (classification,), True)

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
        attempts: list[str] = []
        last_request: CaptureRequest | None = None
        last_result: CaptureResult | None = None
        for step in self.plan.steps:
            request = self._request(url, run_id, scope_id, step, content_kind)
            last_request = request
            try:
                result = self.registry.execute(request)
            except Exception as exc:
                attempts.append("executor_error")
                continue
            last_result = result
            classification, terminal, page = self._classify(request, result, content_kind)
            attempts.append(classification)
            if classification == "accepted":
                return AcquisitionOutcome(request, result, page, classification, tuple(attempts), True)
            if terminal:
                return AcquisitionOutcome(
                    request, result, None, classification, tuple(attempts),
                    classification == "not_found",
                )
        classification = attempts[-1] if attempts else "executor_error"
        return AcquisitionOutcome(last_request, last_result, None, classification, tuple(attempts), False)

    def _request(self, url: str, run_id: str, scope_id: str, step, content_kind: str) -> CaptureRequest:
        identity = json.dumps({
            "plan": self.plan.acquisition_fingerprint, "run_id": run_id, "scope_id": scope_id,
            "url": url, "position": step["position"], "executor_id": step["executor_id"],
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return CaptureRequest(
            request_id=hashlib.sha256(identity.encode()).hexdigest(),
            site_key=self.plan.site_key, site_skill_id=self.plan.site_skill_id or "",
            site_skill_version=self.plan.site_skill_version or "",
            site_skill_digest=self.plan.site_skill_package_sha256 or "",
            recipe_id=step["recipe_id"], run_id=run_id, scope_id=scope_id,
            executor_id=step["executor_id"], url=url, requested_at=datetime.now(timezone.utc),
            config=dict(step.get("config", {})),
            metadata={"acquisition_fingerprint": self.plan.acquisition_fingerprint,
                      "content_kind": content_kind,
                      "executor_version": step["executor_version"],
                      "script_sha256": step["script_sha256"]},
        )

    def _classify(
        self, request: CaptureRequest, result: CaptureResult, content_kind: str
    ) -> tuple[str, bool, FetchResult | None]:
        if not isinstance(result, CaptureResult):
            return "protocol_error", True, None
        lineage = ("request_id", "site_key", "site_skill_id", "site_skill_version",
                   "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id")
        if any(getattr(request, field) != getattr(result, field) for field in lineage):
            return "lineage_mismatch", True, None
        if result.state != "succeeded":
            code = (result.error.code if result.error else "executor_error").lower()
            if any(
                marker in code for marker in ("protocol", "identity", "lineage", "integrity", "runtime_mismatch")
            ):
                return "integrity_error", True, None
            if "timeout" in code:
                return "timeout", False, None
            if "block" in code:
                return "blocked", False, None
            return "executor_error", False, None
        if self._origin(str(result.final_url or request.url)) != self._origin(str(request.url)):
            return "unsafe_redirect", True, None
        if result.status_code in {404, 410}:
            return "not_found", True, None
        if result.status_code is None or not 200 <= result.status_code < 300 or result.content is None:
            return "failed_quality_gate", False, None
        if content_kind == "document":
            metadata = result.content.metadata
            if (
                result.content.text is None
                or result.content.sha256 is None
                or metadata.get("representation") != "base64"
                or metadata.get("sha256_scope") != "decoded-bytes"
            ):
                return "failed_quality_gate", False, None
            try:
                payload = base64.b64decode(result.content.text, validate=True)
            except (binascii.Error, ValueError):
                return "integrity_error", True, None
            if hashlib.sha256(payload).hexdigest() != result.content.sha256:
                return "integrity_error", True, None
            return "accepted", False, FetchResult(
                raw_html="", cleaned_html="", content_text="", markdown="",
                fit_markdown="", metadata_json={},
                final_url=str(result.final_url or request.url), status_code=result.status_code,
            )
        page = self._page(result)
        links = extract_links(page.raw_html, page.final_url or str(request.url))
        document_links = find_document_links(links)
        gates = self.plan.quality_gates
        if (len(page.content_text.split()) < gates.get("min_words", 0)
                or len(links) < gates.get("min_links", 0)
                or len(document_links) < gates.get("min_document_links", 0)):
            return "failed_quality_gate", False, None
        lowered = page.content_text.casefold()
        if any(str(marker).casefold() in lowered for marker in gates.get("blocked_markers", ())):
            return "blocked", False, None
        return "accepted", False, page

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port

    @staticmethod
    def _page(result: CaptureResult) -> FetchResult:
        text = result.content.text if result.content else ""
        from web_listening.blocks.normalizer import normalize_html

        page = normalize_html(text or "", base_url=str(result.final_url or ""))
        untrusted = dict(result.metadata)
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


__all__ = ["AcquisitionGateway", "AcquisitionOutcome", "GovernedAcquisitionGateway", "LegacyCrawlerGateway"]
