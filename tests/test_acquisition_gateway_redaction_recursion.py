from __future__ import annotations

import pytest

from web_listening.blocks.acquisition_gateway import redact_persisted_value


RECORDED_MARKER_URL = (
    "https://static.[BLOCKED_MARKER]insights.com/beacon.min.js/"
    "v31edd6df95cf4e85bb4c19e7a9bdbcba1788362987495"
)


def test_recorded_marker_url_redaction_terminates_with_stable_placeholder():
    redacted = redact_persisted_value({"url": RECORDED_MARKER_URL})

    assert redacted == {"url": "[URL REDACTED]"}


@pytest.mark.parametrize(
    "value",
    (
        "https://static.[BLOCKED_MARKER]insights.com/script.js",
        "https://[not-an-ipv6-host]/script.js",
    ),
)
def test_urlsplit_failure_redaction_uses_stable_placeholder(value: str):
    assert redact_persisted_value({"url": value}) == {"url": "[URL REDACTED]"}


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("https:relative/path?token=secret", "https:relative/path?token=[REDACTED]"),
        ("/relative/path?token=secret", "/relative/path?token=[REDACTED]"),
    ),
)
def test_no_netloc_redaction_keeps_text_rules(value: str, expected: str):
    assert redact_persisted_value({"url": value}) == {"url": expected}


@pytest.mark.parametrize(
    "value",
    (
        "https:///path",
    ),
)
def test_no_netloc_with_url_prefix_redacts_without_recursion(value: str):
    # Pre-fix: scheme present but empty netloc routed value back through
    # _redact_text, which re-matched the URL regex and re-entered _redact_url,
    # producing RecursionError. Post-fix: stable placeholder, no recursion.
    assert redact_persisted_value({"url": value}) == {"url": "[URL REDACTED]"}


def test_marker_query_and_header_text_keep_existing_redaction_rules():
    redacted = redact_persisted_value(
        {
            "url": "https://example.test/path?token=secret",
            "message": "Authorization: Bearer header-secret [BLOCKED_MARKER]",
        }
    )

    assert redacted["url"] == "https://example.test/path?token=%5BREDACTED%5D"
    assert redacted["message"] == "Authorization=[REDACTED] [REDACTED] [BLOCKED_MARKER]"
