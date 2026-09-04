"""Stable terminal classification for acquired HTML pages."""

from __future__ import annotations

import re
from collections.abc import Iterable
from html import unescape
from urllib.parse import urlsplit


DEFAULT_BLOCKED_MARKERS = (
    "access denied",
    "captcha",
    "just a moment",
    "please verify you are human to continue",
    "performing security verification",
    "request unsuccessful",
    "sorry, you have been blocked",
    "verification successful. waiting for",
)

_NON_DECISIVE_BLOCKED_MARKERS = frozenset(
    {
        "cloudflare",
        "enable javascript",
        "forbidden",
    }
)

_BLOCKED_PATH_PARTS = frozenset(
    {
        "access-denied",
        "captcha",
    }
)
_BLOCKED_PATH_SEQUENCES = (("cdn-cgi", "challenge-platform"),)
_NON_VISIBLE_HTML_RE = re.compile(
    r"<(?:script|noscript)\b[^>]*>.*?</(?:script|noscript)\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def classify_html_capture(
    *,
    requested_url: str,
    final_url: str,
    status_code: int | None,
    extracted_text: str,
    raw_text: str = "",
    blocked_markers: Iterable[str] = (),
) -> str:
    """Return one stable terminal reason for an HTML capture."""
    if status_code == 403:
        return "http_403"
    if status_code is None or not 200 <= status_code < 300:
        return "http_status_rejected"
    if _is_same_origin_block_redirect(requested_url, final_url):
        return "blocked_redirect"

    visible_raw_text = unescape(
        _HTML_TAG_RE.sub(" ", _NON_VISIBLE_HTML_RE.sub(" ", raw_text))
    )
    haystack = f"{extracted_text}\n{visible_raw_text}"
    markers = tuple(DEFAULT_BLOCKED_MARKERS) + tuple(blocked_markers)
    if matches_blocked_marker(haystack, markers):
        return "blocked"
    if not extracted_text.strip():
        return "empty_content"
    return "accepted"


def matches_blocked_marker(text: str, markers: Iterable[str]) -> bool:
    """Match configured block evidence without generic brand or UI-only text."""
    haystack = text.casefold()
    return any(
        marker_text not in _NON_DECISIVE_BLOCKED_MARKERS
        and marker_text in haystack
        for marker in markers
        if (marker_text := str(marker).strip().casefold())
    )


def _is_same_origin_block_redirect(requested_url: str, final_url: str) -> bool:
    try:
        requested = urlsplit(requested_url)
        final = urlsplit(final_url)
        if requested_url == final_url or _origin(requested) != _origin(final):
            return False
    except ValueError:
        return False
    ordered_path_parts = tuple(
        re.sub(r"\.[a-z0-9]+$", "", part.casefold())
        for part in final.path.split("/")
        if part
    )
    if set(ordered_path_parts) & _BLOCKED_PATH_PARTS:
        return True
    return any(
        ordered_path_parts[index : index + len(sequence)] == sequence
        for sequence in _BLOCKED_PATH_SEQUENCES
        for index in range(len(ordered_path_parts) - len(sequence) + 1)
    )


def _origin(parsed) -> tuple[str, str, int | None]:
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port


__all__ = [
    "DEFAULT_BLOCKED_MARKERS",
    "classify_html_capture",
    "matches_blocked_marker",
]
