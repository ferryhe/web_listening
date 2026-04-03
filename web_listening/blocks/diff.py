import difflib
import hashlib
import re
from typing import List, Tuple
from urllib.parse import urljoin, urlparse


def canonicalize_text_for_hash(content: str) -> str:
    """Normalize whitespace and blank lines before hashing."""
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    normalized_lines = []
    blank_pending = False
    for line in lines:
        if not line:
            blank_pending = bool(normalized_lines)
            continue
        if blank_pending and normalized_lines:
            normalized_lines.append("")
        normalized_lines.append(line)
        blank_pending = False
    return "\n".join(normalized_lines).strip()


def compute_hash(content: str) -> str:
    return hashlib.sha256(canonicalize_text_for_hash(content).encode()).hexdigest()


def select_compare_text(*, fit_markdown: str = "", markdown: str = "", content_text: str = "") -> str:
    """Pick the most agent-friendly representation available for comparisons."""
    return (fit_markdown or "").strip() or (markdown or "").strip() or content_text


def compute_diff(old: str, new: str) -> Tuple[bool, str]:
    """Returns (has_changed, diff_snippet)."""
    if compute_hash(old) == compute_hash(new):
        return False, ""
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    snippet = "\n".join(list(diff)[:50])
    return True, snippet


def extract_links(html: str, base_url: str) -> List[str]:
    """Extract all absolute HTTP/HTTPS links from HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    links = []
    for tag in soup.find_all("a", href=True):
        href = urljoin(base_url, tag["href"])
        parsed = urlparse(href)
        if parsed.scheme in ("http", "https"):
            links.append(href)
    return sorted(set(links))


def find_new_links(old_links: List[str], new_links: List[str]) -> List[str]:
    return [lnk for lnk in new_links if lnk not in old_links]


def find_document_links(links: List[str]) -> List[str]:
    """Filter links that point to documents (PDF, DOCX, XLSX, etc.)."""
    DOC_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}
    result = []
    for link in links:
        path = urlparse(link).path.lower()
        if any(path.endswith(ext) for ext in DOC_EXTENSIONS):
            result.append(link)
    return result
