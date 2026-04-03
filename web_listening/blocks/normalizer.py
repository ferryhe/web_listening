from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

_NOISE_TAGS = {"script", "style", "nav", "footer", "noscript", "header", "aside"}
_HEADING_LEVELS = {f"h{level}": level for level in range(1, 7)}
_POSITIVE_ROOT_HINTS = ("content", "main", "article", "post", "entry", "story", "page")
_NEGATIVE_ROOT_HINTS = ("nav", "menu", "footer", "header", "cookie", "banner", "breadcrumb", "modal", "popup", "share", "social")


@dataclass(slots=True)
class NormalizedContent:
    raw_html: str
    cleaned_html: str
    content_text: str
    markdown: str
    fit_markdown: str
    metadata: dict[str, Any]


def normalize_html(raw_html: str, base_url: str) -> NormalizedContent:
    if _looks_like_xml_document(raw_html):
        return _normalize_xml_document(raw_html)

    soup = BeautifulSoup(raw_html, "lxml")

    for tag in soup(_NOISE_TAGS):
        tag.decompose()

    root = _select_content_root(soup)
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


def _looks_like_xml_document(raw_text: str) -> bool:
    sample = (raw_text or "").lstrip()[:256].lower()
    return sample.startswith("<?xml") or "<rss" in sample or "<feed" in sample or "<urlset" in sample or "<sitemapindex" in sample


def _normalize_xml_document(raw_xml: str) -> NormalizedContent:
    soup = BeautifulSoup(raw_xml, "xml")

    if soup.find("rss") or soup.find("channel") or soup.find("feed"):
        return _normalize_feed_xml(raw_xml, soup)
    if soup.find("urlset") or soup.find("sitemapindex"):
        return _normalize_sitemap_xml(raw_xml, soup)
    return _normalize_generic_xml(raw_xml, soup)


def _normalize_feed_xml(raw_xml: str, soup: BeautifulSoup) -> NormalizedContent:
    channel = soup.find("channel")
    feed = soup.find("feed")
    title = ""
    if channel and channel.find("title"):
        title = channel.find("title").get_text(" ", strip=True)
    elif feed and feed.find("title"):
        title = feed.find("title").get_text(" ", strip=True)

    entries = soup.find_all("item") or soup.find_all("entry")
    lines: list[str] = [f"# {title or 'Feed'}"]
    content_parts: list[str] = [title] if title else []
    links: list[str] = []

    for entry in entries[:100]:
        entry_title = ""
        if entry.find("title"):
            entry_title = entry.find("title").get_text(" ", strip=True)
        link_value = ""
        link_tag = entry.find("link")
        if link_tag is not None:
            link_value = (link_tag.get("href") or link_tag.get_text(" ", strip=True) or "").strip()
        date_value = ""
        for field_name in ("pubDate", "updated", "published", "lastBuildDate"):
            date_tag = entry.find(field_name)
            if date_tag:
                date_value = date_tag.get_text(" ", strip=True)
                break
        line = "- "
        if entry_title and link_value:
            line += f"[{entry_title}]({link_value})"
        else:
            line += entry_title or link_value or "untitled item"
        if date_value:
            line += f" ({date_value})"
        lines.append(line)
        content_parts.extend(part for part in (entry_title, date_value, link_value) if part)
        if link_value.startswith(("http://", "https://")):
            links.append(link_value)

    markdown = _normalize_markdown("\n\n".join(lines))
    fit_markdown = _build_fit_markdown(markdown)
    content_text = "\n".join(part for part in content_parts if part).strip()
    metadata = {
        "headings": [title] if title else [],
        "link_count": len(set(links)),
        "word_count": len(content_text.split()),
        "line_count": len([line for line in content_text.splitlines() if line.strip()]),
        "source_kind": "xml_feed",
        "item_count": len(entries),
    }
    return NormalizedContent(
        raw_html=raw_xml,
        cleaned_html=str(channel or feed or soup),
        content_text=content_text,
        markdown=markdown,
        fit_markdown=fit_markdown,
        metadata=metadata,
    )


