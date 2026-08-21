from __future__ import annotations

import asyncio
import copy
import ssl
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator, Mapping

import pytest

from web_listening.blocks import access_gateway as access_gateway_module
from web_listening.blocks import site_diagnostic as site_diagnostic_module
from web_listening.blocks.access_gateway import (
    AccessGateway,
    AccessGatewayBudgetError,
    AccessGatewayConfig,
    AccessGatewayOriginError,
    AccessGatewayPolicyError,
    AccessGatewayRedirectError,
    AccessGatewayTransportError,
)
from web_listening.blocks.site_diagnostic import (
    BodyFailure,
    RawHttpResponse,
    SafePinnedTransport,
    TransportFailure,
    normalize_origin,
)
from web_listening.contracts.access_decision import (
    ACCESS_POLICY_VERSION,
    AccessDecision,
    access_policy_cache_key_sha256,
)
from web_listening.contracts.site_diagnostic import (
    DiagnosticIdentity,
    NormalizedOrigin,
    canonical_json,
    canonical_sha256,
)


ORIGIN = normalize_origin("https://example.com")
OTHER_ORIGIN = normalize_origin("https://other.example")
DIAGNOSTIC_SHA256 = "d" * 64
GATEWAY_DIAGNOSTIC_HELPERS = (
    "BodyFailure",
    "decode_robots_utf8",
    "header_value",
    "looks_like_html",
    "parse_content_type",
    "read_bounded_body",
)


def test_gateway_diagnostic_helpers_are_supported_public_exports() -> None:
    supported_exports = set(site_diagnostic_module.__all__)

    for name in GATEWAY_DIAGNOSTIC_HELPERS:
        assert name in supported_exports
        assert getattr(access_gateway_module, name) is getattr(
            site_diagnostic_module,
            name,
        )


def identity() -> DiagnosticIdentity:
    payload = {
        "identity_id": "gateway-default",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/1.4",
    }
    return DiagnosticIdentity(
        **payload,
        identity_sha256=canonical_sha256(payload),
    )


class ManualClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._value
            self._value += timedelta(microseconds=1)
            return value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@dataclass
class ResponseTracker:
    closed: int = 0


def response(
    status: int,
    body: bytes = b"",
    *,
    tracker: ResponseTracker | None = None,
    **headers: str,
) -> RawHttpResponse:
    tracker = tracker or ResponseTracker()

    def close() -> None:
        tracker.closed += 1

    return RawHttpResponse(
        status=status,
        headers={key.replace("_", "-"): value for key, value in headers.items()},
        body_chunks=(body,),
        close=close,
    )


Script = RawHttpResponse | BaseException | Callable[[], RawHttpResponse]


class ScriptedTransport:
    def __init__(self, scripts: dict[str, list[Script]]) -> None:
        self._scripts = {url: deque(items) for url, items in scripts.items()}
        self._lock = threading.Lock()
        self.requests: list[str] = []

    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
    ) -> RawHttpResponse:
        assert user_agent == identity().user_agent
        assert identity_sha256 == identity().identity_sha256
        with self._lock:
            self.requests.append(url)
            queue = self._scripts.get(url)
            if not queue:
                raise AssertionError(f"unexpected request: {url}")
            item = queue.popleft()
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item


def gateway(
    transport: ScriptedTransport,
    *,
    origins: tuple[NormalizedOrigin, ...] = (ORIGIN,),
    clock: ManualClock | None = None,
    policy_ttl: timedelta = timedelta(minutes=5),
    pacing_interval: timedelta = timedelta(0),
    budget_window: timedelta = timedelta(hours=1),
    budget_limit: int = 20,
    max_redirect_hops: int = 3,
    sleeper: Callable[[float], None] | None = None,
) -> AccessGateway:
    clock = clock or ManualClock()
    return AccessGateway(
        AccessGatewayConfig(
            identity=identity(),
            allowed_origins=frozenset(origins),
            diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
            policy_ttl=policy_ttl,
            pacing_interval=pacing_interval,
            budget_window=budget_window,
            budget_limit=budget_limit,
            max_redirect_hops=max_redirect_hops,
            max_robots_body_bytes=64 * 1024,
        ),
        transport=transport,
        clock=clock,
        sleeper=sleeper or clock.sleep,
    )


