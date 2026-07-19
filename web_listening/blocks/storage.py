import json
import base64
import binascii
import hashlib
import os
import sqlite3
import stat
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, List, Optional

from web_listening.models import (
    AnalysisReport,
    AcquisitionArtifact,
    AcquisitionAttempt,
    Change,
    CrawlRun,
    CrawlScope,
    Document,
    FileObservation,
    Job,
    PageEdge,
    PageSnapshot,
    Site,
    SiteSnapshot,
    TrackedFile,
    TrackedPage,
)
from web_listening.blocks.acquisition_gateway import redact_persisted_value
from web_listening.contracts import AcquisitionAttempt as ContractAcquisitionAttempt
from web_listening.contracts._protocol import validate_portable_relative_path


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

            CREATE TABLE IF NOT EXISTS jobs (
                job_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_type TEXT NOT NULL,
                status TEXT DEFAULT 'queued',
                stage TEXT DEFAULT 'accepted',
                stage_message TEXT DEFAULT '',
                progress INTEGER DEFAULT 0,
                scope_id INTEGER,
                run_id INTEGER,
                produced_artifacts_json TEXT DEFAULT '{}',
                artifact_summary_json TEXT DEFAULT '{}',
                error TEXT DEFAULT '',
                error_code TEXT DEFAULT '',
                error_detail_json TEXT DEFAULT '{}',
                is_retryable INTEGER DEFAULT 0,
                accepted_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id)
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
                document_id INTEGER,
                discovered_url TEXT NOT NULL,
                download_url TEXT NOT NULL,
                tracked_local_path TEXT DEFAULT '',
                attempt_id TEXT,
                FOREIGN KEY (scope_id) REFERENCES crawl_scopes(id),
                FOREIGN KEY (run_id) REFERENCES crawl_runs(id),
                FOREIGN KEY (page_id) REFERENCES tracked_pages(id),
                FOREIGN KEY (file_id) REFERENCES tracked_files(id),
                FOREIGN KEY (document_id) REFERENCES documents(id)
            );

            CREATE TABLE IF NOT EXISTS acquisition_attempts (
                attempt_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                scope_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                content_kind TEXT NOT NULL,
                profile_id TEXT,
                site_skill_id TEXT,
                site_skill_version TEXT,
                site_skill_package_sha256 TEXT,
                recipe_id TEXT,
                script_sha256 TEXT,
                executor_id TEXT NOT NULL,
                executor_version TEXT NOT NULL,
                requested_url TEXT NOT NULL,
                final_url TEXT,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                acquisition_fingerprint TEXT,
                classification TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                reason TEXT DEFAULT '',
                validation_json TEXT DEFAULT '{}',
                canonical_json TEXT NOT NULL,
                redaction_status TEXT NOT NULL,
                authority_mode TEXT NOT NULL,
                UNIQUE(run_id, request_id, position)
            );

            CREATE TABLE IF NOT EXISTS acquisition_artifacts (
                attempt_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                portable_path TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                redaction_status TEXT NOT NULL,
                PRIMARY KEY(attempt_id, kind, portable_path),
                FOREIGN KEY(attempt_id) REFERENCES acquisition_attempts(attempt_id)
            );
            CREATE INDEX IF NOT EXISTS idx_acquisition_attempts_run_scope
                ON acquisition_attempts(scope_id, run_id, position, attempt_id);
            CREATE INDEX IF NOT EXISTS idx_acquisition_artifacts_attempt
                ON acquisition_artifacts(attempt_id);
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
        self._ensure_column("jobs", "produced_artifacts_json", "TEXT DEFAULT '{}'")
        self._ensure_column("jobs", "stage", "TEXT DEFAULT 'accepted'")
        self._ensure_column("jobs", "stage_message", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "artifact_summary_json", "TEXT DEFAULT '{}'")
        self._ensure_column("jobs", "error_code", "TEXT DEFAULT ''")
        self._ensure_column("jobs", "error_detail_json", "TEXT DEFAULT '{}'")
        self._ensure_column("jobs", "is_retryable", "INTEGER DEFAULT 0")
        self._ensure_column("jobs", "accepted_at", "TEXT")
        self._ensure_column("file_observations", "document_id", "INTEGER")
        self._ensure_column("file_observations", "tracked_local_path", "TEXT DEFAULT ''")
        self._ensure_column("page_snapshots", "attempt_id", "TEXT")
        self._ensure_column("file_observations", "attempt_id", "TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_page_snapshots_attempt_id ON page_snapshots(attempt_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_file_observations_attempt_id ON file_observations(attempt_id)")
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
        row_keys = set(row.keys())
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
            tracked_local_path=row["tracked_local_path"] if "tracked_local_path" in row_keys else "",
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

    def list_scope_documents(self, scope_id: int, run_id: Optional[int] = None) -> List[Document]:
        if run_id is None:
            rows = self.conn.execute(
                """
                SELECT
                    d.*,
                    COALESCE(tp.canonical_url, d.page_url, '') AS page_url,
                    fo.tracked_local_path AS tracked_local_path
                FROM file_observations fo
                LEFT JOIN tracked_files tf ON tf.id = fo.file_id
                JOIN documents d ON d.id = COALESCE(fo.document_id, tf.latest_document_id)
                LEFT JOIN tracked_pages tp ON tp.id = fo.page_id
                WHERE fo.scope_id = ? AND COALESCE(fo.document_id, tf.latest_document_id) IS NOT NULL
                ORDER BY tp.canonical_url ASC, d.download_url ASC, fo.id ASC
                """,
                (scope_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT
                    d.*,
                    COALESCE(tp.canonical_url, d.page_url, '') AS page_url,
                    fo.tracked_local_path AS tracked_local_path
                FROM file_observations fo
                LEFT JOIN tracked_files tf ON tf.id = fo.file_id
                JOIN documents d ON d.id = COALESCE(fo.document_id, tf.latest_document_id)
                LEFT JOIN tracked_pages tp ON tp.id = fo.page_id
                WHERE fo.scope_id = ? AND fo.run_id = ? AND COALESCE(fo.document_id, tf.latest_document_id) IS NOT NULL
                ORDER BY tp.canonical_url ASC, d.download_url ASC, fo.id ASC
                """,
                (scope_id, run_id),
            ).fetchall()
        return [self._row_to_document(row) for row in rows]

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

    # ── Jobs ───────────────────────────────────────────────────────────────

    def add_job(self, job: Job) -> Job:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO jobs (
                job_type, status, stage, stage_message, progress, scope_id, run_id,
                produced_artifacts_json, artifact_summary_json, error, error_code,
                error_detail_json, is_retryable, accepted_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_type,
                job.status,
                job.stage,
                job.stage_message,
                job.progress,
                job.scope_id,
                job.run_id,
                json.dumps(job.produced_artifacts),
                json.dumps(job.artifact_summary),
                job.error,
                job.error_code,
                json.dumps(job.error_detail),
                int(job.is_retryable),
                job.accepted_at.isoformat() if job.accepted_at else now,
                job.started_at.isoformat() if job.started_at else None,
                job.finished_at.isoformat() if job.finished_at else None,
            ),
        )
        self.conn.commit()
        return self.get_job(cur.lastrowid)

    _UPDATABLE_JOB_FIELDS: Final[frozenset[str]] = frozenset(
        [
            "status",
            "stage",
            "stage_message",
            "progress",
            "scope_id",
            "run_id",
            "produced_artifacts",
            "artifact_summary",
            "error",
            "error_code",
            "error_detail",
            "is_retryable",
            "accepted_at",
            "started_at",
            "finished_at",
        ]
    )

    _JOB_FIELD_COLUMN_MAP: Final[dict[str, str]] = {
        "produced_artifacts": "produced_artifacts_json",
        "artifact_summary": "artifact_summary_json",
        "error_detail": "error_detail_json",
    }

    def update_job(self, job_id: int, **fields) -> Optional[Job]:
        if not fields:
            return self.get_job(job_id)
        unknown = set(fields) - self._UPDATABLE_JOB_FIELDS
        if unknown:
            raise ValueError(f"Unknown job fields: {sorted(unknown)}")
        assignments = []
        params = []
        for key, value in fields.items():
            column_name = self._JOB_FIELD_COLUMN_MAP.get(key, key)
            assignments.append(f"{column_name} = ?")
            if isinstance(value, datetime):
                params.append(value.isoformat())
            elif key in {"produced_artifacts", "artifact_summary", "error_detail"}:
                params.append(json.dumps(value or {}))
            elif key == "is_retryable":
                params.append(int(bool(value)))
            else:
                params.append(value)
        params.append(job_id)
        self.conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id = ?",
            params,
        )
        self.conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> Optional[Job]:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_jobs(
        self,
        *,
        scope_id: Optional[int] = None,
        job_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Job]:
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[object] = []
        if scope_id is not None:
            query += " AND scope_id = ?"
            params.append(scope_id)
        if job_type:
            query += " AND job_type = ?"
            params.append(job_type)
        query += " ORDER BY COALESCE(finished_at, started_at, accepted_at) DESC, job_id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row_to_job(row) for row in rows]

    def get_latest_job(self, *, scope_id: int, job_type: str, status: Optional[str] = None) -> Optional[Job]:
        query = """
            SELECT * FROM jobs
            WHERE scope_id = ? AND job_type = ?
        """
        params: list[object] = [scope_id, job_type]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY COALESCE(finished_at, started_at, accepted_at) DESC, job_id DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def _row_to_job(self, row) -> Job:
        try:
            produced_artifacts = json.loads(row["produced_artifacts_json"] or "{}")
        except json.JSONDecodeError:
            produced_artifacts = {}
        if not isinstance(produced_artifacts, dict):
            produced_artifacts = {}
        try:
            artifact_summary = json.loads(row["artifact_summary_json"] or "{}")
        except json.JSONDecodeError:
            artifact_summary = {}
        if not isinstance(artifact_summary, dict):
            artifact_summary = {}
        try:
            error_detail = json.loads(row["error_detail_json"] or "{}")
        except json.JSONDecodeError:
            error_detail = {}
        if not isinstance(error_detail, dict):
            error_detail = {}
        return Job(
            job_id=row["job_id"],
            job_type=row["job_type"],
            status=row["status"] or "queued",
            stage=row["stage"] or "accepted",
            stage_message=row["stage_message"] or "",
            progress=row["progress"] or 0,
            scope_id=row["scope_id"],
            run_id=row["run_id"],
            produced_artifacts=produced_artifacts,
            artifact_summary=artifact_summary,
            error=row["error"] or "",
            error_code=row["error_code"] or "",
            error_detail=error_detail,
            is_retryable=bool(row["is_retryable"] or 0),
            accepted_at=_parse_dt(row["accepted_at"]),
            started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]),
        )

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
        self._validate_accepted_attempt(snapshot.attempt_id, snapshot.scope_id, snapshot.run_id, "page")
        snapshot = snapshot.model_copy(update={
            "metadata_json": redact_persisted_value(snapshot.metadata_json),
            "final_url": redact_persisted_value(snapshot.final_url),
            "links": redact_persisted_value(snapshot.links),
        })
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            """
            INSERT INTO page_snapshots (
                scope_id, page_id, run_id, captured_at, content_hash, raw_html, cleaned_html,
                content_text, markdown, fit_markdown, metadata_json, fetch_mode,
                final_url, status_code, links, attempt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                snapshot.attempt_id,
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

    def list_scope_page_snapshots(self, scope_id: int) -> List[PageSnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM page_snapshots WHERE scope_id = ? ORDER BY captured_at ASC, id ASC",
            (scope_id,),
        ).fetchall()
        return [self._row_to_page_snapshot(row) for row in rows]

    def _row_to_page_snapshot(self, row) -> PageSnapshot:
        return PageSnapshot(
            id=row["id"],
            scope_id=row["scope_id"],
            page_id=row["page_id"],
            run_id=row["run_id"],
            attempt_id=row["attempt_id"],
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
        self._validate_accepted_attempt(observation.attempt_id, observation.scope_id, observation.run_id, "document")
        observation = observation.model_copy(update={
            "discovered_url": redact_persisted_value(observation.discovered_url),
            "download_url": redact_persisted_value(observation.download_url),
            "tracked_local_path": redact_persisted_value(observation.tracked_local_path),
        })
        cur = self.conn.execute(
            """
            INSERT INTO file_observations (
                scope_id, run_id, page_id, file_id, document_id, discovered_url, download_url, tracked_local_path, attempt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.scope_id,
                observation.run_id,
                observation.page_id,
                observation.file_id,
                observation.document_id,
                observation.discovered_url,
                observation.download_url,
                observation.tracked_local_path,
                observation.attempt_id,
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
            attempt_id=row["attempt_id"],
            page_id=row["page_id"],
            file_id=row["file_id"],
            document_id=row["document_id"],
            discovered_url=row["discovered_url"],
            download_url=row["download_url"],
            tracked_local_path=row["tracked_local_path"] or "",
        )

    def add_acquisition_attempt(self, attempt: AcquisitionAttempt) -> AcquisitionAttempt:
        self._validate_acquisition_attempt_semantics(attempt)
        sanitized_payload = redact_persisted_value(
            attempt.model_dump(mode="python", exclude={"canonical_json", "artifacts"})
        )
        sanitized = AcquisitionAttempt(**sanitized_payload, canonical_json="")
        payload = self._canonical_attempt_payload(attempt, sanitized)
        existing = self.conn.execute(
            "SELECT canonical_json FROM acquisition_attempts WHERE attempt_id = ?", (attempt.attempt_id,)
        ).fetchone()
        if existing is not None:
            persisted = self.get_acquisition_attempt(attempt.attempt_id)
            comparable = lambda value: value.model_dump(
                mode="json", exclude={"canonical_json", "artifacts"}
            )
            if existing["canonical_json"] != payload or comparable(persisted) != comparable(sanitized):
                raise ValueError("conflicting acquisition attempt id")
            return self.get_acquisition_attempt(attempt.attempt_id)
        self.conn.execute(
            """INSERT INTO acquisition_attempts (
                attempt_id, request_id, scope_id, run_id, position, content_kind, profile_id,
                site_skill_id, site_skill_version, site_skill_package_sha256, recipe_id,
                script_sha256, executor_id, executor_version, requested_url, final_url,
                requested_at, started_at, finished_at, acquisition_fingerprint, classification,
                accepted, reason, validation_json, canonical_json, redaction_status, authority_mode
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sanitized.attempt_id, sanitized.request_id, sanitized.scope_id, sanitized.run_id, sanitized.position,
             sanitized.content_kind, sanitized.profile_id, sanitized.site_skill_id, sanitized.site_skill_version,
             sanitized.site_skill_package_sha256, sanitized.recipe_id, sanitized.script_sha256,
             sanitized.executor_id, sanitized.executor_version, sanitized.requested_url, sanitized.final_url,
             sanitized.requested_at.isoformat(), sanitized.started_at.isoformat() if sanitized.started_at else None,
             sanitized.finished_at.isoformat() if sanitized.finished_at else None,
             sanitized.acquisition_fingerprint, sanitized.classification, int(sanitized.accepted), sanitized.reason,
             json.dumps(sanitized.validation, sort_keys=True), payload, sanitized.redaction_status,
             sanitized.authority_mode),
        )
        self.conn.commit()
        return self.get_acquisition_attempt(attempt.attempt_id)

    @staticmethod
    def _validate_acquisition_attempt_semantics(attempt: AcquisitionAttempt) -> None:
        if attempt.classification == "accepted" and not attempt.accepted:
            raise ValueError("accepted classification requires accepted=true")
        if attempt.accepted and (
            attempt.classification != "accepted"
            or (attempt.reason and attempt.reason != "accepted")
        ):
            raise ValueError("conflicting accepted attempt classification or reason")

    def add_legacy_compatibility_attempt(
        self, *, scope_id: int, run_id: int, identity: str, content_kind: str = "page",
    ) -> AcquisitionAttempt:
        """Create explicit lineage for compatibility fixtures/imports that predate governance."""
        request_id = hashlib.sha256(
            f"legacy-compatibility\0{scope_id}\0{run_id}\0{content_kind}\0{identity}".encode()
        ).hexdigest()
        existing = self.get_acquisition_attempt(request_id)
        if existing is not None:
            expected = (scope_id, run_id, content_kind, "legacy_compatibility_import",
                        "legacy-compatibility", redact_persisted_value(identity), True)
            actual = (existing.scope_id, existing.run_id, existing.content_kind, existing.executor_id,
                      existing.executor_version, existing.requested_url, existing.accepted)
            if actual != expected:
                raise ValueError("conflicting legacy compatibility attempt id")
            return existing
        now = datetime.now(timezone.utc)
        return self.add_acquisition_attempt(AcquisitionAttempt(
            attempt_id=request_id, request_id=request_id, scope_id=scope_id, run_id=run_id,
            position=0, content_kind=content_kind, executor_id="legacy_compatibility_import",
            executor_version="legacy-compatibility", requested_url=identity, final_url=identity,
            requested_at=now, started_at=now, finished_at=now, classification="accepted",
            accepted=True, reason="accepted", validation={"decision": "accepted"},
            authority_mode="legacy_compatibility",
        ))

    def admit_inline_acquisition_artifacts(self, attempt_id: str, payloads) -> List[AcquisitionArtifact]:
        if not payloads:
            return []
        if (not isinstance(payloads, Sequence) or isinstance(payloads, (str, bytes, bytearray))
                or len(payloads) > 8):
            raise ValueError("inline acquisition artifacts must be a bounded sequence")
        payloads = tuple(payloads)
        self._validate_portable_component(attempt_id)
        if self.conn.execute(
            "SELECT 1 FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone() is None:
            raise ValueError("acquisition artifact requires an existing persisted attempt")
        allowed = {
            "screenshot": {"image/png", "image/jpeg", "image/webp"},
            "trace": {"application/json", "application/zip"},
            "raw_capture": {"text/html", "application/octet-stream", "application/json"},
        }
        staged: list[tuple[int, str, AcquisitionArtifact]] = []
        created: list[tuple[str, int, int]] = []
        root_fd = artifacts_fd = attempt_fd = None
        try:
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            root_fd = os.open(self.db_path.parent, directory_flags)
            try:
                os.mkdir("acquisition_artifacts", 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            artifacts_fd = os.open("acquisition_artifacts", directory_flags, dir_fd=root_fd)
            try:
                os.mkdir(attempt_id, 0o700, dir_fd=artifacts_fd)
            except FileExistsError:
                pass
            attempt_fd = os.open(attempt_id, directory_flags, dir_fd=artifacts_fd)
            for index, item in enumerate(payloads):
                if not isinstance(item, Mapping):
                    raise ValueError("inline artifact descriptor must be an object")
                kind, mime = str(item.get("kind", "")), str(item.get("mime_type", ""))
                if kind not in allowed or mime not in allowed[kind]:
                    raise ValueError("inline artifact kind or MIME is not allowed")
                try:
                    data = base64.b64decode(str(item.get("data_base64", "")), validate=True)
                except (binascii.Error, ValueError):
                    raise ValueError("inline artifact payload is not valid base64") from None
                if len(data) > 4 * 1024 * 1024 or int(item.get("size_bytes", -1)) != len(data):
                    raise ValueError("inline artifact byte size mismatch or limit exceeded")
                digest = hashlib.sha256(data).hexdigest()
                if item.get("sha256") != digest:
                    raise ValueError("inline artifact SHA-256 mismatch")
                data, redaction_status = self._govern_artifact_bytes(kind, mime, data)
                digest = hashlib.sha256(data).hexdigest()
                suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                          "application/json": ".json", "application/zip": ".zip",
                          "text/html": ".html", "application/octet-stream": ".bin"}[mime]
                portable = f"acquisition_artifacts/{attempt_id}/{index:02d}-{kind}{suffix}"
                target = f"{index:02d}-{kind}{suffix}"
                descriptor = os.open(".", os.O_RDWR | os.O_TMPFILE, 0o600, dir_fd=attempt_fd)
                try:
                    created_identity = os.stat(descriptor)
                    opened = os.fstat(descriptor)
                    if ((opened.st_dev, opened.st_ino)
                            != (created_identity.st_dev, created_identity.st_ino)):
                        raise ValueError("temporary acquisition artifact identity changed")
                    artifact = AcquisitionArtifact(attempt_id=attempt_id, kind=kind,
                        portable_path=portable, mime_type=mime, size_bytes=len(data), sha256=digest,
                        redaction_status=redaction_status)
                    with os.fdopen(descriptor, "wb", closefd=False) as stream:
                        stream.write(data)
                        stream.flush()
                    os.fsync(descriptor)
                    staged.append((descriptor, target, artifact))
                except Exception:
                    os.close(descriptor)
                    raise
            self._verify_pinned_artifact_directories(root_fd, artifacts_fd, attempt_fd, attempt_id)
            self.conn.execute("BEGIN IMMEDIATE")
            for descriptor, target, artifact in staged:
                row = self.conn.execute("""SELECT mime_type, size_bytes, sha256, redaction_status FROM acquisition_artifacts
                    WHERE attempt_id=? AND kind=? AND portable_path=?""",
                    (artifact.attempt_id, artifact.kind, artifact.portable_path)).fetchone()
                if row is not None and (row["mime_type"], row["size_bytes"], row["sha256"],
                                        row["redaction_status"]) != (
                        artifact.mime_type, artifact.size_bytes, artifact.sha256,
                        artifact.redaction_status):
                    raise ValueError("conflicting acquisition artifact metadata")
                try:
                    source_info = os.fstat(descriptor)
                    self._link_unnamed_temporary(descriptor, attempt_fd, target)
                except FileExistsError:
                    self._verify_existing_artifact(attempt_fd, target, artifact)
                else:
                    created.append((target, source_info.st_dev, source_info.st_ino))
                self.conn.execute("""INSERT INTO acquisition_artifacts
                    (attempt_id, kind, portable_path, mime_type, size_bytes, sha256, redaction_status)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(attempt_id, kind, portable_path) DO NOTHING""",
                    (artifact.attempt_id, artifact.kind, artifact.portable_path,
                    artifact.mime_type, artifact.size_bytes, artifact.sha256, artifact.redaction_status))
            for _, target, artifact in staged:
                self._verify_existing_artifact(attempt_fd, target, artifact)
            # Verify every final named target before checking that the pinned
            # directory chain is still the one reachable by its published path.
            for _, target, artifact in staged:
                self._verify_existing_artifact(attempt_fd, target, artifact)
            # This directory rebind is intentionally the final filesystem
            # operation immediately before the database commit.
            self._verify_pinned_artifact_directories(root_fd, artifacts_fd, attempt_fd, attempt_id)
            self.conn.commit()
            return [item[2] for item in staged]
        except Exception:
            self.conn.rollback()
            for target, device, inode in created:
                self._unlink_if_identity(attempt_fd, target, device, inode)
            raise
        finally:
            for descriptor, _, _ in staged:
                os.close(descriptor)
            for descriptor in (attempt_fd, artifacts_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)

    @staticmethod
    def _link_unnamed_temporary(descriptor: int, parent_fd: int, target: str) -> None:
        # procfs resolves this magic link to the already-open unnamed inode;
        # the destination remains relative to the pinned attempt directory.
        os.link(f"/proc/self/fd/{descriptor}", target, dst_dir_fd=parent_fd,
                follow_symlinks=True)

    @staticmethod
    def _validate_portable_component(value: str) -> None:
        try:
            validated = validate_portable_relative_path(value, field_name="attempt_id")
        except ValueError:
            raise ValueError("attempt_id must be one safe portable path component") from None
        if validated is None or "/" in validated:
            raise ValueError("attempt_id must be one safe portable path component")

    def _canonical_attempt_payload(self, supplied: AcquisitionAttempt,
                                   indexed: AcquisitionAttempt) -> str:
        if indexed.authority_mode not in {"governed", "legacy_runtime"}:
            return self._compatibility_attempt_payload(indexed)
        if not supplied.canonical_json:
            raise ValueError("governed acquisition attempt requires canonical JSON authority")
        contract = ContractAcquisitionAttempt.model_validate_json(supplied.canonical_json)
        redacted_json = json.dumps(
            redact_persisted_value(contract.model_dump(mode="json")),
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        redacted = ContractAcquisitionAttempt.model_validate_json(redacted_json)
        redacted_plain = json.loads(redacted_json)
        expected = (
            indexed.attempt_id, indexed.request_id, str(indexed.scope_id), str(indexed.run_id),
            indexed.executor_id, indexed.requested_url, indexed.final_url, indexed.accepted,
        )
        actual = (
            redacted.attempt_id, redacted.request.request_id, redacted.request.scope_id,
            redacted.request.run_id, redacted.request.executor_id, str(redacted.request.url),
            str(redacted.result.final_url) if redacted.result.final_url else None, redacted.accepted,
        )
        if actual != expected:
            raise ValueError("canonical acquisition authority conflicts with relational indexes")
        request_metadata = redacted.request.metadata
        result_metadata = redacted.result.metadata
        governed_request_metadata = {
            "acquisition_fingerprint", "scope_fingerprint", "profile_id", "authority_mode",
            "content_kind", "fallback_position", "executor_version", "entrypoint",
            "script_sha256", "required_capabilities", "executor_capabilities",
            "requires_authorized_access", "verification_rules", "resource_limits",
            "quality_gates", "scope_budgets",
        }
        legacy_runtime_request_metadata = {
            "authority_mode", "content_kind", "fallback_position", "profile_id",
            "legacy_fetch_mode", "legacy_executor_label", "site_skill_lineage",
            "executor_version",
        }
        required_request_metadata = (governed_request_metadata if indexed.authority_mode == "governed"
                                     else legacy_runtime_request_metadata)
        required_result_metadata = {
            "acquisition_classification", "acquisition_validation",
        }
        if not required_request_metadata.issubset(request_metadata):
            raise ValueError("canonical acquisition authority lacks required request metadata")
        if not required_result_metadata.issubset(result_metadata):
            raise ValueError("canonical acquisition authority lacks required result metadata")
        canonical_validation = redacted_plain["result"]["metadata"]["acquisition_validation"]
        semantic_expected = (
            indexed.attempt_id, indexed.request_id,
            indexed.requested_at, indexed.started_at, indexed.finished_at,
            indexed.classification, indexed.reason or indexed.classification, indexed.accepted, indexed.authority_mode,
            indexed.content_kind, indexed.requested_url, indexed.final_url,
        )
        semantic_actual = (
            redacted.attempt_id, redacted.request.request_id, redacted.request.requested_at,
            redacted.result.started_at, redacted.result.finished_at,
            result_metadata["acquisition_classification"],
            redacted.acceptance_reason, redacted.accepted,
            request_metadata["authority_mode"], request_metadata["content_kind"], str(redacted.request.url),
            str(redacted.result.final_url) if redacted.result.final_url else None,
        )
        if semantic_actual != semantic_expected:
            raise ValueError("conflicting canonical acquisition authority and relational semantics")
        if request_metadata["profile_id"] != indexed.profile_id:
            raise ValueError("conflicting canonical acquisition authority and relational profile")
        if request_metadata["fallback_position"] != indexed.position:
            raise ValueError("conflicting canonical acquisition authority and relational position")
        if ("status_code" in indexed.validation
                and redacted.result.status_code != indexed.validation.get("status_code")):
            raise ValueError("conflicting canonical acquisition authority and relational status")
        if canonical_validation != indexed.validation:
            raise ValueError("canonical acquisition authority conflicts with relational validation")
        indexed_authority = (
            indexed.site_skill_id, indexed.site_skill_version, indexed.site_skill_package_sha256,
            indexed.recipe_id, indexed.script_sha256, indexed.executor_version,
            indexed.acquisition_fingerprint, indexed.content_kind,
        )
        canonical_authority = (
            redacted.request.site_skill_id, redacted.request.site_skill_version,
            redacted.request.site_skill_digest, redacted.request.recipe_id,
            request_metadata.get("script_sha256"), request_metadata.get("executor_version"),
            request_metadata.get("acquisition_fingerprint"), request_metadata.get("content_kind"),
        )
        if any(value is not None for value in indexed_authority) and indexed_authority != canonical_authority:
            raise ValueError("canonical acquisition authority conflicts with governed indexes")
        return json.dumps(redacted.model_dump(mode="json"), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _compatibility_attempt_payload(attempt: AcquisitionAttempt) -> str:
        """Keep non-governed lineage useful without claiming the frozen governed contract."""
        return json.dumps(redact_persisted_value({
            "schema_version": "acquisition-attempt-compatibility.v1",
            "authority_mode": attempt.authority_mode,
            "attempt": attempt.model_dump(mode="json", exclude={"canonical_json", "artifacts"}),
        }), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    @staticmethod
    def _govern_artifact_bytes(kind: str, mime: str, data: bytes) -> tuple[bytes, str]:
        Storage._verify_artifact_mime(mime, data)
        if mime == "application/json":
            try:
                decoded = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("textual acquisition artifact is not valid governed JSON") from None
            sanitized = redact_persisted_value(decoded)
            return (json.dumps(sanitized, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode(), "structurally_redacted")
        if mime == "text/html":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("textual acquisition artifact is not valid UTF-8") from None
            text = re.sub(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s<]+",
                          r"\1[REDACTED]", text)
            text = redact_persisted_value(text)
            return str(text).encode(), "structurally_redacted"
        if kind in {"trace", "raw_capture"}:
            raise ValueError("opaque trace or raw capture cannot be verified for redaction")
        return data, "opaque_unverified"

    @staticmethod
    def _verify_artifact_mime(mime: str, data: bytes) -> None:
        valid = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/webp": (len(data) >= 12 and data.startswith(b"RIFF")
                           and data[8:12] == b"WEBP"),
            "application/zip": data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")),
            "application/json": True,
            "text/html": True,
            "application/octet-stream": True,
        }
        if not valid.get(mime, False):
            raise ValueError("inline artifact bytes do not match declared MIME")

    @staticmethod
    def _unlink_if_identity(parent_fd: int | None, target: str,
                            device: int, inode: int) -> None:
        if parent_fd is None:
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = None
        try:
            descriptor = os.open(target, flags, dir_fd=parent_fd)
            info = os.fstat(descriptor)
            named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == (device, inode) == (named.st_dev, named.st_ino):
                os.unlink(target, dir_fd=parent_fd)
        except (FileNotFoundError, OSError):
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _verify_pinned_artifact_directories(self, root_fd: int, artifacts_fd: int,
                                            attempt_fd: int, attempt_id: str) -> None:
        paths = (self.db_path.parent, self.db_path.parent / "acquisition_artifacts",
                 self.db_path.parent / "acquisition_artifacts" / attempt_id)
        for descriptor, path in zip((root_fd, artifacts_fd, attempt_fd), paths):
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                raise ValueError("artifact parent changed during publication") from None
            pinned = os.fstat(descriptor)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise ValueError("artifact parent changed during publication")

    @staticmethod
    def _verify_existing_artifact(parent_fd: int, target: str, artifact: AcquisitionArtifact) -> None:
        digest = hashlib.sha256()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(target, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("existing acquisition artifact is symlinked or nonregular") from exc
        stream = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("existing acquisition artifact is not a regular file")
            if info.st_size != artifact.size_bytes:
                raise ValueError("conflicting acquisition artifact bytes")
            stream = os.fdopen(descriptor, "rb", closefd=False)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            try:
                named = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("existing acquisition artifact changed during verification") from exc
            if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("existing acquisition artifact changed during verification")
        finally:
            if stream is not None:
                stream.close()
            os.close(descriptor)
        if digest.hexdigest() != artifact.sha256:
            raise ValueError("conflicting acquisition artifact bytes")

    def get_acquisition_attempt(self, attempt_id: str) -> Optional[AcquisitionAttempt]:
        row = self.conn.execute("SELECT * FROM acquisition_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return self._row_to_acquisition_attempt(row) if row else None

    def list_acquisition_attempts(self, scope_id: int, run_id: int) -> List[AcquisitionAttempt]:
        rows = self.conn.execute(
            """SELECT * FROM acquisition_attempts WHERE scope_id=? AND run_id=?
               ORDER BY requested_at, COALESCE(started_at, requested_at),
                        COALESCE(finished_at, started_at, requested_at), request_id, position, attempt_id""",
            (scope_id, run_id),
        ).fetchall()
        return [self._row_to_acquisition_attempt(row) for row in rows]

    def _row_to_acquisition_attempt(self, row) -> AcquisitionAttempt:
        artifacts = [AcquisitionArtifact(**dict(item)) for item in self.conn.execute(
            "SELECT * FROM acquisition_artifacts WHERE attempt_id=? ORDER BY kind, portable_path", (row["attempt_id"],)
        ).fetchall()]
        return AcquisitionAttempt(
            attempt_id=row["attempt_id"], request_id=row["request_id"], scope_id=row["scope_id"],
            run_id=row["run_id"], position=row["position"], content_kind=row["content_kind"],
            profile_id=row["profile_id"], site_skill_id=row["site_skill_id"],
            site_skill_version=row["site_skill_version"], site_skill_package_sha256=row["site_skill_package_sha256"],
            recipe_id=row["recipe_id"], script_sha256=row["script_sha256"], executor_id=row["executor_id"],
            executor_version=row["executor_version"], requested_url=row["requested_url"], final_url=row["final_url"],
            requested_at=_parse_dt(row["requested_at"]), started_at=_parse_dt(row["started_at"]),
            finished_at=_parse_dt(row["finished_at"]), acquisition_fingerprint=row["acquisition_fingerprint"],
            classification=row["classification"], accepted=bool(row["accepted"]), reason=row["reason"] or "",
            validation=json.loads(row["validation_json"] or "{}"), canonical_json=row["canonical_json"],
            redaction_status=row["redaction_status"], authority_mode=row["authority_mode"], artifacts=artifacts,
        )

    def _validate_accepted_attempt(
        self, attempt_id: Optional[str], scope_id: int, run_id: int, content_kind: str,
    ) -> None:
        if attempt_id is None:
            raise ValueError("new tracked state requires a non-null accepted acquisition attempt")
        row = self.conn.execute(
            "SELECT accepted, scope_id, run_id, content_kind FROM acquisition_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if (row is None or not row["accepted"] or row["scope_id"] != scope_id
                or row["run_id"] != run_id or row["content_kind"] != content_kind):
            raise ValueError(
                f"tracked state requires an accepted acquisition attempt with content_kind={content_kind} "
                "for the same run and scope"
            )
