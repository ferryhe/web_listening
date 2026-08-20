from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import pytest
from pydantic import ValidationError

from web_listening.blocks.access_contract import (
    AccessContractError,
    load_access_contract,
)
from web_listening.blocks.site_diagnostic import load_site_diagnostic
from web_listening.contracts._protocol import (
    contains_uri_userinfo,
    is_secret_like_key,
    validate_portable_json,
)
from web_listening.contracts.access_decision import (
    _validate_non_sensitive_text,
    AccessDecision,
    AccessPolicy,
    AccessRejectionErrorEnvelope,
    OriginPacingBudgetReservation,
    RedirectAccessProof,
    RedirectHop,
    RequestSlotReservation,
    RobotsObservation,
    build_access_decision,
    build_access_policy,
    build_redirect_access_proof,
)
from web_listening.contracts.site_diagnostic import (
    DiagnosticIdentity,
    OriginPolicyEvidence,
    RobotsPolicyRule,
    SitemapDirective,
    canonical_json,
    canonical_sha256,
)


FIXTURES = Path("docs/testing/fixtures")
DIAGNOSTIC = load_site_diagnostic(FIXTURES / "site-diagnostic-v1.sample.json")
ORIGIN = DIAGNOSTIC.canonical_origin
IDENTITY = DIAGNOSTIC.identity
DIAGNOSTIC_SHA256 = DIAGNOSTIC.artifact_sha256
OBSERVED_AT = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
EXPIRES_AT = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
DECISION_TIME = datetime(2026, 8, 8, 12, 5, tzinfo=timezone.utc)


def _available_evidence() -> OriginPolicyEvidence:
    rules = [RobotsPolicyRule(allow=False, pattern="/private", line_number=3)]
    robots_sha256 = canonical_sha256({"robots": "User-agent: *\nDisallow: /private\n"})
    policy_payload = {
        "origin": ORIGIN.model_dump(mode="json"),
        "robots_sha256": robots_sha256,
        "selected_rules": [rule.model_dump(mode="json") for rule in rules],
        "identity_sha256": IDENTITY.identity_sha256,
    }
    policy_sha256 = canonical_sha256(policy_payload)
    return OriginPolicyEvidence(
        origin=ORIGIN,
        policy_id=f"robots-policy-{policy_sha256[:16]}",
        policy_sha256=policy_sha256,
        robots_status="available",
        robots_sha256=robots_sha256,
        selected_rules=rules,
        declared_sitemaps=[],
        warnings=[],
        fetched_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
        identity_id=IDENTITY.identity_id,
        identity_sha256=IDENTITY.identity_sha256,
    )


def _policy(kind: str) -> AccessPolicy:
    status_by_kind = {
        "valid_200": 200,
        "http_404": 404,
        "http_401": 401,
        "http_403": 403,
        "timeout": None,
        "dns_error": None,
        "network_error": None,
        "parse_error": None,
    }
    evidence = (
        _available_evidence()
        if kind == "valid_200"
        else DIAGNOSTIC.origin_policy_evidence[0]
        if kind == "http_404"
        else None
    )
    return build_access_policy(
        canonical_origin=ORIGIN,
        identity=IDENTITY,
        diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
        robots_observation=RobotsObservation(
            kind=kind,
            http_status=status_by_kind[kind],
        ),
        origin_policy_evidence=evidence,
        observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )


def _allow_reservations(
    *,
    request_slot_ordinal: int = 1,
    reserved_at: datetime = DECISION_TIME,
    not_before: datetime | None = None,
    origin=ORIGIN,
    pacing_interval_ms: int = 1000,
    budget_window_started_at: datetime = OBSERVED_AT,
    budget_window_seconds: int = 3600,
    budget_limit: int = 20,
    budget_used_before_reservation: int = 2,
) -> tuple[RequestSlotReservation, OriginPacingBudgetReservation]:
    not_before = reserved_at if not_before is None else not_before
    request = RequestSlotReservation(
        status="reserved",
        request_slot_ordinal=request_slot_ordinal,
        reserved_at=reserved_at,
    )
    origin = OriginPacingBudgetReservation(
        status="reserved",
        origin=origin,
        reserved_at=reserved_at,
        not_before=not_before,
        pacing_interval_ms=pacing_interval_ms,
        budget_window_started_at=budget_window_started_at,
        budget_window_seconds=budget_window_seconds,
        budget_limit=budget_limit,
        budget_used_before_reservation=budget_used_before_reservation,
        budget_units_reserved=1,
        budget_slot_ordinal=budget_used_before_reservation + 1,
    )
    return request, origin


def _redirect_hop(
    *,
    policy: AccessPolicy | None = None,
    hop_ordinal: int = 1,
    source_url: str = "https://example.com/start",
    target_url: str = "https://example.com/public",
    proof_time: datetime = datetime(2026, 8, 8, 12, 1, tzinfo=timezone.utc),
    request_started_at: datetime = datetime(2026, 8, 8, 12, 1, 1, tzinfo=timezone.utc),
    observed_at: datetime = datetime(2026, 8, 8, 12, 1, 2, tzinfo=timezone.utc),
    budget_used_before_reservation: int = 1,
    pacing_interval_ms: int = 1000,
    not_before: datetime | None = None,
    budget_window_started_at: datetime = OBSERVED_AT,
    budget_window_seconds: int = 3600,
    budget_limit: int = 20,
) -> RedirectHop:
    policy = policy or _policy("http_404")
    request, origin = _allow_reservations(
        request_slot_ordinal=hop_ordinal,
        reserved_at=proof_time,
        not_before=not_before,
        pacing_interval_ms=pacing_interval_ms,
        budget_window_started_at=budget_window_started_at,
        budget_window_seconds=budget_window_seconds,
        budget_limit=budget_limit,
        budget_used_before_reservation=budget_used_before_reservation,
    )
    proof = build_redirect_access_proof(
        policy=policy,
        canonical_url=source_url,
        decision_time=proof_time,
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    return RedirectHop(
        hop_ordinal=hop_ordinal,
        request_slot_ordinal=hop_ordinal,
        source_url=source_url,
        source_origin=proof.canonical_origin,
        access_proof=proof,
        request_started_at=request_started_at,
        http_status=302,
        canonical_target_url=target_url,
        target_origin=ORIGIN,
        observed_at=observed_at,
    )


@pytest.mark.parametrize(
    ("kind", "url", "outcome", "reason_code", "retryable", "rule_source"),
    [
        (
            "valid_200",
            "https://example.com/public",
            "allow",
            "robots.allowed",
            False,
            "origin_policy_evidence",
        ),
        (
            "valid_200",
            "https://example.com/private/a",
            "reject",
            "robots.disallowed",
            False,
            "origin_policy_evidence",
        ),
        (
            "http_404",
            "https://example.com/public",
            "allow",
            "robots.absent",
            False,
            "robots_absent",
        ),
        (
            "http_401",
            "https://example.com/public",
            "reject",
            "robots.auth_required",
            False,
            "http_status",
        ),
        (
            "http_403",
            "https://example.com/public",
            "reject",
            "robots.forbidden",
            False,
            "http_status",
        ),
        (
            "timeout",
            "https://example.com/public",
            "error",
            "robots.timeout",
            True,
            "transport",
        ),
        (
            "dns_error",
            "https://example.com/public",
            "error",
            "robots.dns_error",
            True,
            "transport",
        ),
        (
            "network_error",
            "https://example.com/public",
            "error",
            "robots.network_error",
            True,
            "transport",
        ),
        (
            "parse_error",
            "https://example.com/public",
            "error",
            "robots.parse_error",
            False,
            "parser",
        ),
    ],
)
def test_robots_matrix_is_deterministic_and_fail_closed(
    kind: str,
    url: str,
    outcome: str,
    reason_code: str,
    retryable: bool,
    rule_source: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy(kind),
        canonical_url=url,
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request if outcome == "allow" else None,
        origin_reservation=origin if outcome == "allow" else None,
    )

    assert (decision.outcome, decision.reason_code, decision.retryable) == (
        outcome,
        reason_code,
        retryable,
    )
    assert decision.rule_source == rule_source
    assert (decision.request_slot_reservation is None) == (outcome != "allow")
    assert (decision.origin_reservation is None) == (outcome != "allow")
    assert (decision.rejection_or_error is None) == (outcome == "allow")
    if outcome != "allow":
        assert decision.rejection_or_error is not None
        assert decision.rejection_or_error.model_dump(mode="json") == {
            "schema_version": "access-rejection-error.v1",
            "outcome": outcome,
            "reason_code": reason_code,
            "message": "access rejected by governed robots policy"
            if outcome == "reject"
            else "access failed closed while resolving robots policy",
            "retryable": retryable,
            "evidence": decision.evidence.model_dump(mode="json"),
        }


def _resign_decision(payload: dict[str, object]) -> dict[str, object]:
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"decision_id", "decision_sha256"}
    }
    digest = canonical_sha256(digest_payload)
    payload["decision_sha256"] = digest
    payload["decision_id"] = f"access-decision-{digest[:16]}"
    return payload


