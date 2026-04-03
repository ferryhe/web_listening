from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

_NOISE_TAGS = {"script", "style", "nav", "footer", "noscript"}
_HEADING_LEVELS = {f"h{level}": level for level in range(1, 7)}


@dataclass(slots=True)
class NormalizedContent:
    raw_html: str
    cleaned_html: str
    content_text: str
    markdown: str
    fit_markdown: str
    metadata: dict[str, Any]


def normalize_html(raw_html: str, base_url: str) -> NormalizedContent:
    soup = BeautifulSoup(raw_html, "lxml")

    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    root = soup.body or soup
    cleaned_html = str(root)
    content_text = root.get_text(separator="\n", strip=True)
    markdown = _normalize_markdown(_render_block(root, base_url))
    if not markdown:
        markdown = _normalize_markdown(_text_to_markdown(content_text))

    fit_markdown = _build_fit_markdown(markdown)
    metadata = _build_metadata(root, base_url, content_text)
    return NormalizedContent(
        raw_html=raw_html,
        cleaned_html=cleaned_html,
        content_text=content_text,
        markdown=markdown,
        fit_markdown=fit_markdown,
        metadata=metadata,
    )


def _build_metadata(root: Tag, base_url: str, content_text: str) -> dict[str, Any]:
    headings = []
    for tag_name in _HEADING_LEVELS:
        for heading in root.find_all(tag_name):
            text = heading.get_text(" ", strip=True)
            if text:
                headings.append(text)

    links = []
    for anchor in root.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"])
        links.append(href)

    return {
        "headings": headings[:10],
        "link_count": len(set(links)),
        "word_count": len(content_text.split()),
        "line_count": len([line for line in content_text.splitlines() if line.strip()]),
    }


def _render_block(node: Tag | NavigableString, base_url: str) -> str:
    if isinstance(node, NavigableString):
        return _collapse_inline_whitespace(str(node))

    if not isinstance(node, Tag):
        return ""

    if node.name in _HEADING_LEVELS:
        text = _render_inline(node, base_url)
        if not text:
            return ""
        return f"{'#' * _HEADING_LEVELS[node.name]} {text}"

    if node.name in {"p", "article", "main", "section", "div", "body", "html"}:
        parts = [_render_block(child, base_url) for child in node.children]
        return "\n\n".join(part for part in parts if part)

    if node.name in {"ul", "ol"}:
        parts = [_render_block(child, base_url) for child in node.children]
        return "\n".join(part for part in parts if part)

    if node.name == "li":
        text = _render_inline(node, base_url)
        return f"- {text}" if text else ""

    if node.name == "blockquote":
        text = _render_inline(node, base_url)
        if not text:
            return ""
        return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())

    if node.name == "pre":
        text = node.get_text("\n", strip=False).strip("\n")
        if not text:
            return ""
        return f"```\n{text}\n```"

    if node.name == "table":
        rows = []
        for tr in node.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows)

    text = _render_inline(node, base_url)
    return text


def _render_inline(node: Tag | NavigableString, base_url: str) -> str:
    if isinstance(node, NavigableString):
        return _collapse_inline_whitespace(str(node))

    if not isinstance(node, Tag):
        return ""

    if node.name == "br":
        return "\n"

    if node.name == "a":
        href = node.get("href", "").strip()
        text = _collapse_inline_whitespace("".join(_render_inline(child, base_url) for child in node.children))
        target = urljoin(base_url, href) if href else ""
        if target and text:
            return f"[{text}]({target})"
        return text or target

    if node.name == "img":
        src = node.get("src", "").strip()
        if not src:
            return ""
        alt = node.get("alt", "").strip()
        return f"![{alt}]({urljoin(base_url, src)})"

    if node.name == "code" and node.parent and node.parent.name != "pre":
        text = node.get_text(" ", strip=True)
        return f"`{text}`" if text else ""

    if node.name in {"strong", "b"}:
        text = _collapse_inline_whitespace("".join(_render_inline(child, base_url) for child in node.children))
        return f"**{text}**" if text else ""

    if node.name in {"em", "i"}:
        text = _collapse_inline_whitespace("".join(_render_inline(child, base_url) for child in node.children))
        return f"*{text}*" if text else ""

    parts = [_render_inline(child, base_url) for child in node.children]
    return _collapse_inline_whitespace(" ".join(part for part in parts if part))


def _normalize_markdown(markdown: str) -> str:
    lines = [line.rstrip() for line in markdown.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _build_fit_markdown(markdown: str) -> str:
    result_lines = []
    previous_non_empty = None
    blank_pending = False

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            blank_pending = bool(result_lines)
            continue

        if len(line) == 1 and line in {"|", "-", "_"}:
            continue

        if line == previous_non_empty:
            continue

        if blank_pending and result_lines:
            result_lines.append("")
        result_lines.append(line)
        previous_non_empty = line
        blank_pending = False

    return "\n".join(result_lines).strip()


def _text_to_markdown(content_text: str) -> str:
    paragraphs = [line.strip() for line in content_text.splitlines() if line.strip()]
    return "\n\n".join(paragraphs)


def _collapse_inline_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text.replace("\xa0", " ")).strip()
