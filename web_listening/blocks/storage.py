import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from web_listening.models import AnalysisReport, Change, Document, Site, SiteSnapshot


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
                created_at TEXT,
                last_checked_at TEXT,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS site_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER NOT NULL,
                captured_at TEXT,
                content_hash TEXT NOT NULL,
                content_text TEXT DEFAULT '',
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
                content_md TEXT DEFAULT '',
                FOREIGN KEY (site_id) REFERENCES sites(id)
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
        """)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── Sites ──────────────────────────────────────────────────────────────

    def add_site(self, site: Site) -> Site:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO sites (url, name, tags, created_at, last_checked_at, is_active) VALUES (?,?,?,?,?,?)",
            (
                site.url,
                site.name,
                json.dumps(site.tags),
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
                created_at=_parse_dt(r["created_at"]),
                last_checked_at=_parse_dt(r["last_checked_at"]),
                is_active=bool(r["is_active"]),
            )
            for r in rows
        ]

    def deactivate_site(self, site_id: int):
        self.conn.execute("UPDATE sites SET is_active=0 WHERE id=?", (site_id,))
        self.conn.commit()

    def _update_site_checked(self, site_id: int):
        self.conn.execute(
            "UPDATE sites SET last_checked_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), site_id),
        )
        self.conn.commit()

    # ── Snapshots ──────────────────────────────────────────────────────────

    def add_snapshot(self, snapshot: SiteSnapshot) -> SiteSnapshot:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO site_snapshots (site_id, captured_at, content_hash, content_text, links) VALUES (?,?,?,?,?)",
            (
                snapshot.site_id,
                snapshot.captured_at.isoformat() if snapshot.captured_at else now,
                snapshot.content_hash,
                snapshot.content_text,
                json.dumps(snapshot.links),
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM site_snapshots WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return SiteSnapshot(
            id=row["id"],
            site_id=row["site_id"],
            captured_at=_parse_dt(row["captured_at"]),
            content_hash=row["content_hash"],
            content_text=row["content_text"] or "",
            links=json.loads(row["links"] or "[]"),
        )

    def get_latest_snapshot(self, site_id: int) -> Optional[SiteSnapshot]:
        row = self.conn.execute(
            "SELECT * FROM site_snapshots WHERE site_id=? ORDER BY captured_at DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        if row is None:
            return None
        return SiteSnapshot(
            id=row["id"],
            site_id=row["site_id"],
            captured_at=_parse_dt(row["captured_at"]),
            content_hash=row["content_hash"],
            content_text=row["content_text"] or "",
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
        cur = self.conn.execute(
            """INSERT INTO documents
               (site_id, title, url, download_url, institution, page_url,
                published_at, downloaded_at, local_path, doc_type, content_md)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
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
                doc.content_md,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id=?", (cur.lastrowid,)
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
            content_md=row["content_md"] or "",
        )

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
