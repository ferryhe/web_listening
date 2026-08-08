from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from web_listening.blocks.site_diagnostic import (
    DiagnosticBudgets,
    RawHttpResponse,
    SafePinnedTransport,
    SiteDiagnosticError,
    TransportFailure,
    canonical_host_header,
    diagnose_site,
    load_site_diagnostic,
    normalize_http_url,
    parse_robots,
    write_site_diagnostic,
)
from web_listening.contracts.site_diagnostic import (
    NormalizedOrigin,
    SiteDiagnostic,
    canonical_sha256,
    document_duplicate_reason,
)


@dataclass
class FakeTransport:
    responses: dict[str, list[RawHttpResponse | Exception]]

    def __post_init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []

    def request(self, url: str, *, user_agent: str, identity_sha256: str) -> RawHttpResponse:
        self.requests.append((url, user_agent, identity_sha256))
        queued = self.responses.get(url, [])
        if not queued:
            raise AssertionError(f"unexpected request: {url}")
        item = queued.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int, body: bytes = b"", content_type: str | None = None, **headers: str) -> RawHttpResponse:
    values = {k.replace("_", "-"): v for k, v in headers.items()}
    if content_type is not None:
        values["Content-Type"] = content_type
    return RawHttpResponse(status=status, headers=values, body_chunks=(body,))


def diagnose(transport: FakeTransport, **kwargs: object):
    arguments: dict[str, object] = dict(
        requested_url="https://Example.COM/news",
        site_key="example",
        allowed_domains=["example.com"],
        allowed_document_origins=["https://example.com"],
        user_agent="web-listening-bot/1.1",
        product_token="web-listening-bot",
        identity_id="web-listening-default",
        transport=transport,
    )
    arguments.update(kwargs)
    return diagnose_site(**arguments)


def test_robots_is_first_and_sitemap_pages_are_never_fetched() -> None:
    robots = b"User-agent: web-listening-bot\nAllow: /\nSitemap: https://example.com/map.xml\n"
    sitemap = b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/a</loc></url></urlset>'
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "Text/Plain ; charset = UTF-8")],
        "https://example.com/map.xml": [response(200, sitemap, "application/xml")],
    })

    artifact = diagnose(transport)

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://example.com/map.xml",
    ]
    assert artifact.diagnostic_status == "complete"
    assert artifact.recommendation == "sitemap_seeded"
    assert artifact.accepted_page_urls == ["https://example.com/a"]
    assert all(item[0] != "https://example.com/a" for item in transport.requests)
    assert artifact.attempts[0].attempt_ordinal == 1
    assert artifact.attempts[1].queue_ordinal == 1


def test_cross_origin_sitemap_requires_exact_origin_robots_preflight() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    aux_robots = b"User-agent: web-listening-bot\nAllow: /site.xml\nSitemap: https://maps.example.net/ignored.xml\n"
    sitemap = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://maps.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
        "https://maps.example.net/site.xml": [response(200, sitemap, "application/xml")],
    })

    artifact = diagnose_site(
        requested_url="https://example.com",
        site_key="example",
        allowed_domains=["example.com", "maps.example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
        user_agent="web-listening-bot/1.1",
        product_token="web-listening-bot",
        identity_id="default",
        transport=transport,
    )

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://maps.example.net/robots.txt",
        "https://maps.example.net/site.xml",
    ]
    assert artifact.accepted_page_urls == ["https://example.com/a"]
    assert all("ignored.xml" not in item[0] for item in transport.requests)
    assert len(artifact.origin_policy_evidence) == 2
    assert [[(rule.allow, rule.pattern) for rule in item.selected_rules] for item in artifact.origin_policy_evidence] == [
        [(True, "/")],
        [(True, "/site.xml")],
    ]


@pytest.mark.parametrize(
    (
        "aux_responses",
        "expected_priority",
        "expected_attempt_outcome",
        "expected_preflight_outcome",
    ),
    [
        ([response(400, b"terminal")], 6, "terminal_http", "deterministic"),
        ([response(503, b"retry")] * 3, 4, "transient_http", "transient"),
    ],
)
def test_aux_robots_terminal_failure_participates_in_priority(
    aux_responses: list[RawHttpResponse],
    expected_priority: int,
    expected_attempt_outcome: str,
    expected_preflight_outcome: str,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://maps.example.net/robots.txt": aux_responses,
    })

    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )

    assert artifact.decisive_priority == expected_priority
    assert artifact.sitemap_evidence == []
    assert artifact.rejected_urls[0].reason == (
        f"sitemap_policy_preflight_{expected_preflight_outcome}"
    )
    assert artifact.attempts[-1].outcome == expected_attempt_outcome


@pytest.mark.parametrize(
    (
        "aux_responses",
        "expected_preflight_classification",
        "expected_reason",
        "expected_status",
        "expected_recommendation",
        "expected_priority",
    ),
    [
        (
            [response(400)],
            "deterministic",
            "http:400",
            "partial",
            "sitemap_seeded",
            2,
        ),
        (
            [response(503)] * 3,
            "transient",
            "http:503",
            "partial",
            "sitemap_seeded",
            2,
        ),
    ],
)
def test_aux_terminal_preflight_continues_independent_fifo_root(
    tmp_path: Path,
    aux_responses: list[RawHttpResponse],
    expected_preflight_classification: str,
    expected_reason: str,
    expected_status: str,
    expected_recommendation: str,
    expected_priority: int,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps.example.net/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    second = (
        b"<urlset><url><loc>https://example.com/seed</loc></url></urlset>"
    )
    aux_request_count = len(aux_responses)
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://maps.example.net/robots.txt": aux_responses,
        "https://example.com/two.xml": [
            response(200, second, "application/xml")
        ],
    })

    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
    )

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        *(
            ["https://maps.example.net/robots.txt"]
            * aux_request_count
        ),
        "https://example.com/two.xml",
    ]
    assert artifact.accepted_page_urls == ["https://example.com/seed"]
    assert [item.queue_ordinal for item in artifact.sitemap_evidence] == [2]
    assert [(item.queue_ordinal, item.reason) for item in artifact.rejected_urls] == [
        (
            1,
            "sitemap_policy_preflight_"
            f"{expected_preflight_classification}",
        )
    ]
    assert (
        artifact.diagnostic_status,
        artifact.recommendation,
        artifact.decisive_priority,
    ) == (expected_status, expected_recommendation, expected_priority)
    assert artifact.outcome_reasons == [expected_reason]

    path = write_site_diagnostic(
        artifact,
        tmp_path / f"two-root-{expected_preflight_classification}.json",
    )
    assert load_site_diagnostic(path) == artifact

    wrong_top = artifact.model_dump(mode="json")
    if expected_preflight_classification == "deterministic":
        wrong_top.update({
            "diagnostic_status": "blocked",
            "recommendation": "operator_review",
            "decisive_priority": 6,
            "next_action": "revise_inputs_or_boundaries_and_rediagnose",
        })
    else:
        wrong_top.update({
            "diagnostic_status": "retryable",
            "recommendation": "retry_diagnosis",
            "decisive_priority": 4,
            "next_action": "retry_diagnosis",
        })
    wrong_top_path = _write_rehashed_artifact(
        tmp_path,
        f"two-root-{expected_preflight_classification}-wrong-top.json",
        wrong_top,
    )
    with pytest.raises(SiteDiagnosticError, match="priority|terminal|accepted"):
        load_site_diagnostic(wrong_top_path)


@pytest.mark.parametrize(
    "aux_responses",
    [[response(400)], [response(503)] * 3],
)
def test_seed_before_aux_terminal_preflight_remains_partial_priority_two(
    aux_responses: list[RawHttpResponse],
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://maps.example.net/two.xml\n"
    )
    first = (
        b"<urlset><url><loc>https://example.com/seed</loc></url></urlset>"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://example.com/one.xml": [
            response(200, first, "application/xml")
        ],
        "https://maps.example.net/robots.txt": aux_responses,
    })

    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
    )

    assert artifact.accepted_page_urls == ["https://example.com/seed"]
    assert (
        artifact.diagnostic_status,
        artifact.recommendation,
        artifact.decisive_priority,
    ) == ("partial", "sitemap_seeded", 2)


@pytest.mark.parametrize(
    (
        "aux_response",
        "budgets",
        "expected_outcome",
    ),
    [
        (
            response(403),
            DiagnosticBudgets(),
            ("blocked", "operator_review", 1),
        ),
        (
            response(301, Location="/robots-final.txt"),
            DiagnosticBudgets(redirect_hops_per_document=0),
            ("partial", "sitemap_seeded", 2),
        ),
    ],
)
def test_seed_priority_table_keeps_safety_above_budget_truncation(
    aux_response: RawHttpResponse,
    budgets: DiagnosticBudgets,
    expected_outcome: tuple[str, str, int],
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://maps.example.net/two.xml\n"
    )
    first = (
        b"<urlset><url><loc>https://example.com/seed</loc></url></urlset>"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
            "https://example.com/one.xml": [
                response(200, first, "application/xml")
            ],
            "https://maps.example.net/robots.txt": [aux_response],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
        budgets=budgets,
    )

    assert artifact.accepted_page_urls == ["https://example.com/seed"]
    assert (
        artifact.diagnostic_status,
        artifact.recommendation,
        artifact.decisive_priority,
    ) == expected_outcome


@pytest.mark.parametrize(
    (
        "aux_responses",
        "budgets",
        "expected_preflight_classification",
        "expected_prior_stop",
    ),
    [
        (
            [response(403)],
            DiagnosticBudgets(),
            "safety",
            "prior_safety_stop",
        ),
        (
            [response(301, Location="/robots-final.txt")],
            DiagnosticBudgets(redirect_hops_per_document=0),
            "budget",
            "prior_budget_stop",
        ),
    ],
)
def test_aux_safety_or_budget_preflight_drains_remaining_roots(
    aux_responses: list[RawHttpResponse],
    budgets: DiagnosticBudgets,
    expected_preflight_classification: str,
    expected_prior_stop: str,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps.example.net/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://maps.example.net/robots.txt": aux_responses,
    })

    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
        budgets=budgets,
    )

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://maps.example.net/robots.txt",
    ]
    assert artifact.sitemap_evidence == []
    assert [(item.queue_ordinal, item.reason) for item in artifact.rejected_urls] == [
        (
            1,
            "sitemap_policy_preflight_"
            f"{expected_preflight_classification}",
        ),
        (2, expected_prior_stop),
    ]


def test_rfc9309_group_and_path_matching() -> None:
    parsed = parse_robots(
        "Rule-before-group: ignored\n"
        "User-agent: web-listening\nDisallow: /prefix\n\n"
        "User-agent: WEB-LISTENING-BOT\nDisallow: /Private\nAllow: /Private/Public\n"
        "User-agent: web-listening-bot\nDisallow: /same\nAllow: /same\n"
        "Disallow: /wild/*/end$\nDisallow: /encoded/%7Euser\n"
        "Sitemap: https://example.com/map.xml # extension does not end group\n"
        "Malformed line\n"
        "User-agent: *\nDisallow: /fallback\n",
        product_token="web-listening-bot",
    )

    assert parsed.is_allowed("https://example.com/private")
    assert not parsed.is_allowed("https://example.com/Private/x")
    assert parsed.is_allowed("https://example.com/Private/Public/x")
    assert parsed.is_allowed("https://example.com/same")  # Allow wins equal specificity.
    assert not parsed.is_allowed("https://example.com/wild/a/end")
    assert parsed.is_allowed("https://example.com/wild/a/end/more")
    assert not parsed.is_allowed("https://example.com/encoded/~user")
    assert parsed.is_allowed("https://example.com/fallback")  # exact groups suppress '*'.
    assert parsed.sitemaps[0].line_number == 13
    assert parsed.warnings


def test_rfc9309_specificity_uses_normalized_octets_before_sitemap_transport() -> None:
    robots = (
        b"User-agent: *\n"
        b"Disallow: /foobar/x\n"
        b"Allow: /%66oobar\n"
        b"Sitemap: https://example.com/foobar/x\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
    })
    artifact = diagnose(transport)
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert artifact.decisive_priority == 1


@pytest.mark.parametrize(
    ("token", "user_agent"),
    [("", "bot/1"), ("bad token", "bad token/1"), ("other", "bot/1")],
)
def test_identity_is_rejected_before_transport(token: str, user_agent: str) -> None:
    transport = FakeTransport({})
    with pytest.raises(SiteDiagnosticError):
        diagnose_site(
            requested_url="https://example.com",
            site_key="example",
            allowed_domains=["example.com"],
            allowed_document_origins=["https://example.com"],
            user_agent=user_agent,
            product_token=token,
            identity_id="identity",
            transport=transport,
        )
    assert transport.requests == []


@pytest.mark.parametrize("field", ["identity_id", "user_agent"])
def test_identity_control_characters_are_rejected_before_transport(field: str) -> None:
    transport = FakeTransport({})
    values = {
        "requested_url": "https://example.com",
        "site_key": "example",
        "allowed_domains": ["example.com"],
        "allowed_document_origins": ["https://example.com"],
        "user_agent": "web-listening-bot/1.1",
        "product_token": "web-listening-bot",
        "identity_id": "default",
        "transport": transport,
    }
    values[field] += "\r\nInjected: yes"
    with pytest.raises(SiteDiagnosticError):
        diagnose_site(**values)
    assert transport.requests == []


def test_site_key_control_characters_are_rejected_before_transport() -> None:
    transport = FakeTransport({})
    with pytest.raises(SiteDiagnosticError):
        diagnose_site(
            requested_url="https://example.com", site_key="example\npoison",
            allowed_domains=["example.com"], allowed_document_origins=["https://example.com"],
            user_agent="web-listening-bot/1.1", product_token="web-listening-bot",
            identity_id="default", transport=transport,
        )
    assert transport.requests == []


def test_explicit_zero_port_is_rejected_before_transport() -> None:
    transport = FakeTransport({})
    with pytest.raises(SiteDiagnosticError):
        diagnose_site(
            requested_url="https://example.com:0",
            site_key="example",
            allowed_domains=["example.com"],
            allowed_document_origins=["https://example.com"],
            user_agent="web-listening-bot/1.1",
            product_token="web-listening-bot",
            identity_id="default",
            transport=transport,
        )
    assert transport.requests == []


@pytest.mark.parametrize("field", ["allowed_domains", "allowed_document_origins"])
def test_duplicate_authority_inputs_are_rejected_before_transport(field: str) -> None:
    transport = FakeTransport({})
    values = {
        "requested_url": "https://example.com",
        "site_key": "example",
        "allowed_domains": ["example.com"],
        "allowed_document_origins": ["https://example.com"],
        "user_agent": "web-listening-bot/1.1",
        "product_token": "web-listening-bot",
        "identity_id": "default",
        "transport": transport,
    }
    values[field] = [*values[field], values[field][0]]
    with pytest.raises(SiteDiagnosticError):
        diagnose_site(**values)
    assert transport.requests == []


def test_cross_origin_page_is_recorded_but_not_seeded() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    mixed = b"<urlset><url><loc>https://example.com/a</loc></url><url><loc>https://other.test/b</loc></url></urlset>"
    only_cross = b"<urlset><url><loc>https://other.test/b</loc></url></urlset>"

    first = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, mixed, "application/xml")],
    }))
    second = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, only_cross, "application/xml")],
    }))

    assert (first.diagnostic_status, first.recommendation) == ("complete", "sitemap_seeded")
    assert first.rejected_urls[0].reason == "cross_origin_requires_diagnosis"
    assert (second.diagnostic_status, second.recommendation) == ("blocked", "operator_review")


def test_gzip_budget_and_extra_member_fail_closed() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    bomb = gzip.compress(b"<urlset>" + b" " * 4096 + b"</urlset>")
    extra = gzip.compress(b"<urlset />") + gzip.compress(b"<urlset />")

    for body in (bomb, extra):
        artifact = diagnose(
            FakeTransport({
                "https://example.com/robots.txt": [response(200, robots, "text/plain")],
                "https://example.com/map.xml": [response(200, body, "application/xml", Content_Encoding="gzip")],
            }),
            budgets=DiagnosticBudgets(sitemap_decoded_bytes_per_document=128),
        )
        assert (artifact.diagnostic_status, artifact.recommendation) == ("blocked", "operator_review")
        assert artifact.accepted_page_urls == []


def test_gzip_mime_signal_must_match_body_but_xml_content_encoding_gzip_is_valid() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    xml = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    conflict = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, xml, "application/gzip")],
    }))
    valid = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(
            200, gzip.compress(xml), "application/xml", Content_Encoding="gzip"
        )],
    }))
    assert conflict.decisive_priority == 1
    assert valid.accepted_page_urls == ["https://example.com/a"]


@pytest.mark.parametrize(
    "content_type",
    [
        "application/xml; charset=iso-8859-1",
        "application/xml; charset=utf-8; charset=iso-8859-1",
    ],
)
def test_sitemap_requires_unambiguous_utf8_charset(content_type: str) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, b"<urlset/>", content_type)],
    }))
    assert artifact.decisive_priority == 1
    assert artifact.attempts[-1].outcome == "sitemap_unsupported_mime"


def test_header_names_are_case_insensitive_and_duplicate_encoding_is_rejected() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    xml = b"<urlset/>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [RawHttpResponse(
            status=200,
            headers={
                "Content-Type": "application/xml",
                "Content-Encoding": "identity",
                "content-encoding": "gzip",
            },
            body_chunks=(xml,),
        )],
    }))
    assert artifact.decisive_priority == 1


def test_retry_is_immediate_bounded_and_counted() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(503), response(503), response(503)],
    })
    artifact = diagnose(transport)
    assert len(transport.requests) == 3
    assert artifact.budget_usage.http_requests == 3
    assert (artifact.diagnostic_status, artifact.recommendation) == ("retryable", "retry_diagnosis")


