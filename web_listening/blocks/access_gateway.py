from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Generic, Mapping, TypeVar
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


T = TypeVar("T")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class AccessGatewayError(RuntimeError):
    """Base class for fail-closed gateway enforcement failures."""


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
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.decision = decision


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


@dataclass(frozen=True)
class AccessGatewayResult(Generic[T]):
    decision: AccessDecision
    response: AccessGatewayResponse | None
    value: T | None


@dataclass
class _OriginState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    budget_window_started_at: datetime | None = None
    budget_used: int = 0
    last_request_started_at: datetime | None = None


@dataclass(frozen=True)
class _Authorization:
    decision: AccessDecision
    proof: RedirectAccessProof
    request_started_at: datetime
    release_origin_start: Callable[[], None]


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
        self.config = config
        self.transport = transport or SafePinnedTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._origin_states = {
            origin: _OriginState() for origin in config.allowed_origins
        }
        self._cache_condition = threading.Condition()
        self._policy_cache: dict[str, AccessPolicy] = {}
        self._inflight_policy_keys: set[str] = set()
        self._cache_generations: dict[str, int] = {}

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

    def request(
        self,
        url: str,
        *,
        consume: Callable[[RawHttpResponse], T],
    ) -> AccessGatewayResult[T]:
        """Authorize a request while preserving the raw-only consumer API."""
        return self.request_with_context(
            url,
            consume=lambda raw, _context: consume(raw),
        )

    def request_with_context(
        self,
        url: str,
        *,
        consume: Callable[[RawHttpResponse, AccessGatewayConsumerContext], T],
    ) -> AccessGatewayResult[T]:
        """Authorize and perform one manually redirected content request chain."""
        current_url, current_origin = self._normalize_and_gate(url)
        redirect_hops: list[RedirectHop] = []
        causal_floor: datetime | None = None

        while True:
            policy = self._policy_for(current_origin)
            outcome = evaluate_access_policy(policy, current_url)[0]
            if outcome != "allow":
                decision_time = self._fresh_policy_time(policy, causal_floor)
                decision = build_access_decision(
                    policy=policy,
                    canonical_url=current_url,
                    decision_time=decision_time,
                    redirect_hops=redirect_hops,
                    request_slot_reservation=None,
                    origin_reservation=None,
                )
                return AccessGatewayResult(decision=decision, response=None, value=None)

            authorization = self._authorize_request(
                policy=policy,
                canonical_url=current_url,
                redirect_hops=redirect_hops,
                causal_floor=causal_floor,
            )
            try:
                raw = self.transport.request(
                    current_url,
                    user_agent=self.config.identity.user_agent,
                    identity_sha256=self.config.identity.identity_sha256,
                )
            except asyncio.CancelledError:
                raise
            except TransportFailure as exc:
                raise AccessGatewayTransportError(
                    exc.kind,
                    str(exc),
                    decision=authorization.decision,
                ) from exc
            except TimeoutError as exc:
                raise AccessGatewayTransportError(
                    "timeout",
                    str(exc),
                    decision=authorization.decision,
                ) from exc
            except (ConnectionError, OSError) as exc:
                raise AccessGatewayTransportError(
                    "network",
                    str(exc),
                    decision=authorization.decision,
                ) from exc
            except Exception as exc:
                raise AccessGatewayTransportError(
                    "unclassified_transport",
                    str(exc),
                    decision=authorization.decision,
                ) from exc
            finally:
                authorization.release_origin_start()

            if raw.status in _REDIRECT_STATUSES:
                try:
                    location = header_value(raw.headers, "Location")
                    if not location:
                        raise AccessGatewayRedirectError(
                            "redirect response is missing Location"
                        )
                    try:
                        target_url = canonicalize_access_url(
                            urljoin(current_url, location)
                        )
                    except (SiteDiagnosticError, ValueError) as exc:
                        raise AccessGatewayRedirectError(
                            "redirect target is not a canonical access URL"
                        ) from exc
                    target_url, target_origin = normalize_http_url(target_url)
                    if (
                        current_origin.scheme == "https"
                        and target_origin.scheme == "http"
                    ):
                        raise AccessGatewayRedirectError(
                            "HTTPS redirect downgrade is forbidden"
                        )
                    self._gate_origin(target_origin)
                    if len(redirect_hops) >= self.config.max_redirect_hops:
                        raise AccessGatewayRedirectError(
                            "exact redirect hop limit exhausted"
                        )
                    observed_at = self._causal_now(authorization.request_started_at)
                    redirect_hops.append(
                        RedirectHop(
                            hop_ordinal=len(redirect_hops) + 1,
                            request_slot_ordinal=len(redirect_hops) + 1,
                            source_url=current_url,
                            source_origin=current_origin,
                            access_proof=authorization.proof,
                            request_started_at=authorization.request_started_at,
                            http_status=raw.status,
                            canonical_target_url=target_url,
                            target_origin=target_origin,
                            observed_at=observed_at,
                        )
                    )
                finally:
                    _close_response(raw)
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
                    AccessGatewayConsumerContext(final_url=current_url),
                )
            finally:
                _close_response(raw)
            return AccessGatewayResult(
                decision=authorization.decision,
                response=metadata,
                value=value,
            )

    def _cache_key(self, origin: NormalizedOrigin) -> str:
        return access_policy_cache_key_sha256(
            canonical_origin=origin,
            identity_sha256=self.config.identity.identity_sha256,
        )

    def _normalize_and_gate(self, value: str) -> tuple[str, NormalizedOrigin]:
        try:
            canonical = canonicalize_access_url(value)
            normalized, origin = normalize_http_url(canonical)
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
        key = self._cache_key(origin)
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
            policy = self._fetch_policy(origin)
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
        robots_url = origin.as_url_origin() + "/robots.txt"
        try:
            raw = self.transport.request(
                robots_url,
                user_agent=self.config.identity.user_agent,
                identity_sha256=self.config.identity.identity_sha256,
            )
        except asyncio.CancelledError:
            raise
        except TransportFailure as exc:
            if exc.safety:
                raise AccessGatewayTransportError(exc.kind, str(exc)) from exc
            observation_kind = (
                "timeout"
                if exc.kind == "timeout"
                else "dns_error"
                if exc.kind == "dns"
                else "network_error"
            )
            return self._build_policy(
                origin=origin,
                observation_kind=observation_kind,
                http_status=None,
                evidence=None,
            )
        except TimeoutError:
            return self._build_policy(
                origin=origin,
                observation_kind="timeout",
                http_status=None,
                evidence=None,
            )
        except (ConnectionError, OSError):
            return self._build_policy(
                origin=origin,
                observation_kind="network_error",
                http_status=None,
                evidence=None,
            )

        status = raw.status
        if status != 200:
            _close_response(raw)
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
                evidence = build_origin_policy_evidence(
                    origin=origin,
                    robots=ParsedRobots(
                        self.config.identity.product_token,
                        [],
                        [],
                        [],
                        [],
                    ),
                    robots_sha256=_EMPTY_SHA256,
                    robots_status="absent",
                    identity=self.config.identity,
                    fetched_at=observed_at,
                    expires_at=expires_at,
                )
                return self._build_policy(
                    origin=origin,
                    observation_kind=observation_kind,
                    http_status=404,
                    evidence=evidence,
                    observed_at=observed_at,
                    expires_at=expires_at,
                )
            return self._build_policy(
                origin=origin,
                observation_kind=observation_kind,
                http_status=status,
                evidence=None,
            )

        try:
            try:
                body = read_bounded_body(
                    raw,
                    url=robots_url,
                    wire_limit=self.config.max_robots_body_bytes,
                    decoded_limit=self.config.max_robots_body_bytes,
                    aggregate_wire_remaining=self.config.max_robots_body_bytes,
                    aggregate_decoded_remaining=self.config.max_robots_body_bytes,
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
                return self._build_policy(
                    origin=origin,
                    observation_kind=observation_kind,
                    http_status=None,
                    evidence=None,
                )

            media, parameters = parse_content_type(
                header_value(raw.headers, "Content-Type")
            )
            try:
                if (
                    "parse_error" in parameters
                    or media != "text/plain"
                    or parameters.get("charset", "utf-8") not in {"utf-8", "utf8"}
                ):
                    raise ValueError("unsupported robots MIME or charset")
                text = decode_robots_utf8(body)
                if looks_like_html(text):
                    raise ValueError("robots response looks like markup")
                parsed = parse_robots(
                    text,
                    product_token=self.config.identity.product_token,
                )
                if parsed.errors:
                    raise ValueError("robots parser rejected directives")
            except (UnicodeError, SiteDiagnosticError, ValueError):
                return self._build_policy(
                    origin=origin,
                    observation_kind="parse_error",
                    http_status=None,
                    evidence=None,
                )

            observed_at = self._clock()
            expires_at = observed_at + self.config.policy_ttl
            try:
                evidence = build_origin_policy_evidence(
                    origin=origin,
                    robots=parsed,
                    robots_sha256=hashlib.sha256(body).hexdigest(),
                    robots_status="available",
                    identity=self.config.identity,
                    fetched_at=observed_at,
                    expires_at=expires_at,
                )
                return self._build_policy(
                    origin=origin,
                    observation_kind="valid_200",
                    http_status=200,
                    evidence=evidence,
                    observed_at=observed_at,
                    expires_at=expires_at,
                )
            except (TypeError, ValueError):
                return self._build_policy(
                    origin=origin,
                    observation_kind="parse_error",
                    http_status=None,
                    evidence=None,
                )
        finally:
            _close_response(raw)

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
        return build_access_policy(
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

        state.lock.acquire()
        try:
            reserved_at = self._fresh_policy_time(policy, causal_floor)
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
            if state.last_request_started_at is not None:
                not_before = max(
                    not_before,
                    state.last_request_started_at + self.config.pacing_interval,
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
            decision = build_access_decision(
                policy=policy,
                canonical_url=canonical_url,
                decision_time=reserved_at,
                redirect_hops=redirect_hops,
                request_slot_reservation=request_reservation,
                origin_reservation=origin_reservation,
            )
            proof = build_redirect_access_proof(
                policy=policy,
                canonical_url=canonical_url,
                decision_time=reserved_at,
                request_slot_reservation=request_reservation,
                origin_reservation=origin_reservation,
            )
            state.budget_used += 1

            delay = max(0.0, (not_before - self._clock()).total_seconds())
            if delay:
                self._sleep(delay)
            request_started_at = self._causal_now(max(reserved_at, not_before))
            if request_started_at > policy.expires_at:
                raise AccessGatewayPolicyError(
                    "actual request start exceeds fresh policy authority"
                )
            if (
                not state.budget_window_started_at
                <= request_started_at
                < budget_window_ends_at
            ):
                raise AccessGatewayBudgetError(
                    "actual request start is outside its reserved budget window"
                )
            state.last_request_started_at = request_started_at
            return _Authorization(
                decision=decision,
                proof=proof,
                request_started_at=request_started_at,
                release_origin_start=state.lock.release,
            )
        except BaseException:
            state.lock.release()
            raise

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


def _close_response(response: RawHttpResponse) -> None:
    try:
        response.close()
    except Exception:
        pass


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