def read_body(raw: RawHttpResponse) -> bytes:
    return b"".join(raw.body_chunks)


@pytest.mark.parametrize(
    ("robots", "url", "expected"),
    [
        (
            response(
                200,
                b"User-agent: web-listening-bot\nAllow: /public\nDisallow: /private\n",
                Content_Type="text/plain; charset=utf-8",
            ),
            "https://example.com/public",
            ("allow", "robots.allowed"),
        ),
        (
            response(
                200,
                b"User-agent: web-listening-bot\nAllow: /public\nDisallow: /private\n",
                Content_Type="text/plain; charset=utf-8",
            ),
            "https://example.com/private/report",
            ("reject", "robots.disallowed"),
        ),
        (response(404), "https://example.com/public", ("allow", "robots.absent")),
        (
            response(401),
            "https://example.com/public",
            ("reject", "robots.auth_required"),
        ),
        (
            response(403),
            "https://example.com/public",
            ("reject", "robots.forbidden"),
        ),
        (
            TransportFailure("timeout", "timed out", retryable=True),
            "https://example.com/public",
            ("error", "robots.timeout"),
        ),
        (
            TransportFailure("dns", "lookup failed", retryable=True),
            "https://example.com/public",
            ("error", "robots.dns_error"),
        ),
        (
            TransportFailure("connect", "network failed", retryable=True),
            "https://example.com/public",
            ("error", "robots.network_error"),
        ),
        (
            response(200, b"\xff", Content_Type="text/plain"),
            "https://example.com/public",
            ("error", "robots.parse_error"),
        ),
    ],
)
def test_robots_matrix_denies_before_content_or_write(
    robots: RawHttpResponse | TransportFailure,
    url: str,
    expected: tuple[str, str],
    tmp_path: Path,
) -> None:
    scripts: dict[str, list[Script]] = {
        "https://example.com/robots.txt": [robots],
    }
    if expected[0] == "allow":
        scripts[url] = [response(200, b"content")]
    transport = ScriptedTransport(scripts)
    output = tmp_path / "must-not-exist"

    def write(raw: RawHttpResponse) -> bytes:
        body = read_body(raw)
        output.write_bytes(body)
        return body

    result = gateway(transport).request(url, consume=write)

    assert (result.decision.outcome, result.decision.reason_code) == expected
    if expected[0] == "allow":
        assert result.value == b"content"
        assert output.read_bytes() == b"content"
    else:
        assert result.value is None
        assert not output.exists()
        assert transport.requests == ["https://example.com/robots.txt"]


def test_gateway_owns_valid_robots_200_response_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robots_tracker = ResponseTracker()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [
                response(
                    200,
                    tracker=robots_tracker,
                    Content_Type="text/plain",
                )
            ],
            "https://example.com/public": [response(200, b"content")],
        }
    )
    monkeypatch.setattr(
        "web_listening.blocks.access_gateway.read_bounded_body",
        lambda *args, **kwargs: SimpleNamespace(
            body=b"User-agent: *\nAllow: /public\n"
        ),
    )

    result = gateway(transport).request(
        "https://example.com/public",
        consume=read_body,
    )

    assert result.decision.outcome == "allow"
    assert robots_tracker.closed == 1