@pytest.mark.parametrize(
    (
        "responses",
        "budgets",
        "expected_priority",
        "expected_outcome",
        "expected_redirect_target",
    ),
    [
        ([response(503, b"retry")] * 3, None, 4, "transient", None),
        ([response(302, b"missing")], None, 6, "deterministic", None),
        (
            [response(302, b"forbidden", Location="https://maps.example.net/final.xml")],
            None,
            1,
            "safety",
            None,
        ),
        (
            [response(302, b"hop", Location="/final.xml")],
            DiagnosticBudgets(redirect_hops_per_document=0),
            6,
            "budget",
            "https://example.com/final.xml",
        ),
    ],
)
def test_terminal_sitemap_http_response_preserves_digest_evidence(
    responses: list[RawHttpResponse],
    budgets: DiagnosticBudgets | None,
    expected_priority: int,
    expected_outcome: str,
    expected_redirect_target: str | None,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": responses,
    })
    kwargs = {} if budgets is None else {"budgets": budgets}

    artifact = diagnose(transport, **kwargs)

    final_attempt = artifact.attempts[-1]
    evidence = artifact.sitemap_evidence[-1]
    assert artifact.decisive_priority == expected_priority
    assert evidence.outcome == expected_outcome
    assert evidence.final_url == final_attempt.final_url == "https://example.com/map.xml"
    assert evidence.document_sha256 == final_attempt.content_sha256
    assert evidence.document_sha256 is not None
    assert final_attempt.redirect_target_url == expected_redirect_target


def test_redirect_hop_cap_subdisposition_has_exact_cardinality(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/map.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
            "https://example.com/map.xml": [
                response(302, Location="/final.xml")
            ],
        }),
        budgets=DiagnosticBudgets(redirect_hops_per_document=0),
    )
    evidence = artifact.sitemap_evidence[0]
    rejection = next(
        item
        for item in artifact.rejected_urls
        if item.reason == "redirect_hop_budget_exhausted"
    )
    assert evidence.outcome == "budget"
    assert rejection.url == "https://example.com/final.xml"
    assert rejection.queue_ordinal == evidence.queue_ordinal
    assert rejection.parent_sha256 == evidence.parent_sha256
    assert rejection.entry_ordinal == evidence.parent_entry_ordinal

    removed = artifact.model_dump(mode="json")
    removed_rejection = next(
        item
        for item in removed["rejected_urls"]
        if item["reason"] == "redirect_hop_budget_exhausted"
    )
    removed["rejected_urls"].remove(removed_rejection)
    removed["budget_usage"]["http_requests"] -= 1
    removed_path = _write_rehashed_artifact(
        tmp_path,
        "redirect-hop-cap-removed.json",
        removed,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(removed_path)

    duplicated = artifact.model_dump(mode="json")
    duplicate = dict(next(
        item
        for item in duplicated["rejected_urls"]
        if item["reason"] == "redirect_hop_budget_exhausted"
    ))
    duplicated["budget_usage"]["http_requests"] += 1
    duplicate["request_slot_ordinal"] = duplicated["budget_usage"][
        "http_requests"
    ]
    duplicated["rejected_urls"].append(duplicate)
    duplicated_path = _write_rehashed_artifact(
        tmp_path,
        "redirect-hop-cap-duplicated.json",
        duplicated,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(duplicated_path)


@pytest.mark.parametrize(
    (
        "robots",
        "responses",
        "http_request_cap",
        "expected_queue_ordinal",
        "expected_pages",
        "expected_outcome",
    ),
    [
        (
            b"User-agent: *\nAllow: /\n"
            b"Sitemap: https://example.com/seed.xml\n"
            b"Sitemap: https://example.com/start.xml\n",
            {
                "https://example.com/seed.xml": response(
                    200,
                    b"<urlset><url><loc>https://example.com/seed</loc></url></urlset>",
                    "application/xml",
                ),
                "https://example.com/start.xml": response(
                    301,
                    Location="/final.xml",
                ),
            },
            3,
            2,
            ["https://example.com/seed"],
            ("partial", "sitemap_seeded", 2),
        ),
        (
            b"User-agent: *\nAllow: /\n"
            b"Sitemap: https://example.com/start.xml\n",
            {
                "https://example.com/start.xml": response(
                    301,
                    Location="/final.xml",
                ),
            },
            2,
            1,
            [],
            ("blocked", "operator_review", 6),
        ),
    ],
)
def test_redirect_target_request_cap_preserves_exact_failed_disposition(
    tmp_path: Path,
    robots: bytes,
    responses: dict[str, RawHttpResponse],
    http_request_cap: int,
    expected_queue_ordinal: int,
    expected_pages: list[str],
    expected_outcome: tuple[str, str, int],
) -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        **{url: [item] for url, item in responses.items()},
    })
    artifact = diagnose(
        transport,
        budgets=DiagnosticBudgets(http_requests=http_request_cap),
    )

    assert len(transport.requests) == http_request_cap
    assert artifact.budget_usage.http_requests == http_request_cap
    assert not any(
        url == "https://example.com/final.xml"
        for url, _, _ in transport.requests
    )
    assert artifact.accepted_page_urls == expected_pages
    assert (
        artifact.diagnostic_status,
        artifact.recommendation,
        artifact.decisive_priority,
    ) == expected_outcome

    final_attempt = next(
        item
        for item in reversed(artifact.attempts)
        if item.document_kind == "sitemap"
        and item.queue_ordinal == expected_queue_ordinal
    )
    evidence = next(
        item
        for item in artifact.sitemap_evidence
        if item.queue_ordinal == expected_queue_ordinal
    )
    assert final_attempt.outcome == "redirect"
    assert final_attempt.redirect_target_url == "https://example.com/final.xml"
    assert evidence.root_type == "failed"
    assert evidence.outcome in {"redirect_target_budget_exhausted", "budget"}
    assert evidence.final_url == final_attempt.final_url
    assert evidence.document_sha256 == final_attempt.content_sha256

    target_rejections = [
        item
        for item in artifact.rejected_urls
        if item.queue_ordinal == expected_queue_ordinal
        and item.url == final_attempt.redirect_target_url
        and item.reason in {
            "http_request_budget_exhausted",
            "prior_budget_stop",
        }
    ]
    assert len(target_rejections) == 1
    assert target_rejections[0].request_slot_ordinal is None

    path = write_site_diagnostic(
        artifact,
        tmp_path / f"redirect-target-cap-{http_request_cap}.json",
    )
    assert load_site_diagnostic(path) == artifact

    without_evidence = artifact.model_dump(mode="json")
    without_evidence["sitemap_evidence"] = [
        item
        for item in without_evidence["sitemap_evidence"]
        if item["queue_ordinal"] != expected_queue_ordinal
    ]
    without_evidence_path = _write_rehashed_artifact(
        tmp_path,
        f"redirect-target-cap-{http_request_cap}-without-evidence.json",
        without_evidence,
    )
    with pytest.raises(SiteDiagnosticError, match="attempt|evidence|disposition"):
        load_site_diagnostic(without_evidence_path)

    without_rejection = artifact.model_dump(mode="json")
    without_rejection["rejected_urls"] = [
        item
        for item in without_rejection["rejected_urls"]
        if not (
            item["queue_ordinal"] == expected_queue_ordinal
            and item["url"] == "https://example.com/final.xml"
        )
    ]
    without_rejection_path = _write_rehashed_artifact(
        tmp_path,
        f"redirect-target-cap-{http_request_cap}-without-rejection.json",
        without_rejection,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(without_rejection_path)


def test_retry_request_cap_preserves_post_attempt_failed_disposition() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/map.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://example.com/map.xml": [response(503)],
    })
    artifact = diagnose(
        transport,
        budgets=DiagnosticBudgets(http_requests=2),
    )

    assert len(transport.requests) == artifact.budget_usage.http_requests == 2
    attempt = artifact.attempts[-1]
    evidence = artifact.sitemap_evidence[-1]
    assert attempt.outcome == "transient_http"
    assert evidence.root_type == "failed"
    assert evidence.outcome == "budget"
    assert evidence.final_url == attempt.final_url
    assert evidence.document_sha256 == attempt.content_sha256
    rejection = artifact.rejected_urls[-1]
    assert (
        rejection.url,
        rejection.reason,
        rejection.request_slot_ordinal,
    ) == (
        attempt.requested_url,
        "http_request_budget_exhausted",
        None,
    )


def test_repeated_terminal_reason_is_deduplicated_without_losing_fifo_evidence() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/a.xml\n"
        b"Sitemap: https://example.com/b.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a.xml": [response(400, b"a")],
        "https://example.com/b.xml": [response(400, b"b")],
    }))

    assert artifact.decisive_priority == 6
    assert artifact.outcome_reasons == ["http:400"]
    assert [item.queue_ordinal for item in artifact.sitemap_evidence] == [1, 2]
    assert [item.document_sha256 for item in artifact.sitemap_evidence] == [
        artifact.attempts[1].content_sha256,
        artifact.attempts[2].content_sha256,
    ]


def test_http_request_budget_hard_stop_rejects_remaining_fifo_without_fake_evidence() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/a.xml\n"
        b"Sitemap: https://example.com/b.xml\n"
        b"Sitemap: https://example.com/c.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a.xml": [response(404)],
    })

    artifact = diagnose(transport, budgets=DiagnosticBudgets(http_requests=2))

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://example.com/a.xml",
    ]
    assert artifact.budget_usage.http_requests == 2
    assert [item.url for item in artifact.sitemap_evidence] == ["https://example.com/a.xml"]
    assert [(item.url, item.reason, item.request_slot_ordinal) for item in artifact.rejected_urls] == [
        ("https://example.com/b.xml", "http_request_budget_exhausted", None),
        ("https://example.com/c.xml", "prior_budget_stop", None),
    ]


def test_aux_robots_sitemap_directive_consumes_occurrence_budget_even_when_ignored() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    aux = b"User-agent: *\nAllow: /site.xml\nSitemap: https://maps.example.net/ignored.xml\n"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://maps.example.net/robots.txt": [response(200, aux, "text/plain")],
    })

    artifact = diagnose(
        transport,
        budgets=DiagnosticBudgets(sitemap_documents=1),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://maps.example.net/robots.txt",
    ]
    assert artifact.decisive_priority == 6
    assert artifact.truncation_reasons == ["sitemap_document_budget_exhausted"]
    assert artifact.budget_usage.sitemap_document_occurrences == 1
    assert artifact.origin_policy_evidence[-1].declared_sitemaps[0].url.endswith("/ignored.xml")


def _raising_chunks(prefix: bytes, error: Exception):
    yield prefix
    raise error


def test_partial_body_timeouts_retry_three_times_and_preserve_bytes() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            RawHttpResponse(200, {"Content-Type": "text/plain"}, _raising_chunks(b"abc", TimeoutError("late")))
            for _ in range(3)
        ],
    })
    artifact = diagnose(transport)
    assert len(transport.requests) == 3
    assert artifact.budget_usage.robots_wire_bytes == 9
    assert [item.wire_bytes for item in artifact.attempts] == [3, 3, 3]
    assert artifact.decisive_priority == 4


def test_corrupt_gzip_and_unknown_body_failure_become_priority_one_artifacts() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    corrupt = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, b"\x1f\x8bcorrupt", "application/xml", Content_Encoding="gzip")],
    }))
    unknown = diagnose(FakeTransport({
        "https://example.com/robots.txt": [RawHttpResponse(
            200, {"Content-Type": "text/plain"}, _raising_chunks(b"abc", RuntimeError("unknown"))
        )],
    }))
    assert corrupt.decisive_priority == 1
    assert unknown.decisive_priority == 1
    assert unknown.attempts[0].wire_bytes == 3


def test_incomplete_body_is_deterministic_and_not_retried() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [RawHttpResponse(
            200, {"Content-Type": "text/plain"},
            _raising_chunks(b"abc", http.client.IncompleteRead(b"tail")),
        )],
    })
    artifact = diagnose(transport)
    assert len(transport.requests) == 1
    assert artifact.decisive_priority == 6
    assert artifact.attempts[0].wire_bytes == 7


def test_incomplete_read_partial_bytes_are_recorded_when_nothing_was_yielded() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [RawHttpResponse(
            200, {"Content-Type": "text/plain"},
            _raising_chunks(b"", http.client.IncompleteRead(b"tail")),
        )],
    })
    artifact = diagnose(transport)
    assert artifact.attempts[0].wire_bytes == 4
    assert artifact.attempts[0].decoded_bytes == 4
    assert artifact.budget_usage.robots_wire_bytes == 4
    assert artifact.budget_usage.robots_decoded_bytes == 4


@pytest.mark.parametrize(
    ("terminal", "expected_outcome"),
    [
        (response(301), "redirect_missing_location"),
        (response(101), "final_informational"),
    ],
)
def test_terminal_protocol_failures_are_not_retried(
    terminal: RawHttpResponse, expected_outcome: str
) -> None:
    transport = FakeTransport({"https://example.com/robots.txt": [terminal]})

    artifact = diagnose(transport)

    assert len(transport.requests) == 1
    assert artifact.decisive_priority == 6
    assert artifact.attempts[0].outcome == expected_outcome
    assert artifact.attempts[0].redirect_target_url is None


def test_seed_plus_exhausted_transient_sitemap_is_partial() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/seed.xml\n"
        b"Sitemap: https://example.com/retry.xml\n"
    )
    seeded = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/seed.xml": [response(200, seeded, "application/xml")],
        "https://example.com/retry.xml": [
            TransportFailure("timeout", "timed out", retryable=True),
            TransportFailure("timeout", "timed out", retryable=True),
            TransportFailure("timeout", "timed out", retryable=True),
        ],
    })

    artifact = diagnose(transport)

    assert (artifact.diagnostic_status, artifact.recommendation) == (
        "partial",
        "sitemap_seeded",
    )
    assert artifact.accepted_page_urls == ["https://example.com/a"]
    assert artifact.decisive_priority == 2
    assert sum(url == "https://example.com/retry.xml" for url, _, _ in transport.requests) == 3


def test_digest_round_trip_tamper_detection_and_idempotent_write(tmp_path: Path) -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(404)],
        "https://example.com/sitemap.xml": [response(404)],
    }))
    path = tmp_path / "diagnostic.json"

    assert write_site_diagnostic(artifact, path) == path
    before = path.read_bytes()
    assert write_site_diagnostic(artifact, path) == path
    assert path.read_bytes() == before
    assert load_site_diagnostic(path).artifact_sha256 == artifact.artifact_sha256

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["site_key"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SiteDiagnosticError, match="digest"):
        load_site_diagnostic(path)


def test_contract_binds_declared_root_sitemap_url_and_source_line(tmp_path: Path) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/a.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a.xml": [response(404, b"missing")],
    }))
    payload = artifact.model_dump(mode="json")
    payload["attempts"][1]["requested_url"] = "https://example.com/evil.xml"
    payload["attempts"][1]["final_url"] = "https://example.com/evil.xml"
    payload["sitemap_evidence"][0]["url"] = "https://example.com/evil.xml"
    payload["sitemap_evidence"][0]["final_url"] = "https://example.com/evil.xml"
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / "root-lineage-tamper.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="root sitemap"):
        load_site_diagnostic(path)


def test_contract_binds_normalized_requested_url_to_requested_input(tmp_path: Path) -> None:
    requested = "HTTPS://ExAmple.com:443/a/../b/%7euser?q=1#fragment"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/sitemap.xml": [response(404)],
        }),
        requested_url=requested,
    )
    assert artifact.normalized_requested_url == "https://example.com/b/~user?q=1"

    payload = artifact.model_dump(mode="json")
    payload["normalized_requested_url"] = "https://example.com/other"
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / "normalized-request-tamper.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="normalized_requested_url"):
        load_site_diagnostic(path)


@pytest.mark.parametrize(
    "usage_field",
    ["http_requests", "sitemap_document_occurrences", "url_occurrences"],
)
def test_contract_recomputes_non_byte_budget_usage(tmp_path: Path, usage_field: str) -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(404)],
        "https://example.com/sitemap.xml": [response(404)],
    }))
    payload = artifact.model_dump(mode="json")
    payload["budget_usage"][usage_field] += 1
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / f"usage-{usage_field}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="budget usage"):
        load_site_diagnostic(path)


@pytest.mark.parametrize("declared", [False, True])
def test_contract_requires_every_root_occurrence_to_have_a_disposition(
    tmp_path: Path, declared: bool
) -> None:
    sitemap_url = "https://example.com/map.xml" if declared else "https://example.com/sitemap.xml"
    robots = (
        b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
        if declared
        else b""
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain") if declared else response(404)
        ],
        sitemap_url: [response(404)],
    }))
    payload = artifact.model_dump(mode="json")
    payload["attempts"] = payload["attempts"][:1]
    payload["sitemap_evidence"] = []
    payload["budget_usage"]["http_requests"] = 1
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / f"missing-root-{declared}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="root|fallback|disposition"):
        load_site_diagnostic(path)


@pytest.mark.parametrize(
    "mutation",
    ["missing_sitemap_attempt", "missing_root_occurrence", "missing_page_occurrence"],
)
def test_contract_rejects_reverse_lineage_deletions(tmp_path: Path, mutation: str) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    }))
    payload = artifact.model_dump(mode="json")
    if mutation == "missing_sitemap_attempt":
        payload["attempts"] = [item for item in payload["attempts"] if item["document_kind"] == "robots"]
        payload["budget_usage"]["http_requests"] = 1
        payload["budget_usage"]["sitemap_wire_bytes"] = 0
        payload["budget_usage"]["sitemap_decoded_bytes"] = 0
    else:
        source = "robots_sitemap" if mutation == "missing_root_occurrence" else "urlset"
        payload["counted_url_occurrences"] = [
            item for item in payload["counted_url_occurrences"] if item["source"] != source
        ]
        for index, item in enumerate(payload["counted_url_occurrences"], 1):
            item["occurrence_ordinal"] = index
        payload["budget_usage"]["url_occurrences"] -= 1
        if mutation == "missing_root_occurrence":
            payload["budget_usage"]["sitemap_document_occurrences"] -= 1
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / f"reverse-{mutation}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="attempt|occurrence|lineage|disposition"):
        load_site_diagnostic(path)


def test_contract_rejects_noncanonical_accepted_url_even_when_lists_agree(tmp_path: Path) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    urlset = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    }))
    payload = artifact.model_dump(mode="json")
    payload["accepted_page_urls"] = ["https://example.com/x/../a"]
    payload["accepted_page_evidence"][0]["url"] = "https://example.com/x/../a"
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / "noncanonical-page.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="canonical"):
        load_site_diagnostic(path)