def _resign_policy(payload: dict[str, object]) -> dict[str, object]:
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"policy_id", "policy_sha256"}
    }
    digest = canonical_sha256(digest_payload)
    payload["policy_sha256"] = digest
    payload["policy_id"] = f"access-policy-{digest[:16]}"
    return payload


def _resign_origin_policy_evidence(payload: dict[str, object]) -> None:
    evidence = payload["origin_policy_evidence"]
    assert isinstance(evidence, dict)
    digest = canonical_sha256(
        {
            "origin": evidence["origin"],
            "robots_sha256": evidence["robots_sha256"],
            "selected_rules": evidence["selected_rules"],
            "identity_sha256": evidence["identity_sha256"],
        }
    )
    evidence["policy_sha256"] = digest
    evidence["policy_id"] = f"robots-policy-{digest[:16]}"


def test_semantic_tampering_cannot_be_hidden_by_resigning_the_decision() -> None:
    decision = build_access_decision(
        policy=_policy("http_403"),
        canonical_url="https://example.com/private",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=None,
        origin_reservation=None,
    )
    payload = decision.model_dump(mode="json")
    payload["outcome"] = "allow"
    request, origin = _allow_reservations()
    payload["request_slot_reservation"] = request.model_dump(mode="json")
    payload["origin_reservation"] = origin.model_dump(mode="json")
    payload["rejection_or_error"] = None
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="observation|outcome|fail closed"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "mutation",
    ["identity", "cache_key", "origin_policy", "policy_digest", "decision_digest"],
)
def test_identity_policy_and_digest_tampering_fails_closed(mutation: str) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("valid_200"),
        canonical_url="https://example.com/public",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    if mutation == "identity":
        payload["policy"]["identity"]["user_agent"] = "different-bot/1.0"
        _resign_policy(payload["policy"])
        _resign_decision(payload)
    elif mutation == "cache_key":
        payload["policy"]["cache_key_sha256"] = "0" * 64
        _resign_policy(payload["policy"])
        _resign_decision(payload)
    elif mutation == "origin_policy":
        payload["policy"]["origin_policy_evidence"]["policy_sha256"] = "0" * 64
        _resign_policy(payload["policy"])
        _resign_decision(payload)
    elif mutation == "policy_digest":
        payload["policy"]["policy_sha256"] = "0" * 64
        _resign_decision(payload)
    else:
        payload["decision_sha256"] = "0" * 64

    with pytest.raises(ValidationError):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_redirect_chain_and_reservation_nullability_are_strict() -> None:
    policy = _policy("http_404")
    hop = _redirect_hop(policy=policy)
    request, origin = _allow_reservations(request_slot_ordinal=2)
    decision = build_access_decision(
        policy=policy,
        canonical_url="https://example.com/public",
        decision_time=DECISION_TIME,
        redirect_hops=[hop],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert decision.redirect_hops == [hop]

    payload = decision.model_dump(mode="json")
    payload["request_slot_reservation"] = None
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="reservation"):
        AccessDecision.model_validate(payload)

    payload = decision.model_dump(mode="json")
    payload["redirect_hops"][0]["canonical_target_url"] = "https://example.com/other"
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="redirect"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_redirect_requires_digest_bound_allow_proof_for_every_consumed_request() -> (
    None
):
    policy = _policy("http_404")
    hop = _redirect_hop(policy=policy)
    request, origin = _allow_reservations(request_slot_ordinal=2)
    decision = build_access_decision(
        policy=policy,
        canonical_url="https://example.com/public",
        decision_time=DECISION_TIME,
        redirect_hops=[hop],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert isinstance(decision.redirect_hops[0].access_proof, RedirectAccessProof)
    assert decision.redirect_hops[0].access_proof.outcome == "allow"

    payload = decision.model_dump(mode="json")
    del payload["redirect_hops"][0]["access_proof"]
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="access_proof|proof|Field required"):
        AccessDecision.model_validate_json(canonical_json(payload))

    payload = decision.model_dump(mode="json")
    payload["redirect_hops"][0]["source_url"] = "https://other.example/start"
    payload["redirect_hops"][0]["source_origin"] = {
        "scheme": "https",
        "host": "other.example",
        "effective_port": 443,
    }
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="proof|source"):
        AccessDecision.model_validate_json(canonical_json(payload))

    payload = decision.model_dump(mode="json")
    payload["redirect_hops"][0]["access_proof"]["decision_sha256"] = "0" * 64
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="digest|ID"):
        AccessDecision.model_validate_json(canonical_json(payload))

    payload = decision.model_dump(mode="json")
    proof_payload = payload["redirect_hops"][0]["access_proof"]
    proof_payload["request_slot_reservation"]["request_slot_ordinal"] = 2
    _resign_decision(proof_payload)
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="request slot|request-slot|proof"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_redirect_timestamps_are_strictly_causal() -> None:
    policy = _policy("http_404")
    request, origin = _allow_reservations(request_slot_ordinal=2)

    with pytest.raises(ValidationError, match="request|decision|response"):
        _redirect_hop(
            policy=policy,
            request_started_at=datetime(2026, 8, 8, 12, 1, tzinfo=timezone.utc),
        )

    hop = _redirect_hop(policy=policy, observed_at=DECISION_TIME)
    with pytest.raises(ValidationError, match="after|redirect|decision"):
        build_access_decision(
            policy=policy,
            canonical_url="https://example.com/public",
            decision_time=DECISION_TIME,
            redirect_hops=[hop],
            request_slot_reservation=request,
            origin_reservation=origin,
        )

    first = _redirect_hop(
        policy=policy,
        source_url="https://example.com/start",
        target_url="https://example.com/second",
        budget_used_before_reservation=0,
    )
    second = _redirect_hop(
        policy=policy,
        hop_ordinal=2,
        source_url="https://example.com/second",
        target_url="https://example.com/public",
        proof_time=first.observed_at,
        request_started_at=first.observed_at + timedelta(seconds=1),
        observed_at=first.observed_at + timedelta(seconds=2),
        budget_used_before_reservation=1,
    )
    request, origin = _allow_reservations(
        request_slot_ordinal=3,
        budget_used_before_reservation=2,
    )
    with pytest.raises(ValidationError, match="next redirect|prior observation"):
        build_access_decision(
            policy=policy,
            canonical_url="https://example.com/public",
            decision_time=DECISION_TIME,
            redirect_hops=[first, second],
            request_slot_reservation=request,
            origin_reservation=origin,
        )


