from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module, metadata
from pathlib import Path
from typing import Any

from web_listening.blocks.acquisition_terminal import classify_html_capture
from web_listening.blocks.crawler import FetchResult
from web_listening.blocks.governed_read import ROLLBACK_REQUIRED_READ_ERRORS
from web_listening.blocks.normalizer import normalize_html
from web_listening.contracts import (
    CaptureContent,
    CaptureError,
    CaptureRequest,
    CaptureResult,
)
from web_listening.executors.wrapper_protocol import run_stdio_wrapper

ADAPTER_VERSION = "1.0.0"
PLAYWRIGHT_RUNTIME_VERSION = "1.62.0"
_PREPARE_TOKEN = object()
_SUPPORTED_WAIT_UNTIL = frozenset({"domcontentloaded", "load", "networkidle"})
_FAILURE_CODES = frozenset(
    {
        "blocked",
        "blocked_redirect",
        "browser_authority_mismatch",
        "browser_authority_required",
        "browser_config_unsupported",
        "cancelled",
        "close_failure",
        "empty_content",
        "http_403",
        "http_status_rejected",
        "launch_failure",
        "missing_runtime",
        "navigation_failed",
        "navigation_timeout",
        "page_closed",
        "rendered_content_limit",
        "unsupported_content_kind",
    }
)


class BrowserCaptureError(RuntimeError):
    """Finite browser failure safe for capture-result.v1 evidence."""

    def __init__(
        self,
        code: str,
        *,
        final_url: str | None = None,
        status_code: int | None = None,
    ) -> None:
        if code not in _FAILURE_CODES:
            raise ValueError("unknown browser capture failure code")
        message = (
            "direct browser-rendered target reads are disabled; "
            "browser_authority_required"
            if code == "browser_authority_required"
            else code
        )
        super().__init__(message)
        self.code = code
        self.final_url = final_url
        self.status_code = status_code


class BrowserCaptureCancelled(asyncio.CancelledError):
    """Stable cancellation marker that retains asyncio cancellation semantics."""

    code = "cancelled"