def test_contract_requires_exactly_one_sitemap_queue_disposition(tmp_path: Path) -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(404)],
        "https://example.com/sitemap.xml": [response(404)],
    }))
    payload = artifact.model_dump(mode="json")
    payload["duplicate_urls"] = [{
        "url": "https://example.com/sitemap.xml",
        "raw_value": None,
        "reason": "duplicate_document_digest",
        "queue_ordinal": 1,
        "parent_sha256": payload["attempts"][0]["content_sha256"],
        "entry_ordinal": None,
        "request_slot_ordinal": None,
    }]
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / "double-disposition.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="disposition"):
        load_site_diagnostic(path)


def test_same_input_and_responses_are_semantically_deterministic() -> None:
    fixed = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    def run() -> SiteDiagnostic:
        return diagnose(
            FakeTransport({
                "https://example.com/robots.txt": [response(404)],
                "https://example.com/sitemap.xml": [response(404)],
            }),
            now=lambda: fixed,
        )

    first = run().model_dump(mode="json")
    second = run().model_dump(mode="json")
    for payload in (first, second):
        payload.pop("diagnostic_id")
        payload.pop("artifact_sha256")

    assert first == second


def test_budget_cannot_be_loosened() -> None:
    with pytest.raises(ValueError):
        DiagnosticBudgets(http_requests=65)


@pytest.mark.parametrize(
    ("robots_response", "expected"),
    [
        (response(200, b"User-agent: *\n", "text/html"), ("blocked", "operator_review")),
        (response(200, b"User-agent: *\n", "text/plain; charset=iso-8859-1"), ("blocked", "operator_review")),
        (response(200, b"\xff", "text/plain"), ("blocked", "operator_review")),
        (response(401), ("blocked", "operator_review")),
        (response(503), ("retryable", "retry_diagnosis")),
        (response(400), ("blocked", "operator_review")),
    ],
)
def test_robots_terminal_status_mime_and_utf8_matrix(
    robots_response: RawHttpResponse, expected: tuple[str, str]
) -> None:
    url = "https://example.com/robots.txt"
    queued = [robots_response]
    if robots_response.status == 503:
        queued = [robots_response, response(503), response(503)]
    artifact = diagnose(FakeTransport({url: queued}))
    assert (artifact.diagnostic_status, artifact.recommendation) == expected


@pytest.mark.parametrize("body", [b"<html><body>blocked</body></html>", b"<!DOCTYPE html><head></head>"])
def test_robots_html_sniff_is_priority_one(body: bytes) -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, body, "text/plain")],
    }))
    assert artifact.decisive_priority == 1


@pytest.mark.parametrize(
    "body",
    [
        b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html><html></html>',
        b" \n<!-- proxy banner --><!-- second -->\n<html><body>blocked</body></html>",
    ],
)
def test_robots_html_sniff_skips_markup_preamble_and_never_guesses_sitemap(body: bytes) -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, body, "text/plain")],
    })

    artifact = diagnose(transport)

    assert artifact.decisive_priority == 1
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert artifact.sitemap_evidence == []


def test_utf8_bom_robots_disallow_is_applied_without_sitemap_guess() -> None:
    body = b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, body, "text/plain")],
    })

    artifact = diagnose(transport)

    assert artifact.decisive_priority == 1
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert [(item.allow, item.pattern) for item in artifact.origin_policy_evidence[0].selected_rules] == [
        (False, "/")
    ]


def test_utf8_bom_robots_sitemap_and_rule_are_both_applied() -> None:
    body = (
        b"\xef\xbb\xbfUser-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/map.xml\n"
    )
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, body, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    }))

    assert artifact.decisive_priority == 3
    assert artifact.accepted_page_urls == ["https://example.com/page"]
    assert [(item.allow, item.pattern) for item in artifact.origin_policy_evidence[0].selected_rules] == [
        (True, "/")
    ]


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x7f"])
def test_robots_rule_controls_fail_closed_before_policy_evidence(control: str) -> None:
    body = f"User-agent: *\nDisallow: /a{control}b\n".encode()
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, body, "text/plain")],
    })

    artifact = diagnose(transport)

    encoded = json.dumps(artifact.model_dump(mode="json"), sort_keys=True)
    assert artifact.decisive_priority == 1
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert artifact.origin_policy_evidence == []
    assert control not in encoded
    assert json.dumps(control)[1:-1] not in encoded
    assert "/a" not in encoded


@pytest.mark.parametrize(
    "body",
    [
        b"<?foo bar?><html><body>blocked</body></html>",
        b"<!foo><html><body>blocked</body></html>",
        b"<!--unterminated <html",
    ],
)
def test_unknown_or_unterminated_robots_markup_fails_closed(body: bytes) -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, body, "text/plain")],
    })

    artifact = diagnose(transport)

    assert artifact.decisive_priority == 1
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert artifact.sitemap_evidence == []


def test_robots_conflicting_duplicate_charset_is_blocked_and_preserved() -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [RawHttpResponse(
            status=200,
            headers={
                "Content-Type": "text/plain; charset=iso-8859-1",
                "content-type": "text/plain; charset=utf-8",
            },
            body_chunks=(b"User-agent: *\n",),
        )],
    }))
    assert artifact.decisive_priority == 1
    assert "charset" in artifact.attempts[0].content_type_parameters


def test_seed_plus_empty_is_complete_but_seed_plus_terminal_failure_is_partial(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/a.xml\n"
        b"Sitemap: https://example.com/b.xml\n"
    )
    seeded = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"

    complete = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a.xml": [response(200, seeded, "application/xml")],
        "https://example.com/b.xml": [response(404)],
    }))
    partial = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a.xml": [response(200, seeded, "application/xml")],
        "https://example.com/b.xml": [response(415)],
    }))

    assert (complete.diagnostic_status, complete.recommendation) == ("complete", "sitemap_seeded")
    assert (partial.diagnostic_status, partial.recommendation) == ("partial", "sitemap_seeded")
    assert partial.decisive_priority == 2

    payload = partial.model_dump(mode="json")
    payload.update({
        "diagnostic_status": "blocked",
        "recommendation": "operator_review",
        "decisive_priority": 6,
        "next_action": "revise_inputs_or_boundaries_and_rediagnose",
    })
    path = _write_rehashed_artifact(
        tmp_path,
        "seed-plus-terminal-wrong-top.json",
        payload,
    )
    with pytest.raises(SiteDiagnosticError, match="priority|terminal|accepted"):
        load_site_diagnostic(path)


def test_duplicate_document_digest_is_benign_dedup_for_complete_seeded_plan(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/a.xml\n"
        b"Sitemap: https://example.com/b.xml\n"
    )
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a.xml": [response(200, urlset, "application/xml")],
        "https://example.com/b.xml": [response(200, urlset, "application/xml")],
    }))

    assert artifact.decisive_priority == 3
    assert artifact.accepted_page_urls == ["https://example.com/page"]
    assert [item.outcome for item in artifact.sitemap_evidence] == ["parsed"]
    assert [item.reason for item in artifact.duplicate_urls] == ["duplicate_document_digest"]
    sitemap_attempts = [
        item for item in artifact.attempts if item.document_kind == "sitemap"
    ]
    assert [item.requested_url for item in sitemap_attempts] == [
        "https://example.com/a.xml",
        "https://example.com/b.xml",
    ]
    assert [item.final_url for item in sitemap_attempts] == [
        "https://example.com/a.xml",
        "https://example.com/b.xml",
    ]
    assert load_site_diagnostic(
        write_site_diagnostic(artifact, tmp_path / "legal-digest-duplicate.json")
    ) == artifact


def test_seed_observed_before_later_safety_is_preserved_in_blocked_artifact(tmp_path: Path) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/seed.xml\n"
        b"Sitemap: https://example.com/unsafe.xml\n"
    )
    seeded = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/seed.xml": [response(200, seeded, "application/xml")],
        "https://example.com/unsafe.xml": [response(200, b"<html/>", "application/xml")],
    }))
    assert (artifact.diagnostic_status, artifact.recommendation, artifact.decisive_priority) == (
        "blocked", "operator_review", 1,
    )
    assert artifact.accepted_page_urls == ["https://example.com/page"]
    payload = artifact.model_dump(mode="json")
    payload["diagnostic_id"] = "diag-resigned-blocked-evidence"
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / "blocked-observation.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load_site_diagnostic(path).accepted_page_urls == ["https://example.com/page"]


def test_xml_syntax_is_terminal_but_doctype_and_wrong_root_are_safety_errors() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    bodies = [b"<urlset>", b'<!DOCTYPE x [<!ENTITY e "x">]><urlset/>', b"<html/>"]
    expected_priorities = [6, 1, 1]
    for body, priority in zip(bodies, expected_priorities, strict=True):
        artifact = diagnose(FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/map.xml": [response(200, body, "application/xml")],
        }))
        assert (artifact.diagnostic_status, artifact.recommendation) == ("blocked", "operator_review")
        assert artifact.decisive_priority == priority


@pytest.mark.parametrize(
    "body",
    [
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE urlset [<!ENTITY x "boom">]><urlset><url><loc>&x;</loc></url></urlset>'.encode("utf-16"),
        b'<!DOCTYPE urlset [<!ENTITY % ext SYSTEM "https://example.com/entity.dtd">%ext;]><urlset/>',
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><xi:include xmlns:xi="http://www.w3.org/2001/XInclude" href="https://example.com/x"/></url></urlset>',
        b"<URLSET><url><loc>https://example.com/a</loc></url></URLSET>",
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url xmlns=""><loc>https://example.com/a</loc></url></urlset>',
    ],
)
def test_xml_encoding_case_and_namespace_cannot_bypass_parser_policy(body: bytes) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, body, "application/xml")],
    }))
    assert (artifact.diagnostic_status, artifact.decisive_priority) == ("blocked", 1)


def test_sitemap_disguised_structure_is_blocked_but_safe_empty_and_metadata_work() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    disguised = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, b"<urlset><body>not a sitemap</body></urlset>", "application/xml")],
    }))
    metadata = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(
            200,
            b"<urlset><url><loc>https://example.com/a</loc><lastmod>2026-08-08</lastmod><changefreq>daily</changefreq><priority>0.5</priority></url></urlset>",
            "application/xml",
        )],
    }))
    assert disguised.decisive_priority == 1
    assert metadata.accepted_page_urls == ["https://example.com/a"]


@pytest.mark.parametrize(
    "body",
    [
        b"<urlset>not sitemap entries</urlset>",
        b"<urlset><url>junk<loc>https://example.com/a</loc></url></urlset>",
        b"<urlset><url><loc>https://example.com/a</loc>junk</url></urlset>",
        b"<urlset><url><lastmod>2026-08-08</lastmod>junk<loc>https://example.com/a</loc></url></urlset>",
    ],
)
def test_non_whitespace_xml_structure_text_or_tail_fails_closed(body: bytes) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, body, "application/xml")],
    }))
    assert artifact.decisive_priority == 1
    assert "unsafe_xml_structure" in artifact.outcome_reasons


@pytest.mark.parametrize(
    ("child", "depth"),
    [("", 3), ("https://example.com/child.xml", 0)],
)
def test_index_child_consumes_file_occurrence_before_validation_or_depth(
    child: str, depth: int
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = f"<sitemapindex><sitemap><loc>{child}</loc></sitemap></sitemapindex>".encode()
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/index.xml": [response(200, index, "application/xml")],
        }),
        budgets=DiagnosticBudgets(sitemap_depth=depth),
    )
    assert artifact.budget_usage.sitemap_document_occurrences == 2


def test_file_budget_overflow_stops_before_malformed_xml_tail() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    body = (
        b"<sitemapindex><sitemap><loc>https://example.com/child.xml</loc></sitemap>"
        b"<bad/></sitemapindex>"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/index.xml": [response(200, body, "application/xml")],
        }),
        budgets=DiagnosticBudgets(sitemap_documents=1),
    )
    assert artifact.decisive_priority == 6
    assert "unsafe_xml_structure" not in artifact.outcome_reasons
    assert artifact.budget_usage.http_requests == 3
    rejected = next(item for item in artifact.rejected_urls if item.reason == "sitemap_document_budget_exhausted")
    assert rejected.queue_ordinal == 2
    assert rejected.parent_sha256 == artifact.sitemap_evidence[0].document_sha256
    assert rejected.entry_ordinal == 1


def test_nested_loc_is_not_accepted_as_a_direct_sitemap_entry() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    nested = b"<urlset><url><metadata><loc>https://example.com/a</loc></metadata></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, nested, "application/xml")],
    }))
    assert artifact.accepted_page_urls == []


def test_deeply_nested_url_loc_is_not_accepted() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    nested = b"<urlset><metadata><url><loc>https://example.com/a</loc></url></metadata></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, nested, "application/xml")],
    }))
    assert artifact.accepted_page_urls == []


def test_sitemap_index_uses_deterministic_fifo() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    index = (
        b"<sitemapindex><sitemap><loc>https://example.com/three.xml</loc></sitemap>"
        b"<sitemap><loc>https://example.com/four.xml</loc></sitemap></sitemapindex>"
    )
    empty = b"<urlset/>"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [response(200, index, "application/xml")],
        "https://example.com/two.xml": [response(200, empty, "application/xml")],
        "https://example.com/three.xml": [response(200, empty, "application/xml")],
        "https://example.com/four.xml": [response(200, empty, "application/xml")],
    })
    artifact = diagnose(transport)
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://example.com/one.xml",
        "https://example.com/two.xml",
        "https://example.com/three.xml",
        "https://example.com/four.xml",
    ]
    assert [item.queue_ordinal for item in artifact.sitemap_evidence] == [1, 2]
    assert [item.queue_ordinal for item in artifact.duplicate_urls] == [3, 4]
    assert [item.reason for item in artifact.duplicate_urls] == [
        "duplicate_document_digest",
        "duplicate_document_digest",
    ]


def test_file_occurrence_budget_counts_before_scheduling() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [response(404)],
    })
    artifact = diagnose(transport, budgets=DiagnosticBudgets(sitemap_documents=1))
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt", "https://example.com/one.xml"
    ]
    assert artifact.budget_usage.http_requests == 3
    assert artifact.budget_usage.sitemap_document_occurrences == 1
    assert artifact.truncation_reasons == ["sitemap_document_budget_exhausted"]
    rejected = next(item for item in artifact.rejected_urls if item.reason == "sitemap_document_budget_exhausted")
    assert rejected.url == "https://example.com/two.xml"
    assert rejected.queue_ordinal == 2
    assert rejected.parent_sha256 == artifact.origin_policy_evidence[0].robots_sha256
    assert rejected.entry_ordinal == 4


def test_prior_safety_stop_drains_remaining_fifo_with_rejection_lineage() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://outside.example/map.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
    })
    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "outside.example"],
        allowed_document_origins=["https://example.com"],
    )
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert artifact.budget_usage.http_requests == 3
    assert [(item.queue_ordinal, item.reason) for item in artifact.rejected_urls] == [
        (1, "document_origin_not_approved"),
        (2, "prior_safety_stop"),
    ]
    assert all(item.parent_sha256 == artifact.origin_policy_evidence[0].robots_sha256 for item in artifact.rejected_urls)
    assert [item.entry_ordinal for item in artifact.rejected_urls] == [3, 4]


def test_rejected_index_child_consumes_request_and_has_queue_lineage() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = b"<sitemapindex><sitemap><loc>https://example.com/child.xml</loc></sitemap></sitemapindex>"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/index.xml": [response(200, index, "application/xml")],
        }),
        budgets=DiagnosticBudgets(sitemap_documents=1),
    )
    assert artifact.budget_usage.http_requests == 3
    rejected = next(item for item in artifact.rejected_urls if item.reason == "sitemap_document_budget_exhausted")
    assert rejected.queue_ordinal == 2
    assert rejected.parent_sha256 == artifact.sitemap_evidence[0].document_sha256
    assert rejected.entry_ordinal == 1


def test_url_occurrence_overflow_child_has_rejection_lineage_without_network() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = b"<sitemapindex><sitemap><loc>https://example.com/child.xml</loc></sitemap></sitemapindex>"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/index.xml": [response(200, index, "application/xml")],
    })
    artifact = diagnose(transport, budgets=DiagnosticBudgets(url_occurrences=1))
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt", "https://example.com/index.xml",
    ]
    assert artifact.budget_usage.http_requests == 3
    assert artifact.budget_usage.url_occurrences == 1
    rejected = next(item for item in artifact.rejected_urls if item.reason == "url_occurrence_budget_exhausted")
    assert rejected.url == "https://example.com/child.xml"
    assert rejected.queue_ordinal == 2
    assert rejected.parent_sha256 == artifact.sitemap_evidence[0].document_sha256
    assert rejected.entry_ordinal == 1


def test_page_url_overflow_does_not_consume_a_sitemap_request_slot() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    })
    artifact = diagnose(transport, budgets=DiagnosticBudgets(url_occurrences=1))
    assert artifact.budget_usage.http_requests == 2
    assert artifact.budget_usage.url_occurrences == 1
    assert artifact.accepted_page_urls == []
    assert not any(
        item.reason == "url_occurrence_budget_exhausted"
        for item in artifact.rejected_urls
    )


def test_control_character_remote_loc_is_redacted_and_fails_closed() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    body = b"<urlset><url><loc>https://example.com/a&#10;b</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, body, "application/xml")],
    }))
    assert artifact.decisive_priority == 1
    rejected = next(item for item in artifact.rejected_urls if item.reason == "malformed_sitemap_loc")
    assert rejected.url is None
    assert rejected.raw_value is not None and rejected.raw_value.startswith("sha256:")
    assert "\n" not in rejected.raw_value


def test_credential_bearing_remote_loc_is_hashed_and_never_emitted_verbatim() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    raw_location = "https://user:supersecret@example.com/a"
    body = f"<urlset><url><loc>{raw_location}</loc></url></urlset>".encode()
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, body, "application/xml")],
    }))

    rejected = next(item for item in artifact.rejected_urls if item.reason == "malformed_sitemap_loc")
    encoded = json.dumps(artifact.model_dump(mode="json"), sort_keys=True)
    assert artifact.decisive_priority == 1
    assert rejected.raw_value == "sha256:" + hashlib.sha256(raw_location.encode()).hexdigest()
    assert "supersecret" not in encoded