def test_origin_reservation_uses_a_half_open_active_window() -> None:
    with pytest.raises(ValidationError, match="active|window"):
        _allow_reservations(
            reserved_at=DECISION_TIME,
            budget_window_seconds=1,
        )
    with pytest.raises(ValidationError, match="active|window"):
        _allow_reservations(
            reserved_at=OBSERVED_AT,
            not_before=OBSERVED_AT + timedelta(seconds=1),
            budget_window_seconds=1,
        )


def test_same_origin_redirect_budget_lineage_cannot_roll_back() -> None:
    policy = _policy("http_404")
    first = _redirect_hop(
        policy=policy,
        source_url="https://example.com/start",
        target_url="https://example.com/second",
        budget_used_before_reservation=0,
        pacing_interval_ms=5000,
    )
    second = _redirect_hop(
        policy=policy,
        hop_ordinal=2,
        source_url="https://example.com/second",
        target_url="https://example.com/final",
        proof_time=datetime(2026, 8, 8, 12, 2, tzinfo=timezone.utc),
        request_started_at=datetime(2026, 8, 8, 12, 2, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 8, 8, 12, 2, 2, tzinfo=timezone.utc),
        budget_used_before_reservation=0,
        pacing_interval_ms=5000,
    )
    request, origin = _allow_reservations(
        request_slot_ordinal=3,
        budget_used_before_reservation=2,
    )
    with pytest.raises(ValidationError, match="budget|lineage"):
        build_access_decision(
            policy=policy,
            canonical_url="https://example.com/final",
            decision_time=DECISION_TIME,
            redirect_hops=[first, second],
            request_slot_reservation=request,
            origin_reservation=origin,
        )


def test_same_origin_redirect_pacing_schedule_cannot_contradict_prior_request() -> None:
    policy = _policy("http_404")
    first = _redirect_hop(
        policy=policy,
        source_url="https://example.com/start",
        target_url="https://example.com/second",
        budget_used_before_reservation=0,
        pacing_interval_ms=5000,
    )
    second_time = datetime(2026, 8, 8, 12, 1, 3, tzinfo=timezone.utc)
    second = _redirect_hop(
        policy=policy,
        hop_ordinal=2,
        source_url="https://example.com/second",
        target_url="https://example.com/final",
        proof_time=second_time,
        request_started_at=second_time + timedelta(milliseconds=100),
        observed_at=second_time + timedelta(milliseconds=200),
        not_before=second_time,
        budget_used_before_reservation=1,
        pacing_interval_ms=5000,
    )
    request, origin = _allow_reservations(
        request_slot_ordinal=3,
        budget_used_before_reservation=2,
    )
    with pytest.raises(ValidationError, match="pacing|schedule"):
        build_access_decision(
            policy=policy,
            canonical_url="https://example.com/final",
            decision_time=DECISION_TIME,
            redirect_hops=[first, second],
            request_slot_reservation=request,
            origin_reservation=origin,
        )


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://example.com/?safe=1;apiKey=secret",
        "https://example.com/?safe=1%3BapiKey%3Dsecret",
        "https://example.com/?safe=1%253BapiKey%253Dsecret",
    ],
)
def test_resigned_semicolon_sensitive_urls_fail_closed(canonical_url: str) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?safe=1",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = canonical_url
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://example.com/?safe=1＆apiKey=secretvalue",
        "https://example.com/?safe=1%EF%BC%86apiKey%EF%BC%9Dsecretvalue",
        "https://example.com/?ａｐｉｋｅｙ＝secretvalue",
    ],
)
def test_resigned_nfkc_query_delimiters_fail_closed(canonical_url: str) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?safe=1",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = canonical_url
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "sensitive_text"),
    [
        ("identity", "web-listening-bot/1.1 apiKey secretvalue"),
        ("warning", "proxyCredentials secretvalue"),
    ],
)
def test_resigned_whitespace_sensitive_free_text_fails_closed(
    location: str,
    sensitive_text: str,
) -> None:
    policy = _policy("timeout" if location == "identity" else "http_404")
    payload = policy.model_dump(mode="json")
    if location == "identity":
        payload["identity"]["user_agent"] = sensitive_text
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    else:
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "sensitive_text"),
    [
        ("identity", "web-listening-bot/1.1 prefix:apiKey=secretvalue"),
        ("warning", "note=proxyCredentials secretvalue"),
        ("rule", "prefix=apiKey=secretvalue"),
    ],
)
def test_resigned_nested_boundary_sensitive_free_text_fails_closed(
    location: str,
    sensitive_text: str,
) -> None:
    policy_kind = "timeout" if location == "identity" else "valid_200"
    policy = _policy(policy_kind)
    payload = policy.model_dump(mode="json")
    if location == "identity":
        payload["identity"]["user_agent"] = sensitive_text
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    elif location == "warning":
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    else:
        payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
            sensitive_text
        )
        _resign_origin_policy_evidence(payload)
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "sensitive_text"),
    [
        (
            "identity",
            "web-listening-bot/1.1 Authorization%3A%20Bearer%20secretvalue",
        ),
        ("warning", "proxyCredentials%253Dsecretvalue"),
        ("rule", f"{'x' * 96}ApiKey=secretvalue"),
    ],
)
def test_resigned_encoded_and_long_sensitive_free_text_fails_closed(
    location: str,
    sensitive_text: str,
) -> None:
    policy = _policy("timeout" if location == "identity" else "valid_200")
    payload = policy.model_dump(mode="json")
    if location == "identity":
        payload["identity"]["user_agent"] = sensitive_text
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    elif location == "warning":
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    else:
        payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
            sensitive_text
        )
        _resign_origin_policy_evidence(payload)
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


def test_resigned_decision_rejects_encoded_sensitive_embedded_policy() -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/public",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    policy_payload = payload["policy"]
    policy_payload["origin_policy_evidence"]["warnings"] = [
        "Authorization%253A%2520Bearer%2520secretvalue"
    ]
    _resign_policy(policy_payload)
    payload["evidence"]["access_policy_id"] = policy_payload["policy_id"]
    payload["evidence"]["access_policy_sha256"] = policy_payload["policy_sha256"]
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "value",
    [
        "status%3Dpublic",
        "proxyMode%253Dpublic",
        f"{'x' * 96}Metadata%253Dpublic",
        "AuthenticationGuide%3Dpublic",
    ],
)
def test_encoded_and_long_benign_free_text_remains_valid(value: str) -> None:
    assert _validate_non_sensitive_text(value, location="test") == value


def test_free_text_secret_scan_scales_linearly_for_large_fields() -> None:
    durations: list[float] = []
    for size in (100_000, 200_000, 400_000):
        value = f"{'x' * size}Metadata%253Dpublic"
        started_at = perf_counter()
        assert _validate_non_sensitive_text(value, location="performance") == value
        durations.append(perf_counter() - started_at)

    assert durations[1] <= durations[0] * 4 + 0.05
    assert durations[2] <= durations[1] * 4 + 0.05
    assert max(durations) < 3