def test_gateway_owns_robots_200_close_on_early_body_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    robots_tracker = ResponseTracker()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [
                response(
                    200,
                    tracker=robots_tracker,
                    Content_Encoding="unsupported",
                )
            ]
        }
    )

    def fail_before_body_iteration(*args: object, **kwargs: object) -> object:
        raise BodyFailure(
            "unsupported_or_multiple_content_encoding",
            wire=0,
            decoded=0,
        )

    monkeypatch.setattr(
        "web_listening.blocks.access_gateway.read_bounded_body",
        fail_before_body_iteration,
    )

    result = gateway(transport).request(
        "https://example.com/public",
        consume=read_body,
    )

    assert (result.decision.outcome, result.decision.reason_code) == (
        "error",
        "robots.parse_error",
    )
    assert robots_tracker.closed == 1


def test_robots_tls_body_safety_failure_is_typed_and_closes_response() -> None:
    robots_tracker = ResponseTracker()

    def tls_failure() -> Iterator[bytes]:
        yield b"U"
        raise ssl.SSLError("TLS body stream failed")

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [
                RawHttpResponse(
                    status=200,
                    headers={"Content-Type": "text/plain"},
                    body_chunks=tls_failure(),
                    close=lambda: setattr(
                        robots_tracker,
                        "closed",
                        robots_tracker.closed + 1,
                    ),
                )
            ]
        }
    )

    with pytest.raises(AccessGatewayTransportError) as caught:
        gateway(transport).request(
            "https://example.com/public",
            consume=read_body,
        )

    assert caught.value.kind == "tls_policy"
    assert caught.value.decision is None
    assert robots_tracker.closed >= 1
    assert transport.requests == ["https://example.com/robots.txt"]


@pytest.mark.parametrize("status", [418, 503])
def test_unsupported_robots_status_is_typed_outside_frozen_decision(
    status: int,
) -> None:
    robots_tracker = ResponseTracker()
    transport = ScriptedTransport(
        {"https://example.com/robots.txt": [response(status, tracker=robots_tracker)]}
    )

    with pytest.raises(AccessGatewayTransportError) as caught:
        gateway(transport).request(
            "https://example.com/public",
            consume=read_body,
        )

    assert caught.value.kind == "robots_http_status"
    assert caught.value.decision is None
    assert robots_tracker.closed == 1
    assert transport.requests == ["https://example.com/robots.txt"]


def test_access_contract_rejected_robots_evidence_becomes_parse_error() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [
                response(
                    200,
                    b"User-agent: *\nAllow: /\n"
                    b"Sitemap: https://example.com/?api_key=secret\n",
                    Content_Type="text/plain",
                )
            ]
        }
    )

    result = gateway(transport).request(
        "https://example.com/public",
        consume=read_body,
    )

    assert (result.decision.outcome, result.decision.reason_code) == (
        "error",
        "robots.parse_error",
    )
    assert transport.requests == ["https://example.com/robots.txt"]


def test_initial_disallowed_or_private_origin_fails_before_transport() -> None:
    transport = ScriptedTransport({})
    access = gateway(transport)

    with pytest.raises(AccessGatewayOriginError):
        access.request("https://other.example/data", consume=read_body)

    private_origin = normalize_origin("http://127.0.0.1")
    private_access = gateway(transport, origins=(private_origin,))
    with pytest.raises(AccessGatewayOriginError, match="non-public"):
        private_access.request("http://127.0.0.1/data", consume=read_body)

    assert transport.requests == []


def test_sensitive_identity_fails_config_preflight_before_transport() -> None:
    identity_payload = {
        "identity_id": "gateway-sensitive",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/1.4 Authorization: Bearer secret",
    }
    sensitive_identity = DiagnosticIdentity(
        **identity_payload,
        identity_sha256=canonical_sha256(identity_payload),
    )
    transport = ScriptedTransport({})

    with pytest.raises(ValueError, match="sensitive"):
        AccessGateway(
            AccessGatewayConfig(
                identity=sensitive_identity,
                allowed_origins=frozenset({ORIGIN}),
                diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
            ),
            transport=transport,
        )

    assert transport.requests == []


