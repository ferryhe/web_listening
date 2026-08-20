from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from web_listening.contracts._protocol import (
    NonEmptyString,
    Sha256,
    StrictContractModel,
    contains_uri_userinfo,
    is_secret_like_key,
    require_aware_timestamp,
)
from web_listening.contracts.site_diagnostic import (
    DiagnosticIdentity,
    NormalizedOrigin,
    OriginPolicyEvidence,
    RobotsPolicyRule,
    canonical_json,
    canonical_sha256,
    canonicalize_requested_http_url,
    robots_rule_matches,
    robots_rule_specificity,
    robots_rules_allow,
)


ACCESS_POLICY_VERSION = "access-policy.v1"
ACCESS_DECISION_VERSION = "access-decision.v1"
MAX_RESERVATION_TIMESTAMP = datetime(
    9998,
    12,
    31,
    23,
    59,
    59,
    999999,
    tzinfo=timezone.utc,
)
MAX_RESERVATION_ORDINAL = 1_000_000
MAX_PACING_INTERVAL_MS = 86_400_000
MAX_BUDGET_WINDOW_SECONDS = 86_400
MAX_ORIGIN_BUDGET_LIMIT = 1_000_000

RobotsObservationKind = Literal[
    "valid_200",
    "http_404",
    "http_401",
    "http_403",
    "timeout",
    "dns_error",
    "network_error",
    "parse_error",
]
AccessOutcome = Literal["allow", "reject", "error"]
AccessReasonCode = Literal[
    "robots.allowed",
    "robots.disallowed",
    "robots.absent",
    "robots.auth_required",
    "robots.forbidden",
    "robots.timeout",
    "robots.dns_error",
    "robots.network_error",
    "robots.parse_error",
    "contract.invalid",
]
RuleSource = Literal[
    "origin_policy_evidence",
    "robots_absent",
    "http_status",
    "transport",
    "parser",
]
HttpStatus = Literal[200, 401, 403, 404]
EnvelopeMessage = Literal[
    "access rejected by governed robots policy",
    "access failed closed while resolving robots policy",
    "access contract validation failed",
]

_REJECT_REASONS = {
    "robots.disallowed",
    "robots.auth_required",
    "robots.forbidden",
}
_ERROR_RETRYABILITY = {
    "robots.timeout": True,
    "robots.dns_error": True,
    "robots.network_error": True,
    "robots.parse_error": False,
    "contract.invalid": False,
}
_SENSITIVE_USER_AGENT_RE = re.compile(
    r"(?:authorization|cookie|proxy-authorization)\s*[:=]|\bbearer\s+",
    re.IGNORECASE,
)
_KEY_VALUE_RE = re.compile(
    r"(?<![^\s/?&#:=;,()\[\]{}])"
    r"([^\s/?&#:=;,()\[\]{}]+)"
    r"(?=(?:\s*[:=]\s*|\s+)\S)"
)
_QUERY_KEY_RE = re.compile(r"(?:^|[?&;])([^?&;=]+)=")
_QUERY_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_QUERY_RAW_SAFE = _QUERY_UNRESERVED | frozenset("!$&'()*+,;=:@/?")


def _clean_string(value: str) -> str:
    if value != value.strip() or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ValueError(
            "value must not contain surrounding whitespace or control characters"
        )
    return value


def _validate_non_sensitive_text(value: str, *, location: str) -> str:
    if contains_uri_userinfo(value):
        raise ValueError(f"{location} must not contain sensitive material")
    candidate = value
    for _ in range(3):
        normalized = unicodedata.normalize("NFKC", candidate)
        if _SENSITIVE_USER_AGENT_RE.search(normalized) or any(
            is_secret_like_key(match.group(1))
            for match in _KEY_VALUE_RE.finditer(normalized)
        ):
            raise ValueError(f"{location} must not contain sensitive material")
        candidate = unquote(candidate)
    return value


def _query_decoding_passes(query: str) -> list[str]:
    passes: list[str] = []
    current = query
    for _ in range(3):
        if current in passes:
            break
        passes.append(current)
        decoded = unquote_plus(current)
        if decoded == current:
            break
        current = decoded
    return passes


def _canonical_query(query: str) -> str:
    if any(not char.isascii() or not "!" <= char <= "~" for char in query):
        raise ValueError("URL query must contain only visible ASCII characters")
    if re.search(r"%(?![0-9A-Fa-f]{2})", query):
        raise ValueError("URL query contains malformed percent encoding")
    if any(char not in _QUERY_RAW_SAFE and char != "%" for char in query):
        raise ValueError("URL query contains a non-canonical raw character")

    def replace(match: re.Match[str]) -> str:
        byte = int(match.group(1), 16)
        char = chr(byte)
        return char if char in _QUERY_UNRESERVED else f"%{byte:02X}"

    return re.sub(r"%([0-9A-Fa-f]{2})", replace, query)