@pytest.mark.parametrize(
    "secret_name",
    [
        "accesskey",
        "apikey",
        "awsaccesskeyid",
        "clientapikey",
        "privatekey",
        "proxyauth",
        "proxycredential",
        "proxycredentials",
        "proxypassword",
        "proxyuser",
        "proxyusername",
        "xapikey",
    ],
)
def test_every_compact_secret_name_is_detected_when_namespaced(
    secret_name: str,
) -> None:
    assert is_secret_like_key(f"{'tenant' * 16}{secret_name}")


@pytest.mark.parametrize(
    "canonical_url",
    [
        f"https://example.com/?{'x' * 96}privatekey=secretvalue",
        f"https://example.com/?safe=1;{'x' * 96}proxyauth%3Dsecretvalue",
        (f"https://example.com/?safe=1&{'x' * 96}proxyusername%253Dsecretvalue"),
    ],
)
def test_resigned_namespaced_secret_query_keys_fail_closed(
    canonical_url: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?safe=1",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = canonical_url
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "sensitive_text"),
    [
        ("identity", f"web-listening-bot/1.1 {'x' * 96}privatekey=secret"),
        ("warning", f"{'x' * 96}proxyuser%3Dsecret"),
        ("rule", f"{'x' * 96}proxyauth%253Dsecret"),
    ],
)
def test_resigned_namespaced_secret_policy_text_fails_closed(
    location: str,
    sensitive_text: str,
) -> None:
    policy = _policy("timeout" if location == "identity" else "valid_200")
    payload = policy.model_dump(mode="json")
    if location == "identity":
        payload["identity"]["user_agent"] = sensitive_text
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    elif location == "warning":
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    else:
        payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
            sensitive_text
        )
        _resign_origin_policy_evidence(payload)
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


def test_long_namespaced_benign_keys_remain_valid() -> None:
    benign_key = f"{'x' * 120}publicmetadata"
    assert not is_secret_like_key(benign_key)
    value = f"{benign_key}%253Dpublic"
    assert _validate_non_sensitive_text(value, location="test") == value

    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url=f"https://example.com/?safe=1&{value}",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert decision.outcome == "allow"


def test_nested_boundaries_preserve_noncredential_free_text() -> None:
    identity_payload = _policy("timeout").model_dump(mode="json")
    identity_payload["identity"]["user_agent"] = (
        "web-listening-bot/1.1 prefix:mode=public"
    )
    visible_identity = {
        key: value
        for key, value in identity_payload["identity"].items()
        if key != "identity_sha256"
    }
    identity_sha256 = canonical_sha256(visible_identity)
    identity_payload["identity"]["identity_sha256"] = identity_sha256
    identity_payload["cache_key_sha256"] = canonical_sha256(
        {
            "canonical_origin": ORIGIN.as_url_origin(),
            "identity_sha256": identity_sha256,
            "policy_version": "access-policy.v1",
        }
    )
    _resign_policy(identity_payload)
    assert AccessPolicy.model_validate_json(canonical_json(identity_payload))

    evidence_payload = _policy("valid_200").model_dump(mode="json")
    evidence_payload["origin_policy_evidence"]["warnings"] = [
        "note=publicMetadata available"
    ]
    evidence_payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
        "prefix=monkey=value"
    )
    _resign_origin_policy_evidence(evidence_payload)
    _resign_policy(evidence_payload)
    assert AccessPolicy.model_validate_json(canonical_json(evidence_payload))


