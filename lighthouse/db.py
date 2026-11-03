"""SQLite storage.

Scaling notes
- WAL mode: the web UI keeps reading while a fetch writes.
- Schema is versioned with PRAGMA user_version; MIGRATIONS run in order, once.
- The job list never computes duplicates on the fly: postprocess (dedupe.py) stores
  cluster_id / is_canonical / effective_first_seen per row, so a page of results is a
  plain indexed SELECT ... LIMIT, whatever the table size.
- Full-text search uses FTS5 when the SQLite build has it (every python.org build does),
  falling back to LIKE.
- Writes only touch rows whose content changed (content_hash).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .models import Job, company_key, iso, parse_dt, utcnow
from .sanitize import html_to_text

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "lighthouse.db"

V1 = """
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
    departments   TEXT,
    posted_at     TEXT,
    updated_at    TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    ghost_reasons TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active);
"""

_JOB_COLUMNS_V2 = [
    ("company_key", "TEXT"), ("content_hash", "TEXT"), ("detail_marker", "TEXT"),
    ("workplace", "TEXT"), ("employment_type", "TEXT"),
    ("salary_min", "REAL"), ("salary_max", "REAL"), ("salary_currency", "TEXT"),
    ("salary_interval", "TEXT"), ("locations", "TEXT"),
    # history
    ("closed_at", "TEXT"), ("reopened_count", "INTEGER NOT NULL DEFAULT 0"),
    # near-duplicate clustering (dedupe.py)
    ("minhash", "BLOB"), ("minhash_of", "TEXT"), ("cluster_id", "TEXT"),
    ("is_canonical", "INTEGER NOT NULL DEFAULT 0"), ("cluster_count", "INTEGER NOT NULL DEFAULT 1"),
    ("cluster_locations", "TEXT"), ("effective_first_seen", "TEXT"),
    ("repost_count", "INTEGER NOT NULL DEFAULT 0"),
    # ghost score (ghost.py)
    ("ghost_score", "INTEGER NOT NULL DEFAULT 0"),
    # derived filter fields: ATS structured data, else extraction (extract.py)
    ("workplace_eff", "TEXT"), ("pay_min_annual", "REAL"), ("pay_max_annual", "REAL"),
    ("pay_currency", "TEXT"), ("yoe_min", "INTEGER"), ("visa", "TEXT"),
    ("clearance", "TEXT"), ("citizenship", "INTEGER"), ("degree", "TEXT"),
    ("derived_of", "TEXT"),
    # eligibility (eligibility.py)
    ("elig", "INTEGER"), ("elig_reasons", "TEXT"), ("elig_version", "TEXT"),
    # rowid of this posting's full-text row (FTS5 lookups by rowid are O(log n))
    ("fts_rowid", "INTEGER"),
]

V2_TABLES = """
-- Descriptions live apart from the jobs table so list/filter queries read small rows only.
CREATE TABLE IF NOT EXISTS job_content (
    uid  TEXT PRIMARY KEY,
    html TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boards (
    ats        TEXT NOT NULL,
    slug       TEXT NOT NULL,
    company    TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    origin     TEXT NOT NULL DEFAULT 'config',   -- config | ui | cli
    options    TEXT,                             -- JSON, e.g. {"sponsor_name": "..."}
    added_at   TEXT NOT NULL,
    last_fetch TEXT,
    last_ok    TEXT,
    last_error TEXT,
    job_count  INTEGER,
    PRIMARY KEY (ats, slug)
);
CREATE TABLE IF NOT EXISTS events (
    id   INTEGER PRIMARY KEY,
    uid  TEXT NOT NULL,
    kind TEXT NOT NULL,            -- new | closed | reopened
    at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_uid ON events(uid);
CREATE TABLE IF NOT EXISTS marks (
    uid    TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('saved', 'applied', 'hidden')),
    at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extractions (
    content_hash TEXT PRIMARY KEY,
    method       TEXT NOT NULL,     -- rules | claude
    data         TEXT NOT NULL,     -- JSON: {field: {"value":..., "evidence": "..."}}
    model        TEXT,
    at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fit (
    content_hash TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('Strong', 'Possible', 'Weak')),
    summary      TEXT,
    requirements TEXT,              -- JSON list
    method       TEXT NOT NULL,     -- api | batch | mcp
    model        TEXT,
    at           TEXT NOT NULL,
    PRIMARY KEY (content_hash, profile_hash)
);
CREATE TABLE IF NOT EXISTS api_usage (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    batch         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claude_batches (
    id            TEXT PRIMARY KEY,
    purpose       TEXT NOT NULL,
    model         TEXT NOT NULL,
    profile_hash  TEXT,
    status        TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    reserved_usd  REAL NOT NULL DEFAULT 0,
    submitted_at  TEXT NOT NULL,
    done_at       TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sponsor_lca (
    employer_key  TEXT NOT NULL,
    employer_name TEXT NOT NULL,
    fiscal_year   INTEGER NOT NULL,
    filings       INTEGER NOT NULL DEFAULT 0,
    certified     INTEGER NOT NULL DEFAULT 0,
    wage_l1 INTEGER NOT NULL DEFAULT 0, wage_l2 INTEGER NOT NULL DEFAULT 0,
    wage_l3 INTEGER NOT NULL DEFAULT 0, wage_l4 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (employer_key, fiscal_year)
);
CREATE TABLE IF NOT EXISTS sponsor_uscis (
    employer_key         TEXT NOT NULL,
    employer_name        TEXT NOT NULL,
    fiscal_year          INTEGER NOT NULL,
    initial_approvals    INTEGER NOT NULL DEFAULT 0,
    initial_denials      INTEGER NOT NULL DEFAULT 0,
    continuing_approvals INTEGER NOT NULL DEFAULT 0,
    continuing_denials   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (employer_key, fiscal_year)
);
CREATE TABLE IF NOT EXISTS sponsor_files (
    file_key    TEXT PRIMARY KEY,   -- name|size|mtime
    kind        TEXT NOT NULL,      -- lca | uscis
    rows        INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sponsor_match (
    company_key  TEXT PRIMARY KEY,
    lca_recent   INTEGER NOT NULL DEFAULT 0,   -- certified LCAs, last 3 fiscal years on file
    uscis_recent INTEGER NOT NULL DEFAULT 0,   -- USCIS approvals, last 3 fiscal years on file
    names        TEXT                          -- JSON list of matched employer names
);
CREATE INDEX IF NOT EXISTS idx_jobs_ck ON jobs(company_key);
CREATE INDEX IF NOT EXISTS idx_jobs_cluster ON jobs(cluster_id);
CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(source, source_board);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(content_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_list ON jobs(active, is_canonical, effective_first_seen, ghost_score, elig);
"""

FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    title, company, location, body, tokenize = 'porter unicode61'
);
CREATE INDEX IF NOT EXISTS idx_jobs_fts ON jobs(fts_rowid);
"""


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, decl in _JOB_COLUMNS_V2:
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    conn.executescript(V2_TABLES)
    try:
        conn.executescript(FTS)
    except sqlite3.OperationalError:
        pass  # no FTS5 in this SQLite build; search falls back to LIKE
    # Backfill: UTC timestamps, company keys, hashes, history; seed the FTS index.
    from .models import Job as _J, content_hash
    rows = conn.execute("SELECT uid, company, title, location, content_html, posted_at, updated_at, "
                        "departments, active, first_seen, last_seen FROM jobs").fetchall()
    fts = has_fts(conn)
    for r in rows:
        fp = _J("", "", "", r["title"], r["company"], r["location"] or "", "").fingerprint
        conn.execute(
            """UPDATE jobs SET posted_at=?, updated_at=?, company_key=?, content_hash=?,
                   fingerprint=?, closed_at=CASE WHEN active=0 THEN last_seen END WHERE uid=?""",
            (iso(parse_dt(r["posted_at"])), iso(parse_dt(r["updated_at"])), company_key(r["company"]),
             content_hash(r["content_html"] or ""), fp, r["uid"]))
        conn.execute("INSERT INTO events(uid, kind, at) VALUES (?, 'new', ?)", (r["uid"], r["first_seen"]))
        if fts:
            _fts_put(conn, r["uid"], r["title"], r["company"], r["location"], r["content_html"])
    conn.execute("UPDATE jobs SET ghost_reasons = '[]'")  # recomputed by postprocess
    conn.execute("INSERT OR REPLACE INTO job_content(uid, html) SELECT uid, content_html FROM jobs "
                 "WHERE content_html IS NOT NULL AND content_html <> ''")
    try:
        conn.execute("ALTER TABLE jobs DROP COLUMN content_html")
    except sqlite3.OperationalError:  # SQLite < 3.35: leave the column, emptied
        conn.execute("UPDATE jobs SET content_html = NULL")


MIGRATIONS = [
    lambda c: c.executescript(V1),  # 0 -> 1
    _migrate_1_to_2,                # 1 -> 2
]
SCHEMA_VERSION = len(MIGRATIONS)


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
        version = 1  # pre-versioning database from v0.1
    for i in range(version, SCHEMA_VERSION):
        with conn:
            MIGRATIONS[i](conn)
            conn.execute(f"PRAGMA user_version={i + 1}")


def has_fts(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='jobs_fts'").fetchone() is not None


def _fts_put(conn, uid, title, company, location, html) -> None:
    old = conn.execute("SELECT fts_rowid FROM jobs WHERE uid=?", (uid,)).fetchone()
    if old and old[0] is not None:
        conn.execute("DELETE FROM jobs_fts WHERE rowid=?", (old[0],))
    cur = conn.execute("INSERT INTO jobs_fts(title, company, location, body) VALUES (?,?,?,?)",
                       (title, company, location or "", html_to_text(html or "")))
    conn.execute("UPDATE jobs SET fts_rowid=? WHERE uid=?", (cur.lastrowid, uid))


def fts_delete(conn, uid) -> None:
    old = conn.execute("SELECT fts_rowid FROM jobs WHERE uid=?", (uid,)).fetchone()
    if old and old[0] is not None:
        conn.execute("DELETE FROM jobs_fts WHERE rowid=?", (old[0],))
        conn.execute("UPDATE jobs SET fts_rowid=NULL WHERE uid=?", (uid,))


# ---------------------------------------------------------------- meta / settings

def get_meta(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and r[0] is not None else default


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


# ---------------------------------------------------------------- boards

def sync_config_boards(conn, boards: list[dict]) -> None:
    """Boards listed in config/sources.toml are inserted once; the UI can disable them."""
    now = utcnow()
    for b in boards:
        conn.execute(
            """INSERT INTO boards(ats, slug, company, options, origin, added_at)
               VALUES (?, ?, ?, ?, 'config', ?)
               ON CONFLICT(ats, slug) DO UPDATE SET
                   company=COALESCE(excluded.company, boards.company),
                   options=COALESCE(excluded.options, boards.options)""",
            (b["ats"], b["slug"], b.get("company"), json.dumps(b["options"]) if b.get("options") else None, now))
    conn.commit()


def add_board(conn, ats: str, slug: str, company: str | None = None, origin: str = "ui") -> bool:
    cur = conn.execute(
        """INSERT INTO boards(ats, slug, company, origin, added_at) VALUES (?,?,?,?,?)
           ON CONFLICT(ats, slug) DO UPDATE SET enabled=1""",
        (ats, slug, company, origin, utcnow()))
    conn.commit()
    return cur.rowcount > 0


def set_board_enabled(conn, ats: str, slug: str, enabled: bool) -> None:
    conn.execute("UPDATE boards SET enabled=? WHERE ats=? AND slug=?", (int(enabled), ats, slug))
    conn.commit()


def list_boards(conn, enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM boards" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY ats, slug"
    return conn.execute(sql).fetchall()


def board_result(conn, ats: str, slug: str, *, error: str | None = None, count: int | None = None) -> None:
    now = utcnow()
    if error:
        conn.execute("UPDATE boards SET last_fetch=?, last_error=? WHERE ats=? AND slug=?",
                     (now, error[:500], ats, slug))
    else:
        conn.execute("UPDATE boards SET last_fetch=?, last_ok=?, last_error=NULL, job_count=? "
                     "WHERE ats=? AND slug=?", (now, now, count, ats, slug))
    conn.commit()


# ---------------------------------------------------------------- ingest

def known_postings(conn, source: str, board: str) -> dict[str, tuple[str, bool]]:
    """uid -> (detail_marker, has_content). Lets sources skip detail calls for unchanged postings."""
    return {r[0]: (r[1] or "", bool(r[2])) for r in conn.execute(
        "SELECT j.uid, j.detail_marker, c.html IS NOT NULL AND c.html <> '' "
        "FROM jobs j LEFT JOIN job_content c ON c.uid = j.uid WHERE j.source=? AND j.source_board=?",
        (source, board))}


def upsert_board(conn: sqlite3.Connection, source: str, board: str, jobs: list[Job]) -> dict:
    """Write one board's fresh listing. Returns counts plus which uids/companies changed.

    Postings that are no longer listed are marked inactive (kept for history).
    An empty content_html never overwrites a stored copy (sources that need a detail
    call per posting only fetch new/changed ones).
    """
    now = utcnow()
    existing = {r["uid"]: r for r in conn.execute(
        "SELECT uid, active, content_hash, title, location, detail_marker FROM jobs WHERE source=? AND source_board=?",
        (source, board))}
    fts = has_fts(conn)
    seen, changed, touched = set(), set(), set()
    events = []
    with conn:
        for j in jobs:
            if j.uid in seen:
                continue
            seen.add(j.uid)
            old = existing.get(j.uid)
            chash = j.content_hash if j.content_html else (old["content_hash"] if old else j.content_hash)
            if old is None:
                events.append((j.uid, "new", now))
            elif not old["active"]:
                events.append((j.uid, "reopened", now))
            marker = j.detail_marker if j.content_html else (old["detail_marker"] if old else "")
            is_changed = (old is None or not old["active"] or old["content_hash"] != chash
                          or old["title"] != j.title or (old["location"] or "") != j.location)
            conn.execute(
                """INSERT INTO jobs (uid, fingerprint, source, source_board, external_id, title, company,
                       company_key, location, url, content_hash, departments, posted_at,
                       updated_at, first_seen, last_seen, active, ghost_reasons, detail_marker, workplace,
                       employment_type, salary_min, salary_max, salary_currency, salary_interval, locations,
                       effective_first_seen, cluster_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'[]',?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                       fingerprint=excluded.fingerprint, title=excluded.title, company=excluded.company,
                       company_key=excluded.company_key, location=excluded.location, url=excluded.url,
                       content_hash=excluded.content_hash, departments=excluded.departments,
                       posted_at=excluded.posted_at, updated_at=excluded.updated_at,
                       last_seen=excluded.last_seen, active=1, closed_at=NULL,
                       reopened_count=jobs.reopened_count + (1 - jobs.active),
                       detail_marker=excluded.detail_marker,
                       workplace=excluded.workplace, employment_type=excluded.employment_type,
                       salary_min=excluded.salary_min, salary_max=excluded.salary_max,
                       salary_currency=excluded.salary_currency, salary_interval=excluded.salary_interval,
                       locations=excluded.locations""",
                (j.uid, j.fingerprint, j.source, j.source_board, j.external_id, j.title, j.company,
                 company_key(j.company), j.location, j.url, chash,
                 json.dumps(j.departments), iso(j.posted_at), iso(j.updated_at), now, now,
                 marker, j.workplace, j.employment_type, j.salary_min, j.salary_max,
                 j.salary_currency, j.salary_interval, json.dumps(j.locations or ([j.location] if j.location else [])),
                 now, j.uid))
            if j.content_html and (old is None or old["content_hash"] != chash or not old["active"]):
                conn.execute("INSERT INTO job_content(uid, html) VALUES (?, ?) "
                             "ON CONFLICT(uid) DO UPDATE SET html=excluded.html", (j.uid, j.content_html))
            if is_changed:
                changed.add(j.uid)
                touched.add(company_key(j.company))
                if fts and j.content_html:
                    _fts_put(conn, j.uid, j.title, j.company, j.location, j.content_html)
        gone = [u for u, r in existing.items() if r["active"] and u not in seen]
        conn.executemany("UPDATE jobs SET active=0, closed_at=? WHERE uid=?", [(now, u) for u in gone])
        events += [(u, "closed", now) for u in gone]
        conn.executemany("INSERT INTO events(uid, kind, at) VALUES (?,?,?)", events)
        if gone:
            touched |= {r[0] for r in conn.execute(
                f"SELECT DISTINCT company_key FROM jobs WHERE uid IN ({','.join('?' * len(gone))})", gone)}
    new = sum(1 for e in events if e[1] == "new")
    return {"new": new, "updated": len(seen) - new, "closed": len(gone),
            "changed": changed, "touched": touched}


# ---------------------------------------------------------------- marks

def set_mark(conn, uid: str, status: str | None) -> None:
    if status:
        conn.execute("INSERT INTO marks(uid, status, at) VALUES (?,?,?) "
                     "ON CONFLICT(uid) DO UPDATE SET status=excluded.status, at=excluded.at",
                     (uid, status, utcnow()))
    else:
        conn.execute("DELETE FROM marks WHERE uid=?", (uid,))
    conn.commit()


def mark_counts(conn) -> dict:
    return {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM marks GROUP BY status")}


# ---------------------------------------------------------------- listing

@dataclass
class Filters:
    q: str = ""
    company: str = ""
    source: str = ""
    location: str = ""
    workplace: str = ""          # remote | hybrid | onsite
    min_salary: float | None = None
    max_yoe: int | None = None
    visa: str = ""               # "" | not_no | sponsors
    eligible_only: bool = False
    show_ghosts: bool = False
    view: str = "all"            # all | new | saved | applied | hidden
    tier: str = ""               # "" | Strong | Possible | Strong+Possible
    sort: str = "newest"         # newest | fit | salary
    since: str | None = None     # for view=new
    ghost_hide_at: int = 3
    profile_hash: str = ""


_MARK_JOIN = """
LEFT JOIN (SELECT k.cluster_id AS cid,
                  MAX(CASE m.status WHEN 'applied' THEN 3 WHEN 'saved' THEN 2 WHEN 'hidden' THEN 1 END) AS mrank
           FROM marks m CROSS JOIN jobs k ON k.uid = m.uid GROUP BY k.cluster_id) mk ON mk.cid = j.cluster_id
"""


def _fts_query(q: str) -> str:
    terms = [t.replace('"', "") for t in q.split() if t.replace('"', "")]
    return " ".join(f'"{t}"*' for t in terms)


def list_jobs(conn, f: Filters, page: int = 1, per_page: int = 50) -> tuple[list[sqlite3.Row], int]:
    """One row per near-duplicate cluster (the canonical active posting), filtered and paginated."""
    where = ["j.active = 1", "j.is_canonical = 1"]
    args: list = []
    if f.q:
        if has_fts(conn) and _fts_query(f.q):
            where.append("j.cluster_id IN (SELECT k.cluster_id FROM jobs_fts JOIN jobs k ON k.fts_rowid = jobs_fts.rowid "
                         "WHERE jobs_fts MATCH ?)")
            args.append(_fts_query(f.q))
        else:
            where.append("(j.title LIKE ? OR j.cluster_locations LIKE ? OR j.departments LIKE ? OR j.company LIKE ?)")
            args += [f"%{f.q}%"] * 4
    if f.company:
        where.append("j.company = ?"); args.append(f.company)
    if f.source:
        where.append("j.source = ?"); args.append(f.source)
    if f.location:
        where.append("j.cluster_locations LIKE ?"); args.append(f"%{f.location}%")
    if f.workplace:
        where.append("j.workplace_eff = ?"); args.append(f.workplace)
    if f.min_salary:
        where.append("j.pay_max_annual >= ?"); args.append(f.min_salary)
    if f.max_yoe is not None:
        where.append("(j.yoe_min IS NULL OR j.yoe_min <= ?)"); args.append(f.max_yoe)
    if f.visa == "not_no":
        where.append("(j.visa IS NULL OR j.visa <> 'no')")
    elif f.visa == "sponsors":
        where.append("(j.visa = 'yes' OR EXISTS (SELECT 1 FROM sponsor_match s WHERE s.company_key = j.company_key "
                     "AND (s.lca_recent > 0 OR s.uscis_recent > 0)))")
    if f.eligible_only:
        where.append("(j.elig IS NULL OR j.elig = 1)")
    if f.tier:
        tiers = ["Strong", "Possible"] if f.tier == "Strong+Possible" else [f.tier]
        where.append(f"fit.tier IN ({','.join('?' * len(tiers))})"); args += tiers

    view = f.view
    if view in ("saved", "applied", "hidden"):
        where.append("mk.mrank = ?"); args.append({"saved": 2, "applied": 3, "hidden": 1}[view])
    else:
        where.append("(mk.mrank IS NULL OR mk.mrank > 1)")  # hidden ones stay out of the main list
        if not f.show_ghosts:
            where.append("j.ghost_score < ?"); args.append(f.ghost_hide_at)
        if view == "new" and f.since:
            where.append("j.effective_first_seen > ?"); args.append(f.since)

    order = {
        "fit": "CASE fit.tier WHEN 'Strong' THEN 0 WHEN 'Possible' THEN 1 WHEN 'Weak' THEN 2 ELSE 3 END, "
               "j.effective_first_seen DESC",
        "salary": "j.pay_max_annual IS NULL, j.pay_max_annual DESC",
    }.get(f.sort, "j.effective_first_seen DESC")

    base = f"""FROM jobs j {_MARK_JOIN}
               LEFT JOIN fit ON fit.content_hash = j.content_hash AND fit.profile_hash = ?
               WHERE {' AND '.join(where)}"""
    args = [f.profile_hash] + args
    total = conn.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT j.uid, j.title, j.company, j.company_key, j.location, j.cluster_locations, j.cluster_count,
                   j.url, j.source, j.departments, j.posted_at, j.first_seen, j.effective_first_seen,
                   j.repost_count, j.ghost_score, j.ghost_reasons, j.workplace_eff, j.pay_min_annual,
                   j.pay_max_annual, j.pay_currency, j.yoe_min, j.visa, j.elig, j.elig_reasons,
                   mk.mrank, fit.tier AS fit_tier, fit.summary AS fit_summary
            {base} ORDER BY {order} LIMIT ? OFFSET ?""",
        args + [per_page, max(0, page - 1) * per_page]).fetchall()
    return rows, total


def get_job(conn: sqlite3.Connection, uid: str) -> dict | None:
    r = conn.execute("SELECT * FROM jobs WHERE uid=?", (uid,)).fetchone()
    if r is None:
        return None
    row = dict(r)
    row["content_html"] = content(conn, uid)
    return row


def content(conn, uid: str) -> str:
    r = conn.execute("SELECT html FROM job_content WHERE uid=?", (uid,)).fetchone()
    return r[0] if r else ""


def cluster_members(conn, cluster_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT uid, source, source_board, url, location, active, first_seen, posted_at, closed_at "
        "FROM jobs WHERE cluster_id=? ORDER BY COALESCE(posted_at, first_seen)", (cluster_id,)).fetchall()


def events_for(conn, uids: list[str]) -> list[sqlite3.Row]:
    if not uids:
        return []
    return conn.execute(f"SELECT uid, kind, at FROM events WHERE uid IN ({','.join('?' * len(uids))}) "
                        "ORDER BY at", uids).fetchall()


def mark_for_cluster(conn, cluster_id: str) -> str | None:
    r = conn.execute("SELECT m.status FROM marks m JOIN jobs k ON k.uid=m.uid WHERE k.cluster_id=? "
                     "ORDER BY CASE m.status WHEN 'applied' THEN 0 WHEN 'saved' THEN 1 ELSE 2 END LIMIT 1",
                     (cluster_id,)).fetchone()
    return r[0] if r else None


def companies(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT company FROM jobs WHERE active=1 AND is_canonical=1 ORDER BY company")]


def sources_in_use(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT source FROM jobs WHERE active=1 ORDER BY source")]


def stats(conn: sqlite3.Connection, ghost_hide_at: int = 3) -> dict:
    r = conn.execute("""SELECT COUNT(*) AS total,
                               SUM(is_canonical) AS unique_roles,
                               SUM(CASE WHEN is_canonical=1 AND ghost_score >= ? THEN 1 ELSE 0 END) AS hidden_ghosts,
                               MAX(last_seen) AS last_fetch
                        FROM jobs WHERE active=1""", (ghost_hide_at,)).fetchone()
    return dict(r)
