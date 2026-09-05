from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import tempfile
import uuid
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from pydantic import ValidationError

from web_listening.contracts._protocol import validate_domain
from web_listening.contracts.site_diagnostic import (
    BODY_TLS_POLICY_OUTCOME,
    AcceptedPageEvidence,
    CountedUrlOccurrence,
    DiagnosticAttempt,
    DiagnosticBudgets,
    DiagnosticBudgetUsage,
    DiagnosticIdentity,
    NormalizedOrigin,
    OriginPolicyEvidence,
    RejectedUrl,
    RobotsErrorEvidence,
    RobotsPolicyRule,
    SiteDiagnostic,
    SitemapDirective,
    SitemapEvidence,
    canonical_json,
    canonical_sha256,
    classify_http_status,
    derive_policy_preflight_classification,
    document_duplicate_reason,
    is_retryable_attempt_outcome,
    redirect_transition_allowed,
    robots_rule_matches,
    robots_rule_specificity,
    robots_rules_allow,
)

PRODUCT_TOKEN = re.compile(r"^[-A-Za-z_]+$")
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
DEFAULT_FRESHNESS = timedelta(hours=24)
TOOL_VERSION = "1.1.0"


class SiteDiagnosticError(ValueError):
    pass


class TransportFailure(RuntimeError):
    """A classified failure from a transport that sent no unverified request."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        safety: bool = False,
        deterministic: bool = False,
    ):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.safety = safety
        self.deterministic = deterministic


@dataclass(frozen=True)
class _TlsFailureClassification:
    transport_kind: str
    body_outcome: str = BODY_TLS_POLICY_OUTCOME
    retryable: bool = False
    safety: bool = True


def _classify_tls_failure(
    error: BaseException,
    _certificate_error: type[BaseException] = ssl.SSLCertVerificationError,
    _ssl_error: type[BaseException] = ssl.SSLError,
) -> _TlsFailureClassification | None:
    if isinstance(error, _certificate_error):
        return _TlsFailureClassification("certificate")
    if isinstance(error, _ssl_error):
        return _TlsFailureClassification("tls_policy")
    return None


@dataclass
class RawHttpResponse:
    status: int
    headers: Mapping[str, str]
    body_chunks: Iterable[bytes]
    close: Callable[[], None] = lambda: None


class DiagnosticTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
        progress: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> RawHttpResponse: ...


def _is_public(
    address: str,
    _ip_address: Callable[
        [str], ipaddress.IPv4Address | ipaddress.IPv6Address
    ] = ipaddress.ip_address,
) -> bool:
    try:
        value = _ip_address(address)
    except ValueError:
        return False
    return value.is_global and not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
    )


def is_public_address(address: str) -> bool:
    """Return whether an address passes the pinned transport's SSRF policy."""
    return _is_public(address)


def canonical_host_header(origin: NormalizedOrigin) -> str:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    default = 80 if origin.scheme == "http" else 443
    return (
        host if origin.effective_port == default else f"{host}:{origin.effective_port}"
    )