def _normalize_sitemap_xml(raw_xml: str, soup: BeautifulSoup) -> NormalizedContent:
    url_entries = soup.find_all("url")
    sitemap_entries = soup.find_all("sitemap")
    entries = url_entries or sitemap_entries
    title = "Sitemap"
    lines: list[str] = [f"# {title}"]
    content_parts: list[str] = []
    links: list[str] = []

    for entry in entries[:500]:
        loc = entry.find("loc").get_text(" ", strip=True) if entry.find("loc") else ""
        lastmod = entry.find("lastmod").get_text(" ", strip=True) if entry.find("lastmod") else ""
        line = "- "
        if loc:
            line += f"[{loc}]({loc})"
            links.append(loc)
        else:
            line += "unknown location"
        if lastmod:
            line += f" ({lastmod})"
        lines.append(line)
        content_parts.extend(part for part in (loc, lastmod) if part)

    markdown = _normalize_markdown("\n\n".join(lines))
    fit_markdown = _build_fit_markdown(markdown)
    content_text = "\n".join(part for part in content_parts if part).strip()
    metadata = {
        "headings": [title],
        "link_count": len(set(links)),
        "word_count": len(content_text.split()),
        "line_count": len([line for line in content_text.splitlines() if line.strip()]),
        "source_kind": "xml_sitemap",
        "item_count": len(entries),
    }
    return NormalizedContent(
        raw_html=raw_xml,
        cleaned_html=str(soup.find("urlset") or soup.find("sitemapindex") or soup),
        content_text=content_text,
        markdown=markdown,
        fit_markdown=fit_markdown,
        metadata=metadata,
    )


def _normalize_generic_xml(raw_xml: str, soup: BeautifulSoup) -> NormalizedContent:
    content_text = soup.get_text("\n", strip=True)
    markdown = _normalize_markdown(_text_to_markdown(content_text))
    fit_markdown = _build_fit_markdown(markdown)
    metadata = {
        "headings": [],
        "link_count": 0,
        "word_count": len(content_text.split()),
        "line_count": len([line for line in content_text.splitlines() if line.strip()]),
        "source_kind": "xml_generic",
    }
    return NormalizedContent(
        raw_html=raw_xml,
        cleaned_html=str(soup),
        content_text=content_text,
        markdown=markdown,
        fit_markdown=fit_markdown,
        metadata=metadata,
    )


def _select_content_root(soup: BeautifulSoup) -> Tag:
    candidates: list[Tag] = []
    seen: set[int] = set()

    def add_candidate(tag: Tag | None) -> None:
        if tag is None:
            return
        identity = id(tag)
        if identity in seen:
            return
        seen.add(identity)
        candidates.append(tag)

    for tag in soup.find_all("main"):
        add_candidate(tag)
    for tag in soup.find_all("article"):
        add_candidate(tag)
    add_candidate(soup.find(id="content"))
    for tag in soup.find_all(attrs={"role": "main"}):
        add_candidate(tag)

    for tag in soup.find_all(True):
        signature = _tag_signature(tag)
        if any(hint in signature for hint in _POSITIVE_ROOT_HINTS):
            add_candidate(tag)

    add_candidate(soup.body)

    scored = [(_score_root_candidate(tag), tag) for tag in candidates]
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else (soup.body or soup)


def _score_root_candidate(tag: Tag) -> tuple[int, int, int, int]:
    text = tag.get_text(separator=" ", strip=True)
    word_count = len(text.split())
    link_count = len({anchor.get("href", "").strip() for anchor in tag.find_all("a", href=True)})
    heading_count = sum(
        1
        for tag_name in _HEADING_LEVELS
        for heading in tag.find_all(tag_name)
        if heading.get_text(" ", strip=True)
    )
    signature = _tag_signature(tag)

    hint_bonus = 0
    if tag.name in {"main", "article"}:
        hint_bonus += 80
    if tag.get("role") == "main":
        hint_bonus += 60
    if any(hint in signature for hint in _POSITIVE_ROOT_HINTS):
        hint_bonus += 40
    if tag.name == "body":
        hint_bonus -= 80
    if any(hint in signature for hint in _NEGATIVE_ROOT_HINTS):
        hint_bonus -= 160
    if word_count < 20 and link_count <= 2:
        hint_bonus -= 120

    score = word_count + min(link_count, 80) + (heading_count * 25) + hint_bonus
    return score, word_count, heading_count, -link_count


def _tag_signature(tag: Tag) -> str:
    classes = " ".join(tag.get("class", []))
    return f"{tag.name} {tag.get('id', '')} {classes}".lower()


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
