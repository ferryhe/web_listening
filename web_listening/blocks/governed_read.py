"""Single target-content read facade over the frozen governed AccessGateway."""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from email.message import Message
from typing import Any

from httpx import MockTransport, Request, Response

from web_listening.blocks.access_gateway import (
    AccessGateway,
    AccessGatewayBudgetError,
    AccessGatewayConfig,
    AccessGatewayConsumerContext,
    AccessGatewayError,
    AccessGatewayOriginError,
    AccessGatewayPolicyError,
    AccessGatewayRedirectError,
    AccessGatewayTransportError,
)
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    RawHttpResponse,
    SafePinnedTransport,
    normalize_http_url,
    read_bounded_body,
)
from web_listening.contracts.access_decision import (
    AccessDecision,
    AccessRejectionErrorEnvelope,
)
from web_listening.contracts.site_diagnostic import (
    DiagnosticIdentity,
    canonical_sha256,
)

_MOCK_STATE_LOCK_TYPE = type(threading.RLock())


class AccessRejectedError(RuntimeError):
    """A frozen robots reject/error returned before target content was opened."""

    def __init__(self, decision: AccessDecision) -> None:
        envelope = decision.rejection_or_error
        if envelope is None:
            raise ValueError("access rejection requires a frozen rejection envelope")
        super().__init__(envelope.message)
        self.decision = decision
        self.envelope = envelope


GOVERNED_READ_RUNTIME_ERRORS = (
    AccessGatewayError,
    BodyFailure,
)
ROLLBACK_REQUIRED_READ_ERRORS = (
    AccessRejectedError,
    *GOVERNED_READ_RUNTIME_ERRORS,
)


def governed_read_failure_payload(
    error: AccessGatewayError | BodyFailure,
) -> dict[str, object]:
    """Render a stable non-robots failure without changing the frozen #49 contract."""
    retryable = False
    if isinstance(error, AccessGatewayTransportError):
        error_code = f"gateway.transport.{error.kind}"
        retryable = error.retryable
    elif isinstance(error, AccessGatewayOriginError):
        error_code = "gateway.origin"
    elif isinstance(error, AccessGatewayRedirectError):
        error_code = "gateway.redirect"
    elif isinstance(error, AccessGatewayBudgetError):
        error_code = "gateway.budget"
    elif isinstance(error, AccessGatewayPolicyError):
        error_code = "gateway.policy"
    elif isinstance(error, BodyFailure):
        error_code = f"body.{error.reason}"
        retryable = error.retryable
    else:
        error_code = "gateway.error"
    return {
        "schema_version": "governed-read-error.v1",
        "error_type": type(error).__name__.lstrip("_"),
        "error_code": error_code,
        "message": "governed target read failed",
        "retryable": retryable,
    }


@dataclass(frozen=True, slots=True)
class GovernedReadResult:
    body: bytes
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    sha256: str
    access_decision: AccessDecision
    wire_bytes: int = 0
    decoded_bytes: int = 0
    wire_encoding: str = "identity"
    content_encoding: str = "identity"
    filename: str | None = None

    @property
    def content_type(self) -> str:
        return str(self.headers.get("content-type", ""))

    @property
    def etag(self) -> str:
        return str(self.headers.get("etag", ""))

    @property
    def last_modified(self) -> str:
        return str(self.headers.get("last-modified", ""))


