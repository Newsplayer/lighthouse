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
import zlib
from dataclasses import dataclass
from pathlib import Path

from .models import Job, company_key, iso, parse_dt, utcnow
from .sanitize import html_to_text

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "lighthouse.db"
# Readers (the normal setup) keep two files: the job index exactly as the GitHub Action published it
# (data/index/index-*.db, replaced weekly, patched by small deltas) and their own data in user.db
# (profile, marks, Claude verdicts, sponsorship imports), which the index attaches to as "idx".
USER_DB = DEFAULT_DB.parent / "user.db"
INDEX_DIR = DEFAULT_DB.parent / "index"
USER_APP_ID = 0x4C485553    # PRAGMA application_id of a user.db ("LHUS")
INDEX_APP_ID = 0x4C484958   # ... of a published job index ("LHIX")
INDEX_SCHEMA = 1            # bump when the index file's layout changes (readers then need a new app)

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
    for uid, html in conn.execute("SELECT uid, content_html FROM jobs WHERE content_html IS NOT NULL "
                                  "AND content_html <> ''").fetchall():
        conn.execute("INSERT OR REPLACE INTO job_content(uid, html) VALUES (?, ?)", (uid, pack(html)))
    try:
        conn.execute("ALTER TABLE jobs DROP COLUMN content_html")
    except sqlite3.OperationalError:  # SQLite < 3.35: leave the column, emptied
        conn.execute("UPDATE jobs SET content_html = NULL")


def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(boards)")}
    for name, decl in (("fail_count", "INTEGER NOT NULL DEFAULT 0"), ("list", "TEXT")):
        if name not in have:
            conn.execute(f"ALTER TABLE boards ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_boards_enabled ON boards(enabled)")
    # v0.2 stored descriptions as plain text; compress them in place.
    rows = conn.execute("SELECT uid, html FROM job_content WHERE typeof(html) = 'text'").fetchall()
    conn.executemany("UPDATE job_content SET html=? WHERE uid=?", [(pack(r[1]), r[0]) for r in rows])


MARK_RANK = {"applied": 3, "saved": 2, "hidden": 1}


def _migrate_3_to_4(conn: sqlite3.Connection) -> None:
    """Speed: denormalized mark rank, country codes and a row signature on jobs; partial indexes
    covering the default listing; board-level response hash."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, decl in (("mark", "INTEGER"), ("countries", "TEXT"), ("row_sig", "TEXT"), ("yoe_source", "TEXT")):
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    if "body_hash" not in {r[1] for r in conn.execute("PRAGMA table_info(boards)")}:
        conn.execute("ALTER TABLE boards ADD COLUMN body_hash TEXT")
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_canon_eff ON jobs(effective_first_seen) WHERE active = 1 AND is_canonical = 1;
        CREATE INDEX IF NOT EXISTS idx_canon_company ON jobs(company) WHERE active = 1 AND is_canonical = 1;
        CREATE INDEX IF NOT EXISTS idx_canon_source ON jobs(source) WHERE active = 1 AND is_canonical = 1;
        CREATE INDEX IF NOT EXISTS idx_canon_yoe ON jobs(yoe_min) WHERE active = 1 AND is_canonical = 1;
        CREATE INDEX IF NOT EXISTS idx_canon_pay ON jobs(pay_max_annual) WHERE active = 1 AND is_canonical = 1;
        CREATE INDEX IF NOT EXISTS idx_jobs_mark ON jobs(mark) WHERE mark IS NOT NULL;
    """)
    conn.execute("UPDATE jobs SET derived_of = NULL")  # re-derive once: years-asked fallback + countries
    sync_marks(conn)


CLAUDE_EXTRACTIONS = """
CREATE TABLE IF NOT EXISTS claude_extractions (   -- your optional Claude reading of a posting
    content_hash TEXT PRIMARY KEY,
    data         TEXT NOT NULL,     -- JSON, same shape as extractions.data
    model        TEXT,
    at           TEXT NOT NULL
);
"""

COMPANIES = """
CREATE TABLE IF NOT EXISTS companies (   -- coverage of config/companies.csv (builder; shipped in the index)
    company     TEXT PRIMARY KEY,
    grp         TEXT,
    domain      TEXT,
    careers_url TEXT,
    final_url   TEXT,
    ats         TEXT,
    slug        TEXT,
    status      TEXT,      -- ok | pinned | unsupported | not_found | blocked | error
    detail      TEXT,
    platforms   TEXT,      -- JSON list of careers systems seen on the site
    openings    INTEGER,
    checked_at  TEXT
);
"""

# Filter fields added in v5 (extract.derive fills them).
_JOB_COLUMNS_V5 = [("job_function", "TEXT"), ("seniority", "TEXT"), ("emp_type", "TEXT"), ("skills", "TEXT"),
                   ("pay_interval", "TEXT")]
