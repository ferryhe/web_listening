from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    DiagnosticTransport,
    ParsedRobots,
    RawHttpResponse,
    SafePinnedTransport,
    SiteDiagnosticError,
    TransportFailure,
    build_origin_policy_evidence,
    decode_robots_utf8,
    header_value,
    is_public_address,
    looks_like_html,
    normalize_http_url,
    parse_content_type,
    parse_robots,
    read_bounded_body,
)
from web_listening.contracts.access_decision import (
    MAX_BUDGET_WINDOW_SECONDS,
    MAX_ORIGIN_BUDGET_LIMIT,
    MAX_PACING_INTERVAL_MS,
    MAX_RESERVATION_ORDINAL,
    AccessDecision,
    AccessPolicy,
    OriginPacingBudgetReservation,
    RedirectAccessProof,
    RedirectHop,
    RequestSlotReservation,
    RobotsObservation,
    access_policy_cache_key_sha256,
    build_access_decision,
    build_access_policy,
    build_redirect_access_proof,
    canonicalize_access_url,
    evaluate_access_policy,
    validate_access_identity,
)
from web_listening.contracts.site_diagnostic import (
    BODY_TLS_POLICY_OUTCOME,
    DiagnosticIdentity,
    NormalizedOrigin,
    OriginPolicyEvidence,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_RETRYABLE_TRANSPORT_KINDS = frozenset(
    {"connect", "connect_or_http", "dns", "network", "remote_disconnected", "timeout"}
)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class AccessGatewayError(RuntimeError):
    """Base class for fail-closed gateway enforcement failures."""

    def __init__(
        self,
        message: str,
        *,
        decision: AccessDecision | None = None,
        current_url: str | None = None,
        final_url: str | None = None,
        status_code: int | None = None,
        redirect_hops: tuple[RedirectHop, ...] = (),
    ) -> None:
        super().__init__(message)
        self.decision = decision
        self.current_url = current_url
        self.final_url = final_url
        self.status_code = status_code
        self.redirect_hops = redirect_hops

    def with_context(
        self,
        *,
        decision: AccessDecision | None = None,
        current_url: str | None = None,
        final_url: str | None = None,
        status_code: int | None = None,
        redirect_hops: tuple[RedirectHop, ...] = (),
    ) -> AccessGatewayError:
        if self.decision is None:
            self.decision = decision
        if self.current_url is None:
            self.current_url = current_url
        if self.final_url is None:
            self.final_url = final_url
        if self.status_code is None:
            self.status_code = status_code
        if not self.redirect_hops:
            self.redirect_hops = redirect_hops
        return self


class AccessGatewayOriginError(AccessGatewayError):
    """The requested exact origin is outside configured authority."""


class AccessGatewayRedirectError(AccessGatewayError):
    """A redirect violates the governed transition or hop boundary."""


class AccessGatewayBudgetError(AccessGatewayError):
    """The origin hard budget has no unit available to reserve."""


class AccessGatewayPolicyError(AccessGatewayError):
    """Fresh access-policy authority cannot cover the paced request."""


class AccessGatewayTransportError(AccessGatewayError):
    """A safe transport rejected or failed a target content request."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        decision: AccessDecision | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(
            message,
            decision=decision,
            current_url=getattr(decision, "canonical_url", None),
            final_url=getattr(decision, "canonical_url", None),
            redirect_hops=tuple(getattr(decision, "redirect_hops", ()) or ()),
        )
        self.kind = kind
        self.retryable = (
            kind in _DEFAULT_RETRYABLE_TRANSPORT_KINDS
            if retryable is None
            else retryable
        )


@dataclass(frozen=True)
class AccessGatewayConfig:
    identity: DiagnosticIdentity
    allowed_origins: frozenset[NormalizedOrigin]
    diagnostic_artifact_sha256: str
    policy_ttl: timedelta = timedelta(hours=1)
    pacing_interval: timedelta = timedelta(seconds=1)
    budget_window: timedelta = timedelta(hours=1)
    budget_limit: int = 100
    max_redirect_hops: int = 5
    max_robots_body_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_origins", frozenset(self.allowed_origins))
        if not self.allowed_origins:
            raise ValueError("at least one exact allowed origin is required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.diagnostic_artifact_sha256):
            raise ValueError("diagnostic artifact digest must be lowercase SHA-256")
        validate_access_identity(self.identity)
        if not timedelta(0) < self.policy_ttl <= timedelta(hours=24):
            raise ValueError("policy TTL must be between zero and 24 hours")
        _whole_milliseconds(
            self.pacing_interval,
            maximum=MAX_PACING_INTERVAL_MS,
            field_name="pacing interval",
        )
        _whole_seconds(
            self.budget_window,
            minimum=1,
            maximum=MAX_BUDGET_WINDOW_SECONDS,
            field_name="budget window",
        )
        if not 1 <= self.budget_limit <= MAX_ORIGIN_BUDGET_LIMIT:
            raise ValueError("budget limit is outside the frozen portable range")
        if not 0 <= self.max_redirect_hops <= MAX_RESERVATION_ORDINAL:
            raise ValueError("redirect hop limit is outside the frozen portable range")
        if self.max_robots_body_bytes < 1:
            raise ValueError("robots body limit must be positive")


@dataclass(frozen=True)
class AccessGatewayResponse:
    final_url: str
    status: int
    headers: Mapping[str, str]


@dataclass(frozen=True)
class AccessGatewayConsumerContext:
    """Gateway-owned context for the final non-redirect response."""

    final_url: str
    decision: AccessDecision
    status_code: int
    redirect_hops: tuple[RedirectHop, ...]


@dataclass(frozen=True)
class AccessGatewayResult[T]:
    decision: AccessDecision
    response: AccessGatewayResponse | None
    value: T | None


@dataclass
class _OriginState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    budget_window_started_at: datetime | None = None
    budget_used: int = 0
    last_request_started_at: datetime | None = None
    last_request_reserved_for: datetime | None = None
    next_start_ticket: int = 0
    serving_start_ticket: int = 0
    retired_start_tickets: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


@dataclass(frozen=True)
class _Authorization:
    decision: AccessDecision
    proof: RedirectAccessProof
    policy: AccessPolicy
    origin: NormalizedOrigin
    state: _OriginState
    reserved_at: datetime
    not_before: datetime
    budget_window_started_at: datetime
    budget_window_ends_at: datetime
    budget_slot_ordinal: int


@dataclass(frozen=True, slots=True)
class _TargetSendProgressCapability:
    """Exact bridge from a governed target-send lease into transport progress."""

    state: object
    state_type: type
    validate_state: Callable[[object], None]
    renew_state: Callable[[object], None]
    release_state: Callable[[object], None]

    def validate(self) -> None:
        if (
            type(self) is not _TargetSendProgressCapability
            or _TargetSendProgressCapability.validate
            is not _FROZEN_TARGET_SEND_PROGRESS_VALIDATE
            or _TargetSendProgressCapability.renew
            is not _FROZEN_TARGET_SEND_PROGRESS_RENEW
            or _TargetSendProgressCapability.__call__
            is not _FROZEN_TARGET_SEND_PROGRESS_RELEASE
            or type(self.state) is not self.state_type
            or getattr(self.state_type, "validate", None) is not self.validate_state
            or getattr(self.state_type, "renew", None) is not self.renew_state
            or vars(self.state_type).get("__call__") is not self.release_state
            or getattr(self.state, "_gateway_progress_capability", None) is not self
        ):
            raise TypeError("target-send progress capability changed")
        self.validate_state(self.state)

    def renew(self) -> None:
        _FROZEN_TARGET_SEND_PROGRESS_VALIDATE(self)
        self.renew_state(self.state)
        _FROZEN_TARGET_SEND_PROGRESS_VALIDATE(self)

    def __call__(self) -> None:
        _FROZEN_TARGET_SEND_PROGRESS_VALIDATE(self)
        self.release_state(self.state)
        _FROZEN_TARGET_SEND_PROGRESS_VALIDATE(self)


_FROZEN_TARGET_SEND_PROGRESS_VALIDATE = _TargetSendProgressCapability.validate
_FROZEN_TARGET_SEND_PROGRESS_RENEW = _TargetSendProgressCapability.renew
_FROZEN_TARGET_SEND_PROGRESS_RELEASE = _TargetSendProgressCapability.__call__


def _target_send_progress(
    release: Callable[[], None] | None,
    decision: AccessDecision,
) -> Callable[[], None] | None:
    if release is None:
        return None
    capability = getattr(release, "_gateway_progress_capability", None)
    if capability is not None:
        if (
            type(capability) is not _TargetSendProgressCapability
            or capability.state is not release
        ):
            raise AccessGatewayTransportError(
                "transport_integrity",
                "target-send progress capability is invalid",
                decision=decision,
            )
        try:
            _FROZEN_TARGET_SEND_PROGRESS_VALIDATE(capability)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise AccessGatewayTransportError(
                "transport_integrity",
                "target-send progress capability changed",
                decision=decision,
            ) from exc
        return lambda: _FROZEN_TARGET_SEND_PROGRESS_RENEW(capability)
    progress = getattr(release, "renew", None)
    if progress is not None and not callable(progress):
        raise AccessGatewayTransportError(
            "transport_integrity",
            "target-send progress guard is invalid",
            decision=decision,
        )
    return progress


def _whole_milliseconds(
    value: timedelta,
    *,
    maximum: int,
    field_name: str,
) -> int:
    milliseconds = value / timedelta(milliseconds=1)
    if not 0 <= milliseconds <= maximum or not milliseconds.is_integer():
        raise ValueError(f"{field_name} must be a whole portable millisecond value")
    return int(milliseconds)


def _whole_seconds(
    value: timedelta,
    *,
    minimum: int,
    maximum: int,
    field_name: str,
) -> int:
    seconds = value / timedelta(seconds=1)
    if not minimum <= seconds <= maximum or not seconds.is_integer():
        raise ValueError(f"{field_name} must be a whole portable second value")
    return int(seconds)


class AccessGateway:
    """Shared synchronous governed access core with no storage side effects."""

    def __init__(
        self,
        config: AccessGatewayConfig,
        *,
        transport: DiagnosticTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        object.__setattr__(self, "_AccessGateway__runtime_lock", threading.RLock())
        object.__setattr__(
            self,
            "_AccessGateway__runtime_dispatch",
            _GATEWAY_RUNTIME_DISPATCH,
        )
        self.config = config
        self.transport = transport or SafePinnedTransport()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or time.sleep
        self._origin_states = {
            origin: _OriginState() for origin in config.allowed_origins
        }
        self._cache_condition = threading.Condition()
        self._policy_cache: dict[str, AccessPolicy] = {}
        self._inflight_policy_keys: set[str] = set()
        self._cache_generations: dict[str, int] = {}
        self._runtime_seal: tuple[object, ...] | None = None

    @property
    def _runtime_lock(self) -> threading.RLock:
        return self.__runtime_lock

    @property
    def _runtime_dispatch(self) -> _GatewayRuntimeDispatch:
        return self.__runtime_dispatch

    @property
    def config(self) -> AccessGatewayConfig:
        with self.__runtime_lock:
            return self.__config

    @config.setter
    def config(self, value: AccessGatewayConfig) -> None:
        with self.__runtime_lock:
            self.__config = value

    @property
    def transport(self) -> DiagnosticTransport:
        with self.__runtime_lock:
            return self.__transport

    @transport.setter
    def transport(self, value: DiagnosticTransport) -> None:
        with self.__runtime_lock:
            self.__transport = value

    def _seal_runtime(self) -> tuple[object, ...]:
        with self.__runtime_lock:
            snapshot = self.__runtime_dispatch.runtime_snapshot(self)
            if self._runtime_seal is None:
                self._runtime_seal = snapshot
            elif self._runtime_seal != snapshot:
                raise AccessGatewayTransportError(
                    "transport_integrity", "governed gateway call graph changed"
                )
            return snapshot

    def _validate_runtime(self) -> None:
        with self.__runtime_lock:
            if (
                self._runtime_seal is not None
                and self._runtime_seal != self.__runtime_dispatch.runtime_snapshot(self)
            ):
                raise AccessGatewayTransportError(
                    "transport_integrity", "governed gateway call graph changed"
                )

    def invalidate(self, origin: NormalizedOrigin | None = None) -> None:
        """Explicitly invalidate one exact-origin policy or the complete cache."""
        if origin is not None:
            self._gate_origin(origin)
        origins = self.config.allowed_origins if origin is None else (origin,)
        with self._cache_condition:
            for item in origins:
                key = self._cache_key(item)
                self._policy_cache.pop(key, None)
                self._cache_generations[key] = self._cache_generations.get(key, 0) + 1
            self._cache_condition.notify_all()

    def request[T](
        self,
        url: str,
        *,
        consume: Callable[[RawHttpResponse], T],
    ) -> AccessGatewayResult[T]:
        """Authorize a request while preserving the raw-only consumer API."""
        return self.__runtime_dispatch.request_with_context(
            self,
            url,
            consume=lambda raw, _context: consume(raw),
        )

    def request_with_context[T](
        self,
        url: str,
        *,
        consume: Callable[[RawHttpResponse, AccessGatewayConsumerContext], T],
        before_target_request: Callable[
            [str, AccessDecision], Callable[[], None] | None
        ]
        | None = None,
    ) -> AccessGatewayResult[T]:
        """Authorize and perform one manually redirected content request chain."""
        dispatch = self.__runtime_dispatch
        with self.__runtime_lock:
            dispatch.validate_runtime(self)
            current_url, current_origin = dispatch.normalize_and_gate(self, url)
        redirect_hops: list[RedirectHop] = []
        causal_floor: datetime | None = None

        while True:
            dispatch.validate_runtime(self)
            try:
                policy = dispatch.policy_for(self, current_origin)
            except AccessGatewayError as exc:
                exc.with_context(
                    current_url=current_url,
                    final_url=current_url,
                    redirect_hops=tuple(redirect_hops),
                )
                raise
            outcome = dispatch.evaluate_access_policy(policy, current_url)[0]
            if outcome != "allow":
                decision_time = dispatch.fresh_policy_time(self, policy, causal_floor)
                decision = dispatch.build_access_decision(
                    policy=policy,
                    canonical_url=current_url,
                    decision_time=decision_time,
                    redirect_hops=redirect_hops,
                    request_slot_reservation=None,
                    origin_reservation=None,
                )
                return AccessGatewayResult(decision=decision, response=None, value=None)

            try:
                authorization = dispatch.authorize_request(
                    self,
                    policy=policy,
                    canonical_url=current_url,
                    redirect_hops=redirect_hops,
                    causal_floor=causal_floor,
                )
            except AccessGatewayError as exc:
                exc.with_context(
                    current_url=current_url,
                    final_url=current_url,
                    redirect_hops=tuple(redirect_hops),
                )
                raise
            release_target_send: Callable[[], None] | None = None
            primary_error: BaseException | None = None
            try:
                try:
                    if before_target_request is not None:
                        release_target_send = before_target_request(
                            current_url, authorization.decision
                        )
                    progress = dispatch.target_send_progress(
                        release_target_send, authorization.decision
                    )
                    request_started_at = dispatch.start_authorized_request(
                        self,
                        authorization,
                        progress=progress,
                        canonical_url=current_url,
                        redirect_hops=redirect_hops,
                    )
                    with self.__runtime_lock:
                        dispatch.validate_runtime(self)
                        transport = self.__transport
                        config = self.__config
                        transport_request = type(transport).request
                    raw = dispatch.request_transport(
                        transport,
                        current_url,
                        user_agent=config.identity.user_agent,
                        identity_sha256=config.identity.identity_sha256,
                        progress=progress,
                        transport_request=transport_request,
                        safe_request=dispatch.safe_pinned_request,
                        safe_addresses=dispatch.safe_pinned_addresses,
                    )
                    try:
                        dispatch.validate_runtime(self)
                    except BaseException:
                        dispatch.close_response(raw)
                        raise
                except asyncio.CancelledError:
                    raise
                except AccessGatewayError:
                    raise
                except TransportFailure as exc:
                    raise AccessGatewayTransportError(
                        exc.kind,
                        str(exc),
                        decision=authorization.decision,
                        retryable=exc.retryable,
                    ) from exc
                except TimeoutError as exc:
                    raise AccessGatewayTransportError(
                        "timeout",
                        str(exc),
                        decision=authorization.decision,
                        retryable=True,
                    ) from exc
                except (ConnectionError, OSError) as exc:
                    raise AccessGatewayTransportError(
                        "network",
                        str(exc),
                        decision=authorization.decision,
                        retryable=True,
                    ) from exc
                except Exception as exc:
                    raise AccessGatewayTransportError(
                        "unclassified_transport",
                        str(exc),
                        decision=authorization.decision,
                    ) from exc
                if raw.status in _REDIRECT_STATUSES:
                    try:
                        location = dispatch.header_value(raw.headers, "Location")
                        if not location:
                            raise AccessGatewayRedirectError(
                                "redirect response is missing Location",
                                decision=authorization.decision,
                                current_url=current_url,
                                final_url=current_url,
                                status_code=raw.status,
                                redirect_hops=tuple(redirect_hops),
                            )
                        try:
                            target_url = dispatch.canonicalize_access_url(
                                urljoin(current_url, location)
                            )
                        except (SiteDiagnosticError, ValueError) as exc:
                            raise AccessGatewayRedirectError(
                                "redirect target is not a canonical access URL",
                                decision=authorization.decision,
                                current_url=current_url,
                                final_url=current_url,
                                status_code=raw.status,
                                redirect_hops=tuple(redirect_hops),
                            ) from exc
                        target_url, target_origin = dispatch.normalize_http_url(
                            target_url
                        )
                        if (
                            current_origin.scheme == "https"
                            and target_origin.scheme == "http"
                        ):
                            raise AccessGatewayRedirectError(
                                "HTTPS redirect downgrade is forbidden",
                                decision=authorization.decision,
                                current_url=current_url,
                                final_url=target_url,
                                status_code=raw.status,
                                redirect_hops=tuple(redirect_hops),
                            )
                        try:
                            dispatch.gate_origin(self, target_origin)
                        except AccessGatewayOriginError as exc:
                            exc.with_context(
                                decision=authorization.decision,
                                current_url=current_url,
                                final_url=target_url,
                                status_code=raw.status,
                                redirect_hops=tuple(redirect_hops),
                            )
                            raise
                        if len(redirect_hops) >= self.config.max_redirect_hops:
                            raise AccessGatewayRedirectError(
                                "exact redirect hop limit exhausted",
                                decision=authorization.decision,
                                current_url=current_url,
                                final_url=target_url,
                                status_code=raw.status,
                                redirect_hops=tuple(redirect_hops),
                            )
                        observed_at = dispatch.causal_now(self, request_started_at)
                        redirect_hops.append(
                            RedirectHop(
                                hop_ordinal=len(redirect_hops) + 1,
                                request_slot_ordinal=len(redirect_hops) + 1,
                                source_url=current_url,
                                source_origin=current_origin,
                                access_proof=authorization.proof,
                                request_started_at=request_started_at,
                                http_status=raw.status,
                                canonical_target_url=target_url,
                                target_origin=target_origin,
                                observed_at=observed_at,
                            )
                        )
                    finally:
                        dispatch.close_response(raw)
                    current_url = target_url
                    current_origin = target_origin
                    causal_floor = observed_at
                    continue

                try:
                    metadata = AccessGatewayResponse(
                        final_url=current_url,
                        status=raw.status,
                        headers=dict(raw.headers),
                    )
                    value = consume(
                        raw,
                        AccessGatewayConsumerContext(
                            final_url=current_url,
                            decision=authorization.decision,
                            status_code=raw.status,
                            redirect_hops=tuple(redirect_hops),
                        ),
                    )
                finally:
                    dispatch.close_response(raw)
                return AccessGatewayResult(
                    decision=authorization.decision,
                    response=metadata,
                    value=value,
                )
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if release_target_send is not None:
                    dispatch.release_target_send(release_target_send, primary_error)

    def _cache_key(self, origin: NormalizedOrigin) -> str:
        helper = (
            self.__runtime_dispatch.access_policy_cache_key
            if self._runtime_seal is not None
            else access_policy_cache_key_sha256
        )
        return helper(
            canonical_origin=origin,
            identity_sha256=self.config.identity.identity_sha256,
        )

    def _normalize_and_gate(self, value: str) -> tuple[str, NormalizedOrigin]:
        try:
            canonical = self.__runtime_dispatch.canonicalize_access_url(value)
            normalized, origin = self.__runtime_dispatch.normalize_http_url(canonical)
        except (SiteDiagnosticError, ValueError) as exc:
            raise AccessGatewayOriginError("invalid governed access URL") from exc
        self._gate_origin(origin)
        return normalized, origin

    def _gate_origin(self, origin: NormalizedOrigin) -> None:
        if origin not in self.config.allowed_origins:
            raise AccessGatewayOriginError(
                f"exact origin is outside gateway authority: {origin.as_url_origin()}"
            )
        try:
            ipaddress.ip_address(origin.host)
        except ValueError:
            return
        if not is_public_address(origin.host):
            raise AccessGatewayOriginError("non-public target address is forbidden")

    def _policy_for(self, origin: NormalizedOrigin) -> AccessPolicy:
        dispatch = self.__runtime_dispatch
        key = dispatch.cache_key(self, origin)
        while True:
            with self._cache_condition:
                now = self._clock()
                cached = self._policy_cache.get(key)
                if cached is not None and cached.observed_at <= now < cached.expires_at:
                    return cached
                self._policy_cache.pop(key, None)
                if key in self._inflight_policy_keys:
                    self._cache_condition.wait()
                    continue
                self._inflight_policy_keys.add(key)
                generation = self._cache_generations.get(key, 0)
                break

        try:
            policy = dispatch.fetch_policy(self, origin)
        except BaseException:
            with self._cache_condition:
                self._inflight_policy_keys.discard(key)
                self._cache_condition.notify_all()
            raise
        with self._cache_condition:
            if self._cache_generations.get(key, 0) == generation:
                self._policy_cache[key] = policy
            self._inflight_policy_keys.discard(key)
            self._cache_condition.notify_all()
        return policy

    def _fetch_policy(self, origin: NormalizedOrigin) -> AccessPolicy:
        dispatch = self.__runtime_dispatch
        robots_url = origin.as_url_origin() + "/robots.txt"
        with self.__runtime_lock:
            dispatch.validate_runtime(self)
            transport = self.__transport
            config = self.__config
            transport_request = type(transport).request
            sealed = self._runtime_seal is not None
        build_evidence = (
            dispatch.build_origin_policy_evidence
            if sealed
            else build_origin_policy_evidence
        )
        bounded_body = dispatch.read_bounded_body if sealed else read_bounded_body
        content_type = dispatch.parse_content_type if sealed else parse_content_type
        decode_robots = dispatch.decode_robots_utf8 if sealed else decode_robots_utf8
        html_probe = dispatch.looks_like_html if sealed else looks_like_html
        robots_parser = dispatch.parse_robots if sealed else parse_robots
        try:
            raw = dispatch.request_transport(
                transport,
                robots_url,
                user_agent=config.identity.user_agent,
                identity_sha256=config.identity.identity_sha256,
                progress=None,
                transport_request=transport_request,
                safe_request=dispatch.safe_pinned_request,
                safe_addresses=dispatch.safe_pinned_addresses,
            )
            try:
                dispatch.validate_runtime(self)
            except BaseException:
                dispatch.close_response(raw)
                raise
        except asyncio.CancelledError:
            raise
        except TransportFailure as exc:
            if exc.safety:
                raise AccessGatewayTransportError(
                    exc.kind, str(exc), retryable=exc.retryable
                ) from exc
            observation_kind = (
                "timeout"
                if exc.kind == "timeout"
                else "dns_error"
                if exc.kind == "dns"
                else "network_error"
            )
            return dispatch.build_policy(
                self,
                origin=origin,
                observation_kind=observation_kind,
                http_status=None,
                evidence=None,
            )
        except TimeoutError:
            return dispatch.build_policy(
                self,
                origin=origin,
                observation_kind="timeout",
                http_status=None,
                evidence=None,
            )
        except (ConnectionError, OSError):
            return dispatch.build_policy(
                self,
                origin=origin,
                observation_kind="network_error",
                http_status=None,
                evidence=None,
            )

        status = raw.status
        if status != 200:
            dispatch.close_response(raw)
            if status not in {401, 403, 404}:
                raise AccessGatewayTransportError(
                    "robots_http_status",
                    f"robots HTTP status is outside the frozen matrix: {status}",
                )
            observation_kind = {
                404: "http_404",
                401: "http_401",
                403: "http_403",
            }[status]
            if status == 404:
                observed_at = self._clock()
                expires_at = observed_at + self.config.policy_ttl
                evidence = build_evidence(
                    origin=origin,
                    robots=ParsedRobots(
                        config.identity.product_token,
                        [],
                        [],
                        [],
                        [],
                    ),
                    robots_sha256=_EMPTY_SHA256,
                    robots_status="absent",
                    identity=config.identity,
                    fetched_at=observed_at,
                    expires_at=expires_at,
                )
                return dispatch.build_policy(
                    self,
                    origin=origin,
                    observation_kind=observation_kind,
                    http_status=404,
                    evidence=evidence,
                    observed_at=observed_at,
                    expires_at=expires_at,
                )
            return dispatch.build_policy(
                self,
                origin=origin,
                observation_kind=observation_kind,
                http_status=status,
                evidence=None,
            )

        try:
            try:
                body = bounded_body(
                    raw,
                    url=robots_url,
                    wire_limit=config.max_robots_body_bytes,
                    decoded_limit=config.max_robots_body_bytes,
                    aggregate_wire_remaining=config.max_robots_body_bytes,
                    aggregate_decoded_remaining=config.max_robots_body_bytes,
                ).body
            except BodyFailure as exc:
                if exc.reason == BODY_TLS_POLICY_OUTCOME:
                    raise AccessGatewayTransportError(
                        "tls_policy",
                        "robots response body failed TLS safety validation",
                    ) from exc
                cause = exc.__cause__
                observation_kind = (
                    "timeout"
                    if isinstance(cause, TimeoutError)
                    else "network_error"
                    if exc.retryable
                    else "parse_error"
                )
                return dispatch.build_policy(
                    self,
                    origin=origin,
                    observation_kind=observation_kind,
                    http_status=None,
                    evidence=None,
                )

            media, parameters = content_type(
                dispatch.header_value(raw.headers, "Content-Type")
            )
            try:
                if (
                    "parse_error" in parameters
                    or media != "text/plain"
                    or parameters.get("charset", "utf-8") not in {"utf-8", "utf8"}
                ):
                    raise ValueError("unsupported robots MIME or charset")
                text = decode_robots(body)
                if html_probe(text):
                    raise ValueError("robots response looks like markup")
                parsed = robots_parser(
                    text,
                    product_token=config.identity.product_token,
                )
                if parsed.errors:
                    raise ValueError("robots parser rejected directives")
            except (UnicodeError, SiteDiagnosticError, ValueError):
                return dispatch.build_policy(
                    self,
                    origin=origin,
                    observation_kind="parse_error",
                    http_status=None,
                    evidence=None,
                )

            observed_at = self._clock()
            expires_at = observed_at + self.config.policy_ttl
            try:
                evidence = build_evidence(
                    origin=origin,
                    robots=parsed,
                    robots_sha256=hashlib.sha256(body).hexdigest(),
                    robots_status="available",
                    identity=config.identity,
                    fetched_at=observed_at,
                    expires_at=expires_at,
                )
                return dispatch.build_policy(
                    self,
                    origin=origin,
                    observation_kind="valid_200",
                    http_status=200,
                    evidence=evidence,
                    observed_at=observed_at,
                    expires_at=expires_at,
                )
            except (TypeError, ValueError):
                return dispatch.build_policy(
                    self,
                    origin=origin,
                    observation_kind="parse_error",
                    http_status=None,
                    evidence=None,
                )
        finally:
            dispatch.close_response(raw)

    def _build_policy(
        self,
        *,
        origin: NormalizedOrigin,
        observation_kind: str,
        http_status: int | None,
        evidence: OriginPolicyEvidence | None,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> AccessPolicy:
        observed_at = observed_at or self._clock()
        expires_at = expires_at or observed_at + self.config.policy_ttl
        helper = (
            self.__runtime_dispatch.build_access_policy
            if self._runtime_seal is not None
            else build_access_policy
        )
        return helper(
            canonical_origin=origin,
            identity=self.config.identity,
            diagnostic_artifact_sha256=self.config.diagnostic_artifact_sha256,
            robots_observation=RobotsObservation(
                kind=observation_kind,
                http_status=http_status,
            ),
            origin_policy_evidence=evidence,
            observed_at=observed_at,
            expires_at=expires_at,
        )

    def _authorize_request(
        self,
        *,
        policy: AccessPolicy,
        canonical_url: str,
        redirect_hops: list[RedirectHop],
        causal_floor: datetime | None,
    ) -> _Authorization:
        dispatch = self.__runtime_dispatch
        origin = policy.canonical_origin
        state = self._origin_states[origin]
        pacing_ms = _whole_milliseconds(
            self.config.pacing_interval,
            maximum=MAX_PACING_INTERVAL_MS,
            field_name="pacing interval",
        )
        window_seconds = _whole_seconds(
            self.config.budget_window,
            minimum=1,
            maximum=MAX_BUDGET_WINDOW_SECONDS,
            field_name="budget window",
        )

        with state.lock:
            reserved_at = dispatch.fresh_policy_time(self, policy, causal_floor)
            if (
                state.budget_window_started_at is None
                or reserved_at
                >= state.budget_window_started_at + self.config.budget_window
            ):
                state.budget_window_started_at = reserved_at
                state.budget_used = 0
            assert state.budget_window_started_at is not None
            if state.budget_used >= self.config.budget_limit:
                raise AccessGatewayBudgetError(
                    f"origin hard budget exhausted: {origin.as_url_origin()}"
                )
            not_before = reserved_at
            pacing_floor = (
                state.last_request_reserved_for or state.last_request_started_at
            )
            if pacing_floor is not None:
                not_before = max(
                    not_before,
                    pacing_floor + self.config.pacing_interval,
                )
            if not_before > policy.expires_at:
                raise AccessGatewayPolicyError(
                    "fresh policy authority expires before the paced request slot"
                )
            budget_window_ends_at = (
                state.budget_window_started_at + self.config.budget_window
            )
            if not state.budget_window_started_at <= not_before < budget_window_ends_at:
                raise AccessGatewayBudgetError(
                    "paced request slot is outside the active budget window"
                )

            request_reservation = RequestSlotReservation(
                status="reserved",
                request_slot_ordinal=len(redirect_hops) + 1,
                reserved_at=reserved_at,
            )
            origin_reservation = OriginPacingBudgetReservation(
                status="reserved",
                origin=origin,
                reserved_at=reserved_at,
                not_before=not_before,
                pacing_interval_ms=pacing_ms,
                budget_window_started_at=state.budget_window_started_at,
                budget_window_seconds=window_seconds,
                budget_limit=self.config.budget_limit,
                budget_used_before_reservation=state.budget_used,
                budget_units_reserved=1,
                budget_slot_ordinal=state.budget_used + 1,
            )
            decision = dispatch.build_access_decision(
                policy=policy,
                canonical_url=canonical_url,
                decision_time=reserved_at,
                redirect_hops=redirect_hops,
                request_slot_reservation=request_reservation,
                origin_reservation=origin_reservation,
            )
            proof = dispatch.build_redirect_access_proof(
                policy=policy,
                canonical_url=canonical_url,
                decision_time=reserved_at,
                request_slot_reservation=request_reservation,
                origin_reservation=origin_reservation,
            )
            state.budget_used += 1
            state.last_request_reserved_for = not_before
            return _Authorization(
                decision=decision,
                proof=proof,
                policy=policy,
                origin=origin,
                state=state,
                reserved_at=reserved_at,
                not_before=not_before,
                budget_window_started_at=state.budget_window_started_at,
                budget_window_ends_at=budget_window_ends_at,
                budget_slot_ordinal=origin_reservation.budget_slot_ordinal,
            )

    def _start_authorized_request(
        self,
        authorization: _Authorization,
        *,
        progress: Callable[[], None] | None,
        canonical_url: str,
        redirect_hops: list[RedirectHop],
    ) -> datetime:
        state = authorization.state
        with state.condition:
            start_ticket = state.next_start_ticket
            state.next_start_ticket += 1
        try:
            with state.condition:
                while start_ticket != state.serving_start_ticket:
                    state.condition.wait()
                not_before = authorization.not_before
                if state.last_request_started_at is not None:
                    not_before = max(
                        not_before,
                        state.last_request_started_at + self.config.pacing_interval,
                    )
            delay = max(0.0, (not_before - self._clock()).total_seconds())
            if delay:
                self._sleep(delay)
            if progress is not None:
                progress()
            with state.condition:
                if start_ticket != state.serving_start_ticket:
                    raise AccessGatewayPolicyError(
                        "reserved request start ticket is no longer active",
                        decision=authorization.decision,
                        current_url=canonical_url,
                        final_url=canonical_url,
                        redirect_hops=tuple(redirect_hops),
                    )
                request_started_at = self.__runtime_dispatch.causal_now(
                    self,
                    max(authorization.reserved_at, not_before),
                )
                if (
                    authorization.policy.canonical_origin != authorization.origin
                    or state.budget_window_started_at
                    != authorization.budget_window_started_at
                    or state.budget_used < authorization.budget_slot_ordinal
                    or request_started_at < authorization.not_before
                    or request_started_at > authorization.policy.expires_at
                ):
                    raise AccessGatewayPolicyError(
                        "reserved request authority is no longer fresh",
                        decision=authorization.decision,
                        current_url=canonical_url,
                        final_url=canonical_url,
                        redirect_hops=tuple(redirect_hops),
                    )
                if not (
                    authorization.budget_window_started_at
                    <= request_started_at
                    < authorization.budget_window_ends_at
                ):
                    raise AccessGatewayBudgetError(
                        "actual request start is outside its reserved budget window",
                        decision=authorization.decision,
                        current_url=canonical_url,
                        final_url=canonical_url,
                        redirect_hops=tuple(redirect_hops),
                    )
                state.last_request_started_at = request_started_at
                state.serving_start_ticket += 1
                while state.serving_start_ticket in state.retired_start_tickets:
                    state.retired_start_tickets.remove(state.serving_start_ticket)
                    state.serving_start_ticket += 1
                state.condition.notify_all()
        except BaseException:
            self._retire_authorized_request(state, start_ticket)
            raise
        if progress is not None:
            progress()
        return request_started_at

    def _retire_authorized_request(
        self, state: _OriginState, start_ticket: int
    ) -> None:
        with state.condition:
            if start_ticket < state.serving_start_ticket:
                return
            state.retired_start_tickets.add(start_ticket)
            while state.serving_start_ticket in state.retired_start_tickets:
                state.retired_start_tickets.remove(state.serving_start_ticket)
                state.serving_start_ticket += 1
            state.condition.notify_all()

    def _causal_now(self, after: datetime | None) -> datetime:
        value = self._clock()
        if after is not None and value <= after:
            return after + timedelta(microseconds=1)
        return value

    def _fresh_policy_time(
        self,
        policy: AccessPolicy,
        after: datetime | None,
    ) -> datetime:
        value = self._causal_now(after)
        if not policy.observed_at <= value <= policy.expires_at:
            raise AccessGatewayPolicyError(
                "fresh policy authority does not cover decision time"
            )
        return value


_FROZEN_ACCESS_GATEWAY_REQUEST_WITH_CONTEXT = AccessGateway.request_with_context
_FROZEN_ACCESS_CACHE_KEY = AccessGateway._cache_key
_FROZEN_ACCESS_NORMALIZE_AND_GATE = AccessGateway._normalize_and_gate
_FROZEN_ACCESS_GATE_ORIGIN = AccessGateway._gate_origin
_FROZEN_ACCESS_POLICY_FOR = AccessGateway._policy_for
_FROZEN_ACCESS_FETCH_POLICY = AccessGateway._fetch_policy
_FROZEN_ACCESS_AUTHORIZE_REQUEST = AccessGateway._authorize_request
_FROZEN_ACCESS_START_AUTHORIZED_REQUEST = AccessGateway._start_authorized_request
_FROZEN_ACCESS_RETIRE_AUTHORIZED_REQUEST = AccessGateway._retire_authorized_request
_FROZEN_ACCESS_CAUSAL_NOW = AccessGateway._causal_now
_FROZEN_ACCESS_FRESH_POLICY_TIME = AccessGateway._fresh_policy_time
_FROZEN_ACCESS_SEAL_RUNTIME = AccessGateway._seal_runtime
_FROZEN_ACCESS_VALIDATE_RUNTIME = AccessGateway._validate_runtime
_FROZEN_SAFE_PINNED_REQUEST = SafePinnedTransport.request
_FROZEN_SAFE_PINNED_ADDRESSES = SafePinnedTransport._addresses
_FROZEN_SAFE_PINNED_SEAL_RUNTIME = SafePinnedTransport._seal_runtime
_FROZEN_SAFE_PINNED_VALIDATE_RUNTIME = SafePinnedTransport._validate_runtime


def _request_transport(
    transport: DiagnosticTransport,
    url: str,
    *,
    user_agent: str,
    identity_sha256: str,
    progress: Callable[[], None] | None,
    transport_request: Callable[..., RawHttpResponse],
    safe_request: Callable[..., RawHttpResponse],
    safe_addresses: Callable[..., list[str]],
) -> RawHttpResponse:
    if type(transport) is SafePinnedTransport:
        if (
            "request" in vars(transport)
            or "_addresses" in vars(transport)
            or SafePinnedTransport.request is not safe_request
            or SafePinnedTransport._addresses is not safe_addresses
        ):
            raise AccessGatewayTransportError(
                "transport_integrity", "pinned transport callable changed"
            )
        return safe_request(
            transport,
            url,
            user_agent=user_agent,
            identity_sha256=identity_sha256,
            progress=progress,
        )
    if progress is not None:
        return transport_request(
            transport,
            url,
            user_agent=user_agent,
            identity_sha256=identity_sha256,
            progress=progress,
        )
    return transport_request(
        transport,
        url,
        user_agent=user_agent,
        identity_sha256=identity_sha256,
    )


_FROZEN_REQUEST_TRANSPORT = _request_transport


def _gateway_runtime_snapshot(gateway: AccessGateway) -> tuple[object, ...]:
    config = object.__getattribute__(gateway, "_AccessGateway__config")
    transport = object.__getattribute__(gateway, "_AccessGateway__transport")
    runtime_lock = object.__getattribute__(gateway, "_AccessGateway__runtime_lock")
    runtime_dispatch = object.__getattribute__(
        gateway, "_AccessGateway__runtime_dispatch"
    )
    transport_type = type(transport)
    transport_request = getattr(transport_type, "request", None)
    validate_transport = getattr(transport_type, "_validate_identity", None)
    safe_runtime_seal = None
    if type(transport) is SafePinnedTransport:
        try:
            safe_runtime_seal = _FROZEN_SAFE_PINNED_SEAL_RUNTIME(transport)
        except TransportFailure as exc:
            raise AccessGatewayTransportError(
                "transport_integrity", "pinned transport capability changed"
            ) from exc
    if validate_transport is not None:
        try:
            validate_transport(transport)
        except (TypeError, ValueError) as exc:
            raise AccessGatewayTransportError(
                "transport_integrity", "governed transport capability changed"
            ) from exc
    method_names = (
        "request",
        "request_with_context",
        "_cache_key",
        "_normalize_and_gate",
        "_gate_origin",
        "_policy_for",
        "_fetch_policy",
        "_build_policy",
        "_authorize_request",
        "_start_authorized_request",
        "_retire_authorized_request",
        "_causal_now",
        "_fresh_policy_time",
        "_seal_runtime",
        "_validate_runtime",
        "config",
        "transport",
        "_runtime_lock",
        "_runtime_dispatch",
    )
    alias_names = (
        "_FROZEN_ACCESS_GATEWAY_REQUEST_WITH_CONTEXT",
        "_FROZEN_ACCESS_CACHE_KEY",
        "_FROZEN_ACCESS_NORMALIZE_AND_GATE",
        "_FROZEN_ACCESS_GATE_ORIGIN",
        "_FROZEN_ACCESS_POLICY_FOR",
        "_FROZEN_ACCESS_FETCH_POLICY",
        "_FROZEN_ACCESS_AUTHORIZE_REQUEST",
        "_FROZEN_ACCESS_START_AUTHORIZED_REQUEST",
        "_FROZEN_ACCESS_RETIRE_AUTHORIZED_REQUEST",
        "_FROZEN_ACCESS_CAUSAL_NOW",
        "_FROZEN_ACCESS_FRESH_POLICY_TIME",
        "_FROZEN_ACCESS_SEAL_RUNTIME",
        "_FROZEN_ACCESS_VALIDATE_RUNTIME",
        "_FROZEN_SAFE_PINNED_REQUEST",
        "_FROZEN_SAFE_PINNED_ADDRESSES",
        "_FROZEN_SAFE_PINNED_SEAL_RUNTIME",
        "_FROZEN_SAFE_PINNED_VALIDATE_RUNTIME",
        "_FROZEN_REQUEST_TRANSPORT",
        "_FROZEN_TARGET_SEND_PROGRESS_VALIDATE",
        "_FROZEN_TARGET_SEND_PROGRESS_RENEW",
        "_FROZEN_TARGET_SEND_PROGRESS_RELEASE",
    )
    return (
        id(config),
        config,
        id(transport),
        transport_type,
        id(transport_request),
        id(getattr(transport_request, "__code__", None)),
        id(validate_transport),
        id(getattr(validate_transport, "__code__", None)),
        tuple(
            name
            for name in ("request", "_validate_identity")
            if name in vars(transport)
        ),
        tuple(name for name in method_names if name in vars(gateway)),
        tuple(id(getattr(AccessGateway, name)) for name in method_names),
        tuple(id(globals()[name]) for name in alias_names),
        id(runtime_lock),
        id(runtime_dispatch),
        runtime_dispatch,
        id(_GATEWAY_RUNTIME_DISPATCH),
        id(runtime_dispatch.target_send_progress),
        id(runtime_dispatch.release_target_send),
        id(_request_transport),
        id(_gateway_runtime_snapshot),
        id(_target_send_progress),
        id(_release_target_send),
        safe_runtime_seal,
        id(evaluate_access_policy),
        id(build_access_decision),
        id(build_redirect_access_proof),
        id(canonicalize_access_url),
        id(normalize_http_url),
        id(header_value),
        id(_close_response),
        id(access_policy_cache_key_sha256),
        id(build_access_policy),
        id(build_origin_policy_evidence),
        id(read_bounded_body),
        id(parse_content_type),
        id(decode_robots_utf8),
        id(looks_like_html),
        id(parse_robots),
        (
            transport.timeout,
            transport.chunk_size,
        )
        if type(transport) is SafePinnedTransport
        else None,
    )


def _release_target_send(
    release: Callable[[], None], primary_error: BaseException | None
) -> None:
    try:
        capability = getattr(release, "_gateway_progress_capability", None)
        if capability is None:
            release()
        elif (
            type(capability) is not _TargetSendProgressCapability
            or capability.state is not release
        ):
            raise TypeError("target-send release capability is invalid")
        else:
            _FROZEN_TARGET_SEND_PROGRESS_RELEASE(capability)
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        cleanup_error.__context__ = primary_error.__context__
        primary_error.__context__ = cleanup_error
        primary_error.add_note(
            "target-send release failed after the primary failure "
            f"({type(cleanup_error).__name__})"
        )


def _close_response(response: RawHttpResponse) -> None:
    try:
        response.close()
    except (OSError, RuntimeError, TypeError, ValueError):
        pass


@dataclass(frozen=True, slots=True)
class _GatewayRuntimeDispatch:
    request_with_context: Callable[..., object]
    cache_key: Callable[..., object]
    normalize_and_gate: Callable[..., object]
    gate_origin: Callable[..., object]
    policy_for: Callable[..., object]
    fetch_policy: Callable[..., object]
    build_policy: Callable[..., object]
    authorize_request: Callable[..., object]
    start_authorized_request: Callable[..., datetime]
    retire_authorized_request: Callable[..., None]
    causal_now: Callable[..., object]
    fresh_policy_time: Callable[..., object]
    seal_runtime: Callable[..., object]
    validate_runtime: Callable[..., object]
    request_transport: Callable[..., RawHttpResponse]
    runtime_snapshot: Callable[[AccessGateway], tuple[object, ...]]
    target_send_progress: Callable[..., Callable[[], None] | None]
    release_target_send: Callable[[Callable[[], None], BaseException | None], None]
    close_response: Callable[[RawHttpResponse], None]
    safe_pinned_request: Callable[..., RawHttpResponse]
    safe_pinned_addresses: Callable[..., list[str]]
    evaluate_access_policy: Callable[..., object]
    build_access_decision: Callable[..., object]
    build_redirect_access_proof: Callable[..., object]
    canonicalize_access_url: Callable[[str], str]
    normalize_http_url: Callable[..., object]
    header_value: Callable[..., object]
    access_policy_cache_key: Callable[..., str]
    build_access_policy: Callable[..., AccessPolicy]
    build_origin_policy_evidence: Callable[..., OriginPolicyEvidence]
    read_bounded_body: Callable[..., object]
    parse_content_type: Callable[..., object]
    decode_robots_utf8: Callable[[bytes], str]
    looks_like_html: Callable[[str], bool]
    parse_robots: Callable[..., ParsedRobots]


_GATEWAY_RUNTIME_DISPATCH = _GatewayRuntimeDispatch(
    request_with_context=AccessGateway.request_with_context,
    cache_key=AccessGateway._cache_key,
    normalize_and_gate=AccessGateway._normalize_and_gate,
    gate_origin=AccessGateway._gate_origin,
    policy_for=AccessGateway._policy_for,
    fetch_policy=AccessGateway._fetch_policy,
    build_policy=AccessGateway._build_policy,
    authorize_request=AccessGateway._authorize_request,
    start_authorized_request=AccessGateway._start_authorized_request,
    retire_authorized_request=AccessGateway._retire_authorized_request,
    causal_now=AccessGateway._causal_now,
    fresh_policy_time=AccessGateway._fresh_policy_time,
    seal_runtime=AccessGateway._seal_runtime,
    validate_runtime=AccessGateway._validate_runtime,
    request_transport=_request_transport,
    runtime_snapshot=_gateway_runtime_snapshot,
    target_send_progress=_target_send_progress,
    release_target_send=_release_target_send,
    close_response=_close_response,
    safe_pinned_request=SafePinnedTransport.request,
    safe_pinned_addresses=SafePinnedTransport._addresses,
    evaluate_access_policy=evaluate_access_policy,
    build_access_decision=build_access_decision,
    build_redirect_access_proof=build_redirect_access_proof,
    canonicalize_access_url=canonicalize_access_url,
    normalize_http_url=normalize_http_url,
    header_value=header_value,
    access_policy_cache_key=access_policy_cache_key_sha256,
    build_access_policy=build_access_policy,
    build_origin_policy_evidence=build_origin_policy_evidence,
    read_bounded_body=read_bounded_body,
    parse_content_type=parse_content_type,
    decode_robots_utf8=decode_robots_utf8,
    looks_like_html=looks_like_html,
    parse_robots=parse_robots,
)


__all__ = [
    "AccessGateway",
    "AccessGatewayBudgetError",
    "AccessGatewayConfig",
    "AccessGatewayConsumerContext",
    "AccessGatewayError",
    "AccessGatewayOriginError",
    "AccessGatewayPolicyError",
    "AccessGatewayRedirectError",
    "AccessGatewayResponse",
    "AccessGatewayResult",
    "AccessGatewayTransportError",
]