def test_scheme_relative_credential_loc_is_hashed_and_never_emitted_verbatim() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    raw_location = "//user:pass@example.com/secret"
    body = f"<urlset><url><loc>{raw_location}</loc></url></urlset>".encode()
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, body, "application/xml")],
    }))

    rejected = next(item for item in artifact.rejected_urls if item.reason == "malformed_sitemap_loc")
    encoded = json.dumps(artifact.model_dump(mode="json"), sort_keys=True)
    assert rejected.raw_value == "sha256:" + hashlib.sha256(raw_location.encode()).hexdigest()
    assert "user:pass" not in encoded


def test_url_overflow_stops_xml_parsing_before_malformed_tail() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    body = (
        b"<urlset><url><loc>https://example.com/page</loc></url>"
        b"<bad/></urlset>"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/map.xml": [response(200, body, "application/xml")],
        }),
        budgets=DiagnosticBudgets(url_occurrences=1),
    )
    assert artifact.decisive_priority == 6
    assert artifact.truncation_reasons == ["url_occurrence_budget_exhausted"]
    assert "unsafe_xml_structure" not in artifact.outcome_reasons


def test_redirect_is_exact_origin_gated_and_canonical_origin_never_changes() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(301, Location="https://example.com:444/robots.txt")],
    })
    artifact = diagnose(transport)
    assert len(transport.requests) == 1
    assert artifact.canonical_origin.effective_port == 443
    assert artifact.decisive_priority == 1
    assert artifact.attempts[0].outcome == "redirect_authority_failure"


def test_malformed_redirect_location_is_deterministic_artifact_not_exception() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(302, Location="http://[bad")],
    }))
    assert artifact.decisive_priority == 6
    assert artifact.attempts[-1].outcome == "redirect_malformed_location"
    assert artifact.attempts[-1].redirect_target_url is None
    assert artifact.sitemap_evidence[-1].outcome == "deterministic"


def test_malformed_relative_redirect_location_is_deterministic_not_authority_failure() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(302, b"bad-location", Location="%")],
    }))

    assert artifact.decisive_priority == 6
    assert artifact.attempts[-1].outcome == "redirect_malformed_location"
    assert artifact.attempts[-1].redirect_target_url is None
    assert artifact.sitemap_evidence[-1].outcome == "deterministic"
    assert artifact.sitemap_evidence[-1].document_sha256 == artifact.attempts[-1].content_sha256


def test_canonical_robots_cross_origin_redirect_chain_owns_canonical_policy() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(302, Location="https://maps.example.net/hop")],
        "https://maps.example.net/hop": [response(302, Location="/robots-final")],
        "https://maps.example.net/robots-final": [response(404)],
        "https://example.com/sitemap.xml": [response(404)],
    })
    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://maps.example.net/hop",
        "https://maps.example.net/robots-final",
        "https://example.com/sitemap.xml",
    ]
    assert artifact.decisive_priority == 5
    assert artifact.origin_policy_evidence[0].origin == artifact.canonical_origin
    robots_attempts = [
        item for item in artifact.attempts if item.document_kind == "robots"
    ]
    assert [item.redirect_target_url for item in robots_attempts] == [
        "https://maps.example.net/hop",
        "https://maps.example.net/robots-final",
        None,
    ]


def test_sitemap_redirect_target_is_robots_gated_before_request() -> None:
    robots = (
        b"User-agent: *\nDisallow: /\nAllow: /allowed\nAllow: /page\n"
        b"Sitemap: https://example.com/allowed\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/allowed": [response(302, Location="/blocked")],
    })
    artifact = diagnose(transport)
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt", "https://example.com/allowed",
    ]
    assert artifact.decisive_priority == 1
    assert artifact.sitemap_evidence[0].outcome == "redirect_disallowed_by_robots"


def test_sitemap_redirect_allowed_by_policy_continues_with_same_queue_lineage() -> None:
    robots = (
        b"User-agent: *\nDisallow: /\nAllow: /start\nAllow: /final\nAllow: /page\n"
        b"Sitemap: https://example.com/start\n"
    )
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/start": [response(302, Location="/final")],
        "https://example.com/final": [response(200, urlset, "application/xml")],
    })
    artifact = diagnose(transport)
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://example.com/start",
        "https://example.com/final",
    ]
    assert artifact.accepted_page_urls == ["https://example.com/page"]
    sitemap_attempts = [
        item for item in artifact.attempts if item.document_kind == "sitemap"
    ]
    assert {item.queue_ordinal for item in sitemap_attempts} == {1}
    assert [item.redirect_target_url for item in sitemap_attempts] == [
        "https://example.com/final",
        None,
    ]
    assert artifact.sitemap_evidence[0].final_url == "https://example.com/final"


def test_contract_rejects_tampered_redirect_chain_but_accepts_valid_retry_and_redirect(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nDisallow: /\nAllow: /start\nAllow: /final\nAllow: /page\n"
        b"Sitemap: https://example.com/start\n"
    )
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    redirected = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/start": [response(302, Location="/final")],
        "https://example.com/final": [response(200, urlset, "application/xml")],
    }))
    retried = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(503), response(503), response(503)],
    }))
    assert redirected.decisive_priority == 3
    assert retried.decisive_priority == 4

    payload = redirected.model_dump(mode="json")
    final_attempt = payload["attempts"][-1]
    final_attempt["redirect_chain"] = []
    final_attempt["redirect_ordinal"] = 0
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / "redirect-chain-tamper.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SiteDiagnosticError, match="redirect|chain"):
        load_site_diagnostic(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_redirect_target",
        "target_on_non_redirect",
        "wrong_next_hop_target",
        "noncanonical_target",
        "unapproved_target",
        "https_downgrade_target",
    ],
)
def test_contract_rejects_invalid_redirect_target_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    robots = (
        b"User-agent: *\nDisallow: /\n"
        b"Allow: /start\nAllow: /final\nAllow: /page\n"
        b"Sitemap: https://example.com/start\n"
    )
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://example.com/start": [response(302, Location="/final")],
        "https://example.com/final": [
            response(200, urlset, "application/xml")
        ],
    }))
    payload = artifact.model_dump(mode="json")
    redirect_attempt = payload["attempts"][1]
    final_attempt = payload["attempts"][2]

    if mutation == "missing_redirect_target":
        redirect_attempt["redirect_target_url"] = None
    elif mutation == "target_on_non_redirect":
        final_attempt["redirect_target_url"] = "https://example.com/other"
    elif mutation == "wrong_next_hop_target":
        redirect_attempt["redirect_target_url"] = "https://example.com/other"
    elif mutation == "noncanonical_target":
        redirect_attempt["redirect_target_url"] = "https://EXAMPLE.com/final"
    elif mutation == "unapproved_target":
        redirect_attempt["redirect_target_url"] = "https://maps.example.net/final"
    else:
        payload["allowed_document_origins"].append({
            "scheme": "http",
            "host": "example.com",
            "effective_port": 80,
        })
        redirect_attempt["redirect_target_url"] = "http://example.com/final"

    path = _write_rehashed_artifact(
        tmp_path,
        f"redirect-target-{mutation}.json",
        payload,
    )
    with pytest.raises(
        SiteDiagnosticError,
        match="redirect|target|canonical|approved|HTTPS|HTTP",
    ):
        load_site_diagnostic(path)


def test_index_safety_rejection_disposes_later_parsed_locations_without_network() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = (
        b"<sitemapindex>"
        b"<sitemap><loc>not-a-url</loc></sitemap>"
        b"<sitemap><loc>https://example.com/child.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/index.xml": [response(200, index, "application/xml")],
    })

    artifact = diagnose(transport)

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://example.com/index.xml",
    ]
    assert artifact.decisive_priority == 1
    assert artifact.budget_usage.sitemap_document_occurrences == 3
    assert artifact.budget_usage.url_occurrences == 3
    assert [(item.entry_ordinal, item.reason) for item in artifact.rejected_urls] == [
        (1, "malformed_sitemap_loc"),
        (2, "prior_safety_stop"),
    ]


def test_cross_origin_redirect_runs_aux_robots_before_target_request(
    tmp_path: Path,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/allowed\n"
    aux = b"User-agent: *\nDisallow: /\n"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/allowed": [response(302, Location="https://maps.example.net/blocked")],
        "https://maps.example.net/robots.txt": [response(200, aux, "text/plain")],
    })
    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )
    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt",
        "https://example.com/allowed",
        "https://maps.example.net/robots.txt",
    ]
    assert artifact.decisive_priority == 1
    assert artifact.sitemap_evidence[0].outcome == "redirect_disallowed_by_robots"
    rejection = next(
        item
        for item in artifact.rejected_urls
        if item.reason == "redirect_disallowed_by_robots"
    )
    assert rejection.url == "https://maps.example.net/blocked"

    payload = artifact.model_dump(mode="json")
    next(
        item
        for item in payload["rejected_urls"]
        if item["reason"] == "redirect_disallowed_by_robots"
    )["url"] = "https://maps.example.net/other"
    tampered = _write_rehashed_artifact(
        tmp_path,
        "redirect-disallowed-wrong-target.json",
        payload,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|target|disposition"):
        load_site_diagnostic(tampered)

    removed = artifact.model_dump(mode="json")
    removed_rejection = next(
        item
        for item in removed["rejected_urls"]
        if item["reason"] == "redirect_disallowed_by_robots"
    )
    removed["rejected_urls"].remove(removed_rejection)
    assert removed_rejection["request_slot_ordinal"] is not None
    removed["budget_usage"]["http_requests"] -= 1
    removed_path = _write_rehashed_artifact(
        tmp_path,
        "redirect-disallowed-removed.json",
        removed,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(removed_path)

    duplicated = artifact.model_dump(mode="json")
    duplicate = dict(next(
        item
        for item in duplicated["rejected_urls"]
        if item["reason"] == "redirect_disallowed_by_robots"
    ))
    duplicated["budget_usage"]["http_requests"] += 1
    duplicate["request_slot_ordinal"] = duplicated["budget_usage"][
        "http_requests"
    ]
    duplicated["rejected_urls"].append(duplicate)
    duplicated_path = _write_rehashed_artifact(
        tmp_path,
        "redirect-disallowed-duplicated.json",
        duplicated,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(duplicated_path)


@pytest.mark.parametrize(
    (
        "aux_responses",
        "expected_outcome",
        "expected_reason",
        "expected_status",
        "expected_recommendation",
        "expected_priority",
        "expected_aux_requests",
    ),
    [
        (
            [response(400)],
            "deterministic",
            "http:400",
            "blocked",
            "operator_review",
            6,
            1,
        ),
        (
            [response(503)] * 3,
            "transient",
            "http:503",
            "retryable",
            "retry_diagnosis",
            4,
            3,
        ),
    ],
)
def test_cross_origin_redirect_aux_terminal_preflight_is_structured(
    tmp_path: Path,
    aux_responses: list[RawHttpResponse],
    expected_outcome: str,
    expected_reason: str,
    expected_status: str,
    expected_recommendation: str,
    expected_priority: int,
    expected_aux_requests: int,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/start.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/start.xml": [
            response(301, Location="https://maps.example.net/final.xml")
        ],
        "https://maps.example.net/robots.txt": aux_responses,
    })

    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
    )

    requested_urls = [item[0] for item in transport.requests]
    assert requested_urls.count("https://maps.example.net/robots.txt") == (
        expected_aux_requests
    )
    assert "https://maps.example.net/final.xml" not in requested_urls
    sitemap_attempts = [
        item for item in artifact.attempts if item.document_kind == "sitemap"
    ]
    assert len(sitemap_attempts) == 1
    assert sitemap_attempts[0].outcome == "redirect"
    assert sitemap_attempts[0].redirect_target_url == (
        "https://maps.example.net/final.xml"
    )
    assert sitemap_attempts[0].queue_ordinal == 1
    evidence = artifact.sitemap_evidence[0]
    assert (
        evidence.url,
        evidence.queue_ordinal,
        evidence.parent_sha256,
        evidence.outcome,
    ) == (
        "https://example.com/start.xml",
        1,
        artifact.origin_policy_evidence[0].robots_sha256,
        expected_outcome,
    )
    rejection = next(
        item
        for item in artifact.rejected_urls
        if item.url == "https://maps.example.net/final.xml"
    )
    assert rejection.reason == f"redirect_policy_preflight_{expected_outcome}"
    assert (artifact.diagnostic_status, artifact.recommendation) == (
        expected_status,
        expected_recommendation,
    )
    assert artifact.decisive_priority == expected_priority
    assert artifact.outcome_reasons == [expected_reason]

    path = write_site_diagnostic(
        artifact,
        tmp_path / f"redirect-preflight-{expected_outcome}.json",
    )
    assert load_site_diagnostic(path) == artifact

    payload = artifact.model_dump(mode="json")
    tampered_outcome = (
        "transient" if expected_outcome == "deterministic" else "deterministic"
    )
    payload["sitemap_evidence"][0]["outcome"] = tampered_outcome
    next(
        item
        for item in payload["rejected_urls"]
        if item["url"] == "https://maps.example.net/final.xml"
    )["reason"] = f"redirect_policy_preflight_{tampered_outcome}"
    tampered = _write_rehashed_artifact(
        tmp_path,
        f"redirect-preflight-{expected_outcome}-tampered.json",
        payload,
    )
    with pytest.raises(SiteDiagnosticError, match="preflight|outcome|terminal"):
        load_site_diagnostic(tampered)

    wrong_target_payload = artifact.model_dump(mode="json")
    next(
        item
        for item in wrong_target_payload["rejected_urls"]
        if item["url"] == "https://maps.example.net/final.xml"
    )["url"] = "https://maps.example.net/completely-different.xml"
    wrong_target = _write_rehashed_artifact(
        tmp_path,
        f"redirect-preflight-{expected_outcome}-wrong-target.json",
        wrong_target_payload,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|target|preflight"):
        load_site_diagnostic(wrong_target)


def test_initial_aux_safety_preflight_cannot_be_resigned_as_budget(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps.example.net/site.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
            "https://maps.example.net/robots.txt": [response(403)],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
    )

    rejection = next(
        item
        for item in artifact.rejected_urls
        if item.url == "https://maps.example.net/site.xml"
    )
    assert rejection.reason == "sitemap_policy_preflight_safety"
    assert artifact.attempts[-1].outcome == "authority_http"

    payload = artifact.model_dump(mode="json")
    next(
        item
        for item in payload["rejected_urls"]
        if item["url"] == "https://maps.example.net/site.xml"
    )["reason"] = "sitemap_policy_preflight_budget"
    tampered = _write_rehashed_artifact(
        tmp_path,
        "initial-aux-safety-resigned-as-budget.json",
        payload,
    )

    with pytest.raises(SiteDiagnosticError, match="preflight|classification|robots"):
        load_site_diagnostic(tampered)


@pytest.mark.parametrize(
    ("aux_item", "expected_attempt_outcome"),
    [
        (
            TransportFailure(
                "certificate",
                "unsafe certificate",
                safety=True,
            ),
            "transport_certificate",
        ),
        (
            TransportFailure(
                "dns_address_policy",
                "unsafe DNS answer",
                safety=True,
            ),
            "transport_dns_address_policy",
        ),
        (RuntimeError("unknown transport failure"), "unclassified_transport"),
        (response(200, b"\xff", "text/plain"), "success"),
        (response(200, b"not robots", "application/json"),
         "robots_unsupported_mime_or_charset"),
        (
            response(301, Location="https://outside.invalid/robots.txt"),
            "redirect_authority_failure",
        ),
    ],
)
def test_aux_safety_preflight_binds_transport_and_parser_evidence(
    aux_item: RawHttpResponse | Exception,
    expected_attempt_outcome: str,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps.example.net/site.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
            "https://maps.example.net/robots.txt": [aux_item],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
    )

    assert artifact.attempts[-1].outcome == expected_attempt_outcome
    assert artifact.rejected_urls[0].reason == (
        "sitemap_policy_preflight_safety"
    )
    assert artifact.decisive_priority == 1


def test_initial_aux_request_cap_preflight_trigger_is_exact(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps.example.net/site.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
        budgets=DiagnosticBudgets(http_requests=1),
    )

    rejection = artifact.rejected_urls[0]
    assert (rejection.reason, rejection.trigger_reason) == (
        "prior_budget_stop",
        "sitemap_policy_preflight_budget",
    )
    assert artifact.truncation_reasons == ["http_request_budget_exhausted"]

    payload = artifact.model_dump(mode="json")
    payload["rejected_urls"][0]["trigger_reason"] = (
        "sitemap_policy_preflight_safety"
    )
    payload["diagnostic_status"] = "blocked"
    payload["recommendation"] = "operator_review"
    payload["decisive_priority"] = 1
    payload["next_action"] = "resolve_safety_or_authority_error"
    tampered = _write_rehashed_artifact(
        tmp_path,
        "initial-aux-request-budget-resigned-as-safety.json",
        payload,
    )
    with pytest.raises(SiteDiagnosticError, match="preflight|classification|robots"):
        load_site_diagnostic(tampered)


@pytest.mark.parametrize(
    "aggregate_budget_field",
    ["robots_wire_bytes_total", "robots_decoded_bytes_total"],
)
def test_initial_aux_aggregate_robots_cap_is_preflight_budget(
    aggregate_budget_field: str,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps.example.net/site.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
        budgets=DiagnosticBudgets(**{aggregate_budget_field: len(robots)}),
    )

    assert artifact.rejected_urls[0].reason == (
        "sitemap_policy_preflight_budget"
    )
    assert artifact.outcome_reasons == [
        "robots_aggregate_byte_budget_exhausted"
    ]
    assert all(
        attempt.requested_url != "https://maps.example.net/robots.txt"
        for attempt in artifact.attempts
    )


@pytest.mark.parametrize(
    (
        "aux_response",
        "redirected_aux_response",
        "redirect_reason",
        "evidence_outcome",
        "tampered_class",
    ),
    [
        (
            response(403),
            None,
            "redirect_policy_preflight_safety",
            "safety",
            "budget",
        ),
        (
            response(301, Location="/robots-final.txt"),
            response(301, Location="/robots-never.txt"),
            "redirect_policy_preflight_budget",
            "budget",
            "safety",
        ),
    ],
)
def test_redirect_aux_preflight_classification_cannot_be_resigned(
    tmp_path: Path,
    aux_response: RawHttpResponse,
    redirected_aux_response: RawHttpResponse | None,
    redirect_reason: str,
    evidence_outcome: str,
    tampered_class: str,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/start.xml\n"
    )
    responses = {
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://example.com/start.xml": [
            response(301, Location="https://maps.example.net/final.xml")
        ],
        "https://maps.example.net/robots.txt": [aux_response],
    }
    if redirected_aux_response is not None:
        responses["https://maps.example.net/robots-final.txt"] = [
            redirected_aux_response
        ]
    artifact = diagnose(
        FakeTransport(responses),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
        budgets=DiagnosticBudgets(redirect_hops_per_document=1),
    )

    evidence = artifact.sitemap_evidence[0]
    rejection = next(
        item
        for item in artifact.rejected_urls
        if item.url == "https://maps.example.net/final.xml"
    )
    assert evidence.outcome == evidence_outcome
    assert rejection.reason == redirect_reason

    payload = artifact.model_dump(mode="json")
    payload["sitemap_evidence"][0]["outcome"] = tampered_class
    next(
        item
        for item in payload["rejected_urls"]
        if item["url"] == "https://maps.example.net/final.xml"
    )["reason"] = f"redirect_policy_preflight_{tampered_class}"
    if tampered_class == "safety":
        payload["diagnostic_status"] = "blocked"
        payload["recommendation"] = "operator_review"
        payload["decisive_priority"] = 1
        payload["next_action"] = "resolve_safety_or_authority_error"
    tampered = _write_rehashed_artifact(
        tmp_path,
        f"redirect-aux-{evidence_outcome}-resigned-as-{tampered_class}.json",
        payload,
    )

    with pytest.raises(SiteDiagnosticError, match="preflight|classification|robots"):
        load_site_diagnostic(tampered)