def test_nested_query_secret_fails_but_benign_nested_url_remains_valid() -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?next=https://other.example/?page=2",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert decision.outcome == "allow"

    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = (
        "https://example.com/?next=https://other.example/?apiKey=secretvalue"
    )
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="sensitive"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_resigned_embedded_uri_userinfo_in_evidence_fails_closed() -> None:
    policy = _policy("http_404")
    payload = policy.model_dump(mode="json")
    payload["origin_policy_evidence"]["warnings"] = [
        "proxy failed at http://alice:supersecret@proxy.example:8080/"
    ]
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="userinfo|sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "nested_url",
    [
        "https://safe.example/?proxy=https://alice:supersecret@proxy.example:8080/",
        (
            "https://safe.example/?proxy="
            "https%3A%2F%2Falice%3Asupersecret%40proxy.example%3A8080%2F"
        ),
        (
            "https://safe.example/?proxy="
            "https%253A%252F%252Falice%253Asupersecret%2540"
            "proxy.example%253A8080%252F"
        ),
    ],
)
def test_resigned_overlapping_nested_uri_userinfo_in_url_fails_closed(
    nested_url: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?next=https://safe.example/public",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = f"https://example.com/?next={nested_url}"
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="userinfo|sensitive|credentials"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "nested_url"),
    [
        (
            "identity",
            "https://safe.example/?proxy=https://alice:supersecret@proxy.example/",
        ),
        (
            "warning",
            "https://safe.example/?proxy=https://alice:supersecret@proxy.example/",
        ),
        (
            "warning",
            "https://safe.example/?proxy="
            "https%3A%2F%2Falice%3Asupersecret%40proxy.example%2F",
        ),
        (
            "warning",
            "https://safe.example/?proxy="
            "https%253A%252F%252Falice%253Asupersecret%2540proxy.example%252F",
        ),
        (
            "rule",
            "https://safe.example/?proxy="
            "https%253A%252F%252Falice%253Asupersecret%2540proxy.example%252F",
        ),
    ],
)
def test_resigned_overlapping_nested_uri_userinfo_in_policy_text_fails_closed(
    location: str,
    nested_url: str,
) -> None:
    policy = _policy("timeout" if location == "identity" else "valid_200")
    payload = policy.model_dump(mode="json")
    sensitive_text = f"nested fetch evidence: {nested_url}"
    if location == "identity":
        payload["identity"]["user_agent"] = f"web-listening-bot/1.1 {sensitive_text}"
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    elif location == "warning":
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    else:
        payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
            sensitive_text
        )
        _resign_origin_policy_evidence(payload)
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="userinfo|sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "nested_url",
    [
        "https://safe.example/?next=https://public.example/path",
        "https://safe.example/?next=https%3A%2F%2Fpublic.example%2Fpath",
        "https://safe.example/?next=https%253A%252F%252Fpublic.example%252Fpath",
    ],
)
def test_overlapping_nested_uri_scan_preserves_benign_urls(nested_url: str) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url=f"https://example.com/?next={nested_url}",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert decision.outcome == "allow"

    payload = _policy("valid_200").model_dump(mode="json")
    payload["origin_policy_evidence"]["warnings"] = [
        f"public nested location: {nested_url}"
    ]
    _resign_policy(payload)
    assert AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "nested_authority",
    [
        "https://al(ice)'x:secret@proxy.example/",
        "https%3A%2F%2Fal%28ice%29%27x%3Asecret%40proxy.example%2F",
        "https%253A%252F%252Fal%2528ice%2529%2527x%253Asecret%2540proxy.example%252F",
        "socks5://alice:secret@proxy.example:1080/",
        "//alice:secret@proxy.example:8080/",
    ],
)
def test_resigned_nested_authority_userinfo_variants_fail_closed(
    nested_authority: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?proxy=https://public.example/",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = f"https://example.com/?proxy={nested_authority}"
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="userinfo|credentials|sensitive"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "nested_authority"),
    [
        ("identity", "socks5://alice:secret@proxy.example:1080/"),
        ("warning", "https://al(ice)'x:secret@proxy.example/"),
        ("warning", "%2F%2Falice%3Asecret%40proxy.example%3A8080%2F"),
        (
            "rule",
            "socks5%253A%252F%252Falice%253Asecret%2540proxy.example%253A1080",
        ),
    ],
)
def test_resigned_policy_text_rejects_proxy_and_network_authority_userinfo(
    location: str,
    nested_authority: str,
) -> None:
    policy = _policy("timeout" if location == "identity" else "valid_200")
    payload = policy.model_dump(mode="json")
    sensitive_text = f"nested proxy evidence: {nested_authority}"
    if location == "identity":
        payload["identity"]["user_agent"] = f"web-listening-bot/1.1 {sensitive_text}"
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    elif location == "warning":
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    else:
        payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
            sensitive_text
        )
        _resign_origin_policy_evidence(payload)
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="userinfo|sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    "nested_authority",
    [
        "https://public.example/path/alice@example.net",
        "socks5://proxy.example/path/alice@example.net",
        "//public.example/path/alice@example.net",
        "https://public.example/?contact=alice@example.net",
        "notsocks5://alice:secret@proxy.example",
        "myhttps://alice:secret@proxy.example",
    ],
)
def test_authority_terminators_preserve_benign_nested_references(
    nested_authority: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url=f"https://example.com/?proxy={nested_authority}",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert decision.outcome == "allow"


def test_access_authority_scan_does_not_change_generic_portable_json() -> None:
    value = {
        "note": (
            "https://safe.example/?proxy=socks5://alice:secret@proxy.example:1080/"
        )
    }
    assert validate_portable_json(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "socks5://al(ice)'x:secret@proxy.example/",
        "nested //alice:secret@proxy.example/",
        "nested %2F%2Falice%3Asecret%40proxy.example%2F",
        "nested socks5%253A%252F%252Falice%253Asecret%2540proxy.example",
    ],
)
def test_access_authority_scanner_covers_identity_text_forms(value: str) -> None:
    assert contains_uri_userinfo(f"web-listening-bot/1.1 {value}")


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://example.com/?next=note|//alice:secret@proxy.example/",
        ("https://example.com/?next=note%7C%2F%2Falice%3Asecret%40proxy.example%2F"),
        (
            "https://example.com/?next="
            "note%257C%252F%252Falice%253Asecret%2540proxy.example%252F"
        ),
    ],
)
def test_resigned_pipe_delimited_network_path_in_decision_fails_closed(
    canonical_url: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?next=note",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = canonical_url
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="userinfo|credentials"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("location", "sensitive_text"),
    [
        ("identity", "web-listening-bot/1.1 note|//alice:secret@proxy.example/"),
        ("warning", "note%7C%2F%2Falice%3Asecret%40proxy.example%2F"),
        (
            "rule",
            "note%257C%252F%252Falice%253Asecret%2540proxy.example%252F",
        ),
    ],
)
def test_resigned_pipe_delimited_network_path_in_policy_text_fails_closed(
    location: str,
    sensitive_text: str,
) -> None:
    policy = _policy("timeout" if location == "identity" else "valid_200")
    payload = policy.model_dump(mode="json")
    if location == "identity":
        payload["identity"]["user_agent"] = sensitive_text
        identity_payload = {
            key: value
            for key, value in payload["identity"].items()
            if key != "identity_sha256"
        }
        identity_sha256 = canonical_sha256(identity_payload)
        payload["identity"]["identity_sha256"] = identity_sha256
        payload["cache_key_sha256"] = canonical_sha256(
            {
                "canonical_origin": ORIGIN.as_url_origin(),
                "identity_sha256": identity_sha256,
                "policy_version": "access-policy.v1",
            }
        )
    elif location == "warning":
        payload["origin_policy_evidence"]["warnings"] = [sensitive_text]
    else:
        payload["origin_policy_evidence"]["selected_rules"][0]["pattern"] = (
            sensitive_text
        )
        _resign_origin_policy_evidence(payload)
    _resign_policy(payload)

    with pytest.raises(ValidationError, match="userinfo|sensitive"):
        AccessPolicy.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize("boundary", ["|", "^", "`"])
def test_network_path_scanner_accepts_principled_text_boundaries(
    boundary: str,
) -> None:
    assert contains_uri_userinfo(f"note{boundary}//alice:secret@proxy.example/path")


@pytest.mark.parametrize(
    "value",
    [
        "noteA//alice:secret@proxy.example/path",
        "https://public.example/path//alice:secret@proxy.example/path",
        "https://public.example/path)//alice:secret@proxy.example/path",
        "note=notsocks5://alice:secret@proxy.example/path",
        "note|//public.example/path",
        "note|/public//alice:secret@proxy.example/path",
        "note|//alice|annotation@proxy.example/path",
    ],
)
def test_network_path_text_boundaries_preserve_benign_adjacent_paths(
    value: str,
) -> None:
    assert not contains_uri_userinfo(value)


@pytest.mark.parametrize(
    ("canonical_url", "noncanonical_url"),
    [
        ("https://example.com/?x=%2F", "https://example.com/?x=%2f"),
        ("https://example.com/?x=~", "https://example.com/?x=%7E"),
    ],
)
def test_resigned_query_equivalents_require_one_canonical_representation(
    canonical_url: str,
    noncanonical_url: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url=canonical_url,
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    assert decision.canonical_url == canonical_url

    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = noncanonical_url
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="canonical|percent"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_canonical_query_preserves_parameter_order_and_reserved_encoding() -> None:
    request, origin = _allow_reservations()
    canonical_url = "https://example.com/?b=a%2Fb&a=1"
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url=canonical_url,
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )

    assert decision.canonical_url == canonical_url


@pytest.mark.parametrize(
    "noncanonical_url",
    [
        "https://example.com/?x=a b",
        "https://example.com/?x=a\tb",
        "https://example.com/?x=café",
        "https://example.com/?x=%",
        "https://example.com/?x=%2",
        "https://example.com/?x=%GG",
    ],
)
def test_resigned_noncanonical_query_text_fails_closed(
    noncanonical_url: str,
) -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/?x=safe",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["canonical_url"] = noncanonical_url
    _resign_decision(payload)

    with pytest.raises(
        ValidationError,
        match="canonical|percent|clean|whitespace|ASCII|malformed",
    ):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize("window_mutation", ["duration", "shifted_start"])
