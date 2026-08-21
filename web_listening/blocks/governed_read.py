"""Single target-content read facade over the frozen governed AccessGateway."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Mapping

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
ROLLBACK_REQUIRED_READ_ERRORS = (AccessRejectedError,) + GOVERNED_READ_RUNTIME_ERRORS


def governed_read_failure_payload(
    error: AccessGatewayError | BodyFailure,
) -> dict[str, object]:
    """Render a stable non-robots failure without changing the frozen #49 contract."""
    retryable = False
    if isinstance(error, AccessGatewayTransportError):
        error_code = f"gateway.transport.{error.kind}"
        retryable = error.kind in {"timeout", "network", "dns"}
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
        self.gateway = gateway
        self.max_body_bytes = max_body_bytes

    @property
    def user_agent(self) -> str:
        return self.gateway.config.identity.user_agent

    def read(self, url: str) -> GovernedReadResult:
        def consume(
            raw: RawHttpResponse,
            context: AccessGatewayConsumerContext,
        ) -> bytes:
            return read_bounded_body(
                raw,
                url=context.final_url,
                wire_limit=self.max_body_bytes,
                decoded_limit=self.max_body_bytes,
                aggregate_wire_remaining=self.max_body_bytes,
                aggregate_decoded_remaining=self.max_body_bytes,
            ).body

        result = self.gateway.request_with_context(url, consume=consume)
        if result.value is None or result.response is None:
            raise AccessRejectedError(result.decision)
        body = result.value
        return GovernedReadResult(
            body=body,
            final_url=result.response.final_url,
            status_code=result.response.status,
            headers=result.response.headers,
            sha256=hashlib.sha256(body).hexdigest(),
            access_decision=result.decision,
        )

    def close(self) -> None:
        return None


class _MockClientTransport:
    """Offline-test transport adapter; construction rejects every real client."""

    def __init__(self, client: Any) -> None:
        transport = getattr(client, "_transport", None)
        if (
            transport is None
            or transport.__class__.__module__ != "httpx"
            or transport.__class__.__name__ != "MockTransport"
        ):
            raise ValueError(
                "only httpx.MockTransport may be adapted for offline gateway tests"
            )
        self.client = client

    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
    ) -> RawHttpResponse:
        del identity_sha256
        if normalize_http_url(url)[0].endswith("/robots.txt"):
            return RawHttpResponse(status=404, headers={}, body_chunks=())
        request = self.client.build_request(
            "GET",
            url,
            headers={"User-Agent": user_agent},
        )
        response = self.client.send(request, follow_redirects=False)
        return RawHttpResponse(
            status=response.status_code,
            headers={
                str(key).casefold(): str(value)
                for key, value in response.headers.items()
            },
            body_chunks=(response.content,),
            close=response.close,
        )


class MockClientReadGateway:
    """Test-only lazy gateway factory for existing offline MockTransport fixtures."""

    def __init__(self, client: Any, *, user_agent: str, max_body_bytes: int) -> None:
        self._transport = _MockClientTransport(client)
        self._user_agent = user_agent
        self._max_body_bytes = max_body_bytes
        self._gateways: dict[str, GovernedReadGateway] = {}

    @property
    def gateway(self) -> AccessGateway:
        if len(self._gateways) != 1:
            raise RuntimeError(
                "test gateway identity is available after one exact origin is used"
            )
        return next(iter(self._gateways.values())).gateway

    @property
    def user_agent(self) -> str:
        return self._user_agent

    def read(self, url: str) -> GovernedReadResult:
        normalized_url, origin = normalize_http_url(url)
        key = origin.as_url_origin()
        gateway = self._gateways.get(key)
        if gateway is None:
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
                        allowed_origins=frozenset({origin}),
                        diagnostic_artifact_sha256=hashlib.sha256(
                            f"offline-test:{key}".encode()
                        ).hexdigest(),
                        pacing_interval=timedelta(0),
                        budget_limit=10_000,
                    ),
                    transport=self._transport,
                ),
                max_body_bytes=self._max_body_bytes,
            )
            self._gateways[key] = gateway
        result = gateway.read(normalized_url)
        if result.final_url == normalized_url and normalized_url != url:
            return replace(result, final_url=url)
        return result

    def close(self) -> None:
        return None


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


__all__ = [
    "AccessRejectedError",
    "ROLLBACK_REQUIRED_READ_ERRORS",
    "GovernedReadGateway",
    "GovernedReadResult",
    "GOVERNED_READ_RUNTIME_ERRORS",
    "MockClientReadGateway",
    "access_rejection_payload",
    "build_runtime_read_gateway",
    "governed_read_failure_payload",
]