_V5_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_canon_function ON jobs(job_function) WHERE active = 1 AND is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_canon_seniority ON jobs(seniority) WHERE active = 1 AND is_canonical = 1;
"""


def _migrate_4_to_5(conn: sqlite3.Connection) -> None:
    """Claude extractions get their own table (so a shared index never overwrites what you paid for),
    new filter fields, and the companies coverage table."""
    conn.executescript(CLAUDE_EXTRACTIONS + COMPANIES)
    conn.execute("INSERT OR REPLACE INTO claude_extractions(content_hash, data, model, at) "
                 "SELECT content_hash, data, model, at FROM extractions WHERE method = 'claude'")
    conn.execute("DELETE FROM extractions WHERE method = 'claude'")
    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for name, decl in _JOB_COLUMNS_V5:
        if name not in have:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
    conn.executescript(_V5_INDEXES)
    conn.execute("UPDATE jobs SET derived_of = NULL")   # derive the new fields once


MIGRATIONS = [
    lambda c: c.executescript(V1),  # 0 -> 1
    _migrate_1_to_2,                # 1 -> 2
    _migrate_2_to_3,                # 2 -> 3
    _migrate_3_to_4,                # 3 -> 4
    _migrate_4_to_5,                # 4 -> 5
]
SCHEMA_VERSION = len(MIGRATIONS)


def pack(html: str) -> bytes:
    """Descriptions are stored zlib-compressed (~2.5x smaller on real postings)."""
    return zlib.compress((html or "").encode("utf-8"), 6)


def unpack(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, memoryview)):
        return zlib.decompress(bytes(value)).decode("utf-8")
    return value  # plain text from an older v0.2 database


FTS_BODY_CHARS = 4000  # index the start of each description: plenty for search, keeps the index small


def _tune(conn, schema: str = "main") -> None:
    conn.execute(f"PRAGMA {schema}.journal_mode=WAL")
    conn.execute(f"PRAGMA {schema}.synchronous=NORMAL")
    conn.execute(f"PRAGMA {schema}.cache_size=-65536")      # 64 MB page cache
    conn.execute(f"PRAGMA {schema}.mmap_size=268435456")    # read through memory-mapped I/O (256 MB window)


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open a Lighthouse database. A user.db (reader) gets its job index attached as "idx", so every
    query can say plain `jobs`; any other file is a full single-file database (builder, local mode)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("unz", 1, unpack, deterministic=True)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    _tune(conn)
    if conn.execute("PRAGMA application_id").fetchone()[0] == USER_APP_ID:
        migrate_user(conn)
        attach_index(conn, Path(path).parent)
    else:
        migrate(conn)
    return conn


# ---------------------------------------------------------------- reader: user.db + job index

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS marks (
    uid    TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('saved', 'applied', 'hidden')),
    at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fit (
    content_hash TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    tier         TEXT NOT NULL CHECK (tier IN ('Strong', 'Possible', 'Weak')),
    summary      TEXT,
    requirements TEXT,
    method       TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sponsor_lca (
    employer_key TEXT NOT NULL, employer_name TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
    filings INTEGER NOT NULL DEFAULT 0, certified INTEGER NOT NULL DEFAULT 0,
    wage_l1 INTEGER NOT NULL DEFAULT 0, wage_l2 INTEGER NOT NULL DEFAULT 0,
    wage_l3 INTEGER NOT NULL DEFAULT 0, wage_l4 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (employer_key, fiscal_year)
);
CREATE TABLE IF NOT EXISTS sponsor_uscis (
    employer_key TEXT NOT NULL, employer_name TEXT NOT NULL, fiscal_year INTEGER NOT NULL,
    initial_approvals INTEGER NOT NULL DEFAULT 0, initial_denials INTEGER NOT NULL DEFAULT 0,
    continuing_approvals INTEGER NOT NULL DEFAULT 0, continuing_denials INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (employer_key, fiscal_year)
);
CREATE TABLE IF NOT EXISTS sponsor_files (
    file_key TEXT PRIMARY KEY, kind TEXT NOT NULL, rows INTEGER NOT NULL, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sponsor_match (
    company_key TEXT PRIMARY KEY, lca_recent INTEGER NOT NULL DEFAULT 0,
    uscis_recent INTEGER NOT NULL DEFAULT 0, names TEXT
);
""" + CLAUDE_EXTRACTIONS

USER_MIGRATIONS = [lambda c: c.executescript(USER_SCHEMA)]


def migrate_user(conn) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(USER_MIGRATIONS)):
        with conn:
            USER_MIGRATIONS[i](conn)
            conn.execute(f"PRAGMA user_version={i + 1}")