def test_resigned_overlapping_window_redefinition_fails_closed(
    window_mutation: str,
) -> None:
    policy = _policy("http_404")
    first = _redirect_hop(
        policy=policy,
        source_url="https://example.com/start",
        target_url="https://example.com/second",
        budget_used_before_reservation=0,
    )
    second = _redirect_hop(
        policy=policy,
        hop_ordinal=2,
        source_url="https://example.com/second",
        target_url="https://example.com/final",
        proof_time=datetime(2026, 8, 8, 12, 2, tzinfo=timezone.utc),
        request_started_at=datetime(2026, 8, 8, 12, 2, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 8, 8, 12, 2, 2, tzinfo=timezone.utc),
        budget_used_before_reservation=1,
    )
    request, origin = _allow_reservations(
        request_slot_ordinal=3,
        budget_used_before_reservation=2,
    )
    decision = build_access_decision(
        policy=policy,
        canonical_url="https://example.com/final",
        decision_time=DECISION_TIME,
        redirect_hops=[first, second],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    second_reservation = payload["redirect_hops"][1]["access_proof"][
        "origin_reservation"
    ]
    second_reservation["budget_used_before_reservation"] = 0
    second_reservation["budget_slot_ordinal"] = 1
    if window_mutation == "duration":
        second_reservation["budget_window_seconds"] = 3599
    else:
        second_reservation["budget_window_started_at"] = "2026-08-08T12:00:01Z"
    _resign_decision(payload["redirect_hops"][1]["access_proof"])
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="window|overlap|lineage"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_non_overlapping_windows_keep_origin_pacing_and_reset_budget() -> None:
    policy = _policy("http_404")
    first = _redirect_hop(
        policy=policy,
        source_url="https://example.com/start",
        target_url="https://example.com/second",
        budget_used_before_reservation=0,
        budget_window_started_at=datetime(2026, 8, 8, 12, 1, tzinfo=timezone.utc),
        budget_window_seconds=30,
    )
    second = _redirect_hop(
        policy=policy,
        hop_ordinal=2,
        source_url="https://example.com/second",
        target_url="https://example.com/final",
        proof_time=datetime(2026, 8, 8, 12, 2, tzinfo=timezone.utc),
        request_started_at=datetime(2026, 8, 8, 12, 2, 1, tzinfo=timezone.utc),
        observed_at=datetime(2026, 8, 8, 12, 2, 2, tzinfo=timezone.utc),
        budget_used_before_reservation=0,
        budget_window_started_at=datetime(2026, 8, 8, 12, 2, tzinfo=timezone.utc),
        budget_window_seconds=30,
    )
    request, origin = _allow_reservations(
        request_slot_ordinal=3,
        budget_used_before_reservation=0,
        budget_window_started_at=DECISION_TIME,
        budget_window_seconds=30,
    )
    decision = build_access_decision(
        policy=policy,
        canonical_url="https://example.com/final",
        decision_time=DECISION_TIME,
        redirect_hops=[first, second],
        request_slot_reservation=request,
        origin_reservation=origin,
    )

    assert decision.origin_reservation == origin

    payload = decision.model_dump(mode="json")
    second_proof = payload["redirect_hops"][1]["access_proof"]
    second_proof["origin_reservation"]["pacing_interval_ms"] = 2000
    _resign_decision(second_proof)
    _resign_decision(payload)
    with pytest.raises(ValidationError, match="pacing contract"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_resigned_final_not_before_cannot_outlive_policy_authority() -> None:
    request, origin = _allow_reservations(
        budget_window_started_at=DECISION_TIME,
        budget_window_seconds=86_400,
    )
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/final",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    payload["origin_reservation"]["not_before"] = "2026-08-09T12:01:00Z"
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="not_before|expiry|authority"):
        AccessDecision.model_validate_json(canonical_json(payload))


def test_resigned_redirect_request_must_start_inside_its_budget_window() -> None:
    policy = _policy("http_404")
    proof_time = datetime(2026, 8, 8, 12, 1, tzinfo=timezone.utc)
    hop = _redirect_hop(
        policy=policy,
        source_url="https://example.com/start",
        target_url="https://example.com/final",
        proof_time=proof_time,
        budget_used_before_reservation=0,
        budget_window_started_at=proof_time,
        budget_window_seconds=2,
    )
    request, origin = _allow_reservations(
        request_slot_ordinal=2,
        budget_used_before_reservation=0,
        budget_window_started_at=DECISION_TIME,
        budget_window_seconds=30,
    )
    decision = build_access_decision(
        policy=policy,
        canonical_url="https://example.com/final",
        decision_time=DECISION_TIME,
        redirect_hops=[hop],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    proof_payload = payload["redirect_hops"][0]["access_proof"]
    proof_payload["origin_reservation"]["budget_window_seconds"] = 1
    _resign_decision(proof_payload)
    _resign_decision(payload)

    with pytest.raises(ValidationError, match="request|window"):
        AccessDecision.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("budget_window_seconds", 10**100),
        ("pacing_interval_ms", 10**100),
        ("budget_limit", 10**100),
        ("budget_used_before_reservation", 10**100),
        ("budget_slot_ordinal", 10**100),
    ],
)
def test_extreme_reservation_numbers_are_governed_validation_errors(
    field_name: str,
    value: int,
) -> None:
    payload = _allow_reservations()[1].model_dump(mode="python")
    payload[field_name] = value
    with pytest.raises(ValidationError):
        OriginPacingBudgetReservation.model_validate(payload)

    if field_name == "budget_window_seconds":
        payload[field_name] = 1
        payload["budget_window_started_at"] = datetime.max.replace(tzinfo=timezone.utc)
        payload["reserved_at"] = payload["budget_window_started_at"]
        payload["not_before"] = payload["budget_window_started_at"]
        with pytest.raises(ValidationError):
            OriginPacingBudgetReservation.model_validate(payload)


def test_reservation_numeric_bounds_are_frozen_in_json_schema() -> None:
    origin_properties = OriginPacingBudgetReservation.model_json_schema()["properties"]
    assert origin_properties["pacing_interval_ms"]["maximum"] == 86_400_000
    assert origin_properties["budget_window_seconds"]["maximum"] == 86_400
    assert origin_properties["budget_limit"]["maximum"] == 1_000_000
    assert origin_properties["budget_used_before_reservation"]["maximum"] == 1_000_000
    assert origin_properties["budget_slot_ordinal"]["maximum"] == 1_000_000
    assert (
        RequestSlotReservation.model_json_schema()["properties"][
            "request_slot_ordinal"
        ]["maximum"]
        == 1_000_000
    )
    assert (
        RedirectHop.model_json_schema()["properties"]["hop_ordinal"]["maximum"]
        == 1_000_000
    )


