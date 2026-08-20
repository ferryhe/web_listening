from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Collection, Iterable, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from web_listening.contracts._protocol import (
    NonEmptyString,
    Sha256,
    SkillVersion,
    StrictContractModel,
    require_aware_timestamp,
    validate_domain,
    validate_http_url_without_credentials,
)


HttpUrlString = Annotated[str, Field(min_length=1)]
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
BODY_TLS_POLICY_OUTCOME = "body_tls_policy"
REDIRECT_HTTP_STATUSES = frozenset({301, 302, 303, 307, 308})
EMPTY_HTTP_STATUSES = frozenset({404, 410})
RETRYABLE_ATTEMPT_OUTCOMES = frozenset({
    "transient_http",
    "body_remote_disconnected",
    "body_transient",
    "transport_dns",
    "transport_connect",
    "transport_remote_disconnected",
    "transport_connect_or_http",
    # Kept for injected/test transports that expose a classified timeout directly.
    "transport_timeout",
})
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, *range(500, 600)})
_EMPTY_CONTENT_SHA256 = hashlib.sha256(b"").hexdigest()
_PREFLIGHT_CLASSIFICATIONS = frozenset({
    "safety",
    "deterministic",
    "transient",
    "budget",
})
_SITEMAP_PREFLIGHT_REASONS = frozenset(
    f"sitemap_policy_preflight_{classification}"
    for classification in _PREFLIGHT_CLASSIFICATIONS
)
_REDIRECT_PREFLIGHT_REASONS = frozenset(
    f"redirect_policy_preflight_{classification}"
    for classification in _PREFLIGHT_CLASSIFICATIONS
)
_POLICY_PREFLIGHT_REASONS = (
    _SITEMAP_PREFLIGHT_REASONS | _REDIRECT_PREFLIGHT_REASONS
)
_DIRECT_REDIRECT_SUBDISPOSITION_REASONS = frozenset({
    "redirect_disallowed_by_robots",
    "redirect_hop_budget_exhausted",
})
_REDIRECT_SUBDISPOSITION_REASONS = (
    _REDIRECT_PREFLIGHT_REASONS | _DIRECT_REDIRECT_SUBDISPOSITION_REASONS
)
_DIRECT_REDIRECT_EVIDENCE_OUTCOME = {
    "redirect_disallowed_by_robots": "redirect_disallowed_by_robots",
    "redirect_hop_budget_exhausted": "budget",
}
_DETERMINISTIC_PREFLIGHT_ATTEMPT_OUTCOMES = frozenset({
    "terminal_http",
    "final_informational",
    "transport_malformed_status",
    "body_incomplete",
    "redirect_missing_location",
    "redirect_malformed_location",
})
_EXPLICIT_SAFETY_PREFLIGHT_ATTEMPT_OUTCOMES = frozenset({
    "authority_http",
    BODY_TLS_POLICY_OUTCOME,
    "robots_unsupported_mime_or_charset",
    "unclassified_http",
    "redirect_authority_failure",
    "unclassified_transport",
})

PolicyPreflightClassification = Literal[
    "safety",
    "deterministic",
    "transient",
    "budget",
]


def derive_policy_preflight_classification(
    *,
    final_attempt_outcome: str | None,
    final_retry_ordinal: int | None,
    has_related_safety_evidence: bool,
    has_related_budget_evidence: bool,
) -> PolicyPreflightClassification | None:
    """Classify an auxiliary robots preflight from its causal evidence."""
    if final_attempt_outcome in _DETERMINISTIC_PREFLIGHT_ATTEMPT_OUTCOMES:
        return "deterministic"
    if final_attempt_outcome is not None and is_retryable_attempt_outcome(
        final_attempt_outcome
    ):
        if final_retry_ordinal == 2:
            return "transient"
        return "budget" if has_related_budget_evidence else None
    if (
        final_attempt_outcome in _EXPLICIT_SAFETY_PREFLIGHT_ATTEMPT_OUTCOMES
        or (
            final_attempt_outcome is not None
            and final_attempt_outcome.startswith("transport_")
        )
    ):
        return "safety"
    if final_attempt_outcome in {"wire_budget_exhausted", "decoded_budget_exhausted"}:
        return "budget" if has_related_budget_evidence else "safety"
    if has_related_safety_evidence:
        return "safety"
    if has_related_budget_evidence:
        return "budget"
    if final_attempt_outcome not in {None, "success", "completed_empty", "redirect"}:
        return "safety"
    return None


def classify_http_status(status: int) -> Literal[
    "body",
    "authority",
    "empty",
    "redirect",
    "informational",
    "transient",
    "terminal",
    "unclassified",
]:
    """Return the governed terminal class for a received HTTP status line."""
    if 200 <= status < 300:
        return "body"
    if status in {401, 403}:
        return "authority"
    if status in EMPTY_HTTP_STATUSES:
        return "empty"
    if status in REDIRECT_HTTP_STATUSES:
        return "redirect"
    if 100 <= status < 200:
        return "informational"
    if status in TRANSIENT_HTTP_STATUSES:
        return "transient"
    if 400 <= status < 500:
        return "terminal"
    return "unclassified"


def is_retryable_attempt_outcome(outcome: str) -> bool:
    """Return whether a visible attempt outcome may be followed by a retry."""
    return outcome in RETRYABLE_ATTEMPT_OUTCOMES


def redirect_transition_allowed(previous_url: str, next_url: str) -> bool:
    """Return whether a redirect preserves or strengthens transport security."""
    previous_scheme = urlsplit(previous_url).scheme.casefold()
    next_scheme = urlsplit(next_url).scheme.casefold()
    return not (previous_scheme == "https" and next_scheme == "http")


DocumentDuplicateReason = Literal[
    "duplicate_document_url",
    "duplicate_document_final_url",
    "duplicate_document_digest",
]


def document_duplicate_reason(
    *,
    scheduled_initial_url: str,
    resolved_final_url: str | None,
    content_sha256: str | None,
    prior_scheduled_initial_urls: Collection[str],
    prior_observed_document_urls: Collection[str],
    prior_document_digests: Collection[str],
) -> DocumentDuplicateReason | None:
    """Return the first applicable governed document-deduplication reason."""
    if scheduled_initial_url in prior_scheduled_initial_urls:
        return "duplicate_document_url"
    if (
        resolved_final_url is not None
        and resolved_final_url in prior_observed_document_urls
    ):
        return "duplicate_document_final_url"
    if content_sha256 is not None and content_sha256 in prior_document_digests:
        return "duplicate_document_digest"
    return None


