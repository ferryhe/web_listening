import hashlib
import difflib
from typing import List, Tuple
from urllib.parse import urljoin, urlparse


def compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


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
    return list(set(links))


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