def _canonical_url(value: str) -> str:
    if contains_uri_userinfo(value):
        raise ValueError("URL must not contain credentials or userinfo")
    parsed = urlsplit(value)
    for query in _query_decoding_passes(parsed.query):
        inspection_query = unicodedata.normalize("NFKC", query)
        for match in _QUERY_KEY_RE.finditer(inspection_query):
            if is_secret_like_key(match.group(1)):
                raise ValueError("URL must not contain sensitive query keys")
    canonical_url = canonicalize_requested_http_url(value)
    canonical_parts = urlsplit(canonical_url)
    canonical_url = urlunsplit(
        (
            canonical_parts.scheme,
            canonical_parts.netloc,
            canonical_parts.path,
            _canonical_query(canonical_parts.query),
            "",
        )
    )
    if value != canonical_url:
        raise ValueError("URL must use the complete canonical HTTP(S) representation")
    return value


def _portable_reservation_timestamp(value: datetime) -> datetime:
    value = require_aware_timestamp(value)
    try:
        outside_portable_range = value > MAX_RESERVATION_TIMESTAMP
    except (OverflowError, ValueError) as exc:
        raise ValueError("reservation timestamp is outside the portable range") from exc
    if outside_portable_range:
        raise ValueError("reservation timestamp is outside the portable range")
    return value


def _origin_for_url(value: str) -> NormalizedOrigin:
    parsed = urlsplit(value)
    assert parsed.hostname is not None
    return NormalizedOrigin(
        scheme=parsed.scheme,
        host=parsed.hostname,
        effective_port=parsed.port or (443 if parsed.scheme == "https" else 80),
    )


def _identity_sha256(identity: DiagnosticIdentity) -> str:
    return canonical_sha256(
        {
            "identity_id": identity.identity_id,
            "product_token": identity.product_token,
            "user_agent": identity.user_agent,
        }
    )


def access_policy_cache_key_sha256(
    *, canonical_origin: NormalizedOrigin, identity_sha256: str
) -> str:
    """Return the frozen origin + identity + policy-version cache-key digest."""
    return canonical_sha256(
        {
            "canonical_origin": canonical_origin.as_url_origin(),
            "identity_sha256": identity_sha256,
            "policy_version": ACCESS_POLICY_VERSION,
        }
    )


class RobotsObservation(StrictContractModel):
    kind: RobotsObservationKind
    http_status: HttpStatus | None

    @model_validator(mode="after")
    def exact_status_shape(self) -> "RobotsObservation":
        expected = {
            "valid_200": 200,
            "http_404": 404,
            "http_401": 401,
            "http_403": 403,
            "timeout": None,
            "dns_error": None,
            "network_error": None,
            "parse_error": None,
        }[self.kind]
        if self.http_status != expected:
            raise ValueError(
                "robots observation kind/http_status relationship is invalid"
            )
        return self


class _AccessPolicyPayload(StrictContractModel):
    schema_version: Literal["access-policy.v1"]
    policy_version: Literal["access-policy.v1"]
    canonical_origin: NormalizedOrigin
    identity: DiagnosticIdentity
    cache_key_sha256: Sha256
    diagnostic_artifact_sha256: Sha256
    robots_observation: RobotsObservation
    origin_policy_evidence: OriginPolicyEvidence | None
    observed_at: datetime
    expires_at: datetime

    _validate_times = field_validator("observed_at", "expires_at")(
        require_aware_timestamp
    )

    @model_validator(mode="after")
    def valid_policy_binding(self) -> "_AccessPolicyPayload":
        if self.identity.identity_sha256 != _identity_sha256(self.identity):
            raise ValueError("access identity digest mismatch")
        for field_name in ("identity_id", "user_agent", "product_token"):
            _validate_non_sensitive_text(
                getattr(self.identity, field_name),
                location=f"identity.{field_name}",
            )
        expected_cache_key = access_policy_cache_key_sha256(
            canonical_origin=self.canonical_origin,
            identity_sha256=self.identity.identity_sha256,
        )
        if self.cache_key_sha256 != expected_cache_key:
            raise ValueError("access policy cache key mismatch")
        if (
            self.expires_at < self.observed_at
            or self.expires_at - self.observed_at > timedelta(hours=24)
        ):
            raise ValueError(
                "access policy freshness must be between zero and 24 hours"
            )

        evidence = self.origin_policy_evidence
        evidence_required = self.robots_observation.kind in {"valid_200", "http_404"}
        if evidence_required != (evidence is not None):
            raise ValueError(
                "origin policy evidence has invalid required/nullability shape"
            )
        if evidence is None:
            return self
        _validate_non_sensitive_text(
            evidence.identity_id,
            location="origin_policy_evidence.identity_id",
        )
        for warning in evidence.warnings:
            _validate_non_sensitive_text(
                warning,
                location="origin_policy_evidence.warnings",
            )
        for rule in evidence.selected_rules:
            _validate_non_sensitive_text(
                rule.pattern,
                location="origin_policy_evidence.selected_rules",
            )
        for sitemap in evidence.declared_sitemaps:
            _canonical_url(sitemap.url)
        expected_status = (
            "available" if self.robots_observation.kind == "valid_200" else "absent"
        )
        if evidence.robots_status != expected_status:
            raise ValueError("robots observation conflicts with origin policy evidence")
        if evidence.robots_sha256 is None:
            raise ValueError(
                "bound origin policy evidence must retain the robots digest"
            )
        if (
            evidence.origin != self.canonical_origin
            or evidence.identity_id != self.identity.identity_id
            or evidence.identity_sha256 != self.identity.identity_sha256
            or evidence.fetched_at != self.observed_at
            or evidence.expires_at != self.expires_at
        ):
            raise ValueError("origin policy evidence binding mismatch")
        return self