class GovernedReadGateway:
    """Read bounded bytes only after AccessGateway authorizes every request hop."""

    def __init__(self, gateway: AccessGateway, *, max_body_bytes: int) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        object.__setattr__(
            self, "_GovernedReadGateway__runtime_lock", threading.RLock()
        )
        object.__setattr__(
            self,
            "_GovernedReadGateway__runtime_dispatch",
            _GOVERNED_READ_RUNTIME_DISPATCH,
        )
        object.__setattr__(self, "_GovernedReadGateway__gateway", gateway)
        object.__setattr__(self, "_GovernedReadGateway__max_body_bytes", max_body_bytes)
        object.__setattr__(
            self,
            "_GovernedReadGateway__runtime_seal",
            _GOVERNED_READ_RUNTIME_DISPATCH.runtime_snapshot(self),
        )

    @property
    def gateway(self) -> AccessGateway:
        with self.__runtime_lock:
            return self.__gateway

    @property
    def max_body_bytes(self) -> int:
        with self.__runtime_lock:
            return self.__max_body_bytes

    def _seal_runtime(self) -> tuple[object, ...]:
        with self.__runtime_lock:
            snapshot = self.__runtime_dispatch.runtime_snapshot(self)
            if snapshot != self.__runtime_seal:
                raise AccessGatewayTransportError(
                    "transport_integrity", "governed read call graph changed"
                )
            return snapshot

    def _validate_runtime(self) -> None:
        self._seal_runtime()

    @property
    def user_agent(self) -> str:
        with self.__runtime_lock:
            self.__runtime_dispatch.validate_runtime(self)
            gateway = self.__gateway
        return gateway.config.identity.user_agent

    def read(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        before_target_request: (
            Callable[[str, AccessDecision], Callable[[], None] | None] | None
        ) = None,
        timeout_seconds: float | None = None,
    ) -> GovernedReadResult:
        with self.__runtime_lock:
            dispatch = self.__runtime_dispatch
            dispatch.validate_runtime(self)
            gateway = self.__gateway
            effective_limit = self.__max_body_bytes
        if max_body_bytes is not None:
            if type(max_body_bytes) is not int or max_body_bytes < 1:
                raise ValueError("max_body_bytes must be a positive integer")
            effective_limit = min(effective_limit, max_body_bytes)
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")

        body_evidence = None

        def consume(
            raw: RawHttpResponse,
            context: AccessGatewayConsumerContext,
        ) -> bytes:
            nonlocal body_evidence
            dispatch.validate_runtime(self)
            try:
                body_evidence = dispatch.read_bounded_body(
                    raw,
                    url=context.final_url,
                    wire_limit=effective_limit,
                    decoded_limit=effective_limit,
                    aggregate_wire_remaining=effective_limit,
                    aggregate_decoded_remaining=effective_limit,
                )
            except BodyFailure as exc:
                dispatch.attach_body_failure_context(
                    exc,
                    decision=context.decision,
                    final_url=context.final_url,
                    status_code=context.status_code,
                    redirect_hops=context.redirect_hops,
                )
                raise
            return body_evidence.body

        result = dispatch.access_request_with_context(
            gateway,
            url,
            consume=consume,
            before_target_request=before_target_request,
            timeout_seconds=timeout_seconds,
        )
        dispatch.validate_runtime(self)
        if result.value is None or result.response is None:
            raise AccessRejectedError(result.decision)
        if body_evidence is None:
            raise RuntimeError("governed read returned no body evidence")
        body = result.value
        try:
            filename = dispatch.content_disposition_filename(result.response.headers)
            content_encoding = dispatch.response_character_encoding(
                result.response.headers
            )
        except ValueError as exc:
            failure = BodyFailure(
                "response_metadata_invalid",
                wire=body_evidence.wire,
                decoded=body_evidence.decoded,
                deterministic=True,
            )
            dispatch.attach_body_failure_context(
                failure,
                decision=result.decision,
                final_url=result.response.final_url,
                status_code=result.response.status,
                redirect_hops=tuple(result.decision.redirect_hops),
            )
            raise failure from exc
        return GovernedReadResult(
            body=body,
            final_url=result.response.final_url,
            status_code=result.response.status,
            headers=result.response.headers,
            sha256=hashlib.sha256(body).hexdigest(),
            access_decision=result.decision,
            wire_bytes=body_evidence.wire,
            decoded_bytes=body_evidence.decoded,
            wire_encoding="gzip" if body_evidence.compressed else "identity",
            content_encoding=content_encoding,
            filename=filename,
        )

    def close(self) -> None:
        return None


_FROZEN_ACCESS_REQUEST_WITH_CONTEXT = AccessGateway.request_with_context
_FROZEN_GOVERNED_READ = GovernedReadGateway.read


