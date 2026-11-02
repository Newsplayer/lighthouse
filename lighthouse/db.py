"""SQLite storage. One row per posting (uid); duplicates share a fingerprint."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Job

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "lighthouse.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    uid           TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_board  TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    url           TEXT,
    content_html  TEXT,
    departments   TEXT,            -- JSON list
    posted_at     TEXT,
    updated_at    TEXT,
    first_seen    TEXT NOT NULL,   -- when Lighthouse first saw it
    last_seen     TEXT NOT NULL,   -- last fetch that still listed it
    active        INTEGER NOT NULL DEFAULT 1,
    ghost_reasons TEXT             -- JSON list
);
CREATE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active);
"""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def upsert_board(conn: sqlite3.Connection, source: str, board: str,
                 jobs: list[Job], ghost: dict[str, list[str]]) -> dict:
    """Write one board's fresh listing. Postings no longer listed are marked inactive."""
    now = datetime.now(timezone.utc).isoformat()
    existing = {r["uid"] for r in conn.execute(
        "SELECT uid FROM jobs WHERE source=? AND source_board=?", (source, board))}
    seen = set()
    for j in jobs:
        seen.add(j.uid)
        conn.execute(
            """INSERT INTO jobs (uid, fingerprint, source, source_board, external_id, title,
                   company, location, url, content_html, departments, posted_at, updated_at,
                   first_seen, last_seen, active, ghost_reasons)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
               ON CONFLICT(uid) DO UPDATE SET
                   fingerprint=excluded.fingerprint, title=excluded.title,
                   company=excluded.company, location=excluded.location, url=excluded.url,
                   content_html=excluded.content_html, departments=excluded.departments,
                   posted_at=excluded.posted_at, updated_at=excluded.updated_at,
                   last_seen=excluded.last_seen, active=1,
                   ghost_reasons=excluded.ghost_reasons""",
            (j.uid, j.fingerprint, j.source, j.source_board, j.external_id, j.title,
             j.company, j.location, j.url, j.content_html, json.dumps(j.departments),
             _iso(j.posted_at), _iso(j.updated_at), now, now, json.dumps(ghost.get(j.uid, []))),
        )
    gone = existing - seen
    conn.executemany("UPDATE jobs SET active=0 WHERE uid=?", [(u,) for u in gone])
    conn.commit()
    return {"new": len(seen - existing), "updated": len(seen & existing), "closed": len(gone)}


def list_jobs(conn: sqlite3.Connection, q: str = "", company: str = "",
              hide_ghosts: bool = False) -> list[sqlite3.Row]:
    """Active jobs, one row per fingerprint (cross-source duplicates collapsed)."""
    sql = """
        SELECT j.*, COUNT(*) OVER (PARTITION BY j.fingerprint) AS dup_count
        FROM jobs j WHERE j.active = 1
    """
    args: list = []
    if q:
        sql += " AND (j.title LIKE ? OR j.location LIKE ? OR j.departments LIKE ?)"
        args += [f"%{q}%"] * 3
    if company:
        sql += " AND j.company = ?"
        args.append(company)
    if hide_ghosts:
        sql += " AND (j.ghost_reasons IS NULL OR j.ghost_reasons = '[]')"
    # Keep the earliest-seen row per fingerprint as the canonical listing.
    sql = f"""
        SELECT * FROM (
            SELECT x.*, ROW_NUMBER() OVER (PARTITION BY x.fingerprint ORDER BY x.first_seen, x.uid) AS rn
            FROM ({sql}) x
        ) WHERE rn = 1
        ORDER BY COALESCE(posted_at, first_seen) DESC
    """
    return conn.execute(sql, args).fetchall()


def get_job(conn: sqlite3.Connection, uid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE uid=?", (uid,)).fetchone()


def duplicates_of(conn: sqlite3.Connection, fingerprint: str, uid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT uid, source, source_board, url FROM jobs WHERE fingerprint=? AND uid<>? AND active=1",
        (fingerprint, uid)).fetchall()


def companies(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE active=1 ORDER BY company")]


def stats(conn: sqlite3.Connection) -> dict:
    r = conn.execute("""SELECT COUNT(*) AS total,
                               COUNT(DISTINCT fingerprint) AS unique_roles,
                               MAX(last_seen) AS last_fetch
                        FROM jobs WHERE active=1""").fetchone()
    return dict(r)