class AccessPolicy(_AccessPolicyPayload):
    policy_id: NonEmptyString
    policy_sha256: Sha256

    _clean_policy_id = field_validator("policy_id")(_clean_string)

    def expected_policy_sha256(self) -> str:
        return canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"policy_id", "policy_sha256"},
            )
        )

    @model_validator(mode="after")
    def valid_policy_identity(self) -> "AccessPolicy":
        expected = self.expected_policy_sha256()
        if (
            self.policy_sha256 != expected
            or self.policy_id != f"access-policy-{expected[:16]}"
        ):
            raise ValueError("access policy ID/digest mismatch")
        return self


def build_access_policy(
    *,
    canonical_origin: NormalizedOrigin,
    identity: DiagnosticIdentity,
    diagnostic_artifact_sha256: str,
    robots_observation: RobotsObservation,
    origin_policy_evidence: OriginPolicyEvidence | None,
    observed_at: datetime,
    expires_at: datetime,
) -> AccessPolicy:
    """Build a digest-bound access policy without performing any I/O."""
    payload = _AccessPolicyPayload(
        schema_version=ACCESS_POLICY_VERSION,
        policy_version=ACCESS_POLICY_VERSION,
        canonical_origin=canonical_origin,
        identity=identity,
        cache_key_sha256=access_policy_cache_key_sha256(
            canonical_origin=canonical_origin,
            identity_sha256=identity.identity_sha256,
        ),
        diagnostic_artifact_sha256=diagnostic_artifact_sha256,
        robots_observation=robots_observation,
        origin_policy_evidence=origin_policy_evidence,
        observed_at=observed_at,
        expires_at=expires_at,
    )
    digest = canonical_sha256(payload.model_dump(mode="json"))
    return AccessPolicy(
        **payload.model_dump(mode="python"),
        policy_id=f"access-policy-{digest[:16]}",
        policy_sha256=digest,
    )


class AccessDecisionEvidence(StrictContractModel):
    diagnostic_artifact_sha256: Sha256
    access_policy_id: NonEmptyString
    access_policy_sha256: Sha256
    cache_key_sha256: Sha256
    identity_sha256: Sha256
    canonical_origin: NormalizedOrigin
    robots_observation: RobotsObservation
    policy_observed_at: datetime
    policy_expires_at: datetime
    origin_policy_id: NonEmptyString | None
    origin_policy_sha256: Sha256 | None
    robots_sha256: Sha256 | None

    _validate_times = field_validator("policy_observed_at", "policy_expires_at")(
        require_aware_timestamp
    )
    _clean_ids = field_validator("access_policy_id", "origin_policy_id")(
        lambda value: _clean_string(value) if value is not None else value
    )

    @model_validator(mode="after")
    def exact_evidence_semantics(self) -> "AccessDecisionEvidence":
        _validate_non_sensitive_text(
            self.access_policy_id,
            location="evidence.access_policy_id",
        )
        if self.access_policy_id != f"access-policy-{self.access_policy_sha256[:16]}":
            raise ValueError("evidence access policy ID/digest mismatch")
        if self.cache_key_sha256 != access_policy_cache_key_sha256(
            canonical_origin=self.canonical_origin,
            identity_sha256=self.identity_sha256,
        ):
            raise ValueError("evidence access policy cache key mismatch")
        try:
            freshness = self.policy_expires_at - self.policy_observed_at
        except (OverflowError, ValueError) as exc:
            raise ValueError("evidence policy freshness is invalid") from exc
        if not timedelta(0) <= freshness <= timedelta(hours=24):
            raise ValueError(
                "evidence policy freshness must be between zero and 24 hours"
            )

        origin_policy_refs = (
            self.origin_policy_id,
            self.origin_policy_sha256,
            self.robots_sha256,
        )
        if any(value is None for value in origin_policy_refs) and any(
            value is not None for value in origin_policy_refs
        ):
            raise ValueError(
                "evidence origin policy ID/digest/robots digest must be all null or all present"
            )
        if self.origin_policy_id is not None:
            assert self.origin_policy_sha256 is not None
            _validate_non_sensitive_text(
                self.origin_policy_id,
                location="evidence.origin_policy_id",
            )
            if self.origin_policy_id != (
                f"robots-policy-{self.origin_policy_sha256[:16]}"
            ):
                raise ValueError("evidence origin policy ID/digest mismatch")
        return self