class _MockClientTransport:
    """Offline-test transport adapter; construction rejects every real client."""

    _LEGACY_ROBOTS_MODE = object()
    _AGENTIC_ROBOTS_MODE = object()

    def __init__(self, client: Any) -> None:
        transport = getattr(client, "_transport", None)
        if type(transport) is not MockTransport:
            raise ValueError(
                "only httpx.MockTransport may be adapted for offline gateway tests"
            )
        handler_attribute = "_handler" if hasattr(transport, "_handler") else "handler"
        handler = getattr(transport, handler_attribute, None)
        if not callable(handler):
            raise TypeError("offline mock transport requires one callable handler")
        self.client = client
        self._client_identity = id(client)
        self._transport_identity = id(transport)
        self._handler_attribute = handler_attribute
        self._handler_identity = id(handler)
        self._handler_type = type(handler)
        self._handler_call_identity = id(type(handler).__call__)
        self._handler_code_identity = id(getattr(handler, "__code__", None))
        self._transport_capability = transport
        self._handler_capability = handler
        self._handle_request_identity = id(type(transport).handle_request)
        self._handle_request_code_identity = id(
            getattr(type(transport).handle_request, "__code__", None)
        )
        self._legacy_robots_mode_identity = id(type(self)._LEGACY_ROBOTS_MODE)
        self._agentic_robots_mode_identity = id(type(self)._AGENTIC_ROBOTS_MODE)
        self._robots_mode = type(self)._LEGACY_ROBOTS_MODE

    def _prepare_handler_driven_robots(self) -> None:
        self._validate_identity()
        if (
            id(type(self)._LEGACY_ROBOTS_MODE) != self._legacy_robots_mode_identity
            or id(type(self)._AGENTIC_ROBOTS_MODE) != self._agentic_robots_mode_identity
        ):
            raise ValueError("offline mock robots mode identity changed")
        if self._robots_mode is type(self)._AGENTIC_ROBOTS_MODE:
            return
        if self._robots_mode is not type(self)._LEGACY_ROBOTS_MODE:
            raise ValueError("offline mock robots mode changed")
        self._robots_mode = type(self)._AGENTIC_ROBOTS_MODE

    def _validate_identity(
        self,
    ) -> tuple[MockTransport, Callable[[Request], Response]]:
        transport = getattr(self.client, "_transport", None)
        handler = getattr(transport, self._handler_attribute, None)
        if (
            id(self.client) != self._client_identity
            or id(transport) != self._transport_identity
            or type(transport) is not MockTransport
            or transport is not self._transport_capability
            or id(handler) != self._handler_identity
            or handler is not self._handler_capability
            or type(handler) is not self._handler_type
            or id(type(handler).__call__) != self._handler_call_identity
            or id(getattr(handler, "__code__", None)) != self._handler_code_identity
            or id(type(transport).handle_request) != self._handle_request_identity
            or id(getattr(type(transport).handle_request, "__code__", None))
            != self._handle_request_code_identity
            or "handle_request" in vars(transport)
            or id(type(self)._LEGACY_ROBOTS_MODE) != self._legacy_robots_mode_identity
            or id(type(self)._AGENTIC_ROBOTS_MODE) != self._agentic_robots_mode_identity
            or self._robots_mode
            not in {
                type(self)._LEGACY_ROBOTS_MODE,
                type(self)._AGENTIC_ROBOTS_MODE,
            }
        ):
            raise ValueError("offline mock client or transport identity changed")
        return transport, handler

    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
        progress: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> RawHttpResponse:
        del identity_sha256
        _transport, handler = self._validate_identity()
        if progress is not None:
            progress()
        if self._robots_mode is type(self)._LEGACY_ROBOTS_MODE and normalize_http_url(
            url
        )[0].endswith("/robots.txt"):
            return RawHttpResponse(status=404, headers={}, body_chunks=())
        request = Request(
            "GET",
            url,
            headers={"User-Agent": user_agent},
        )
        if timeout_seconds is not None:
            request.extensions["web_listening_timeout_seconds"] = timeout_seconds
        request.read()
        response = handler(request)
        if not isinstance(response, Response):
            raise TypeError("offline mock handler must return an httpx.Response")
        response.request = request
        if progress is not None:
            try:
                progress()
            except BaseException:
                response.close()
                raise
        source_chunks = (
            (response.content,) if response.is_stream_consumed else response.iter_raw()
        )

        def body_chunks():
            try:
                iterator = iter(source_chunks)
                while True:
                    if progress is not None:
                        progress()
                    try:
                        chunk = next(iterator)
                    except StopIteration:
                        return
                    if progress is not None:
                        progress()
                    yield chunk
            finally:
                response.close()

        def close_response() -> None:
            progress_error: BaseException | None = None
            if progress is not None:
                try:
                    progress()
                except BaseException as exc:  # noqa: BLE001 - preserve renewal through cleanup.
                    progress_error = exc
            response.close()
            if progress_error is not None:
                raise progress_error
            if progress is not None:
                progress()

        return RawHttpResponse(
            status=response.status_code,
            headers={
                str(key).casefold(): str(value)
                for key, value in response.headers.items()
            },
            body_chunks=body_chunks(),
            close=close_response,
        )