@pytest.mark.parametrize(
    (
        "classification",
        "aux_responses",
        "redirected_aux_response",
        "budgets",
    ),
    [
        ("safety", [response(403)], None, DiagnosticBudgets()),
        ("deterministic", [response(400)], None, DiagnosticBudgets()),
        ("transient", [response(503)] * 3, None, DiagnosticBudgets()),
        (
            "budget",
            [response(301, Location="/robots-final.txt")],
            response(301, Location="/robots-never.txt"),
            DiagnosticBudgets(redirect_hops_per_document=1),
        ),
    ],
)
def test_structured_redirect_preflight_subdisposition_has_exact_cardinality(
    tmp_path: Path,
    classification: str,
    aux_responses: list[RawHttpResponse],
    redirected_aux_response: RawHttpResponse | None,
    budgets: DiagnosticBudgets,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/start.xml\n"
    )
    responses = {
        "https://example.com/robots.txt": [
            response(200, robots, "text/plain")
        ],
        "https://example.com/start.xml": [
            response(301, Location="https://maps.example.net/final.xml")
        ],
        "https://maps.example.net/robots.txt": aux_responses,
    }
    if redirected_aux_response is not None:
        responses["https://maps.example.net/robots-final.txt"] = [
            redirected_aux_response
        ]
    artifact = diagnose(
        FakeTransport(responses),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps.example.net",
        ],
        budgets=budgets,
    )
    expected_reason = f"redirect_policy_preflight_{classification}"
    assert artifact.sitemap_evidence[0].outcome == classification
    assert [
        item.reason
        for item in artifact.rejected_urls
        if item.reason == expected_reason
    ] == [expected_reason]

    removed = artifact.model_dump(mode="json")
    removed_rejection = next(
        item
        for item in removed["rejected_urls"]
        if item["reason"] == expected_reason
    )
    removed["rejected_urls"].remove(removed_rejection)
    assert removed_rejection["request_slot_ordinal"] is not None
    removed["budget_usage"]["http_requests"] -= 1
    removed_path = _write_rehashed_artifact(
        tmp_path,
        f"redirect-preflight-{classification}-removed.json",
        removed,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(removed_path)

    duplicated = artifact.model_dump(mode="json")
    duplicate = dict(next(
        item
        for item in duplicated["rejected_urls"]
        if item["reason"] == expected_reason
    ))
    duplicated["budget_usage"]["http_requests"] += 1
    duplicate["request_slot_ordinal"] = duplicated["budget_usage"][
        "http_requests"
    ]
    duplicated["rejected_urls"].append(duplicate)
    duplicated_path = _write_rehashed_artifact(
        tmp_path,
        f"redirect-preflight-{classification}-duplicated.json",
        duplicated,
    )
    with pytest.raises(SiteDiagnosticError, match="redirect|cardinality|disposition"):
        load_site_diagnostic(duplicated_path)


def test_cross_origin_redirect_preserves_declared_root_queue_lineage() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/start.xml\n"
    aux = b"User-agent: *\nAllow: /final.xml\n"
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/start.xml": [
                response(302, b"redirect", Location="https://maps.example.net/final.xml")
            ],
            "https://maps.example.net/robots.txt": [response(200, aux, "text/plain")],
            "https://maps.example.net/final.xml": [response(200, urlset, "application/xml")],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )

    evidence = artifact.sitemap_evidence[0]
    sitemap_attempts = [item for item in artifact.attempts if item.document_kind == "sitemap"]
    assert evidence.url == "https://example.com/start.xml"
    assert evidence.parent_entry_ordinal == artifact.robots_sitemap_directives[0].line_number
    assert evidence.final_url == "https://maps.example.net/final.xml"
    assert sitemap_attempts[0].requested_url == evidence.url
    assert sitemap_attempts[-1].final_url == evidence.final_url


def test_malformed_hosts_fail_closed_without_raw_validation_errors_or_network() -> None:
    transport = FakeTransport({})
    with pytest.raises(SiteDiagnosticError):
        diagnose_site(
            requested_url="https://example.com", site_key="example",
            allowed_domains=["example.com", "bad_domain"],
            allowed_document_origins=["https://example.com"],
            user_agent="web-listening-bot/1.1", product_token="web-listening-bot",
            identity_id="default", transport=transport,
        )
    assert transport.requests == []

    robots = b"User-agent: *\nAllow: /\nSitemap: https://bad_host/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
    }))
    assert artifact.decisive_priority == 1


def test_malformed_sitemap_directive_model_validation_becomes_safety_artifact() -> None:
    robots = "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml?x=\\\n".encode()
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
    })
    artifact = diagnose(transport)
    assert artifact.decisive_priority == 1
    assert [item[0] for item in transport.requests] == ["https://example.com/robots.txt"]
    assert any("malformed Sitemap directive" in reason for reason in artifact.outcome_reasons)


def test_safe_transport_rejects_mixed_dns_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[object] = []
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("10.0.0.1", 80)),
        ],
    )
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: connected.append(args))
    with pytest.raises(TransportFailure, match="mixed"):
        SafePinnedTransport().request("http://example.com/robots.txt", user_agent="bot", identity_sha256="a" * 64)
    assert connected == []


def test_safe_transport_rejects_peer_before_http_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def settimeout(self, value: float) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            return "93.184.216.35", 80

        def close(self) -> None:
            pass

    sent: list[object] = []
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))]
    )
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr("http.client.HTTPConnection.putrequest", lambda *args, **kwargs: sent.append(args))
    with pytest.raises(TransportFailure, match="peer"):
        SafePinnedTransport().request("http://example.com/robots.txt", user_agent="bot", identity_sha256="a" * 64)
    assert sent == []


def test_safe_transport_compares_equivalent_ipv6_peer_values(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Socket:
        def settimeout(self, value: float) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            events.append("peer")
            return "2001:4860:4860:0:0:0:0:8888", 80

        def close(self) -> None:
            pass

    class Response:
        status = 404
        def getheaders(self):
            return []
        def read(self, size: int) -> bytes:
            return b""
        def close(self) -> None:
            pass

    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = None
        def putrequest(self, *args, **kwargs):
            events.append("request")
        def putheader(self, *args, **kwargs):
            pass
        def endheaders(self):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:4860:4860::8888", 80, 0, 0))],
    )
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr("http.client.HTTPConnection", Connection)
    result = SafePinnedTransport().request(
        "http://[2001:4860:4860::8888]/robots.txt", user_agent="bot", identity_sha256="a" * 64
    )
    list(result.body_chunks)
    assert events == ["peer", "request"]


def test_certificate_failure_sends_no_http_application_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def settimeout(self, value: float) -> None: pass
        def close(self) -> None: pass
    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            raise ssl.SSLCertVerificationError(1, "certificate mismatch")

    sent: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr("http.client.HTTPSConnection.putrequest", lambda *args, **kwargs: sent.append("putrequest"))
    monkeypatch.setattr("http.client.HTTPSConnection.endheaders", lambda *args, **kwargs: sent.append("endheaders"))
    with pytest.raises(TransportFailure) as caught:
        SafePinnedTransport().request("https://example.com/robots.txt", user_agent="bot", identity_sha256="a" * 64)
    assert caught.value.safety is True
    assert sent == []


def _install_https_getresponse_tls_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    events: list[str] = []

    class Socket:
        def settimeout(self, value: float) -> None:
            pass

        def getpeername(self) -> tuple[str, int]:
            return "93.184.216.34", 443

        def close(self) -> None:
            events.append("socket_close")

    class Context:
        def wrap_socket(self, raw: Socket, *, server_hostname: str) -> Socket:
            assert server_hostname == "example.com"
            return raw

    class Connection:
        def __init__(self, *args: object, **kwargs: object):
            self.sock = None

        def putrequest(self, *args: object, **kwargs: object) -> None:
            events.append("putrequest")

        def putheader(self, *args: object, **kwargs: object) -> None:
            pass

        def endheaders(self) -> None:
            events.append("endheaders")

        def getresponse(self) -> None:
            events.append("getresponse")
            raise ssl.SSLError("unknown TLS record failure")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    return events


def test_safe_transport_classifies_getresponse_ssl_error_as_tls_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _install_https_getresponse_tls_failure(monkeypatch)

    with pytest.raises(TransportFailure) as caught:
        SafePinnedTransport().request(
            "https://example.com/robots.txt",
            user_agent="bot",
            identity_sha256="a" * 64,
        )

    assert caught.value.kind == "tls_policy"
    assert caught.value.retryable is False
    assert caught.value.safety is True
    assert events.count("getresponse") == 1


def test_getresponse_ssl_error_blocks_diagnosis_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _install_https_getresponse_tls_failure(monkeypatch)

    artifact = diagnose(
        SafePinnedTransport(),
        requested_url="https://example.com",
    )

    assert events.count("getresponse") == 1
    assert artifact.budget_usage.http_requests == 1
    assert len(artifact.attempts) == 1
    assert artifact.attempts[0].outcome == "transport_tls_policy"
    assert artifact.decisive_priority == 1
    assert (artifact.diagnostic_status, artifact.recommendation) == (
        "blocked",
        "operator_review",
    )


def test_https_sni_uses_normalized_unbracketed_host_without_port(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, str] | str] = []
    class Socket:
        def settimeout(self, value: float) -> None: pass
        def getpeername(self): return ("2001:4860:4860:0:0:0:0:8888", 444)
        def close(self) -> None: pass
    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            events.append(("sni", server_hostname))
            return raw
    class Response:
        status = 404
        def getheaders(self): return []
        def read(self, size): return b""
        def close(self): pass
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = None
        def putrequest(self, *args, **kwargs): events.append("request")
        def putheader(self, *args, **kwargs): pass
        def endheaders(self): pass
        def getresponse(self): return Response()
        def close(self): pass

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET6, 1, 6, "", ("2001:4860:4860::8888", 444, 0, 0))])
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    result = SafePinnedTransport().request(
        "https://[2001:4860:4860::8888]:444/robots.txt", user_agent="bot", identity_sha256="a" * 64
    )
    list(result.body_chunks)
    assert events == [("sni", "2001:4860:4860::8888"), "request"]


def test_retry_re_resolves_and_rebinding_is_rejected_before_second_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    resolutions = iter([
        [(2, 1, 6, "", ("93.184.216.34", 443))],
        [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))],
    ])
    connects: list[object] = []
    class Socket:
        def settimeout(self, value): pass
        def getpeername(self): return ("93.184.216.34", 443)
        def close(self): pass
    class Context:
        def wrap_socket(self, raw, *, server_hostname): return raw
    class Response:
        status = 503
        def getheaders(self): return []
        def read(self, size): return b""
        def close(self): pass
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = None
        def putrequest(self, *args, **kwargs): pass
        def putheader(self, *args, **kwargs): pass
        def endheaders(self): pass
        def getresponse(self): return Response()
        def close(self): pass

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: next(resolutions))
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: connects.append(args) or Socket())
    monkeypatch.setattr(ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    artifact = diagnose_site(
        requested_url="https://example.com", site_key="example",
        allowed_domains=["example.com"], allowed_document_origins=["https://example.com"],
        user_agent="web-listening-bot/1.1", product_token="web-listening-bot",
        identity_id="default", transport=SafePinnedTransport(),
    )
    assert len(connects) == 1
    assert artifact.decisive_priority == 1


def test_unclassified_three_digit_status_is_preserved_as_priority_one() -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(700)],
    }))
    assert artifact.decisive_priority == 1
    assert artifact.attempts[0].http_status == 700
    assert artifact.attempts[0].outcome == "unclassified_http"


def test_malformed_status_is_deterministic_and_not_retried() -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            TransportFailure("malformed_status", "bad status line", deterministic=True)
        ],
    })
    artifact = diagnose(transport)
    assert len(transport.requests) == 1
    assert artifact.decisive_priority == 6
    assert artifact.attempts[0].outcome == "transport_malformed_status"


def test_safe_transport_classifies_bad_status_line_as_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def settimeout(self, value): pass
        def getpeername(self): return ("93.184.216.34", 80)
        def close(self): pass
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = None
        def putrequest(self, *args, **kwargs): pass
        def putheader(self, *args, **kwargs): pass
        def endheaders(self): pass
        def getresponse(self): raise http.client.BadStatusLine("NOT HTTP")
        def close(self): pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))])
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(TransportFailure) as caught:
        SafePinnedTransport().request("http://example.com/robots.txt", user_agent="bot", identity_sha256="a" * 64)
    assert caught.value.deterministic is True


def test_safe_transport_classifies_unknown_http_exception_as_safety(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def settimeout(self, value): pass
        def getpeername(self): return ("93.184.216.34", 80)
        def close(self): pass
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = None
        def putrequest(self, *args, **kwargs): pass
        def putheader(self, *args, **kwargs): pass
        def endheaders(self): pass
        def getresponse(self): raise http.client.HTTPException("unknown protocol failure")
        def close(self): pass
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 80))])
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    with pytest.raises(TransportFailure) as caught:
        SafePinnedTransport().request("http://example.com/robots.txt", user_agent="bot", identity_sha256="a" * 64)
    assert caught.value.safety is True
    assert caught.value.retryable is False


def test_canonical_host_authority_variants() -> None:
    assert canonical_host_header(NormalizedOrigin(scheme="https", host="example.com", effective_port=443)) == "example.com"
    assert canonical_host_header(NormalizedOrigin(scheme="https", host="example.com", effective_port=444)) == "example.com:444"
    assert canonical_host_header(NormalizedOrigin(scheme="http", host="2001:4860:4860::8888", effective_port=80)) == "[2001:4860:4860::8888]"


def test_committed_fixture_validates_schema_and_digest() -> None:
    fixture = Path("docs/testing/fixtures/site-diagnostic-v1.sample.json")
    artifact = SiteDiagnostic.model_validate_json(fixture.read_text(encoding="utf-8"))
    artifact.verify_artifact_sha256()
    schema = SiteDiagnostic.model_json_schema()
    assert schema["title"] == "SiteDiagnostic"
    attempt_schema = schema["$defs"]["DiagnosticAttempt"]
    assert "redirect_target_url" in attempt_schema["properties"]