class AccessRejectionErrorEnvelope(StrictContractModel):
    schema_version: Literal["access-rejection-error.v1"]
    outcome: Literal["reject", "error"]
    reason_code: AccessReasonCode
    message: EnvelopeMessage
    retryable: bool
    evidence: AccessDecisionEvidence | None

    _clean_message = field_validator("message")(_clean_string)

    @model_validator(mode="after")
    def exact_outcome(self) -> "AccessRejectionErrorEnvelope":
        if self.reason_code in _REJECT_REASONS:
            expected_outcome, expected_retryable = "reject", False
        elif self.reason_code in _ERROR_RETRYABILITY:
            expected_outcome = "error"
            expected_retryable = _ERROR_RETRYABILITY[self.reason_code]
        else:
            raise ValueError(
                "allow reason codes cannot appear in a rejection/error envelope"
            )
        if (self.outcome, self.retryable) != (expected_outcome, expected_retryable):
            raise ValueError(
                "rejection/error envelope outcome or retryability mismatch"
            )
        expected_message = (
            "access contract validation failed"
            if self.reason_code == "contract.invalid"
            else "access rejected by governed robots policy"
            if self.outcome == "reject"
            else "access failed closed while resolving robots policy"
        )
        if self.message != expected_message:
            raise ValueError("rejection/error envelope message mismatch")
        evidence_required = self.reason_code != "contract.invalid"
        if evidence_required != (self.evidence is not None):
            raise ValueError(
                "rejection/error envelope evidence has invalid nullability"
            )
        if self.evidence is None:
            return self
        expected_observation_kind = {
            "robots.disallowed": "valid_200",
            "robots.auth_required": "http_401",
            "robots.forbidden": "http_403",
            "robots.timeout": "timeout",
            "robots.dns_error": "dns_error",
            "robots.network_error": "network_error",
            "robots.parse_error": "parse_error",
        }[self.reason_code]
        if self.evidence.robots_observation.kind != expected_observation_kind:
            raise ValueError(
                "rejection/error envelope reason conflicts with robots observation"
            )
        origin_policy_required = self.reason_code == "robots.disallowed"
        if origin_policy_required != (self.evidence.origin_policy_id is not None):
            raise ValueError(
                "rejection/error envelope reason has invalid origin policy evidence shape"
            )
        return self


class RequestSlotReservation(StrictContractModel):
    status: Literal["reserved"]
    request_slot_ordinal: int = Field(ge=1, le=MAX_RESERVATION_ORDINAL)
    reserved_at: datetime

    _validate_time = field_validator("reserved_at")(_portable_reservation_timestamp)


class OriginPacingBudgetReservation(StrictContractModel):
    status: Literal["reserved"]
    origin: NormalizedOrigin
    reserved_at: datetime
    not_before: datetime
    pacing_interval_ms: int = Field(ge=0, le=MAX_PACING_INTERVAL_MS)
    budget_window_started_at: datetime
    budget_window_seconds: int = Field(ge=1, le=MAX_BUDGET_WINDOW_SECONDS)
    budget_limit: int = Field(ge=1, le=MAX_ORIGIN_BUDGET_LIMIT)
    budget_used_before_reservation: int = Field(
        ge=0,
        le=MAX_ORIGIN_BUDGET_LIMIT,
    )
    budget_units_reserved: Literal[1]
    budget_slot_ordinal: int = Field(ge=1, le=MAX_RESERVATION_ORDINAL)

    _validate_times = field_validator(
        "reserved_at", "not_before", "budget_window_started_at"
    )(_portable_reservation_timestamp)

    @model_validator(mode="after")
    def valid_reservation(self) -> "OriginPacingBudgetReservation":
        window_end = _reservation_window_end(self)
        if self.not_before < self.reserved_at:
            raise ValueError("pacing not_before precedes reservation time")
        if not self.budget_window_started_at <= self.reserved_at < window_end:
            raise ValueError("reservation is outside the active budget window")
        if not self.budget_window_started_at <= self.not_before < window_end:
            raise ValueError("pacing not_before is outside the active budget window")
        if (
            self.budget_used_before_reservation + self.budget_units_reserved
            > self.budget_limit
        ):
            raise ValueError("origin budget reservation exceeds the declared limit")
        if self.budget_slot_ordinal != self.budget_used_before_reservation + 1:
            raise ValueError("origin budget slot ordinal does not match prior usage")
        return self


def _reservation_window_end(
    reservation: OriginPacingBudgetReservation,
) -> datetime:
    try:
        return reservation.budget_window_started_at + timedelta(
            seconds=reservation.budget_window_seconds
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            "budget-window time arithmetic is outside the portable range"
        ) from exc


def _matched_rule_lines(rules: list[RobotsPolicyRule], url: str) -> list[int]:
    matches = [rule for rule in rules if robots_rule_matches(rule.pattern, url)]
    if not matches:
        return []
    longest = max(robots_rule_specificity(rule.pattern) for rule in matches)
    return [
        rule.line_number
        for rule in matches
        if robots_rule_specificity(rule.pattern) == longest
    ]