def test_contracts_are_strict_and_publish_closed_json_schema() -> None:
    policy_schema = AccessPolicy.model_json_schema()
    decision_schema = AccessDecision.model_json_schema()
    envelope_schema = AccessRejectionErrorEnvelope.model_json_schema()
    assert policy_schema["additionalProperties"] is False
    assert decision_schema["additionalProperties"] is False
    assert envelope_schema["additionalProperties"] is False
    evidence_schema = envelope_schema["$defs"]["AccessDecisionEvidence"]
    assert evidence_schema["additionalProperties"] is False
    assert "robots_observation" in evidence_schema["required"]

    policy_payload = _policy("http_404").model_dump(mode="json")
    policy_payload["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        AccessPolicy.model_validate(policy_payload)

    decision = build_access_decision(
        policy=_policy("http_401"),
        canonical_url="https://example.com/",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=None,
        origin_reservation=None,
    )
    decision_payload = decision.model_dump(mode="json")
    decision_payload["reason_code"] = None
    with pytest.raises(ValidationError):
        AccessDecision.model_validate(decision_payload)

    envelope_payload = _standalone_envelope("timeout").model_dump(mode="json")
    envelope_payload["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        AccessRejectionErrorEnvelope.model_validate(envelope_payload)


def test_model_json_duplicate_key_preparse_rejects_excessive_nesting() -> None:
    deeply_nested = "[" * 5_000 + '{"schema_version":"access-policy.v1"}' + "]" * 5_000

    # CPython's decoder and pydantic-core may report this limit at different
    # layers.  The governed contract is the exception boundary, not either
    # implementation's diagnostic wording.
    with pytest.raises(ValueError):
        AccessPolicy.model_validate_json(deeply_nested)


def test_loader_wraps_excessive_json_nesting(tmp_path: Path) -> None:
    path = tmp_path / "deep-access-contract.json"
    path.write_text(
        "[" * 5_000 + '{"schema_version":"access-policy.v1"}' + "]" * 5_000,
        encoding="utf-8",
    )

    with pytest.raises(AccessContractError):
        load_access_contract(path)


def test_validation_is_idempotent_and_does_not_mutate_input() -> None:
    policy = _policy("http_404")
    hop = _redirect_hop(policy=policy)
    request, origin = _allow_reservations(request_slot_ordinal=2)
    decision = build_access_decision(
        policy=policy,
        canonical_url="https://example.com/public",
        decision_time=DECISION_TIME,
        redirect_hops=[hop],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    payload = decision.model_dump(mode="json")
    original = copy.deepcopy(payload)

    first = AccessDecision.model_validate_json(canonical_json(payload))
    second = AccessDecision.model_validate_json(canonical_json(payload))

    assert payload == original
    assert first == second == decision
    assert canonical_json(first.model_dump(mode="json")) == canonical_json(
        second.model_dump(mode="json")
    )


def _standalone_envelope(kind: str) -> AccessRejectionErrorEnvelope:
    decision = build_access_decision(
        policy=_policy(kind),
        canonical_url=(
            "https://example.com/private"
            if kind == "valid_200"
            else "https://example.com/"
        ),
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=None,
        origin_reservation=None,
    )
    assert decision.rejection_or_error is not None
    return decision.rejection_or_error


@pytest.mark.parametrize(
    ("kind", "outcome", "reason_code", "retryable"),
    [
        ("valid_200", "reject", "robots.disallowed", False),
        ("http_401", "reject", "robots.auth_required", False),
        ("http_403", "reject", "robots.forbidden", False),
        ("timeout", "error", "robots.timeout", True),
        ("dns_error", "error", "robots.dns_error", True),
        ("network_error", "error", "robots.network_error", True),
        ("parse_error", "error", "robots.parse_error", False),
    ],
)
def test_standalone_envelope_freezes_the_robots_matrix(
    kind: str,
    outcome: str,
    reason_code: str,
    retryable: bool,
) -> None:
    envelope = _standalone_envelope(kind)
    standalone = AccessRejectionErrorEnvelope.model_validate_json(
        canonical_json(envelope.model_dump(mode="json"))
    )
    assert (standalone.outcome, standalone.reason_code, standalone.retryable) == (
        outcome,
        reason_code,
        retryable,
    )


def test_standalone_envelope_loads_and_roundtrips(tmp_path: Path) -> None:
    envelope = _standalone_envelope("timeout")
    path = tmp_path / "access-error.json"
    canonical = canonical_json(envelope.model_dump(mode="json")) + "\n"
    path.write_text(canonical, encoding="utf-8")

    loaded = load_access_contract(path)
    assert isinstance(loaded, AccessRejectionErrorEnvelope)
    assert loaded == envelope
    assert canonical_json(loaded.model_dump(mode="json")) + "\n" == canonical


@pytest.mark.parametrize(
    "mutation",
    [
        "access_policy_id",
        "origin_policy_id",
        "cache_key",
        "reverse_freshness",
        "long_freshness",
        "partial_origin_policy",
    ],
)
def test_standalone_envelope_evidence_tampering_fails_closed(
    mutation: str,
) -> None:
    payload = _standalone_envelope("valid_200").model_dump(mode="json")
    evidence = payload["evidence"]
    assert isinstance(evidence, dict)
    if mutation == "access_policy_id":
        evidence["access_policy_id"] = "apiKey=secret"
    elif mutation == "origin_policy_id":
        evidence["origin_policy_id"] = "robots-policy-0000000000000000"
    elif mutation == "cache_key":
        evidence["cache_key_sha256"] = "0" * 64
    elif mutation == "reverse_freshness":
        evidence["policy_observed_at"], evidence["policy_expires_at"] = (
            evidence["policy_expires_at"],
            evidence["policy_observed_at"],
        )
    elif mutation == "long_freshness":
        evidence["policy_expires_at"] = "2026-08-10T12:00:01Z"
    else:
        evidence["origin_policy_id"] = None

    with pytest.raises(ValidationError):
        AccessRejectionErrorEnvelope.model_validate_json(canonical_json(payload))


@pytest.mark.parametrize(
    ("target_kind", "evidence_kind"),
    [
        ("valid_200", "timeout"),
        ("http_401", "valid_200"),
        ("http_401", "timeout"),
        ("http_403", "valid_200"),
        ("timeout", "valid_200"),
        ("timeout", "http_401"),
        ("dns_error", "valid_200"),
        ("network_error", "valid_200"),
        ("parse_error", "valid_200"),
    ],
)
def test_standalone_envelope_reason_requires_exact_evidence_shape(
    target_kind: str,
    evidence_kind: str,
) -> None:
    payload = _standalone_envelope(target_kind).model_dump(mode="json")
    payload["evidence"] = _standalone_envelope(evidence_kind).model_dump(mode="json")[
        "evidence"
    ]

    with pytest.raises(ValidationError, match="evidence|policy"):
        AccessRejectionErrorEnvelope.model_validate_json(canonical_json(payload))


def test_shared_envelope_rejects_secret_like_evidence() -> None:
    with pytest.raises(ValidationError):
        AccessRejectionErrorEnvelope(
            outcome="error",
            reason_code="contract.invalid",
            message="invalid access contract",
            retryable=False,
            evidence={"Authorization": "Bearer secret"},
        )


def test_contract_rejects_sensitive_url_keys_and_header_shaped_identity() -> None:
    request, origin = _allow_reservations()
    for query_key in (
        "access_token",
        "accessToken",
        "api-key",
        "apikey",
        "proxycredentials",
        "ａｐｉｋｅｙ",
    ):
        with pytest.raises(ValueError, match="sensitive"):
            build_access_decision(
                policy=_policy("http_404"),
                canonical_url=f"https://example.com/?{query_key}=secret",
                decision_time=DECISION_TIME,
                redirect_hops=[],
                request_slot_reservation=request,
                origin_reservation=origin,
            )

    identity_payload = {
        "identity_id": "unsafe-identity",
        "product_token": "web-listening-bot",
        "user_agent": "web-listening-bot/1.1 Authorization: Bearer secret",
    }
    identity = DiagnosticIdentity(
        **identity_payload,
        identity_sha256=canonical_sha256(identity_payload),
    )
    with pytest.raises(ValidationError, match="sensitive"):
        build_access_policy(
            canonical_origin=ORIGIN,
            identity=identity,
            diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
            robots_observation=RobotsObservation(kind="timeout", http_status=None),
            origin_policy_evidence=None,
            observed_at=OBSERVED_AT,
            expires_at=EXPIRES_AT,
        )


@pytest.mark.parametrize("mutation", ["warning", "declared_sitemap"])
def test_embedded_origin_policy_evidence_cannot_carry_sensitive_material(
    mutation: str,
) -> None:
    evidence = _available_evidence()
    if mutation == "warning":
        evidence = evidence.model_copy(update={"warnings": ["apiKey=secret"]})
    else:
        evidence = evidence.model_copy(
            update={
                "declared_sitemaps": [
                    SitemapDirective(
                        url=("https://example.com/sitemap.xml?proxycredentials=secret"),
                        line_number=4,
                    )
                ]
            }
        )

    with pytest.raises(ValidationError, match="sensitive"):
        build_access_policy(
            canonical_origin=ORIGIN,
            identity=IDENTITY,
            diagnostic_artifact_sha256=DIAGNOSTIC_SHA256,
            robots_observation=RobotsObservation(kind="valid_200", http_status=200),
            origin_policy_evidence=evidence,
            observed_at=OBSERVED_AT,
            expires_at=EXPIRES_AT,
        )


def test_shared_envelope_messages_are_governed_literals() -> None:
    with pytest.raises(ValidationError):
        AccessRejectionErrorEnvelope(
            schema_version="access-rejection-error.v1",
            outcome="error",
            reason_code="contract.invalid",
            message="Authorization: Bearer secret",
            retryable=False,
            evidence=None,
        )

    with pytest.raises(ValidationError, match="evidence"):
        AccessRejectionErrorEnvelope(
            schema_version="access-rejection-error.v1",
            outcome="reject",
            reason_code="robots.forbidden",
            message="access rejected by governed robots policy",
            retryable=False,
            evidence=None,
        )


def test_canonical_and_negative_contract_fixtures() -> None:
    policy = load_access_contract(FIXTURES / "access-policy-v1.sample.json")
    decision = load_access_contract(FIXTURES / "access-decision-v1.sample.json")
    envelope = load_access_contract(FIXTURES / "access-rejection-error-v1.sample.json")
    assert isinstance(policy, AccessPolicy)
    assert isinstance(decision, AccessDecision)
    assert isinstance(envelope, AccessRejectionErrorEnvelope)
    assert canonical_json(policy.model_dump(mode="json")) + "\n" == (
        FIXTURES / "access-policy-v1.sample.json"
    ).read_text(encoding="utf-8")
    assert canonical_json(decision.model_dump(mode="json")) + "\n" == (
        FIXTURES / "access-decision-v1.sample.json"
    ).read_text(encoding="utf-8")
    assert canonical_json(envelope.model_dump(mode="json")) + "\n" == (
        FIXTURES / "access-rejection-error-v1.sample.json"
    ).read_text(encoding="utf-8")

    for name in (
        "access-policy-v1.unknown-shape.invalid.json",
        "access-policy-v1.duplicate-key.invalid.json",
        "access-decision-v1.tampered.invalid.json",
    ):
        assert (FIXTURES / name).is_file(), name
        with pytest.raises(AccessContractError):
            load_access_contract(FIXTURES / name)

    for name in (
        "access-decision-v1.proxy-authority.invalid.json",
        "access-decision-v1.overlapping-userinfo.invalid.json",
        "access-decision-v1.nested-sensitive-url.invalid.json",
        "access-decision-v1.nfkc-query.invalid.json",
        "access-decision-v1.namespaced-secret.invalid.json",
        "access-decision-v1.sensitive-url.invalid.json",
        "access-decision-v1.pipe-network-userinfo.invalid.json",
        "access-policy-v1.encoded-nested-userinfo.invalid.json",
        "access-policy-v1.encoded-sensitive-text.invalid.json",
        "access-policy-v1.namespaced-secret.invalid.json",
        "access-policy-v1.network-authority.invalid.json",
        "access-policy-v1.nested-sensitive-text.invalid.json",
        "access-policy-v1.pipe-network-userinfo.invalid.json",
        "access-policy-v1.sensitive-evidence.invalid.json",
        "access-policy-v1.sensitive-identity.invalid.json",
        "access-policy-v1.uri-userinfo.invalid.json",
    ):
        assert (FIXTURES / name).is_file(), name
        with pytest.raises(
            AccessContractError,
            match="sensitive|credentials|userinfo",
        ):
            load_access_contract(FIXTURES / name)

    for name, id_field, digest_field, prefix in (
        (
            "access-decision-v1.proxy-authority.invalid.json",
            "decision_id",
            "decision_sha256",
            "access-decision-",
        ),
        (
            "access-decision-v1.overlapping-userinfo.invalid.json",
            "decision_id",
            "decision_sha256",
            "access-decision-",
        ),
        (
            "access-decision-v1.nested-sensitive-url.invalid.json",
            "decision_id",
            "decision_sha256",
            "access-decision-",
        ),
        (
            "access-decision-v1.namespaced-secret.invalid.json",
            "decision_id",
            "decision_sha256",
            "access-decision-",
        ),
        (
            "access-decision-v1.pipe-network-userinfo.invalid.json",
            "decision_id",
            "decision_sha256",
            "access-decision-",
        ),
        (
            "access-policy-v1.network-authority.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
        (
            "access-policy-v1.encoded-nested-userinfo.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
        (
            "access-policy-v1.encoded-sensitive-text.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
        (
            "access-policy-v1.namespaced-secret.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
        (
            "access-policy-v1.pipe-network-userinfo.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
        (
            "access-policy-v1.uri-userinfo.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
        (
            "access-decision-v1.nfkc-query.invalid.json",
            "decision_id",
            "decision_sha256",
            "access-decision-",
        ),
        (
            "access-policy-v1.nested-sensitive-text.invalid.json",
            "policy_id",
            "policy_sha256",
            "access-policy-",
        ),
    ):
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        expected = canonical_sha256(
            {
                key: value
                for key, value in payload.items()
                if key not in {id_field, digest_field}
            }
        )
        assert payload[digest_field] == expected
        assert payload[id_field] == f"{prefix}{expected[:16]}"

    for name, message in (
        (
            "access-rejection-error-v1.tampered.invalid.json",
            "ID/digest|sensitive",
        ),
        (
            "access-rejection-error-v1.cache-key.invalid.json",
            "cache key",
        ),
        (
            "access-rejection-error-v1.freshness.invalid.json",
            "freshness",
        ),
        (
            "access-rejection-error-v1.matrix.invalid.json",
            "robots observation",
        ),
        (
            "access-rejection-error-v1.partial-policy.invalid.json",
            "all null or all present",
        ),
    ):
        assert (FIXTURES / name).is_file(), name
        with pytest.raises(AccessContractError, match=message):
            load_access_contract(FIXTURES / name)

    numeric_fixture = FIXTURES / "access-decision-v1.numeric-overflow.invalid.json"
    assert numeric_fixture.is_file()
    with pytest.raises(AccessContractError, match="maximum|portable|less than"):
        load_access_contract(numeric_fixture)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":"access-policy.v1","schema_version":"access-policy.v1"}',
        encoding="utf-8",
    )
    with pytest.raises(AccessContractError, match="duplicate"):
        load_access_contract(path)


def test_loader_wraps_unexpected_model_arithmetic_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def overflow(*_args: object, **_kwargs: object) -> AccessDecision:
        raise OverflowError("simulated governed arithmetic overflow")

    monkeypatch.setattr(AccessDecision, "model_validate_json", overflow)
    with pytest.raises(AccessContractError, match="overflow"):
        load_access_contract(FIXTURES / "access-decision-v1.sample.json")


def test_canonical_json_contains_no_sensitive_header_or_userinfo() -> None:
    request, origin = _allow_reservations()
    decision = build_access_decision(
        policy=_policy("http_404"),
        canonical_url="https://example.com/",
        decision_time=DECISION_TIME,
        redirect_hops=[],
        request_slot_reservation=request,
        origin_reservation=origin,
    )
    encoded = canonical_json(decision.model_dump(mode="json")).casefold()
    assert "authorization" not in encoded
    assert "cookie" not in encoded
    assert "proxy_credential" not in encoded
    assert "@example.com" not in encoded
