import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from web_listening.models import (
    AnalysisReport,
    Change,
    CrawlRun,
    CrawlScope,
    Document,
    FileObservation,
    PageEdge,
    PageSnapshot,
    Site,
    SiteSnapshot,
    TrackedFile,
    TrackedPage,
)


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    from dateutil import parser as dtparser
    return dtparser.parse(value)


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                name TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                fetch_mode TEXT DEFAULT 'http',
                fetch_config_json TEXT DEFAULT '{}',
                created_at TEXT,
                last_checked_at TEXT,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS site_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                captured_at TEXT,
                content_hash TEXT NOT NULL,
                raw_html TEXT DEFAULT '',
                cleaned_html TEXT DEFAULT '',
                content_text TEXT DEFAULT '',
                markdown TEXT DEFAULT '',
                fit_markdown TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                fetch_mode TEXT DEFAULT 'http',
                final_url TEXT DEFAULT '',
                status_code INTEGER,
                links TEXT DEFAULT '[]',
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                detected_at TEXT,
                change_type TEXT NOT NULL,
                summary TEXT DEFAULT '',
                diff_snippet TEXT DEFAULT '',
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                title TEXT DEFAULT '',
                url TEXT NOT NULL,
                download_url TEXT NOT NULL,
                institution TEXT DEFAULT '',
                page_url TEXT DEFAULT '',
                published_at TEXT,
                downloaded_at TEXT,
                local_path TEXT DEFAULT '',
                doc_type TEXT DEFAULT '',
                sha256 TEXT DEFAULT '',
                file_size INTEGER,
                content_type TEXT DEFAULT '',
                etag TEXT DEFAULT '',
                last_modified TEXT DEFAULT '',
                content_md TEXT DEFAULT '',
                content_md_status TEXT DEFAULT 'pending',
                content_md_updated_at TEXT,
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS document_blobs (
                sha256 TEXT PRIMARY KEY,
                canonical_path TEXT NOT NULL,
                file_size INTEGER,
                content_type TEXT DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS analysis_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                generated_at TEXT,
                site_ids TEXT DEFAULT '[]',
                summary_md TEXT DEFAULT '',
                change_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS crawl_scopes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                seed_url TEXT NOT NULL,
                allowed_origin TEXT NOT NULL,
                allowed_page_prefixes_json TEXT DEFAULT '[]',
                allowed_file_prefixes_json TEXT DEFAULT '[]',
                max_depth INTEGER DEFAULT 3,
                max_pages INTEGER DEFAULT 100,
                max_files INTEGER DEFAULT 20,
                follow_files INTEGER DEFAULT 1,
                fetch_mode TEXT DEFAULT 'http',
                fetch_config_json TEXT DEFAULT '{}',
                is_initialized INTEGER DEFAULT 0,
                baseline_run_id INTEGER,
                created_at TEXT,
                updated_at TEXT,
                FOREIGN KEY (site_id) REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                run_type TEXT DEFAULT 'bootstrap',
                status TEXT DEFAULT 'queued',
                started_at TEXT,
                finished_at TEXT,
                pages_seen INTEGER DEFAULT 0,
                files_seen INTEGER DEFAULT 0,
                pages_changed INTEGER DEFAULT 0,
                files_changed INTEGER DEFAULT 0,
                error_message TEXT DEFAULT '',
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id)
            );

            CREATE TABLE IF NOT EXISTS tracked_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                canonical_url TEXT NOT NULL,
                depth INTEGER DEFAULT 0,
                first_seen_run_id INTEGER,
                last_seen_run_id INTEGER,
                miss_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                latest_snapshot_id INTEGER,
                latest_hash TEXT DEFAULT '',
                UNIQUE(scope_id, canonical_url),
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id)
            );

            CREATE TABLE IF NOT EXISTS page_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                page_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                captured_at TEXT,
                content_hash TEXT NOT NULL,
                raw_html TEXT DEFAULT '',
                cleaned_html TEXT DEFAULT '',
                content_text TEXT DEFAULT '',
                markdown TEXT DEFAULT '',
                fit_markdown TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                fetch_mode TEXT DEFAULT 'http',
                final_url TEXT DEFAULT '',
                status_code INTEGER,
                links TEXT DEFAULT '[]',
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
            );

            CREATE TABLE IF NOT EXISTS page_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                from_page_id INTEGER NOT NULL,
                to_page_id INTEGER NOT NULL,
                UNIQUE(scope_id, run_id, from_page_id, to_page_id),
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id),
                FOREIGN KEY (from_page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (to_page_id) REFERENCES tracked_pages(id)
            );

            CREATE TABLE IF NOT EXISTS tracked_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                canonical_url TEXT NOT NULL,
                first_seen_run_id INTEGER,
                last_seen_run_id INTEGER,
                miss_count INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                latest_document_id INTEGER,
                latest_sha256 TEXT DEFAULT '',
                UNIQUE(scope_id, canonical_url),
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (latest_document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS file_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                page_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                discovered_url TEXT NOT NULL,
                download_url TEXT NOT NULL,
                tracked_local_path TEXT DEFAULT '',
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id),
                FOREIGN KEY (page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (file_id) REFERENCES tracked_files(id)
            );
        """)
        self._ensure_column("sites", "fetch_mode", "TEXT DEFAULT 'http'")
        self._ensure_column("sites", "fetch_config_json", "TEXT DEFAULT '{}'")
        self._ensure_column("site_snapshots", "raw_html", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "cleaned_html", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "markdown", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "fit_markdown", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "metadata_json", "TEXT DEFAULT '{}'")
        self._ensure_column("site_snapshots", "fetch_mode", "TEXT DEFAULT 'http'")
        self._ensure_column("site_snapshots", "final_url", "TEXT DEFAULT ''")
        self._ensure_column("site_snapshots", "status_code", "INTEGER")
        self._ensure_column("documents", "sha256", "TEXT DEFAULT ''")
        self._ensure_column("documents", "file_size", "INTEGER")
        self._ensure_column("documents", "content_type", "TEXT DEFAULT ''")
        self._ensure_column("documents", "etag", "TEXT DEFAULT ''")
        self._ensure_column("documents", "last_modified", "TEXT DEFAULT ''")
        self._ensure_column("documents", "content_md_status", "TEXT DEFAULT 'pending'")
        self._ensure_column("documents", "content_md_updated_at", "TEXT")
        self._ensure_column("file_observations", "tracked_local_path", "TEXT DEFAULT ''")
        self.conn.commit()

    def _ensure_column(self, table_name: str, column_name: str, column_sql: str):
        rows = self.conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name not in existing:
            self.conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def close(self):
        self.conn.close()

    # ── Sites ──────────────────────────────────────────────────────────────

    def add_site(self, site: Site) -> Site:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO sites (url, name, tags, fetch_mode, fetch_config_json, created_at, last_checked_at, is_active) VALUES (?,?,?,?,?,?,?,?)",
            (
                site.url,
                site.name,
                json.dumps(site.tags),
                site.fetch_mode,
                json.dumps(site.fetch_config_json),
                site.created_at.isoformat() if site.created_at else now,
                site.last_checked_at.isoformat() if site.last_checked_at else None,
                int(site.is_active),
            ),
        )
        self.conn.commit()
        return self.get_site(cur.lastrowid)

    def get_site(self, site_id: int) -> Optional[Site]:
        row = self.conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        if row is None:
            return None
        return Site(
            id=row["id"],
            url=row["url"],
            name=row["name"] or "",
            tags=json.loads(row["tags"] or "[]"),
            fetch_mode=row["fetch_mode"] or "http",
            fetch_config_json=json.loads(row["fetch_config_json"] or "{}"),
            created_at=_parse_dt(row["created_at"]),
            last_checked_at=_parse_dt(row["last_checked_at"]),
            is_active=bool(row["is_active"]),
        )

    def list_sites(self, active_only: bool = True) -> List[Site]:
        if active_only:
            rows = self.conn.execute("SELECT * FROM sites WHERE is_active=1").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM sites").fetchall()
        return [
            Site(
                id=r["id"],
                url=r["url"],
                name=r["name"] or "",
                tags=json.loads(r["tags"] or "[]"),
                fetch_mode=r["fetch_mode"] or "http",
                fetch_config_json=json.loads(r["fetch_config_json"] or "{}"),
                created_at=_parse_dt(r["created_at"]),
                last_checked_at=_parse_dt(r["last_checked_at"]),
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    def deactivate_site(self, site_id: int):
        self.conn.execute("UPDATE sites SET is_active=0 WHERE id=?", (site_id,))
        self.conn.commit()

    def update_site_checked(self, site_id: int):
        self.conn.execute(
            "UPDATE sites SET last_checked_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), site_id),
        )
        self.conn.commit()

    # ── Snapshots ──────────────────────────────────────────────────────────

    def add_snapshot(self, snapshot: SiteSnapshot) -> SiteSnapshot:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO site_snapshots (
                   site_id, captured_at, content_hash, raw_html, cleaned_html, content_text,
                   markdown, fit_markdown, metadata_json, fetch_mode, final_url, status_code, links
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.site_id,
                snapshot.captured_at.isoformat() if snapshot.captured_at else now,
                snapshot.content_hash,
                snapshot.raw_html,
                snapshot.cleaned_html,
                snapshot.content_text,
                snapshot.markdown,
                snapshot.fit_markdown,
                json.dumps(snapshot.metadata_json),
                snapshot.fetch_mode,
                snapshot.final_url,
                snapshot.status_code,
                json.dumps(snapshot.links),
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM site_snapshots WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return self._row_to_snapshot(row)

    def get_latest_snapshot(self, site_id: int) -> Optional[SiteSnapshot]:
        row = self.conn.execute(
            "SELECT * FROM site_snapshots WHERE site_id=? ORDER BY captured_at DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_snapshot(row)

    def _row_to_snapshot(self, row) -> SiteSnapshot:
        return SiteSnapshot(
            id=row["id"],
            site_id=row["site_id"],
            captured_at=_parse_dt(row["captured_at"]),
            content_hash=row["content_hash"],
            raw_html=row["raw_html"] or "",
            cleaned_html=row["cleaned_html"] or "",
            content_text=row["content_text"] or "",
            markdown=row["markdown"] or "",
            fit_markdown=row["fit_markdown"] or "",
            metadata_json=json.loads(row["metadata_json"] or "{}"),
            fetch_mode=row["fetch_mode"] or "http",
            final_url=row["final_url"] or "",
            status_code=row["status_code"],
            links=json.loads(row["links"] or "[]"),
        )

    # ── Changes ────────────────────────────────────────────────────────────

    def add_change(self, change: Change) -> Change:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO changes (site_id, detected_at, change_type, summary, diff_snippet) VALUES (?,?,?,?,?)",
            (
                change.site_id,
                change.detected_at.isoformat() if change.detected_at else now,
                change.change_type,
                change.summary,
                change.diff_snippet,
            ),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM changes WHERE id=?", (cur.lastrowid,)).fetchone()
        return Change(
            id=row["id"],
            site_id=row["site_id"],
            detected_at=_parse_dt(row["detected_at"]),
            change_type=row["change_type"],
            summary=row["summary"] or "",
            diff_snippet=row["diff_snippet"] or "",
        )

    def list_changes(
        self, site_id: Optional[int] = None, since: Optional[datetime] = None
    ) -> List[Change]:
        query = "SELECT * FROM changes WHERE 1=1"
        params = []
        if site_id is not None:
            query += " AND site_id=?"
            params.append(site_id)
        if since is not None:
            query += " AND detected_at>=?"
            params.append(since.isoformat())
        query += " ORDER BY detected_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [
            Change(
                id=r["id"],
                site_id=r["site_id"],
                detected_at=_parse_dt(r["detected_at"]),
                change_type=r["change_type"],
                summary=r["summary"] or "",
                diff_snippet=r["diff_snippet"] or "",
            )
            for r in rows
        ]

    # ── Documents ──────────────────────────────────────────────────────────

    def add_document(self, doc: Document) -> Document:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.conn.execute(
            "SELECT id FROM documents WHERE download_url = ? ORDER BY id ASC LIMIT 1",
            (doc.download_url,),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """INSERT INTO documents
                   (site_id, title, url, download_url, institution, page_url,
                    published_at, downloaded_at, local_path, doc_type, sha256,
                    file_size, content_type, etag, last_modified, content_md,
                    content_md_status, content_md_updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    doc.site_id,
                    doc.title,
                    doc.url,
                    doc.download_url,
                    doc.institution,
                    doc.page_url,
                    doc.published_at.isoformat() if doc.published_at else None,
                    doc.downloaded_at.isoformat() if doc.downloaded_at else now,
                    doc.local_path,
                    doc.doc_type,
                    doc.sha256,
                    doc.file_size,
                    doc.content_type,
                    doc.etag,
                    doc.last_modified,
                    doc.content_md,
                    doc.content_md_status,
                    doc.content_md_updated_at.isoformat() if doc.content_md_updated_at else None,
                ),
            )
            row_id = cur.lastrowid
        else:
            row_id = existing["id"]
            self.conn.execute(
                """UPDATE documents
                   SET site_id = ?,
                       title = ?,
                       url = ?,
                       institution = ?,
                       page_url = ?,
                       published_at = ?,
                       downloaded_at = ?,
                       local_path = ?,
                       doc_type = ?,
                       sha256 = ?,
                       file_size = ?,
                       content_type = ?,
                       etag = ?,
                       last_modified = ?,
                       content_md = ?,
                       content_md_status = ?,
                       content_md_updated_at = ?
                   WHERE id = ?""",
                (
                    doc.site_id,
                    doc.title,
                    doc.url,
                    doc.institution,
                    doc.page_url,
                    doc.published_at.isoformat() if doc.published_at else None,
                    doc.downloaded_at.isoformat() if doc.downloaded_at else now,
                    doc.local_path,
                    doc.doc_type,
                    doc.sha256,
                    doc.file_size,
                    doc.content_type,
                    doc.etag,
                    doc.last_modified,
                    doc.content_md,
                    doc.content_md_status,
                    doc.content_md_updated_at.isoformat() if doc.content_md_updated_at else None,
                    row_id,
                ),
            )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id=?", (row_id,)
        ).fetchone()
        return self._row_to_document(row)

    def _row_to_document(self, row) -> Document:
        return Document(
            id=row["id"],
            site_id=row["site_id"],
            title=row["title"] or "",
            url=row["url"],
            download_url=row["download_url"],
            institution=row["institution"] or "",
            page_url=row["page_url"] or "",
            published_at=_parse_dt(row["published_at"]),
            downloaded_at=_parse_dt(row["downloaded_at"]),
            local_path=row["local_path"] or "",
            doc_type=row["doc_type"] or "",
            sha256=row["sha256"] or "",
            file_size=row["file_size"],
            content_type=row["content_type"] or "",
            etag=row["etag"] or "",
            last_modified=row["last_modified"] or "",
            content_md=row["content_md"] or "",
            content_md_status=row["content_md_status"] or "pending",
            content_md_updated_at=_parse_dt(row["content_md_updated_at"]),
        )

    def get_document_by_download_url(self, download_url: str) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE download_url = ? ORDER BY id ASC LIMIT 1",
            (download_url,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    def get_document(self, document_id: int) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    def get_document_by_sha256(self, sha256: str) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE sha256 = ? ORDER BY id ASC LIMIT 1",
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    def get_blob(self, sha256: str) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT sha256, canonical_path, file_size, content_type
               FROM document_blobs WHERE sha256 = ?""",
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return {
            "sha256": row["sha256"],
            "canonical_path": row["canonical_path"],
            "file_size": row["file_size"],
            "content_type": row["content_type"] or "",
        }

    def upsert_blob(
        self,
        *,
        sha256: str,
        canonical_path: str,
        file_size: Optional[int],
        content_type: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO document_blobs (
                sha256, canonical_path, file_size, content_type, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                canonical_path = excluded.canonical_path,
                file_size = excluded.file_size,
                content_type = excluded.content_type,
                last_seen_at = excluded.last_seen_at
            """,
            (sha256, canonical_path, file_size, content_type, now, now),
        )
        self.conn.commit()

    def list_documents(
        self, site_id: Optional[int] = None, institution: Optional[str] = None
    ) -> List[Document]:
        query = "SELECT * FROM documents WHERE 1=1"
        params = []
        if site_id is not None:
            query += " AND site_id=?"
            params.append(site_id)
        if institution is not None:
            query += " AND institution=?"
            params.append(institution)
        query += " ORDER BY downloaded_at DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_document(r) for r in rows]

    def update_document_content_md(
        self,
        document_id: int,
        *,
        content_md: str,
        content_md_status: str = "converted",
    ) -> Optional[Document]:
        existing = self.get_document(document_id)
        if existing is None:
            return None

        updated_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE documents
            SET content_md = ?,
                content_md_status = ?,
                content_md_updated_at = ?
            WHERE id = ?
            """,
            (content_md, content_md_status, updated_at, document_id),
        )
        self.conn.commit()
        return self.get_document(document_id)

    # ── Analyses ───────────────────────────────────────────────────────────

    def add_analysis(self, report: AnalysisReport) -> AnalysisReport:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """INSERT INTO analysis_reports
               (period_start, period_end, generated_at, site_ids, summary_md, change_count)
               VALUES (?,?,?,?,?,?)""",
            (
                report.period_start.isoformat(),
                report.period_end.isoformat(),
                report.generated_at.isoformat() if report.generated_at else now,
                json.dumps(report.site_ids),
                report.summary_md,
                report.change_count,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM analysis_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return self._row_to_analysis(row)

    def _row_to_analysis(self, row) -> AnalysisReport:
        return AnalysisReport(
            id=row["id"],
            period_start=_parse_dt(row["period_start"]),
            period_end=_parse_dt(row["period_end"]),
            generated_at=_parse_dt(row["generated_at"]),
            site_ids=json.loads(row["site_ids"] or "[]"),
            summary_md=row["summary_md"] or "",
            change_count=row["change_count"] or 0,
        )

    def list_analyses(self) -> List[AnalysisReport]:
        rows = self.conn.execute(
            "SELECT * FROM analysis_reports ORDER BY generated_at DESC"
        ).fetchall()
        return [self._row_to_analysis(r) for r in rows]

    # ── Recursive Scopes ──────────────────────────────────────────────────

    def add_crawl_scope(self, scope: CrawlScope) -> CrawlScope:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO crawl_scopes (
                site_id, seed_url, allowed_origin, allowed_page_prefixes_json,
                allowed_file_prefixes_json, max_depth, max_pages, max_files,
                follow_files, fetch_mode, fetch_config_json, is_initialized,
                baseline_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope.site_id,
                scope.seed_url,
                scope.allowed_origin,
                json.dumps(scope.allowed_page_prefixes),
                json.dumps(scope.allowed_file_prefixes),
                scope.max_depth,
                scope.max_pages,
                scope.max_files,
                int(scope.follow_files),
                scope.fetch_mode,
                json.dumps(scope.fetch_config_json),
                int(scope.is_initialized),
                scope.baseline_run_id,
                scope.created_at.isoformat() if scope.created_at else now,
                scope.updated_at.isoformat() if scope.updated_at else now,
            ),
        )
        self.conn.commit()
        return self.get_crawl_scope(cur.lastrowid)

    def update_crawl_scope(self, scope: CrawlScope) -> CrawlScope:
        if scope.id is None:
            raise ValueError("scope.id must not be None when updating a crawl scope")
        updated_at = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            UPDATE crawl_scopes
            SET site_id = ?,
                seed_url = ?,
                allowed_origin = ?,
                allowed_page_prefixes_json = ?,
                allowed_file_prefixes_json = ?,
                max_depth = ?,
                max_pages = ?,
                max_files = ?,
                follow_files = ?,
                fetch_mode = ?,
                fetch_config_json = ?,
                is_initialized = ?,
                baseline_run_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                scope.site_id,
                scope.seed_url,
                scope.allowed_origin,
                json.dumps(scope.allowed_page_prefixes),
                json.dumps(scope.allowed_file_prefixes),
                scope.max_depth,
                scope.max_pages,
                scope.max_files,
                int(scope.follow_files),
                scope.fetch_mode,
                json.dumps(scope.fetch_config_json),
                int(scope.is_initialized),
                scope.baseline_run_id,
                updated_at,
                scope.id,
            ),
        )
        self.conn.commit()
        return self.get_crawl_scope(scope.id)

    def get_crawl_scope(self, scope_id: int) -> Optional[CrawlScope]:
        row = self.conn.execute(
            "SELECT * FROM crawl_scopes WHERE id = ?",
            (scope_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_crawl_scope(row)

    def list_crawl_scopes(self, site_id: Optional[int] = None) -> List[CrawlScope]:
        if site_id is None:
            rows = self.conn.execute("SELECT * FROM crawl_scopes ORDER BY id ASC").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM crawl_scopes WHERE site_id = ? ORDER BY id ASC",
                (site_id,),
            ).fetchall()
        return [self._row_to_crawl_scope(row) for row in rows]

    def _row_to_crawl_scope(self, row) -> CrawlScope:
        return CrawlScope(
            id=row["id"],
            site_id=row["site_id"],
            seed_url=row["seed_url"],
            allowed_origin=row["allowed_origin"] or "",
            allowed_page_prefixes=json.loads(row["allowed_page_prefixes_json"] or "[]"),
            allowed_file_prefixes=json.loads(row["allowed_file_prefixes_json"] or "[]"),
            max_depth=row["max_depth"] or 3,
            max_pages=row["max_pages"] or 100,
            max_files=row["max_files"] or 20,
            follow_files=bool(row["follow_files"]),
            fetch_mode=row["fetch_mode"] or "http",
            fetch_config_json=json.loads(row["fetch_config_json"] or "{}"),
            is_initialized=bool(row["is_initialized"]),
            baseline_run_id=row["baseline_run_id"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def add_crawl_run(self, run: CrawlRun) -> CrawlRun:
        cur = self.conn.execute(
            """
            INSERT INTO crawl_runs (
                scope_id, run_type, status, started_at, finished_at,
                pages_seen, files_seen, pages_changed, files_changed, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.scope_id,
                run.run_type,
                run.status,
                run.started_at.isoformat() if run.started_at else None,
                run.finished_at.isoformat() if run.finished_at else None,
                run.pages_seen,
                run.files_seen,
                run.pages_changed,
                run.files_changed,
                run.error_message,
            ),
        )
        self.conn.commit()
        return self.get_crawl_run(cur.lastrowid)

    def update_crawl_run(self, run_id: int, **fields) -> Optional[CrawlRun]:
        if not fields:
            return self.get_crawl_run(run_id)
        assignments = []
        params = []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if isinstance(value, datetime):
                params.append(value.isoformat())
            else:
                params.append(value)
        params.append(run_id)
        self.conn.execute(
            f"UPDATE crawl_runs SET {', '.join(assignments)} WHERE id = ?",
            params,
        )
        self.conn.commit()
        return self.get_crawl_run(run_id)

    def get_crawl_run(self, run_id: int) -> Optional[CrawlRun]:
        row = self.conn.execute(
            "SELECT * FROM crawl_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_crawl_run(row)

    def _row_to_crawl_run(self, row) -> CrawlRun:
        return CrawlRun(
            id=row["id"],
            scope_id=row["scope_id"],
            run_type=row["run_type"] or "bootstrap",
            status=row["status"] or "queued",
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
            pages_seen=row["pages_seen"] or 0,
            files_seen=row["files_seen"] or 0,
            pages_changed=row["pages_changed"] or 0,
            files_changed=row["files_changed"] or 0,
            error_message=row["error_message"] or "",
        )

    def upsert_tracked_page(
        self,
        *,
        scope_id: int,
        canonical_url: str,
        depth: int,
        run_id: int,
        latest_hash: str = "",
        latest_snapshot_id: Optional[int] = None,
    ) -> TrackedPage:
        existing = self.conn.execute(
            "SELECT * FROM tracked_pages WHERE scope_id = ? AND canonical_url = ?",
            (scope_id, canonical_url),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO tracked_pages (
                    scope_id, canonical_url, depth, first_seen_run_id, last_seen_run_id,
                    miss_count, is_active, latest_snapshot_id, latest_hash
                ) VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?)
                """,
                (
                    scope_id,
                    canonical_url,
                    depth,
                    run_id,
                    run_id,
                    latest_snapshot_id,
                    latest_hash,
                ),
            )
            row_id = cur.lastrowid
        else:
            row_id = existing["id"]
            self.conn.execute(
                """
                UPDATE tracked_pages
                SET depth = ?,
                    last_seen_run_id = ?,
                    miss_count = 0,
                    is_active = 1,
                    latest_snapshot_id = COALESCE(?, latest_snapshot_id),
                    latest_hash = CASE WHEN ? <> '' THEN ? ELSE latest_hash END
                WHERE id = ?
                """,
                (
                    depth,
                    run_id,
                    latest_snapshot_id,
                    latest_hash,
                    latest_hash,
                    row_id,
                ),
            )
        self.conn.commit()
        return self.get_tracked_page(row_id)

    def get_tracked_page(self, page_id: int) -> Optional[TrackedPage]:
        row = self.conn.execute(
            "SELECT * FROM tracked_pages WHERE id = ?",
            (page_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tracked_page(row)

    def list_tracked_pages(self, scope_id: int) -> List[TrackedPage]:
        rows = self.conn.execute(
            "SELECT * FROM tracked_pages WHERE scope_id = ? ORDER BY canonical_url ASC",
            (scope_id,),
        ).fetchall()
        return [self._row_to_tracked_page(row) for row in rows]

    def _row_to_tracked_page(self, row) -> TrackedPage:
        return TrackedPage(
            id=row["id"],
            scope_id=row["scope_id"],
            canonical_url=row["canonical_url"],
            depth=row["depth"] or 0,
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            miss_count=row["miss_count"] or 0,
            is_active=bool(row["is_active"]),
            latest_snapshot_id=row["latest_snapshot_id"],
            latest_hash=row["latest_hash"] or "",
        )

    def add_page_snapshot(self, snapshot: PageSnapshot) -> PageSnapshot:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO page_snapshots (
                scope_id, page_id, run_id, captured_at, content_hash, raw_html, cleaned_html,
                content_text, markdown, fit_markdown, metadata_json, fetch_mode,
                final_url, status_code, links
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.scope_id,
                snapshot.page_id,
                snapshot.run_id,
                snapshot.captured_at.isoformat() if snapshot.captured_at else now,
                snapshot.content_hash,
                snapshot.raw_html,
                snapshot.cleaned_html,
                snapshot.content_text,
                snapshot.markdown,
                snapshot.fit_markdown,
                json.dumps(snapshot.metadata_json),
                snapshot.fetch_mode,
                snapshot.final_url,
                snapshot.status_code,
                json.dumps(snapshot.links),
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return self._row_to_page_snapshot(row)

    def list_page_snapshots(self, page_id: int) -> List[PageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE page_id = ? ORDER BY captured_at DESC",
            (page_id,),
        ).fetchall()
        return [self._row_to_page_snapshot(row) for row in rows]

    def list_page_snapshots_for_run(self, scope_id: int, run_id: int) -> List[PageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE scope_id = ? AND run_id = ? ORDER BY id ASC",
            (scope_id, run_id),
        ).fetchall()
        return [self._row_to_page_snapshot(row) for row in rows]

    def _row_to_page_snapshot(self, row) -> PageSnapshot:
        return PageSnapshot(
            id=row["id"],
            scope_id=row["scope_id"],
            page_id=row["page_id"],
            run_id=row["run_id"],
            captured_at=_parse_dt(row["captured_at"]),
            content_hash=row["content_hash"],
            raw_html=row["raw_html"] or "",
            cleaned_html=row["cleaned_html"] or "",
            content_text=row["content_text"] or "",
            markdown=row["markdown"] or "",
            fit_markdown=row["fit_markdown"] or "",
            metadata_json=json.loads(row["metadata_json"] or "{}"),
            fetch_mode=row["fetch_mode"] or "http",
            final_url=row["final_url"] or "",
            status_code=row["status_code"],
            links=json.loads(row["links"] or "[]"),
        )

    def add_page_edge(self, edge: PageEdge) -> PageEdge:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO page_edges (
                scope_id, run_id, from_page_id, to_page_id
            ) VALUES (?, ?, ?, ?)
            """,
            (
                edge.scope_id,
                edge.run_id,
                edge.from_page_id,
                edge.to_page_id,
            ),
        )
        self.conn.commit()
        if cur.lastrowid:
            row_id = cur.lastrowid
        else:
            row = self.conn.execute(
                """
                SELECT * FROM page_edges
                WHERE scope_id = ? AND run_id = ? AND from_page_id = ? AND to_page_id = ?
                """,
                (edge.scope_id, edge.run_id, edge.from_page_id, edge.to_page_id),
            ).fetchone()
            row_id = row["id"]
        row = self.conn.execute("SELECT * FROM page_edges WHERE id = ?", (row_id,)).fetchone()
        return self._row_to_page_edge(row)

    def list_page_edges(self, scope_id: int, run_id: Optional[int] = None) -> List[PageEdge]:
        if run_id is None:
            rows = self.conn.execute(
                "SELECT * FROM page_edges WHERE scope_id = ? ORDER BY id ASC",
                (scope_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM page_edges WHERE scope_id = ? AND run_id = ? ORDER BY id ASC",
                (scope_id, run_id),
            ).fetchall()
        return [self._row_to_page_edge(row) for row in rows]

    def _row_to_page_edge(self, row) -> PageEdge:
        return PageEdge(
            id=row["id"],
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            from_page_id=row["from_page_id"],
            to_page_id=row["to_page_id"],
        )

    def upsert_tracked_file(
        self,
        *,
        scope_id: int,
        canonical_url: str,
        run_id: int,
        latest_document_id: Optional[int] = None,
        latest_sha256: str = "",
    ) -> TrackedFile:
        existing = self.conn.execute(
            "SELECT * FROM tracked_files WHERE scope_id = ? AND canonical_url = ?",
            (scope_id, canonical_url),
        ).fetchone()
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO tracked_files (
                    scope_id, canonical_url, first_seen_run_id, last_seen_run_id,
                    miss_count, is_active, latest_document_id, latest_sha256
                ) VALUES (?, ?, ?, ?, 0, 1, ?, ?)
                """,
                (
                    scope_id,
                    canonical_url,
                    run_id,
                    run_id,
                    latest_document_id,
                    latest_sha256,
                ),
            )
            row_id = cur.lastrowid
        else:
            row_id = existing["id"]
            self.conn.execute(
                """
                UPDATE tracked_files
                SET last_seen_run_id = ?,
                    miss_count = 0,
                    is_active = 1,
                    latest_document_id = COALESCE(?, latest_document_id),
                    latest_sha256 = CASE WHEN ? <> '' THEN ? ELSE latest_sha256 END
                WHERE id = ?
                """,
                (
                    run_id,
                    latest_document_id,
                    latest_sha256,
                    latest_sha256,
                    row_id,
                ),
            )
        self.conn.commit()
        return self.get_tracked_file(row_id)

    def get_tracked_file(self, file_id: int) -> Optional[TrackedFile]:
        row = self.conn.execute(
            "SELECT * FROM tracked_files WHERE id = ?",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tracked_file(row)

    def list_tracked_files(self, scope_id: int) -> List[TrackedFile]:
        rows = self.conn.execute(
            "SELECT * FROM tracked_files WHERE scope_id = ? ORDER BY canonical_url ASC",
            (scope_id,),
        ).fetchall()
        return [self._row_to_tracked_file(row) for row in rows]

    def _row_to_tracked_file(self, row) -> TrackedFile:
        return TrackedFile(
            id=row["id"],
            scope_id=row["scope_id"],
            canonical_url=row["canonical_url"],
            first_seen_run_id=row["first_seen_run_id"],
            last_seen_run_id=row["last_seen_run_id"],
            miss_count=row["miss_count"] or 0,
            is_active=bool(row["is_active"]),
            latest_document_id=row["latest_document_id"],
            latest_sha256=row["latest_sha256"] or "",
        )

    def add_file_observation(self, observation: FileObservation) -> FileObservation:
        cur = self.conn.execute(
            """
            INSERT INTO file_observations (
                scope_id, run_id, page_id, file_id, discovered_url, download_url, tracked_local_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.scope_id,
                observation.run_id,
                observation.page_id,
                observation.file_id,
                observation.discovered_url,
                observation.download_url,
                observation.tracked_local_path,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM file_observations WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return self._row_to_file_observation(row)

    def list_file_observations(self, scope_id: int, run_id: Optional[int] = None) -> List[FileObservation]:
        if run_id is None:
            rows = self.conn.execute(
                "SELECT * FROM file_observations WHERE scope_id = ? ORDER BY id ASC",
                (scope_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM file_observations WHERE scope_id = ? AND run_id = ? ORDER BY id ASC",
                (scope_id, run_id),
            ).fetchall()
        return [self._row_to_file_observation(row) for row in rows]

    def _row_to_file_observation(self, row) -> FileObservation:
        return FileObservation(
            id=row["id"],
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            page_id=row["page_id"],
            file_id=row["file_id"],
            discovered_url=row["discovered_url"],
            download_url=row["download_url"],
            tracked_local_path=row["tracked_local_path"] or "",
        )