def _decision_disposition(
    policy: AccessPolicy, canonical_url: str
) -> tuple[AccessOutcome, AccessReasonCode, bool, RuleSource, list[int]]:
    kind = policy.robots_observation.kind
    if kind == "valid_200":
        evidence = policy.origin_policy_evidence
        assert evidence is not None
        allowed = robots_rules_allow(evidence.selected_rules, canonical_url)
        return (
            "allow" if allowed else "reject",
            "robots.allowed" if allowed else "robots.disallowed",
            False,
            "origin_policy_evidence",
            _matched_rule_lines(evidence.selected_rules, canonical_url),
        )
    matrix: dict[
        str, tuple[AccessOutcome, AccessReasonCode, bool, RuleSource, list[int]]
    ] = {
        "http_404": ("allow", "robots.absent", False, "robots_absent", []),
        "http_401": ("reject", "robots.auth_required", False, "http_status", []),
        "http_403": ("reject", "robots.forbidden", False, "http_status", []),
        "timeout": ("error", "robots.timeout", True, "transport", []),
        "dns_error": ("error", "robots.dns_error", True, "transport", []),
        "network_error": ("error", "robots.network_error", True, "transport", []),
        "parse_error": ("error", "robots.parse_error", False, "parser", []),
    }
    return matrix[kind]


def _decision_evidence(policy: AccessPolicy) -> AccessDecisionEvidence:
    origin_policy = policy.origin_policy_evidence
    return AccessDecisionEvidence(
        diagnostic_artifact_sha256=policy.diagnostic_artifact_sha256,
        access_policy_id=policy.policy_id,
        access_policy_sha256=policy.policy_sha256,
        cache_key_sha256=policy.cache_key_sha256,
        identity_sha256=policy.identity.identity_sha256,
        canonical_origin=policy.canonical_origin,
        robots_observation=policy.robots_observation,
        policy_observed_at=policy.observed_at,
        policy_expires_at=policy.expires_at,
        origin_policy_id=origin_policy.policy_id if origin_policy else None,
        origin_policy_sha256=origin_policy.policy_sha256 if origin_policy else None,
        robots_sha256=origin_policy.robots_sha256 if origin_policy else None,
    )


def _validate_request_decision_fields(
    *,
    policy: AccessPolicy,
    canonical_url: str,
    canonical_origin: NormalizedOrigin,
    decision_time: datetime,
    outcome: AccessOutcome,
    reason_code: AccessReasonCode,
    retryable: bool,
    rule_source: RuleSource,
    matched_rule_line_numbers: list[int],
    evidence: AccessDecisionEvidence,
    request_slot_reservation: RequestSlotReservation | None,
    origin_reservation: OriginPacingBudgetReservation | None,
) -> None:
    if canonical_origin != _origin_for_url(canonical_url):
        raise ValueError("canonical origin does not match canonical URL")
    if policy.canonical_origin != canonical_origin:
        raise ValueError("decision origin does not match access policy")
    if not policy.observed_at <= decision_time <= policy.expires_at:
        raise ValueError("access policy evidence is not fresh at decision time")

    expected = _decision_disposition(policy, canonical_url)
    actual = (
        outcome,
        reason_code,
        retryable,
        rule_source,
        matched_rule_line_numbers,
    )
    if actual != expected:
        raise ValueError("robots observation and rules conflict with decision outcome")
    if evidence != _decision_evidence(policy):
        raise ValueError("decision evidence does not match the bound access policy")

    allowed = outcome == "allow"
    reservations_present = (
        request_slot_reservation is not None and origin_reservation is not None
    )
    if allowed != reservations_present or (
        (request_slot_reservation is None) != (origin_reservation is None)
    ):
        raise ValueError(
            "allow requires both reservations; reject/error require null reservations"
        )
    if allowed:
        assert request_slot_reservation is not None
        assert origin_reservation is not None
        if (
            request_slot_reservation.reserved_at != decision_time
            or origin_reservation.reserved_at != decision_time
            or origin_reservation.origin != canonical_origin
        ):
            raise ValueError("allow reservation does not match decision time or origin")
        if origin_reservation.not_before > policy.expires_at:
            raise ValueError("allow reservation outlives access policy authority")