@dataclass(frozen=True, slots=True)
class _PreparedBrowserAuthority:
    plan: Any
    step: Mapping[str, Any]
    read_gateway: Any
    session_factory: Callable[[], Any]
    plan_json: str
    plan_fingerprint: str
    step_position: int
    config_json: str
    gateway_type: type
    gateway_read: object
    timeout_seconds: float
    timeout_milliseconds: int
    max_body_bytes: int
    wait_until: str

    def validate(self) -> None:
        from web_listening.blocks.acquisition_execution_plan import (
            AcquisitionExecutionPlan,
        )

        try:
            normalized_config = _normalize_browser_config(self.step.get("config", {}))
        except BrowserCaptureError as exc:
            raise BrowserCaptureError("browser_authority_mismatch") from exc
        if (
            type(self.plan) is not AcquisitionExecutionPlan
            or self.plan.mode != "governed"
            or self.plan.acquisition_fingerprint != self.plan_fingerprint
            or self.plan.to_json() != self.plan_json
            or self.step_position < 0
            or self.step_position >= len(self.plan.steps)
            or self.plan.steps[self.step_position] is not self.step
            or self.step.get("position") != self.step_position
            or self.step.get("executor_id") != "browser_rendered"
            or self.step.get("adapter") != "browser_rendered"
            or self.step.get("executor_version") != ADAPTER_VERSION
            or type(self.read_gateway) is not self.gateway_type
            or getattr(self.gateway_type, "read", None) is not self.gateway_read
            or "read" in getattr(self.read_gateway, "__dict__", {})
            or _canonical_json(normalized_config) != self.config_json
            or normalized_config["wait_until"] != self.wait_until
            or not callable(self.session_factory)
        ):
            raise BrowserCaptureError("browser_authority_mismatch")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _normalize_browser_config(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise BrowserCaptureError("browser_config_unsupported")
    config = dict(value)
    if set(config) - {"wait_until"}:
        raise BrowserCaptureError("browser_config_unsupported")
    wait_until = config.get("wait_until", "domcontentloaded")
    if type(wait_until) is not str or wait_until not in _SUPPORTED_WAIT_UNTIL:
        raise BrowserCaptureError("browser_config_unsupported")
    return {"wait_until": wait_until}


def _portable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    def portable(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): portable(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [portable(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        return str(item)

    return {str(key): portable(child) for key, child in value.items()}


def _capture_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    portable = _portable_mapping(value)
    return {
        key: (_canonical_json(child) if isinstance(child, (dict, list)) else child)
        for key, child in portable.items()
    }


class _PlaywrightSession:
    """One browser capture lifecycle; no resource survives ``close``."""

    runtime_id = "playwright"

    def __init__(
        self,
        *,
        sync_api: Any,
        runtime_version: str,
        temporary_directory_factory: Callable[..., Any] = tempfile.TemporaryDirectory,
    ) -> None:
        self.runtime_version = runtime_version
        self._sync_api = sync_api
        self._temporary_directory_factory = temporary_directory_factory
        self._temporary_manager = None
        self._temporary_path: str | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def open(self) -> None:
        if self.runtime_version != PLAYWRIGHT_RUNTIME_VERSION:
            raise BrowserCaptureError("missing_runtime")
        try:
            self._temporary_manager = self._temporary_directory_factory(
                prefix="web-listening-browser-reader-"
            )
        except TypeError:
            self._temporary_manager = self._temporary_directory_factory()
        try:
            self._temporary_path = self._temporary_manager.__enter__()
            self._playwright = self._sync_api.sync_playwright().start()
            executable = getattr(
                getattr(self._playwright, "chromium", None), "executable_path", None
            )
            if not isinstance(executable, str) or not Path(executable).is_file():
                raise BrowserCaptureError("missing_runtime")
        except BrowserCaptureError:
            raise
        except Exception as exc:
            raise BrowserCaptureError("missing_runtime") from exc

    def render(
        self,
        response: Any,
        *,
        timeout_milliseconds: int,
        wait_until: str,
    ) -> str:
        if self._playwright is None or self._temporary_path is None:
            raise BrowserCaptureError("missing_runtime")
        try:
            self._browser = self._playwright.chromium.launch(
                headless=True,
                downloads_path=self._temporary_path,
                timeout=timeout_milliseconds,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise BrowserCaptureError("launch_failure") from exc

        try:
            self._context = self._browser.new_context(
                accept_downloads=False,
                offline=True,
                service_workers="block",
            )
            self._page = self._context.new_page()
            fulfilled = False

            def route_request(route: Any) -> None:
                nonlocal fulfilled
                request = route.request
                is_navigation = request.is_navigation_request()
                if (
                    not fulfilled
                    and is_navigation
                    and str(request.url) == response.final_url
                ):
                    fulfilled = True
                    route.fulfill(
                        status=response.status_code,
                        headers={"content-type": response.content_type or "text/html"},
                        body=response.body,
                    )
                    return
                route.abort("blockedbyclient")

            self._page.route("**/*", route_request)
            self._page.route_web_socket("**/*", lambda web_socket: web_socket.close())
            navigation = self._page.goto(
                response.final_url,
                wait_until=wait_until,
                timeout=timeout_milliseconds,
            )
            if (
                not fulfilled
                or navigation is None
                or navigation.status != response.status_code
            ):
                raise BrowserCaptureError(
                    "navigation_failed",
                    final_url=response.final_url,
                    status_code=response.status_code,
                )
            return self._page.content()
        except BrowserCaptureError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            timeout_type = getattr(self._sync_api, "TimeoutError", ())
            if isinstance(timeout_type, type) and isinstance(exc, timeout_type):
                code = "navigation_timeout"
            elif "closed" in str(exc).casefold():
                code = "page_closed"
            else:
                code = "navigation_failed"
            raise BrowserCaptureError(
                code,
                final_url=response.final_url,
                status_code=response.status_code,
            ) from exc

    def close(self) -> None:
        resources = (
            (self._page, "close"),
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        )
        temporary_manager = self._temporary_manager
        self._page = self._context = self._browser = self._playwright = None
        self._temporary_manager = None
        self._temporary_path = None
        failures: list[BaseException] = []
        for resource, method_name in resources:
            if resource is None:
                continue
            try:
                getattr(resource, method_name)()
            except BaseException as exc:
                failures.append(exc)
        if temporary_manager is not None:
            try:
                temporary_manager.__exit__(None, None, None)
            except BaseException as exc:
                failures.append(exc)
        for failure in failures:
            if not isinstance(failure, Exception):
                raise failure
        if failures:
            raise BrowserCaptureError("close_failure")


def _default_session_factory() -> _PlaywrightSession:
    try:
        runtime_version = metadata.version("playwright")
        sync_api = import_module("playwright.sync_api")
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise BrowserCaptureError("missing_runtime") from exc
    return _PlaywrightSession(
        sync_api=sync_api,
        runtime_version=runtime_version,
    )


class BrowserAcquisitionAdapter:
    adapter_id = "browser_rendered"
    adapter_version = ADAPTER_VERSION
    __slots__ = ("_authority",)

    def __init__(self, crawler: object | None = None) -> None:
        del crawler
        object.__setattr__(self, "_authority", None)

    @classmethod
    def _from_authority(
        cls, authority: _PreparedBrowserAuthority, *, token: object
    ) -> BrowserAcquisitionAdapter:
        if token is not _PREPARE_TOKEN:
            raise BrowserCaptureError("browser_authority_required")
        instance = cls()
        object.__setattr__(instance, "_authority", authority)
        authority.validate()
        return instance

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared browser adapter is immutable")

    def capture(self, url: str, *, config: dict | None = None) -> FetchResult:
        authority = self._authority
        if authority is None:
            raise BrowserCaptureError("browser_authority_required")
        authority.validate()
        normalized_config = _normalize_browser_config(config or {})
        if _canonical_json(normalized_config) != authority.config_json:
            raise BrowserCaptureError("browser_authority_mismatch")

        session = None
        primary: BaseException | None = None
        result: FetchResult | None = None
        try:
            session = authority.session_factory()
            session.open()
            response = authority.read_gateway.read(
                url,
                max_body_bytes=authority.max_body_bytes,
                timeout_seconds=authority.timeout_seconds,
            )
            rendered_html = session.render(
                response,
                timeout_milliseconds=authority.timeout_milliseconds,
                wait_until=authority.wait_until,
            )
            rendered_bytes = rendered_html.encode("utf-8")
            if len(rendered_bytes) > authority.max_body_bytes:
                raise BrowserCaptureError(
                    "rendered_content_limit",
                    final_url=response.final_url,
                    status_code=response.status_code,
                )
            page = normalize_html(rendered_html, base_url=response.final_url)
            terminal = classify_html_capture(
                requested_url=url,
                final_url=response.final_url,
                status_code=response.status_code,
                extracted_text=page.content_text,
                raw_text=page.raw_html,
            )
            if terminal != "accepted":
                raise BrowserCaptureError(
                    terminal,
                    final_url=response.final_url,
                    status_code=response.status_code,
                )
            content_sha256 = hashlib.sha256(page.raw_html.encode("utf-8")).hexdigest()
            access_decision = response.access_decision
            result = FetchResult(
                raw_html=page.raw_html,
                cleaned_html=page.cleaned_html,
                content_text=page.content_text,
                markdown=page.markdown,
                fit_markdown=page.fit_markdown,
                metadata_json=_portable_mapping(
                    {
                        **dict(page.metadata),
                        "requested_url": url,
                        "final_url": response.final_url,
                        "content_sha256": content_sha256,
                        "source_sha256": response.sha256,
                        "adapter_id": self.adapter_id,
                        "adapter_version": self.adapter_version,
                        "runtime_id": str(session.runtime_id),
                        "runtime_version": str(session.runtime_version),
                        "network_policy": "governed_main_document_subresources_blocked",
                        "access_decision_id": access_decision.decision_id,
                        "access_decision_sha256": access_decision.decision_sha256,
                        "access_reason_code": access_decision.reason_code,
                        "redirect_count": len(access_decision.redirect_hops),
                        "content_type": response.content_type or "text/html",
                    }
                ),
                final_url=response.final_url,
                status_code=response.status_code,
            )
        except BaseException as exc:
            primary = exc

        close_failure: BaseException | None = None
        if session is not None:
            try:
                session.close()
            except BaseException as exc:
                close_failure = exc
        if primary is not None and not isinstance(primary, Exception):
            if isinstance(primary, BrowserCaptureCancelled):
                raise primary
            if isinstance(primary, asyncio.CancelledError):
                raise BrowserCaptureCancelled() from primary
            raise primary
        if close_failure is not None and not isinstance(close_failure, Exception):
            if isinstance(close_failure, BrowserCaptureCancelled):
                raise close_failure
            if isinstance(close_failure, asyncio.CancelledError):
                raise BrowserCaptureCancelled() from close_failure
            raise close_failure
        if primary is not None:
            if isinstance(primary, ROLLBACK_REQUIRED_READ_ERRORS):
                raise primary
            if isinstance(primary, BrowserCaptureError):
                raise primary
            raise BrowserCaptureError("navigation_failed") from primary
        if close_failure is not None:
            if isinstance(close_failure, BrowserCaptureError):
                raise close_failure
            raise BrowserCaptureError("close_failure") from close_failure
        if result is None:
            raise BrowserCaptureError("navigation_failed")
        return result


def prepare_browser_acquisition_adapter(
    plan: Any,
    step: Mapping[str, Any],
    read_gateway: Any,
    *,
    session_factory: Callable[[], Any] | None = None,
) -> BrowserAcquisitionAdapter:
    """Bind one browser adapter to one immutable compiled plan step and gateway."""
    from web_listening.blocks.acquisition_execution_plan import AcquisitionExecutionPlan
    from web_listening.blocks.governed_read import GovernedReadGateway

    if type(plan) is not AcquisitionExecutionPlan:
        raise BrowserCaptureError("browser_authority_mismatch")
    try:
        position = int(step["position"])
        limits = step["limits"]
        timeout = float(limits["timeout_seconds"])
        max_body_bytes = limits["stdout_bytes"]
    except (KeyError, TypeError, ValueError) as exc:
        raise BrowserCaptureError("browser_authority_mismatch") from exc
    if (
        isinstance(max_body_bytes, bool)
        or not isinstance(max_body_bytes, int)
        or max_body_bytes < 1
        or timeout <= 0
    ):
        raise BrowserCaptureError("browser_authority_mismatch")
    normalized_config = _normalize_browser_config(step.get("config", {}))
    if type(read_gateway) is GovernedReadGateway:
        gateway = read_gateway.gateway
        if gateway.config.diagnostic_artifact_sha256 != plan.acquisition_fingerprint:
            raise BrowserCaptureError("browser_authority_mismatch")
    authority = _PreparedBrowserAuthority(
        plan=plan,
        step=step,
        read_gateway=read_gateway,
        session_factory=session_factory or _default_session_factory,
        plan_json=plan.to_json(),
        plan_fingerprint=plan.acquisition_fingerprint,
        step_position=position,
        config_json=_canonical_json(normalized_config),
        gateway_type=type(read_gateway),
        gateway_read=getattr(type(read_gateway), "read", None),
        timeout_seconds=timeout,
        timeout_milliseconds=max(1, int(timeout * 1000)),
        max_body_bytes=max_body_bytes,
        wait_until=normalized_config["wait_until"],
    )
    authority.validate()
    return BrowserAcquisitionAdapter._from_authority(authority, token=_PREPARE_TOKEN)


class BrowserAcquisitionExecutor:
    executor_id = "browser_rendered"
    __slots__ = ("_adapter",)

    def __init__(self, adapter: BrowserAcquisitionAdapter) -> None:
        if type(adapter) is not BrowserAcquisitionAdapter or adapter._authority is None:
            raise BrowserCaptureError("browser_authority_required")
        adapter._authority.validate()
        object.__setattr__(self, "_adapter", adapter)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("prepared browser executor is immutable")

    def execute(self, request: CaptureRequest) -> CaptureResult:
        started = datetime.now(timezone.utc)
        lineage = request.model_dump(
            include={
                "site_key",
                "site_skill_id",
                "site_skill_version",
                "site_skill_digest",
                "recipe_id",
                "run_id",
                "scope_id",
                "request_id",
                "executor_id",
            }
        )
        if request.metadata.get("content_kind") != "page":
            error = BrowserCaptureError("unsupported_content_kind")
        else:
            try:
                page = self._adapter.capture(
                    str(request.url), config=dict(request.config)
                )
            except BrowserCaptureError as exc:
                error = exc
            else:
                metadata_json = dict(page.metadata_json)
                return CaptureResult(
                    **lineage,
                    state="succeeded",
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    final_url=page.final_url,
                    status_code=page.status_code,
                    content=CaptureContent(
                        media_type=str(metadata_json["content_type"]),
                        text=page.raw_html,
                        sha256=str(metadata_json["content_sha256"]),
                        metadata={
                            "representation": "utf-8",
                            "sha256_scope": "rendered-html",
                            "adapter_id": metadata_json["adapter_id"],
                            "adapter_version": metadata_json["adapter_version"],
                            "runtime_id": metadata_json["runtime_id"],
                            "runtime_version": metadata_json["runtime_version"],
                        },
                    ),
                    metadata=_capture_metadata(
                        {
                            **metadata_json,
                            "content_text": page.content_text,
                            "markdown": page.markdown,
                            "fit_markdown": page.fit_markdown,
                        }
                    ),
                )
        failure_metadata: dict[str, object] = {
            "requested_url": str(request.url),
            "adapter_id": "browser_rendered",
            "adapter_version": ADAPTER_VERSION,
            "runtime_id": "playwright",
            "runtime_version": (
                "" if error.code == "missing_runtime" else PLAYWRIGHT_RUNTIME_VERSION
            ),
        }
        if error.final_url is not None:
            failure_metadata["final_url"] = error.final_url
        return CaptureResult(
            **lineage,
            state="failed",
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            final_url=error.final_url,
            status_code=error.status_code,
            error=CaptureError(code=error.code, message=error.code),
            metadata=failure_metadata,
        )

    def close(self) -> None:
        return None


def execute(request: CaptureRequest) -> CaptureResult:
    """Stable local denial for the legacy stdio wrapper; it has no plan authority."""
    started = datetime.now(timezone.utc)
    lineage = request.model_dump(
        include={
            "site_key",
            "site_skill_id",
            "site_skill_version",
            "site_skill_digest",
            "recipe_id",
            "run_id",
            "scope_id",
            "request_id",
            "executor_id",
        }
    )
    return CaptureResult(
        **lineage,
        state="failed",
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        error=CaptureError(
            code="browser_authority_required", message="browser_authority_required"
        ),
        metadata={
            "requested_url": str(request.url),
            "adapter_id": "browser_rendered",
            "adapter_version": ADAPTER_VERSION,
        },
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_stdio_wrapper(execute))


__all__ = [
    "ADAPTER_VERSION",
    "PLAYWRIGHT_RUNTIME_VERSION",
    "BrowserAcquisitionAdapter",
    "BrowserAcquisitionExecutor",
    "BrowserCaptureCancelled",
    "BrowserCaptureError",
    "execute",
    "prepare_browser_acquisition_adapter",
]