class MockClientReadGateway:
    """Test-only lazy gateway factory for existing offline MockTransport fixtures."""

    def __init__(self, client: Any, *, user_agent: str, max_body_bytes: int) -> None:
        self._transport = _MockClientTransport(client)
        self._user_agent = user_agent
        self._max_body_bytes = max_body_bytes
        self._state_lock = threading.RLock()
        self._gateways: dict[str, GovernedReadGateway] = {}
        self._prepared_origins = None
        self._preparation_graph = _mock_preparation_graph()

    def _validate_preparation_graph(self) -> None:
        if (
            type(self) is not MockClientReadGateway
            or self._preparation_graph != _mock_preparation_graph()
        ):
            raise ValueError("offline mock preparation graph changed")

    @property
    def gateway(self) -> AccessGateway:
        with self._state_lock:
            if len(self._gateways) != 1:
                raise RuntimeError(
                    "test gateway identity is available after one exact origin is used"
                )
            return next(iter(self._gateways.values())).gateway

    @property
    def user_agent(self) -> str:
        return self._user_agent

    def read(
        self,
        url: str,
        *,
        max_body_bytes: int | None = None,
        before_target_request: (
            Callable[[str, AccessDecision], Callable[[], None] | None] | None
        ) = None,
        timeout_seconds: float | None = None,
    ) -> GovernedReadResult:
        normalized_url, origin = normalize_http_url(url)
        with self._state_lock:
            gateway = self._gateway_for_origin(origin)
        result = _FROZEN_GOVERNED_READ(
            gateway,
            normalized_url,
            max_body_bytes=max_body_bytes,
            before_target_request=before_target_request,
            timeout_seconds=timeout_seconds,
        )
        if result.final_url == normalized_url and normalized_url != url:
            return replace(result, final_url=url)
        return result

    def _preview_origins(self, origins: tuple[str, ...]):
        with self._state_lock:
            _FROZEN_MOCK_VALIDATE_PREPARATION(self)
            normalized = tuple(
                dict.fromkeys(normalize_http_url(value)[1] for value in origins)
            )
            if not normalized:
                raise ValueError("offline mock gateway requires a prepared origin")
            self._transport._validate_identity()
            mode = self._transport._robots_mode
            if mode is type(self._transport)._AGENTIC_ROBOTS_MODE:
                if self._prepared_origins != normalized or set(self._gateways) != {
                    item.as_url_origin() for item in normalized
                }:
                    raise ValueError("offline mock gateway origins already changed")
                gateways = self._gateways
            else:
                gateways = {
                    origin.as_url_origin(): self._build_gateway(origin, normalized)
                    for origin in normalized
                }
            preparation = (
                normalized,
                gateways,
                self._gateways,
                self._prepared_origins,
                mode,
                self._state_lock,
            )
            _FROZEN_MOCK_VALIDATE_PREPARATION(self)
            return preparation

    def _commit_origins(self, preparation) -> None:
        (
            normalized,
            gateways,
            source_gateways,
            source_origins,
            source_mode,
            source_lock,
        ) = preparation
        with self._state_lock:
            _FROZEN_MOCK_VALIDATE_PREPARATION(self)
            self._transport._validate_identity()
            if (
                self._state_lock is not source_lock
                or self._gateways is not source_gateways
                or self._prepared_origins is not source_origins
                or self._transport._robots_mode is not source_mode
            ):
                raise ValueError("offline mock gateway preparation changed")
            if source_mode is type(self._transport)._AGENTIC_ROBOTS_MODE:
                return
            if source_mode is not type(self._transport)._LEGACY_ROBOTS_MODE:
                raise ValueError("offline mock robots mode changed")
            prior = (
                self._gateways,
                self._prepared_origins,
                self._transport._robots_mode,
            )
            try:
                _FROZEN_MOCK_VALIDATE_PREPARATION(self)
                self._gateways = gateways
                self._prepared_origins = normalized
                self._transport._robots_mode = type(
                    self._transport
                )._AGENTIC_ROBOTS_MODE
                _FROZEN_MOCK_VALIDATE_PREPARATION(self)
            except BaseException:
                (
                    self._gateways,
                    self._prepared_origins,
                    self._transport._robots_mode,
                ) = prior
                raise

    def _prepare_origins(self, origins: tuple[str, ...]) -> None:
        self._commit_origins(self._preview_origins(origins))

    def _gateway_for_origin(self, origin) -> GovernedReadGateway:
        with self._state_lock:
            key = origin.as_url_origin()
            gateway = self._gateways.get(key)
            if gateway is None:
                if self._prepared_origins is not None:
                    raise ValueError("offline mock gateway origin is outside its seal")
                gateway = self._build_gateway(origin, (origin,))
                self._gateways[key] = gateway
            return gateway

    def _build_gateway(self, origin, allowed_origins) -> GovernedReadGateway:
        visible_identity = {
            "identity_id": "web-listening-offline-test-v2",
            "product_token": "web-listening-bot",
            "user_agent": self._user_agent,
        }
        identity = DiagnosticIdentity(
            **visible_identity,
            identity_sha256=canonical_sha256(visible_identity),
        )
        gateway = GovernedReadGateway(
            AccessGateway(
                AccessGatewayConfig(
                    identity=identity,
                    allowed_origins=frozenset(allowed_origins),
                    diagnostic_artifact_sha256=hashlib.sha256(
                        f"offline-test:{origin.as_url_origin()}".encode()
                    ).hexdigest(),
                    pacing_interval=timedelta(0),
                    budget_limit=10_000,
                ),
                transport=self._transport,
            ),
            max_body_bytes=self._max_body_bytes,
        )
        return gateway

    def close(self) -> None:
        return None