class SafePinnedTransport:
    """Minimal no-proxy transport with DNS-set pinning and pre-HTTP peer checks."""

    def __init__(self, *, timeout: float = 30.0, chunk_size: int = 65_536):
        self.timeout = timeout
        self.chunk_size = chunk_size
        self.__runtime_dispatch: _SafePinnedDispatch | None = None
        self.__runtime_seal: tuple[object, ...] | None = None

    def _seal_runtime(self) -> tuple[object, ...]:
        if self.__runtime_dispatch is None:
            self.__runtime_dispatch = _SAFE_PINNED_DISPATCH
        snapshot = _FROZEN_SAFE_RUNTIME_SNAPSHOT(self)
        if self.__runtime_seal is None:
            self.__runtime_seal = snapshot
        elif self.__runtime_seal != snapshot:
            raise TransportFailure(
                "transport_integrity",
                "pinned transport helper capability changed",
                safety=True,
            )
        return snapshot

    def _validate_runtime(self) -> None:
        snapshot = _FROZEN_SAFE_RUNTIME_SNAPSHOT(self)
        if self.__runtime_seal is not None and self.__runtime_seal != snapshot:
            raise TransportFailure(
                "transport_integrity",
                "pinned transport helper capability changed",
                safety=True,
            )

    def _addresses(self, host: str, port: int) -> list[str]:
        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
        dispatch = self.__runtime_dispatch
        getaddrinfo = (
            dispatch.getaddrinfo if dispatch is not None else socket.getaddrinfo
        )
        ip_address = (
            dispatch.ip_address if dispatch is not None else ipaddress.ip_address
        )
        public_address = dispatch.is_public if dispatch is not None else _is_public
        try:
            rows = getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise TransportFailure("dns", str(exc), retryable=True) from exc
        try:
            addresses = list(dict.fromkeys(str(ip_address(row[4][0])) for row in rows))
        except ValueError as exc:
            raise TransportFailure(
                "dns_address_policy", "DNS returned a malformed address", safety=True
            ) from exc
        if not addresses or any(not public_address(item) for item in addresses):
            raise TransportFailure(
                "dns_address_policy",
                "DNS returned an empty, non-public, or mixed address set",
                safety=True,
            )
        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
        return addresses

    def request(
        self,
        url: str,
        *,
        user_agent: str,
        identity_sha256: str,
        progress: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> RawHttpResponse:
        del identity_sha256  # The caller records and checks it; it is never sent as a header.
        effective_timeout = self.timeout
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
                or timeout_seconds > self.timeout
            ):
                raise TransportFailure(
                    "timeout_configuration",
                    "per-request timeout must be positive and cannot enlarge transport timeout",
                    safety=True,
                )
            effective_timeout = float(timeout_seconds)
        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
        dispatch = self.__runtime_dispatch
        if progress is not None:
            progress()
        if dispatch is not None:
            normalized, origin = _FROZEN_SAFE_DISPATCH_NORMALIZE(dispatch, url)
        else:
            normalized, origin = normalize_http_url(url)
        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
        if (
            "_addresses" in vars(self)
            or SafePinnedTransport._addresses is not _FROZEN_SAFE_PINNED_ADDRESSES
        ):
            raise TransportFailure(
                "transport_integrity",
                "pinned address resolver callable changed",
                safety=True,
            )
        addresses = _FROZEN_SAFE_PINNED_ADDRESSES(
            self, origin.host, origin.effective_port
        )
        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
        if progress is not None:
            progress()
        last_error: OSError | None = None
        connection_failures: list[OSError] = []
        raw: socket.socket | None = None
        create_connection = (
            dispatch.create_connection
            if dispatch is not None
            else socket.create_connection
        )
        for address in addresses:
            try:
                if progress is not None:
                    progress()
                raw = create_connection(
                    (address, origin.effective_port), timeout=effective_timeout
                )
                if dispatch is not None and type(raw) is not dispatch.socket_type:
                    raise TransportFailure(
                        "transport_integrity",
                        "socket constructor returned an unsupported type",
                        safety=True,
                    )
                if progress is not None:
                    try:
                        progress()
                    except BaseException:
                        if dispatch is not None:
                            dispatch.socket_close(raw)
                        else:
                            raw.close()
                        raise
                break
            except (TimeoutError, OSError) as exc:
                last_error = exc
                connection_failures.append(exc)
        if raw is None:
            kind = (
                "timeout"
                if connection_failures
                and all(isinstance(item, TimeoutError) for item in connection_failures)
                else "connect"
            )
            raise TransportFailure(
                kind,
                str(last_error or "connection failed"),
                retryable=True,
            )

        connection: http.client.HTTPConnection | http.client.HTTPSConnection
        response: http.client.HTTPResponse | None = None
        try:
            if dispatch is not None:
                _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
                if "settimeout" in getattr(raw, "__dict__", {}):
                    raise TransportFailure(
                        "transport_integrity",
                        "socket timeout capability changed",
                        safety=True,
                    )
                dispatch.socket_settimeout(raw, effective_timeout)
            else:
                raw.settimeout(effective_timeout)
            if origin.scheme == "https":
                if progress is not None:
                    progress()
                try:
                    create_default_context = (
                        dispatch.create_default_context
                        if dispatch is not None
                        else ssl.create_default_context
                    )
                    context = create_default_context()
                    if dispatch is not None:
                        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
                        if type(
                            context
                        ) is not dispatch.ssl_context_type or "wrap_socket" in getattr(
                            context, "__dict__", {}
                        ):
                            raise TransportFailure(
                                "transport_integrity",
                                "TLS context capability changed",
                                safety=True,
                            )
                        raw = dispatch.ssl_wrap_socket(
                            context, raw, server_hostname=origin.host
                        )
                        if type(raw) is not dispatch.ssl_socket_type:
                            raise TransportFailure(
                                "transport_integrity",
                                "TLS wrapper returned an unsupported socket type",
                                safety=True,
                            )
                    else:
                        raw = context.wrap_socket(raw, server_hostname=origin.host)
                except ssl.SSLError as exc:
                    classifier = (
                        dispatch.classify_tls_failure
                        if dispatch is not None
                        else _classify_tls_failure
                    )
                    classification = classifier(exc)
                    assert classification is not None
                    raise TransportFailure(
                        classification.transport_kind,
                        str(exc),
                        retryable=classification.retryable,
                        safety=classification.safety,
                    ) from exc
                if progress is not None:
                    progress()
            try:
                ip_address = (
                    dispatch.ip_address
                    if dispatch is not None
                    else ipaddress.ip_address
                )
                if dispatch is not None:
                    _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
                    if "getpeername" in getattr(raw, "__dict__", {}):
                        raise TransportFailure(
                            "transport_integrity",
                            "socket peer capability changed",
                            safety=True,
                        )
                    peer_name = dispatch.socket_getpeername(raw)
                else:
                    peer_name = raw.getpeername()
                peer = str(ip_address(peer_name[0]))
            except ValueError:
                peer = ""
            public_address = dispatch.is_public if dispatch is not None else _is_public
            if peer not in addresses or not public_address(peer):
                raise TransportFailure(
                    "peer_mismatch",
                    "connected peer is outside the validated DNS set",
                    safety=True,
                )

            # Assign the already-connected, already-verified socket. http.client therefore
            # cannot resolve again or send a request before the peer gate above.
            if origin.scheme == "https":
                connection_type = (
                    dispatch.https_connection
                    if dispatch is not None
                    else http.client.HTTPSConnection
                )
            else:
                connection_type = (
                    dispatch.http_connection
                    if dispatch is not None
                    else http.client.HTTPConnection
                )
            connection = connection_type(
                origin.host, origin.effective_port, timeout=effective_timeout
            )
            if dispatch is not None and (
                type(connection) is not connection_type
                or any(
                    name in getattr(connection, "__dict__", {})
                    for name in (
                        "putrequest",
                        "putheader",
                        "endheaders",
                        "getresponse",
                        "close",
                    )
                )
            ):
                raise TransportFailure(
                    "transport_integrity",
                    "HTTP connection capability changed",
                    safety=True,
                )
            connection.sock = raw
            split_url = dispatch.urlsplit if dispatch is not None else urlsplit
            parts = split_url(normalized)
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            putrequest = (
                dispatch.http_putrequest
                if dispatch is not None
                else type(connection).putrequest
            )
            putheader = (
                dispatch.http_putheader
                if dispatch is not None
                else type(connection).putheader
            )
            endheaders = (
                dispatch.http_endheaders
                if dispatch is not None
                else type(connection).endheaders
            )
            getresponse = (
                dispatch.http_getresponse
                if dispatch is not None
                else type(connection).getresponse
            )
            putrequest(
                connection,
                "GET",
                target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            host_header = (
                dispatch.canonical_host_header
                if dispatch is not None
                else canonical_host_header
            )
            putheader(connection, "Host", host_header(origin))
            putheader(connection, "User-Agent", user_agent)
            putheader(connection, "Accept-Encoding", "identity, gzip")
            putheader(connection, "Connection", "close")
            if progress is not None:
                progress()
            _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
            endheaders(connection)
            response = getresponse(connection)
            _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
            if dispatch is not None and (
                type(response) is not dispatch.http_response_type
                or any(
                    name in getattr(response, "__dict__", {})
                    for name in ("read", "getheaders", "close")
                )
            ):
                dispatch.response_close(response)
                dispatch.http_close(connection)
                raise TransportFailure(
                    "transport_integrity",
                    "HTTP response capability changed",
                    safety=True,
                )
            if progress is not None:
                try:
                    progress()
                except BaseException:
                    if dispatch is not None:
                        dispatch.response_close(response)
                        dispatch.http_close(connection)
                    else:
                        response.close()
                        connection.close()
                    raise
        except TransportFailure:
            raise
        except ssl.SSLError as exc:
            classifier = (
                dispatch.classify_tls_failure
                if dispatch is not None
                else _classify_tls_failure
            )
            classification = classifier(exc)
            assert classification is not None
            raise TransportFailure(
                classification.transport_kind,
                str(exc),
                retryable=classification.retryable,
                safety=classification.safety,
            ) from exc
        except http.client.RemoteDisconnected as exc:
            raise TransportFailure(
                "remote_disconnected", str(exc), retryable=True
            ) from exc
        except (
            http.client.BadStatusLine,
            http.client.LineTooLong,
            http.client.IncompleteRead,
        ) as exc:
            raise TransportFailure(
                "malformed_status", str(exc), deterministic=True
            ) from exc
        except TimeoutError as exc:
            raise TransportFailure("timeout", str(exc), retryable=True) from exc
        except (ConnectionError, OSError) as exc:
            raise TransportFailure("connect_or_http", str(exc), retryable=True) from exc
        except http.client.HTTPException as exc:
            raise TransportFailure(
                "unclassified_http_protocol", str(exc), safety=True
            ) from exc
        except Exception as exc:
            raise TransportFailure(
                "unclassified_pre_response",
                str(exc),
                safety=True,
            ) from exc
        finally:
            if response is None:
                with suppress(OSError):
                    if dispatch is not None:
                        dispatch.socket_close(raw)
                    else:
                        raw.close()

        def close_response() -> None:
            progress_error: BaseException | None = None
            if progress is not None:
                try:
                    progress()
                except BaseException as exc:  # noqa: BLE001 - preserve renewal through cleanup.
                    progress_error = exc
            try:
                if dispatch is not None:
                    dispatch.response_close(response)
                else:
                    response.close()
            finally:
                if dispatch is not None:
                    dispatch.http_close(connection)
                else:
                    connection.close()
            if progress_error is not None:
                raise progress_error
            if progress is not None:
                progress()

        def chunks() -> Iterator[bytes]:
            try:
                while True:
                    if progress is not None:
                        progress()
                    if dispatch is not None:
                        _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME(self)
                        chunk = dispatch.response_read(response, self.chunk_size)
                    else:
                        chunk = response.read(self.chunk_size)
                    if progress is not None:
                        progress()
                    if not chunk:
                        break
                    yield chunk
            finally:
                close_response()

        response_headers: dict[str, str] = {}
        handed_off = False
        try:
            if progress is not None:
                progress()
            response_header_rows = (
                dispatch.response_getheaders(response)
                if dispatch is not None
                else response.getheaders()
            )
            for key, value in response_header_rows:
                normalized_key = key.casefold()
                response_headers[normalized_key] = (
                    f"{response_headers[normalized_key]}, {value}"
                    if normalized_key in response_headers
                    else value
                )
            if progress is not None:
                progress()
            result = RawHttpResponse(
                status=response.status,
                headers=response_headers,
                body_chunks=chunks(),
                close=close_response,
            )
            handed_off = True
            return result
        except Exception as exc:
            raise TransportFailure(
                "unclassified_http_protocol",
                str(exc),
                safety=True,
            ) from exc
        finally:
            if not handed_off:
                # Cleanup must never replace the primary protocol failure, including
                # cancellation raised by a close implementation.
                try:
                    close_response()
                except BaseException:  # noqa: BLE001,S110
                    pass


_FROZEN_SAFE_PINNED_ADDRESSES = SafePinnedTransport._addresses


def _normalize_host(host: str) -> str:
    if not host:
        raise SiteDiagnosticError("URL host is required")
    try:
        return str(ipaddress.ip_address(host)).lower()
    except ValueError:
        try:
            return validate_domain(
                host.rstrip(".").encode("idna").decode("ascii").lower()
            )
        except (UnicodeError, ValueError) as exc:
            raise SiteDiagnosticError("URL host cannot be normalized") from exc


def _normalize_percent_path(path: str) -> str:
    # Normalize percent hex and decode only unreserved octets so reserved path
    # separators retain their meaning.
    path = path or "/"
    if re.search(r"%(?![0-9A-Fa-f]{2})", path):
        raise SiteDiagnosticError("malformed percent encoding in URL path")
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def replace(match: re.Match[str]) -> str:
        value = chr(int(match.group(1), 16))
        return value if value in unreserved else "%" + match.group(1).upper()

    path = re.sub(r"%([0-9A-Fa-f]{2})", replace, path)
    path = _remove_dot_segments(path)
    return quote(path, safe="/%:@!$&'()*+,;=-._~")


def _remove_dot_segments(path: str) -> str:
    source = path
    output = ""
    while source:
        if source.startswith("../"):
            source = source[3:]
        elif source.startswith(("./", "/./")):
            source = source[2:]
        elif source == "/.":
            source = "/"
        elif source.startswith("/../"):
            source = source[3:]
            output = output.rsplit("/", 1)[0]
        elif source == "/..":
            source = "/"
            output = output.rsplit("/", 1)[0]
        elif source in {".", ".."}:
            source = ""
        else:
            start = 1 if source.startswith("/") else 0
            slash = source.find("/", start)
            if slash < 0:
                output += source
                source = ""
            else:
                output += source[:slash]
                source = source[slash:]
    return output or "/"


def normalize_http_url(url: str) -> tuple[str, NormalizedOrigin]:
    if (
        url != url.strip()
        or "\\" in url
        or any(ord(char) < 32 or ord(char) == 127 for char in url)
    ):
        raise SiteDiagnosticError(
            "URL contains forbidden whitespace, controls, or backslash"
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SiteDiagnosticError("malformed URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise SiteDiagnosticError("absolute HTTP(S) URL required")
    if parts.username is not None or parts.password is not None:
        raise SiteDiagnosticError("credentials are forbidden in diagnostic URLs")
    host = _normalize_host(parts.hostname)
    if port is not None and not 1 <= port <= 65535:
        raise SiteDiagnosticError("URL port must be in 1..65535")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    try:
        origin = NormalizedOrigin(
            scheme=scheme, host=host, effective_port=effective_port
        )
    except (ValidationError, ValueError) as exc:
        raise SiteDiagnosticError("URL host cannot be normalized") from exc
    netloc = canonical_host_header(origin)
    path = _normalize_percent_path(parts.path or "/")
    return urlunsplit((scheme, netloc, path, parts.query, "")), origin


def normalize_origin(value: str) -> NormalizedOrigin:
    normalized, origin = normalize_http_url(value)
    parts = urlsplit(normalized)
    if (parts.path or "/") != "/" or parts.query or parts.fragment:
        raise SiteDiagnosticError(
            "allowed document origins must be origins, not URLs with paths"
        )
    return origin


def _sealed_normalize_host(
    host: str,
    *,
    ip_address: Callable[[str], ipaddress.IPv4Address | ipaddress.IPv6Address],
    domain_validator: Callable[[str], str],
) -> str:
    if not host:
        raise SiteDiagnosticError("URL host is required")
    try:
        return str(ip_address(host)).lower()
    except ValueError:
        try:
            return domain_validator(
                host.rstrip(".").encode("idna").decode("ascii").lower()
            )
        except (UnicodeError, ValueError) as exc:
            raise SiteDiagnosticError("URL host cannot be normalized") from exc


def _sealed_normalize_percent_path(
    path: str,
    *,
    regex_search: Callable[..., re.Match[str] | None],
    regex_sub: Callable[..., str],
    remove_dot_segments: Callable[[str], str],
    quote_path: Callable[..., str],
) -> str:
    path = path or "/"
    if regex_search(r"%(?![0-9A-Fa-f]{2})", path):
        raise SiteDiagnosticError("malformed percent encoding in URL path")
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def replace(match: re.Match[str]) -> str:
        value = chr(int(match.group(1), 16))
        return value if value in unreserved else "%" + match.group(1).upper()

    path = regex_sub(r"%([0-9A-Fa-f]{2})", replace, path)
    return quote_path(remove_dot_segments(path), safe="/%:@!$&'()*+,;=-._~")


@dataclass(frozen=True, slots=True)
class _SafePinnedDispatch:
    getaddrinfo: Callable[..., object]
    create_connection: Callable[..., socket.socket]
    create_default_context: Callable[..., ssl.SSLContext]
    http_connection: type[http.client.HTTPConnection]
    https_connection: type[http.client.HTTPSConnection]
    ip_address: Callable[[str], ipaddress.IPv4Address | ipaddress.IPv6Address]
    is_public: Callable[[str], bool]
    classify_tls_failure: Callable[[BaseException], _TlsFailureClassification | None]
    urlsplit: Callable[[str], object]
    urlunsplit: Callable[..., str]
    quote: Callable[..., str]
    validate_domain: Callable[[str], str]
    remove_dot_segments: Callable[[str], str]
    canonical_host_header: Callable[[NormalizedOrigin], str]
    normalize_host: Callable[..., str]
    normalize_percent_path: Callable[..., str]
    regex_search: Callable[..., re.Match[str] | None]
    regex_sub: Callable[..., str]
    normalized_origin: Callable[..., NormalizedOrigin]
    normalized_origin_type: type[NormalizedOrigin]
    ssl_wrap_socket: Callable[..., ssl.SSLSocket]
    socket_settimeout: Callable[..., None]
    socket_getpeername: Callable[..., object]
    socket_close: Callable[..., None]
    http_putrequest: Callable[..., None]
    http_putheader: Callable[..., None]
    http_endheaders: Callable[..., None]
    http_getresponse: Callable[..., http.client.HTTPResponse]
    http_close: Callable[..., None]
    response_read: Callable[..., bytes]
    response_getheaders: Callable[..., list[tuple[str, str]]]
    response_close: Callable[..., None]
    socket_type: type[socket.socket]
    ssl_context_type: type[ssl.SSLContext]
    ssl_socket_type: type[ssl.SSLSocket]
    http_response_type: type[http.client.HTTPResponse]

    def normalize_http_url(self, url: str) -> tuple[str, NormalizedOrigin]:
        if (
            url != url.strip()
            or "\\" in url
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise SiteDiagnosticError(
                "URL contains forbidden whitespace, controls, or backslash"
            )
        try:
            parts = self.urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise SiteDiagnosticError("malformed URL") from exc
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"} or not parts.hostname:
            raise SiteDiagnosticError("absolute HTTP(S) URL required")
        if parts.username is not None or parts.password is not None:
            raise SiteDiagnosticError("credentials are forbidden in diagnostic URLs")
        host = self.normalize_host(
            parts.hostname,
            ip_address=self.ip_address,
            domain_validator=self.validate_domain,
        )
        if port is not None and not 1 <= port <= 65535:
            raise SiteDiagnosticError("URL port must be in 1..65535")
        effective_port = (
            port if port is not None else (443 if scheme == "https" else 80)
        )
        try:
            origin = self.normalized_origin(
                scheme=scheme,
                host=host,
                effective_port=effective_port,
            )
        except (ValidationError, ValueError) as exc:
            raise SiteDiagnosticError("URL host cannot be normalized") from exc
        if type(origin) is not self.normalized_origin_type:
            raise SiteDiagnosticError("URL origin capability returned an invalid type")
        path = self.normalize_percent_path(
            parts.path or "/",
            regex_search=self.regex_search,
            regex_sub=self.regex_sub,
            remove_dot_segments=self.remove_dot_segments,
            quote_path=self.quote,
        )
        return (
            self.urlunsplit(
                (
                    scheme,
                    self.canonical_host_header(origin),
                    path,
                    parts.query,
                    "",
                )
            ),
            origin,
        )


_SAFE_PINNED_DISPATCH = _SafePinnedDispatch(
    getaddrinfo=socket.getaddrinfo,
    create_connection=socket.create_connection,
    create_default_context=ssl.create_default_context,
    http_connection=http.client.HTTPConnection,
    https_connection=http.client.HTTPSConnection,
    ip_address=ipaddress.ip_address,
    is_public=_is_public,
    classify_tls_failure=_classify_tls_failure,
    urlsplit=urlsplit,
    urlunsplit=urlunsplit,
    quote=quote,
    validate_domain=validate_domain,
    remove_dot_segments=_remove_dot_segments,
    canonical_host_header=canonical_host_header,
    normalize_host=_sealed_normalize_host,
    normalize_percent_path=_sealed_normalize_percent_path,
    regex_search=re.search,
    regex_sub=re.sub,
    normalized_origin=NormalizedOrigin,
    normalized_origin_type=NormalizedOrigin,
    ssl_wrap_socket=ssl.SSLContext.wrap_socket,
    socket_settimeout=socket.socket.settimeout,
    socket_getpeername=socket.socket.getpeername,
    socket_close=socket.socket.close,
    http_putrequest=http.client.HTTPConnection.putrequest,
    http_putheader=http.client.HTTPConnection.putheader,
    http_endheaders=http.client.HTTPConnection.endheaders,
    http_getresponse=http.client.HTTPConnection.getresponse,
    http_close=http.client.HTTPConnection.close,
    response_read=http.client.HTTPResponse.read,
    response_getheaders=http.client.HTTPResponse.getheaders,
    response_close=http.client.HTTPResponse.close,
    socket_type=socket.socket,
    ssl_context_type=ssl.SSLContext,
    ssl_socket_type=ssl.SSLSocket,
    http_response_type=http.client.HTTPResponse,
)


_FROZEN_SAFE_DISPATCH_NORMALIZE = _SafePinnedDispatch.normalize_http_url
_FROZEN_SAFE_PINNED_REQUEST = SafePinnedTransport.request
_FROZEN_SAFE_PINNED_SEAL_RUNTIME = SafePinnedTransport._seal_runtime
_FROZEN_SAFE_PINNED_VALIDATE_RUNTIME = SafePinnedTransport._validate_runtime
_FROZEN_SAFE_IS_PUBLIC = _is_public
_FROZEN_SAFE_PUBLIC_ADDRESS = is_public_address
_FROZEN_SAFE_NORMALIZE_HOST = _normalize_host
_FROZEN_SAFE_NORMALIZE_PERCENT_PATH = _normalize_percent_path
_FROZEN_SAFE_NORMALIZE_HTTP_URL = normalize_http_url
_FROZEN_SAFE_NORMALIZE_ORIGIN = normalize_origin
_FROZEN_SAFE_HOST_HEADER = canonical_host_header
_FROZEN_SAFE_TLS_CLASSIFIER = _classify_tls_failure
_FROZEN_SAFE_IP_ADDRESS = ipaddress.ip_address
_FROZEN_SAFE_RE_SEARCH = re.search
_FROZEN_SAFE_RE_SUB = re.sub
_FROZEN_SAFE_URLSPLIT = urlsplit


def _safe_pinned_runtime_snapshot(transport: SafePinnedTransport) -> tuple[object, ...]:
    runtime_dispatch = object.__getattribute__(
        transport, "_SafePinnedTransport__runtime_dispatch"
    )
    if (
        type(transport) is not SafePinnedTransport
        or any(
            name in vars(transport)
            for name in (
                "request",
                "_addresses",
                "_seal_runtime",
                "_validate_runtime",
            )
        )
        or SafePinnedTransport.request is not _FROZEN_SAFE_PINNED_REQUEST
        or SafePinnedTransport._addresses is not _FROZEN_SAFE_PINNED_ADDRESSES
        or SafePinnedTransport._seal_runtime is not _FROZEN_SAFE_PINNED_SEAL_RUNTIME
        or SafePinnedTransport._validate_runtime
        is not _FROZEN_SAFE_PINNED_VALIDATE_RUNTIME
        or _is_public is not _FROZEN_SAFE_IS_PUBLIC
        or is_public_address is not _FROZEN_SAFE_PUBLIC_ADDRESS
        or _normalize_host is not _FROZEN_SAFE_NORMALIZE_HOST
        or _normalize_percent_path is not _FROZEN_SAFE_NORMALIZE_PERCENT_PATH
        or normalize_http_url is not _FROZEN_SAFE_NORMALIZE_HTTP_URL
        or normalize_origin is not _FROZEN_SAFE_NORMALIZE_ORIGIN
        or canonical_host_header is not _FROZEN_SAFE_HOST_HEADER
        or _classify_tls_failure is not _FROZEN_SAFE_TLS_CLASSIFIER
        or ipaddress.ip_address is not _FROZEN_SAFE_IP_ADDRESS
        or re.search is not _FROZEN_SAFE_RE_SEARCH
        or re.sub is not _FROZEN_SAFE_RE_SUB
        or urlsplit is not _FROZEN_SAFE_URLSPLIT
        or _FROZEN_SAFE_PINNED_ADDRESSES is not SafePinnedTransport._addresses
        or _SafePinnedDispatch.normalize_http_url is not _FROZEN_SAFE_DISPATCH_NORMALIZE
        or (
            runtime_dispatch is not None
            and (
                runtime_dispatch is not _SAFE_PINNED_DISPATCH
                or socket.getaddrinfo is not runtime_dispatch.getaddrinfo
                or socket.create_connection is not runtime_dispatch.create_connection
                or ssl.create_default_context
                is not runtime_dispatch.create_default_context
                or http.client.HTTPConnection is not runtime_dispatch.http_connection
                or http.client.HTTPSConnection is not runtime_dispatch.https_connection
                or ipaddress.ip_address is not runtime_dispatch.ip_address
                or _is_public is not runtime_dispatch.is_public
                or _classify_tls_failure is not runtime_dispatch.classify_tls_failure
                or urlsplit is not runtime_dispatch.urlsplit
                or urlunsplit is not runtime_dispatch.urlunsplit
                or quote is not runtime_dispatch.quote
                or validate_domain is not runtime_dispatch.validate_domain
                or _remove_dot_segments is not runtime_dispatch.remove_dot_segments
                or canonical_host_header is not runtime_dispatch.canonical_host_header
                or _sealed_normalize_host is not runtime_dispatch.normalize_host
                or _sealed_normalize_percent_path
                is not runtime_dispatch.normalize_percent_path
                or re.search is not runtime_dispatch.regex_search
                or re.sub is not runtime_dispatch.regex_sub
                or NormalizedOrigin is not runtime_dispatch.normalized_origin
                or NormalizedOrigin is not runtime_dispatch.normalized_origin_type
                or ssl.SSLContext.wrap_socket is not runtime_dispatch.ssl_wrap_socket
                or socket.socket.settimeout is not runtime_dispatch.socket_settimeout
                or socket.socket.getpeername is not runtime_dispatch.socket_getpeername
                or socket.socket.close is not runtime_dispatch.socket_close
                or http.client.HTTPConnection.putrequest
                is not runtime_dispatch.http_putrequest
                or http.client.HTTPConnection.putheader
                is not runtime_dispatch.http_putheader
                or http.client.HTTPConnection.endheaders
                is not runtime_dispatch.http_endheaders
                or http.client.HTTPConnection.getresponse
                is not runtime_dispatch.http_getresponse
                or http.client.HTTPConnection.close is not runtime_dispatch.http_close
                or http.client.HTTPResponse.read is not runtime_dispatch.response_read
                or http.client.HTTPResponse.getheaders
                is not runtime_dispatch.response_getheaders
                or http.client.HTTPResponse.close is not runtime_dispatch.response_close
                or socket.socket is not runtime_dispatch.socket_type
                or ssl.SSLContext is not runtime_dispatch.ssl_context_type
                or ssl.SSLSocket is not runtime_dispatch.ssl_socket_type
                or http.client.HTTPResponse is not runtime_dispatch.http_response_type
            )
        )
    ):
        raise TransportFailure(
            "transport_integrity",
            "pinned transport helper capability changed",
            safety=True,
        )
    return (
        id(transport),
        transport.timeout,
        transport.chunk_size,
        id(_FROZEN_SAFE_PINNED_REQUEST),
        id(_FROZEN_SAFE_PINNED_ADDRESSES),
        id(_FROZEN_SAFE_PINNED_SEAL_RUNTIME),
        id(_FROZEN_SAFE_PINNED_VALIDATE_RUNTIME),
        id(_FROZEN_SAFE_IS_PUBLIC),
        id(_FROZEN_SAFE_PUBLIC_ADDRESS),
        id(_FROZEN_SAFE_NORMALIZE_HOST),
        id(_FROZEN_SAFE_NORMALIZE_PERCENT_PATH),
        id(_FROZEN_SAFE_NORMALIZE_HTTP_URL),
        id(_FROZEN_SAFE_NORMALIZE_ORIGIN),
        id(_FROZEN_SAFE_HOST_HEADER),
        id(_FROZEN_SAFE_TLS_CLASSIFIER),
        id(_FROZEN_SAFE_IP_ADDRESS),
        id(_FROZEN_SAFE_RE_SEARCH),
        id(_FROZEN_SAFE_RE_SUB),
        id(_FROZEN_SAFE_URLSPLIT),
        id(_FROZEN_SAFE_DISPATCH_NORMALIZE),
        id(runtime_dispatch),
        *(
            (
                id(runtime_dispatch.getaddrinfo),
                id(runtime_dispatch.create_connection),
                id(runtime_dispatch.create_default_context),
                id(runtime_dispatch.http_connection),
                id(runtime_dispatch.https_connection),
                id(runtime_dispatch.ip_address),
                id(runtime_dispatch.is_public),
                id(runtime_dispatch.classify_tls_failure),
                id(runtime_dispatch.urlsplit),
                id(runtime_dispatch.urlunsplit),
                id(runtime_dispatch.quote),
                id(runtime_dispatch.validate_domain),
                id(runtime_dispatch.remove_dot_segments),
                id(runtime_dispatch.canonical_host_header),
                id(runtime_dispatch.normalize_host),
                id(runtime_dispatch.normalize_percent_path),
                id(runtime_dispatch.regex_search),
                id(runtime_dispatch.regex_sub),
                id(runtime_dispatch.normalized_origin),
                id(runtime_dispatch.normalized_origin_type),
                id(runtime_dispatch.ssl_wrap_socket),
                id(runtime_dispatch.socket_settimeout),
                id(runtime_dispatch.socket_getpeername),
                id(runtime_dispatch.socket_close),
                id(runtime_dispatch.http_putrequest),
                id(runtime_dispatch.http_putheader),
                id(runtime_dispatch.http_endheaders),
                id(runtime_dispatch.http_getresponse),
                id(runtime_dispatch.http_close),
                id(runtime_dispatch.response_read),
                id(runtime_dispatch.response_getheaders),
                id(runtime_dispatch.response_close),
                id(runtime_dispatch.socket_type),
                id(runtime_dispatch.ssl_context_type),
                id(runtime_dispatch.ssl_socket_type),
                id(runtime_dispatch.http_response_type),
            )
            if runtime_dispatch is not None
            else ()
        ),
        id(_FROZEN_SAFE_RUNTIME_SNAPSHOT),
    )


_FROZEN_SAFE_RUNTIME_SNAPSHOT = _safe_pinned_runtime_snapshot


def _domain_allowed(host: str, domains: set[str]) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return any(
            host == item or (not _is_ip_literal(item) and host.endswith("." + item))
            for item in domains
        )
    return host in domains


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str
    line_number: int

    @property
    def specificity(self) -> int:
        return robots_rule_specificity(self.pattern)

    def matches(self, path: str) -> bool:
        return robots_rule_matches(self.pattern, path)


@dataclass
class RobotsGroup:
    agents: list[str] = field(default_factory=list)
    rules: list[RobotsRule] = field(default_factory=list)


@dataclass
class ParsedRobots:
    product_token: str
    groups: list[RobotsGroup]
    sitemaps: list[SitemapDirective]
    warnings: list[str]
    errors: list[str]
    sitemap_occurrence_count: int = 0
    sitemap_occurrence_lines: list[int] = field(default_factory=list)

    def selected_rules(self) -> list[RobotsRule]:
        exact = [
            g
            for g in self.groups
            if any(a.casefold() == self.product_token.casefold() for a in g.agents)
        ]
        selected = exact or [g for g in self.groups if any(a == "*" for a in g.agents)]
        return [rule for group in selected for rule in group.rules]

    def is_allowed(self, url: str) -> bool:
        return robots_rules_allow(self.selected_rules(), url)  # type: ignore[arg-type]


def parse_robots(text: str, *, product_token: str) -> ParsedRobots:
    if not PRODUCT_TOKEN.fullmatch(product_token):
        raise SiteDiagnosticError("invalid RFC 9309 product token")
    groups: list[RobotsGroup] = []
    current: RobotsGroup | None = None
    saw_rules = False
    sitemaps: list[SitemapDirective] = []
    warnings: list[str] = []
    errors: list[str] = []
    sitemap_occurrence_count = 0
    sitemap_occurrence_lines: list[int] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            warnings.append(f"line {line_number}: malformed line ignored")
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        directive = name.casefold()
        if directive == "sitemap":
            sitemap_occurrence_count += 1
            sitemap_occurrence_lines.append(line_number)
            if not value:
                errors.append(f"line {line_number}: empty Sitemap directive")
            else:
                try:
                    normalized, _ = normalize_http_url(value)
                    sitemaps.append(
                        SitemapDirective(url=normalized, line_number=line_number)
                    )
                except ValueError:
                    errors.append(f"line {line_number}: malformed Sitemap directive")
            continue
        if directive == "user-agent":
            if not value or (value != "*" and not PRODUCT_TOKEN.fullmatch(value)):
                warnings.append(f"line {line_number}: invalid User-agent ignored")
                continue
            if current is None or saw_rules:
                current = RobotsGroup()
                groups.append(current)
                saw_rules = False
            current.agents.append(value.casefold() if value == "*" else value)
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in name + value):
            errors.append(f"line {line_number}: control character in robots directive")
            continue
        if directive in {"allow", "disallow"}:
            if current is None or not current.agents:
                warnings.append(f"line {line_number}: rule before a group ignored")
                continue
            saw_rules = True
            if not value:  # Empty Allow/Disallow imposes no rule.
                continue
            current.rules.append(
                RobotsRule(
                    allow=directive == "allow", pattern=value, line_number=line_number
                )
            )
            continue
        warnings.append(f"line {line_number}: unknown directive ignored")
    return ParsedRobots(
        product_token,
        groups,
        sitemaps,
        warnings,
        errors,
        sitemap_occurrence_count,
        sitemap_occurrence_lines,
    )


def _looks_like_html(text: str) -> bool:
    remainder = text.removeprefix("\ufeff").lstrip()
    declaration = re.match(r"<\?xml\s+[^?]*\?>", remainder, re.IGNORECASE)
    if declaration is not None:
        remainder = remainder[declaration.end() :].lstrip()
    while remainder.startswith("<!--"):
        end = remainder.find("-->", 4)
        if end < 0:
            return True
        remainder = remainder[end + 3 :].lstrip()
    if (
        re.match(
            r"(?:<!doctype\s+html\b|<html\b|<head\b|<body\b)",
            remainder,
            re.IGNORECASE,
        )
        is not None
    ):
        return True
    return remainder.startswith("<")


def _decode_robots_utf8(body: bytes) -> str:
    text = body.decode("utf-8", errors="strict")
    text = text.removeprefix("\ufeff")
    if "\ufeff" in text:
        raise UnicodeError("misplaced or repeated UTF-8 BOM")
    return text


def _safe_raw_evidence(value: str) -> str:
    authority = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)", value)
    try:
        parsed = urlsplit(value)
        has_userinfo = (
            parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
        )
    except ValueError:
        has_userinfo = authority is not None and "@" in authority.group(1)
    if (
        any(ord(char) < 32 or ord(char) == 127 for char in value)
        or has_userinfo
        or (authority is not None and "@" in authority.group(1))
    ):
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    return value


def _stable_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


@dataclass
class _BodyResult:
    body: bytes
    wire: int
    decoded: int
    compressed: bool = False
    complete: bool = True


class _BodyFailure(Exception):
    def __init__(
        self,
        reason: str,
        *,
        wire: int,
        decoded: int,
        retryable: bool = False,
        deterministic: bool = False,
    ):
        super().__init__(reason)
        self.reason = reason
        self.wire = wire
        self.decoded = decoded
        self.retryable = retryable
        self.deterministic = deterministic


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [
        value.strip()
        for key, value in headers.items()
        if key.casefold() == name.casefold()
    ]
    return ", ".join(values) if values else None


def _read_bounded_body(
    response: RawHttpResponse,
    *,
    url: str,
    wire_limit: int,
    decoded_limit: int,
    aggregate_wire_remaining: int,
    aggregate_decoded_remaining: int,
) -> _BodyResult:
    encoding = (_header(response.headers, "Content-Encoding") or "").strip().casefold()
    media_type, _ = _parse_content_type(_header(response.headers, "Content-Type"))
    encodings = [item.strip() for item in encoding.split(",") if item.strip()]
    pending: list[bytes] = []
    prefix = b""
    wire = 0
    decoded = 0
    try:
        if len(encodings) > 1 or any(
            item not in {"identity", "gzip"} for item in encodings
        ):
            raise _BodyFailure(
                "unsupported_or_multiple_content_encoding", wire=0, decoded=0
            )
        iterator = iter(response.body_chunks)
        while len(prefix) < 2:
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            if not isinstance(chunk, bytes):
                raise _BodyFailure(
                    "transport_returned_non_bytes", wire=wire, decoded=decoded
                )
            next_wire = wire + len(chunk)
            if next_wire > wire_limit or next_wire > aggregate_wire_remaining:
                wire = min(wire_limit, aggregate_wire_remaining)
                raise _BodyFailure("wire_budget_exhausted", wire=wire, decoded=decoded)
            wire = next_wire
            pending.append(chunk)
            prefix += chunk
        magic = prefix.startswith(b"\x1f\x8b")
        suffix = urlsplit(url).path.casefold().endswith(".gz")
        header_gzip = encodings == ["gzip"]
        mime_gzip = media_type in {"application/gzip", "application/x-gzip"}
        if (header_gzip or suffix or mime_gzip) and not magic:
            raise _BodyFailure("gzip_signal_mismatch", wire=wire, decoded=decoded)
        if encodings == ["identity"] and magic:
            raise _BodyFailure("gzip_signal_mismatch", wire=wire, decoded=decoded)
        compressed = header_gzip or suffix or mime_gzip or magic
        output = bytearray()

        def append(value: bytes) -> None:
            nonlocal decoded
            if (
                len(value) > decoded_limit - decoded
                or len(value) > aggregate_decoded_remaining - decoded
            ):
                decoded = min(decoded_limit, aggregate_decoded_remaining)
                raise _BodyFailure(
                    "decoded_budget_exhausted", wire=wire, decoded=decoded
                )
            decoded += len(value)
            output.extend(value)

        def remaining_chunks() -> Iterator[bytes]:
            nonlocal wire
            yield from pending
            for item in iterator:
                if not isinstance(item, bytes):
                    raise _BodyFailure(
                        "transport_returned_non_bytes", wire=wire, decoded=decoded
                    )
                next_wire = wire + len(item)
                if next_wire > wire_limit or next_wire > aggregate_wire_remaining:
                    wire = min(wire_limit, aggregate_wire_remaining)
                    raise _BodyFailure(
                        "wire_budget_exhausted", wire=wire, decoded=decoded
                    )
                wire = next_wire
                yield item

        if not compressed:
            for item in remaining_chunks():
                append(item)
            return _BodyResult(bytes(output), wire, decoded, False)

        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        for item in remaining_chunks():
            data = item
            while data:
                remaining = min(
                    decoded_limit - decoded, aggregate_decoded_remaining - decoded
                )
                piece = decompressor.decompress(data, max(0, remaining) + 1)
                append(piece)
                data = decompressor.unconsumed_tail
                if not data:
                    break
        if (
            not decompressor.eof
            or decompressor.unused_data
            or decompressor.unconsumed_tail
        ):
            raise _BodyFailure(
                "incomplete_or_multiple_gzip_members", wire=wire, decoded=decoded
            )
        if bytes(output).startswith(b"\x1f\x8b"):
            raise _BodyFailure("nested_compression", wire=wire, decoded=decoded)
        return _BodyResult(bytes(output), wire, decoded, True)
    except _BodyFailure:
        raise
    except http.client.IncompleteRead as exc:
        partial = exc.partial if isinstance(exc.partial, bytes) else b""
        wire = min(wire + len(partial), wire_limit, aggregate_wire_remaining)
        suffix_gzip = urlsplit(url).path.casefold().endswith(".gz")
        signaled_gzip = (
            encodings == ["gzip"]
            or media_type in {"application/gzip", "application/x-gzip"}
            or suffix_gzip
            or prefix.startswith(b"\x1f\x8b")
            or partial.startswith(b"\x1f\x8b")
        )
        if not signaled_gzip:
            decoded = min(
                decoded + len(partial), decoded_limit, aggregate_decoded_remaining
            )
        raise _BodyFailure(
            "body_incomplete", wire=wire, decoded=decoded, deterministic=True
        ) from exc
    except http.client.RemoteDisconnected as exc:
        raise _BodyFailure(
            "body_remote_disconnected", wire=wire, decoded=decoded, retryable=True
        ) from exc
    except ssl.SSLError as exc:
        classification = _classify_tls_failure(exc)
        assert classification is not None
        raise _BodyFailure(
            classification.body_outcome,
            wire=wire,
            decoded=decoded,
            retryable=classification.retryable,
        ) from exc
    except (TimeoutError, ConnectionError, OSError) as exc:
        raise _BodyFailure(
            "body_transient", wire=wire, decoded=decoded, retryable=True
        ) from exc
    except zlib.error as exc:
        raise _BodyFailure("malformed_gzip", wire=wire, decoded=decoded) from exc
    except Exception as exc:
        raise _BodyFailure(
            f"unclassified_body_failure:{type(exc).__name__}",
            wire=wire,
            decoded=decoded,
        ) from exc
    finally:
        # Raw response cleanup is an untrusted transport boundary; an ordinary close
        # failure must not replace the classified body outcome.
        with suppress(Exception):
            response.close()


def _parse_content_type(value: str | None) -> tuple[str | None, dict[str, str]]:
    if not value:
        return None, {}
    header_values = [
        item.strip() for item in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", value)
    ]
    multiple = len(header_values) != 1
    pieces = [part.strip() for part in header_values[0].split(";")]
    media = pieces[0].casefold()
    params: dict[str, str] = {}
    for header_value in header_values:
        current = [part.strip() for part in header_value.split(";")]
        if current[0].casefold() != media:
            multiple = True
        for item in current[1:]:
            if not item:
                continue
            if "=" not in item:
                key, normalized = item.casefold(), ""
            else:
                key, raw = item.split("=", 1)
                key, normalized = (
                    key.strip().casefold(),
                    raw.strip().strip('"').casefold(),
                )
            if key in params:
                params[key] = f"{params[key]}, {normalized}"
                multiple = True
            else:
                params[key] = normalized
    if multiple:
        params["parse_error"] = "multiple_or_conflicting_content_type"
    return media, params


@dataclass
class _FetchResult:
    outcome: str
    final_url: str | None = None
    body: bytes = b""
    sha256: str | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class _PolicyPreflightFailure:
    classification: str
    reason: str
    evidence_outcome: str


@dataclass
class _MutableBudgetUsage:
    http_requests: int = 0
    robots_wire_bytes: int = 0
    robots_decoded_bytes: int = 0
    sitemap_wire_bytes: int = 0
    sitemap_decoded_bytes: int = 0
    sitemap_document_occurrences: int = 0
    url_occurrences: int = 0


@dataclass
class _CountedOccurrence:
    source: str
    source_origin: str
    counts_sitemap_document: bool
    queue_ordinal: int | None = None
    parent_sha256: str | None = None
    entry_ordinal: int | None = None


@dataclass
class _RunState:
    budgets: DiagnosticBudgets
    identity: DiagnosticIdentity
    allowed_domains: set[str]
    allowed_origins: set[tuple[str, str, int]]
    transport: DiagnosticTransport
    now: Callable[[], datetime]
    usage: _MutableBudgetUsage = field(default_factory=_MutableBudgetUsage)
    attempts: list[DiagnosticAttempt] = field(default_factory=list)
    safety_errors: list[str] = field(default_factory=list)
    deterministic_errors: list[str] = field(default_factory=list)
    final_transient_errors: list[str] = field(default_factory=list)
    truncations: list[str] = field(default_factory=list)
    counted_occurrences: list[_CountedOccurrence] = field(default_factory=list)
    robots_errors: list[RobotsErrorEvidence] = field(default_factory=list)
    request_budget_exhausted: bool = False

    def gate_url(
        self, url: str, *, previous: str | None = None
    ) -> tuple[str, NormalizedOrigin]:
        normalized, origin = normalize_http_url(url)
        key = (origin.scheme, origin.host, origin.effective_port)
        if key not in self.allowed_origins or not _domain_allowed(
            origin.host, self.allowed_domains
        ):
            raise SiteDiagnosticError("document URL origin is not exactly approved")
        if previous and not redirect_transition_allowed(previous, normalized):
            raise SiteDiagnosticError("HTTPS to HTTP redirect is forbidden")
        return normalized, origin

    def request_slot(self) -> int | None:
        if self.usage.http_requests >= self.budgets.http_requests:
            self.truncations.append("http_request_budget_exhausted")
            self.request_budget_exhausted = True
            return None
        self.usage.http_requests += 1
        return self.usage.http_requests


def _scheduled_rejection_slot(
    state: _RunState, intended_reason: str
) -> tuple[int | None, str]:
    """Reserve a non-network scheduling slot or emit a closed budget disposition."""
    if state.request_budget_exhausted:
        return None, "prior_budget_stop"
    request_slot_ordinal = state.request_slot()
    if request_slot_ordinal is None:
        return None, "http_request_budget_exhausted"
    return request_slot_ordinal, intended_reason


def _record_robots_error(state: _RunState, *, source_origin: str, reason: str) -> None:
    initiating_url = source_origin.removesuffix("/") + "/robots.txt"
    attempt = next(
        (
            item
            for item in reversed(state.attempts)
            if item.document_kind == "robots"
            and (item.redirect_chain[0] if item.redirect_chain else item.requested_url)
            == initiating_url
            and item.outcome == "success"
            and item.content_sha256 is not None
        ),
        None,
    )
    if attempt is None or attempt.content_sha256 is None:
        raise SiteDiagnosticError(
            "robots parser error lacks successful attempt evidence"
        )
    state.robots_errors.append(
        RobotsErrorEvidence(
            attempt_ordinal=attempt.attempt_ordinal,
            source_origin=source_origin,
            document_sha256=attempt.content_sha256,
            reason=reason,
        )
    )


def _consume_sitemap_occurrences(
    state: _RunState,
    lines: Iterable[int],
    *,
    source_origin: str,
    parent_sha256: str | None,
    source: str = "robots_sitemap",
    queue_ordinal: int | None = None,
) -> int:
    consumed = 0
    for line_number in lines:
        if state.usage.sitemap_document_occurrences >= state.budgets.sitemap_documents:
            state.truncations.append("sitemap_document_budget_exhausted")
            break
        if state.usage.url_occurrences >= state.budgets.url_occurrences:
            state.truncations.append("url_occurrence_budget_exhausted")
            break
        state.usage.sitemap_document_occurrences += 1
        state.usage.url_occurrences += 1
        state.counted_occurrences.append(
            _CountedOccurrence(
                source=source,
                source_origin=source_origin,
                counts_sitemap_document=True,
                queue_ordinal=queue_ordinal,
                parent_sha256=parent_sha256,
                entry_ordinal=line_number,
            )
        )
        consumed += 1
    return consumed


def _attempt(
    state: _RunState,
    *,
    url: str,
    kind: str,
    queue_ordinal: int | None,
    retry_ordinal: int,
    request_slot_ordinal: int,
    redirect_ordinal: int,
    redirect_chain: list[str],
    parent_sha256: str | None,
    response: RawHttpResponse | None,
    body: _BodyResult | None,
    outcome: str,
    fetched_at: datetime,
    redirect_target_url: str | None = None,
) -> None:
    headers = response.headers if response else {}
    media, parameters = _parse_content_type(_header(headers, "Content-Type"))
    state.attempts.append(
        DiagnosticAttempt(
            attempt_ordinal=len(state.attempts) + 1,
            request_slot_ordinal=request_slot_ordinal,
            queue_ordinal=queue_ordinal,
            retry_ordinal=retry_ordinal,
            redirect_ordinal=redirect_ordinal,
            document_kind=kind,  # type: ignore[arg-type]
            parent_sha256=parent_sha256,
            requested_url=url,
            redirect_chain=list(redirect_chain),
            redirect_target_url=redirect_target_url,
            final_url=url if response else None,
            http_status=response.status if response else None,
            fetched_at=fetched_at,
            media_type=media,
            content_type_parameters=parameters,
            content_encoding=_header(headers, "Content-Encoding"),
            wire_bytes=body.wire if body else 0,
            decoded_bytes=body.decoded if body else 0,
            content_sha256=hashlib.sha256(body.body).hexdigest()
            if body and body.complete
            else None,
            outcome=outcome,
            actual_user_agent=state.identity.user_agent,
            product_token=state.identity.product_token,
            identity_sha256=state.identity.identity_sha256,
        )
    )


def _fetch_document(
    state: _RunState,
    url: str,
    *,
    kind: str,
    queue_ordinal: int | None,
    parent_sha256: str | None,
    redirect_policy_gate: Callable[[str], _PolicyPreflightFailure | None] | None = None,
) -> _FetchResult:
    current = url
    redirects: list[str] = []
    retry = 0
    redirect_count = 0
    document_wire = 0
    document_decoded = 0
    last_redirect: _FetchResult | None = None
    while True:
        if kind == "sitemap" and (
            document_wire >= state.budgets.sitemap_wire_bytes_per_document
            or document_decoded >= state.budgets.sitemap_decoded_bytes_per_document
            or state.usage.sitemap_wire_bytes >= state.budgets.sitemap_wire_bytes_total
            or state.usage.sitemap_decoded_bytes
            >= state.budgets.sitemap_decoded_bytes_total
        ):
            state.truncations.append("sitemap_byte_budget_exhausted")
            if last_redirect is not None:
                return _FetchResult(
                    "budget",
                    last_redirect.final_url,
                    last_redirect.body,
                    last_redirect.sha256,
                    last_redirect.fetched_at,
                )
            return _FetchResult("budget")
        if kind == "robots" and (
            state.usage.robots_wire_bytes >= state.budgets.robots_wire_bytes_total
            or state.usage.robots_decoded_bytes
            >= state.budgets.robots_decoded_bytes_total
        ):
            state.safety_errors.append("robots_aggregate_byte_budget_exhausted")
            if last_redirect is not None:
                return _FetchResult(
                    "safety",
                    last_redirect.final_url,
                    last_redirect.body,
                    last_redirect.sha256,
                    last_redirect.fetched_at,
                )
            return _FetchResult("safety")
        try:
            current, _ = state.gate_url(
                current, previous=redirects[-1] if redirects else None
            )
        except SiteDiagnosticError as exc:
            state.safety_errors.append(f"authority:{exc}")
            if last_redirect is not None:
                return _FetchResult(
                    "safety",
                    last_redirect.final_url,
                    last_redirect.body,
                    last_redirect.sha256,
                    last_redirect.fetched_at,
                )
            return _FetchResult("safety")
        if kind == "sitemap" and redirect_count and redirect_policy_gate is not None:
            policy_failure = redirect_policy_gate(current)
            if policy_failure is not None:
                if last_redirect is None:
                    return _FetchResult(policy_failure.evidence_outcome)
                return _FetchResult(
                    policy_failure.evidence_outcome,
                    last_redirect.final_url,
                    last_redirect.body,
                    last_redirect.sha256,
                    last_redirect.fetched_at,
                )
        request_slot_ordinal = state.request_slot()
        if request_slot_ordinal is None:
            return _FetchResult("request_budget")
        fetched_at = state.now()
        try:
            response = state.transport.request(
                current,
                user_agent=state.identity.user_agent,
                identity_sha256=state.identity.identity_sha256,
            )
        except TransportFailure as exc:
            outcome = f"transport_{exc.kind}"
            _attempt(
                state,
                url=current,
                kind=kind,
                queue_ordinal=queue_ordinal,
                retry_ordinal=retry,
                request_slot_ordinal=request_slot_ordinal,
                redirect_ordinal=redirect_count,
                redirect_chain=redirects,
                parent_sha256=parent_sha256,
                response=None,
                body=None,
                outcome=outcome,
                fetched_at=fetched_at,
            )
            retryable_outcome = is_retryable_attempt_outcome(outcome)
            if (
                exc.safety
                or exc.deterministic
                or not (exc.retryable and retryable_outcome)
            ):
                if exc.deterministic:
                    state.deterministic_errors.append(f"transport:{exc.kind}")
                    return _FetchResult("deterministic")
                state.safety_errors.append(f"transport:{exc.kind}")
                return _FetchResult("safety")
            if retry < 2:
                retry += 1
                continue
            state.final_transient_errors.append(f"transport:{exc.kind}")
            return _FetchResult("transient")
        except Exception:  # noqa: BLE001 - untrusted transport boundary
            _attempt(
                state,
                url=current,
                kind=kind,
                queue_ordinal=queue_ordinal,
                retry_ordinal=retry,
                request_slot_ordinal=request_slot_ordinal,
                redirect_ordinal=redirect_count,
                redirect_chain=redirects,
                parent_sha256=parent_sha256,
                response=None,
                body=None,
                outcome="unclassified_transport",
                fetched_at=fetched_at,
            )
            state.safety_errors.append("unclassified_transport")
            return _FetchResult("safety")

        status = response.status
        status_class = classify_http_status(status)
        if status_class != "body":
            # Governed terminal status lines take precedence over adversarial or
            # stalled bodies. Only 2xx responses require body classification.
            empty_body = _BodyResult(b"", 0, 0)
            digest = hashlib.sha256(b"").hexdigest()
            # Preserve the governed status classification if an untrusted close hook
            # raises an ordinary exception.
            with suppress(Exception):
                response.close()
            if status_class == "authority":
                _attempt(
                    state,
                    url=current,
                    kind=kind,
                    queue_ordinal=queue_ordinal,
                    retry_ordinal=retry,
                    request_slot_ordinal=request_slot_ordinal,
                    redirect_ordinal=redirect_count,
                    redirect_chain=redirects,
                    parent_sha256=parent_sha256,
                    response=response,
                    body=empty_body,
                    outcome="authority_http",
                    fetched_at=fetched_at,
                )
                state.safety_errors.append(f"http:{status}")
                return _FetchResult("safety", current, b"", digest, fetched_at)
            if status_class == "empty":
                _attempt(
                    state,
                    url=current,
                    kind=kind,
                    queue_ordinal=queue_ordinal,
                    retry_ordinal=retry,
                    request_slot_ordinal=request_slot_ordinal,
                    redirect_ordinal=redirect_count,
                    redirect_chain=redirects,
                    parent_sha256=parent_sha256,
                    response=response,
                    body=empty_body,
                    outcome="completed_empty",
                    fetched_at=fetched_at,
                )
                return _FetchResult("empty", current, b"", digest, fetched_at)
            if status_class == "redirect":
                location = _header(response.headers, "Location")
                if not location:
                    _attempt(
                        state,
                        url=current,
                        kind=kind,
                        queue_ordinal=queue_ordinal,
                        retry_ordinal=retry,
                        request_slot_ordinal=request_slot_ordinal,
                        redirect_ordinal=redirect_count,
                        redirect_chain=redirects,
                        parent_sha256=parent_sha256,
                        response=response,
                        body=empty_body,
                        outcome="redirect_missing_location",
                        fetched_at=fetched_at,
                    )
                    state.deterministic_errors.append("redirect_missing_location")
                    return _FetchResult(
                        "deterministic", current, b"", digest, fetched_at
                    )
                try:
                    joined = urljoin(current, location)
                    target, _ = normalize_http_url(joined)
                except (SiteDiagnosticError, ValueError):
                    _attempt(
                        state,
                        url=current,
                        kind=kind,
                        queue_ordinal=queue_ordinal,
                        retry_ordinal=retry,
                        request_slot_ordinal=request_slot_ordinal,
                        redirect_ordinal=redirect_count,
                        redirect_chain=redirects,
                        parent_sha256=parent_sha256,
                        response=response,
                        body=empty_body,
                        outcome="redirect_malformed_location",
                        fetched_at=fetched_at,
                    )
                    state.deterministic_errors.append("redirect_malformed_location")
                    return _FetchResult(
                        "deterministic", current, b"", digest, fetched_at
                    )
                try:
                    target, _ = state.gate_url(target, previous=current)
                except SiteDiagnosticError:
                    _attempt(
                        state,
                        url=current,
                        kind=kind,
                        queue_ordinal=queue_ordinal,
                        retry_ordinal=retry,
                        request_slot_ordinal=request_slot_ordinal,
                        redirect_ordinal=redirect_count,
                        redirect_chain=redirects,
                        parent_sha256=parent_sha256,
                        response=response,
                        body=empty_body,
                        outcome="redirect_authority_failure",
                        fetched_at=fetched_at,
                    )
                    state.safety_errors.append("redirect_authority_failure")
                    return _FetchResult("safety", current, b"", digest, fetched_at)
                _attempt(
                    state,
                    url=current,
                    kind=kind,
                    queue_ordinal=queue_ordinal,
                    retry_ordinal=retry,
                    request_slot_ordinal=request_slot_ordinal,
                    redirect_ordinal=redirect_count,
                    redirect_chain=redirects,
                    parent_sha256=parent_sha256,
                    response=response,
                    body=empty_body,
                    outcome="redirect",
                    fetched_at=fetched_at,
                    redirect_target_url=target,
                )
                if redirect_count >= state.budgets.redirect_hops_per_document:
                    state.truncations.append("redirect_hop_budget_exhausted")
                    return _FetchResult("budget", current, b"", digest, fetched_at)
                redirects.append(current)
                last_redirect = _FetchResult(
                    "redirect", current, b"", digest, fetched_at
                )
                current = target
                redirect_count += 1
                continue
            if status_class == "transient":
                _attempt(
                    state,
                    url=current,
                    kind=kind,
                    queue_ordinal=queue_ordinal,
                    retry_ordinal=retry,
                    request_slot_ordinal=request_slot_ordinal,
                    redirect_ordinal=redirect_count,
                    redirect_chain=redirects,
                    parent_sha256=parent_sha256,
                    response=response,
                    body=empty_body,
                    outcome="transient_http",
                    fetched_at=fetched_at,
                )
                if retry < 2:
                    retry += 1
                    continue
                state.final_transient_errors.append(f"http:{status}")
                return _FetchResult("transient", current, b"", digest, fetched_at)
            if status_class == "informational":
                outcome, result = "final_informational", "deterministic"
                state.deterministic_errors.append(f"http:{status}")
            elif status_class == "terminal":
                outcome, result = "terminal_http", "deterministic"
                state.deterministic_errors.append(f"http:{status}")
            else:
                outcome, result = "unclassified_http", "safety"
                state.safety_errors.append(f"http:{status}")
            _attempt(
                state,
                url=current,
                kind=kind,
                queue_ordinal=queue_ordinal,
                retry_ordinal=retry,
                request_slot_ordinal=request_slot_ordinal,
                redirect_ordinal=redirect_count,
                redirect_chain=redirects,
                parent_sha256=parent_sha256,
                response=response,
                body=empty_body,
                outcome=outcome,
                fetched_at=fetched_at,
            )
            return _FetchResult(result, current, b"", digest, fetched_at)

        if kind == "robots":
            wire_limit = state.budgets.robots_wire_bytes_per_attempt
            decoded_limit = state.budgets.robots_decoded_bytes_per_attempt
            agg_wire = (
                state.budgets.robots_wire_bytes_total - state.usage.robots_wire_bytes
            )
            agg_decoded = (
                state.budgets.robots_decoded_bytes_total
                - state.usage.robots_decoded_bytes
            )
        else:
            wire_limit = max(
                0, state.budgets.sitemap_wire_bytes_per_document - document_wire
            )
            decoded_limit = max(
                0, state.budgets.sitemap_decoded_bytes_per_document - document_decoded
            )
            agg_wire = (
                state.budgets.sitemap_wire_bytes_total - state.usage.sitemap_wire_bytes
            )
            agg_decoded = (
                state.budgets.sitemap_decoded_bytes_total
                - state.usage.sitemap_decoded_bytes
            )
        try:
            body = _read_bounded_body(
                response,
                url=current,
                wire_limit=wire_limit,
                decoded_limit=decoded_limit,
                aggregate_wire_remaining=max(0, agg_wire),
                aggregate_decoded_remaining=max(0, agg_decoded),
            )
        except _BodyFailure as exc:
            if kind == "robots":
                state.usage.robots_wire_bytes += exc.wire
                state.usage.robots_decoded_bytes += exc.decoded
            else:
                state.usage.sitemap_wire_bytes += exc.wire
                state.usage.sitemap_decoded_bytes += exc.decoded
                document_wire += exc.wire
                document_decoded += exc.decoded
            partial = _BodyResult(b"", exc.wire, exc.decoded, complete=False)
            _attempt(
                state,
                url=current,
                kind=kind,
                queue_ordinal=queue_ordinal,
                retry_ordinal=retry,
                request_slot_ordinal=request_slot_ordinal,
                redirect_ordinal=redirect_count,
                redirect_chain=redirects,
                parent_sha256=parent_sha256,
                response=response,
                body=partial,
                outcome=exc.reason,
                fetched_at=fetched_at,
            )
            # Robots over-limit and all compression/parser-signal failures are
            # priority-1; sitemap byte exhaustion is a budget truncation.
            if "budget_exhausted" in exc.reason and kind == "sitemap":
                state.truncations.append(exc.reason)
                return _FetchResult("budget", current, fetched_at=fetched_at)
            if exc.retryable and is_retryable_attempt_outcome(exc.reason):
                if retry < 2:
                    retry += 1
                    continue
                state.final_transient_errors.append(exc.reason)
                return _FetchResult("transient", current, fetched_at=fetched_at)
            if exc.deterministic:
                state.deterministic_errors.append(exc.reason)
                return _FetchResult("deterministic", current, fetched_at=fetched_at)
            state.safety_errors.append(exc.reason)
            return _FetchResult("safety", current, fetched_at=fetched_at)
        if kind == "robots":
            state.usage.robots_wire_bytes += body.wire
            state.usage.robots_decoded_bytes += body.decoded
        else:
            state.usage.sitemap_wire_bytes += body.wire
            state.usage.sitemap_decoded_bytes += body.decoded
            document_wire += body.wire
            document_decoded += body.decoded

        digest = hashlib.sha256(body.body).hexdigest()
        media, parameters = _parse_content_type(
            _header(response.headers, "Content-Type")
        )
        if kind == "robots" and (
            "parse_error" in parameters
            or media != "text/plain"
            or parameters.get("charset", "utf-8") not in {"utf-8", "utf8"}
        ):
            outcome = "robots_unsupported_mime_or_charset"
            state.safety_errors.append(outcome)
            result = "safety"
        elif kind == "sitemap" and (
            "parse_error" in parameters
            or parameters.get("charset", "utf-8") not in {"utf-8", "utf8"}
            or (
                media
                not in {
                    "application/xml",
                    "text/xml",
                    "application/gzip",
                    "application/x-gzip",
                }
                and not (body.compressed and media == "application/octet-stream")
            )
        ):
            outcome = "sitemap_unsupported_mime"
            state.safety_errors.append(outcome)
            result = "safety"
        else:
            outcome = "success"
            result = "success"
        _attempt(
            state,
            url=current,
            kind=kind,
            queue_ordinal=queue_ordinal,
            retry_ordinal=retry,
            request_slot_ordinal=request_slot_ordinal,
            redirect_ordinal=redirect_count,
            redirect_chain=redirects,
            parent_sha256=parent_sha256,
            response=response,
            body=body,
            outcome=outcome,
            fetched_at=fetched_at,
        )
        return _FetchResult(result, current, body.body, digest, fetched_at)


def _safe_xml_locations(
    body: bytes, *, max_locations: int, max_sitemap_locations: int
) -> tuple[str, list[str], bool, tuple[int, str, str] | None]:
    try:
        xml_text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SiteDiagnosticError("unsafe_xml_encoding") from exc
    if "\x00" in xml_text:
        raise SiteDiagnosticError("unsafe_xml_encoding")
    declaration = re.match(r"^\ufeff?<\?xml\s+([^?]+)\?>", xml_text, re.IGNORECASE)
    if declaration:
        encoding = re.search(
            r"\bencoding\s*=\s*(['\"])([^'\"]+)\1", declaration.group(1), re.IGNORECASE
        )
        if encoding and encoding.group(2).casefold().replace("_", "-") not in {
            "utf-8",
            "utf8",
        }:
            raise SiteDiagnosticError("unsafe_xml_encoding")
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise SiteDiagnosticError("unsafe_xml_entity_or_doctype")
    root_type: str | None = None
    locations: list[str] = []
    stack: list[str] = []
    root_namespace: str | None = None
    truncated = False
    entry_loc_count = 0
    entry_ordinal = 0
    overflow_location: tuple[int, str, str] | None = None
    try:
        for event, element in ElementTree.iterparse(
            io.BytesIO(body), events=("start", "end")
        ):
            tag = element.tag
            if not isinstance(tag, str):
                raise SiteDiagnosticError("unsafe_xml_node")
            if tag.startswith("{"):
                namespace, local = tag[1:].split("}", 1)
            else:
                namespace, local = "", tag
            if local == "include" or "xinclude" in namespace.casefold():
                raise SiteDiagnosticError("unsafe_xml_xinclude")
            if event == "start":
                stack.append(local)
                if root_type is None:
                    if local not in {"sitemapindex", "urlset"}:
                        raise SiteDiagnosticError("unsafe_xml_root")
                    root_type = local
                    root_namespace = namespace
                    if root_namespace not in {"", SITEMAP_NAMESPACE}:
                        raise SiteDiagnosticError("unsupported_xml_namespace")
                elif namespace != root_namespace:
                    raise SiteDiagnosticError("mixed_xml_namespace")
                else:
                    expected_entry = "sitemap" if root_type == "sitemapindex" else "url"
                    allowed_fields = (
                        {"loc", "lastmod"}
                        if root_type == "sitemapindex"
                        else {"loc", "lastmod", "changefreq", "priority"}
                    )
                    if len(stack) == 2:
                        if local != expected_entry:
                            raise SiteDiagnosticError("unsafe_xml_structure")
                        entry_ordinal += 1
                        entry_loc_count = 0
                    elif len(stack) == 3:
                        if stack[-2] != expected_entry or local not in allowed_fields:
                            raise SiteDiagnosticError("unsafe_xml_structure")
                        if local == "loc":
                            entry_loc_count += 1
                            if entry_loc_count > 1:
                                raise SiteDiagnosticError("unsafe_xml_structure")
                    elif len(stack) > 3:
                        raise SiteDiagnosticError("unsafe_xml_structure")
            else:
                if local == "loc" and len(stack) == 3:
                    expected_parent = (
                        "sitemap" if root_type == "sitemapindex" else "url"
                    )
                    if stack[-2] == expected_parent and stack[0] == root_type:
                        raw_location = (element.text or "").strip()
                        if len(locations) >= max_locations:
                            truncated = True
                            if overflow_location is None:
                                overflow_location = (
                                    entry_ordinal,
                                    raw_location,
                                    "url_occurrence_budget_exhausted",
                                )
                            break
                        if (
                            root_type == "sitemapindex"
                            and len(locations) >= max_sitemap_locations
                        ):
                            if overflow_location is None:
                                overflow_location = (
                                    entry_ordinal,
                                    raw_location,
                                    "sitemap_document_budget_exhausted",
                                )
                            break
                        else:
                            locations.append(raw_location)
                if element.tail and element.tail.strip():
                    raise SiteDiagnosticError("unsafe_xml_structure")
                if len(stack) in {1, 2} and element.text and element.text.strip():
                    raise SiteDiagnosticError("unsafe_xml_structure")
                if len(stack) == 2:
                    expected_entry = "sitemap" if root_type == "sitemapindex" else "url"
                    if local == expected_entry and entry_loc_count != 1:
                        raise SiteDiagnosticError("unsafe_xml_structure")
                stack.pop()
                element.clear()
    except ElementTree.ParseError as exc:
        raise SyntaxError("xml_syntax_error") from exc
    if root_type is None:
        raise SyntaxError("xml_syntax_error")
    return root_type, locations, truncated, overflow_location


def _identity(
    identity_id: str, user_agent: str, product_token: str
) -> DiagnosticIdentity:
    identity_id = identity_id.strip()
    user_agent = user_agent.strip()
    product_token = product_token.strip()
    if (
        not identity_id
        or not user_agent
        or any(ord(char) < 32 or ord(char) == 127 for char in identity_id + user_agent)
        or not PRODUCT_TOKEN.fullmatch(product_token)
    ):
        raise SiteDiagnosticError("invalid diagnostic identity")
    if product_token.casefold() not in user_agent.casefold():
        raise SiteDiagnosticError("product token is absent from actual User-Agent")
    payload = {
        "identity_id": identity_id,
        "product_token": product_token,
        "user_agent": user_agent,
    }
    return DiagnosticIdentity(**payload, identity_sha256=canonical_sha256(payload))


def _origin_key(origin: NormalizedOrigin) -> tuple[str, str, int]:
    return origin.scheme, origin.host, origin.effective_port


def build_origin_policy_evidence(
    *,
    origin: NormalizedOrigin,
    robots: ParsedRobots,
    robots_sha256: str,
    robots_status: str,
    identity: DiagnosticIdentity,
    fetched_at: datetime,
    expires_at: datetime,
) -> OriginPolicyEvidence:
    """Build the shared digest-bound evidence for one robots observation."""
    if robots_status not in {"available", "absent"}:
        raise SiteDiagnosticError("invalid robots policy evidence status")
    selected_rules = [
        RobotsPolicyRule(
            allow=rule.allow, pattern=rule.pattern, line_number=rule.line_number
        )
        for rule in robots.selected_rules()
    ]
    policy_payload = {
        "origin": origin.model_dump(mode="json"),
        "robots_sha256": robots_sha256,
        "selected_rules": [rule.model_dump(mode="json") for rule in selected_rules],
        "identity_sha256": identity.identity_sha256,
    }
    digest = canonical_sha256(policy_payload)
    return OriginPolicyEvidence(
        origin=origin,
        policy_id=f"robots-policy-{digest[:16]}",
        policy_sha256=digest,
        robots_status=robots_status,
        robots_sha256=robots_sha256,
        selected_rules=selected_rules,
        declared_sitemaps=robots.sitemaps,
        warnings=robots.warnings,
        fetched_at=fetched_at,
        expires_at=expires_at,
        identity_id=identity.identity_id,
        identity_sha256=identity.identity_sha256,
    )


def _policy_evidence(
    origin: NormalizedOrigin,
    robots: ParsedRobots,
    result: _FetchResult,
    identity: DiagnosticIdentity,
    completed_at: datetime,
    freshness: timedelta,
) -> OriginPolicyEvidence:
    fetched_at = result.fetched_at or completed_at
    assert result.sha256 is not None
    return build_origin_policy_evidence(
        origin=origin,
        robots=robots,
        robots_sha256=result.sha256,
        robots_status="available" if result.outcome == "success" else "absent",
        identity=identity,
        fetched_at=fetched_at,
        expires_at=fetched_at + freshness,
    )


def diagnose_site(
    *,
    requested_url: str,
    site_key: str,
    allowed_domains: Iterable[str],
    allowed_document_origins: Iterable[str],
    user_agent: str,
    product_token: str,
    identity_id: str,
    transport: DiagnosticTransport | None = None,
    budgets: DiagnosticBudgets | None = None,
    freshness: timedelta = DEFAULT_FRESHNESS,
    now: Callable[[], datetime] | None = None,
) -> SiteDiagnostic:
    """Produce robots/sitemap planning evidence without fetching a page seed."""
    clock = now or (lambda: datetime.now(UTC))
    started_at = clock()
    if started_at.tzinfo is None:
        raise SiteDiagnosticError("diagnostic clock must be timezone-aware")
    if (
        not site_key.strip()
        or site_key != site_key.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in site_key)
    ):
        raise SiteDiagnosticError("site_key is required")
    if freshness <= timedelta(0) or freshness > DEFAULT_FRESHNESS:
        raise SiteDiagnosticError(
            "freshness must be greater than zero and at most 24 hours"
        )
    identity = _identity(identity_id, user_agent, product_token)
    requested_normalized, requested_origin = normalize_http_url(requested_url)
    canonical_origin = requested_origin

    domains = []
    for item in allowed_domains:
        domain = _normalize_host(str(item).strip())
        if _is_ip_literal(domain) and not _is_public(domain):
            raise SiteDiagnosticError("allowed IP literals must be public")
        if domain in domains:
            raise SiteDiagnosticError(
                "allowed_domains must be unique after normalization"
            )
        domains.append(domain)
    if not domains:
        raise SiteDiagnosticError("allowed_domains must be non-empty")
    origins: list[NormalizedOrigin] = []
    for item in allowed_document_origins:
        origin = normalize_origin(str(item).strip())
        if _is_ip_literal(origin.host) and not _is_public(origin.host):
            raise SiteDiagnosticError(
                "allowed document origin IP literals must be public"
            )
        if _origin_key(origin) in {_origin_key(row) for row in origins}:
            raise SiteDiagnosticError(
                "allowed_document_origins must be unique after normalization"
            )
        origins.append(origin)
    if not origins:
        raise SiteDiagnosticError("allowed_document_origins must be non-empty")
    if _origin_key(canonical_origin) not in {_origin_key(item) for item in origins}:
        raise SiteDiagnosticError("canonical origin is not exactly approved")
    if not _domain_allowed(canonical_origin.host, set(domains)):
        raise SiteDiagnosticError("canonical host is outside allowed_domains")
    for origin in origins:
        if not _domain_allowed(origin.host, set(domains)):
            raise SiteDiagnosticError(
                "approved document origin is outside allowed_domains"
            )

    state = _RunState(
        budgets=budgets or DiagnosticBudgets(),
        identity=identity,
        allowed_domains=set(domains),
        allowed_origins={_origin_key(item) for item in origins},
        transport=transport or SafePinnedTransport(),
        now=clock,
    )
    origin_policies: dict[
        tuple[str, str, int], tuple[ParsedRobots, OriginPolicyEvidence]
    ] = {}
    canonical_source_origin = canonical_origin.as_url_origin() + "/"
    canonical_robots_url = canonical_source_origin + "robots.txt"
    robots_result = _fetch_document(
        state,
        canonical_robots_url,
        kind="robots",
        queue_ordinal=None,
        parent_sha256=None,
    )
    directives: list[SitemapDirective] = []
    counted_directives = 0
    canonical_robots = ParsedRobots(product_token, [], [], [], [], 0)

    if robots_result.outcome == "success":
        try:
            text = _decode_robots_utf8(robots_result.body)
        except UnicodeError:
            state.safety_errors.append("robots_invalid_utf8")
            _record_robots_error(
                state,
                source_origin=canonical_source_origin,
                reason="robots_invalid_utf8",
            )
        else:
            if _looks_like_html(text):
                state.safety_errors.append("robots_html_response")
                _record_robots_error(
                    state,
                    source_origin=canonical_source_origin,
                    reason="robots_html_response",
                )
            else:
                canonical_robots = parse_robots(text, product_token=product_token)
                counted_directives = _consume_sitemap_occurrences(
                    state,
                    canonical_robots.sitemap_occurrence_lines,
                    source_origin=canonical_source_origin,
                    parent_sha256=robots_result.sha256,
                )
                if canonical_robots.errors:
                    state.safety_errors.extend(canonical_robots.errors)
                    for reason in canonical_robots.errors:
                        _record_robots_error(
                            state,
                            source_origin=canonical_source_origin,
                            reason=reason,
                        )
                directives = canonical_robots.sitemaps
                if not canonical_robots.errors:
                    evidence = _policy_evidence(
                        canonical_origin,
                        canonical_robots,
                        robots_result,
                        identity,
                        clock(),
                        freshness,
                    )
                    origin_policies[_origin_key(canonical_origin)] = (
                        canonical_robots,
                        evidence,
                    )
    elif robots_result.outcome == "empty":
        evidence = _policy_evidence(
            canonical_origin,
            canonical_robots,
            robots_result,
            identity,
            clock(),
            freshness,
        )
        origin_policies[_origin_key(canonical_origin)] = (canonical_robots, evidence)

    rejected: list[RejectedUrl] = []
    duplicates: list[RejectedUrl] = []
    queue: deque[tuple[int, str, int, str | None, int | None]] = deque()
    queue_counter = 0
    if state.safety_errors and directives:
        for directive in directives:
            queue_counter += 1
            counted_occurrence: _CountedOccurrence | None = None
            for occurrence in state.counted_occurrences:
                if (
                    occurrence.source == "robots_sitemap"
                    and occurrence.parent_sha256 == robots_result.sha256
                    and occurrence.entry_ordinal == directive.line_number
                ):
                    occurrence.queue_ordinal = queue_counter
                    counted_occurrence = occurrence
                    break
            reason = "prior_safety_stop"
            if counted_occurrence is None and state.truncations:
                reason = (
                    "sitemap_document_budget_exhausted"
                    if "sitemap_document_budget_exhausted" in state.truncations
                    else "url_occurrence_budget_exhausted"
                )
            request_slot_ordinal, reason = _scheduled_rejection_slot(state, reason)
            rejected.append(
                RejectedUrl(
                    url=directive.url,
                    reason=reason,
                    queue_ordinal=queue_counter,
                    parent_sha256=robots_result.sha256,
                    entry_ordinal=directive.line_number,
                    request_slot_ordinal=request_slot_ordinal,
                )
            )
    if not state.safety_errors and robots_result.outcome in {"success", "empty"}:
        candidates = [
            (item.url, 0, robots_result.sha256, item.line_number)
            for item in directives[:counted_directives]
        ]
        if not directives:
            candidates = [
                (
                    canonical_origin.as_url_origin() + "/sitemap.xml",
                    0,
                    robots_result.sha256,
                    None,
                )
            ]
        for url, depth, parent, entry in candidates:
            # Canonical robots Sitemap occurrences were counted before validation;
            # only the deterministic fallback needs an occurrence here.
            if not directives:
                if (
                    state.usage.sitemap_document_occurrences
                    >= state.budgets.sitemap_documents
                ):
                    state.truncations.append("sitemap_document_budget_exhausted")
                    break
                state.usage.sitemap_document_occurrences += 1
                if state.usage.url_occurrences >= state.budgets.url_occurrences:
                    state.truncations.append("url_occurrence_budget_exhausted")
                    break
                state.usage.url_occurrences += 1
            queue_counter += 1
            if directives:
                occurrence = next(
                    item
                    for item in state.counted_occurrences
                    if item.source == "robots_sitemap"
                    and item.parent_sha256 == robots_result.sha256
                    and item.entry_ordinal == entry
                )
                occurrence.queue_ordinal = queue_counter
            else:
                state.counted_occurrences.append(
                    _CountedOccurrence(
                        source="fallback",
                        source_origin=canonical_origin.as_url_origin() + "/",
                        counts_sitemap_document=True,
                        queue_ordinal=queue_counter,
                        parent_sha256=robots_result.sha256,
                    )
                )
            queue.append((queue_counter, url, depth, parent, entry))
        if directives and counted_directives < len(directives):
            reason = (
                "sitemap_document_budget_exhausted"
                if state.usage.sitemap_document_occurrences
                >= state.budgets.sitemap_documents
                else "url_occurrence_budget_exhausted"
            )
            for directive in directives[counted_directives:]:
                request_slot_ordinal, disposition_reason = _scheduled_rejection_slot(
                    state, reason
                )
                queue_counter += 1
                rejected.append(
                    RejectedUrl(
                        url=directive.url,
                        reason=disposition_reason,
                        queue_ordinal=queue_counter,
                        parent_sha256=robots_result.sha256,
                        entry_ordinal=directive.line_number,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )

    accepted: list[str] = []
    accepted_evidence: list[AcceptedPageEvidence] = []
    accepted_set: set[str] = set()
    sitemap_evidence: list[SitemapEvidence] = []
    seen_scheduled_document_urls: set[str] = set()
    seen_observed_document_urls: set[str] = set()
    seen_document_digests: set[str] = set()
    cross_origin_pages = False
    canonical_page_candidates = 0
    robots_disallowed_page_candidates = 0

    def reject_scheduled(
        url: str,
        reason: str,
        queue_ordinal: int,
        parent_sha256: str | None,
        entry_ordinal: int | None,
    ) -> str:
        request_slot_ordinal, disposition_reason = _scheduled_rejection_slot(
            state, reason
        )
        rejected.append(
            RejectedUrl(
                url=url,
                reason=disposition_reason,
                trigger_reason=reason if disposition_reason != reason else None,
                queue_ordinal=queue_ordinal,
                parent_sha256=parent_sha256,
                entry_ordinal=entry_ordinal,
                request_slot_ordinal=request_slot_ordinal,
            )
        )
        return disposition_reason

    def ensure_sitemap_policy(
        url: str,
        queue_ordinal: int,
        parent_sha256: str | None,
        entry_ordinal: int | None,
        *,
        redirect: bool,
    ) -> _PolicyPreflightFailure | None:
        def fail_preflight(
            classification: str,
            *,
            reason: str | None = None,
            evidence_outcome: str | None = None,
        ) -> _PolicyPreflightFailure:
            intended_reason = reason or (
                f"{'redirect' if redirect else 'sitemap'}_policy_preflight_"
                f"{classification}"
            )
            disposition_reason = reject_scheduled(
                normalized_url,
                intended_reason,
                queue_ordinal,
                parent_sha256,
                entry_ordinal,
            )
            if disposition_reason != intended_reason:
                return _PolicyPreflightFailure("budget", disposition_reason, "budget")
            return _PolicyPreflightFailure(
                classification,
                intended_reason,
                evidence_outcome or classification,
            )

        normalized_url, document_origin = state.gate_url(url)
        key = _origin_key(document_origin)
        if key not in origin_policies:
            aux_url = document_origin.as_url_origin() + "/robots.txt"

            def classify_aux_preflight(
                *,
                safety_evidence: bool = False,
                budget_evidence: bool = False,
            ) -> str:
                source_attempt = next(
                    (
                        attempt
                        for attempt in reversed(state.attempts)
                        if attempt.document_kind == "robots"
                        and attempt.queue_ordinal == queue_ordinal
                        and (
                            attempt.redirect_chain[0]
                            if attempt.redirect_chain
                            else attempt.requested_url
                        )
                        == aux_url
                    ),
                    None,
                )
                classification = derive_policy_preflight_classification(
                    final_attempt_outcome=(
                        source_attempt.outcome if source_attempt is not None else None
                    ),
                    final_retry_ordinal=(
                        source_attempt.retry_ordinal
                        if source_attempt is not None
                        else None
                    ),
                    has_related_safety_evidence=safety_evidence,
                    has_related_budget_evidence=budget_evidence,
                )
                if classification is None:
                    raise SiteDiagnosticError(
                        "auxiliary robots preflight lacks classifiable evidence"
                    )
                return classification

            aux_result = _fetch_document(
                state,
                aux_url,
                kind="robots",
                queue_ordinal=queue_ordinal,
                parent_sha256=parent_sha256,
            )
            aux_robots = ParsedRobots(product_token, [], [], [], [], 0)
            truncations_before_aux = len(state.truncations)
            if aux_result.outcome == "success":
                latest = state.attempts[-1]
                if latest.media_type != "text/plain":
                    state.safety_errors.append("aux_robots_unsupported_mime")
                    _record_robots_error(
                        state,
                        source_origin=document_origin.as_url_origin() + "/",
                        reason="aux_robots_unsupported_mime",
                    )
                    return fail_preflight(classify_aux_preflight(safety_evidence=True))
                try:
                    aux_text = _decode_robots_utf8(aux_result.body)
                except UnicodeError:
                    state.safety_errors.append("aux_robots_invalid_utf8")
                    _record_robots_error(
                        state,
                        source_origin=document_origin.as_url_origin() + "/",
                        reason="aux_robots_invalid_utf8",
                    )
                    return fail_preflight(classify_aux_preflight(safety_evidence=True))
                if _looks_like_html(aux_text):
                    state.safety_errors.append("aux_robots_html_response")
                    _record_robots_error(
                        state,
                        source_origin=document_origin.as_url_origin() + "/",
                        reason="aux_robots_html_response",
                    )
                    return fail_preflight(classify_aux_preflight(safety_evidence=True))
                aux_robots = parse_robots(aux_text, product_token=product_token)
                _consume_sitemap_occurrences(
                    state,
                    aux_robots.sitemap_occurrence_lines,
                    source_origin=document_origin.as_url_origin() + "/",
                    parent_sha256=aux_result.sha256,
                    source="aux_robots_sitemap",
                    queue_ordinal=queue_ordinal,
                )
                if aux_robots.errors:
                    state.safety_errors.extend(aux_robots.errors)
                    for aux_reason in aux_robots.errors:
                        _record_robots_error(
                            state,
                            source_origin=document_origin.as_url_origin() + "/",
                            reason=aux_reason,
                        )
                    return fail_preflight(classify_aux_preflight(safety_evidence=True))
            elif aux_result.outcome != "empty":
                source_attempt = next(
                    (
                        attempt
                        for attempt in reversed(state.attempts)
                        if attempt.document_kind == "robots"
                        and attempt.queue_ordinal == queue_ordinal
                        and (
                            attempt.redirect_chain[0]
                            if attempt.redirect_chain
                            else attempt.requested_url
                        )
                        == aux_url
                    ),
                    None,
                )
                robots_byte_cap = (
                    source_attempt is not None
                    and source_attempt.outcome
                    in {"wire_budget_exhausted", "decoded_budget_exhausted"}
                ) or (
                    source_attempt is None
                    and (
                        state.usage.robots_wire_bytes
                        == state.budgets.robots_wire_bytes_total
                        or state.usage.robots_decoded_bytes
                        == state.budgets.robots_decoded_bytes_total
                    )
                )
                return fail_preflight(
                    classify_aux_preflight(
                        safety_evidence=(
                            aux_result.outcome == "safety" and not robots_byte_cap
                        ),
                        budget_evidence=(
                            aux_result.outcome in {"request_budget", "budget"}
                            or robots_byte_cap
                        ),
                    )
                )
            evidence = _policy_evidence(
                document_origin, aux_robots, aux_result, identity, clock(), freshness
            )
            origin_policies[key] = (aux_robots, evidence)
            if len(state.truncations) > truncations_before_aux:
                return fail_preflight(classify_aux_preflight(budget_evidence=True))
        policy, _ = origin_policies[key]
        if not policy.is_allowed(normalized_url):
            reason = (
                "redirect_disallowed_by_robots"
                if redirect
                else "sitemap_disallowed_by_robots"
            )
            state.safety_errors.append(reason)
            return fail_preflight(
                "safety",
                reason=reason,
                evidence_outcome=reason,
            )
        return None

    queue_stop_reason: str | None = None
    while queue and not state.safety_errors and not state.request_budget_exhausted:
        queue_ordinal, candidate, depth, parent_sha, parent_entry = queue.popleft()
        try:
            normalized_candidate, _document_origin = state.gate_url(candidate)
        except SiteDiagnosticError:
            state.safety_errors.append("document_origin_not_approved")
            request_slot_ordinal, disposition_reason = _scheduled_rejection_slot(
                state, "document_origin_not_approved"
            )
            rejected.append(
                RejectedUrl(
                    url=candidate,
                    reason=disposition_reason,
                    trigger_reason=(
                        "document_origin_not_approved"
                        if disposition_reason != "document_origin_not_approved"
                        else None
                    ),
                    queue_ordinal=queue_ordinal,
                    parent_sha256=parent_sha,
                    entry_ordinal=parent_entry,
                    request_slot_ordinal=request_slot_ordinal,
                )
            )
            break
        policy_failure = ensure_sitemap_policy(
            normalized_candidate,
            queue_ordinal,
            parent_sha,
            parent_entry,
            redirect=False,
        )
        if policy_failure is not None:
            if policy_failure.classification in {"deterministic", "transient"}:
                continue
            queue_stop_reason = (
                "prior_safety_stop"
                if policy_failure.classification == "safety"
                else "prior_budget_stop"
            )
            break
        pre_request_duplicate_reason = document_duplicate_reason(
            scheduled_initial_url=normalized_candidate,
            resolved_final_url=normalized_candidate,
            content_sha256=None,
            prior_scheduled_initial_urls=seen_scheduled_document_urls,
            prior_observed_document_urls=seen_observed_document_urls,
            prior_document_digests=seen_document_digests,
        )
        if pre_request_duplicate_reason is not None:
            request_slot_ordinal, disposition_reason = _scheduled_rejection_slot(
                state, pre_request_duplicate_reason
            )
            target = (
                duplicates
                if disposition_reason == pre_request_duplicate_reason
                else rejected
            )
            target.append(
                RejectedUrl(
                    url=normalized_candidate,
                    final_url=(
                        normalized_candidate
                        if disposition_reason == "duplicate_document_final_url"
                        else None
                    ),
                    reason=disposition_reason,
                    queue_ordinal=queue_ordinal,
                    parent_sha256=parent_sha,
                    entry_ordinal=parent_entry,
                    request_slot_ordinal=request_slot_ordinal,
                )
            )
            if state.request_budget_exhausted:
                break
            continue
        result = _fetch_document(
            state,
            normalized_candidate,
            kind="sitemap",
            queue_ordinal=queue_ordinal,
            parent_sha256=parent_sha,
            redirect_policy_gate=lambda target, queue_ordinal=queue_ordinal, parent_sha=parent_sha, parent_entry=parent_entry: (
                ensure_sitemap_policy(
                    target,
                    queue_ordinal,
                    parent_sha,
                    parent_entry,
                    redirect=True,
                )
            ),
        )
        final_queue_attempt = next(
            (
                attempt
                for attempt in reversed(state.attempts)
                if attempt.document_kind == "sitemap"
                and attempt.queue_ordinal == queue_ordinal
            ),
            None,
        )
        if result.outcome == "request_budget":
            rejection_url = normalized_candidate
            if final_queue_attempt is not None:
                rejection_url = (
                    final_queue_attempt.redirect_target_url
                    or final_queue_attempt.requested_url
                )
                sitemap_evidence.append(
                    SitemapEvidence(
                        queue_ordinal=queue_ordinal,
                        url=normalized_candidate,
                        final_url=final_queue_attempt.final_url,
                        parent_sha256=parent_sha,
                        parent_entry_ordinal=parent_entry,
                        depth=depth,
                        document_sha256=final_queue_attempt.content_sha256,
                        root_type="failed",
                        outcome=(
                            "redirect_target_budget_exhausted"
                            if final_queue_attempt.outcome == "redirect"
                            else "budget"
                        ),
                    )
                )
            rejected.append(
                RejectedUrl(
                    url=rejection_url,
                    reason="http_request_budget_exhausted",
                    queue_ordinal=queue_ordinal,
                    parent_sha256=parent_sha,
                    entry_ordinal=parent_entry,
                )
            )
            break
        if (
            result.outcome == "budget"
            and final_queue_attempt is not None
            and final_queue_attempt.outcome == "redirect"
            and final_queue_attempt.redirect_ordinal
            == state.budgets.redirect_hops_per_document
            and final_queue_attempt.redirect_target_url is not None
        ):
            reject_scheduled(
                final_queue_attempt.redirect_target_url,
                "redirect_hop_budget_exhausted",
                queue_ordinal,
                parent_sha,
                parent_entry,
            )
        post_request_duplicate_reason = document_duplicate_reason(
            scheduled_initial_url=normalized_candidate,
            resolved_final_url=result.final_url,
            content_sha256=(result.sha256 if result.outcome == "success" else None),
            prior_scheduled_initial_urls=seen_scheduled_document_urls,
            prior_observed_document_urls=seen_observed_document_urls,
            prior_document_digests=seen_document_digests,
        )
        seen_scheduled_document_urls.add(normalized_candidate)
        seen_observed_document_urls.add(normalized_candidate)
        if result.final_url:
            seen_observed_document_urls.add(result.final_url)
        if result.outcome == "success" and result.sha256:
            seen_document_digests.add(result.sha256)
        if post_request_duplicate_reason == "duplicate_document_final_url":
            duplicates.append(
                RejectedUrl(
                    url=normalized_candidate,
                    final_url=result.final_url,
                    reason=post_request_duplicate_reason,
                    queue_ordinal=queue_ordinal,
                    parent_sha256=parent_sha,
                    entry_ordinal=parent_entry,
                )
            )
            continue
        if result.outcome == "empty":
            sitemap_evidence.append(
                SitemapEvidence(
                    queue_ordinal=queue_ordinal,
                    url=normalized_candidate,
                    final_url=result.final_url,
                    parent_sha256=parent_sha,
                    parent_entry_ordinal=parent_entry,
                    depth=depth,
                    document_sha256=result.sha256,
                    root_type="empty",
                    outcome="completed_empty",
                )
            )
            continue
        if result.outcome != "success":
            sitemap_evidence.append(
                SitemapEvidence(
                    queue_ordinal=queue_ordinal,
                    url=normalized_candidate,
                    final_url=result.final_url,
                    parent_sha256=parent_sha,
                    parent_entry_ordinal=parent_entry,
                    depth=depth,
                    document_sha256=result.sha256,
                    root_type="failed",
                    outcome=result.outcome,
                )
            )
            continue
        if post_request_duplicate_reason == "duplicate_document_digest":
            duplicates.append(
                RejectedUrl(
                    url=normalized_candidate,
                    reason=post_request_duplicate_reason,
                    queue_ordinal=queue_ordinal,
                    parent_sha256=parent_sha,
                    entry_ordinal=parent_entry,
                )
            )
            continue
        try:
            root_type, locations, url_truncated, overflow_location = (
                _safe_xml_locations(
                    result.body,
                    max_locations=max(
                        0, state.budgets.url_occurrences - state.usage.url_occurrences
                    ),
                    max_sitemap_locations=max(
                        0,
                        state.budgets.sitemap_documents
                        - state.usage.sitemap_document_occurrences,
                    ),
                )
            )
        except SyntaxError:
            state.deterministic_errors.append("xml_syntax_error")
            sitemap_evidence.append(
                SitemapEvidence(
                    queue_ordinal=queue_ordinal,
                    url=normalized_candidate,
                    final_url=result.final_url,
                    parent_sha256=parent_sha,
                    parent_entry_ordinal=parent_entry,
                    depth=depth,
                    document_sha256=result.sha256,
                    root_type="failed",
                    outcome="xml_syntax_error",
                )
            )
            continue
        except SiteDiagnosticError as exc:
            state.safety_errors.append(str(exc))
            sitemap_evidence.append(
                SitemapEvidence(
                    queue_ordinal=queue_ordinal,
                    url=normalized_candidate,
                    final_url=result.final_url,
                    parent_sha256=parent_sha,
                    parent_entry_ordinal=parent_entry,
                    depth=depth,
                    document_sha256=result.sha256,
                    root_type="failed",
                    outcome=str(exc),
                )
            )
            break
        if url_truncated:
            state.truncations.append("url_occurrence_budget_exhausted")
        sitemap_evidence.append(
            SitemapEvidence(
                queue_ordinal=queue_ordinal,
                url=normalized_candidate,
                final_url=result.final_url,
                parent_sha256=parent_sha,
                parent_entry_ordinal=parent_entry,
                depth=depth,
                document_sha256=result.sha256,
                root_type=root_type,
                outcome="parsed",
            )
        )
        _, source_document_origin = normalize_http_url(
            result.final_url or normalized_candidate
        )
        source_origin_url = source_document_origin.as_url_origin() + "/"
        for entry_ordinal, raw_location in enumerate(locations, 1):
            child_queue_ordinal: int | None = None
            if root_type == "sitemapindex":
                queue_counter += 1
                child_queue_ordinal = queue_counter
            if state.usage.url_occurrences >= state.budgets.url_occurrences:
                state.truncations.append("url_occurrence_budget_exhausted")
                if child_queue_ordinal is not None:
                    request_slot_ordinal, disposition_reason = (
                        _scheduled_rejection_slot(
                            state, "url_occurrence_budget_exhausted"
                        )
                    )
                    try:
                        rejected_location, _ = normalize_http_url(raw_location)
                        rejected.append(
                            RejectedUrl(
                                url=rejected_location,
                                reason=disposition_reason,
                                queue_ordinal=child_queue_ordinal,
                                parent_sha256=result.sha256,
                                entry_ordinal=entry_ordinal,
                                request_slot_ordinal=request_slot_ordinal,
                            )
                        )
                    except SiteDiagnosticError:
                        rejected.append(
                            RejectedUrl(
                                raw_value=_safe_raw_evidence(raw_location),
                                reason=disposition_reason,
                                queue_ordinal=child_queue_ordinal,
                                parent_sha256=result.sha256,
                                entry_ordinal=entry_ordinal,
                                request_slot_ordinal=request_slot_ordinal,
                            )
                        )
                break
            state.usage.url_occurrences += 1
            occurrence = _CountedOccurrence(
                source=root_type,
                source_origin=source_origin_url,
                counts_sitemap_document=False,
                queue_ordinal=child_queue_ordinal or queue_ordinal,
                parent_sha256=result.sha256,
                entry_ordinal=entry_ordinal,
            )
            state.counted_occurrences.append(occurrence)
            if root_type == "sitemapindex":
                if (
                    state.usage.sitemap_document_occurrences
                    >= state.budgets.sitemap_documents
                ):
                    state.truncations.append("sitemap_document_budget_exhausted")
                    request_slot_ordinal, disposition_reason = (
                        _scheduled_rejection_slot(
                            state, "sitemap_document_budget_exhausted"
                        )
                    )
                    try:
                        rejected_location, _ = normalize_http_url(raw_location)
                        rejected.append(
                            RejectedUrl(
                                url=rejected_location,
                                reason=disposition_reason,
                                queue_ordinal=child_queue_ordinal,
                                parent_sha256=result.sha256,
                                entry_ordinal=entry_ordinal,
                                request_slot_ordinal=request_slot_ordinal,
                            )
                        )
                    except SiteDiagnosticError:
                        rejected.append(
                            RejectedUrl(
                                raw_value=_safe_raw_evidence(raw_location),
                                reason=disposition_reason,
                                queue_ordinal=child_queue_ordinal,
                                parent_sha256=result.sha256,
                                entry_ordinal=entry_ordinal,
                                request_slot_ordinal=request_slot_ordinal,
                            )
                        )
                    break
                state.usage.sitemap_document_occurrences += 1
                occurrence.counts_sitemap_document = True
            if state.safety_errors:
                request_slot_ordinal, disposition_reason = (
                    _scheduled_rejection_slot(state, "prior_safety_stop")
                    if child_queue_ordinal is not None
                    else (None, "prior_safety_stop")
                )
                try:
                    stopped_location, _ = normalize_http_url(raw_location)
                    rejected.append(
                        RejectedUrl(
                            url=stopped_location,
                            reason=disposition_reason,
                            queue_ordinal=child_queue_ordinal or queue_ordinal,
                            parent_sha256=result.sha256,
                            entry_ordinal=entry_ordinal,
                            request_slot_ordinal=request_slot_ordinal,
                        )
                    )
                except SiteDiagnosticError:
                    rejected.append(
                        RejectedUrl(
                            raw_value=_safe_raw_evidence(raw_location),
                            reason=disposition_reason,
                            queue_ordinal=child_queue_ordinal or queue_ordinal,
                            parent_sha256=result.sha256,
                            entry_ordinal=entry_ordinal,
                            request_slot_ordinal=request_slot_ordinal,
                        )
                    )
                if state.request_budget_exhausted:
                    break
                continue
            if not raw_location:
                state.safety_errors.append("empty_sitemap_loc")
                request_slot_ordinal, disposition_reason = (
                    _scheduled_rejection_slot(state, "empty_sitemap_loc")
                    if child_queue_ordinal is not None
                    else (None, "empty_sitemap_loc")
                )
                rejected.append(
                    RejectedUrl(
                        raw_value=_safe_raw_evidence(raw_location),
                        reason=disposition_reason,
                        trigger_reason=(
                            "empty_sitemap_loc"
                            if disposition_reason != "empty_sitemap_loc"
                            else None
                        ),
                        queue_ordinal=child_queue_ordinal or queue_ordinal,
                        parent_sha256=result.sha256,
                        entry_ordinal=entry_ordinal,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )
                if state.request_budget_exhausted:
                    break
                continue
            try:
                location, location_origin = normalize_http_url(raw_location)
            except SiteDiagnosticError:
                state.safety_errors.append("malformed_sitemap_loc")
                request_slot_ordinal, disposition_reason = (
                    _scheduled_rejection_slot(state, "malformed_sitemap_loc")
                    if child_queue_ordinal is not None
                    else (None, "malformed_sitemap_loc")
                )
                rejected.append(
                    RejectedUrl(
                        raw_value=_safe_raw_evidence(raw_location),
                        reason=disposition_reason,
                        trigger_reason=(
                            "malformed_sitemap_loc"
                            if disposition_reason != "malformed_sitemap_loc"
                            else None
                        ),
                        queue_ordinal=child_queue_ordinal or queue_ordinal,
                        parent_sha256=result.sha256,
                        entry_ordinal=entry_ordinal,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )
                if state.request_budget_exhausted:
                    break
                continue
            if root_type == "sitemapindex":
                if depth >= state.budgets.sitemap_depth:
                    state.truncations.append("sitemap_depth_budget_exhausted")
                    request_slot_ordinal, disposition_reason = (
                        _scheduled_rejection_slot(
                            state, "sitemap_depth_budget_exhausted"
                        )
                    )
                    rejected.append(
                        RejectedUrl(
                            url=location,
                            reason=disposition_reason,
                            queue_ordinal=child_queue_ordinal,
                            parent_sha256=result.sha256,
                            entry_ordinal=entry_ordinal,
                            request_slot_ordinal=request_slot_ordinal,
                        )
                    )
                    if state.request_budget_exhausted:
                        break
                    continue
                assert child_queue_ordinal is not None
                queue.append(
                    (
                        child_queue_ordinal,
                        location,
                        depth + 1,
                        result.sha256,
                        entry_ordinal,
                    )
                )
                continue
            if _origin_key(location_origin) != _origin_key(canonical_origin):
                cross_origin_pages = True
                rejected.append(
                    RejectedUrl(
                        url=location,
                        reason="cross_origin_requires_diagnosis",
                        queue_ordinal=queue_ordinal,
                        parent_sha256=result.sha256,
                        entry_ordinal=entry_ordinal,
                    )
                )
                continue
            canonical_page_candidates += 1
            if not canonical_robots.is_allowed(location):
                robots_disallowed_page_candidates += 1
                rejected.append(
                    RejectedUrl(
                        url=location,
                        reason="robots_disallowed",
                        parent_sha256=result.sha256,
                        queue_ordinal=queue_ordinal,
                        entry_ordinal=entry_ordinal,
                    )
                )
                continue
            if location in accepted_set:
                duplicates.append(
                    RejectedUrl(
                        url=location,
                        reason="duplicate_page_url",
                        parent_sha256=result.sha256,
                        queue_ordinal=queue_ordinal,
                        entry_ordinal=entry_ordinal,
                    )
                )
                continue
            accepted_set.add(location)
            accepted.append(location)
            assert result.sha256 is not None
            accepted_evidence.append(
                AcceptedPageEvidence(
                    url=location,
                    parent_sha256=result.sha256,
                    entry_ordinal=entry_ordinal,
                    source_queue_ordinal=queue_ordinal,
                )
            )
        if overflow_location is not None and root_type == "sitemapindex":
            overflow_entry_ordinal, raw_overflow_location, overflow_reason = (
                overflow_location
            )
            if overflow_reason == "sitemap_document_budget_exhausted":
                state.truncations.append(overflow_reason)
                if state.usage.url_occurrences < state.budgets.url_occurrences:
                    state.usage.url_occurrences += 1
                    state.counted_occurrences.append(
                        _CountedOccurrence(
                            source="sitemapindex",
                            source_origin=source_origin_url,
                            counts_sitemap_document=False,
                            queue_ordinal=queue_counter + 1,
                            parent_sha256=result.sha256,
                            entry_ordinal=overflow_entry_ordinal,
                        )
                    )
            queue_counter += 1
            request_slot_ordinal, disposition_reason = _scheduled_rejection_slot(
                state, overflow_reason
            )
            try:
                normalized_overflow, _ = normalize_http_url(raw_overflow_location)
                rejected.append(
                    RejectedUrl(
                        url=normalized_overflow,
                        reason=disposition_reason,
                        queue_ordinal=queue_counter,
                        parent_sha256=result.sha256,
                        entry_ordinal=overflow_entry_ordinal,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )
            except SiteDiagnosticError:
                rejected.append(
                    RejectedUrl(
                        raw_value=_safe_raw_evidence(raw_overflow_location),
                        reason=disposition_reason,
                        queue_ordinal=queue_counter,
                        parent_sha256=result.sha256,
                        entry_ordinal=overflow_entry_ordinal,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )

    if state.safety_errors or state.request_budget_exhausted or queue_stop_reason:
        intended_prior_stop = queue_stop_reason or (
            "prior_safety_stop" if state.safety_errors else "prior_budget_stop"
        )
        while queue:
            queued_ordinal, queued_url, _, queued_parent, queued_entry = queue.popleft()
            request_slot_ordinal, prior_stop_reason = _scheduled_rejection_slot(
                state, intended_prior_stop
            )
            try:
                normalized_queued, _ = normalize_http_url(queued_url)
                rejected.append(
                    RejectedUrl(
                        url=normalized_queued,
                        reason=prior_stop_reason,
                        queue_ordinal=queued_ordinal,
                        parent_sha256=queued_parent,
                        entry_ordinal=queued_entry,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )
            except SiteDiagnosticError:
                rejected.append(
                    RejectedUrl(
                        raw_value=_safe_raw_evidence(queued_url),
                        reason=prior_stop_reason,
                        queue_ordinal=queued_ordinal,
                        parent_sha256=queued_parent,
                        entry_ordinal=queued_entry,
                        request_slot_ordinal=request_slot_ordinal,
                    )
                )

    state.safety_errors = _stable_unique(state.safety_errors)
    state.final_transient_errors = _stable_unique(state.final_transient_errors)
    state.deterministic_errors = _stable_unique(state.deterministic_errors)
    state.truncations = _stable_unique(state.truncations)
    completed_at = clock()
    fallback_documents_complete = all(
        item.root_type == "empty"
        or (item.root_type in {"urlset", "sitemapindex"} and item.outcome == "parsed")
        or item.outcome in {"duplicate_document_final_url", "duplicate_document_digest"}
        for item in sitemap_evidence
    )
    if (
        not accepted
        and not cross_origin_pages
        and not state.safety_errors
        and not state.truncations
        and not state.final_transient_errors
        and not state.deterministic_errors
        and robots_result.outcome in {"success", "empty"}
        and fallback_documents_complete
        and not canonical_robots.is_allowed(canonical_origin.as_url_origin() + "/")
    ):
        state.safety_errors.append("homepage_fallback_disallowed_by_robots")
    if (
        canonical_page_candidates > 0
        and not accepted
        and robots_disallowed_page_candidates == canonical_page_candidates
    ):
        state.safety_errors.append("all_canonical_page_candidates_disallowed_by_robots")
    if state.safety_errors:
        status, recommendation, priority, next_action = (
            "blocked",
            "operator_review",
            1,
            "resolve_safety_or_authority_error",
        )
    elif accepted:
        if (
            state.truncations
            or state.final_transient_errors
            or state.deterministic_errors
        ):
            status, recommendation, priority, next_action = (
                "partial",
                "sitemap_seeded",
                2,
                "review_partial_sitemap_evidence",
            )
        else:
            status, recommendation, priority, next_action = (
                "complete",
                "sitemap_seeded",
                3,
                "submit_diagnostic_for_operator_review",
            )
    elif (
        state.final_transient_errors
        and not state.truncations
        and not state.deterministic_errors
    ):
        status, recommendation, priority, next_action = (
            "retryable",
            "retry_diagnosis",
            4,
            "retry_diagnosis",
        )
    elif (
        robots_result.outcome in {"success", "empty"}
        and not state.truncations
        and not state.deterministic_errors
        and not cross_origin_pages
        and canonical_robots.is_allowed(canonical_origin.as_url_origin() + "/")
        and fallback_documents_complete
    ):
        status, recommendation, priority, next_action = (
            "complete",
            "bounded_homepage_fallback",
            5,
            "submit_bounded_homepage_fallback_for_operator_review",
        )
    else:
        status, recommendation, priority, next_action = (
            "blocked",
            "operator_review",
            6,
            "revise_inputs_or_boundaries_and_rediagnose",
        )

    reasons = _stable_unique(
        [
            *state.safety_errors,
            *state.final_transient_errors,
            *state.deterministic_errors,
            *state.truncations,
        ]
    )

    def policy_completion_ordinal(evidence: OriginPolicyEvidence) -> int:
        initiating_url = evidence.origin.as_url_origin() + "/robots.txt"
        matching_attempts = [
            item
            for item in state.attempts
            if item.document_kind == "robots"
            and (item.redirect_chain[0] if item.redirect_chain else item.requested_url)
            == initiating_url
            and item.outcome in {"success", "completed_empty"}
            and item.content_sha256 == evidence.robots_sha256
        ]
        if not matching_attempts:
            raise SiteDiagnosticError(
                "origin policy is not bound to a completed robots attempt"
            )
        return matching_attempts[-1].attempt_ordinal

    ordered_origin_policies = sorted(
        (item[1] for item in origin_policies.values()),
        key=policy_completion_ordinal,
    )
    payload = {
        "schema_version": "site-diagnostic.v1",
        "diagnostic_id": f"diag-{uuid.uuid4()}",
        "site_key": site_key.strip(),
        "requested_url": requested_url.strip(),
        "normalized_requested_url": requested_normalized,
        "requested_origin": requested_origin,
        "canonical_origin": canonical_origin,
        "allowed_domains": domains,
        "allowed_document_origins": origins,
        "started_at": started_at,
        "completed_at": completed_at,
        "expires_at": completed_at + freshness,
        "tool_version": TOOL_VERSION,
        "identity": identity,
        "budgets": state.budgets,
        "budget_usage": DiagnosticBudgetUsage(**vars(state.usage)),
        "diagnostic_status": status,
        "recommendation": recommendation,
        "decisive_priority": priority,
        "next_action": next_action,
        "robots_sitemap_directives": directives,
        "robots_warnings": canonical_robots.warnings,
        "origin_policy_evidence": ordered_origin_policies,
        "attempts": state.attempts,
        "sitemap_evidence": sitemap_evidence,
        "accepted_page_urls": accepted,
        "accepted_page_evidence": accepted_evidence,
        "rejected_urls": rejected,
        "duplicate_urls": duplicates,
        "counted_url_occurrences": [
            CountedUrlOccurrence(
                occurrence_ordinal=index,
                source=item.source,  # type: ignore[arg-type]
                source_origin=item.source_origin,
                counts_sitemap_document=item.counts_sitemap_document,
                queue_ordinal=item.queue_ordinal,
                parent_sha256=item.parent_sha256,
                entry_ordinal=item.entry_ordinal,
            )
            for index, item in enumerate(state.counted_occurrences, 1)
        ],
        "robots_errors": state.robots_errors,
        "truncation_reasons": state.truncations,
        "outcome_reasons": reasons,
    }
    provisional = SiteDiagnostic.model_validate(
        {**payload, "artifact_sha256": "0" * 64}
    )
    return SiteDiagnostic.model_validate(
        {
            **provisional.model_dump(mode="python", exclude={"artifact_sha256"}),
            "artifact_sha256": provisional.expected_artifact_sha256(),
        }
    )


def write_site_diagnostic(artifact: SiteDiagnostic, path: str | Path) -> Path:
    artifact.verify_artifact_sha256()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(artifact.model_dump(mode="json")) + "\n").encode("utf-8")
    if destination.exists():
        existing = destination.read_bytes()
        if existing == encoded:
            return destination
        raise SiteDiagnosticError(
            "refusing to overwrite a different diagnostic artifact"
        )
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != encoded:
                raise SiteDiagnosticError(
                    "refusing to overwrite a different diagnostic artifact"
                )
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def load_site_diagnostic(path: str | Path) -> SiteDiagnostic:
    try:
        artifact = SiteDiagnostic.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
        artifact.verify_artifact_sha256()
        return artifact
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
    ) as exc:
        raise SiteDiagnosticError(
            f"invalid site diagnostic artifact or digest: {exc}"
        ) from exc


BodyFailure = _BodyFailure
decode_robots_utf8 = _decode_robots_utf8
header_value = _header
looks_like_html = _looks_like_html
parse_content_type = _parse_content_type
read_bounded_body = _read_bounded_body


__all__ = [
    "BodyFailure",
    "DiagnosticBudgets",
    "DiagnosticTransport",
    "RawHttpResponse",
    "SafePinnedTransport",
    "SiteDiagnosticError",
    "TransportFailure",
    "build_origin_policy_evidence",
    "canonical_host_header",
    "decode_robots_utf8",
    "diagnose_site",
    "header_value",
    "is_public_address",
    "load_site_diagnostic",
    "looks_like_html",
    "normalize_http_url",
    "parse_content_type",
    "parse_robots",
    "read_bounded_body",
    "write_site_diagnostic",
]