def test_accepted_page_evidence_preserves_source_lineage() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    xml = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, xml, "application/xml")],
    }))
    accepted = artifact.accepted_page_evidence[0]
    assert accepted.url == artifact.accepted_page_urls[0]
    assert accepted.parent_sha256 == artifact.sitemap_evidence[0].document_sha256
    assert accepted.entry_ordinal == 1
    assert accepted.source_queue_ordinal == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "credentials", "noncanonical_url", "naive_time", "negative_usage",
        "token_absent", "missing_attempt", "usage_mismatch", "duplicate_seed",
        "bad_robots_status", "tampered_policy", "orphan_attempt_queue",
        "root_parent_fork", "seeded_without_evidence", "accepted_bad_lineage",
        "fallback_without_policy", "sitemap_digest_fork", "robots_digest_fork",
        "robots_200_completed_empty", "sitemap_200_empty_evidence",
        "empty_404_success", "fallback_wrong_path", "fallback_disallowed_policy",
        "accepted_disallowed_policy", "accepted_priority_six",
        "fallback_terminal_downgrade", "complete_fallback_with_truncation",
        "budget_below_usage", "bad_next_action", "absent_declared_sitemap",
        "bogus_outcome_reason",
    ],
)
def test_loader_rejects_malicious_rehashed_artifacts(tmp_path: Path, mutation: str) -> None:
    from web_listening.contracts.site_diagnostic import canonical_sha256

    payload = json.loads(Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text())
    if mutation == "credentials":
        payload["attempts"][0]["requested_url"] = "https://user:pass@example.com/robots.txt"
    elif mutation == "noncanonical_url":
        payload["sitemap_evidence"][0]["url"] = "https://EXAMPLE.com:443/sitemap.xml#fragment"
    elif mutation == "naive_time":
        payload["started_at"] = "2026-08-08T12:00:00"
    elif mutation == "negative_usage":
        payload["budget_usage"]["http_requests"] = -1
    elif mutation == "token_absent":
        payload["identity"]["product_token"] = "absent"
        identity_digest = canonical_sha256({
            "identity_id": payload["identity"]["identity_id"],
            "product_token": "absent",
            "user_agent": payload["identity"]["user_agent"],
        })
        payload["identity"]["identity_sha256"] = identity_digest
        for attempt in payload["attempts"]:
            attempt["product_token"] = "absent"
            attempt["identity_sha256"] = identity_digest
        for evidence in payload["origin_policy_evidence"]:
            evidence["identity_sha256"] = identity_digest
            policy_digest = canonical_sha256({
                "origin": evidence["origin"], "robots_sha256": evidence["robots_sha256"],
                "selected_rules": evidence.get("selected_rules", []), "identity_sha256": identity_digest,
            })
            evidence["policy_sha256"] = policy_digest
            evidence["policy_id"] = f"robots-policy-{policy_digest[:16]}"
    elif mutation == "missing_attempt":
        payload["attempts"] = []
    elif mutation == "usage_mismatch":
        payload["budget_usage"]["robots_wire_bytes"] += 1
    elif mutation == "duplicate_seed":
        payload["accepted_page_urls"] = ["https://example.com/a", "https://example.com/a"]
    elif mutation == "bad_robots_status":
        payload["origin_policy_evidence"][0]["robots_status"] = "unknown"
    elif mutation == "orphan_attempt_queue":
        payload["attempts"][1]["queue_ordinal"] = 99
    elif mutation == "root_parent_fork":
        payload["attempts"][1]["parent_sha256"] = "f" * 64
        payload["sitemap_evidence"][0]["parent_sha256"] = "f" * 64
    elif mutation == "seeded_without_evidence":
        payload["diagnostic_status"] = "complete"
        payload["recommendation"] = "sitemap_seeded"
        payload["decisive_priority"] = 3
    elif mutation == "accepted_bad_lineage":
        payload["diagnostic_status"] = "complete"
        payload["recommendation"] = "sitemap_seeded"
        payload["decisive_priority"] = 3
        payload["accepted_page_urls"] = ["https://example.com/a"]
        payload["accepted_page_evidence"] = [{
            "url": "https://example.com/a",
            "parent_sha256": payload["sitemap_evidence"][0]["document_sha256"],
            "entry_ordinal": 1,
            "source_queue_ordinal": 1,
        }]
    elif mutation == "fallback_without_policy":
        payload["origin_policy_evidence"] = []
    elif mutation == "sitemap_digest_fork":
        payload["sitemap_evidence"][0]["document_sha256"] = "f" * 64
    elif mutation == "robots_digest_fork":
        evidence = payload["origin_policy_evidence"][0]
        evidence["robots_sha256"] = "f" * 64
        policy_digest = canonical_sha256({
            "origin": evidence["origin"],
            "robots_sha256": evidence["robots_sha256"],
            "selected_rules": evidence.get("selected_rules", []),
            "identity_sha256": evidence["identity_sha256"],
        })
        evidence["policy_sha256"] = policy_digest
        evidence["policy_id"] = f"robots-policy-{policy_digest[:16]}"
        payload["attempts"][1]["parent_sha256"] = "f" * 64
        payload["sitemap_evidence"][0]["parent_sha256"] = "f" * 64
    elif mutation == "robots_200_completed_empty":
        payload["attempts"][0]["http_status"] = 200
    elif mutation == "sitemap_200_empty_evidence":
        payload["attempts"][1]["http_status"] = 200
        payload["attempts"][1]["outcome"] = "success"
    elif mutation == "empty_404_success":
        payload["attempts"][1]["outcome"] = "success"
    elif mutation == "fallback_wrong_path":
        payload["attempts"][1]["requested_url"] = "https://example.com/other.xml"
        payload["attempts"][1]["final_url"] = "https://example.com/other.xml"
        payload["sitemap_evidence"][0]["url"] = "https://example.com/other.xml"
        payload["sitemap_evidence"][0]["final_url"] = "https://example.com/other.xml"
    elif mutation == "fallback_disallowed_policy":
        evidence = payload["origin_policy_evidence"][0]
        evidence["robots_status"] = "available"
        evidence["selected_rules"] = [{"allow": False, "pattern": "/", "line_number": 1}]
        payload["attempts"][0]["http_status"] = 200
        payload["attempts"][0]["outcome"] = "success"
        policy_digest = canonical_sha256({
            "origin": evidence["origin"],
            "robots_sha256": evidence["robots_sha256"],
            "selected_rules": evidence["selected_rules"],
            "identity_sha256": evidence["identity_sha256"],
        })
        evidence["policy_sha256"] = policy_digest
        evidence["policy_id"] = f"robots-policy-{policy_digest[:16]}"
    elif mutation == "accepted_disallowed_policy":
        payload["diagnostic_status"] = "complete"
        payload["recommendation"] = "sitemap_seeded"
        payload["decisive_priority"] = 3
        payload["accepted_page_urls"] = ["https://example.com/a"]
        payload["accepted_page_evidence"] = [{
            "url": "https://example.com/a",
            "parent_sha256": payload["sitemap_evidence"][0]["document_sha256"],
            "entry_ordinal": 1,
            "source_queue_ordinal": 1,
        }]
        payload["attempts"][1]["http_status"] = 200
        payload["attempts"][1]["outcome"] = "success"
        payload["sitemap_evidence"][0]["root_type"] = "urlset"
        payload["sitemap_evidence"][0]["outcome"] = "parsed"
        evidence = payload["origin_policy_evidence"][0]
        evidence["robots_status"] = "available"
        evidence["selected_rules"] = [
            {"allow": True, "pattern": "/sitemap.xml", "line_number": 1},
            {"allow": False, "pattern": "/a", "line_number": 2},
        ]
        payload["attempts"][0]["http_status"] = 200
        payload["attempts"][0]["outcome"] = "success"
        policy_digest = canonical_sha256({
            "origin": evidence["origin"],
            "robots_sha256": evidence["robots_sha256"],
            "selected_rules": evidence["selected_rules"],
            "identity_sha256": evidence["identity_sha256"],
        })
        evidence["policy_sha256"] = policy_digest
        evidence["policy_id"] = f"robots-policy-{policy_digest[:16]}"
    elif mutation == "accepted_priority_six":
        payload["accepted_page_urls"] = ["https://example.com/a"]
        payload["accepted_page_evidence"] = [{
            "url": "https://example.com/a",
            "parent_sha256": payload["sitemap_evidence"][0]["document_sha256"],
            "entry_ordinal": 1,
            "source_queue_ordinal": 1,
        }]
        payload["diagnostic_status"] = "blocked"
        payload["recommendation"] = "operator_review"
        payload["decisive_priority"] = 6
        payload["next_action"] = "revise_inputs_or_boundaries_and_rediagnose"
    elif mutation == "fallback_terminal_downgrade":
        payload["attempts"][1]["http_status"] = 400
        payload["attempts"][1]["outcome"] = "terminal_http"
        payload["sitemap_evidence"][0]["root_type"] = "failed"
        payload["sitemap_evidence"][0]["outcome"] = "deterministic"
    elif mutation == "complete_fallback_with_truncation":
        payload["truncation_reasons"] = ["sitemap_document_budget_exhausted"]
        payload["outcome_reasons"] = ["sitemap_document_budget_exhausted"]
    elif mutation == "budget_below_usage":
        payload["attempts"][0]["wire_bytes"] = 2
        payload["budget_usage"]["robots_wire_bytes"] = 2
        payload["budgets"]["robots_wire_bytes_per_attempt"] = 1
        payload["budgets"]["robots_wire_bytes_total"] = 1
    elif mutation == "bad_next_action":
        payload["next_action"] = "retry_diagnosis"
    elif mutation == "absent_declared_sitemap":
        directive = {"url": "https://example.com/other.xml", "line_number": 1}
        payload["robots_sitemap_directives"] = [directive]
        payload["origin_policy_evidence"][0]["declared_sitemaps"] = [directive]
    elif mutation == "bogus_outcome_reason":
        payload["outcome_reasons"] = ["xml_syntax_error"]
    else:
        payload["origin_policy_evidence"][0]["selected_rules"] = [
            {"allow": False, "pattern": "/tampered", "line_number": 1}
        ]
    payload["artifact_sha256"] = canonical_sha256({k: v for k, v in payload.items() if k != "artifact_sha256"})
    path = tmp_path / "malicious.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SiteDiagnosticError):
        load_site_diagnostic(path)


def test_loader_rejects_duplicate_json_keys_even_with_valid_last_value(tmp_path: Path) -> None:
    original = Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text()
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"site-diagnostic.v1",' + original.lstrip()[1:], encoding="utf-8")
    with pytest.raises(SiteDiagnosticError, match="duplicate"):
        load_site_diagnostic(path)


def test_url_normalization_preserves_query_reserved_path_and_requested_input() -> None:
    normalized, origin = normalize_http_url(
        "HTTPS://ExAmple.com:443/a/../b/%7euser%2fdoc?q=A#fragment"
    )
    assert normalized == "https://example.com/b/~user%2Fdoc?q=A"
    assert origin == NormalizedOrigin(scheme="https", host="example.com", effective_port=443)

    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(404)],
            "https://example.com/sitemap.xml": [response(404)],
        }),
        requested_url="HTTPS://ExAmple.com:443/a/../b?q=A#fragment",
    )
    assert artifact.requested_url == "HTTPS://ExAmple.com:443/a/../b?q=A#fragment"
    assert artifact.normalized_requested_url == "https://example.com/b?q=A"


def _write_rehashed_artifact(tmp_path: Path, name: str, payload: dict[str, object]) -> Path:
    payload["artifact_sha256"] = canonical_sha256({
        key: value for key, value in payload.items() if key != "artifact_sha256"
    })
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_contract_requires_aux_robots_to_complete_before_cross_origin_sitemap(
    tmp_path: Path,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    aux_robots = b"User-agent: *\nAllow: /site.xml\n"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://maps.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
            "https://maps.example.net/site.xml": [response(404)],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )
    payload = artifact.model_dump(mode="json")
    payload["attempts"] = [
        payload["attempts"][0], payload["attempts"][2], payload["attempts"][1]
    ]
    for ordinal, attempt in enumerate(payload["attempts"], 1):
        attempt["attempt_ordinal"] = ordinal
        attempt["request_slot_ordinal"] = ordinal

    path = _write_rehashed_artifact(tmp_path, "preflight-after-sitemap.json", payload)
    with pytest.raises(SiteDiagnosticError, match="robots|preflight|policy"):
        load_site_diagnostic(path)


def test_body_remote_disconnect_is_retried_exactly_three_times() -> None:
    def disconnected_body() -> RawHttpResponse:
        def chunks():
            yield b"partial"
            raise http.client.RemoteDisconnected("closed")

        return RawHttpResponse(200, {"Content-Type": "text/plain"}, chunks())

    transport = FakeTransport({
        "https://example.com/robots.txt": [disconnected_body() for _ in range(3)],
    })
    artifact = diagnose(transport)

    assert len(transport.requests) == 3
    assert artifact.decisive_priority == 4
    assert [item.outcome for item in artifact.attempts] == ["body_remote_disconnected"] * 3


def test_contract_rejects_retry_after_certificate_failure(tmp_path: Path) -> None:
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [
            TransportFailure("connect", "timeout", retryable=True),
            response(404),
        ],
        "https://example.com/sitemap.xml": [response(404)],
    }))
    payload = artifact.model_dump(mode="json")
    payload["attempts"][0]["outcome"] = "transport_certificate"

    path = _write_rehashed_artifact(tmp_path, "certificate-retry.json", payload)
    with pytest.raises(SiteDiagnosticError, match="retry|transport|safety"):
        load_site_diagnostic(path)


@pytest.mark.parametrize("kind", ["certificate", "peer_mismatch", "dns_address_policy"])
def test_safety_transport_failures_are_priority_one_and_never_retried(kind: str) -> None:
    transport = FakeTransport({
        "https://example.com/robots.txt": [
            TransportFailure(kind, "unsafe transport", retryable=True, safety=True),
            response(404),
        ],
    })
    artifact = diagnose(transport)

    assert len(transport.requests) == 1
    assert artifact.decisive_priority == 1
    assert artifact.attempts[0].outcome == f"transport_{kind}"


def test_403_stops_before_reading_body_and_is_never_retried() -> None:
    body_reads = 0

    def forbidden() -> RawHttpResponse:
        def chunks():
            nonlocal body_reads
            body_reads += 1
            raise TimeoutError("must not read")
            yield b""  # pragma: no cover

        return RawHttpResponse(403, {"Content-Type": "text/plain"}, chunks())

    transport = FakeTransport({
        "https://example.com/robots.txt": [forbidden(), forbidden(), forbidden()],
    })
    artifact = diagnose(transport)

    assert len(transport.requests) == 1
    assert body_reads == 0
    assert artifact.decisive_priority == 1
    assert (artifact.diagnostic_status, artifact.recommendation) == ("blocked", "operator_review")
    assert artifact.attempts[0].outcome == "authority_http"
    assert artifact.attempts[0].wire_bytes == artifact.attempts[0].decoded_bytes == 0


def test_contract_rejects_403_body_transient_retry_chain(tmp_path: Path) -> None:
    def timeout_body() -> RawHttpResponse:
        def chunks():
            raise TimeoutError("read timeout")
            yield b""  # pragma: no cover

        return RawHttpResponse(200, {"Content-Type": "text/plain"}, chunks())

    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [timeout_body(), timeout_body(), timeout_body()],
    }))
    payload = artifact.model_dump(mode="json")
    payload["attempts"][0]["http_status"] = 403

    path = _write_rehashed_artifact(tmp_path, "forbidden-body-retry.json", payload)
    with pytest.raises(SiteDiagnosticError, match="403|authority|retry|status"):
        load_site_diagnostic(path)


def test_root_rejection_reason_cannot_be_recast_as_page_only_reason(tmp_path: Path) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://outside.example/map.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        }),
        allowed_domains=["example.com", "outside.example"],
        allowed_document_origins=["https://example.com"],
    )
    payload = artifact.model_dump(mode="json")
    payload["rejected_urls"][0]["reason"] = "cross_origin_requires_diagnosis"
    payload["rejected_urls"][0]["request_slot_ordinal"] = None
    payload["rejected_urls"][1]["request_slot_ordinal"] = 2
    payload["budget_usage"]["http_requests"] -= 1
    payload["diagnostic_status"] = "blocked"
    payload["recommendation"] = "operator_review"
    payload["decisive_priority"] = 6
    payload["next_action"] = "revise_inputs_or_boundaries_and_rediagnose"
    payload["outcome_reasons"] = []

    path = _write_rehashed_artifact(tmp_path, "root-as-page-rejection.json", payload)
    with pytest.raises(SiteDiagnosticError, match="rejection|lineage|root|page"):
        load_site_diagnostic(path)


def test_public_ipv6_literal_is_supported_end_to_end() -> None:
    host = "2001:4860:4860::8888"
    transport = FakeTransport({
        f"https://[{host}]/robots.txt": [response(404)],
        f"https://[{host}]/sitemap.xml": [response(404)],
    })
    artifact = diagnose_site(
        requested_url=f"https://[{host}]/news",
        site_key="ipv6-example",
        allowed_domains=[host],
        allowed_document_origins=[f"https://[{host}]"],
        user_agent="web-listening-bot/1.1",
        product_token="web-listening-bot",
        identity_id="default",
        transport=transport,
    )

    assert artifact.canonical_origin.host == host
    assert artifact.allowed_domains == [host]
    assert artifact.decisive_priority == 5
    assert [item[0] for item in transport.requests] == [
        f"https://[{host}]/robots.txt", f"https://[{host}]/sitemap.xml"
    ]
    artifact.verify_artifact_sha256()


@pytest.mark.parametrize("host", ["::1", "fe80::1", "fc00::1"])
def test_non_public_ipv6_literal_is_rejected_before_transport(host: str) -> None:
    transport = FakeTransport({})
    with pytest.raises(SiteDiagnosticError, match="public"):
        diagnose_site(
            requested_url=f"https://[{host}]/",
            site_key="ipv6-private",
            allowed_domains=[host],
            allowed_document_origins=[f"https://[{host}]"],
            user_agent="web-listening-bot/1.1",
            product_token="web-listening-bot",
            identity_id="default",
            transport=transport,
        )
    assert transport.requests == []


def test_aux_robots_consuming_last_slot_uses_budget_disposition_for_disallowed_sitemap() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    aux_robots = b"User-agent: *\nDisallow: /site.xml\n"
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://maps.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
    })

    artifact = diagnose(
        transport,
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
        budgets=DiagnosticBudgets(http_requests=2),
    )

    assert [item[0] for item in transport.requests] == [
        "https://example.com/robots.txt", "https://maps.example.net/robots.txt"
    ]
    assert artifact.budget_usage.http_requests == 2
    assert artifact.decisive_priority == 1
    assert artifact.truncation_reasons == ["http_request_budget_exhausted"]
    assert "sitemap_disallowed_by_robots" in artifact.outcome_reasons
    assert [(item.reason, item.request_slot_ordinal) for item in artifact.rejected_urls] == [
        ("http_request_budget_exhausted", None)
    ]


def test_prior_safety_drain_switches_to_budget_dispositions_after_exact_request_cap() -> None:
    robots = (
        b"User-agent: *\nAllow: /\nSitemap:\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
    })

    artifact = diagnose(transport, budgets=DiagnosticBudgets(http_requests=2))

    assert len(transport.requests) == 1
    assert artifact.budget_usage.http_requests == 2
    assert artifact.decisive_priority == 1
    assert [(item.reason, item.request_slot_ordinal) for item in artifact.rejected_urls] == [
        ("prior_safety_stop", 2),
        ("http_request_budget_exhausted", None),
    ]


def test_unknown_body_ssl_error_is_priority_one_and_never_retried() -> None:
    def tls_failure() -> RawHttpResponse:
        def chunks():
            raise ssl.SSLError("unknown tls record failure")
            yield b""  # pragma: no cover

        return RawHttpResponse(200, {"Content-Type": "text/plain"}, chunks())

    transport = FakeTransport({
        "https://example.com/robots.txt": [tls_failure(), tls_failure(), tls_failure()],
    })
    artifact = diagnose(transport)

    assert len(transport.requests) == 1
    assert artifact.decisive_priority == 1
    assert (artifact.diagnostic_status, artifact.recommendation) == ("blocked", "operator_review")
    assert artifact.attempts[0].outcome == "body_tls_policy"