def test_config_snapshots_mutable_allowed_origins_before_gateway_use() -> None:
    mutable_origins = {ORIGIN}
    transport = ScriptedTransport({"https://other.example/robots.txt": [response(404)]})
    config = AccessGatewayConfig(
        identity=identity(),
        allowed_origins=mutable_origins,  # type: ignore[arg-type]
        diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
    )
    access = AccessGateway(config, transport=transport)

    mutable_origins.add(OTHER_ORIGIN)

    with pytest.raises(AccessGatewayOriginError):
        access.request("https://other.example/private", consume=read_body)

    assert config.allowed_origins == frozenset({ORIGIN})
    assert transport.requests == []


def test_policy_cache_key_hit_expiry_and_explicit_invalidation() -> None:
    clock = ManualClock()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [
                response(404),
                response(404),
                response(404),
            ],
            "https://example.com/a": [response(200), response(200), response(200)],
            "https://example.com/b": [response(200)],
        }
    )
    access = gateway(transport, clock=clock, policy_ttl=timedelta(seconds=10))

    first = access.request("https://example.com/a", consume=read_body)
    second = access.request("https://example.com/b", consume=read_body)
    assert first.decision.policy.policy_id == second.decision.policy.policy_id
    assert first.decision.policy.cache_key_sha256 == access_policy_cache_key_sha256(
        canonical_origin=ORIGIN,
        identity_sha256=identity().identity_sha256,
    )
    assert first.decision.policy.policy_version == ACCESS_POLICY_VERSION
    assert transport.requests.count("https://example.com/robots.txt") == 1

    clock.advance(11)
    access.request("https://example.com/a", consume=read_body)
    assert transport.requests.count("https://example.com/robots.txt") == 2

    access.invalidate(ORIGIN)
    access.request("https://example.com/a", consume=read_body)
    assert transport.requests.count("https://example.com/robots.txt") == 3


def test_reject_policy_expiry_between_cache_check_and_decision_is_typed() -> None:
    start = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    moments = iter(
        [
            start,
            start,
            start + timedelta(milliseconds=100),
            start + timedelta(milliseconds=900),
            start + timedelta(seconds=2),
        ]
    )
    transport = ScriptedTransport({"https://example.com/robots.txt": [response(401)]})
    access = AccessGateway(
        AccessGatewayConfig(
            identity=identity(),
            allowed_origins=frozenset({ORIGIN}),
            diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
            policy_ttl=timedelta(seconds=1),
            pacing_interval=timedelta(0),
        ),
        transport=transport,
        clock=lambda: next(moments),
        sleeper=lambda _: None,
    )

    first = access.request("https://example.com/private", consume=read_body)
    assert first.decision.reason_code == "robots.auth_required"

    with pytest.raises(AccessGatewayPolicyError, match="fresh"):
        access.request("https://example.com/private", consume=read_body)

    assert transport.requests == ["https://example.com/robots.txt"]


def test_policy_cache_is_single_flight_per_exact_key() -> None:
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def slow_robots() -> RawHttpResponse:
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return response(404)

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [slow_robots],
            "https://example.com/a": [response(200), response(200)],
        }
    )
    access = gateway(transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(access.request, "https://example.com/a", consume=read_body)
        assert fetch_started.wait(timeout=2)
        second = pool.submit(access.request, "https://example.com/a", consume=read_body)
        release_fetch.set()
        assert first.result(timeout=2).decision.outcome == "allow"
        assert second.result(timeout=2).decision.outcome == "allow"

    assert transport.requests.count("https://example.com/robots.txt") == 1


def test_inflight_invalidation_prevents_stale_cache_repopulation() -> None:
    fetch_started = threading.Event()
    release_fetch = threading.Event()

    def slow_robots() -> RawHttpResponse:
        fetch_started.set()
        assert release_fetch.wait(timeout=2)
        return response(404)

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [slow_robots, response(404)],
            "https://example.com/a": [response(200), response(200)],
        }
    )
    access = gateway(transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(access.request, "https://example.com/a", consume=read_body)
        assert fetch_started.wait(timeout=2)
        access.invalidate(ORIGIN)
        release_fetch.set()
        assert first.result(timeout=2).decision.outcome == "allow"

    access.request("https://example.com/a", consume=read_body)
    assert transport.requests.count("https://example.com/robots.txt") == 2


def test_each_redirect_target_revalidates_exact_origin_and_robots() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/start": [
                response(302, Location="https://other.example/final")
            ],
            "https://other.example/robots.txt": [
                response(
                    200,
                    b"User-agent: *\nAllow: /final\n",
                    Content_Type="text/plain",
                )
            ],
            "https://other.example/final": [response(200, b"done")],
        }
    )
    result = gateway(transport, origins=(ORIGIN, OTHER_ORIGIN)).request(
        "https://example.com/start",
        consume=read_body,
    )

    assert result.value == b"done"
    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/start",
        "https://other.example/robots.txt",
        "https://other.example/final",
    ]
    assert (
        result.decision.redirect_hops[0].access_proof.policy.canonical_origin == ORIGIN
    )
    assert result.decision.policy.canonical_origin == OTHER_ORIGIN