def init_user(path: Path | str = USER_DB) -> None:
    """Create a reader's user.db (no-op if it exists)."""
    path = Path(path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute(f"PRAGMA application_id={USER_APP_ID}")
    c.commit()
    c.close()


_PERSONAL_META = ("profile", "visit")


def app_db(cfg=None, data_dir: Path | None = None) -> Path:
    """The database file the app opens: user.db for readers (created, and converted from a pre-0.6
    single-file database, on first use), the full lighthouse.db otherwise (builder, local mode)."""
    if cfg is None:
        from . import config
        cfg = config.load()
    folder = Path(data_dir or DEFAULT_DB.parent)
    if not cfg.reader:
        return folder / DEFAULT_DB.name
    user = folder / USER_DB.name
    if not user.exists():
        init_user(user)
        legacy = folder / DEFAULT_DB.name
        if legacy.exists():
            _convert_legacy(legacy, user)
    return user


def _convert_legacy(legacy: Path, user: Path) -> None:
    """Copy your own data out of a pre-0.6 database into user.db. The old file (mostly job rows) is
    deleted once the first real index has been installed."""
    c = connect(user)
    try:
        c.execute("ATTACH DATABASE ? AS old", (str(legacy),))
        have = {r[0] for r in c.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
        with c:
            for t in ("marks", "fit", "api_usage", "claude_batches", "sponsor_lca", "sponsor_uscis", "sponsor_files",
                      "sponsor_match"):
                if t in have:
                    cols = [r[1] for r in c.execute(f"PRAGMA old.table_info({t})")
                            if r[1] in {x[1] for x in c.execute(f"PRAGMA main.table_info({t})")}]
                    c.execute(f"INSERT OR IGNORE INTO main.{t} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM old.{t}")
            if "claude_extractions" in have:
                c.execute("INSERT OR IGNORE INTO main.claude_extractions SELECT content_hash, data, model, at "
                          "FROM old.claude_extractions")
            if "extractions" in have:
                c.execute("INSERT OR IGNORE INTO main.claude_extractions SELECT content_hash, data, model, at "
                          "FROM old.extractions WHERE method = 'claude'")
            if "meta" in have:
                c.executemany("INSERT OR REPLACE INTO main.meta (key, value) SELECT key, value FROM old.meta WHERE key = ?",
                              [(k,) for k in _PERSONAL_META])
        c.execute("DETACH DATABASE old")
        set_meta(c, "legacy_db", legacy.name)
    finally:
        c.close()


def is_user_db(conn) -> bool:
    return conn.execute("PRAGMA main.application_id").fetchone()[0] == USER_APP_ID


def index_path(conn, folder: Path | str | None = None) -> Path | None:
    name = get_meta(conn, "index_file")
    return (Path(folder or INDEX_DIR.parent) / name) if name else None


def attach_index(conn, folder: Path) -> None:
    """Attach the current job index as "idx" (an empty one until the first download)."""
    rel = get_meta(conn, "index_file")
    path = Path(folder) / rel if rel else None
    if path is None or not path.exists():
        path = Path(folder) / "index" / "index-empty.db"
        if not path.exists():
            create_empty_index(path)
        set_meta(conn, "index_file", str(path.relative_to(folder)).replace("\\", "/"))
    conn.execute("ATTACH DATABASE ? AS idx", (str(path),))
    _tune(conn, "idx")


def create_empty_index(path: Path) -> None:
    """An index with the published layout and no rows (before the first download)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(":memory:")
    for i in range(SCHEMA_VERSION):
        MIGRATIONS[i](src)
    cols = [(r[1], r[2]) for r in src.execute("PRAGMA table_info(jobs)")]
    src.close()
    c = sqlite3.connect(tmp)
    create_index_tables(c, [x for x in cols if x[0] not in INDEX_EXCLUDED_COLUMNS], fts=True)
    c.commit()
    c.close()
    tmp.replace(path)


# Job columns that never go into a published index: personal (eligibility is computed per reader),
# or builder-only (near-duplicate signatures).
INDEX_EXCLUDED_COLUMNS = {"elig", "elig_reasons", "elig_version", "minhash", "minhash_of"}

INDEX_TABLES = """
CREATE TABLE IF NOT EXISTS job_content (uid TEXT PRIMARY KEY, html TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS boards (
    ats TEXT NOT NULL, slug TEXT NOT NULL, company TEXT, enabled INTEGER NOT NULL DEFAULT 1,
    origin TEXT NOT NULL DEFAULT 'config', options TEXT, added_at TEXT NOT NULL, last_fetch TEXT, last_ok TEXT,
    last_error TEXT, job_count INTEGER, fail_count INTEGER NOT NULL DEFAULT 0, list TEXT, body_hash TEXT,
    PRIMARY KEY (ats, slug)
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, uid TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS extractions (
    content_hash TEXT PRIMARY KEY, method TEXT NOT NULL, data TEXT NOT NULL, model TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT);
""" + COMPANIES

INDEX_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_uid ON events(uid);
CREATE INDEX IF NOT EXISTS idx_jobs_ck ON jobs(company_key);
CREATE INDEX IF NOT EXISTS idx_jobs_cluster ON jobs(cluster_id);
CREATE INDEX IF NOT EXISTS idx_jobs_board ON jobs(source, source_board);
CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(content_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_fts ON jobs(fts_rowid);
CREATE INDEX IF NOT EXISTS idx_canon_eff ON jobs(effective_first_seen) WHERE active = 1 AND is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_canon_company ON jobs(company) WHERE active = 1 AND is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_canon_source ON jobs(source) WHERE active = 1 AND is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_canon_yoe ON jobs(yoe_min) WHERE active = 1 AND is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_canon_pay ON jobs(pay_max_annual) WHERE active = 1 AND is_canonical = 1;
CREATE INDEX IF NOT EXISTS idx_jobs_mark ON jobs(mark) WHERE mark IS NOT NULL;
""" + _V5_INDEXES

# Search index of a published index: contentless (the text isn't stored twice) and without term
# positions, which keeps it ~15% of the file. Queries are AND-ed prefix terms, which this supports.
INDEX_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(title, company, location, body, "
             "tokenize = 'porter unicode61', content = '', detail = none)")


def create_index_tables(conn, job_columns: list[tuple[str, str]], fts: bool = True, indexes: bool = True) -> None:
    """The published index layout. job_columns: (name, declared type) of the shipped job columns."""
    cols = ",\n    ".join(f"{n} {t or ''}{' PRIMARY KEY' if n == 'uid' else ''}" for n, t in job_columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS jobs (\n    {cols}\n)")
    conn.executescript(INDEX_TABLES)
    if indexes:
        conn.executescript(INDEX_INDEXES)
    if fts:
        conn.execute(INDEX_FTS)
    conn.execute(f"PRAGMA application_id={INDEX_APP_ID}")
    conn.execute(f"PRAGMA user_version={INDEX_SCHEMA}")


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0 and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
        version = 1  # pre-versioning database from v0.1
    for i in range(version, SCHEMA_VERSION):
        with conn:
            MIGRATIONS[i](conn)
            conn.execute(f"PRAGMA user_version={i + 1}")


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    """In the main database or the attached job index."""
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name=?", (name,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM pragma_database_list WHERE name='idx'").fetchone():
        return conn.execute("SELECT 1 FROM idx.sqlite_master WHERE name=?", (name,)).fetchone() is not None
    return False


def has_fts(conn: sqlite3.Connection) -> bool:
    return has_table(conn, "jobs_fts")


def is_reader(conn) -> bool:
    return conn.execute("SELECT 1 FROM pragma_database_list WHERE name='idx'").fetchone() is not None


def _fts_put(conn, uid, title, company, location, html) -> None:
    old = conn.execute("SELECT fts_rowid FROM jobs WHERE uid=?", (uid,)).fetchone()
    if old and old[0] is not None:
        conn.execute("DELETE FROM jobs_fts WHERE rowid=?", (old[0],))
    cur = conn.execute("INSERT INTO jobs_fts(title, company, location, body) VALUES (?,?,?,?)",
                       (title, company, location or "", html_to_text(html or "")[:FTS_BODY_CHARS]))
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
    """Boards from sources.toml and config/boards/*.csv. On/off comes from the file only the first
    time a board is seen; after that the Sources page owns it."""
    now = utcnow()
    with conn:
        conn.executemany(
            """INSERT INTO boards(ats, slug, company, options, origin, added_at, enabled, list)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ats, slug) DO UPDATE SET
                   company=COALESCE(boards.company, excluded.company),
                   options=COALESCE(excluded.options, boards.options),
                   list=COALESCE(boards.list, excluded.list)""",
            [(b["ats"], b["slug"], b.get("company"), json.dumps(b["options"]) if b.get("options") else None,
              "list" if b.get("list") else "config", now, int(b.get("enabled", True)), b.get("list"))
             for b in boards])


def add_board(conn, ats: str, slug: str, company: str | None = None, origin: str = "ui") -> bool:
    cur = conn.execute(
        """INSERT INTO boards(ats, slug, company, origin, added_at) VALUES (?,?,?,?,?)
           ON CONFLICT(ats, slug) DO UPDATE SET enabled=1""",
        (ats, slug, company, origin, utcnow()))
    conn.commit()
    return cur.rowcount > 0


def set_board_enabled(conn, ats: str, slug: str, enabled: bool) -> None:
    conn.execute("UPDATE boards SET enabled=?, fail_count=0 WHERE ats=? AND slug=?", (int(enabled), ats, slug))
    conn.commit()


def list_boards(conn, enabled_only: bool = False, show: str = "all", q: str = "", ats: str = "") -> list[sqlite3.Row]:
    where, args = ["1=1"], []
    if enabled_only or show == "on":
        where.append("enabled=1")
    elif show == "off":
        where.append("enabled=0")
    elif show == "errors":
        where.append("last_error IS NOT NULL")
    if q:
        where.append("(slug LIKE ? OR company LIKE ?)"); args += [f"%{q}%"] * 2
    if ats:
        where.append("ats = ?"); args.append(ats)
    return conn.execute(f"SELECT * FROM boards WHERE {' AND '.join(where)} "
                        "ORDER BY enabled DESC, COALESCE(company, slug) COLLATE NOCASE", args).fetchall()


def board_result(conn, ats: str, slug: str, *, error: str | None = None, count: int | None = None,
                 not_found: bool = False, disable_after: int = 0, body_hash: str | None = None) -> bool:
    """Record a fetch outcome. Returns True if the board was switched off (not found N runs in a row).
    Network errors and timeouts never switch a board off; only an explicit "not found" does."""
    now = utcnow()
    disabled = False
    if error:
        conn.execute("UPDATE boards SET last_fetch=?, last_error=?, fail_count=fail_count + ? WHERE ats=? AND slug=?",
                     (now, error[:500], int(not_found), ats, slug))
        if not_found and disable_after:
            cur = conn.execute("UPDATE boards SET enabled=0, last_error='Switched off automatically: ' || last_error "
                               "WHERE ats=? AND slug=? AND fail_count >= ? AND enabled=1", (ats, slug, disable_after))
            disabled = cur.rowcount > 0
    else:
        conn.execute("UPDATE boards SET last_fetch=?, last_ok=?, last_error=NULL, job_count=?, fail_count=0, "
                     "body_hash=COALESCE(?, body_hash) WHERE ats=? AND slug=?", (now, now, count, body_hash, ats, slug))
    conn.commit()
    return disabled


def board_unchanged(conn, ats: str, slug: str) -> None:
    """Board answered with exactly the same bytes as last run: just mark it seen."""
    now = utcnow()
    conn.execute("UPDATE jobs SET last_seen=? WHERE source=? AND source_board=? AND active=1", (now, ats, slug))
    conn.execute("UPDATE boards SET last_fetch=?, last_ok=?, last_error=NULL, fail_count=0 WHERE ats=? AND slug=?",
                 (now, now, ats, slug))
    conn.commit()


def board_counts(conn) -> dict:
    r = conn.execute("""SELECT COUNT(*) total, SUM(enabled) enabled,
                               SUM(CASE WHEN enabled=1 AND last_error IS NOT NULL THEN 1 ELSE 0 END) errors,
                               SUM(CASE WHEN enabled=0 AND last_error LIKE 'Switched off automatically%' THEN 1 ELSE 0 END) auto_off,
                               SUM(CASE WHEN enabled=1 AND last_fetch IS NULL THEN 1 ELSE 0 END) never
                        FROM boards""").fetchone()
    return {k: r[k] or 0 for k in r.keys()}


# ---------------------------------------------------------------- ingest

def known_postings(conn, source: str, board: str) -> dict[str, tuple[str, bool]]:
    """uid -> (detail_marker, has_content). Lets sources skip detail calls for unchanged postings."""
    return {r[0]: (r[1] or "", bool(r[2])) for r in conn.execute(
        "SELECT j.uid, j.detail_marker, c.uid IS NOT NULL "
        "FROM jobs j LEFT JOIN job_content c ON c.uid = j.uid WHERE j.source=? AND j.source_board=?",
        (source, board))}


def _row_sig(j: Job, chash: str, marker: str) -> str:
    import hashlib
    parts = (j.fingerprint, j.title, j.company, j.location, j.url, chash, json.dumps(j.departments), iso(j.posted_at),
             iso(j.updated_at), j.workplace, j.employment_type, j.salary_min, j.salary_max, j.salary_currency,
             j.salary_interval, json.dumps(j.locations), marker)
    return hashlib.blake2b(repr(parts).encode(), digest_size=12).hexdigest()


def upsert_board(conn: sqlite3.Connection, source: str, board: str, jobs: list[Job],
                 keep_alive: set[str] | None = None) -> dict:
    """Write one board's fresh listing. Returns counts plus which uids/companies changed.

    Postings that are no longer listed are marked inactive (kept for history).
    An empty content_html never overwrites a stored copy (sources that need a detail
    call per posting only fetch new/changed ones).
    Rows whose fields are identical to last time (row_sig) are not rewritten; one UPDATE at
    the end refreshes last_seen for the whole board.
    keep_alive: uids the board still lists but that were filtered out this run (title/country
    filters) — they are neither updated nor recorded as closed.
    """
    now = utcnow()
    keep_alive = keep_alive or set()
    existing = {r["uid"]: r for r in conn.execute(
        "SELECT uid, active, content_hash, title, location, url, detail_marker, row_sig FROM jobs "
        "WHERE source=? AND source_board=?", (source, board))}
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
                          or old["title"] != j.title or (not j.partial and (old["location"] or "") != j.location))
            sig = _row_sig(j, chash, marker)
            if old is not None and old["active"] and not is_changed and (
                    old["row_sig"] == sig or (j.partial and old["url"] == j.url)):
                continue  # nothing new: no write (last_seen is refreshed for the board below)
            conn.execute(
                """INSERT INTO jobs (uid, fingerprint, source, source_board, external_id, title, company,
                       company_key, location, url, content_hash, departments, posted_at,
                       updated_at, first_seen, last_seen, active, ghost_reasons, detail_marker, workplace,
                       employment_type, salary_min, salary_max, salary_currency, salary_interval, locations,
                       effective_first_seen, cluster_id, row_sig)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'[]',?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uid) DO UPDATE SET
                       fingerprint=CASE WHEN ? THEN jobs.fingerprint ELSE excluded.fingerprint END,
                       title=excluded.title, company=excluded.company, company_key=excluded.company_key,
                       location=CASE WHEN ? THEN jobs.location ELSE excluded.location END, url=excluded.url,
                       content_hash=excluded.content_hash, departments=excluded.departments,
                       posted_at=CASE WHEN ? THEN COALESCE(jobs.posted_at, excluded.posted_at) ELSE excluded.posted_at END,
                       updated_at=excluded.updated_at,
                       last_seen=excluded.last_seen, active=1, closed_at=NULL,
                       reopened_count=jobs.reopened_count + (1 - jobs.active),
                       detail_marker=excluded.detail_marker,
                       workplace=CASE WHEN ? THEN COALESCE(jobs.workplace, excluded.workplace) ELSE excluded.workplace END,
                       employment_type=COALESCE(excluded.employment_type, jobs.employment_type),
                       salary_min=excluded.salary_min, salary_max=excluded.salary_max,
                       salary_currency=excluded.salary_currency, salary_interval=excluded.salary_interval,
                       locations=CASE WHEN ? THEN jobs.locations ELSE excluded.locations END,
                       row_sig=excluded.row_sig""",
                (j.uid, j.fingerprint, j.source, j.source_board, j.external_id, j.title, j.company,
                 company_key(j.company), j.location, j.url, chash,
                 json.dumps(j.departments), iso(j.posted_at), iso(j.updated_at), now, now,
                 marker, j.workplace, j.employment_type, j.salary_min, j.salary_max,
                 j.salary_currency, j.salary_interval, json.dumps(j.locations or ([j.location] if j.location else [])),
                 now, j.uid, sig, *([int(j.partial and old is not None)] * 5)))
            if j.content_html and (old is None or old["content_hash"] != chash or not old["active"]):
                conn.execute("INSERT INTO job_content(uid, html) VALUES (?, ?) "
                             "ON CONFLICT(uid) DO UPDATE SET html=excluded.html", (j.uid, pack(j.content_html)))
            if is_changed:
                changed.add(j.uid)
                touched.add(company_key(j.company))
                if fts and j.content_html:
                    _fts_put(conn, j.uid, j.title, j.company, j.location, j.content_html)
        conn.execute("UPDATE jobs SET last_seen=? WHERE source=? AND source_board=? AND active=1", (now, source, board))
        gone = [u for u, r in existing.items() if r["active"] and u not in seen and u not in keep_alive]
        conn.executemany("UPDATE jobs SET active=0, closed_at=? WHERE uid=?", [(now, u) for u in gone])
        events += [(u, "closed", now) for u in gone]
        conn.executemany("INSERT INTO events(uid, kind, at) VALUES (?,?,?)", events)
        if gone:
            touched |= {r[0] for r in conn.execute(
                f"SELECT DISTINCT company_key FROM jobs WHERE uid IN ({','.join('?' * len(gone))})", gone)}
    new = sum(1 for e in events if e[1] == "new")
    return {"new": new, "updated": len(changed) - new, "unchanged": len(seen) - len(changed),
            "closed": len(gone), "changed": changed, "touched": touched}


# ---------------------------------------------------------------- marks

def set_mark(conn, uid: str, status: str | None) -> None:
    if status:
        conn.execute("INSERT INTO marks(uid, status, at) VALUES (?,?,?) "
                     "ON CONFLICT(uid) DO UPDATE SET status=excluded.status, at=excluded.at",
                     (uid, status, utcnow()))
    else:
        conn.execute("DELETE FROM marks WHERE uid=?", (uid,))
    r = conn.execute("SELECT cluster_id FROM jobs WHERE uid=?", (uid,)).fetchone()
    sync_marks(conn, [r[0]] if r and r[0] else None)
    conn.commit()


def sync_marks(conn, cluster_ids=None) -> None:
    """jobs.mark = strongest mark (applied > saved > hidden) on any listing in the row's cluster.
    Denormalized so the job list never joins the marks table."""
    if cluster_ids is None:
        conn.execute("UPDATE jobs SET mark = NULL WHERE mark IS NOT NULL")
        cluster_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT k.cluster_id FROM marks m JOIN jobs k ON k.uid = m.uid WHERE k.cluster_id IS NOT NULL")]
    for cid in cluster_ids:
        r = conn.execute("SELECT MAX(CASE m.status WHEN 'applied' THEN 3 WHEN 'saved' THEN 2 WHEN 'hidden' THEN 1 END) "
                         "FROM marks m JOIN jobs k ON k.uid = m.uid WHERE k.cluster_id = ?", (cid,)).fetchone()
        conn.execute("UPDATE jobs SET mark = ? WHERE cluster_id = ?", (r[0], cid))


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
    yoe_lo: int | None = None    # years of experience asked: at least
    yoe_hi: int | None = None    # years of experience asked: at most
    yoe_unknown: bool = True     # keep roles that don't say (or can't be inferred)
    max_yoe: int | None = None   # older name for yoe_hi
    visa: str = ""               # "" | not_no | sponsors
    eligible_only: bool = False
    show_ghosts: bool = False
    view: str = "all"            # all | new | saved | applied | hidden
    tier: str = ""               # "" | Strong | Possible | Strong+Possible
    sort: str = "newest"         # newest | fit | salary | yoe
    since: str | None = None     # for view=new
    ghost_hide_at: int = 3
    profile_hash: str = ""
    countries: tuple = ()        # e.g. ("US", "CA"); empty = everywhere
    profile: dict | None = None  # for eligibility (filter + reasons on the rows shown)
    yoe_slack: int = 2
    function: str = ""           # job_function, e.g. "Software Engineering"
    seniority: str = ""          # intern | entry | mid | senior | lead | manager | director | executive
    emp_type: str = ""           # full_time | part_time | contract | internship | temporary
    skills: tuple = ()           # every one must be mentioned, e.g. ("python", "sql")


class Count(int):
    """A result count that may be capped (shown as "5,000+")."""
    capped = False


COUNT_CAP = 5000
_LIST_COLS = """j.uid, j.title, j.company, j.company_key, j.location, j.cluster_locations, j.cluster_count,
    j.url, j.source, j.departments, j.posted_at, j.first_seen, j.effective_first_seen, j.content_hash,
    j.repost_count, j.ghost_score, j.ghost_reasons, j.workplace_eff, j.pay_min_annual, j.pay_max_annual,
    j.pay_currency, j.pay_interval, j.yoe_min, j.yoe_source, j.visa, j.citizenship, j.clearance, j.degree,
    j.job_function, j.seniority, j.emp_type, j.skills, j.mark AS mrank"""


def _fts_query(q: str) -> str:
    """Every word must appear (prefix match). Words are split on punctuation ("full-stack" -> full,
    stack) so no query needs term positions, which the published search index doesn't store."""
    import re
    terms = [t for t in re.split(r"[^\w]+", q or "", flags=re.UNICODE) if t]
    return " ".join(f'"{t}"*' for t in terms)


def _where(conn, f: Filters) -> tuple[list[str], list, bool]:
    """WHERE clauses over jobs only (no joins) + whether the fit table is needed."""
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
    lo, hi = f.yoe_lo, f.yoe_hi if f.yoe_hi is not None else f.max_yoe
    if lo is not None or hi is not None or not f.yoe_unknown:
        conds, cargs = ["j.yoe_min IS NOT NULL"], []
        if lo is not None:
            conds.append("j.yoe_min >= ?"); cargs.append(lo)
        if hi is not None:
            conds.append("j.yoe_min <= ?"); cargs.append(hi)
        clause = " AND ".join(conds)
        where.append(f"(j.yoe_min IS NULL OR ({clause}))" if f.yoe_unknown else f"({clause})")
        args += cargs
    if f.countries:
        ors = " OR ".join("j.countries LIKE ?" for _ in f.countries)
        where.append(f"(j.countries IS NULL OR j.countries = '' OR {ors})")
        args += [f"%,{c},%" for c in f.countries]
    if f.visa == "not_no":
        where.append("(j.visa IS NULL OR j.visa <> 'no')")
    elif f.visa == "sponsors":
        where.append("(j.visa = 'yes' OR EXISTS (SELECT 1 FROM sponsor_match s WHERE s.company_key = j.company_key "
                     "AND (s.lca_recent > 0 OR s.uscis_recent > 0)))")
    if f.function:
        where.append("j.job_function = ?"); args.append(f.function)
    if f.seniority:
        where.append("j.seniority = ?"); args.append(f.seniority)
    if f.emp_type:
        where.append("j.emp_type = ?"); args.append(f.emp_type)
    for sk in f.skills:
        where.append("j.skills LIKE ?"); args.append(f"%,{sk.strip().lower()},%")
    if f.eligible_only and f.profile:
        from .profile import eligibility_sql
        clause = eligibility_sql(conn, f.profile, f.yoe_slack)
        if clause:
            where.append(f"({clause[0]})"); args += clause[1]
    need_fit = bool(f.tier) or f.sort == "fit"
    if f.tier:
        tiers = ["Strong", "Possible"] if f.tier == "Strong+Possible" else [f.tier]
        where.append(f"fit.tier IN ({','.join('?' * len(tiers))})"); args += tiers
    if f.view in ("saved", "applied", "hidden"):
        where.append("j.mark = ?"); args.append(MARK_RANK[f.view])
    else:
        where.append("(j.mark IS NULL OR j.mark > 1)")  # hidden ones stay out of the main list
        if not f.show_ghosts:
            where.append("j.ghost_score < ?"); args.append(f.ghost_hide_at)
        if f.view == "new" and f.since:
            where.append("j.effective_first_seen > ?"); args.append(f.since)
    return where, args, need_fit


def count_jobs(conn, f: Filters, cap: int = COUNT_CAP) -> Count:
    where, args, need_fit = _where(conn, f)
    join = "LEFT JOIN fit ON fit.content_hash = j.content_hash AND fit.profile_hash = ?" if need_fit else ""
    jargs = [f.profile_hash] if need_fit else []
    n = conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM jobs j {join} WHERE {' AND '.join(where)} LIMIT ?)",
                     jargs + args + [cap + 1]).fetchone()[0]
    out = Count(min(n, cap))
    out.capped = n > cap
    return out


def list_jobs(conn, f: Filters, page: int = 1, per_page: int = 50, with_count: bool = True):
    """One row per near-duplicate cluster (the canonical active posting), filtered and paginated.

    The query touches only the jobs table (marks and filter fields are denormalized onto it),
    so the default view walks a partial index and stops after one page. Fit tiers for the rows
    on the page are looked up afterwards (50 index probes) unless a fit filter/sort needs the join.
    Counts are capped at COUNT_CAP so "how many" never costs a full scan.
    """
    where, args, need_fit = _where(conn, f)
    order = {
        "fit": "CASE fit.tier WHEN 'Strong' THEN 0 WHEN 'Possible' THEN 1 WHEN 'Weak' THEN 2 ELSE 3 END, "
               "j.effective_first_seen DESC",
        "salary": "j.pay_max_annual IS NULL, j.pay_max_annual DESC",
        "yoe": "j.yoe_min IS NULL, j.yoe_min ASC, j.effective_first_seen DESC",
    }.get(f.sort, "j.effective_first_seen DESC")
    join = "LEFT JOIN fit ON fit.content_hash = j.content_hash AND fit.profile_hash = ?" if need_fit else ""
    jargs = [f.profile_hash] if need_fit else []
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_LIST_COLS} FROM jobs j {join} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
        jargs + args + [per_page, max(0, page - 1) * per_page])]
    hashes = [r["content_hash"] for r in rows if r["content_hash"]]
    tiers = {}
    if hashes and f.profile_hash:
        tiers = {r[0]: (r[1], r[2]) for r in conn.execute(
            f"SELECT content_hash, tier, summary FROM fit WHERE profile_hash = ? "
            f"AND content_hash IN ({','.join('?' * len(hashes))})", [f.profile_hash, *hashes])}
    for r in rows:
        r["fit_tier"], r["fit_summary"] = tiers.get(r["content_hash"], (None, None))
    from .profile import annotate
    annotate(rows, f.profile or {}, f.yoe_slack)
    total = count_jobs(conn, f) if with_count else Count(len(rows))
    return rows, total


def get_job(conn: sqlite3.Connection, uid: str, profile: dict | None = None, yoe_slack: int = 2) -> dict | None:
    r = conn.execute("SELECT * FROM jobs WHERE uid=?", (uid,)).fetchone()
    if r is None:
        return None
    row = dict(r)
    row["content_html"] = content(conn, uid)
    from .profile import annotate
    annotate([row], profile or {}, yoe_slack)
    return row


def content(conn, uid: str) -> str:
    r = conn.execute("SELECT html FROM job_content WHERE uid=?", (uid,)).fetchone()
    return unpack(r[0]) if r else ""


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


def generation(conn) -> int:
    """Bumped whenever the job data changes; web pages cache facet lists per generation."""
    return int(get_meta(conn, "generation", 0) or 0)


def bump_generation(conn) -> None:
    set_meta(conn, "generation", generation(conn) + 1)


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