def _mock_preparation_graph() -> tuple[object, ...]:
    return (
        MockClientReadGateway.read,
        MockClientReadGateway._preview_origins,
        MockClientReadGateway._commit_origins,
        MockClientReadGateway._prepare_origins,
        MockClientReadGateway._gateway_for_origin,
        MockClientReadGateway._build_gateway,
        MockClientReadGateway._validate_preparation_graph,
        normalize_http_url,
        DiagnosticIdentity,
        canonical_sha256,
        AccessGateway,
        AccessGatewayConfig,
        GovernedReadGateway,
        hashlib.sha256,
        timedelta,
    )


_FROZEN_MOCK_VALIDATE_PREPARATION = MockClientReadGateway._validate_preparation_graph


def build_runtime_read_gateway(
    *,
    authority_sha256: str,
    seed_urls: tuple[str, ...],
    allowed_domains: tuple[str, ...],
    user_agent: str,
    max_body_bytes: int,
    timeout_seconds: float,
    budget_limit: int,
) -> GovernedReadGateway:
    """Construct the runtime gateway only from already-validated authority."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be positive and finite")
    visible_identity = {
        "identity_id": "web-listening-runtime-v2",
        "product_token": "web-listening-bot",
        "user_agent": user_agent,
    }
    identity = DiagnosticIdentity(
        **visible_identity,
        identity_sha256=canonical_sha256(visible_identity),
    )
    origins = {normalize_http_url(url)[1] for url in seed_urls}
    origins.update(
        normalize_http_url(f"https://{domain}")[1] for domain in allowed_domains
    )
    gateway = AccessGateway(
        AccessGatewayConfig(
            identity=identity,
            allowed_origins=frozenset(origins),
            diagnostic_artifact_sha256=authority_sha256,
            budget_limit=budget_limit,
        ),
        transport=SafePinnedTransport(timeout=float(timeout_seconds)),
    )
    return GovernedReadGateway(gateway, max_body_bytes=max_body_bytes)


def access_rejection_payload(
    error: AccessRejectedError | AccessRejectionErrorEnvelope,
) -> dict[str, object]:
    """Return the exact frozen envelope used by every supported interface."""
    envelope = error.envelope if isinstance(error, AccessRejectedError) else error
    return envelope.model_dump(mode="json")


def _response_header(headers: Mapping[str, str], name: str) -> str:
    values = [
        str(value).strip()
        for key, value in headers.items()
        if str(key).casefold() == name.casefold()
    ]
    value = ", ".join(item for item in values if item)
    if len(value) > 4096 or any(
        ord(character) < 32 and character != "\t" for character in value
    ):
        raise ValueError("response header evidence is invalid")
    return value


def _attach_body_failure_context(
    failure: BodyFailure,
    *,
    decision: AccessDecision,
    final_url: str,
    status_code: int,
    redirect_hops: tuple[object, ...],
) -> None:
    failure.decision = decision
    failure.current_url = final_url
    failure.final_url = final_url
    failure.status_code = status_code
    failure.redirect_hops = redirect_hops


def _response_character_encoding(headers: Mapping[str, str]) -> str:
    content_type = _response_header(headers, "content-type")
    if not content_type:
        return "identity"
    message = Message()
    message["content-type"] = content_type
    charset = message.get_content_charset()
    if charset is None:
        return "identity"
    normalized = charset.casefold()
    if (
        not normalized
        or len(normalized) > 64
        or any(
            ord(character) < 0x21 or ord(character) > 0x7E for character in normalized
        )
    ):
        raise ValueError("response character encoding is invalid")
    return normalized


def _content_disposition_filename(headers: Mapping[str, str]) -> str | None:
    disposition = _response_header(headers, "content-disposition")
    if not disposition:
        return None
    message = Message()
    message["content-disposition"] = disposition
    filename = message.get_filename()
    if filename is None:
        return None
    if (
        not filename
        or len(filename) > 255
        or filename != filename.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        or "/" in filename
        or "\\" in filename
    ):
        raise ValueError("response filename evidence is invalid")
    return filename


def _governed_read_runtime_snapshot(
    reader: GovernedReadGateway,
) -> tuple[object, ...]:
    gateway = object.__getattribute__(reader, "_GovernedReadGateway__gateway")
    max_body_bytes = object.__getattribute__(
        reader, "_GovernedReadGateway__max_body_bytes"
    )
    runtime_lock = object.__getattribute__(reader, "_GovernedReadGateway__runtime_lock")
    runtime_dispatch = object.__getattribute__(
        reader, "_GovernedReadGateway__runtime_dispatch"
    )
    method_names = (
        "read",
        "_seal_runtime",
        "_validate_runtime",
        "gateway",
        "max_body_bytes",
        "user_agent",
    )
    helper_names = (
        "_FROZEN_ACCESS_REQUEST_WITH_CONTEXT",
        "read_bounded_body",
        "_attach_body_failure_context",
        "_content_disposition_filename",
        "_response_character_encoding",
        "_governed_read_runtime_snapshot",
    )
    return (
        id(gateway),
        type(gateway),
        max_body_bytes,
        id(runtime_lock),
        type(runtime_lock),
        id(runtime_dispatch),
        runtime_dispatch,
        id(_GOVERNED_READ_RUNTIME_DISPATCH),
        tuple(name for name in method_names if name in vars(reader)),
        tuple(id(getattr(GovernedReadGateway, name)) for name in method_names),
        tuple(id(globals()[name]) for name in helper_names),
    )


@dataclass(frozen=True, slots=True)
class _GovernedReadRuntimeDispatch:
    access_request_with_context: Callable[..., object]
    read_bounded_body: Callable[..., object]
    attach_body_failure_context: Callable[..., None]
    content_disposition_filename: Callable[[Mapping[str, str]], str | None]
    response_character_encoding: Callable[[Mapping[str, str]], str]
    runtime_snapshot: Callable[[GovernedReadGateway], tuple[object, ...]]
    validate_runtime: Callable[[GovernedReadGateway], None]


_GOVERNED_READ_RUNTIME_DISPATCH = _GovernedReadRuntimeDispatch(
    access_request_with_context=AccessGateway.request_with_context,
    read_bounded_body=read_bounded_body,
    attach_body_failure_context=_attach_body_failure_context,
    content_disposition_filename=_content_disposition_filename,
    response_character_encoding=_response_character_encoding,
    runtime_snapshot=_governed_read_runtime_snapshot,
    validate_runtime=GovernedReadGateway._validate_runtime,
)


__all__ = [
    "GOVERNED_READ_RUNTIME_ERRORS",
    "ROLLBACK_REQUIRED_READ_ERRORS",
    "AccessRejectedError",
    "GovernedReadGateway",
    "GovernedReadResult",
    "MockClientReadGateway",
    "access_rejection_payload",
    "build_runtime_read_gateway",
    "governed_read_failure_payload",
]