class _RedirectAccessProofPayload(StrictContractModel):
    schema_version: Literal["access-decision-proof.v1"]
    policy: AccessPolicy
    canonical_url: NonEmptyString
    canonical_origin: NormalizedOrigin
    decision_time: datetime
    outcome: Literal["allow"]
    reason_code: Literal["robots.allowed", "robots.absent"]
    retryable: Literal[False]
    rule_source: Literal["origin_policy_evidence", "robots_absent"]
    matched_rule_line_numbers: list[int]
    evidence: AccessDecisionEvidence
    request_slot_reservation: RequestSlotReservation
    origin_reservation: OriginPacingBudgetReservation

    _validate_url = field_validator("canonical_url")(_canonical_url)
    _validate_time = field_validator("decision_time")(require_aware_timestamp)

    @model_validator(mode="after")
    def exact_allow_decision(self) -> "_RedirectAccessProofPayload":
        _validate_request_decision_fields(
            policy=self.policy,
            canonical_url=self.canonical_url,
            canonical_origin=self.canonical_origin,
            decision_time=self.decision_time,
            outcome=self.outcome,
            reason_code=self.reason_code,
            retryable=self.retryable,
            rule_source=self.rule_source,
            matched_rule_line_numbers=self.matched_rule_line_numbers,
            evidence=self.evidence,
            request_slot_reservation=self.request_slot_reservation,
            origin_reservation=self.origin_reservation,
        )
        return self


class RedirectAccessProof(_RedirectAccessProofPayload):
    decision_id: NonEmptyString
    decision_sha256: Sha256

    _clean_decision_id = field_validator("decision_id")(_clean_string)

    def expected_decision_sha256(self) -> str:
        return canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"decision_id", "decision_sha256"},
            )
        )

    @model_validator(mode="after")
    def valid_decision_identity(self) -> "RedirectAccessProof":
        expected = self.expected_decision_sha256()
        if (
            self.decision_sha256 != expected
            or self.decision_id != f"access-decision-{expected[:16]}"
        ):
            raise ValueError("redirect access proof decision ID/digest mismatch")
        return self


def build_redirect_access_proof(
    *,
    policy: AccessPolicy,
    canonical_url: str,
    decision_time: datetime,
    request_slot_reservation: RequestSlotReservation,
    origin_reservation: OriginPacingBudgetReservation,
) -> RedirectAccessProof:
    """Build the finite allow-decision proof for one consumed redirect request."""
    canonical_url = _canonical_url(canonical_url)
    outcome, reason_code, retryable, rule_source, matched_lines = _decision_disposition(
        policy, canonical_url
    )
    if outcome != "allow":
        raise ValueError("redirect source access proof must have an allow disposition")
    payload = _RedirectAccessProofPayload(
        schema_version="access-decision-proof.v1",
        policy=policy,
        canonical_url=canonical_url,
        canonical_origin=_origin_for_url(canonical_url),
        decision_time=decision_time,
        outcome=outcome,
        reason_code=reason_code,
        retryable=retryable,
        rule_source=rule_source,
        matched_rule_line_numbers=matched_lines,
        evidence=_decision_evidence(policy),
        request_slot_reservation=request_slot_reservation,
        origin_reservation=origin_reservation,
    )
    digest = canonical_sha256(payload.model_dump(mode="json"))
    return RedirectAccessProof(
        **payload.model_dump(mode="python"),
        decision_id=f"access-decision-{digest[:16]}",
        decision_sha256=digest,
    )


class RedirectHop(StrictContractModel):
    hop_ordinal: int = Field(ge=1, le=MAX_RESERVATION_ORDINAL)
    request_slot_ordinal: int = Field(ge=1, le=MAX_RESERVATION_ORDINAL)
    source_url: NonEmptyString
    source_origin: NormalizedOrigin
    access_proof: RedirectAccessProof
    request_started_at: datetime
    http_status: Literal[301, 302, 303, 307, 308]
    canonical_target_url: NonEmptyString
    target_origin: NormalizedOrigin
    observed_at: datetime

    _validate_urls = field_validator("source_url", "canonical_target_url")(
        _canonical_url
    )
    _validate_times = field_validator("request_started_at", "observed_at")(
        require_aware_timestamp
    )

    @model_validator(mode="after")
    def exact_request_and_transition(self) -> "RedirectHop":
        if (
            self.source_url != self.access_proof.canonical_url
            or self.source_origin != self.access_proof.canonical_origin
        ):
            raise ValueError("redirect source URL/origin does not match access proof")
        if (
            self.request_slot_ordinal
            != self.access_proof.request_slot_reservation.request_slot_ordinal
        ):
            raise ValueError("redirect request slot does not match access proof")
        if not (
            self.access_proof.decision_time < self.request_started_at < self.observed_at
        ):
            raise ValueError(
                "redirect access decision must precede request and response"
            )
        if self.access_proof.origin_reservation.not_before > self.request_started_at:
            raise ValueError("redirect request precedes its pacing reservation")
        reservation = self.access_proof.origin_reservation
        if not (
            reservation.budget_window_started_at
            <= self.request_started_at
            < _reservation_window_end(reservation)
        ):
            raise ValueError("redirect request is outside its active budget window")
        if self.request_started_at > self.access_proof.policy.expires_at:
            raise ValueError("redirect request starts after access policy expiry")
        if self.target_origin != _origin_for_url(self.canonical_target_url):
            raise ValueError("redirect target origin does not match target URL")
        if self.source_origin.scheme == "https" and self.target_origin.scheme == "http":
            raise ValueError("redirect chain cannot downgrade HTTPS")
        return self


