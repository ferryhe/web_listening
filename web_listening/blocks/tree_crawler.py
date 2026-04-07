from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web_listening.blocks.crawler import Crawler, resolve_request_headers
from web_listening.blocks.diff import compute_hash, extract_links, find_document_links, select_compare_artifact
from web_listening.blocks.document import DocumentProcessor
from web_listening.blocks.polite import PolitePacer
from web_listening.blocks.storage import Storage
from web_listening.models import CrawlRun, CrawlScope, FileObservation, PageEdge, PageSnapshot, Site, TrackedFile, TrackedPage

_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def sanitize_request_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in _TRACKING_QUERY_KEYS:
            continue
        if any(lowered.startswith(prefix) for prefix in _TRACKING_QUERY_PREFIXES):
            continue
        query_pairs.append((key, value))
    normalized_query = urlencode(sorted(query_pairs))
    normalized_path = parts.path or "/"
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            normalized_path,
            normalized_query,
            "",
        )
    )


def canonicalize_tracked_url(url: str) -> str:
    parts = urlsplit(sanitize_request_url(url))
    if not parts.scheme or not parts.netloc:
        return ""
    normalized_path = parts.path or "/"
    if normalized_path != "/" and normalized_path.endswith("/"):
        normalized_path = normalized_path.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            normalized_path,
            parts.query,
            "",
        )
    )


def get_origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), "", "", ""))


def path_matches_prefixes(path: str, prefixes: list[str]) -> bool:
    if not prefixes:
        return False
    normalized_path = (path or "/").rstrip("/") or "/"
    for prefix in prefixes:
        normalized_prefix = (prefix or "/").rstrip("/") or "/"
        if normalized_prefix == "/":
            return True
        if normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix + "/"):
            return True
    return False


def is_page_url_in_scope(scope: CrawlScope, url: str) -> bool:
    parts = urlsplit(url)
    if get_origin(url) != scope.allowed_origin:
        return False
    return path_matches_prefixes(parts.path or "/", scope.allowed_page_prefixes)


def is_file_url_in_scope(scope: CrawlScope, url: str) -> bool:
    parts = urlsplit(url)
    if get_origin(url) != scope.allowed_origin:
        return False
    return path_matches_prefixes(parts.path or "/", scope.allowed_file_prefixes)


def build_scope_from_site(
    site: Site,
    *,
    max_depth: int = 3,
    max_pages: int = 100,
    max_files: int = 20,
    allowed_page_prefixes: Optional[list[str]] = None,
    allowed_file_prefixes: Optional[list[str]] = None,
) -> CrawlScope:
    seed_url = (site.url or "").strip()
    return CrawlScope(
        site_id=site.id or 0,
        seed_url=seed_url,
        allowed_origin=get_origin(seed_url),
        allowed_page_prefixes=allowed_page_prefixes or ["/"],
        allowed_file_prefixes=allowed_file_prefixes or ["/"],
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
        follow_files=True,
        fetch_mode=site.fetch_mode,
        fetch_config_json=site.fetch_config_json,
    )


@dataclass(slots=True)
class TreeCrawlResult:
    scope: CrawlScope
    run: CrawlRun
    pages: list[TrackedPage]
    files: list[TrackedFile]
    page_failures: list[str] = field(default_factory=list)
    file_failures: list[str] = field(default_factory=list)
    skipped_external_pages: int = 0
    skipped_external_files: int = 0
    skipped_duplicate_pages: int = 0
    skipped_duplicate_files: int = 0
    off_prefix_same_origin_files: int = 0
    new_pages: list[str] = field(default_factory=list)
    changed_pages: list[str] = field(default_factory=list)
    missing_pages: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)