def test_contract_rejects_retry_after_body_tls_policy_failure(tmp_path: Path) -> None:
    def timeout_body() -> RawHttpResponse:
        def chunks():
            raise TimeoutError("read timeout")
            yield b""  # pragma: no cover

        return RawHttpResponse(200, {"Content-Type": "text/plain"}, chunks())

    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [timeout_body(), timeout_body(), timeout_body()],
    }))
    payload = artifact.model_dump(mode="json")
    payload["attempts"][0]["outcome"] = "body_tls_policy"

    path = _write_rehashed_artifact(tmp_path, "body-tls-retry.json", payload)
    with pytest.raises(SiteDiagnosticError, match="retry|TLS|tls|safety"):
        load_site_diagnostic(path)


@pytest.mark.parametrize("status", [404, 410])
def test_empty_status_stops_before_body_and_fallback_remains_complete(status: int) -> None:
    body_reads = 0

    def empty_status() -> RawHttpResponse:
        def chunks():
            nonlocal body_reads
            body_reads += 1
            raise TimeoutError("must not read known empty status body")
            yield b""  # pragma: no cover

        return RawHttpResponse(status, {"Content-Type": "text/plain"}, chunks())

    transport = FakeTransport({
        "https://example.com/robots.txt": [empty_status()],
        "https://example.com/sitemap.xml": [empty_status()],
    })
    artifact = diagnose(transport)

    assert len(transport.requests) == 2
    assert body_reads == 0
    assert artifact.decisive_priority == 5
    assert [item.outcome for item in artifact.attempts] == ["completed_empty"] * 2
    assert all(item.wire_bytes == item.decoded_bytes == 0 for item in artifact.attempts)


def test_known_transient_status_is_not_overridden_by_body_timeout() -> None:
    body_reads = 0

    def unavailable() -> RawHttpResponse:
        def chunks():
            nonlocal body_reads
            body_reads += 1
            raise TimeoutError("must not read known transient status body")
            yield b""  # pragma: no cover

        return RawHttpResponse(503, {"Content-Type": "text/plain"}, chunks())

    transport = FakeTransport({
        "https://example.com/robots.txt": [unavailable(), unavailable(), unavailable()],
    })
    artifact = diagnose(transport)

    assert len(transport.requests) == 3
    assert body_reads == 0
    assert artifact.decisive_priority == 4
    assert [item.outcome for item in artifact.attempts] == ["transient_http"] * 3


def test_contract_rejects_404_reclassified_as_terminal_http(tmp_path: Path) -> None:
    payload = json.loads(
        Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text(encoding="utf-8")
    )
    payload["attempts"][1]["outcome"] = "terminal_http"
    payload["sitemap_evidence"][0]["root_type"] = "failed"
    payload["sitemap_evidence"][0]["outcome"] = "deterministic"
    payload["diagnostic_status"] = "blocked"
    payload["recommendation"] = "operator_review"
    payload["decisive_priority"] = 6
    payload["next_action"] = "revise_inputs_or_boundaries_and_rediagnose"
    payload["outcome_reasons"] = ["http:404"]

    path = _write_rehashed_artifact(tmp_path, "404-terminal.json", payload)
    with pytest.raises(SiteDiagnosticError, match="404|410|empty|status"):
        load_site_diagnostic(path)


def test_contract_rejects_file_budget_disposition_below_cap(tmp_path: Path) -> None:
    payload = json.loads(
        Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text(encoding="utf-8")
    )
    payload["attempts"] = payload["attempts"][:1]
    payload["sitemap_evidence"] = []
    payload["rejected_urls"] = [{
        "url": "https://example.com/sitemap.xml",
        "final_url": None,
        "raw_value": None,
        "reason": "sitemap_document_budget_exhausted",
        "queue_ordinal": 1,
        "parent_sha256": payload["attempts"][0]["content_sha256"],
        "entry_ordinal": None,
        "request_slot_ordinal": 2,
    }]
    payload["budget_usage"]["sitemap_wire_bytes"] = 0
    payload["budget_usage"]["sitemap_decoded_bytes"] = 0
    payload["diagnostic_status"] = "blocked"
    payload["recommendation"] = "operator_review"
    payload["decisive_priority"] = 6
    payload["next_action"] = "revise_inputs_or_boundaries_and_rediagnose"
    payload["truncation_reasons"] = ["sitemap_document_budget_exhausted"]
    payload["outcome_reasons"] = ["sitemap_document_budget_exhausted"]

    path = _write_rehashed_artifact(tmp_path, "fake-file-budget.json", payload)
    with pytest.raises(SiteDiagnosticError, match="budget|cap|sitemap"):
        load_site_diagnostic(path)


def test_contract_requires_origin_policy_evidence_completion_order(tmp_path: Path) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    aux_robots = b"User-agent: *\nAllow: /site.xml\n"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://maps.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
            "https://maps.example.net/site.xml": [response(404)],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )
    assert [item.origin.host for item in artifact.origin_policy_evidence] == [
        "example.com", "maps.example.net"
    ]

    payload = artifact.model_dump(mode="json")
    payload["origin_policy_evidence"].reverse()
    path = _write_rehashed_artifact(tmp_path, "reversed-policies.json", payload)
    with pytest.raises(SiteDiagnosticError, match="policy|order|attempt"):
        load_site_diagnostic(path)


def test_contract_binds_attempt_order_to_request_slot_order(tmp_path: Path) -> None:
    payload = json.loads(
        Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text(encoding="utf-8")
    )
    payload["attempts"][0]["request_slot_ordinal"] = 2
    payload["attempts"][1]["request_slot_ordinal"] = 1

    path = _write_rehashed_artifact(tmp_path, "reversed-attempt-slots.json", payload)
    with pytest.raises(SiteDiagnosticError, match="attempt|request.slot|causal|order"):
        load_site_diagnostic(path)


def test_contract_rejects_index_child_slot_before_parent_attempt(tmp_path: Path) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = (
        b"<sitemapindex><sitemap><loc>https://example.com/child.xml</loc>"
        b"</sitemap></sitemapindex>"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/index.xml": [response(200, index, "application/xml")],
        }),
        budgets=DiagnosticBudgets(sitemap_documents=1),
    )
    rejected = next(
        item for item in artifact.rejected_urls
        if item.reason == "sitemap_document_budget_exhausted"
    )
    payload = artifact.model_dump(mode="json")
    payload["attempts"][1]["request_slot_ordinal"] = rejected.request_slot_ordinal
    payload["rejected_urls"][0]["request_slot_ordinal"] = 2

    path = _write_rehashed_artifact(tmp_path, "child-before-parent.json", payload)
    with pytest.raises(SiteDiagnosticError, match="parent|causal|request.slot|order"):
        load_site_diagnostic(path)


def test_contract_rejects_sitemap_rejection_slot_before_aux_policy(tmp_path: Path) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://maps.example.net/site.xml\n"
    aux_robots = b"User-agent: *\nDisallow: /site.xml\n"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://maps.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=["https://example.com", "https://maps.example.net"],
    )
    payload = artifact.model_dump(mode="json")
    payload["attempts"][1]["request_slot_ordinal"] = 3
    payload["rejected_urls"][0]["request_slot_ordinal"] = 2

    path = _write_rehashed_artifact(tmp_path, "rejection-before-aux-policy.json", payload)
    with pytest.raises(SiteDiagnosticError, match="policy|causal|request.slot|order"):
        load_site_diagnostic(path)


def test_retry_and_redirect_attempts_use_strictly_increasing_request_slots() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/start.xml\n"
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(503), response(200, robots, "text/plain")],
        "https://example.com/start.xml": [response(302, Location="/final.xml")],
        "https://example.com/final.xml": [response(200, urlset, "application/xml")],
    }))

    assert artifact.accepted_page_urls == ["https://example.com/page"]
    assert [item.attempt_ordinal for item in artifact.attempts] == [1, 2, 3, 4]
    assert [item.request_slot_ordinal for item in artifact.attempts] == [1, 2, 3, 4]
    assert [item.outcome for item in artifact.attempts] == [
        "transient_http", "success", "redirect", "success"
    ]


def test_contract_rejects_duplicate_and_later_attempt_primary_fifo_swap(tmp_path: Path) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/three.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [response(404)],
        "https://example.com/three.xml": [response(404)],
    }))
    duplicate = next(
        item for item in artifact.duplicate_urls
        if item.reason == "duplicate_document_url"
    )
    queue_three_attempt = next(
        item for item in artifact.attempts if item.queue_ordinal == 3
    )
    assert (duplicate.request_slot_ordinal, queue_three_attempt.request_slot_ordinal) == (3, 4)

    payload = artifact.model_dump(mode="json")
    payload["duplicate_urls"][0]["request_slot_ordinal"] = 4
    next(item for item in payload["attempts"] if item["queue_ordinal"] == 3)[
        "request_slot_ordinal"
    ] = 3
    path = _write_rehashed_artifact(tmp_path, "duplicate-fifo-swap.json", payload)
    with pytest.raises(SiteDiagnosticError, match="FIFO|primary|queue|causal"):
        load_site_diagnostic(path)


def test_contract_rejects_two_root_attempts_reordered_and_renumbered(tmp_path: Path) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [response(404)],
        "https://example.com/two.xml": [response(404)],
    }))
    payload = artifact.model_dump(mode="json")
    payload["attempts"] = [
        payload["attempts"][0], payload["attempts"][2], payload["attempts"][1]
    ]
    for ordinal, attempt in enumerate(payload["attempts"], 1):
        attempt["attempt_ordinal"] = ordinal
        attempt["request_slot_ordinal"] = ordinal

    path = _write_rehashed_artifact(tmp_path, "root-attempt-fifo-swap.json", payload)
    with pytest.raises(SiteDiagnosticError, match="FIFO|primary|queue|causal"):
        load_site_diagnostic(path)


def test_contract_requires_unsafe_xml_reason_exactly(tmp_path: Path) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [
            response(200, b"<html><body>not sitemap</body></html>", "application/xml")
        ],
    }))
    assert "unsafe_xml_root" in artifact.outcome_reasons
    payload = artifact.model_dump(mode="json")
    payload["outcome_reasons"] = []

    path = _write_rehashed_artifact(tmp_path, "missing-unsafe-root-reason.json", payload)
    with pytest.raises(SiteDiagnosticError, match="reason|unsafe_xml_root|evidence"):
        load_site_diagnostic(path)


def test_contract_rejects_fabricated_reason_even_with_priority_one(tmp_path: Path) -> None:
    payload = json.loads(
        Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text(encoding="utf-8")
    )
    payload["diagnostic_status"] = "blocked"
    payload["recommendation"] = "operator_review"
    payload["decisive_priority"] = 1
    payload["next_action"] = "resolve_safety_or_authority_error"
    payload["outcome_reasons"] = ["fabricated_safety_reason"]

    path = _write_rehashed_artifact(tmp_path, "fabricated-reason.json", payload)
    with pytest.raises(SiteDiagnosticError, match="reason|evidence|fabricated"):
        load_site_diagnostic(path)


def test_contract_requires_every_reason_in_multi_reason_block(tmp_path: Path) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/terminal.xml\n"
        b"Sitemap: https://example.com/unsafe.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/terminal.xml": [response(400)],
        "https://example.com/unsafe.xml": [
            response(200, b"<html/>", "application/xml")
        ],
    }))
    assert set(artifact.outcome_reasons) == {"http:400", "unsafe_xml_root"}

    for missing_reason in artifact.outcome_reasons:
        payload = artifact.model_dump(mode="json")
        payload["outcome_reasons"] = [
            reason for reason in payload["outcome_reasons"]
            if reason != missing_reason
        ]
        path = _write_rehashed_artifact(
            tmp_path, f"missing-{missing_reason}.json", payload
        )
        with pytest.raises(SiteDiagnosticError, match="reason|evidence"):
            load_site_diagnostic(path)


def test_two_aux_origins_with_identical_robots_digest_and_line_are_unambiguous(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://maps1.example.net/site.xml\n"
        b"Sitemap: https://maps2.example.net/site.xml\n"
    )
    aux_robots = (
        b"User-agent: *\nAllow: /site.xml\n"
        b"Sitemap: https://ignored.example/ignored.xml\n"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://maps1.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
            "https://maps1.example.net/site.xml": [response(404)],
            "https://maps2.example.net/robots.txt": [response(200, aux_robots, "text/plain")],
            "https://maps2.example.net/site.xml": [response(404)],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://maps1.example.net",
            "https://maps2.example.net",
        ],
    )
    aux_occurrences = [
        item for item in artifact.counted_url_occurrences
        if item.source == "aux_robots_sitemap"
    ]
    assert [item.source_origin for item in aux_occurrences] == [
        "https://maps1.example.net/", "https://maps2.example.net/"
    ]

    payload = artifact.model_dump(mode="json")
    payload_aux = [
        item for item in payload["counted_url_occurrences"]
        if item["source"] == "aux_robots_sitemap"
    ]
    payload_aux[1]["source_origin"] = payload_aux[0]["source_origin"]
    path = _write_rehashed_artifact(tmp_path, "aux-origin-misbound.json", payload)
    with pytest.raises(SiteDiagnosticError, match="auxiliary|origin|policy|lineage"):
        load_site_diagnostic(path)


def test_contract_rejects_cross_origin_page_resigned_as_duplicate_fallback(
    tmp_path: Path,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    urlset = (
        b"<urlset><url><loc>https://other.test/page</loc></url></urlset>"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    }))
    assert artifact.decisive_priority == 6
    payload = artifact.model_dump(mode="json")
    forged_duplicate = payload["rejected_urls"].pop()
    forged_duplicate["reason"] = "duplicate_page_url"
    payload["duplicate_urls"].append(forged_duplicate)
    payload["diagnostic_status"] = "complete"
    payload["recommendation"] = "bounded_homepage_fallback"
    payload["decisive_priority"] = 5
    payload["next_action"] = (
        "submit_bounded_homepage_fallback_for_operator_review"
    )

    path = _write_rehashed_artifact(tmp_path, "cross-origin-as-duplicate.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|canonical|origin|earlier"):
        load_site_diagnostic(path)


def test_contract_rejects_first_seen_page_resigned_as_duplicate_fallback(
    tmp_path: Path,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    urlset = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    }))
    payload = artifact.model_dump(mode="json")
    accepted = payload["accepted_page_evidence"].pop()
    payload["accepted_page_urls"] = []
    payload["duplicate_urls"].append({
        "url": accepted["url"],
        "final_url": None,
        "raw_value": None,
        "reason": "duplicate_page_url",
        "trigger_reason": None,
        "queue_ordinal": accepted["source_queue_ordinal"],
        "parent_sha256": accepted["parent_sha256"],
        "entry_ordinal": accepted["entry_ordinal"],
        "request_slot_ordinal": None,
    })
    payload["diagnostic_status"] = "complete"
    payload["recommendation"] = "bounded_homepage_fallback"
    payload["decisive_priority"] = 5
    payload["next_action"] = (
        "submit_bounded_homepage_fallback_for_operator_review"
    )

    path = _write_rehashed_artifact(tmp_path, "first-page-as-duplicate.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|earlier|first"):
        load_site_diagnostic(path)


def test_contract_rejects_duplicate_backed_only_by_a_later_urlset_observation(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    first = b"<urlset><url><loc>https://example.com/one</loc></url></urlset>"
    second = b"<urlset><url><loc>https://example.com/two</loc></url></urlset>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [response(200, first, "application/xml")],
        "https://example.com/two.xml": [response(200, second, "application/xml")],
    }))
    payload = artifact.model_dump(mode="json")
    earlier = payload["accepted_page_evidence"].pop(0)
    payload["accepted_page_urls"].pop(0)
    payload["duplicate_urls"].append({
        "url": "https://example.com/two",
        "final_url": None,
        "raw_value": None,
        "reason": "duplicate_page_url",
        "trigger_reason": None,
        "queue_ordinal": earlier["source_queue_ordinal"],
        "parent_sha256": earlier["parent_sha256"],
        "entry_ordinal": earlier["entry_ordinal"],
        "request_slot_ordinal": None,
    })

    path = _write_rehashed_artifact(tmp_path, "duplicate-backed-by-later.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|earlier"):
        load_site_diagnostic(path)


def test_duplicate_page_url_is_valid_within_one_urlset() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/map.xml\n"
    urlset = (
        b"<urlset>"
        b"<url><loc>https://example.com/page</loc></url>"
        b"<url><loc>https://example.com/page</loc></url>"
        b"</urlset>"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(200, urlset, "application/xml")],
    }))

    assert artifact.accepted_page_urls == ["https://example.com/page"]
    assert [
        (item.url, item.queue_ordinal, item.entry_ordinal)
        for item in artifact.duplicate_urls
    ] == [("https://example.com/page", 1, 2)]


def test_duplicate_page_url_is_valid_across_multiple_urlsets() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
    )
    first = b"<urlset><url><loc>https://example.com/page</loc></url></urlset>"
    second = (
        b"<urlset><url><loc>https://example.com/page</loc>"
        b"<lastmod>2026-08-08</lastmod></url></urlset>"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [response(200, first, "application/xml")],
        "https://example.com/two.xml": [response(200, second, "application/xml")],
    }))

    assert artifact.accepted_page_urls == ["https://example.com/page"]
    assert [
        (item.url, item.queue_ordinal, item.entry_ordinal)
        for item in artifact.duplicate_urls
    ] == [("https://example.com/page", 2, 1)]