def _canonical_allowed_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return validate_domain(value)
    normalized = str(address).lower()
    if value != normalized:
        raise ValueError("allowed IP literals must use canonical unbracketed spelling")
    if not (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    ):
        raise ValueError("allowed IP literals must be public")
    return normalized


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _is_public_ip_literal(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def canonical_json(value: object) -> str:
    """Return the UTF-8 canonical JSON representation used by this contract."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _validate_http_url(value: object) -> object:
    validate_http_url_without_credentials(value)
    if not isinstance(value, str) or value != value.strip() or _CONTROL_RE.search(value) or "\\" in value:
        raise ValueError("URL must be a clean absolute HTTP(S) string")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be absolute HTTP(S)")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port must be in 1..65535")
    return value


def _validate_canonical_http_url(value: object) -> object:
    value = _validate_http_url(value)
    assert isinstance(value, str)
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    host = parsed.hostname.lower()
    try:
        host = str(ipaddress.ip_address(host)).lower()
    except ValueError:
        host = validate_domain(host)
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    default = 443 if parsed.scheme == "https" else 80
    authority_host = f"[{host}]" if ":" in host else host
    expected_netloc = authority_host if port == default else f"{authority_host}:{port}"
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc != expected_netloc
        or parsed.fragment
        or not (parsed.path or "/").startswith("/")
    ):
        raise ValueError("URL must use canonical HTTP(S) authority and no fragment")
    if value != canonicalize_requested_http_url(value):
        raise ValueError("URL must use the complete canonical HTTP(S) representation")
    return value


def _remove_dot_segments(path: str) -> str:
    source = path
    output = ""
    while source:
        if source.startswith("../"):
            source = source[3:]
        elif source.startswith("./"):
            source = source[2:]
        elif source.startswith("/./"):
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


def _normalize_requested_path(path: str) -> str:
    path = path or "/"
    if re.search(r"%(?![0-9A-Fa-f]{2})", path):
        raise ValueError("requested URL contains malformed percent encoding")
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def replace(match: re.Match[str]) -> str:
        value = chr(int(match.group(1), 16))
        return value if value in unreserved else "%" + match.group(1).upper()

    normalized = re.sub(r"%([0-9A-Fa-f]{2})", replace, path)
    return quote(
        _remove_dot_segments(normalized),
        safe="/%:@!$&'()*+,;=-._~",
    )


def canonicalize_requested_http_url(value: str) -> str:
    """Canonicalize a requested URL using the producer's governed URL rules."""
    _validate_http_url(value)
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    try:
        host = str(ipaddress.ip_address(parsed.hostname)).lower()
    except ValueError:
        try:
            host = validate_domain(
                parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
            )
        except (UnicodeError, ValueError) as exc:
            raise ValueError("requested URL host cannot be normalized") from exc
    scheme = parsed.scheme.lower()
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    default = 443 if scheme == "https" else 80
    authority_host = f"[{host}]" if ":" in host else host
    netloc = authority_host if port == default else f"{authority_host}:{port}"
    return urlunsplit(
        (scheme, netloc, _normalize_requested_path(parsed.path), parsed.query, "")
    )


def _validate_clean_string(value: str) -> str:
    if value != value.strip() or _CONTROL_RE.search(value):
        raise ValueError("value must not contain surrounding whitespace or control characters")
    return value


def _origin_key(origin: "NormalizedOrigin") -> tuple[str, str, int]:
    return origin.scheme, origin.host, origin.effective_port


def _origin_url_from_key(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    authority_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    suffix = "" if port == default_port else f":{port}"
    return f"{scheme}://{authority_host}{suffix}"


class NormalizedOrigin(StrictContractModel):
    scheme: Literal["http", "https"]
    host: NonEmptyString
    effective_port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def canonical_host(cls, value: str) -> str:
        if value != value.lower() or value != value.strip() or _CONTROL_RE.search(value):
            raise ValueError("origin host must be canonical lowercase text")
        try:
            normalized = str(ipaddress.ip_address(value)).lower()
        except ValueError:
            normalized = validate_domain(value)
        if value != normalized:
            raise ValueError("origin host must use canonical IP/domain spelling")
        return value

    def as_url_origin(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 80 if self.scheme == "http" else 443
        suffix = "" if self.effective_port == default else f":{self.effective_port}"
        return f"{self.scheme}://{host}{suffix}"


class DiagnosticIdentity(StrictContractModel):
    identity_id: NonEmptyString
    user_agent: NonEmptyString
    product_token: Annotated[str, Field(pattern=r"^[-A-Za-z_]+$")]
    identity_sha256: Sha256

    _clean_identity = field_validator("identity_id", "user_agent", "product_token")(_validate_clean_string)

    @model_validator(mode="after")
    def product_token_matches_user_agent(self) -> "DiagnosticIdentity":
        if self.product_token.casefold() not in self.user_agent.casefold():
            raise ValueError("product_token must occur in user_agent")
        return self


class DiagnosticBudgets(StrictContractModel):
    redirect_hops_per_document: int = Field(default=5, ge=0, le=5)
    http_requests: int = Field(default=64, ge=1, le=64)
    robots_wire_bytes_per_attempt: int = Field(default=1_048_576, ge=1, le=1_048_576)
    robots_decoded_bytes_per_attempt: int = Field(default=1_048_576, ge=1, le=1_048_576)
    robots_wire_bytes_total: int = Field(default=3_145_728, ge=1, le=3_145_728)
    robots_decoded_bytes_total: int = Field(default=3_145_728, ge=1, le=3_145_728)
    sitemap_wire_bytes_per_document: int = Field(default=10_485_760, ge=1, le=10_485_760)
    sitemap_decoded_bytes_per_document: int = Field(default=52_428_800, ge=1, le=52_428_800)
    sitemap_wire_bytes_total: int = Field(default=33_554_432, ge=1, le=33_554_432)
    sitemap_decoded_bytes_total: int = Field(default=134_217_728, ge=1, le=134_217_728)
    sitemap_depth: int = Field(default=3, ge=0, le=3)
    sitemap_documents: int = Field(default=32, ge=1, le=32)
    url_occurrences: int = Field(default=50_000, ge=1, le=50_000)


class DiagnosticBudgetUsage(StrictContractModel):
    http_requests: int = Field(default=0, ge=0)
    robots_wire_bytes: int = Field(default=0, ge=0)
    robots_decoded_bytes: int = Field(default=0, ge=0)
    sitemap_wire_bytes: int = Field(default=0, ge=0)
    sitemap_decoded_bytes: int = Field(default=0, ge=0)
    sitemap_document_occurrences: int = Field(default=0, ge=0)
    url_occurrences: int = Field(default=0, ge=0)


class DiagnosticAttempt(StrictContractModel):
    attempt_ordinal: int = Field(ge=1)
    request_slot_ordinal: int = Field(ge=1)
    queue_ordinal: int | None = Field(default=None, ge=1)
    retry_ordinal: int = Field(default=0, ge=0, le=2)
    redirect_ordinal: int = Field(default=0, ge=0, le=5)
    document_kind: Literal["robots", "sitemap"]
    parent_sha256: Sha256 | None = None
    requested_url: HttpUrlString
    redirect_chain: list[HttpUrlString] = Field(default_factory=list)
    redirect_target_url: HttpUrlString | None = None
    final_url: HttpUrlString | None = None
    http_status: int | None = Field(default=None, ge=100, le=999)
    fetched_at: datetime
    media_type: str | None = None
    content_type_parameters: dict[str, str] = Field(default_factory=dict)
    content_encoding: str | None = None
    wire_bytes: int = Field(default=0, ge=0)
    decoded_bytes: int = Field(default=0, ge=0)
    content_sha256: Sha256 | None = None
    outcome: NonEmptyString
    actual_user_agent: NonEmptyString
    product_token: Annotated[str, Field(pattern=r"^[-A-Za-z_]+$")]
    identity_sha256: Sha256

    _validate_urls = field_validator(
        "requested_url",
        "redirect_chain",
        "redirect_target_url",
        "final_url",
        mode="before",
    )(
        lambda value: [_validate_canonical_http_url(item) for item in value] if isinstance(value, list) else _validate_canonical_http_url(value) if value is not None else value
    )
    _validate_time = field_validator("fetched_at")(require_aware_timestamp)
    _clean_strings = field_validator("actual_user_agent", "product_token", "outcome")(_validate_clean_string)

    @model_validator(mode="after")
    def redirect_target_matches_outcome(self) -> "DiagnosticAttempt":
        if (self.outcome == "redirect") != (self.redirect_target_url is not None):
            raise ValueError(
                "redirect target URL must exist exactly for successful redirect outcomes"
            )
        return self


class SitemapDirective(StrictContractModel):
    url: HttpUrlString
    line_number: int = Field(ge=1)
    _validate_url = field_validator("url", mode="before")(_validate_canonical_http_url)


class RobotsPolicyRule(StrictContractModel):
    allow: bool
    pattern: NonEmptyString
    line_number: int = Field(ge=1)

    _clean_pattern = field_validator("pattern")(_validate_clean_string)


def _normalize_robots_match_target(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        char = chr(byte)
        return char if char.isascii() and (char.isalnum() or char in "-._~") else f"%{byte:02X}"

    normalized = re.sub(r"%([0-9A-Fa-f]{2})", replace, value)
    return quote(normalized, safe="/%?=&:+,;$!*'()@[]~-._")


def robots_rule_specificity(pattern: str) -> int:
    core = pattern[:-1] if pattern.endswith("$") else pattern
    normalized = _normalize_robots_match_target(core).replace("*", "")
    octets = 0
    index = 0
    while index < len(normalized):
        if re.match(r"%[0-9A-F]{2}", normalized[index:index + 3]):
            octets += 1
            index += 3
        else:
            octets += len(normalized[index].encode("utf-8"))
            index += 1
    return octets


def robots_rule_matches(pattern: str, url: str) -> bool:
    anchor = pattern.endswith("$")
    core = pattern[:-1] if anchor else pattern
    normalized_pattern = _normalize_robots_match_target(core)
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    normalized_target = _normalize_robots_match_target(target)

    segments = normalized_pattern.split("*")
    if len(segments) == 1:
        return (
            normalized_target == normalized_pattern
            if anchor
            else normalized_target.startswith(normalized_pattern)
        )

    position = 0
    first = segments[0]
    if first:
        if not normalized_target.startswith(first):
            return False
        position = len(first)

    for segment in segments[1:-1]:
        if not segment:
            continue
        found_at = normalized_target.find(segment, position)
        if found_at < 0:
            return False
        position = found_at + len(segment)

    last = segments[-1]
    if anchor:
        if not last:
            return True
        final_start = len(normalized_target) - len(last)
        return final_start >= position and normalized_target.startswith(
            last, final_start
        )
    return not last or normalized_target.find(last, position) >= 0


def robots_rules_allow(rules: Iterable[RobotsPolicyRule], url: str) -> bool:
    matches = [rule for rule in rules if robots_rule_matches(rule.pattern, url)]
    if not matches:
        return True
    longest = max(robots_rule_specificity(rule.pattern) for rule in matches)
    return any(
        rule.allow for rule in matches if robots_rule_specificity(rule.pattern) == longest
    )


class OriginPolicyEvidence(StrictContractModel):
    origin: NormalizedOrigin
    policy_id: NonEmptyString
    policy_sha256: Sha256
    robots_status: Literal["available", "absent"]
    robots_sha256: Sha256 | None = None
    selected_rules: list[RobotsPolicyRule] = Field(default_factory=list)
    declared_sitemaps: list[SitemapDirective] = Field(default_factory=list)
    warnings: list[NonEmptyString] = Field(default_factory=list)
    fetched_at: datetime
    expires_at: datetime
    identity_id: NonEmptyString
    identity_sha256: Sha256

    _validate_times = field_validator("fetched_at", "expires_at")(require_aware_timestamp)
    _clean_identity = field_validator("identity_id", "policy_id", "robots_status")(_validate_clean_string)

    @model_validator(mode="after")
    def valid_freshness(self) -> "OriginPolicyEvidence":
        if self.expires_at < self.fetched_at or self.expires_at - self.fetched_at > timedelta(hours=24):
            raise ValueError("origin policy freshness must be between zero and 24 hours")
        if self.robots_status == "absent" and (self.selected_rules or self.declared_sitemaps):
            raise ValueError("absent robots policy cannot contain selected rules or sitemap directives")
        line_numbers = [rule.line_number for rule in self.selected_rules]
        if line_numbers != sorted(line_numbers) or len(line_numbers) != len(set(line_numbers)):
            raise ValueError("selected robots rules must preserve unique source-line order")
        policy_payload = {
            "origin": self.origin.model_dump(mode="json"),
            "robots_sha256": self.robots_sha256,
            "selected_rules": [rule.model_dump(mode="json") for rule in self.selected_rules],
            "identity_sha256": self.identity_sha256,
        }
        expected = canonical_sha256(policy_payload)
        if self.policy_sha256 != expected or self.policy_id != f"robots-policy-{expected[:16]}":
            raise ValueError("robots policy ID/digest does not match visible selected rules")
        return self


class SitemapEvidence(StrictContractModel):
    queue_ordinal: int = Field(ge=1)
    url: HttpUrlString
    final_url: HttpUrlString | None = None
    parent_sha256: Sha256 | None = None
    parent_entry_ordinal: int | None = Field(default=None, ge=1)
    depth: int = Field(ge=0)
    document_sha256: Sha256 | None = None
    root_type: Literal["sitemapindex", "urlset", "empty", "failed"]
    outcome: NonEmptyString

    _validate_urls = field_validator("url", "final_url", mode="before")(
        lambda value: _validate_canonical_http_url(value) if value is not None else value
    )
    _clean_outcome = field_validator("outcome")(_validate_clean_string)


class AcceptedPageEvidence(StrictContractModel):
    url: HttpUrlString
    parent_sha256: Sha256
    entry_ordinal: int = Field(ge=1)
    source_queue_ordinal: int = Field(ge=1)

    _validate_url = field_validator("url", mode="before")(_validate_canonical_http_url)


class RejectedUrl(StrictContractModel):
    url: HttpUrlString | None = None
    final_url: HttpUrlString | None = None
    raw_value: str | None = None
    reason: NonEmptyString
    trigger_reason: NonEmptyString | None = None
    queue_ordinal: int | None = Field(default=None, ge=1)
    parent_sha256: Sha256 | None = None
    entry_ordinal: int | None = Field(default=None, ge=1)
    request_slot_ordinal: int | None = Field(default=None, ge=1)

    _validate_url = field_validator("url", "final_url", mode="before")(
        lambda value: _validate_canonical_http_url(value) if value is not None else value
    )
    _clean_raw = field_validator("raw_value")(
        lambda value: _validate_clean_string(value) if value is not None else value
    )
    _clean_reason = field_validator("reason", "trigger_reason")(
        lambda value: _validate_clean_string(value) if value is not None else value
    )

    @model_validator(mode="after")
    def has_visible_rejected_value(self) -> "RejectedUrl":
        if (self.url is None) == (self.raw_value is None):
            raise ValueError("rejected URL evidence must contain exactly one of url or raw_value")
        return self


class CountedUrlOccurrence(StrictContractModel):
    occurrence_ordinal: int = Field(ge=1)
    source: Literal[
        "robots_sitemap",
        "aux_robots_sitemap",
        "fallback",
        "sitemapindex",
        "urlset",
    ]
    source_origin: HttpUrlString
    counts_sitemap_document: bool
    queue_ordinal: int | None = Field(default=None, ge=1)
    parent_sha256: Sha256 | None = None
    entry_ordinal: int | None = Field(default=None, ge=1)

    _validate_source_origin = field_validator("source_origin", mode="before")(
        _validate_canonical_http_url
    )

    @model_validator(mode="after")
    def valid_lineage_shape(self) -> "CountedUrlOccurrence":
        parsed_source_origin = urlsplit(self.source_origin)
        if parsed_source_origin.path != "/" or parsed_source_origin.query:
            raise ValueError("counted URL source_origin must be a canonical origin URL")
        if (
            self.source in {"robots_sitemap", "aux_robots_sitemap", "fallback"}
            and not self.counts_sitemap_document
        ):
            raise ValueError("root URL occurrence must count as a sitemap document")
        if self.source == "urlset" and self.counts_sitemap_document:
            raise ValueError("urlset page occurrence cannot count as a sitemap document")
        if self.parent_sha256 is None:
            raise ValueError("counted URL occurrence requires parent digest lineage")
        if self.source == "fallback":
            if self.queue_ordinal is None or self.entry_ordinal is not None:
                raise ValueError("fallback occurrence requires queue lineage without source entry")
        elif self.entry_ordinal is None:
            raise ValueError("non-fallback occurrence requires source entry lineage")
        if self.source in {"sitemapindex", "urlset"} and self.queue_ordinal is None:
            raise ValueError("sitemap XML occurrence requires queue lineage")
        return self


class RobotsErrorEvidence(StrictContractModel):
    attempt_ordinal: int = Field(ge=1)
    source_origin: HttpUrlString
    document_sha256: Sha256
    reason: NonEmptyString

    _validate_source_origin = field_validator("source_origin", mode="before")(
        _validate_canonical_http_url
    )
    _clean_reason = field_validator("reason")(_validate_clean_string)

    @model_validator(mode="after")
    def valid_error_shape(self) -> "RobotsErrorEvidence":
        parsed_source_origin = urlsplit(self.source_origin)
        if parsed_source_origin.path != "/" or parsed_source_origin.query:
            raise ValueError("robots error source_origin must be a canonical origin URL")
        if self.reason not in {
            "robots_invalid_utf8",
            "robots_html_response",
            "aux_robots_unsupported_mime",
            "aux_robots_invalid_utf8",
            "aux_robots_html_response",
        } and not re.fullmatch(
            r"line [1-9][0-9]*: (?:empty Sitemap directive|malformed Sitemap directive|control character in robots directive)",
            self.reason,
        ):
            raise ValueError("robots error reason is not a governed parser outcome")
        return self


_ALLOWED_OUTCOMES = {
    ("complete", "sitemap_seeded"),
    ("partial", "sitemap_seeded"),
    ("retryable", "retry_diagnosis"),
    ("complete", "bounded_homepage_fallback"),
    ("blocked", "operator_review"),
}


class SiteDiagnostic(StrictContractModel):
    schema_version: Literal["site-diagnostic.v1"] = "site-diagnostic.v1"
    diagnostic_id: NonEmptyString
    site_key: NonEmptyString
    requested_url: HttpUrlString
    normalized_requested_url: HttpUrlString
    requested_origin: NormalizedOrigin
    canonical_origin: NormalizedOrigin
    allowed_domains: list[NonEmptyString] = Field(min_length=1)
    allowed_document_origins: list[NormalizedOrigin] = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    expires_at: datetime
    tool_version: SkillVersion
    identity: DiagnosticIdentity
    budgets: DiagnosticBudgets
    budget_usage: DiagnosticBudgetUsage
    diagnostic_status: Literal["complete", "partial", "retryable", "blocked"]
    recommendation: Literal["sitemap_seeded", "bounded_homepage_fallback", "retry_diagnosis", "operator_review"]
    decisive_priority: int = Field(ge=1, le=6)
    next_action: NonEmptyString
    robots_sitemap_directives: list[SitemapDirective] = Field(default_factory=list)
    robots_warnings: list[NonEmptyString] = Field(default_factory=list)
    origin_policy_evidence: list[OriginPolicyEvidence] = Field(default_factory=list)
    attempts: list[DiagnosticAttempt] = Field(min_length=1)
    sitemap_evidence: list[SitemapEvidence] = Field(default_factory=list)
    accepted_page_urls: list[HttpUrlString] = Field(default_factory=list)
    accepted_page_evidence: list[AcceptedPageEvidence] = Field(default_factory=list)
    rejected_urls: list[RejectedUrl] = Field(default_factory=list)
    duplicate_urls: list[RejectedUrl] = Field(default_factory=list)
    counted_url_occurrences: list[CountedUrlOccurrence] = Field(default_factory=list)
    robots_errors: list[RobotsErrorEvidence] = Field(default_factory=list)
    truncation_reasons: list[NonEmptyString] = Field(default_factory=list)
    outcome_reasons: list[NonEmptyString] = Field(default_factory=list)
    artifact_sha256: Sha256

    _validate_requested_url = field_validator("requested_url", mode="before")(_validate_http_url)
    _validate_normalized_url = field_validator("normalized_requested_url", mode="before")(
        _validate_canonical_http_url
    )
    _validate_accepted_urls = field_validator("accepted_page_urls", mode="before")(
        lambda value: [_validate_canonical_http_url(item) for item in value]
    )
    _validate_times = field_validator("started_at", "completed_at", "expires_at")(require_aware_timestamp)
    _clean_strings = field_validator("diagnostic_id", "site_key", "tool_version", "next_action")(_validate_clean_string)

    @field_validator("allowed_domains")
    @classmethod
    def canonical_unique_domains(cls, value: list[str]) -> list[str]:
        for domain in value:
            if _canonical_allowed_host(domain) != domain:
                raise ValueError("allowed_domains must contain canonical hosts")
        if len(set(value)) != len(value):
            raise ValueError("allowed_domains must be unique")
        return value

    @model_validator(mode="after")
    def valid_outcome(self) -> "SiteDiagnostic":
        if len({_origin_key(item) for item in self.allowed_document_origins}) != len(self.allowed_document_origins):
            raise ValueError("allowed_document_origins must be unique")
        if (self.diagnostic_status, self.recommendation) not in _ALLOWED_OUTCOMES:
            raise ValueError("invalid diagnostic_status/recommendation combination")
        if self.completed_at < self.started_at or self.expires_at < self.completed_at:
            raise ValueError("diagnostic timestamps are out of order")
        if self.expires_at - self.completed_at > timedelta(hours=24):
            raise ValueError("site diagnostic freshness exceeds 24 hours")
        expected_by_priority = {
            1: ("blocked", "operator_review"), 2: ("partial", "sitemap_seeded"),
            3: ("complete", "sitemap_seeded"), 4: ("retryable", "retry_diagnosis"),
            5: ("complete", "bounded_homepage_fallback"), 6: ("blocked", "operator_review"),
        }
        if expected_by_priority[self.decisive_priority] != (self.diagnostic_status, self.recommendation):
            raise ValueError("decisive_priority does not match the diagnostic outcome")
        expected_actions = {
            1: "resolve_safety_or_authority_error",
            2: "review_partial_sitemap_evidence",
            3: "submit_diagnostic_for_operator_review",
            4: "retry_diagnosis",
            5: "submit_bounded_homepage_fallback_for_operator_review",
            6: "revise_inputs_or_boundaries_and_rediagnose",
        }
        if self.next_action != expected_actions[self.decisive_priority]:
            raise ValueError("next_action does not match decisive priority")
        identity_payload = {
            "identity_id": self.identity.identity_id,
            "product_token": self.identity.product_token,
            "user_agent": self.identity.user_agent,
        }
        if self.identity.identity_sha256 != canonical_sha256(identity_payload):
            raise ValueError("diagnostic identity digest mismatch")
        approved = {_origin_key(item) for item in self.allowed_document_origins}
        if _origin_key(self.requested_origin) != _url_origin(self.requested_url):
            raise ValueError("requested_origin does not match requested_url")
        if self.requested_origin != self.canonical_origin:
            raise ValueError("canonical_origin must remain the normalized requested origin")
        if _origin_key(self.canonical_origin) != _url_origin(self.normalized_requested_url):
            raise ValueError("normalized_requested_url does not match canonical_origin")
        if self.normalized_requested_url != canonicalize_requested_http_url(self.requested_url):
            raise ValueError("normalized_requested_url does not match normalized requested_url")
        if _origin_key(self.canonical_origin) not in approved:
            raise ValueError("canonical origin is not present in allowed_document_origins")
        if not all(
            any(
                origin.host == domain
                or (
                    not _is_ip_literal(domain)
                    and not _is_ip_literal(origin.host)
                    and origin.host.endswith("." + domain)
                )
                for domain in self.allowed_domains
            )
            for origin in self.allowed_document_origins
        ):
            raise ValueError("allowed document origin is outside allowed_domains")
        if any(_is_ip_literal(origin.host) and not _is_public_ip_literal(origin.host) for origin in self.allowed_document_origins):
            raise ValueError("allowed document origin IP literals must be public")
        if [item.attempt_ordinal for item in self.attempts] != list(range(1, len(self.attempts) + 1)):
            raise ValueError("attempt ordinals must be contiguous")
        attempt_request_slots = [item.request_slot_ordinal for item in self.attempts]
        if attempt_request_slots[0] != 1 or any(
            current <= previous
            for previous, current in zip(attempt_request_slots, attempt_request_slots[1:])
        ):
            raise ValueError("attempt request slots must start at one and strictly follow attempt order")
        if any(not (self.started_at <= item.fetched_at <= self.completed_at) for item in self.attempts):
            raise ValueError("attempt timestamp is outside diagnostic interval")
        if any(
            item.actual_user_agent != self.identity.user_agent
            or item.product_token != self.identity.product_token
            or item.identity_sha256 != self.identity.identity_sha256
            for item in self.attempts
        ):
            raise ValueError("attempt identity does not match canonical diagnostic identity")
        sitemap_attempt_queue_ordinals = {
            item.queue_ordinal
            for item in self.attempts
            if item.document_kind == "sitemap" and item.queue_ordinal is not None
        }

        def duplicate_requires_non_network_slot(item: RejectedUrl) -> bool:
            return item.reason == "duplicate_document_url" or (
                item.reason == "duplicate_document_final_url"
                and item.queue_ordinal not in sitemap_attempt_queue_ordinals
            )

        request_slot_ordinals = [item.request_slot_ordinal for item in self.attempts]
        request_slot_ordinals.extend(
            item.request_slot_ordinal
            for item in [*self.rejected_urls, *self.duplicate_urls]
            if item.request_slot_ordinal is not None
        )
        if sorted(request_slot_ordinals) != list(range(1, self.budget_usage.http_requests + 1)):
            raise ValueError("HTTP request budget usage does not match request-slot evidence")
        for collection in (self.rejected_urls, self.duplicate_urls):
            visible_slots = [
                item.request_slot_ordinal
                for item in collection
                if item.request_slot_ordinal is not None
            ]
            if visible_slots != sorted(visible_slots) or len(visible_slots) != len(set(visible_slots)):
                raise ValueError("rejection request slots must preserve causal evidence order")
        scheduled_slot_reasons = {
            "document_origin_not_approved",
            *_POLICY_PREFLIGHT_REASONS,
            "sitemap_disallowed_by_robots",
            "redirect_disallowed_by_robots",
            "redirect_hop_budget_exhausted",
            "url_occurrence_budget_exhausted",
            "sitemap_document_budget_exhausted",
            "empty_sitemap_loc",
            "malformed_sitemap_loc",
            "sitemap_depth_budget_exhausted",
            "prior_safety_stop",
            "http_request_budget_exhausted",
            "prior_budget_stop",
        }
        if any(
            item.request_slot_ordinal is not None
            and item.reason not in scheduled_slot_reasons
            for item in self.rejected_urls
        ) or any(
            (item.request_slot_ordinal is not None)
            != duplicate_requires_non_network_slot(item)
            for item in self.duplicate_urls
        ):
            raise ValueError("request-slot rejection evidence has an invalid disposition")
        if [item.occurrence_ordinal for item in self.counted_url_occurrences] != list(
            range(1, len(self.counted_url_occurrences) + 1)
        ):
            raise ValueError("counted URL occurrence ordinals must be contiguous")
        if (
            self.budget_usage.url_occurrences != len(self.counted_url_occurrences)
            or self.budget_usage.sitemap_document_occurrences
            != sum(item.counts_sitemap_document for item in self.counted_url_occurrences)
        ):
            raise ValueError("non-byte budget usage does not match counted occurrence evidence")
        status_outcomes = {
            "authority": {"authority_http"},
            "empty": {"completed_empty"},
            "redirect": {
                "redirect",
                "redirect_missing_location",
                "redirect_malformed_location",
                "redirect_authority_failure",
            },
            "informational": {"final_informational"},
            "transient": {"transient_http"},
            "terminal": {"terminal_http"},
            "unclassified": {"unclassified_http"},
        }
        non_body_http_outcomes = set().union(*status_outcomes.values())
        for item in self.attempts:
            if item.http_status is None:
                if not (
                    item.outcome == "unclassified_transport"
                    or item.outcome.startswith("transport_")
                ):
                    raise ValueError("non-transport outcome requires a received status line")
            else:
                if item.outcome == "unclassified_transport" or (
                    item.outcome.startswith("transport_")
                    and item.outcome != "transport_returned_non_bytes"
                ):
                    raise ValueError("transport outcome cannot contain an HTTP status")
                status_class = classify_http_status(item.http_status)
                if status_class == "body":
                    if item.outcome in non_body_http_outcomes:
                        raise ValueError("2xx response has an incompatible terminal outcome")
                elif item.outcome not in status_outcomes[status_class]:
                    raise ValueError("HTTP status and terminal outcome are inconsistent")
            if item.http_status is not None and classify_http_status(item.http_status) != "body" and (
                item.wire_bytes != 0
                or item.decoded_bytes != 0
                or item.content_sha256 != _EMPTY_CONTENT_SHA256
            ):
                raise ValueError("known terminal HTTP status must stop before reading a body")
            if item.redirect_ordinal != len(item.redirect_chain):
                raise ValueError("redirect ordinal does not match redirect chain length")
            if item.http_status is not None and item.final_url != item.requested_url:
                raise ValueError("HTTP attempt final URL must equal its requested hop URL")
            if item.redirect_target_url is not None and not redirect_transition_allowed(
                item.requested_url,
                item.redirect_target_url,
            ):
                raise ValueError("HTTPS to HTTP redirect target is forbidden")
            redirect_hops = [*item.redirect_chain, item.requested_url]
            if item.final_url is not None and item.final_url != item.requested_url:
                redirect_hops.append(item.final_url)
            if any(
                not redirect_transition_allowed(previous, current)
                for previous, current in zip(redirect_hops, redirect_hops[1:])
            ):
                raise ValueError("HTTPS to HTTP redirect is forbidden")
        attempt_documents: dict[tuple[str, int | None, str], list[DiagnosticAttempt]] = {}
        for item in self.attempts:
            initiating_url = item.redirect_chain[0] if item.redirect_chain else item.requested_url
            document_key = "sitemap-queue" if item.document_kind == "sitemap" else initiating_url
            attempt_documents.setdefault(
                (item.document_kind, item.queue_ordinal, document_key), []
            ).append(item)
        for document_attempts in attempt_documents.values():
            if document_attempts[0].retry_ordinal != 0 or document_attempts[0].redirect_chain:
                raise ValueError("document attempt chain must start at retry/redirect ordinal zero")
            for previous, current in zip(document_attempts, document_attempts[1:]):
                if current.redirect_chain == previous.redirect_chain:
                    if (
                        current.requested_url != previous.requested_url
                        or current.redirect_ordinal != previous.redirect_ordinal
                        or current.retry_ordinal != previous.retry_ordinal + 1
                        or not is_retryable_attempt_outcome(previous.outcome)
                    ):
                        raise ValueError("retry attempt chain is not contiguous")
                elif current.redirect_chain == [
                    *previous.redirect_chain,
                    previous.requested_url,
                ]:
                    if (
                        previous.outcome != "redirect"
                        or current.requested_url != previous.redirect_target_url
                        or current.redirect_ordinal != previous.redirect_ordinal + 1
                        or current.retry_ordinal != previous.retry_ordinal
                    ):
                        raise ValueError("redirect attempt chain is not contiguous")
                else:
                    raise ValueError("attempt redirect chain is not contiguous")
        if (
            self.attempts[0].document_kind != "robots"
            or self.attempts[0].requested_url != self.canonical_origin.as_url_origin() + "/robots.txt"
        ):
            raise ValueError("first diagnostic attempt must be canonical robots.txt")
        attempt_urls = [
            url
            for item in self.attempts
            for url in [
                item.requested_url,
                *item.redirect_chain,
                *(
                    [item.redirect_target_url]
                    if item.redirect_target_url is not None
                    else []
                ),
                *([item.final_url] if item.final_url else []),
            ]
        ]
        if any(_url_origin(url) not in approved for url in attempt_urls):
            raise ValueError("attempt URL is outside allowed_document_origins")
        if any(
            item.identity_id != self.identity.identity_id
            or item.identity_sha256 != self.identity.identity_sha256
            or _origin_key(item.origin) not in approved
            or item.expires_at > self.expires_at
            for item in self.origin_policy_evidence
        ):
            raise ValueError("origin policy evidence is outside diagnostic authority/freshness")
        if len({_origin_key(item.origin) for item in self.origin_policy_evidence}) != len(self.origin_policy_evidence):
            raise ValueError("origin policy evidence origins must be unique")
        if any(
            not (self.started_at <= item.fetched_at <= self.completed_at)
            for item in self.origin_policy_evidence
        ):
            raise ValueError("origin policy timestamp is outside diagnostic interval")
        if any(
            _url_origin(url) not in approved
            for item in self.sitemap_evidence
            for url in [item.url, *([item.final_url] if item.final_url else [])]
        ):
            raise ValueError("sitemap evidence URL is outside allowed_document_origins")
        if (
            self.budget_usage.http_requests > self.budgets.http_requests
            or self.budget_usage.sitemap_document_occurrences > self.budgets.sitemap_documents
            or self.budget_usage.url_occurrences > self.budgets.url_occurrences
        ):
            raise ValueError("budget usage exceeds occurrence/request authority")
        if self.budget_usage.http_requests < len(self.attempts):
            raise ValueError("HTTP request usage cannot be less than recorded attempts")
        robots_attempts = [item for item in self.attempts if item.document_kind == "robots"]
        sitemap_attempts = [item for item in self.attempts if item.document_kind == "sitemap"]
        if any(
            item.wire_bytes > self.budgets.robots_wire_bytes_per_attempt
            or item.decoded_bytes > self.budgets.robots_decoded_bytes_per_attempt
            for item in robots_attempts
        ):
            raise ValueError("robots attempt byte evidence exceeds declared per-attempt budget")
        sitemap_bytes_by_queue: dict[int, tuple[int, int]] = {}
        for item in sitemap_attempts:
            assert item.queue_ordinal is not None
            wire, decoded = sitemap_bytes_by_queue.get(item.queue_ordinal, (0, 0))
            sitemap_bytes_by_queue[item.queue_ordinal] = (
                wire + item.wire_bytes,
                decoded + item.decoded_bytes,
            )
        if any(
            wire > self.budgets.sitemap_wire_bytes_per_document
            or decoded > self.budgets.sitemap_decoded_bytes_per_document
            for wire, decoded in sitemap_bytes_by_queue.values()
        ):
            raise ValueError("sitemap byte evidence exceeds declared per-document budget")
        if (
            self.budget_usage.robots_wire_bytes > self.budgets.robots_wire_bytes_total
            or self.budget_usage.robots_decoded_bytes > self.budgets.robots_decoded_bytes_total
            or self.budget_usage.sitemap_wire_bytes > self.budgets.sitemap_wire_bytes_total
            or self.budget_usage.sitemap_decoded_bytes > self.budgets.sitemap_decoded_bytes_total
        ):
            raise ValueError("aggregate byte evidence exceeds declared budgets")
        completed_policy_attempt_ordinal: dict[tuple[str, str, int], int] = {}
        for policy in self.origin_policy_evidence:
            initiating_robots_url = policy.origin.as_url_origin() + "/robots.txt"
            completed_policy_attempts = [
                item
                for item in robots_attempts
                if (item.redirect_chain[0] if item.redirect_chain else item.requested_url)
                == initiating_robots_url
                and item.outcome in {"success", "completed_empty"}
            ]
            if (
                not completed_policy_attempts
                or completed_policy_attempts[-1].content_sha256 != policy.robots_sha256
            ):
                raise ValueError("origin policy robots digest is not bound to attempt evidence")
            expected_policy_status = (
                "available" if completed_policy_attempts[-1].outcome == "success" else "absent"
            )
            if policy.robots_status != expected_policy_status:
                raise ValueError("origin policy availability does not match robots attempt outcome")
            completed_policy_attempt_ordinal[_origin_key(policy.origin)] = completed_policy_attempts[-1].attempt_ordinal
        policy_origins_in_completion_order = sorted(
            completed_policy_attempt_ordinal,
            key=completed_policy_attempt_ordinal.__getitem__,
        )
        if [
            _origin_key(item.origin) for item in self.origin_policy_evidence
        ] != policy_origins_in_completion_order:
            raise ValueError("origin policy evidence must follow robots completion attempt order")
        if (
            self.budget_usage.robots_wire_bytes != sum(item.wire_bytes for item in robots_attempts)
            or self.budget_usage.robots_decoded_bytes != sum(item.decoded_bytes for item in robots_attempts)
            or self.budget_usage.sitemap_wire_bytes != sum(item.wire_bytes for item in sitemap_attempts)
            or self.budget_usage.sitemap_decoded_bytes != sum(item.decoded_bytes for item in sitemap_attempts)
        ):
            raise ValueError("byte budget usage must equal recorded attempt evidence")
        if len(set(self.accepted_page_urls)) != len(self.accepted_page_urls):
            raise ValueError("accepted_page_urls must be unique")
        canonical = _origin_key(self.canonical_origin)
        if any(_url_origin(url) != canonical for url in self.accepted_page_urls):
            raise ValueError("accepted page URL is outside canonical origin")
        if self.accepted_page_urls != [item.url for item in self.accepted_page_evidence]:
            raise ValueError("accepted page URLs must exactly match accepted page evidence")

        evidence_ordinals = [item.queue_ordinal for item in self.sitemap_evidence]
        if evidence_ordinals != sorted(evidence_ordinals) or len(evidence_ordinals) != len(set(evidence_ordinals)):
            raise ValueError("sitemap evidence must preserve unique FIFO queue order")
        evidence_by_queue = {item.queue_ordinal: item for item in self.sitemap_evidence}
        document_duplicates_by_queue = {
            item.queue_ordinal: item
            for item in self.duplicate_urls
            if item.reason in {
                "duplicate_document_url",
                "duplicate_document_final_url",
                "duplicate_document_digest",
            }
            and item.queue_ordinal is not None
        }
        if set(evidence_by_queue) & set(document_duplicates_by_queue):
            raise ValueError("sitemap queue must have exactly one evidence or duplicate disposition")
        attempts_by_queue: dict[int, list[DiagnosticAttempt]] = {}
        for attempt in sitemap_attempts:
            if attempt.queue_ordinal is None:
                raise ValueError("sitemap attempt requires queue lineage")
            disposition = evidence_by_queue.get(attempt.queue_ordinal)
            duplicate = document_duplicates_by_queue.get(attempt.queue_ordinal)
            if disposition is None and duplicate is None:
                raise ValueError("every sitemap attempt must map to one document disposition")
            parent_sha256 = (
                disposition.parent_sha256 if disposition is not None else duplicate.parent_sha256
            )
            if attempt.parent_sha256 != parent_sha256:
                raise ValueError("sitemap attempt parent does not match document disposition")
            attempts_by_queue.setdefault(attempt.queue_ordinal, []).append(attempt)
        for queue_ordinal, queue_attempts in attempts_by_queue.items():
            evidence = evidence_by_queue.get(queue_ordinal)
            duplicate = document_duplicates_by_queue.get(queue_ordinal)
            disposition_url = evidence.url if evidence is not None else duplicate.url
            if queue_attempts[0].requested_url != disposition_url:
                raise ValueError("first sitemap attempt URL does not match document disposition")
            disposition_final_url = (
                evidence.final_url if evidence is not None else duplicate.final_url
            )
            if (
                disposition_final_url is not None
                and queue_attempts[-1].final_url != disposition_final_url
            ):
                raise ValueError("final sitemap attempt URL does not match document disposition")
            if evidence is None:
                if duplicate.reason == "duplicate_document_digest" and not any(
                    item.attempt_ordinal < queue_attempts[-1].attempt_ordinal
                    and item.document_kind == "sitemap"
                    and item.content_sha256 == queue_attempts[-1].content_sha256
                    for item in self.attempts
                ):
                    raise ValueError("duplicate sitemap digest lacks prior attempt evidence")
                continue
            if queue_attempts[-1].content_sha256 != evidence.document_sha256:
                raise ValueError("sitemap document digest is not bound to attempt evidence")
            final_attempt = queue_attempts[-1]
            if (
                (evidence.root_type == "empty" or evidence.outcome == "completed_empty")
                != (final_attempt.outcome == "completed_empty")
            ):
                raise ValueError("empty sitemap evidence does not match final attempt outcome")
            if evidence.outcome == "parsed" and final_attempt.outcome != "success":
                raise ValueError("parsed sitemap evidence requires a successful final attempt")
        if any(
            item.outcome == "parsed" and item.queue_ordinal not in attempts_by_queue
            for item in self.sitemap_evidence
        ):
            raise ValueError("parsed sitemap evidence requires final attempt lineage")

        final_canonical_robots = [
            item for item in robots_attempts if item.queue_ordinal is None
        ][-1]
        canonical_policies = [
            item for item in self.origin_policy_evidence if _origin_key(item.origin) == canonical
        ]
        policies_by_origin = {
            _origin_key(item.origin): item for item in self.origin_policy_evidence
        }
        attempts_by_ordinal = {
            item.attempt_ordinal: item for item in self.attempts
        }
        robots_error_keys: list[tuple[int, str]] = []
        for error in self.robots_errors:
            attempt = attempts_by_ordinal.get(error.attempt_ordinal)
            initiating_url = error.source_origin.removesuffix("/") + "/robots.txt"
            if (
                attempt is None
                or attempt.document_kind != "robots"
                or attempt.outcome != "success"
                or attempt.content_sha256 != error.document_sha256
                or (
                    attempt.redirect_chain[0]
                    if attempt.redirect_chain
                    else attempt.requested_url
                )
                != initiating_url
            ):
                raise ValueError("robots error lacks successful source-attempt lineage")
            source_key = _url_origin(error.source_origin)
            if source_key in policies_by_origin:
                raise ValueError("robots error source cannot also publish valid policy evidence")
            if source_key == canonical and error.reason.startswith("aux_robots_"):
                raise ValueError("canonical robots error cannot use an auxiliary reason")
            if source_key != canonical and error.reason.startswith("robots_"):
                raise ValueError("auxiliary robots error must use an auxiliary reason")
            robots_error_keys.append((error.attempt_ordinal, error.reason))
        if len(robots_error_keys) != len(set(robots_error_keys)):
            raise ValueError("robots errors must preserve unique attempt/reason evidence")
        for attempt in sitemap_attempts:
            policy = policies_by_origin.get(_url_origin(attempt.requested_url))
            if policy is None or not robots_rules_allow(policy.selected_rules, attempt.requested_url):
                raise ValueError("sitemap attempt is not allowed by visible robots policy")
            if completed_policy_attempt_ordinal.get(_origin_key(policy.origin), len(self.attempts) + 1) >= attempt.attempt_ordinal:
                raise ValueError("sitemap attempt occurred before its origin robots preflight completed")

        def exact_preflight_classification(
            rejection: RejectedUrl,
        ) -> PolicyPreflightClassification | None:
            if rejection.url is None:
                return None
            source_key = _url_origin(rejection.url)
            source_origin = _origin_url_from_key(source_key) + "/"
            robots_url = (
                source_origin.removesuffix("/") + "/robots.txt"
            )
            source_attempts = [
                attempt
                for attempt in robots_attempts
                if attempt.queue_ordinal == rejection.queue_ordinal
                and (
                    attempt.redirect_chain[0]
                    if attempt.redirect_chain
                    else attempt.requested_url
                )
                == robots_url
            ]
            final_attempt = source_attempts[-1] if source_attempts else None
            source_attempt_ordinals = {
                attempt.attempt_ordinal for attempt in source_attempts
            }
            has_related_safety_evidence = any(
                error.source_origin == source_origin
                and error.attempt_ordinal in source_attempt_ordinals
                for error in self.robots_errors
            )

            request_cap_stopped_preflight = (
                "http_request_budget_exhausted" in self.truncation_reasons
                and self.budget_usage.http_requests == self.budgets.http_requests
                and (
                    final_attempt is None
                    or final_attempt.outcome == "redirect"
                    or (
                        is_retryable_attempt_outcome(final_attempt.outcome)
                        and final_attempt.retry_ordinal < 2
                    )
                )
            )
            redirect_cap_stopped_preflight = (
                final_attempt is not None
                and final_attempt.outcome == "redirect"
                and final_attempt.redirect_ordinal
                == self.budgets.redirect_hops_per_document
                and "redirect_hop_budget_exhausted" in self.truncation_reasons
            )
            robots_wire_cap_stopped_preflight = (
                final_attempt is not None
                and final_attempt.outcome == "wire_budget_exhausted"
                and (
                    final_attempt.wire_bytes
                    == self.budgets.robots_wire_bytes_per_attempt
                    or self.budget_usage.robots_wire_bytes
                    == self.budgets.robots_wire_bytes_total
                )
            )
            robots_decoded_cap_stopped_preflight = (
                final_attempt is not None
                and final_attempt.outcome == "decoded_budget_exhausted"
                and (
                    final_attempt.decoded_bytes
                    == self.budgets.robots_decoded_bytes_per_attempt
                    or self.budget_usage.robots_decoded_bytes
                    == self.budgets.robots_decoded_bytes_total
                )
            )
            aggregate_robots_cap_stopped_preflight = (
                not source_attempts
                and "robots_aggregate_byte_budget_exhausted"
                in self.outcome_reasons
                and (
                    self.budget_usage.robots_wire_bytes
                    == self.budgets.robots_wire_bytes_total
                    or self.budget_usage.robots_decoded_bytes
                    == self.budgets.robots_decoded_bytes_total
                )
            )

            source_policy = policies_by_origin.get(source_key)
            counted_aux_lines = {
                occurrence.entry_ordinal
                for occurrence in self.counted_url_occurrences
                if occurrence.source == "aux_robots_sitemap"
                and occurrence.source_origin == source_origin
                and occurrence.queue_ordinal == rejection.queue_ordinal
                and source_policy is not None
                and occurrence.parent_sha256 == source_policy.robots_sha256
            }
            uncounted_policy_directive = (
                source_policy is not None
                and any(
                    directive.line_number not in counted_aux_lines
                    for directive in source_policy.declared_sitemaps
                )
            )
            occurrence_cap_stopped_preflight = (
                uncounted_policy_directive
                and (
                    (
                        "sitemap_document_budget_exhausted"
                        in self.truncation_reasons
                        and self.budget_usage.sitemap_document_occurrences
                        == self.budgets.sitemap_documents
                    )
                    or (
                        "url_occurrence_budget_exhausted"
                        in self.truncation_reasons
                        and self.budget_usage.url_occurrences
                        == self.budgets.url_occurrences
                    )
                )
            )
            return derive_policy_preflight_classification(
                final_attempt_outcome=(
                    final_attempt.outcome if final_attempt is not None else None
                ),
                final_retry_ordinal=(
                    final_attempt.retry_ordinal if final_attempt is not None else None
                ),
                has_related_safety_evidence=has_related_safety_evidence,
                has_related_budget_evidence=any((
                    request_cap_stopped_preflight,
                    redirect_cap_stopped_preflight,
                    robots_wire_cap_stopped_preflight,
                    robots_decoded_cap_stopped_preflight,
                    aggregate_robots_cap_stopped_preflight,
                    occurrence_cap_stopped_preflight,
                )),
            )

        for rejection in self.rejected_urls:
            preflight_reason = rejection.trigger_reason or rejection.reason
            is_policy_preflight = preflight_reason in _POLICY_PREFLIGHT_REASONS
            classification: str | None = None
            if is_policy_preflight:
                classification = preflight_reason.rsplit("_", 1)[-1]
                evidence_classification = exact_preflight_classification(rejection)
                if classification != evidence_classification:
                    raise ValueError(
                        "policy preflight classification does not match robots evidence"
                    )
            if preflight_reason in _REDIRECT_SUBDISPOSITION_REASONS:
                evidence = evidence_by_queue.get(rejection.queue_ordinal)
                queue_attempts = attempts_by_queue.get(rejection.queue_ordinal or -1, [])
                expected_evidence_outcome = (
                    classification
                    if is_policy_preflight
                    else _DIRECT_REDIRECT_EVIDENCE_OUTCOME[preflight_reason]
                )
                if (
                    evidence is None
                    or evidence.root_type != "failed"
                    or evidence.outcome != expected_evidence_outcome
                    or not queue_attempts
                    or queue_attempts[-1].outcome != "redirect"
                    or rejection.url
                    != queue_attempts[-1].redirect_target_url
                ):
                    raise ValueError(
                        "redirect subdisposition lacks matching target evidence"
                    )
            elif is_policy_preflight and rejection.queue_ordinal in attempts_by_queue:
                raise ValueError(
                    "initial sitemap policy preflight cannot contain a sitemap attempt"
                )
            queue_attempts = attempts_by_queue.get(
                rejection.queue_ordinal or -1,
                [],
            )
            if (
                rejection.trigger_reason is None
                and rejection.reason
                in {"http_request_budget_exhausted", "prior_budget_stop"}
                and rejection.request_slot_ordinal is None
                and queue_attempts
                and queue_attempts[-1].outcome == "redirect"
                and rejection.url == queue_attempts[-1].redirect_target_url
            ):
                evidence = evidence_by_queue.get(rejection.queue_ordinal)
                if (
                    evidence is None
                    or evidence.root_type != "failed"
                    or evidence.outcome
                    != "redirect_target_budget_exhausted"
                ):
                    raise ValueError(
                        "redirect-target budget disposition lacks failed evidence"
                    )
            if (
                rejection.trigger_reason is None
                and rejection.reason
                in {"http_request_budget_exhausted", "prior_budget_stop"}
                and rejection.request_slot_ordinal is None
                and queue_attempts
                and is_retryable_attempt_outcome(queue_attempts[-1].outcome)
                and queue_attempts[-1].retry_ordinal < 2
                and rejection.url == queue_attempts[-1].requested_url
            ):
                evidence = evidence_by_queue.get(rejection.queue_ordinal)
                if (
                    evidence is None
                    or evidence.root_type != "failed"
                    or evidence.outcome != "budget"
                ):
                    raise ValueError(
                        "retry budget disposition lacks failed evidence"
                    )
        for evidence in self.sitemap_evidence:
            if evidence.root_type != "failed":
                continue
            queue_attempts = attempts_by_queue.get(evidence.queue_ordinal, [])
            if not queue_attempts:
                continue
            final_attempt = queue_attempts[-1]
            if (
                evidence.outcome == "budget"
                and is_retryable_attempt_outcome(final_attempt.outcome)
                and final_attempt.retry_ordinal < 2
                and "http_request_budget_exhausted"
                in self.truncation_reasons
            ):
                matching_rejections = [
                    rejection
                    for rejection in self.rejected_urls
                    if rejection.queue_ordinal == evidence.queue_ordinal
                    and rejection.url == final_attempt.requested_url
                    and rejection.reason
                    in {
                        "http_request_budget_exhausted",
                        "prior_budget_stop",
                    }
                    and rejection.trigger_reason is None
                    and rejection.request_slot_ordinal is None
                    and rejection.parent_sha256 == evidence.parent_sha256
                    and rejection.entry_ordinal
                    == evidence.parent_entry_ordinal
                ]
                if len(matching_rejections) != 1:
                    raise ValueError(
                        "retry budget disposition cardinality must be one"
                    )
                continue
            if final_attempt.outcome != "redirect":
                continue
            if (
                evidence.outcome == "budget"
                and final_attempt.redirect_ordinal
                == self.budgets.redirect_hops_per_document
                and "redirect_hop_budget_exhausted" in self.truncation_reasons
            ):
                expected_reason = "redirect_hop_budget_exhausted"
            elif evidence.outcome == "redirect_target_budget_exhausted":
                matching_rejections = [
                    rejection
                    for rejection in self.rejected_urls
                    if rejection.queue_ordinal == evidence.queue_ordinal
                    and rejection.url == final_attempt.redirect_target_url
                    and rejection.reason
                    in {
                        "http_request_budget_exhausted",
                        "prior_budget_stop",
                    }
                    and rejection.trigger_reason is None
                    and rejection.request_slot_ordinal is None
                    and rejection.parent_sha256 == evidence.parent_sha256
                    and rejection.entry_ordinal
                    == evidence.parent_entry_ordinal
                ]
                if len(matching_rejections) != 1:
                    raise ValueError(
                        "redirect-target budget disposition cardinality must be one"
                    )
                continue
            elif evidence.outcome in _PREFLIGHT_CLASSIFICATIONS:
                expected_reason = f"redirect_policy_preflight_{evidence.outcome}"
            elif evidence.outcome in _DIRECT_REDIRECT_SUBDISPOSITION_REASONS:
                expected_reason = evidence.outcome
            else:
                continue
            matching_rejections = [
                rejection
                for rejection in self.rejected_urls
                if rejection.queue_ordinal == evidence.queue_ordinal
                and rejection.url == final_attempt.redirect_target_url
                and (rejection.trigger_reason or rejection.reason)
                == expected_reason
                and rejection.parent_sha256 == evidence.parent_sha256
                and rejection.entry_ordinal == evidence.parent_entry_ordinal
            ]
            if len(matching_rejections) != 1:
                raise ValueError(
                    "redirect failed evidence/subdisposition cardinality must be one"
                )
        if canonical_policies and self.robots_sitemap_directives != canonical_policies[0].declared_sitemaps:
            raise ValueError("canonical robots sitemap directives do not match policy evidence")
        canonical_robots_sha = (
            canonical_policies[0].robots_sha256
            if canonical_policies
            else final_canonical_robots.content_sha256
        )
        root_evidence = [item for item in self.sitemap_evidence if item.depth == 0]
        if self.robots_sitemap_directives:
            declared_roots = [
                (item.url, item.line_number) for item in self.robots_sitemap_directives
            ]
            observed_roots = [
                (item.url, item.parent_entry_ordinal) for item in root_evidence
            ]
            declared_positions = {item: index for index, item in enumerate(declared_roots)}
            if (
                any(item not in declared_positions for item in observed_roots)
                or [declared_positions[item] for item in observed_roots]
                != sorted(declared_positions[item] for item in observed_roots)
            ):
                raise ValueError(
                    "root sitemap URL/source lineage does not match canonical robots directives"
                )
        root_dispositions = [
            *root_evidence,
            *[
                item
                for item in [*self.rejected_urls, *self.duplicate_urls]
                if item.parent_sha256 == canonical_robots_sha
            ],
        ]
        if self.robots_sitemap_directives:
            for directive in self.robots_sitemap_directives:
                queues = {
                    item.queue_ordinal
                    for item in root_dispositions
                    if item.url == directive.url
                    and (
                        getattr(item, "parent_entry_ordinal", None) == directive.line_number
                        or getattr(item, "entry_ordinal", None) == directive.line_number
                    )
                }
                if len(queues) != 1:
                    raise ValueError("declared root sitemap occurrence lacks one disposition")
        elif any(item.source == "fallback" for item in self.counted_url_occurrences):
            fallback_url = self.canonical_origin.as_url_origin() + "/sitemap.xml"
            queues = {
                item.queue_ordinal
                for item in root_dispositions
                if item.url == fallback_url
                and getattr(item, "parent_entry_ordinal", None) is None
                and getattr(item, "entry_ordinal", None) is None
            }
            if len(queues) != 1:
                raise ValueError("fallback root sitemap occurrence lacks one disposition")
        occurrence_lineages = [
            (item.source, item.source_origin, item.parent_sha256, item.entry_ordinal)
            for item in self.counted_url_occurrences
        ]
        if len(occurrence_lineages) != len(set(occurrence_lineages)):
            raise ValueError("counted URL occurrence lineage must be unique")
        canonical_directive_lines = {
            item.line_number for item in self.robots_sitemap_directives
        }
        policy_directive_lines_by_origin_digest = {
            (
                item.origin.as_url_origin() + "/",
                item.robots_sha256,
            ): {directive.line_number for directive in item.declared_sitemaps}
            for item in self.origin_policy_evidence
            if item.robots_sha256 is not None
        }
        parsed_sitemap_sources = {
            (
                item.root_type,
                item.document_sha256,
                _origin_url_from_key(_url_origin(item.final_url or item.url)) + "/",
            )
            for item in self.sitemap_evidence
            if item.outcome == "parsed" and item.document_sha256 is not None
        }
        sitemap_disposition_queues = {
            item.queue_ordinal
            for item in [
                *self.sitemap_evidence,
                *self.rejected_urls,
                *self.duplicate_urls,
            ]
            if item.queue_ordinal is not None
        }
        for occurrence in self.counted_url_occurrences:
            if occurrence.source == "robots_sitemap":
                line_reason_prefix = f"line {occurrence.entry_ordinal}: "
                if (
                    occurrence.source_origin != self.canonical_origin.as_url_origin() + "/"
                    or
                    occurrence.parent_sha256 != final_canonical_robots.content_sha256
                    or (
                        occurrence.entry_ordinal not in canonical_directive_lines
                        and not any(
                            reason.startswith(line_reason_prefix)
                            for reason in self.outcome_reasons
                        )
                    )
                ):
                    raise ValueError("robots sitemap occurrence lacks visible directive lineage")
            elif occurrence.source == "aux_robots_sitemap":
                robots_url = occurrence.source_origin.removesuffix("/") + "/robots.txt"
                if (
                    occurrence.entry_ordinal
                    not in policy_directive_lines_by_origin_digest.get(
                        (occurrence.source_origin, occurrence.parent_sha256), set()
                    )
                    or not any(
                        item.document_kind == "robots"
                        and item.queue_ordinal == occurrence.queue_ordinal
                        and item.content_sha256 == occurrence.parent_sha256
                        and (
                            item.redirect_chain[0]
                            if item.redirect_chain
                            else item.requested_url
                        )
                        == robots_url
                        for item in self.attempts
                    )
                ):
                    raise ValueError("auxiliary robots sitemap occurrence lacks policy lineage")
            elif occurrence.source == "fallback" and (
                occurrence.source_origin != self.canonical_origin.as_url_origin() + "/"
                or occurrence.parent_sha256 != final_canonical_robots.content_sha256
                or occurrence.queue_ordinal not in {
                    item.queue_ordinal for item in root_dispositions
                }
            ):
                raise ValueError("fallback occurrence lacks visible root disposition lineage")
            elif occurrence.source in {"sitemapindex", "urlset"} and (
                occurrence.source,
                occurrence.parent_sha256,
                occurrence.source_origin,
            ) not in parsed_sitemap_sources:
                raise ValueError("sitemap XML occurrence lacks document-origin lineage")
            if (
                occurrence.source in {"robots_sitemap", "fallback", "sitemapindex"}
                and occurrence.queue_ordinal is not None
                and occurrence.queue_ordinal not in sitemap_disposition_queues
            ):
                raise ValueError("scheduled sitemap occurrence lacks visible queue disposition")
        prior_documents: dict[str, SitemapEvidence] = {}
        for evidence in self.sitemap_evidence:
            if evidence.depth == 0:
                if canonical_robots_sha is None or evidence.parent_sha256 != canonical_robots_sha:
                    raise ValueError("root sitemap evidence must descend from canonical robots evidence")
            else:
                parent = prior_documents.get(evidence.parent_sha256 or "")
                if (
                    parent is None
                    or evidence.parent_entry_ordinal is None
                    or parent.root_type != "sitemapindex"
                    or parent.outcome != "parsed"
                    or evidence.depth != parent.depth + 1
                ):
                    raise ValueError("nested sitemap evidence must descend from earlier parsed index evidence")
            if evidence.document_sha256 is not None and evidence.outcome == "parsed":
                prior_documents.setdefault(evidence.document_sha256, evidence)

        for occurrence in self.counted_url_occurrences:
            if occurrence.source not in {"sitemapindex", "urlset"}:
                continue
            parent = prior_documents.get(occurrence.parent_sha256 or "")
            if parent is None or parent.root_type != occurrence.source or parent.outcome != "parsed":
                raise ValueError("XML URL occurrence lacks parsed parent sitemap lineage")
            if occurrence.source == "sitemapindex":
                dispositions = [
                    item
                    for item in [*self.sitemap_evidence, *self.rejected_urls, *self.duplicate_urls]
                    if item.queue_ordinal == occurrence.queue_ordinal
                    and item.parent_sha256 == occurrence.parent_sha256
                    and (
                        getattr(item, "parent_entry_ordinal", None) == occurrence.entry_ordinal
                        or getattr(item, "entry_ordinal", None) == occurrence.entry_ordinal
                    )
                ]
            else:
                dispositions = [
                    item
                    for item in [
                        *self.accepted_page_evidence,
                        *self.rejected_urls,
                        *self.duplicate_urls,
                    ]
                    if item.parent_sha256 == occurrence.parent_sha256
                    and item.entry_ordinal == occurrence.entry_ordinal
                ]
            if not dispositions:
                raise ValueError("counted XML URL occurrence lacks one visible disposition")

        def is_document_subdisposition(item: object) -> bool:
            reason = getattr(item, "trigger_reason", None) or getattr(
                item,
                "reason",
                None,
            )
            if reason in _REDIRECT_SUBDISPOSITION_REASONS:
                return True
            queue_ordinal = getattr(item, "queue_ordinal", None)
            queue_attempts = attempts_by_queue.get(queue_ordinal or -1, [])
            if not queue_attempts:
                return False
            final_attempt = queue_attempts[-1]
            continuation_url = (
                final_attempt.redirect_target_url
                if final_attempt.outcome == "redirect"
                else final_attempt.requested_url
                if is_retryable_attempt_outcome(final_attempt.outcome)
                and final_attempt.retry_ordinal < 2
                else None
            )
            return bool(
                getattr(item, "trigger_reason", None) is None
                and reason
                in {"http_request_budget_exhausted", "prior_budget_stop"}
                and getattr(item, "request_slot_ordinal", None) is None
                and continuation_url is not None
                and getattr(item, "url", None)
                == continuation_url
            )

        canonical_directives_by_line = {
            item.line_number: item.url for item in self.robots_sitemap_directives
        }
        for occurrence in self.counted_url_occurrences:
            if occurrence.source == "aux_robots_sitemap" or occurrence.queue_ordinal is None:
                continue
            if occurrence.source == "robots_sitemap":
                expected_url = canonical_directives_by_line.get(occurrence.entry_ordinal)
                if expected_url is None:
                    continue
                dispositions = [
                    item
                    for item in [
                        *root_evidence,
                        *self.rejected_urls,
                        *self.duplicate_urls,
                    ]
                    if item.queue_ordinal == occurrence.queue_ordinal
                    and item.parent_sha256 == occurrence.parent_sha256
                    and item.url == expected_url
                    and not is_document_subdisposition(item)
                ]
            elif occurrence.source == "fallback":
                expected_url = self.canonical_origin.as_url_origin() + "/sitemap.xml"
                dispositions = [
                    item
                    for item in [
                        *root_evidence,
                        *self.rejected_urls,
                        *self.duplicate_urls,
                    ]
                    if item.queue_ordinal == occurrence.queue_ordinal
                    and item.parent_sha256 == occurrence.parent_sha256
                    and item.url == expected_url
                    and not is_document_subdisposition(item)
                ]
            elif occurrence.source == "sitemapindex":
                dispositions = [
                    item
                    for item in [
                        *self.sitemap_evidence,
                        *self.rejected_urls,
                        *self.duplicate_urls,
                    ]
                    if item.queue_ordinal == occurrence.queue_ordinal
                    and item.parent_sha256 == occurrence.parent_sha256
                    and (
                        getattr(item, "parent_entry_ordinal", None) == occurrence.entry_ordinal
                        or getattr(item, "entry_ordinal", None) == occurrence.entry_ordinal
                    )
                    and not is_document_subdisposition(item)
                ]
            else:
                dispositions = [
                    item
                    for item in [
                        *self.accepted_page_evidence,
                        *self.rejected_urls,
                        *self.duplicate_urls,
                    ]
                    if item.parent_sha256 == occurrence.parent_sha256
                    and item.entry_ordinal == occurrence.entry_ordinal
                ]
            if len(dispositions) != 1:
                raise ValueError("counted URL occurrence must have exactly one disposition")

        occurrence_root_keys = {
            (item.queue_ordinal, item.parent_sha256, item.entry_ordinal)
            for item in self.counted_url_occurrences
            if item.source in {"robots_sitemap", "fallback"}
            and item.queue_ordinal is not None
        }
        for item in root_evidence:
            if (
                item.queue_ordinal,
                item.parent_sha256,
                item.parent_entry_ordinal,
            ) not in occurrence_root_keys:
                raise ValueError("root sitemap disposition lacks counted occurrence lineage")
        occurrence_page_keys = {
            (item.parent_sha256, item.entry_ordinal)
            for item in self.counted_url_occurrences
            if item.source == "urlset"
        }
        occurrence_page_schedule_keys = {
            (item.queue_ordinal, item.parent_sha256, item.entry_ordinal)
            for item in self.counted_url_occurrences
            if item.source == "urlset"
        }
        if any(
            (item.parent_sha256, item.entry_ordinal) not in occurrence_page_keys
            for item in self.accepted_page_evidence
        ):
            raise ValueError("accepted page disposition lacks counted occurrence lineage")
        uncounted_root_reasons = {
            "url_occurrence_budget_exhausted",
            "sitemap_document_budget_exhausted",
        }
        if any(
            item.parent_sha256 == canonical_robots_sha
            and not is_document_subdisposition(item)
            and item.reason not in uncounted_root_reasons
            and (item.queue_ordinal, item.parent_sha256, item.entry_ordinal)
            not in occurrence_root_keys
            for item in [*self.rejected_urls, *self.duplicate_urls]
        ):
            raise ValueError("root rejection/duplicate lacks counted occurrence lineage")
        parsed_urlset_digests = {
            item.document_sha256
            for item in self.sitemap_evidence
            if item.root_type == "urlset"
            and item.outcome == "parsed"
            and item.document_sha256 is not None
        }
        if any(
            item.parent_sha256 in parsed_urlset_digests
            and (item.parent_sha256, item.entry_ordinal) not in occurrence_page_keys
            for item in [*self.rejected_urls, *self.duplicate_urls]
        ):
            raise ValueError("page rejection/duplicate lacks counted occurrence lineage")
        occurrence_index_keys = {
            (item.queue_ordinal, item.parent_sha256, item.entry_ordinal)
            for item in self.counted_url_occurrences
            if item.source == "sitemapindex"
        }
        parsed_index_digests = {
            item.document_sha256
            for item in self.sitemap_evidence
            if item.root_type == "sitemapindex"
            and item.outcome == "parsed"
            and item.document_sha256 is not None
        }
        parsed_urlset_digests = {
            item.document_sha256
            for item in self.sitemap_evidence
            if item.root_type == "urlset"
            and item.outcome == "parsed"
            and item.document_sha256 is not None
        }
        scheduled_document_reasons = {
            "document_origin_not_approved",
            *_POLICY_PREFLIGHT_REASONS,
            "sitemap_disallowed_by_robots",
            "redirect_disallowed_by_robots",
            "redirect_hop_budget_exhausted",
            "url_occurrence_budget_exhausted",
            "sitemap_document_budget_exhausted",
            "prior_safety_stop",
            "http_request_budget_exhausted",
            "prior_budget_stop",
        }
        entry_safety_reasons = {"empty_sitemap_loc", "malformed_sitemap_loc"}
        page_reasons = {
            "cross_origin_requires_diagnosis",
            "robots_disallowed",
            "prior_safety_stop",
            *entry_safety_reasons,
        }
        index_only_reasons = {"sitemap_depth_budget_exhausted"}
        parsed_index_by_digest = {
            item.document_sha256: item
            for item in self.sitemap_evidence
            if item.document_sha256 in parsed_index_digests
        }
        parsed_document_completion_slot = {
            item.document_sha256: max(
                attempt.request_slot_ordinal
                for attempt in attempts_by_queue[item.queue_ordinal]
            )
            for item in self.sitemap_evidence
            if item.document_sha256 is not None
            and item.queue_ordinal in attempts_by_queue
            and item.outcome == "parsed"
        }
        policy_causal_reasons = {
            *_POLICY_PREFLIGHT_REASONS,
            "sitemap_disallowed_by_robots",
            "redirect_disallowed_by_robots",
        }
        saw_http_request_budget_stop = False
        saw_non_request_budget_stop = False
        for item in self.rejected_urls:
            effective_reason = item.trigger_reason or item.reason
            if item.trigger_reason is not None and (
                item.reason
                not in {"http_request_budget_exhausted", "prior_budget_stop"}
                or item.trigger_reason
                not in {
                    "document_origin_not_approved",
                    *_POLICY_PREFLIGHT_REASONS,
                    "sitemap_disallowed_by_robots",
                    "redirect_disallowed_by_robots",
                    "redirect_hop_budget_exhausted",
                    "empty_sitemap_loc",
                    "malformed_sitemap_loc",
                }
                or item.trigger_reason == item.reason
            ):
                raise ValueError("rejection trigger reason lacks a governed budget disposition")
            if item.parent_sha256 == canonical_robots_sha:
                context = "root"
            elif item.parent_sha256 in parsed_index_digests:
                context = "index"
            elif item.parent_sha256 in parsed_urlset_digests:
                context = "page"
            else:
                raise ValueError("rejected URL has no root/index/page source lineage")
            allowed_reasons = (
                scheduled_document_reasons
                if context == "root"
                else scheduled_document_reasons | entry_safety_reasons | index_only_reasons
                if context == "index"
                else page_reasons
            )
            if item.reason not in allowed_reasons:
                raise ValueError(f"rejection reason is invalid for {context} source lineage")
            request_budget_disposition = (
                item.reason == "http_request_budget_exhausted"
                or (
                    item.reason == "prior_budget_stop"
                    and "http_request_budget_exhausted"
                    in self.truncation_reasons
                )
            )
            must_have_slot = (
                context in {"root", "index"}
                and not request_budget_disposition
            )
            if (item.request_slot_ordinal is not None) != must_have_slot:
                raise ValueError("rejection request slot does not match source context")
            if item.request_slot_ordinal is not None:
                parent_completion_slot = (
                    final_canonical_robots.request_slot_ordinal
                    if context == "root"
                    else parsed_document_completion_slot.get(item.parent_sha256)
                )
                if (
                    parent_completion_slot is None
                    or item.request_slot_ordinal <= parent_completion_slot
                ):
                    raise ValueError("rejection request slot occurs before its parent attempt completed")
                if effective_reason in policy_causal_reasons and item.url is not None:
                    rejected_origin = _url_origin(item.url)
                    robots_url = _origin_url_from_key(rejected_origin) + "/robots.txt"
                    policy_attempt_slots = [
                        attempt.request_slot_ordinal
                        for attempt in robots_attempts
                        if (
                            attempt.redirect_chain[0]
                            if attempt.redirect_chain
                            else attempt.requested_url
                        )
                        == robots_url
                    ]
                    budget_stopped_before_aux_request = (
                        not policy_attempt_slots
                        and effective_reason in _POLICY_PREFLIGHT_REASONS
                        and exact_preflight_classification(item) == "budget"
                    )
                    if not budget_stopped_before_aux_request and (
                        not policy_attempt_slots
                        or item.request_slot_ordinal <= max(policy_attempt_slots)
                    ):
                        raise ValueError("rejection request slot occurs before robots policy preflight")
            if effective_reason == "document_origin_not_approved" and (
                item.url is None or _url_origin(item.url) in approved
            ):
                raise ValueError("document-origin rejection does not match approved origins")
            if item.reason == "cross_origin_requires_diagnosis" and (
                item.url is None or _url_origin(item.url) == canonical
            ):
                raise ValueError("cross-origin page rejection does not cross canonical origin")
            if item.reason == "robots_disallowed" and (
                item.url is None
                or _url_origin(item.url) != canonical
                or not canonical_policies
                or robots_rules_allow(canonical_policies[0].selected_rules, item.url)
            ):
                raise ValueError("robots-disallowed page rejection contradicts policy")
            if effective_reason in {"sitemap_disallowed_by_robots", "redirect_disallowed_by_robots"}:
                policy = policies_by_origin.get(_url_origin(item.url)) if item.url else None
                if policy is None or robots_rules_allow(policy.selected_rules, item.url):
                    raise ValueError("sitemap robots rejection contradicts policy")
            if effective_reason == "empty_sitemap_loc" and item.raw_value != "":
                raise ValueError("empty sitemap location rejection must preserve an empty raw value")
            if effective_reason == "malformed_sitemap_loc" and not item.raw_value:
                raise ValueError("malformed sitemap location rejection must preserve its raw value")
            if item.reason.endswith("_budget_exhausted") and item.reason not in self.truncation_reasons:
                raise ValueError("budget rejection requires matching top-level truncation reason")
            if item.reason == "http_request_budget_exhausted" or (
                item.reason == "prior_budget_stop"
                and item.request_slot_ordinal is None
            ):
                if self.budget_usage.http_requests != self.budgets.http_requests:
                    raise ValueError("HTTP budget disposition requires usage at the request cap")
                preflight_request_cap_stop = (
                    item.reason == "prior_budget_stop"
                    and effective_reason in _POLICY_PREFLIGHT_REASONS
                    and "http_request_budget_exhausted"
                    in self.truncation_reasons
                    and exact_preflight_classification(item) == "budget"
                )
                if (
                    item.reason == "prior_budget_stop"
                    and not saw_http_request_budget_stop
                    and not preflight_request_cap_stop
                ):
                    raise ValueError("prior-budget disposition requires an earlier HTTP budget stop")
            elif (
                item.reason == "prior_budget_stop"
                and not saw_non_request_budget_stop
            ):
                raise ValueError(
                    "prior-budget disposition requires an earlier non-request budget stop"
                )
            if item.reason == "http_request_budget_exhausted":
                saw_http_request_budget_stop = True
            if item.reason == "sitemap_document_budget_exhausted" and (
                self.budget_usage.sitemap_document_occurrences != self.budgets.sitemap_documents
            ):
                raise ValueError("sitemap document budget disposition requires usage at the cap")
            if item.reason == "url_occurrence_budget_exhausted" and (
                self.budget_usage.url_occurrences != self.budgets.url_occurrences
            ):
                raise ValueError("URL occurrence budget disposition requires usage at the cap")
            if item.reason == "sitemap_depth_budget_exhausted":
                parent_index = parsed_index_by_digest.get(item.parent_sha256)
                if parent_index is None or parent_index.depth + 1 <= self.budgets.sitemap_depth:
                    raise ValueError("sitemap depth disposition does not exceed the depth cap")
            if (
                effective_reason.endswith("_policy_preflight_budget")
                or item.reason
                in {
                    "url_occurrence_budget_exhausted",
                    "sitemap_document_budget_exhausted",
                    "sitemap_depth_budget_exhausted",
                    "redirect_hop_budget_exhausted",
                }
            ):
                saw_non_request_budget_stop = True

        if "http_request_budget_exhausted" in self.truncation_reasons and (
            self.budget_usage.http_requests != self.budgets.http_requests
        ):
            raise ValueError("HTTP request truncation requires usage at the request cap")
        if "sitemap_document_budget_exhausted" in self.truncation_reasons and (
            self.budget_usage.sitemap_document_occurrences != self.budgets.sitemap_documents
        ):
            raise ValueError("sitemap document truncation requires usage at the cap")
        if "url_occurrence_budget_exhausted" in self.truncation_reasons and (
            self.budget_usage.url_occurrences != self.budgets.url_occurrences
        ):
            raise ValueError("URL occurrence truncation requires usage at the cap")
        if "redirect_hop_budget_exhausted" in self.truncation_reasons and not any(
            item.outcome == "redirect"
            and item.redirect_ordinal == self.budgets.redirect_hops_per_document
            for item in self.attempts
        ):
            raise ValueError("redirect-hop truncation lacks an attempt at the redirect cap")
        for byte_kind, usage, per_document_cap, total_cap in (
            (
                "wire",
                self.budget_usage.sitemap_wire_bytes,
                self.budgets.sitemap_wire_bytes_per_document,
                self.budgets.sitemap_wire_bytes_total,
            ),
            (
                "decoded",
                self.budget_usage.sitemap_decoded_bytes,
                self.budgets.sitemap_decoded_bytes_per_document,
                self.budgets.sitemap_decoded_bytes_total,
            ),
        ):
            reason = f"{byte_kind}_budget_exhausted"
            if reason not in self.truncation_reasons:
                continue
            queue_totals = {
                queue_ordinal: sum(
                    getattr(item, f"{byte_kind}_bytes")
                    for item in sitemap_attempts
                    if item.queue_ordinal == queue_ordinal
                )
                for queue_ordinal in {item.queue_ordinal for item in sitemap_attempts}
            }
            if (
                usage != total_cap
                and all(value != per_document_cap for value in queue_totals.values())
            ):
                raise ValueError(f"sitemap {byte_kind} truncation lacks evidence at a byte cap")

        duplicate_page_items: list[RejectedUrl] = []
        for item in self.duplicate_urls:
            if item.trigger_reason is not None:
                raise ValueError("duplicate disposition cannot contain a rejection trigger")
            if item.parent_sha256 == canonical_robots_sha or item.parent_sha256 in parsed_index_digests:
                context = "document"
                allowed_reasons = {
                    "duplicate_document_url",
                    "duplicate_document_final_url",
                    "duplicate_document_digest",
                }
            elif item.parent_sha256 in parsed_urlset_digests:
                context = "page"
                allowed_reasons = {"duplicate_page_url"}
                duplicate_page_items.append(item)
            else:
                raise ValueError("duplicate URL has no document/page source lineage")
            if item.reason not in allowed_reasons:
                raise ValueError(f"duplicate reason is invalid for {context} source lineage")
            if (item.request_slot_ordinal is not None) != (
                duplicate_requires_non_network_slot(item)
            ):
                raise ValueError("duplicate request slot does not match source context")
            if item.request_slot_ordinal is not None:
                parent_completion_slot = (
                    final_canonical_robots.request_slot_ordinal
                    if item.parent_sha256 == canonical_robots_sha
                    else parsed_document_completion_slot.get(item.parent_sha256)
                )
                if (
                    parent_completion_slot is None
                    or item.request_slot_ordinal <= parent_completion_slot
                ):
                    raise ValueError("duplicate request slot occurs before its parent attempt completed")

        page_observation_keys: dict[str, list[tuple[int, int, int]]] = {}
        for item in self.accepted_page_evidence:
            parent_completion_slot = parsed_document_completion_slot.get(
                item.parent_sha256
            )
            if parent_completion_slot is None:
                raise ValueError("accepted page lacks parsed parent completion evidence")
            page_observation_keys.setdefault(item.url, []).append((
                item.source_queue_ordinal,
                parent_completion_slot,
                item.entry_ordinal,
            ))
        ordered_duplicate_pages: list[
            tuple[tuple[int, int, int], RejectedUrl]
        ] = []
        for item in duplicate_page_items:
            if (
                item.url is None
                or item.final_url is not None
                or _url_origin(item.url) != canonical
                or item.queue_ordinal is None
                or item.parent_sha256 is None
                or item.entry_ordinal is None
                or (
                    item.queue_ordinal,
                    item.parent_sha256,
                    item.entry_ordinal,
                )
                not in occurrence_page_schedule_keys
            ):
                raise ValueError(
                    "duplicate page must match a canonical counted page occurrence"
                )
            parent_completion_slot = parsed_document_completion_slot.get(
                item.parent_sha256
            )
            if parent_completion_slot is None:
                raise ValueError("duplicate page lacks parsed parent completion evidence")
            ordered_duplicate_pages.append((
                (
                    item.queue_ordinal,
                    parent_completion_slot,
                    item.entry_ordinal,
                ),
                item,
            ))
        for observation_key, item in sorted(
            ordered_duplicate_pages, key=lambda value: value[0]
        ):
            assert item.url is not None
            if not any(
                earlier_key < observation_key
                for earlier_key in page_observation_keys.get(item.url, [])
            ):
                raise ValueError(
                    "duplicate page URL requires an identical earlier page observation"
                )
            page_observation_keys.setdefault(item.url, []).append(observation_key)

        document_rejections = [
            item
            for item in self.rejected_urls
            if item.queue_ordinal is not None
            and item.parent_sha256 in {canonical_robots_sha, *parsed_index_digests}
            and not is_document_subdisposition(item)
        ]
        document_duplicates = [
            item
            for item in self.duplicate_urls
            if item.queue_ordinal is not None
            and item.reason.startswith("duplicate_document_")
        ]
        expected_primary_queues = {
            item.queue_ordinal for item in self.sitemap_evidence
        } | {
            item.queue_ordinal for item in [*document_rejections, *document_duplicates]
        }
        primary_by_queue: dict[
            int,
            tuple[int | None, str, str | None, int | None, str | None],
        ] = {}

        def add_primary(
            *,
            queue_ordinal: int,
            request_slot_ordinal: int | None,
            kind: str,
            parent_sha256: str | None,
            entry_ordinal: int | None,
            initial_url: str | None,
        ) -> None:
            if queue_ordinal in primary_by_queue:
                raise ValueError("sitemap queue has multiple primary dispositions")
            primary_by_queue[queue_ordinal] = (
                request_slot_ordinal,
                kind,
                parent_sha256,
                entry_ordinal,
                initial_url,
            )

        for queue_ordinal, queue_attempts in attempts_by_queue.items():
            disposition = evidence_by_queue.get(queue_ordinal)
            duplicate = document_duplicates_by_queue.get(queue_ordinal)
            add_primary(
                queue_ordinal=queue_ordinal,
                request_slot_ordinal=queue_attempts[0].request_slot_ordinal,
                kind="attempt",
                parent_sha256=(
                    disposition.parent_sha256
                    if disposition is not None
                    else duplicate.parent_sha256
                ),
                entry_ordinal=(
                    disposition.parent_entry_ordinal
                    if disposition is not None
                    else duplicate.entry_ordinal
                ),
                initial_url=queue_attempts[0].requested_url,
            )
        for item in document_duplicates:
            if item.queue_ordinal not in attempts_by_queue:
                assert item.queue_ordinal is not None
                add_primary(
                    queue_ordinal=item.queue_ordinal,
                    request_slot_ordinal=item.request_slot_ordinal,
                    kind="duplicate",
                    parent_sha256=item.parent_sha256,
                    entry_ordinal=item.entry_ordinal,
                    initial_url=item.url,
                )
        for item in document_rejections:
            assert item.queue_ordinal is not None
            add_primary(
                queue_ordinal=item.queue_ordinal,
                request_slot_ordinal=item.request_slot_ordinal,
                kind="rejection",
                parent_sha256=item.parent_sha256,
                entry_ordinal=item.entry_ordinal,
                initial_url=item.url,
            )
        if set(primary_by_queue) != expected_primary_queues:
            raise ValueError("every sitemap queue requires exactly one primary disposition")
        document_queue_ordinals = sorted(primary_by_queue)
        if document_queue_ordinals and document_queue_ordinals != list(
            range(1, document_queue_ordinals[-1] + 1)
        ):
            raise ValueError("document queue ordinals must be contiguous from one")
        referenced_document_queues = {
            item.queue_ordinal
            for item in self.attempts
            if item.queue_ordinal is not None
        } | {
            item.queue_ordinal for item in self.sitemap_evidence
        } | {
            item.queue_ordinal
            for item in [*self.rejected_urls, *self.duplicate_urls]
            if item.queue_ordinal is not None
        } | {
            item.queue_ordinal
            for item in self.counted_url_occurrences
            if item.queue_ordinal is not None
        } | {
            item.source_queue_ordinal for item in self.accepted_page_evidence
        }
        if not referenced_document_queues.issubset(primary_by_queue):
            raise ValueError("queue reference lacks a document primary disposition")

        duplicate_document_urls_by_queue = {
            item.queue_ordinal: item
            for item in document_duplicates
            if item.reason == "duplicate_document_url"
            and item.queue_ordinal is not None
        }
        earlier_document_urls: dict[str, list[tuple[int, int]]] = {}
        for queue_ordinal in document_queue_ordinals:
            (
                primary_slot,
                _,
                parent_sha256,
                entry_ordinal,
                initial_url,
            ) = primary_by_queue[queue_ordinal]
            duplicate_url = duplicate_document_urls_by_queue.get(queue_ordinal)
            if duplicate_url is not None:
                current_occurrence_key = (
                    queue_ordinal,
                    parent_sha256,
                    entry_ordinal,
                )
                if (
                    duplicate_url.url is None
                    or duplicate_url.final_url is not None
                    or primary_slot is None
                    or (
                        parent_sha256 == canonical_robots_sha
                        and current_occurrence_key not in occurrence_root_keys
                    )
                    or (
                        parent_sha256 in parsed_index_digests
                        and current_occurrence_key not in occurrence_index_keys
                    )
                    or not any(
                        earlier_queue < queue_ordinal
                        and earlier_slot < primary_slot
                        for earlier_queue, earlier_slot in earlier_document_urls.get(
                            duplicate_url.url, []
                        )
                    )
                ):
                    raise ValueError(
                        "duplicate document URL requires an identical earlier document primary"
                    )
            if initial_url is not None and primary_slot is not None:
                earlier_document_urls.setdefault(initial_url, []).append((
                    queue_ordinal,
                    primary_slot,
                ))

        duplicate_document_final_urls_by_queue = {
            item.queue_ordinal: item
            for item in document_duplicates
            if item.reason == "duplicate_document_final_url"
            and item.queue_ordinal is not None
        }
        earlier_observed_document_resources: set[str] = set()
        for queue_ordinal in document_queue_ordinals:
            duplicate_final = duplicate_document_final_urls_by_queue.get(queue_ordinal)
            queue_attempts = attempts_by_queue.get(queue_ordinal, [])
            if duplicate_final is not None:
                current_occurrence_key = (
                    queue_ordinal,
                    duplicate_final.parent_sha256,
                    duplicate_final.entry_ordinal,
                )
                has_counted_occurrence = (
                    duplicate_final.parent_sha256 == canonical_robots_sha
                    and current_occurrence_key in occurrence_root_keys
                ) or (
                    duplicate_final.parent_sha256 in parsed_index_digests
                    and current_occurrence_key in occurrence_index_keys
                )
                if (
                    duplicate_final.url is None
                    or duplicate_final.final_url is None
                    or not has_counted_occurrence
                    or (
                        not queue_attempts
                        and duplicate_final.final_url != duplicate_final.url
                    )
                    or duplicate_final.final_url
                    not in earlier_observed_document_resources
                ):
                    raise ValueError(
                        "duplicate document final URL requires an identical earlier "
                        "observed document initial or final URL"
                    )
            if queue_attempts:
                earlier_observed_document_resources.add(
                    queue_attempts[0].requested_url
                )
                if queue_attempts[-1].final_url is not None:
                    earlier_observed_document_resources.add(
                        queue_attempts[-1].final_url
                    )

        replay_scheduled_initial_urls: set[str] = set()
        replay_observed_document_urls: set[str] = set()
        replay_document_digests: set[str] = set()
        for queue_ordinal in document_queue_ordinals:
            duplicate = document_duplicates_by_queue.get(queue_ordinal)
            queue_attempts = attempts_by_queue.get(queue_ordinal, [])
            initial_url = (
                queue_attempts[0].requested_url
                if queue_attempts
                else duplicate.url if duplicate is not None else None
            )
            if duplicate is not None and initial_url is not None:
                resolved_final_url = (
                    queue_attempts[-1].final_url
                    if queue_attempts
                    else duplicate.final_url
                )
                content_sha256 = (
                    queue_attempts[-1].content_sha256
                    if queue_attempts
                    and queue_attempts[-1].outcome == "success"
                    else None
                )
                expected_reason = document_duplicate_reason(
                    scheduled_initial_url=initial_url,
                    resolved_final_url=resolved_final_url,
                    content_sha256=content_sha256,
                    prior_scheduled_initial_urls=replay_scheduled_initial_urls,
                    prior_observed_document_urls=replay_observed_document_urls,
                    prior_document_digests=replay_document_digests,
                )
                if duplicate.reason != expected_reason:
                    raise ValueError(
                        "document duplicate reason violates URL/final/digest precedence"
                    )
            if queue_attempts:
                replay_scheduled_initial_urls.add(
                    queue_attempts[0].requested_url
                )
                replay_observed_document_urls.add(
                    queue_attempts[0].requested_url
                )
                if queue_attempts[-1].final_url is not None:
                    replay_observed_document_urls.add(
                        queue_attempts[-1].final_url
                    )
                if (
                    queue_attempts[-1].outcome == "success"
                    and queue_attempts[-1].content_sha256 is not None
                ):
                    replay_document_digests.add(
                        queue_attempts[-1].content_sha256
                    )

        slotted_primaries = sorted(
            (
                request_slot_ordinal,
                queue_ordinal,
                kind,
                parent_sha256,
                entry_ordinal,
            )
            for queue_ordinal, (
                request_slot_ordinal,
                kind,
                parent_sha256,
                entry_ordinal,
                _,
            ) in primary_by_queue.items()
            if request_slot_ordinal is not None
        )
        for earlier, later in zip(slotted_primaries, slotted_primaries[1:]):
            if earlier[1] <= later[1]:
                continue
            # A bounded rejection can be allocated while an earlier parent is
            # parsed, before a lower-ordinal queue item is dequeued. Its visible
            # parent completion proves that preallocation; sibling rejection
            # order is checked independently below.
            earlier_parent_completion = (
                final_canonical_robots.request_slot_ordinal
                if earlier[3] == canonical_robots_sha
                else parsed_document_completion_slot.get(earlier[3])
            )
            if not (
                earlier[2] == "rejection"
                and earlier_parent_completion is not None
                and earlier_parent_completion < earlier[0]
            ):
                raise ValueError(
                    "sitemap primary dispositions must preserve FIFO queue causality"
                )

        scheduled_entries_by_parent: dict[str, list[tuple[int, int]]] = {}
        for (
            request_slot_ordinal,
            _,
            kind,
            parent_sha256,
            entry_ordinal,
        ) in slotted_primaries:
            if (
                kind == "rejection"
                and parent_sha256 is not None
                and entry_ordinal is not None
            ):
                scheduled_entries_by_parent.setdefault(parent_sha256, []).append((
                    request_slot_ordinal,
                    entry_ordinal,
                ))
        if any(
            [entry for _, entry in sorted(items)]
            != sorted(entry for _, entry in items)
            for items in scheduled_entries_by_parent.values()
        ):
            raise ValueError(
                "scheduled sitemap rejections must preserve parent entry order"
            )

        if self.decisive_priority != 1 and any(
            (item.trigger_reason or item.reason)
            in {
                "document_origin_not_approved",
                "sitemap_policy_preflight_safety",
                "redirect_policy_preflight_safety",
                "sitemap_disallowed_by_robots",
                "redirect_disallowed_by_robots",
                "empty_sitemap_loc",
                "malformed_sitemap_loc",
                "prior_safety_stop",
            }
            for item in self.rejected_urls
        ):
            raise ValueError("safety rejection evidence requires priority one")
        nested_primary_dispositions = [
            *[item for item in self.sitemap_evidence if item.depth > 0],
            *[
                item
                for item in [*self.rejected_urls, *self.duplicate_urls]
                if item.parent_sha256 in parsed_index_digests
                and not is_document_subdisposition(item)
                and item.reason != "url_occurrence_budget_exhausted"
            ],
        ]
        if any(
            (
                item.queue_ordinal,
                item.parent_sha256,
                getattr(item, "parent_entry_ordinal", None)
                or getattr(item, "entry_ordinal", None),
            )
            not in occurrence_index_keys
            for item in nested_primary_dispositions
        ):
            raise ValueError("nested sitemap disposition lacks counted occurrence lineage")

        for item in self.accepted_page_evidence:
            evidence = evidence_by_queue.get(item.source_queue_ordinal)
            policy = policies_by_origin.get(_url_origin(item.url))
            if (
                evidence is None
                or evidence.root_type != "urlset"
                or evidence.outcome != "parsed"
                or evidence.document_sha256 != item.parent_sha256
                or policy is None
                or not robots_rules_allow(policy.selected_rules, item.url)
            ):
                raise ValueError("accepted page evidence has invalid sitemap lineage")

        if self.recommendation == "sitemap_seeded":
            if not self.accepted_page_evidence or not canonical_policies:
                raise ValueError("sitemap-seeded outcome requires accepted evidence and canonical policy")
        elif self.accepted_page_evidence and not (
            (
                self.diagnostic_status == "blocked"
                and self.recommendation == "operator_review"
                and self.decisive_priority in {1, 6}
            )
            or (
                self.diagnostic_status == "retryable"
                and self.recommendation == "retry_diagnosis"
                and self.decisive_priority == 4
            )
        ):
            raise ValueError(
                "accepted observations contradict the terminal diagnostic outcome"
            )
        if self.recommendation == "bounded_homepage_fallback" and not canonical_policies:
            raise ValueError("bounded fallback requires canonical policy evidence")
        if self.recommendation == "bounded_homepage_fallback":
            homepage = self.canonical_origin.as_url_origin() + "/"
            if not robots_rules_allow(canonical_policies[0].selected_rules, homepage):
                raise ValueError("bounded fallback homepage is disallowed by visible robots policy")
        if not self.robots_sitemap_directives and self.sitemap_evidence:
            fallback_url = self.canonical_origin.as_url_origin() + "/sitemap.xml"
            root_evidence = [item for item in self.sitemap_evidence if item.depth == 0]
            if (
                len(root_evidence) != 1
                or root_evidence[0].url != fallback_url
                or root_evidence[0].parent_entry_ordinal is not None
            ):
                raise ValueError("undeclared sitemap fallback must be exact canonical /sitemap.xml")

        deterministic_attempt_outcomes = {
            "body_incomplete",
            "redirect_missing_location",
            "redirect_malformed_location",
            "final_informational",
            "terminal_http",
            "transport_malformed_status",
        }
        explicit_safety_attempt_outcomes = {
            "authority_http",
            BODY_TLS_POLICY_OUTCOME,
            "robots_unsupported_mime_or_charset",
            "sitemap_unsupported_mime",
            "unclassified_http",
            "redirect_authority_failure",
            "unclassified_transport",
        }
        safety_rejection_reasons = {
            "document_origin_not_approved",
            "sitemap_disallowed_by_robots",
            "redirect_disallowed_by_robots",
            "empty_sitemap_loc",
            "malformed_sitemap_loc",
        }
        benign_dedup_outcomes = {
            "duplicate_document_final_url",
            "duplicate_document_digest",
        }
        deterministic_failure_outcomes = {
            "deterministic",
            "xml_syntax_error",
        }
        non_safety_failure_outcomes = (
            deterministic_failure_outcomes
            | benign_dedup_outcomes
            | {
                "transient",
                "budget",
                "redirect_target_budget_exhausted",
            }
        )
        derived_non_safety_reasons = set(self.truncation_reasons) | {
            "xml_syntax_error",
            "body_incomplete",
            "body_remote_disconnected",
            "body_transient",
            "redirect_missing_location",
            "redirect_malformed_location",
        }
        for item in self.attempts:
            if item.outcome in deterministic_attempt_outcomes and item.http_status is not None:
                derived_non_safety_reasons.add(f"http:{item.http_status}")
            if item.retry_ordinal == 2 and item.outcome == "transient_http" and item.http_status is not None:
                derived_non_safety_reasons.add(f"http:{item.http_status}")
            if item.outcome.startswith("transport_"):
                transport_reason = item.outcome.removeprefix("transport_")
                if (
                    item.outcome == "transport_malformed_status"
                    or (
                        item.retry_ordinal == 2
                        and is_retryable_attempt_outcome(item.outcome)
                    )
                ):
                    derived_non_safety_reasons.add(f"transport:{transport_reason}")

        expected_outcome_reasons = set(self.truncation_reasons)
        expected_outcome_reasons.update(item.reason for item in self.robots_errors)
        for index, attempt in enumerate(self.attempts):
            outcome = attempt.outcome
            if outcome in {"authority_http", "unclassified_http"}:
                if attempt.http_status is not None:
                    expected_outcome_reasons.add(f"http:{attempt.http_status}")
                continue
            if outcome in {"final_informational", "terminal_http"}:
                if attempt.http_status is not None:
                    expected_outcome_reasons.add(f"http:{attempt.http_status}")
                continue
            if outcome == "transient_http":
                if attempt.retry_ordinal == 2 and attempt.http_status is not None:
                    expected_outcome_reasons.add(f"http:{attempt.http_status}")
                continue
            if outcome == "unclassified_transport":
                expected_outcome_reasons.add("unclassified_transport")
                continue
            if outcome.startswith("transport_") and outcome != "transport_returned_non_bytes":
                transport_reason = f"transport:{outcome.removeprefix('transport_')}"
                if outcome == "transport_malformed_status":
                    expected_outcome_reasons.add(transport_reason)
                elif is_retryable_attempt_outcome(outcome):
                    has_later_retry = any(
                        later.document_kind == attempt.document_kind
                        and later.queue_ordinal == attempt.queue_ordinal
                        and later.requested_url == attempt.requested_url
                        and later.redirect_chain == attempt.redirect_chain
                        and later.retry_ordinal == attempt.retry_ordinal + 1
                        for later in self.attempts[index + 1:]
                    )
                    if attempt.retry_ordinal == 2 or (
                        not has_later_retry
                        and "http_request_budget_exhausted" not in self.truncation_reasons
                    ):
                        expected_outcome_reasons.add(transport_reason)
                else:
                    expected_outcome_reasons.add(transport_reason)
                continue
            if outcome in {"redirect_missing_location", "redirect_malformed_location"}:
                expected_outcome_reasons.add(outcome)
                continue
            if outcome == "redirect_authority_failure":
                expected_outcome_reasons.add(outcome)
                continue
            if outcome in {"success", "completed_empty", "redirect"}:
                continue
            if is_retryable_attempt_outcome(outcome):
                has_later_retry = any(
                    later.document_kind == attempt.document_kind
                    and later.queue_ordinal == attempt.queue_ordinal
                    and later.requested_url == attempt.requested_url
                    and later.redirect_chain == attempt.redirect_chain
                    and later.retry_ordinal == attempt.retry_ordinal + 1
                    for later in self.attempts[index + 1:]
                )
                if attempt.retry_ordinal == 2 or (
                    not has_later_retry
                    and "http_request_budget_exhausted" not in self.truncation_reasons
                ):
                    expected_outcome_reasons.add(outcome)
                continue
            if outcome in {"wire_budget_exhausted", "decoded_budget_exhausted"}:
                if attempt.document_kind == "robots":
                    expected_outcome_reasons.add(outcome)
                continue
            expected_outcome_reasons.add(outcome)

        expected_outcome_reasons.update(
            item.outcome
            for item in self.sitemap_evidence
            if item.root_type == "failed"
            and item.outcome
            not in {
                "safety",
                "deterministic",
                "transient",
                "budget",
                "redirect_target_budget_exhausted",
            }
        )
        expected_outcome_reasons.update(
            item.trigger_reason or item.reason
            for item in self.rejected_urls
            if (item.trigger_reason or item.reason)
            in {
                "document_origin_not_approved",
                "sitemap_disallowed_by_robots",
                "redirect_disallowed_by_robots",
                "empty_sitemap_loc",
                "malformed_sitemap_loc",
            }
        )
        if (
            any(
                (item.trigger_reason or item.reason) in _POLICY_PREFLIGHT_REASONS
                for item in self.rejected_urls
            )
            and (
                self.budget_usage.robots_wire_bytes == self.budgets.robots_wire_bytes_total
                or self.budget_usage.robots_decoded_bytes
                == self.budgets.robots_decoded_bytes_total
            )
            and any(
                rejected.url is not None
                and (
                    not (
                        source_attempts := [
                            attempt
                            for attempt in robots_attempts
                            if (
                                attempt.redirect_chain[0]
                                if attempt.redirect_chain
                                else attempt.requested_url
                            )
                            == _origin_url_from_key(_url_origin(rejected.url))
                            + "/robots.txt"
                        ]
                    )
                    or (
                        is_retryable_attempt_outcome(source_attempts[-1].outcome)
                        and source_attempts[-1].retry_ordinal < 2
                    )
                )
                for rejected in self.rejected_urls
                if (rejected.trigger_reason or rejected.reason)
                in _POLICY_PREFLIGHT_REASONS
            )
        ):
            expected_outcome_reasons.add("robots_aggregate_byte_budget_exhausted")
        if not self.accepted_page_evidence and any(
            item.reason == "robots_disallowed" for item in self.rejected_urls
        ):
            expected_outcome_reasons.add(
                "all_canonical_page_candidates_disallowed_by_robots"
            )
        derived_homepage_allowed = bool(canonical_policies) and robots_rules_allow(
            canonical_policies[0].selected_rules,
            self.canonical_origin.as_url_origin() + "/",
        )
        complete_for_homepage_fallback = all(
            item.root_type == "empty"
            or (
                item.root_type in {"urlset", "sitemapindex"}
                and item.outcome == "parsed"
            )
            or item.outcome in benign_dedup_outcomes
            for item in self.sitemap_evidence
        )
        if (
            not self.accepted_page_evidence
            and not expected_outcome_reasons
            and canonical_policies
            and final_canonical_robots.outcome in {"success", "completed_empty"}
            and complete_for_homepage_fallback
            and not derived_homepage_allowed
            and not any(
                item.reason == "cross_origin_requires_diagnosis"
                for item in self.rejected_urls
            )
        ):
            expected_outcome_reasons.add(
                "homepage_fallback_disallowed_by_robots"
            )
        if set(self.outcome_reasons) != expected_outcome_reasons:
            raise ValueError(
                "outcome reasons must exactly match terminal evidence"
            )
        homepage_allowed = bool(canonical_policies) and robots_rules_allow(
            canonical_policies[0].selected_rules,
            self.canonical_origin.as_url_origin() + "/",
        )
        has_safety = (
            any(item.outcome in explicit_safety_attempt_outcomes for item in self.attempts)
            or any(
                item.outcome.startswith("transport_")
                and item.outcome != "transport_malformed_status"
                and not is_retryable_attempt_outcome(item.outcome)
                for item in self.attempts
            )
            or any(
                item.root_type == "failed" and item.outcome not in non_safety_failure_outcomes
                for item in self.sitemap_evidence
            )
            or any(
                (item.trigger_reason or item.reason) in safety_rejection_reasons
                for item in self.rejected_urls
            )
            or (
                not self.accepted_page_evidence
                and any(item.reason == "robots_disallowed" for item in self.rejected_urls)
            )
            or (
                not canonical_policies
                and final_canonical_robots.outcome in {"success", "completed_empty"}
            )
            or any(reason not in derived_non_safety_reasons for reason in self.outcome_reasons)
        )
        has_deterministic = (
            any(item.outcome in deterministic_attempt_outcomes for item in self.attempts)
            or any(item.outcome in deterministic_failure_outcomes for item in self.sitemap_evidence)
        )
        has_transient = (
            any(item.outcome == "transient" for item in self.sitemap_evidence)
            or any(
                item.retry_ordinal == 2
                and (
                    is_retryable_attempt_outcome(item.outcome)
                )
                for item in self.attempts
            )
        )
        has_truncation = bool(self.truncation_reasons)
        complete_sitemap_observation = all(
            (item.root_type == "empty" and item.outcome == "completed_empty")
            or (item.root_type in {"urlset", "sitemapindex"} and item.outcome == "parsed")
            or item.outcome in benign_dedup_outcomes
            for item in self.sitemap_evidence
        )
        fallback_eligible = (
            bool(canonical_policies)
            and final_canonical_robots.outcome in {"success", "completed_empty"}
            and complete_sitemap_observation
            and not self.accepted_page_evidence
            and not has_truncation
            and not has_deterministic
            and not has_transient
            and homepage_allowed
            and not any(item.reason == "cross_origin_requires_diagnosis" for item in self.rejected_urls)
        )
        if has_safety:
            evidence_priority = 1
        elif self.accepted_page_evidence:
            evidence_priority = 2 if (has_truncation or has_deterministic or has_transient) else 3
        elif has_transient and not has_truncation and not has_deterministic:
            evidence_priority = 4
        elif fallback_eligible:
            evidence_priority = 5
        else:
            evidence_priority = 6
        if self.decisive_priority != evidence_priority:
            raise ValueError("decisive priority does not match terminal diagnostic evidence")
        if len(set(self.truncation_reasons)) != len(self.truncation_reasons):
            raise ValueError("truncation reasons must be unique")
        if len(set(self.outcome_reasons)) != len(self.outcome_reasons):
            raise ValueError("outcome reasons must be unique")
        if any(reason not in self.outcome_reasons for reason in self.truncation_reasons):
            raise ValueError("every truncation reason must be present in outcome reasons")
        if self.decisive_priority in {3, 5} and self.outcome_reasons:
            raise ValueError("complete diagnostic outcome cannot contain terminal reasons")
        scheduled_rejection_reasons = {
            "url_occurrence_budget_exhausted",
            "sitemap_document_budget_exhausted",
            "sitemap_depth_budget_exhausted",
            "redirect_hop_budget_exhausted",
            "empty_sitemap_loc",
            "malformed_sitemap_loc",
            "prior_safety_stop",
            "http_request_budget_exhausted",
            "prior_budget_stop",
        }
        if any(
            item.reason in scheduled_rejection_reasons
            and (
                item.queue_ordinal is None
                or item.parent_sha256 is None
                or (
                    item.entry_ordinal is None
                    and item.url != self.canonical_origin.as_url_origin() + "/sitemap.xml"
                )
            )
            for item in self.rejected_urls
        ):
            raise ValueError("rejected scheduled sitemap evidence is missing queue lineage")
        return self

    def digest_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"artifact_sha256"})

    def expected_artifact_sha256(self) -> str:
        return canonical_sha256(self.digest_payload())

    def verify_artifact_sha256(self) -> None:
        if self.artifact_sha256 != self.expected_artifact_sha256():
            raise ValueError("site diagnostic artifact digest mismatch")


def _url_origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    try:
        host = str(ipaddress.ip_address(parsed.hostname)).lower()
    except ValueError:
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    return parsed.scheme.lower(), host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