@pytest.mark.parametrize(
    ("target", "error_type"),
    [
        ("https://outside.example/final", AccessGatewayOriginError),
        ("http://example.com/final", AccessGatewayRedirectError),
    ],
)
def test_redirect_origin_expansion_and_https_downgrade_fail_before_target(
    target: str,
    error_type: type[Exception],
    tmp_path: Path,
) -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/start": [response(302, Location=target)],
        }
    )
    output = tmp_path / "no-write"

    with pytest.raises(error_type):
        gateway(transport).request(
            "https://example.com/start",
            consume=lambda raw: output.write_bytes(read_body(raw)),
        )

    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/start",
    ]
    assert not output.exists()


@pytest.mark.parametrize(
    "failure",
    [
        TransportFailure("dns_address_policy", "private or reserved", safety=True),
        TransportFailure("peer_mismatch", "peer changed", safety=True),
    ],
)
def test_rebinding_private_reserved_and_peer_failures_open_no_body_or_write(
    failure: TransportFailure,
    tmp_path: Path,
) -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/report": [failure],
        }
    )
    output = tmp_path / "no-write"

    with pytest.raises(AccessGatewayTransportError) as caught:
        gateway(transport).request(
            "https://example.com/report",
            consume=lambda raw: output.write_bytes(read_body(raw)),
        )

    assert caught.value.kind == failure.kind
    assert not output.exists()
    assert transport.requests == [
        "https://example.com/robots.txt",
        "https://example.com/report",
    ]


def test_safe_pinned_transport_timeout_maps_to_frozen_robots_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    access = AccessGateway(
        AccessGatewayConfig(
            identity=identity(),
            allowed_origins=frozenset({ORIGIN}),
            diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
            pacing_interval=timedelta(0),
        ),
        transport=SafePinnedTransport(),
        clock=ManualClock(),
    )

    result = access.request("https://example.com/report", consume=read_body)

    assert (result.decision.outcome, result.decision.reason_code) == (
        "error",
        "robots.timeout",
    )


def test_safe_pinned_pre_response_unicode_failure_closes_socket_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Socket:
        def settimeout(self, value: float) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            return "93.184.216.34", 80

        def close(self) -> None:
            events.append("socket_close")

    class Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.sock = None

        def putrequest(self, *args: object, **kwargs: object) -> None:
            pass

        def putheader(self, name: str, value: str) -> None:
            if name == "User-Agent":
                value.encode("latin-1")

        def close(self) -> None:
            events.append("connection_close")

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr("http.client.HTTPConnection", Connection)

    with pytest.raises(TransportFailure) as caught:
        SafePinnedTransport().request(
            "http://example.com/robots.txt",
            user_agent="web-listening-bot/1.4 \u0100",
            identity_sha256="a" * 64,
        )

    assert caught.value.kind == "unclassified_pre_response"
    assert caught.value.safety is True
    assert events == ["socket_close"]