def test_contract_rejects_reversed_depth_rejection_primary_timeline(
    tmp_path: Path,
) -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = (
        b"<sitemapindex>"
        b"<sitemap><loc>https://example.com/one.xml</loc></sitemap>"
        b"<sitemap><loc>https://example.com/two.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [response(200, robots, "text/plain")],
            "https://example.com/index.xml": [
                response(200, index, "application/xml")
            ],
        }),
        budgets=DiagnosticBudgets(sitemap_depth=0),
    )
    depth_rejections = [
        item for item in artifact.rejected_urls
        if item.reason == "sitemap_depth_budget_exhausted"
    ]
    assert [
        (item.entry_ordinal, item.queue_ordinal, item.request_slot_ordinal)
        for item in depth_rejections
    ] == [(1, 2, 3), (2, 3, 4)]

    payload = artifact.model_dump(mode="json")
    payload_depth_rejections = [
        item for item in payload["rejected_urls"]
        if item["reason"] == "sitemap_depth_budget_exhausted"
    ]
    first, second = payload_depth_rejections
    first["request_slot_ordinal"] = 4
    second["request_slot_ordinal"] = 3
    payload["rejected_urls"] = [second, first]

    path = _write_rehashed_artifact(tmp_path, "reversed-depth-primary.json", payload)
    with pytest.raises(SiteDiagnosticError, match="FIFO|primary|entry|causal"):
        load_site_diagnostic(path)


@pytest.mark.parametrize("case", ["file_budget", "prior_stop"])
def test_contract_rejects_reversed_scheduled_root_rejections(
    tmp_path: Path,
    case: str,
) -> None:
    if case == "file_budget":
        robots = (
            b"User-agent: *\nAllow: /\n"
            b"Sitemap: https://example.com/one.xml\n"
            b"Sitemap: https://example.com/two.xml\n"
            b"Sitemap: https://example.com/three.xml\n"
        )
        artifact = diagnose(
            FakeTransport({
                "https://example.com/robots.txt": [
                    response(200, robots, "text/plain")
                ],
                "https://example.com/one.xml": [response(404)],
            }),
            budgets=DiagnosticBudgets(sitemap_documents=1),
        )
        scheduled = [
            item for item in artifact.rejected_urls
            if item.reason == "sitemap_document_budget_exhausted"
        ]
    else:
        robots = (
            b"User-agent: *\nAllow: /\n"
            b"Sitemap: https://outside.example/one.xml\n"
            b"Sitemap: https://outside.example/two.xml\n"
        )
        artifact = diagnose(
            FakeTransport({
                "https://example.com/robots.txt": [
                    response(200, robots, "text/plain")
                ],
            }),
            allowed_domains=["example.com", "outside.example"],
        )
        scheduled = artifact.rejected_urls
    assert len(scheduled) == 2
    assert [item.request_slot_ordinal for item in scheduled] == [2, 3]

    payload = artifact.model_dump(mode="json")
    first, second = payload["rejected_urls"]
    first["request_slot_ordinal"] = 3
    second["request_slot_ordinal"] = 2
    payload["rejected_urls"] = [second, first]

    path = _write_rehashed_artifact(
        tmp_path, f"reversed-{case}-primary.json", payload
    )
    with pytest.raises(SiteDiagnosticError, match="FIFO|primary|entry|causal"):
        load_site_diagnostic(path)


def test_contract_rejects_fallback_duplicate_without_earlier_document(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text(
            encoding="utf-8"
        )
    )
    robots_attempt, sitemap_attempt = payload["attempts"]
    payload["attempts"] = [robots_attempt]
    payload["sitemap_evidence"] = []
    payload["duplicate_urls"] = [{
        "url": sitemap_attempt["requested_url"],
        "final_url": None,
        "raw_value": None,
        "reason": "duplicate_document_url",
        "trigger_reason": None,
        "queue_ordinal": 1,
        "parent_sha256": robots_attempt["content_sha256"],
        "entry_ordinal": None,
        "request_slot_ordinal": 2,
    }]

    path = _write_rehashed_artifact(tmp_path, "unproven-fallback-duplicate.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|earlier|document"):
        load_site_diagnostic(path)


def test_duplicate_robots_sitemap_directive_has_earlier_document_proof() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/map.xml\n"
        b"Sitemap: https://example.com/map.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [response(404)],
    }))

    duplicate = next(
        item for item in artifact.duplicate_urls
        if item.reason == "duplicate_document_url"
    )
    assert (duplicate.queue_ordinal, duplicate.entry_ordinal) == (2, 4)


def test_duplicate_sitemapindex_child_has_earlier_document_proof() -> None:
    robots = b"User-agent: *\nAllow: /\nSitemap: https://example.com/index.xml\n"
    index = (
        b"<sitemapindex>"
        b"<sitemap><loc>https://example.com/child.xml</loc></sitemap>"
        b"<sitemap><loc>https://example.com/child.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/index.xml": [response(200, index, "application/xml")],
        "https://example.com/child.xml": [response(404)],
    }))

    duplicate = next(
        item for item in artifact.duplicate_urls
        if item.reason == "duplicate_document_url"
    )
    assert (duplicate.queue_ordinal, duplicate.entry_ordinal) == (3, 2)


def test_duplicate_initial_document_url_remains_proven_after_redirect() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/start.xml\n"
        b"Sitemap: https://example.com/start.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/start.xml": [
            response(301, Location="/final.xml")
        ],
        "https://example.com/final.xml": [response(404)],
    }))

    duplicate = next(
        item for item in artifact.duplicate_urls
        if item.reason == "duplicate_document_url"
    )
    assert duplicate.url == "https://example.com/start.xml"


def test_contract_rejects_scheduled_url_duplicate_relabeled_as_digest(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/map.xml\n"
        b"Sitemap: https://example.com/map.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/map.xml": [
            response(200, b"<urlset/>", "application/xml")
        ],
    }))
    duplicate = artifact.duplicate_urls[0]
    assert duplicate.reason == "duplicate_document_url"

    payload = artifact.model_dump(mode="json")
    payload["duplicate_urls"][0]["reason"] = "duplicate_document_digest"
    payload["duplicate_urls"][0]["request_slot_ordinal"] = None
    payload["budget_usage"]["http_requests"] -= 1
    path = _write_rehashed_artifact(tmp_path, "url-duplicate-as-digest.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|precedence|reason"):
        load_site_diagnostic(path)


def test_redirect_final_url_deduplicates_later_direct_schedule_before_request(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/start.xml\n"
        b"Sitemap: https://example.com/final.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/start.xml": [
            response(301, Location="/final.xml")
        ],
        "https://example.com/final.xml": [response(404)],
    })
    artifact = diagnose(transport)

    assert [item[0] for item in transport.requests].count(
        "https://example.com/final.xml"
    ) == 1
    duplicate = next(
        item for item in artifact.duplicate_urls
        if item.reason == "duplicate_document_final_url"
    )
    assert (duplicate.url, duplicate.final_url, duplicate.queue_ordinal) == (
        "https://example.com/final.xml",
        "https://example.com/final.xml",
        2,
    )
    assert duplicate.request_slot_ordinal == 4
    assert artifact.budget_usage.http_requests == 4
    assert len(transport.requests) == 3
    assert not any(item.queue_ordinal == 2 for item in artifact.attempts)
    assert load_site_diagnostic(
        write_site_diagnostic(artifact, tmp_path / "legal-direct-final-duplicate.json")
    ) == artifact

    payload = artifact.model_dump(mode="json")
    payload["duplicate_urls"][0]["request_slot_ordinal"] = None
    payload["budget_usage"]["http_requests"] = 3
    path = _write_rehashed_artifact(tmp_path, "missing-final-duplicate-slot.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|slot|attempt"):
        load_site_diagnostic(path)


def test_direct_document_then_redirect_to_it_is_a_legal_final_url_duplicate(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/final.xml\n"
        b"Sitemap: https://example.com/start.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/final.xml": [response(404), response(404)],
        "https://example.com/start.xml": [
            response(301, Location="/final.xml")
        ],
    })
    artifact = diagnose(transport)

    duplicate = next(
        item for item in artifact.duplicate_urls
        if item.reason == "duplicate_document_final_url"
    )
    assert (duplicate.url, duplicate.final_url, duplicate.queue_ordinal) == (
        "https://example.com/start.xml",
        "https://example.com/final.xml",
        2,
    )
    assert [item[0] for item in transport.requests].count(
        "https://example.com/final.xml"
    ) == 2
    assert duplicate.request_slot_ordinal is None
    assert artifact.budget_usage.http_requests == len(transport.requests) == 4
    assert load_site_diagnostic(
        write_site_diagnostic(artifact, tmp_path / "legal-redirect-final-duplicate.json")
    ) == artifact

    payload = artifact.model_dump(mode="json")
    payload["duplicate_urls"][0]["request_slot_ordinal"] = 5
    payload["budget_usage"]["http_requests"] = 5
    path = _write_rehashed_artifact(tmp_path, "extra-final-duplicate-slot.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|slot|attempt"):
        load_site_diagnostic(path)

    payload = artifact.model_dump(mode="json")
    payload["duplicate_urls"][0]["reason"] = "duplicate_document_digest"
    path = _write_rehashed_artifact(tmp_path, "final-duplicate-as-digest.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|precedence|reason"):
        load_site_diagnostic(path)


def test_pre_request_final_url_duplicate_respects_http_request_budget() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/start.xml\n"
        b"Sitemap: https://example.com/final.xml\n"
    )
    transport = FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/start.xml": [
            response(301, Location="/final.xml")
        ],
        "https://example.com/final.xml": [response(404)],
    })
    artifact = diagnose(
        transport,
        budgets=DiagnosticBudgets(http_requests=3),
    )

    assert artifact.budget_usage.http_requests == 3
    assert len(transport.requests) == 3
    assert not any(
        item.reason == "duplicate_document_final_url"
        for item in artifact.duplicate_urls
    )
    rejected = next(item for item in artifact.rejected_urls if item.queue_ordinal == 2)
    assert (rejected.reason, rejected.request_slot_ordinal) == (
        "http_request_budget_exhausted",
        None,
    )


def test_final_url_duplicate_precedes_duplicate_document_digest() -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/one.xml\n"
        b"Sitemap: https://example.com/two.xml\n"
        b"Sitemap: https://example.com/three.xml\n"
    )
    sitemap = b"<urlset/>"
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/one.xml": [
            response(200, sitemap, "application/xml"),
            response(200, sitemap, "application/xml"),
        ],
        "https://example.com/two.xml": [
            response(200, sitemap, "application/xml")
        ],
        "https://example.com/three.xml": [
            response(301, Location="/one.xml")
        ],
    }))

    assert [item.reason for item in artifact.duplicate_urls] == [
        "duplicate_document_digest",
        "duplicate_document_final_url",
    ]
    assert artifact.duplicate_urls[-1].final_url == "https://example.com/one.xml"


def test_document_duplicate_reason_has_url_then_final_then_digest_precedence() -> None:
    prior_initials = {"https://example.com/initial.xml"}
    prior_resources = {
        "https://example.com/initial.xml",
        "https://example.com/final.xml",
    }
    prior_digests = {"a" * 64}

    assert document_duplicate_reason(
        scheduled_initial_url="https://example.com/initial.xml",
        resolved_final_url="https://example.com/new-final.xml",
        content_sha256="a" * 64,
        prior_scheduled_initial_urls=prior_initials,
        prior_observed_document_urls=prior_resources,
        prior_document_digests=prior_digests,
    ) == "duplicate_document_url"
    assert document_duplicate_reason(
        scheduled_initial_url="https://example.com/new.xml",
        resolved_final_url="https://example.com/final.xml",
        content_sha256="a" * 64,
        prior_scheduled_initial_urls=prior_initials,
        prior_observed_document_urls=prior_resources,
        prior_document_digests=prior_digests,
    ) == "duplicate_document_final_url"
    assert document_duplicate_reason(
        scheduled_initial_url="https://example.com/new.xml",
        resolved_final_url="https://example.com/new-final.xml",
        content_sha256="a" * 64,
        prior_scheduled_initial_urls=prior_initials,
        prior_observed_document_urls=prior_resources,
        prior_document_digests=prior_digests,
    ) == "duplicate_document_digest"


def test_contract_rejects_rehashed_unproven_duplicate_document_final_url(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: https://example.com/a-start.xml\n"
        b"Sitemap: https://example.com/b-start.xml\n"
    )
    artifact = diagnose(FakeTransport({
        "https://example.com/robots.txt": [response(200, robots, "text/plain")],
        "https://example.com/a-start.xml": [
            response(301, Location="/a-final.xml")
        ],
        "https://example.com/a-final.xml": [
            response(200, b"<urlset/>", "application/xml")
        ],
        "https://example.com/b-start.xml": [
            response(301, Location="/b-final.xml")
        ],
        "https://example.com/b-final.xml": [
            response(200, b"<urlset></urlset>", "application/xml")
        ],
    }))
    assert [item.final_url for item in artifact.sitemap_evidence] == [
        "https://example.com/a-final.xml",
        "https://example.com/b-final.xml",
    ]

    payload = artifact.model_dump(mode="json")
    second_evidence = next(
        item for item in payload["sitemap_evidence"] if item["queue_ordinal"] == 2
    )
    payload["sitemap_evidence"] = [
        item for item in payload["sitemap_evidence"] if item["queue_ordinal"] != 2
    ]
    payload["duplicate_urls"].append({
        "url": second_evidence["url"],
        "final_url": second_evidence["final_url"],
        "raw_value": None,
        "reason": "duplicate_document_final_url",
        "trigger_reason": None,
        "queue_ordinal": second_evidence["queue_ordinal"],
        "parent_sha256": second_evidence["parent_sha256"],
        "entry_ordinal": second_evidence["parent_entry_ordinal"],
        "request_slot_ordinal": None,
    })

    path = _write_rehashed_artifact(tmp_path, "unproven-final-duplicate.json", payload)
    with pytest.raises(SiteDiagnosticError, match="duplicate|final|earlier|observed"):
        load_site_diagnostic(path)


def test_contract_rejects_document_queue_ordinals_shifted_away_from_one(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        Path("docs/testing/fixtures/site-diagnostic-v1.sample.json").read_text(
            encoding="utf-8"
        )
    )
    payload["attempts"][1]["queue_ordinal"] = 10
    payload["sitemap_evidence"][0]["queue_ordinal"] = 10
    payload["counted_url_occurrences"][0]["queue_ordinal"] = 10

    path = _write_rehashed_artifact(tmp_path, "shifted-document-queue.json", payload)
    with pytest.raises(SiteDiagnosticError, match="queue|contiguous|ordinal"):
        load_site_diagnostic(path)


def test_contract_rejects_rehashed_sitemap_redirect_https_to_http_downgrade(
    tmp_path: Path,
) -> None:
    robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: http://maps.example.net/seed.xml\n"
        b"Sitemap: https://maps.example.net/start.xml\n"
    )
    aux_robots = b"User-agent: *\nAllow: /\n"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(200, robots, "text/plain")
            ],
            "http://maps.example.net/robots.txt": [
                response(200, aux_robots, "text/plain")
            ],
            "http://maps.example.net/seed.xml": [response(404)],
            "https://maps.example.net/robots.txt": [
                response(200, aux_robots, "text/plain")
            ],
            "https://maps.example.net/start.xml": [
                response(301, Location="https://maps.example.net/final.xml")
            ],
            "https://maps.example.net/final.xml": [response(404)],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "http://maps.example.net",
            "https://maps.example.net",
        ],
    )
    assert load_site_diagnostic(
        write_site_diagnostic(artifact, tmp_path / "legal-sitemap-upgrade.json")
    ) == artifact

    payload = artifact.model_dump(mode="json")
    final_attempt = next(
        item
        for item in payload["attempts"]
        if item["document_kind"] == "sitemap"
        and item["queue_ordinal"] == 2
        and item["redirect_chain"]
    )
    final_attempt["requested_url"] = "http://maps.example.net/final.xml"
    final_attempt["final_url"] = "http://maps.example.net/final.xml"
    final_evidence = next(
        item for item in payload["sitemap_evidence"] if item["queue_ordinal"] == 2
    )
    final_evidence["final_url"] = "http://maps.example.net/final.xml"

    path = _write_rehashed_artifact(tmp_path, "sitemap-redirect-downgrade.json", payload)
    with pytest.raises(SiteDiagnosticError, match="HTTPS.*HTTP|downgrade"):
        load_site_diagnostic(path)


def test_contract_rejects_rehashed_robots_redirect_https_to_http_downgrade(
    tmp_path: Path,
) -> None:
    canonical_robots = (
        b"User-agent: *\nAllow: /\n"
        b"Sitemap: http://maps.example.net/seed.xml\n"
    )
    aux_robots = b"User-agent: *\nAllow: /\n"
    artifact = diagnose(
        FakeTransport({
            "https://example.com/robots.txt": [
                response(301, Location="https://secure.example.net/robots.txt")
            ],
            "https://secure.example.net/robots.txt": [
                response(200, canonical_robots, "text/plain")
            ],
            "http://maps.example.net/robots.txt": [
                response(200, aux_robots, "text/plain")
            ],
            "http://maps.example.net/seed.xml": [response(404)],
        }),
        allowed_domains=["example.com", "example.net"],
        allowed_document_origins=[
            "https://example.com",
            "https://secure.example.net",
            "http://maps.example.net",
        ],
    )
    assert load_site_diagnostic(
        write_site_diagnostic(artifact, tmp_path / "legal-robots-https.json")
    ) == artifact

    payload = artifact.model_dump(mode="json")
    redirected_attempt = next(
        item
        for item in payload["attempts"]
        if item["document_kind"] == "robots" and item["redirect_chain"]
    )
    redirected_attempt["requested_url"] = "http://maps.example.net/robots.txt"
    redirected_attempt["final_url"] = "http://maps.example.net/robots.txt"

    path = _write_rehashed_artifact(tmp_path, "robots-redirect-downgrade.json", payload)
    with pytest.raises(SiteDiagnosticError, match="HTTPS.*HTTP|downgrade"):
        load_site_diagnostic(path)


def test_contract_accepts_http_to_https_redirect_upgrade(tmp_path: Path) -> None:
    artifact = diagnose(
        FakeTransport({
            "http://example.com/robots.txt": [
                response(301, Location="https://example.com/robots.txt")
            ],
            "https://example.com/robots.txt": [response(404)],
            "http://example.com/sitemap.xml": [response(404)],
        }),
        requested_url="http://example.com",
        allowed_document_origins=["http://example.com", "https://example.com"],
    )

    assert artifact.attempts[1].redirect_chain == [
        "http://example.com/robots.txt"
    ]
    assert load_site_diagnostic(
        write_site_diagnostic(artifact, tmp_path / "legal-http-upgrade.json")
    ) == artifact