def _reservation_origin_key(
    reservation: OriginPacingBudgetReservation,
) -> tuple[str, str, int]:
    origin = reservation.origin
    return (origin.scheme, origin.host, origin.effective_port)


def _validate_origin_pacing_lineage(
    *,
    previous: OriginPacingBudgetReservation,
    current: OriginPacingBudgetReservation,
    previous_request_started_at: datetime,
) -> None:
    if current.pacing_interval_ms != previous.pacing_interval_ms:
        raise ValueError("same-origin pacing contract changed")
    try:
        pacing_floor = previous_request_started_at + timedelta(
            milliseconds=previous.pacing_interval_ms
        )
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            "origin pacing time arithmetic is outside the portable range"
        ) from exc
    if current.not_before < pacing_floor:
        raise ValueError("same-origin pacing schedule contradicts prior request")


def _validate_budget_window_lineage(
    *,
    previous: OriginPacingBudgetReservation,
    current: OriginPacingBudgetReservation,
) -> None:
    same_window = (
        current.budget_window_started_at == previous.budget_window_started_at
        and current.budget_window_seconds == previous.budget_window_seconds
    )
    if not same_window:
        windows_overlap = previous.budget_window_started_at < _reservation_window_end(
            current
        ) and current.budget_window_started_at < _reservation_window_end(previous)
        if windows_overlap:
            raise ValueError("same-origin budget windows overlap with different shapes")
        return
    if current.budget_limit != previous.budget_limit:
        raise ValueError("same-window origin budget contract changed")
    if (
        current.budget_used_before_reservation
        < previous.budget_used_before_reservation + previous.budget_units_reserved
    ):
        raise ValueError("same-origin budget reservation lineage rolled back")


class _AccessDecisionPayload(StrictContractModel):
    schema_version: Literal["access-decision.v1"]
    policy: AccessPolicy
    canonical_url: NonEmptyString
    canonical_origin: NormalizedOrigin
    decision_time: datetime
    outcome: AccessOutcome
    reason_code: AccessReasonCode
    retryable: bool
    rule_source: RuleSource
    matched_rule_line_numbers: list[int]
    evidence: AccessDecisionEvidence
    redirect_hops: list[RedirectHop]
    request_slot_reservation: RequestSlotReservation | None
    origin_reservation: OriginPacingBudgetReservation | None
    rejection_or_error: AccessRejectionErrorEnvelope | None

    _validate_url = field_validator("canonical_url")(_canonical_url)
    _validate_time = field_validator("decision_time")(require_aware_timestamp)

    @model_validator(mode="after")
    def exact_decision_semantics(self) -> "_AccessDecisionPayload":
        _validate_request_decision_fields(
            policy=self.policy,
            canonical_url=self.canonical_url,
            canonical_origin=self.canonical_origin,
            decision_time=self.decision_time,
            outcome=self.outcome,
            reason_code=self.reason_code,
            retryable=self.retryable,
            rule_source=self.rule_source,
            matched_rule_line_numbers=self.matched_rule_line_numbers,
            evidence=self.evidence,
            request_slot_reservation=self.request_slot_reservation,
            origin_reservation=self.origin_reservation,
        )
        expected_evidence = _decision_evidence(self.policy)

        hop_count = len(self.redirect_hops)
        if [hop.hop_ordinal for hop in self.redirect_hops] != list(
            range(1, hop_count + 1)
        ):
            raise ValueError("redirect hop ordinals must be contiguous")
        if [hop.request_slot_ordinal for hop in self.redirect_hops] != list(
            range(1, hop_count + 1)
        ):
            raise ValueError("redirect request-slot ordinals must be contiguous")
        latest_by_origin: dict[
            tuple[str, str, int],
            tuple[OriginPacingBudgetReservation, datetime],
        ] = {}
        latest_budget_by_origin: dict[
            tuple[str, str, int],
            OriginPacingBudgetReservation,
        ] = {}
        for previous, current in zip(self.redirect_hops, self.redirect_hops[1:]):
            if previous.canonical_target_url != current.source_url:
                raise ValueError("redirect hop chain is not contiguous")
            if current.access_proof.decision_time <= previous.observed_at:
                raise ValueError(
                    "next redirect access decision must follow prior observation"
                )
            if current.observed_at <= previous.observed_at:
                raise ValueError("redirect observation times must strictly increase")
        for hop in self.redirect_hops:
            if hop.access_proof.policy.identity != self.policy.identity:
                raise ValueError("redirect access proof identity changed within chain")
            reservation = hop.access_proof.origin_reservation
            origin_key = _reservation_origin_key(reservation)
            prior = latest_by_origin.get(origin_key)
            if prior is not None:
                _validate_origin_pacing_lineage(
                    previous=prior[0],
                    current=reservation,
                    previous_request_started_at=prior[1],
                )
            prior_window = latest_budget_by_origin.get(origin_key)
            if prior_window is not None:
                _validate_budget_window_lineage(
                    previous=prior_window,
                    current=reservation,
                )
            latest_budget_by_origin[origin_key] = reservation
            latest_by_origin[origin_key] = (reservation, hop.request_started_at)
        if (
            self.redirect_hops
            and self.redirect_hops[-1].canonical_target_url != self.canonical_url
        ):
            raise ValueError("redirect chain does not terminate at the canonical URL")
        if (
            self.redirect_hops
            and self.decision_time <= self.redirect_hops[-1].observed_at
        ):
            raise ValueError(
                "final access decision must follow the last redirect observation"
            )

        allowed = self.outcome == "allow"
        if allowed:
            assert self.request_slot_reservation is not None
            assert self.origin_reservation is not None
            if self.request_slot_reservation.request_slot_ordinal != hop_count + 1:
                raise ValueError("allow reservation does not match final request slot")
            origin_key = _reservation_origin_key(self.origin_reservation)
            prior = latest_by_origin.get(origin_key)
            if prior is not None:
                _validate_origin_pacing_lineage(
                    previous=prior[0],
                    current=self.origin_reservation,
                    previous_request_started_at=prior[1],
                )
            prior_window = latest_budget_by_origin.get(origin_key)
            if prior_window is not None:
                _validate_budget_window_lineage(
                    previous=prior_window,
                    current=self.origin_reservation,
                )

        if allowed != (self.rejection_or_error is None):
            raise ValueError(
                "rejection/error envelope has invalid required/nullability shape"
            )
        if not allowed:
            assert self.rejection_or_error is not None
            expected_message = (
                "access rejected by governed robots policy"
                if self.outcome == "reject"
                else "access failed closed while resolving robots policy"
            )
            expected_envelope = AccessRejectionErrorEnvelope(
                schema_version="access-rejection-error.v1",
                outcome=self.outcome,
                reason_code=self.reason_code,
                message=expected_message,
                retryable=self.retryable,
                evidence=expected_evidence,
            )
            if self.rejection_or_error != expected_envelope:
                raise ValueError("rejection/error envelope does not match the decision")
        return self