def test_safe_pinned_header_cancellation_closes_before_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Socket:
        def settimeout(self, value: float) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            return "93.184.216.34", 80

        def close(self) -> None:
            events.append("socket_close")

    class Response:
        status = 200

        def getheaders(self) -> list[tuple[str, str]]:
            raise asyncio.CancelledError

        def close(self) -> None:
            events.append("response_close")

    class Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.sock: Socket | None = None

        def putrequest(self, *args: object, **kwargs: object) -> None:
            pass

        def putheader(self, *args: object, **kwargs: object) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            events.append("connection_close")
            assert self.sock is not None
            self.sock.close()

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr("http.client.HTTPConnection", Connection)

    with pytest.raises(asyncio.CancelledError):
        SafePinnedTransport().request(
            "http://example.com/robots.txt",
            user_agent="bot",
            identity_sha256="a" * 64,
        )

    assert events == ["response_close", "connection_close", "socket_close"]


def test_safe_pinned_response_close_failure_still_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Socket:
        def settimeout(self, value: float) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            return "93.184.216.34", 80

        def close(self) -> None:
            events.append("socket_close")

    class Response:
        status = 404

        def getheaders(self) -> list[tuple[str, str]]:
            return []

        def read(self, size: int) -> bytes:
            return b""

        def close(self) -> None:
            events.append("response_close")
            raise OSError("response close failed")

    class Connection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.sock = None

        def putrequest(self, *args: object, **kwargs: object) -> None:
            pass

        def putheader(self, *args: object, **kwargs: object) -> None:
            pass

        def endheaders(self) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            events.append("connection_close")

    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))],
    )
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr("http.client.HTTPConnection", Connection)

    raw = SafePinnedTransport().request(
        "http://example.com/robots.txt",
        user_agent="bot",
        identity_sha256="a" * 64,
    )
    with pytest.raises(OSError, match="close failed"):
        raw.close()

    assert events == ["response_close", "connection_close"]


@pytest.mark.parametrize(
    ("max_hops", "expected_content_requests"),
    [(0, 1), (1, 2)],
)
def test_redirect_hop_limit_is_exact(
    max_hops: int,
    expected_content_requests: int,
) -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/start": [response(302, Location="/middle")],
            "https://example.com/middle": [response(302, Location="/final")],
        }
    )

    with pytest.raises(AccessGatewayRedirectError, match="hop"):
        gateway(transport, max_redirect_hops=max_hops).request(
            "https://example.com/start",
            consume=read_body,
        )

    content_requests = [
        url for url in transport.requests if not url.endswith("/robots.txt")
    ]
    assert len(content_requests) == expected_content_requests


def test_same_origin_concurrency_respects_pacing_and_hard_budget() -> None:
    clock = ManualClock()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/a": [response(200)],
            "https://example.com/b": [response(200)],
        }
    )
    access = gateway(
        transport,
        clock=clock,
        pacing_interval=timedelta(milliseconds=250),
        budget_limit=2,
    )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(
                access.request, f"https://example.com/{name}", consume=read_body
            )
            for name in ("a", "b", "c")
        ]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [
        future.exception() for future in futures if future.exception() is not None
    ]

    assert len(successes) == 2
    assert len(failures) == 1
    assert isinstance(failures[0], AccessGatewayBudgetError)
    reservations = sorted(
        (result.decision.origin_reservation for result in successes),
        key=lambda item: item.budget_slot_ordinal if item else 0,
    )
    assert all(item is not None for item in reservations)
    assert reservations[0].budget_slot_ordinal == 1  # type: ignore[union-attr]
    assert reservations[1].budget_slot_ordinal == 2  # type: ignore[union-attr]
    assert (
        reservations[1].not_before - reservations[0].not_before  # type: ignore[union-attr]
        >= timedelta(milliseconds=250)
    )