class TreeCrawler:
    def __init__(
        self,
        *,
        storage: Storage,
        crawler: Crawler | None = None,
        document_processor: DocumentProcessor | None = None,
    ):
        self.storage = storage
        self.crawler = crawler or Crawler()
        self.document_processor = document_processor
        self._owns_crawler = crawler is None

    def close(self) -> None:
        if self._owns_crawler:
            self.crawler.close()
        if self.document_processor is not None:
            self.document_processor.close()

    def __enter__(self) -> "TreeCrawler":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def bootstrap_scope(
        self,
        scope: CrawlScope,
        *,
        institution: str = "",
        download_files: bool = True,
    ) -> TreeCrawlResult:
        stored_scope = self.storage.add_crawl_scope(scope) if scope.id is None else self.storage.update_crawl_scope(scope)
        run = self.storage.add_crawl_run(
            CrawlRun(
                scope_id=stored_scope.id,
                run_type="bootstrap",
                status="running",
                started_at=datetime.now(timezone.utc),
            )
        )
        result = TreeCrawlResult(scope=stored_scope, run=run, pages=[], files=[])
        pacer = PolitePacer.from_config(stored_scope.fetch_config_json)
        queued_pages: deque[tuple[str, int, Optional[int]]] = deque([(stored_scope.seed_url, 0, None)])
        seed_canonical_url = canonicalize_tracked_url(stored_scope.seed_url)
        queued_page_urls = {seed_canonical_url}
        processed_page_urls: set[str] = set()
        processed_file_urls: set[str] = set()
        page_id_by_url: dict[str, int] = {}

        try:
            while queued_pages and len(processed_page_urls) < stored_scope.max_pages:
                queued_url, depth, from_page_id = queued_pages.popleft()
                request_url = sanitize_request_url(queued_url) or queued_url
                try:
                    pacer.wait_for_request("page")
                    page = self.crawler.fetch_page(
                        request_url,
                        fetch_mode=stored_scope.fetch_mode,
                        fetch_config_json=stored_scope.fetch_config_json,
                    )
                except Exception as exc:  # pragma: no cover - live failure path
                    result.page_failures.append(f"{request_url}: {type(exc).__name__}: {exc}")
                    continue

                canonical_page_url = canonicalize_tracked_url(page.final_url or request_url)
                if not canonical_page_url:
                    continue
                page_in_scope = is_page_url_in_scope(stored_scope, canonical_page_url)
                entrypoint_only = canonical_page_url == seed_canonical_url and depth == 0 and not page_in_scope
                if not page_in_scope and not entrypoint_only:
                    result.skipped_external_pages += 1
                    continue
                if page_in_scope and canonical_page_url in processed_page_urls:
                    result.skipped_duplicate_pages += 1
                    if from_page_id is not None and canonical_page_url in page_id_by_url:
                        self.storage.add_page_edge(
                            PageEdge(
                                scope_id=stored_scope.id,
                                run_id=run.id,
                                from_page_id=from_page_id,
                                to_page_id=page_id_by_url[canonical_page_url],
                            )
                        )
                    continue

                hash_basis, compare_text = select_compare_artifact(
                    fit_markdown=page.fit_markdown,
                    markdown=page.markdown,
                    content_text=page.content_text,
                )
                page_links = extract_links(page.raw_html, page.final_url or request_url)
                tracked_page = None
                if page_in_scope:
                    tracked_page = self.storage.upsert_tracked_page(
                        scope_id=stored_scope.id,
                        canonical_url=canonical_page_url,
                        depth=depth,
                        run_id=run.id,
                    )
                    snapshot = self.storage.add_page_snapshot(
                        PageSnapshot(
                            scope_id=stored_scope.id,
                            page_id=tracked_page.id,
                            run_id=run.id,
                            captured_at=datetime.now(timezone.utc),
                            content_hash=compute_hash(compare_text),
                            raw_html=page.raw_html,
                            cleaned_html=page.cleaned_html,
                            content_text=page.content_text,
                            markdown=page.markdown,
                            fit_markdown=page.fit_markdown,
                            metadata_json={
                                **page.metadata_json,
                                "hash_basis": hash_basis,
                                "hash_normalization": "whitespace-normalized-v1",
                                "tree_depth": depth,
                            },
                            fetch_mode=stored_scope.fetch_mode,
                            final_url=page.final_url,
                            status_code=page.status_code,
                            links=page_links,
                        )
                    )
                    tracked_page = self.storage.upsert_tracked_page(
                        scope_id=stored_scope.id,
                        canonical_url=canonical_page_url,
                        depth=depth,
                        run_id=run.id,
                        latest_hash=snapshot.content_hash,
                        latest_snapshot_id=snapshot.id,
                    )
                    result.pages.append(tracked_page)
                    processed_page_urls.add(canonical_page_url)
                    page_id_by_url[canonical_page_url] = tracked_page.id

                    if from_page_id is not None:
                        self.storage.add_page_edge(
                            PageEdge(
                                scope_id=stored_scope.id,
                                run_id=run.id,
                                from_page_id=from_page_id,
                                to_page_id=tracked_page.id,
                            )
                        )

                document_links = set(find_document_links(page_links))
                for link in page_links:
                    canonical_link = canonicalize_tracked_url(link)
                    if not canonical_link:
                        continue
                    if link in document_links:
                        if not page_in_scope:
                            continue
                        if not is_file_url_in_scope(stored_scope, canonical_link):
                            result.skipped_external_files += 1
                            continue
                        if canonical_link in processed_file_urls:
                            result.skipped_duplicate_files += 1
                            continue
                        if not stored_scope.follow_files or len(processed_file_urls) >= stored_scope.max_files:
                            continue
                        if not path_matches_prefixes(urlsplit(canonical_link).path or "/", stored_scope.allowed_page_prefixes):
                            result.off_prefix_same_origin_files += 1
                        try:
                            pacer.wait_for_request("file")
                            tracked_file = self._track_file(
                                scope=stored_scope,
                                run=run,
                                page_id=tracked_page.id,
                                file_url=canonical_link,
                                page_url=canonical_page_url,
                                institution=institution or canonical_page_url,
                                download_files=download_files,
                                force_download=False,
                            )
                        except Exception as exc:  # pragma: no cover - live failure path
                            result.file_failures.append(f"{canonical_link}: {type(exc).__name__}: {exc}")
                            continue
                        result.files.append(tracked_file)
                        processed_file_urls.add(canonical_link)
                        continue

                    if not is_page_url_in_scope(stored_scope, canonical_link):
                        result.skipped_external_pages += 1
                        continue
                    if depth >= stored_scope.max_depth:
                        continue
                    if canonical_link in queued_page_urls or canonical_link in processed_page_urls:
                        result.skipped_duplicate_pages += 1
                        continue
                    queued_pages.append((sanitize_request_url(link) or link, depth + 1, tracked_page.id if tracked_page else None))
                    queued_page_urls.add(canonical_link)

            updated_run = self.storage.update_crawl_run(
                run.id,
                status="completed",
                finished_at=datetime.now(timezone.utc),
                pages_seen=len(result.pages),
                files_seen=len(result.files),
            )
            updated_scope = self.storage.update_crawl_scope(
                CrawlScope(
                    **{
                        **stored_scope.model_dump(),
                        "is_initialized": True,
                        "baseline_run_id": updated_run.id,
                    }
                )
            )
            return TreeCrawlResult(
                scope=updated_scope,
                run=updated_run,
                pages=result.pages,
                files=result.files,
                page_failures=result.page_failures,
                file_failures=result.file_failures,
                skipped_external_pages=result.skipped_external_pages,
                skipped_external_files=result.skipped_external_files,
                skipped_duplicate_pages=result.skipped_duplicate_pages,
                skipped_duplicate_files=result.skipped_duplicate_files,
                off_prefix_same_origin_files=result.off_prefix_same_origin_files,
            )
        except Exception as exc:
            failed_run = self.storage.update_crawl_run(
                run.id,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                pages_seen=len(result.pages),
                files_seen=len(result.files),
                error_message=str(exc),
            )
            raise RuntimeError(f"Tree crawl failed for scope {stored_scope.id}: {exc}") from exc

    def run_scope(
        self,
        scope: CrawlScope,
        *,
        institution: str = "",
        download_files: bool = True,
    ) -> TreeCrawlResult:
        if scope.id is None:
            raise ValueError("scope.id must not be None for incremental tree runs")

        stored_scope = self.storage.update_crawl_scope(scope)
        if not stored_scope.is_initialized:
            raise ValueError(
                f"Scope {stored_scope.id} is not initialized. Run bootstrap_scope() first."
            )

        run = self.storage.add_crawl_run(
            CrawlRun(
                scope_id=stored_scope.id,
                run_type="incremental",
                status="running",
                started_at=datetime.now(timezone.utc),
            )
        )
        result = TreeCrawlResult(scope=stored_scope, run=run, pages=[], files=[])
        pacer = PolitePacer.from_config(stored_scope.fetch_config_json)
        queued_pages: deque[tuple[str, int, Optional[int]]] = deque([(stored_scope.seed_url, 0, None)])
        seed_canonical_url = canonicalize_tracked_url(stored_scope.seed_url)
        queued_page_urls = {seed_canonical_url}
        processed_page_urls: set[str] = set()
        processed_file_urls: set[str] = set()
        page_id_by_url: dict[str, int] = {}
        existing_pages = {page.canonical_url: page for page in self.storage.list_tracked_pages(stored_scope.id)}
        existing_files = {tracked_file.canonical_url: tracked_file for tracked_file in self.storage.list_tracked_files(stored_scope.id)}

        try:
            while queued_pages and len(processed_page_urls) < stored_scope.max_pages:
                queued_url, depth, from_page_id = queued_pages.popleft()
                request_url = sanitize_request_url(queued_url) or queued_url
                try:
                    pacer.wait_for_request("page")
                    page = self.crawler.fetch_page(
                        request_url,
                        fetch_mode=stored_scope.fetch_mode,
                        fetch_config_json=stored_scope.fetch_config_json,
                    )
                except Exception as exc:  # pragma: no cover - live failure path
                    result.page_failures.append(f"{request_url}: {type(exc).__name__}: {exc}")
                    continue

                canonical_page_url = canonicalize_tracked_url(page.final_url or request_url)
                if not canonical_page_url:
                    continue
                page_in_scope = is_page_url_in_scope(stored_scope, canonical_page_url)
                entrypoint_only = canonical_page_url == seed_canonical_url and depth == 0 and not page_in_scope
                if not page_in_scope and not entrypoint_only:
                    result.skipped_external_pages += 1
                    continue
                if page_in_scope and canonical_page_url in processed_page_urls:
                    result.skipped_duplicate_pages += 1
                    if from_page_id is not None and canonical_page_url in page_id_by_url:
                        self.storage.add_page_edge(
                            PageEdge(
                                scope_id=stored_scope.id,
                                run_id=run.id,
                                from_page_id=from_page_id,
                                to_page_id=page_id_by_url[canonical_page_url],
                            )
                        )
                    continue

                previous_page = existing_pages.get(canonical_page_url)
                previous_hash = previous_page.latest_hash if previous_page is not None else ""
                hash_basis, compare_text = select_compare_artifact(
                    fit_markdown=page.fit_markdown,
                    markdown=page.markdown,
                    content_text=page.content_text,
                )
                page_links = extract_links(page.raw_html, page.final_url or request_url)
                tracked_page = None
                if page_in_scope:
                    tracked_page = self.storage.upsert_tracked_page(
                        scope_id=stored_scope.id,
                        canonical_url=canonical_page_url,
                        depth=depth,
                        run_id=run.id,
                    )
                    snapshot = self.storage.add_page_snapshot(
                        PageSnapshot(
                            scope_id=stored_scope.id,
                            page_id=tracked_page.id,
                            run_id=run.id,
                            captured_at=datetime.now(timezone.utc),
                            content_hash=compute_hash(compare_text),
                            raw_html=page.raw_html,
                            cleaned_html=page.cleaned_html,
                            content_text=page.content_text,
                            markdown=page.markdown,
                            fit_markdown=page.fit_markdown,
                            metadata_json={
                                **page.metadata_json,
                                "hash_basis": hash_basis,
                                "hash_normalization": "whitespace-normalized-v1",
                                "tree_depth": depth,
                            },
                            fetch_mode=stored_scope.fetch_mode,
                            final_url=page.final_url,
                            status_code=page.status_code,
                            links=page_links,
                        )
                    )
                    tracked_page = self.storage.upsert_tracked_page(
                        scope_id=stored_scope.id,
                        canonical_url=canonical_page_url,
                        depth=depth,
                        run_id=run.id,
                        latest_hash=snapshot.content_hash,
                        latest_snapshot_id=snapshot.id,
                    )
                    result.pages.append(tracked_page)
                    processed_page_urls.add(canonical_page_url)
                    page_id_by_url[canonical_page_url] = tracked_page.id
                    if previous_page is None:
                        result.new_pages.append(canonical_page_url)
                    elif previous_hash and previous_hash != snapshot.content_hash:
                        result.changed_pages.append(canonical_page_url)

                    if from_page_id is not None:
                        self.storage.add_page_edge(
                            PageEdge(
                                scope_id=stored_scope.id,
                                run_id=run.id,
                                from_page_id=from_page_id,
                                to_page_id=tracked_page.id,
                            )
                        )

                document_links = set(find_document_links(page_links))
                for link in page_links:
                    canonical_link = canonicalize_tracked_url(link)
                    if not canonical_link:
                        continue
                    if link in document_links:
                        if not page_in_scope:
                            continue
                        if not is_file_url_in_scope(stored_scope, canonical_link):
                            result.skipped_external_files += 1
                            continue
                        if canonical_link in processed_file_urls:
                            result.skipped_duplicate_files += 1
                            continue
                        if not stored_scope.follow_files or len(processed_file_urls) >= stored_scope.max_files:
                            continue
                        previous_file = existing_files.get(canonical_link)
                        previous_sha256 = previous_file.latest_sha256 if previous_file is not None else ""
                        if not path_matches_prefixes(urlsplit(canonical_link).path or "/", stored_scope.allowed_page_prefixes):
                            result.off_prefix_same_origin_files += 1
                        try:
                            pacer.wait_for_request("file")
                            tracked_file = self._track_file(
                                scope=stored_scope,
                                run=run,
                                page_id=tracked_page.id,
                                file_url=canonical_link,
                                page_url=canonical_page_url,
                                institution=institution or canonical_page_url,
                                download_files=download_files,
                                force_download=True,
                            )
                        except Exception as exc:  # pragma: no cover - live failure path
                            result.file_failures.append(f"{canonical_link}: {type(exc).__name__}: {exc}")
                            continue
                        result.files.append(tracked_file)
                        processed_file_urls.add(canonical_link)
                        if previous_file is None:
                            result.new_files.append(canonical_link)
                        elif (
                            previous_sha256
                            and tracked_file.latest_sha256
                            and previous_sha256 != tracked_file.latest_sha256
                        ):
                            result.changed_files.append(canonical_link)
                        continue

                    if not is_page_url_in_scope(stored_scope, canonical_link):
                        result.skipped_external_pages += 1
                        continue
                    if depth >= stored_scope.max_depth:
                        continue
                    if canonical_link in queued_page_urls or canonical_link in processed_page_urls:
                        result.skipped_duplicate_pages += 1
                        continue
                    queued_pages.append((sanitize_request_url(link) or link, depth + 1, tracked_page.id if tracked_page else None))
                    queued_page_urls.add(canonical_link)

            result.missing_pages = sorted(
                url for url, page in existing_pages.items() if page.is_active and url not in processed_page_urls
            )
            result.missing_files = sorted(
                url for url, tracked_file in existing_files.items() if tracked_file.is_active and url not in processed_file_urls
            )
            updated_run = self.storage.update_crawl_run(
                run.id,
                status="completed",
                finished_at=datetime.now(timezone.utc),
                pages_seen=len(result.pages),
                files_seen=len(result.files),
                pages_changed=len(result.new_pages) + len(result.changed_pages) + len(result.missing_pages),
                files_changed=len(result.new_files) + len(result.changed_files) + len(result.missing_files),
            )
            return TreeCrawlResult(
                scope=stored_scope,
                run=updated_run,
                pages=result.pages,
                files=result.files,
                page_failures=result.page_failures,
                file_failures=result.file_failures,
                skipped_external_pages=result.skipped_external_pages,
                skipped_external_files=result.skipped_external_files,
                skipped_duplicate_pages=result.skipped_duplicate_pages,
                skipped_duplicate_files=result.skipped_duplicate_files,
                off_prefix_same_origin_files=result.off_prefix_same_origin_files,
                new_pages=result.new_pages,
                changed_pages=result.changed_pages,
                missing_pages=result.missing_pages,
                new_files=result.new_files,
                changed_files=result.changed_files,
                missing_files=result.missing_files,
            )
        except Exception as exc:
            self.storage.update_crawl_run(
                run.id,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                pages_seen=len(result.pages),
                files_seen=len(result.files),
                pages_changed=len(result.new_pages) + len(result.changed_pages) + len(result.missing_pages),
                files_changed=len(result.new_files) + len(result.changed_files) + len(result.missing_files),
                error_message=str(exc),
            )
            raise RuntimeError(f"Incremental tree crawl failed for scope {stored_scope.id}: {exc}") from exc

    def _track_file(
        self,
        *,
        scope: CrawlScope,
        run: CrawlRun,
        page_id: int,
        file_url: str,
        page_url: str,
        institution: str,
        download_files: bool,
        force_download: bool = False,
    ) -> TrackedFile:
        request_headers = resolve_request_headers(scope.fetch_config_json)
        latest_document_id = None
        latest_sha256 = ""
        tracked_local_path = ""

        if download_files and self.document_processor is not None:
            document = self.document_processor.process(
                file_url,
                site_id=scope.site_id,
                institution=institution,
                page_url=page_url,
                request_headers=request_headers,
                force_download=force_download,
            )
            persisted = self.storage.add_document(document)
            latest_document_id = persisted.id
            latest_sha256 = persisted.sha256
            tracked_local_path = str(
                self.document_processor.materialize_tracked_view(
                    canonical_local_path=persisted.local_path,
                    page_url=page_url,
                    file_url=file_url,
                    sha256=persisted.sha256,
                    content_type=persisted.content_type,
                )
            )

        tracked_file = self.storage.upsert_tracked_file(
            scope_id=scope.id,
            canonical_url=file_url,
            run_id=run.id,
            latest_document_id=latest_document_id,
            latest_sha256=latest_sha256,
        )
        self.storage.add_file_observation(
            FileObservation(
                scope_id=scope.id,
                run_id=run.id,
                page_id=page_id,
                file_id=tracked_file.id,
                document_id=latest_document_id,
                discovered_url=file_url,
                download_url=file_url,
                tracked_local_path=tracked_local_path,
            )
        )
        return tracked_file