class AccessDecision(_AccessDecisionPayload):
    decision_id: NonEmptyString
    decision_sha256: Sha256

    _clean_decision_id = field_validator("decision_id")(_clean_string)

    def expected_decision_sha256(self) -> str:
        return canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"decision_id", "decision_sha256"},
            )
        )

    @model_validator(mode="after")
    def valid_decision_identity(self) -> "AccessDecision":
        expected = self.expected_decision_sha256()
        if (
            self.decision_sha256 != expected
            or self.decision_id != f"access-decision-{expected[:16]}"
        ):
            raise ValueError("access decision ID/digest mismatch")
        return self


def build_access_decision(
    *,
    policy: AccessPolicy,
    canonical_url: str,
    decision_time: datetime,
    redirect_hops: list[RedirectHop],
    request_slot_reservation: RequestSlotReservation | None,
    origin_reservation: OriginPacingBudgetReservation | None,
) -> AccessDecision:
    """Build one deterministic access decision without performing any I/O."""
    canonical_url = _canonical_url(canonical_url)
    outcome, reason_code, retryable, rule_source, matched_lines = _decision_disposition(
        policy, canonical_url
    )
    evidence = _decision_evidence(policy)
    rejection_or_error = None
    if outcome != "allow":
        rejection_or_error = AccessRejectionErrorEnvelope(
            schema_version="access-rejection-error.v1",
            outcome=outcome,
            reason_code=reason_code,
            message=(
                "access rejected by governed robots policy"
                if outcome == "reject"
                else "access failed closed while resolving robots policy"
            ),
            retryable=retryable,
            evidence=evidence,
        )
    payload = _AccessDecisionPayload(
        schema_version=ACCESS_DECISION_VERSION,
        policy=policy,
        canonical_url=canonical_url,
        canonical_origin=_origin_for_url(canonical_url),
        decision_time=decision_time,
        outcome=outcome,
        reason_code=reason_code,
        retryable=retryable,
        rule_source=rule_source,
        matched_rule_line_numbers=matched_lines,
        evidence=evidence,
        redirect_hops=redirect_hops,
        request_slot_reservation=request_slot_reservation,
        origin_reservation=origin_reservation,
        rejection_or_error=rejection_or_error,
    )
    digest = canonical_sha256(payload.model_dump(mode="json"))
    return AccessDecision(
        **payload.model_dump(mode="python"),
        decision_id=f"access-decision-{digest[:16]}",
        decision_sha256=digest,
    )


__all__ = [
    "ACCESS_DECISION_VERSION",
    "ACCESS_POLICY_VERSION",
    "AccessDecision",
    "AccessDecisionEvidence",
    "AccessPolicy",
    "AccessRejectionErrorEnvelope",
    "OriginPacingBudgetReservation",
    "RedirectAccessProof",
    "RedirectHop",
    "RequestSlotReservation",
    "RobotsObservation",
    "access_policy_cache_key_sha256",
    "build_access_decision",
    "build_access_policy",
    "build_redirect_access_proof",
    "canonical_json",
    "canonical_sha256",
]