def test_distinct_origins_have_independent_pacing_and_budget() -> None:
    clock = ManualClock()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://other.example/robots.txt": [response(404)],
            "https://example.com/a": [response(200)],
            "https://other.example/a": [response(200)],
        }
    )
    access = gateway(
        transport,
        origins=(ORIGIN, OTHER_ORIGIN),
        clock=clock,
        pacing_interval=timedelta(seconds=10),
        budget_limit=1,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(access.request, url, consume=read_body).result(timeout=2)
            for url in ("https://example.com/a", "https://other.example/a")
        ]

    assert [
        item.decision.origin_reservation.budget_slot_ordinal for item in results
    ] == [
        1,
        1,
    ]
    assert all(
        item.decision.origin_reservation.not_before
        == item.decision.origin_reservation.reserved_at
        for item in results
    )


def test_blocked_same_origin_pacing_does_not_block_another_origin() -> None:
    clock = ManualClock()
    sleep_started = threading.Event()
    release_sleep = threading.Event()

    def blocking_sleep(seconds: float) -> None:
        sleep_started.set()
        assert release_sleep.wait(timeout=2)
        clock.sleep(seconds)

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://other.example/robots.txt": [response(404)],
            "https://example.com/first": [response(200)],
            "https://example.com/second": [response(200)],
            "https://other.example/independent": [response(200)],
        }
    )
    access = gateway(
        transport,
        origins=(ORIGIN, OTHER_ORIGIN),
        clock=clock,
        pacing_interval=timedelta(seconds=1),
        sleeper=blocking_sleep,
    )
    access.request("https://example.com/first", consume=read_body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        same_origin = pool.submit(
            access.request,
            "https://example.com/second",
            consume=read_body,
        )
        assert sleep_started.wait(timeout=2)
        other_origin = pool.submit(
            access.request,
            "https://other.example/independent",
            consume=read_body,
        )
        assert other_origin.result(timeout=1).decision.outcome == "allow"
        release_sleep.set()
        assert same_origin.result(timeout=2).decision.outcome == "allow"


@pytest.mark.parametrize(
    ("policy_ttl", "budget_window", "oversleep", "error_type"),
    [
        (
            timedelta(seconds=5),
            timedelta(hours=1),
            10.0,
            AccessGatewayPolicyError,
        ),
        (
            timedelta(hours=1),
            timedelta(seconds=2),
            3.0,
            AccessGatewayBudgetError,
        ),
    ],
)
def test_actual_start_rechecks_policy_and_budget_window_before_transport(
    policy_ttl: timedelta,
    budget_window: timedelta,
    oversleep: float,
    error_type: type[Exception],
) -> None:
    clock = ManualClock()

    def sleeper(_: float) -> None:
        clock.advance(oversleep)

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/first": [response(200)],
            "https://example.com/late": [response(200)],
        }
    )
    access = gateway(
        transport,
        clock=clock,
        policy_ttl=policy_ttl,
        pacing_interval=timedelta(seconds=1),
        budget_window=budget_window,
        budget_limit=2,
        sleeper=sleeper,
    )
    access.request("https://example.com/first", consume=read_body)

    with pytest.raises(error_type):
        access.request("https://example.com/late", consume=read_body)

    assert "https://example.com/late" not in transport.requests
    state = access._origin_states[ORIGIN]
    assert state.budget_used == 2
    assert state.lock.acquire(blocking=False)
    state.lock.release()


def test_pacing_slot_outside_budget_window_is_typed_before_reservation() -> None:
    clock = ManualClock()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/first": [response(200)],
            "https://example.com/third": [response(200)],
        }
    )
    access = gateway(
        transport,
        clock=clock,
        pacing_interval=timedelta(seconds=2),
        budget_window=timedelta(seconds=1),
        budget_limit=2,
    )
    access.request("https://example.com/first", consume=read_body)

    with pytest.raises(AccessGatewayBudgetError, match="budget window"):
        access.request("https://example.com/outside", consume=read_body)

    assert "https://example.com/outside" not in transport.requests
    assert access._origin_states[ORIGIN].budget_used == 1
    clock.advance(3)
    third = access.request("https://example.com/third", consume=read_body)
    assert third.decision.outcome == "allow"


def test_cancellation_consumes_reservation_but_releases_origin_lock() -> None:
    clock = ManualClock()
    sleep_calls = 0

    def cancel_once(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            raise asyncio.CancelledError
        clock.sleep(seconds)

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/first": [response(200)],
            "https://example.com/third": [response(200)],
        }
    )
    access = gateway(
        transport,
        clock=clock,
        pacing_interval=timedelta(seconds=1),
        budget_limit=3,
        sleeper=cancel_once,
    )
    access.request("https://example.com/first", consume=read_body)

    with pytest.raises(asyncio.CancelledError):
        access.request("https://example.com/cancelled", consume=read_body)

    third = access.request("https://example.com/third", consume=read_body)
    assert third.decision.origin_reservation is not None
    assert third.decision.origin_reservation.budget_used_before_reservation == 2
    assert "https://example.com/cancelled" not in transport.requests


def test_timeout_and_consumer_error_close_response_and_do_not_leak_lock() -> None:
    failing_tracker = ResponseTracker()
    next_tracker = ResponseTracker()
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/fail": [
                response(200, b"partial", tracker=failing_tracker)
            ],
            "https://example.com/next": [response(200, b"ok", tracker=next_tracker)],
        }
    )
    access = gateway(transport)

    def timeout(_: RawHttpResponse) -> bytes:
        raise TimeoutError("consumer timed out")

    with pytest.raises(TimeoutError, match="consumer"):
        access.request("https://example.com/fail", consume=timeout)
    assert failing_tracker.closed == 1

    assert access.request("https://example.com/next", consume=read_body).value == b"ok"
    assert next_tracker.closed == 1


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_final_header_copy_failure_closes_before_consume(
    error_type: type[BaseException],
    tmp_path: Path,
) -> None:
    tracker = ResponseTracker()

    class FailingHeaders(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            raise error_type("header copy failed")

        def __len__(self) -> int:
            return 1

    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/final": [
                RawHttpResponse(
                    status=200,
                    headers=FailingHeaders(),
                    body_chunks=(b"content",),
                    close=lambda: setattr(tracker, "closed", tracker.closed + 1),
                )
            ],
        }
    )
    output = tmp_path / "must-not-exist"
    consumed = False

    def consume(raw: RawHttpResponse) -> bytes:
        nonlocal consumed
        consumed = True
        body = read_body(raw)
        output.write_bytes(body)
        return body

    with pytest.raises(error_type):
        gateway(transport).request(
            "https://example.com/final",
            consume=consume,
        )

    assert tracker.closed == 1
    assert consumed is False
    assert not output.exists()


def test_content_transport_error_consumes_budget_and_closes_gateway_state() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/fail": [
                TransportFailure("timeout", "content timeout", retryable=True)
            ],
        }
    )
    access = gateway(transport, budget_limit=1)

    with pytest.raises(AccessGatewayTransportError) as caught:
        access.request("https://example.com/fail", consume=read_body)
    assert caught.value.decision.outcome == "allow"

    with pytest.raises(AccessGatewayBudgetError):
        access.request("https://example.com/never", consume=read_body)
    assert "https://example.com/never" not in transport.requests


def test_emitted_decision_revalidates_repeatedly_without_mutation() -> None:
    transport = ScriptedTransport(
        {
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/data": [response(200, b"data")],
        }
    )
    decision = (
        gateway(transport)
        .request("https://example.com/data", consume=read_body)
        .decision
    )
    payload = decision.model_dump(mode="json")
    original = copy.deepcopy(payload)
    encoded = canonical_json(payload)

    first = AccessDecision.model_validate_json(encoded)
    second = AccessDecision.model_validate_json(encoded)

    assert first == second == decision
    assert payload == original
    assert canonical_json(first.model_dump(mode="json")) == encoded
