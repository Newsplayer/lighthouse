"""SQLite storage for the builder (the GitHub Action's working database, data/builder.db).

- Writes only touch rows whose content changed (content_hash, row_sig).
- Duplicates are never computed on the fly: postprocess (dedupe.py) stores cluster_id / is_canonical /
  effective_first_seen per row, so "open roles" is a plain SELECT ... WHERE active AND is_canonical.
- Descriptions live apart from the jobs table (job_content, zlib-compressed), so scans read small rows.
- The list queries at the end (Filters, list_jobs) are the reference the website's filters are tested
  against (tests/test_site.py runs site/logic.js against them).
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Job, company_key, iso, strings, utcnow

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "builder.db"

SCHEMA = """
CREATE TABLE jobs (
    uid           TEXT PRIMARY KEY,      -- "<system>:<board>:<the system's id>"
    fingerprint   TEXT NOT NULL,         -- company | title | location, normalized: exact duplicates share it
    source        TEXT NOT NULL,         -- careers system, e.g. "greenhouse"
    source_board  TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    company_key   TEXT,
    location      TEXT,
    locations     TEXT,                  -- JSON list: every place this listing names
    url           TEXT,
    departments   TEXT,                  -- JSON list
    posted_at     TEXT,
    updated_at    TEXT,
    expires_at    TEXT,                  -- the posting's own closing date, when it gives one
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    closed_at     TEXT,
    reopened_count INTEGER NOT NULL DEFAULT 0,
    missed        INTEGER NOT NULL DEFAULT 0,   -- listings in a row that left this posting out
    content_hash  TEXT,
    detail_marker TEXT,
    row_sig       TEXT,
    -- what the careers system itself says
    workplace TEXT, employment_type TEXT, salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
    -- near-duplicate clustering (dedupe.py): one role = one cluster, shown by its canonical row
    minhash BLOB, minhash_of TEXT, cluster_id TEXT, is_canonical INTEGER NOT NULL DEFAULT 0,
    cluster_count INTEGER NOT NULL DEFAULT 1, cluster_locations TEXT, effective_first_seen TEXT,
    repost_count INTEGER NOT NULL DEFAULT 0,
    -- ghost score (ghost.py)
    ghost_score INTEGER NOT NULL DEFAULT 0, ghost_reasons TEXT,
    -- filter fields: the careers system's structure, else the rules, else the gap fill (extract.derive)
    workplace_eff TEXT, pay_min_annual REAL, pay_max_annual REAL, pay_currency TEXT, pay_interval TEXT,
    yoe_min INTEGER, yoe_source TEXT, visa TEXT, clearance TEXT, citizenship INTEGER, degree TEXT,
    job_function TEXT, seniority TEXT, emp_type TEXT, skills TEXT, benefits TEXT, travel TEXT, languages TEXT,
    countries TEXT, derived_of TEXT
);
CREATE INDEX idx_jobs_fp ON jobs(fingerprint);
CREATE INDEX idx_jobs_active ON jobs(active);
CREATE INDEX idx_jobs_ck ON jobs(company_key);
CREATE INDEX idx_jobs_cluster ON jobs(cluster_id);
CREATE INDEX idx_jobs_board ON jobs(source, source_board);
CREATE INDEX idx_jobs_hash ON jobs(content_hash);
CREATE INDEX idx_jobs_closed ON jobs(closed_at) WHERE active = 0;
CREATE INDEX idx_jobs_expires ON jobs(expires_at) WHERE expires_at IS NOT NULL AND active = 1;
CREATE INDEX idx_canon_eff ON jobs(effective_first_seen) WHERE active = 1 AND is_canonical = 1;
CREATE TABLE job_content (uid TEXT PRIMARY KEY, html TEXT NOT NULL);   -- sanitized description, zlib (pack)
CREATE TABLE boards (
    ats        TEXT NOT NULL,
    slug       TEXT NOT NULL,
    company    TEXT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    origin     TEXT NOT NULL DEFAULT 'config',
    options    TEXT,
    added_at   TEXT NOT NULL,
    last_fetch TEXT,
    last_ok    TEXT,
    last_error TEXT,
    job_count  INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    list       TEXT,                     -- which list it came from (a file in config/boards/, or "companies")
    body_hash  TEXT,                     -- one-request boards: hash of the last response
    held_closures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ats, slug)
);
CREATE INDEX idx_boards_enabled ON boards(enabled);
CREATE TABLE events (id INTEGER PRIMARY KEY, uid TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL);  -- new | closed | reopened
CREATE INDEX idx_events_uid ON events(uid);
CREATE TABLE extractions (               -- what the rules read from a description, by description hash
    content_hash TEXT PRIMARY KEY,
    method       TEXT NOT NULL,
    data         TEXT NOT NULL,          -- JSON: {field: {"value": ..., "evidence": "..."}}
    model        TEXT,                   -- the rules version
    at           TEXT NOT NULL,
    unresolved   TEXT                    -- fields the posting talks about but the rules couldn't settle
);
CREATE INDEX idx_extractions_unresolved ON extractions(content_hash) WHERE unresolved IS NOT NULL;
CREATE TABLE llm_extractions (           -- the gap fill's readings, kept by description hash (a repost never costs twice)
    content_hash TEXT PRIMARY KEY, data TEXT NOT NULL, model TEXT, at TEXT NOT NULL
);
CREATE TABLE companies (                 -- coverage of config/companies.csv
    company TEXT PRIMARY KEY, grp TEXT, domain TEXT, careers_url TEXT, final_url TEXT, ats TEXT, slug TEXT,
    status TEXT,                         -- ok | pinned | unsupported | not_found | blocked | error
    detail TEXT, platforms TEXT, openings INTEGER, checked_at TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
SCHEMA_VERSION = 9
# Left behind in a database from before the local app was retired (schema 8): dropped once.
_RETIRED_TABLES = ("marks", "fit", "api_usage", "claude_batches", "claude_extractions", "sponsor_lca", "sponsor_uscis",
                   "sponsor_files", "sponsor_match", "tombstones", "export_sig", "index_zdicts", "jobs_fts")
_RETIRED_INDEXES = ("idx_jobs_fts", "idx_jobs_mark", "idx_jobs_list", "idx_canon_company", "idx_canon_function",
                    "idx_canon_pay", "idx_canon_seniority", "idx_canon_source", "idx_canon_yoe")
# ...and its job columns, which such a database still has (harmless, never read, never published).
RETIRED_JOB_COLUMNS = {"elig", "elig_reasons", "elig_version", "fts_rowid", "mark"}


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    with conn:
        if version == 0 and not has_table(conn, "jobs"):
            conn.executescript(SCHEMA)
        elif version == 8:
            for name in _RETIRED_INDEXES:
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            for name in _RETIRED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {name}")
        else:
            raise RuntimeError(f"this database is from an older Lighthouse (schema {version}): delete it and "
                               "start again, or seed a new one from the published jobs.db.xz")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def pack(html: str) -> bytes:
    """Descriptions are stored zlib-compressed (~2.5x smaller on real postings)."""
    return zlib.compress((html or "").encode("utf-8"), 6)


def unpack(value) -> str:
    return zlib.decompress(bytes(value)).decode("utf-8") if value is not None else ""


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("unz", 1, unpack, deterministic=True)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-65536")      # 64 MB page cache
    conn.execute("PRAGMA mmap_size=268435456")    # read through memory-mapped I/O (256 MB window)
    migrate(conn)
    return conn


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None



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
    """Boards from sources.toml and config/boards/*.csv (snapshot.apply_board_list then sets on/off)."""
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


def list_boards(conn, enabled_only: bool = False) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM boards {'WHERE enabled = 1' if enabled_only else ''} "
                        "ORDER BY enabled DESC, COALESCE(company, slug) COLLATE NOCASE").fetchall()


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


def _close(conn, uids, now: str) -> set[str]:
    """Mark postings closed, with a 'closed' event each. Returns their companies (to re-cluster)."""
    uids = list(uids)
    keys = set()
    for i in range(0, len(uids), 500):
        part = uids[i:i + 500]
        keys |= {r[0] for r in conn.execute(
            f"SELECT DISTINCT company_key FROM jobs WHERE uid IN ({','.join('?' * len(part))})", part)}
    conn.executemany("UPDATE jobs SET active=0, closed_at=?, missed=0 WHERE uid=?", [(now, u) for u in uids])
    conn.executemany("INSERT INTO events(uid, kind, at) VALUES (?, 'closed', ?)", [(u, now) for u in uids])
    return keys


def close_expired(conn) -> set[str]:
    """Close open postings whose own closing date has passed (a careers site's page can stay up, and in
    its sitemap, long after). Returns their companies (to re-cluster)."""
    now = utcnow()
    with conn:
        return _close(conn, [r[0] for r in conn.execute(
            "SELECT uid FROM jobs WHERE active=1 AND expires_at IS NOT NULL AND expires_at <= ?", (now,))], now)


def board_unchanged(conn, ats: str, slug: str) -> set[str]:
    """Board answered with exactly the same bytes as last run: mark it seen. Postings that run had
    already left out (held closures) are still left out, so they close now. Returns the companies
    whose postings closed (to re-cluster)."""
    now = utcnow()
    gone = [r[0] for r in conn.execute("SELECT uid FROM jobs WHERE source=? AND source_board=? AND active=1 "
                                       "AND missed >= 1", (ats, slug))]
    touched = _close(conn, gone, now)
    conn.execute("UPDATE jobs SET last_seen=? WHERE source=? AND source_board=? AND active=1", (now, ats, slug))
    conn.execute("UPDATE boards SET last_fetch=?, last_ok=?, last_error=NULL, fail_count=0, held_closures=0 "
                 "WHERE ats=? AND slug=?", (now, now, ats, slug))
    conn.commit()
    return touched


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
    if j.expires_at:
        parts += (iso(j.expires_at),)
    return hashlib.blake2b(repr(parts).encode(), digest_size=12).hexdigest()


MASS_CLOSE_MIN = 20       # boards with at least this many open postings...
MASS_CLOSE_SHARE = 0.5    # ...that suddenly stop listing more than this share of them: hold, confirm next run


def upsert_board(conn: sqlite3.Connection, source: str, board: str, jobs: list[Job],
                 keep_alive: set[str] | None = None, hold_mass_closures: bool = True,
                 close_after: int = 1) -> dict:
    """Write one board's fresh listing. Returns counts plus which uids/companies changed.

    Postings that are no longer listed are marked inactive (kept for history; purge_closed deletes
    them later) once `close_after` successful listings in a row have left them out (paged listings
    skip postings when the order shifts between pages); until then they stay open with last_seen
    untouched. If a board that had 20+ open postings suddenly lists fewer than half of them, that
    is far more often an API hiccup than mass closure: the closures are held (the postings stay open)
    and only carried out if the next successful fetch agrees.
    A posting whose own closing date (expires_at) has passed is closed now, however the board lists it.
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
        "SELECT uid, active, content_hash, title, location, url, detail_marker, row_sig, missed, "
        "EXISTS (SELECT 1 FROM job_content c WHERE c.uid = jobs.uid) AS has_content FROM jobs "
        "WHERE source=? AND source_board=?", (source, board))}
    held_before = conn.execute("SELECT held_closures FROM boards WHERE ats=? AND slug=?", (source, board)).fetchone()
    seen, changed, touched, expired = set(), set(), set(), set()
    events = []
    with conn:
        for j in jobs:
            if j.uid in seen or j.uid in expired:
                continue
            if j.expires_at and iso(j.expires_at) <= now:
                expired.add(j.uid)
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
            # A stored row without its description (a builder seeded from the published database, which
            # carries none) takes the text the moment a listing brings it.
            fill = bool(j.content_html) and old is not None and not old["has_content"]
            if old is not None and old["active"] and not is_changed and not fill and (
                    old["row_sig"] == sig or (j.partial and old["url"] == j.url)):
                if old["missed"]:
                    conn.execute("UPDATE jobs SET missed=0 WHERE uid=?", (j.uid,))
                continue  # nothing new: no write (last_seen is refreshed for the board below)
            conn.execute(
                """INSERT INTO jobs (uid, fingerprint, source, source_board, external_id, title, company,
                       company_key, location, url, content_hash, departments, posted_at,
                       updated_at, first_seen, last_seen, active, ghost_reasons, detail_marker, workplace,
                       employment_type, salary_min, salary_max, salary_currency, salary_interval, locations,
                       effective_first_seen, cluster_id, row_sig, expires_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'[]',?,?,?,?,?,?,?,?,?,?,?,?)
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
                       expires_at=CASE WHEN ? THEN jobs.expires_at ELSE excluded.expires_at END,
                       row_sig=excluded.row_sig, missed=0""",
                (j.uid, j.fingerprint, j.source, j.source_board, j.external_id, j.title, j.company,
                 company_key(j.company), j.location, j.url, chash,
                 json.dumps(j.departments), iso(j.posted_at), iso(j.updated_at), now, now,
                 marker, j.workplace, j.employment_type, j.salary_min, j.salary_max,
                 j.salary_currency, j.salary_interval, json.dumps(j.locations or ([j.location] if j.location else [])),
                 now, j.uid, sig, iso(j.expires_at), *([int(j.partial and old is not None)] * 6)))
            if j.content_html and (old is None or old["content_hash"] != chash or not old["active"] or fill):
                conn.execute("INSERT INTO job_content(uid, html) VALUES (?, ?) "
                             "ON CONFLICT(uid) DO UPDATE SET html=excluded.html", (j.uid, pack(j.content_html)))
            if fill:
                touched.add(company_key(j.company))   # its signature and rules are computed next
            if is_changed:
                changed.add(j.uid)
                touched.add(company_key(j.company))
        unseen = [u for u, r in existing.items() if r["active"] and u not in seen and u not in keep_alive
                  and u not in expired]
        was_open = sum(1 for r in existing.values() if r["active"])
        held = 0
        need = max(1, close_after)
        if (hold_mass_closures and held_before is not None and not held_before[0]
                and was_open >= MASS_CLOSE_MIN and len(unseen) > was_open * MASS_CLOSE_SHARE):
            held, need = len(unseen), max(need, 2)   # closed only if the next run agrees
        if held_before is not None:
            conn.execute("UPDATE boards SET held_closures=? WHERE ats=? AND slug=?", (int(bool(held)), source, board))
        gone = [u for u in unseen if (existing[u]["missed"] or 0) + 1 >= need]
        closing = set(gone)
        missed = [u for u in unseen if u not in closing]
        gone += [u for u in expired if u in existing and existing[u]["active"]]
        conn.executemany("UPDATE jobs SET missed=missed + 1 WHERE uid=?", [(u,) for u in missed])
        # last_seen: what the board listed this time (postings it left out keep theirs)
        conn.execute("UPDATE jobs SET last_seen=? WHERE source=? AND source_board=? AND active=1 AND missed=0",
                     (now, source, board))
        conn.executemany("INSERT INTO events(uid, kind, at) VALUES (?,?,?)", events)
        touched |= _close(conn, gone, now)
    new = sum(1 for e in events if e[1] == "new")
    return {"new": new, "updated": len(changed) - new, "unchanged": len(seen) - len(changed),
            "closed": len(gone), "missed": len(missed), "held": held, "changed": changed, "touched": touched}


PURGE_ANYWAY_DAYS = 365   # closed this long: deleted even while another listing of the role is open


def purge_closed(conn, days: int) -> int:
    """Delete postings that closed more than `days` ago, once no other listing in their cluster is
    still open (until then they carry the role's repost history), or after a year regardless.
    Returns rows deleted."""
    if days <= 0:
        return 0
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(days=days)).isoformat(timespec="seconds")
    hard = (now - timedelta(days=max(days, PURGE_ANYWAY_DAYS))).isoformat(timespec="seconds")
    uids = [r[0] for r in conn.execute(
        """SELECT j.uid FROM jobs j WHERE j.active = 0 AND j.closed_at < ?
               AND (j.closed_at < ? OR j.cluster_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM jobs k WHERE k.cluster_id = j.cluster_id AND k.active = 1))""", (cut, hard))]
    return delete_postings(conn, uids)


def delete_postings(conn, uids: list[str]) -> int:
    """Delete postings for good, and what only they referred to. Returns rows deleted."""
    if not uids:
        return 0
    now = datetime.now(timezone.utc)
    with conn:
        for i in range(0, len(uids), 500):
            part = uids[i:i + 500]
            q = ",".join("?" * len(part))
            for table in ("job_content", "events", "jobs"):
                conn.execute(f"DELETE FROM {table} WHERE uid IN ({q})", part)
        # Rule extractions nothing refers to any more; the gap fill's readings are kept a while longer (a
        # role reposted with the same text reuses them at no cost).
        conn.execute("DELETE FROM extractions WHERE method = 'rules' AND NOT EXISTS "
                     "(SELECT 1 FROM jobs j WHERE j.content_hash = extractions.content_hash)")
        old = (now - timedelta(days=180)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM llm_extractions WHERE at < ? AND NOT EXISTS "
                     "(SELECT 1 FROM jobs j WHERE j.content_hash = llm_extractions.content_hash)", (old,))
    return len(uids)


def purge_out_of_scope(conn, countries) -> tuple[int, set[str]]:
    """Builder with a country scope: delete postings known to be elsewhere. New ones are never stored
    (the readers drop them before any detail call), but ones stored before the scope was set stayed for
    good: listed by their board, so never closed, and never refreshed either. Postings whose country
    isn't known are kept, as everywhere else. Returns (rows deleted, their company keys)."""
    want = {c.strip().upper() for c in countries or () if c.strip()}
    if not want:
        return 0, set()
    from . import geo
    uids, keys = [], set()
    for r in conn.execute("SELECT uid, company_key, location, locations FROM jobs"):
        locs = strings(r["locations"]) or ([r["location"]] if r["location"] else [])
        if locs and not geo.in_scope(locs, want):
            uids.append(r["uid"])
            keys.add(r["company_key"])
    return delete_postings(conn, uids), keys


def compact(conn, min_free_share: float = 0.15) -> bool:
    """Give deleted space back (VACUUM) once free pages are a real share of the file: the builder's
    database travels to and from the Actions cache on every build."""
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    if not pages or free / pages < min_free_share:
        return False
    conn.commit()
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return True


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
    visa: str = ""               # "" | not_no | yes
    eligible_only: bool = False
    show_ghosts: bool = False
    view: str = "all"            # all | new
    sort: str = "newest"         # newest | salary | yoe
    since: str | None = None     # for view=new
    ghost_hide_at: int = 3
    countries: tuple = ()        # e.g. ("US", "CA"); empty = everywhere
    profile: dict | None = None  # for eligibility (filter + reasons on the rows shown)
    yoe_slack: int = 2
    function: str = ""           # job_function, e.g. "Software Engineering"
    seniority: str = ""          # intern | entry | mid | senior | lead | manager | director | executive
    emp_type: str = ""           # full_time | part_time | contract | internship | temporary
    skills: tuple = ()           # every one must be mentioned, e.g. ("python", "sql")
    posted_within: int | None = None   # days: first seen (repost-aware) this recently
    pay_listed: bool = False     # only roles that state pay
    degree_max: str = ""         # "" | none (no degree asked) | bachelors | masters (at most this asked)
    clearance: str = ""          # "" | none (no clearance asked) | required
    no_citizenship: bool = False # leave out roles limited to U.S. citizens
    benefits: tuple = ()         # every one must be offered, e.g. ("equity", "unlimited_pto")
    travel: str = ""             # "" | low (leave out frequent travel)
    language: str = ""           # "" | english (no other language required) | a language, e.g. "german"


class Count(int):
    """A result count that may be capped (shown as "5,000+")."""
    capped = False


COUNT_CAP = 5000
_DEGREE_RANK = {"bachelors": 1, "masters": 2, "phd": 3}
_DEGREE_SQL = "CASE j.degree WHEN 'bachelors' THEN 1 WHEN 'masters' THEN 2 WHEN 'phd' THEN 3 ELSE 0 END"
_LIST_COLS ="""j.uid, j.title, j.company, j.company_key, j.location, j.cluster_locations, j.cluster_count,
    j.url, j.source, j.departments, j.posted_at, j.first_seen, j.effective_first_seen, j.content_hash,
    j.repost_count, j.ghost_score, j.ghost_reasons, j.workplace_eff, j.pay_min_annual, j.pay_max_annual,
    j.pay_currency, j.pay_interval, j.yoe_min, j.yoe_source, j.visa, j.citizenship, j.clearance, j.degree,
    j.job_function, j.seniority, j.emp_type, j.skills, j.benefits, j.travel, j.languages"""


def _where(conn, f: Filters) -> tuple[list[str], list]:
    """WHERE clauses over jobs only (no joins)."""
    where = ["j.active = 1", "j.is_canonical = 1"]
    args: list = []
    if f.q:
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
    elif f.visa == "yes":
        where.append("j.visa = 'yes'")
    if f.function:
        where.append("j.job_function = ?"); args.append(f.function)
    if f.seniority:
        where.append("j.seniority = ?"); args.append(f.seniority)
    if f.emp_type:
        where.append("j.emp_type = ?"); args.append(f.emp_type)
    for sk in f.skills:
        where.append("j.skills LIKE ?"); args.append(f"%,{sk.strip().lower()},%")
    if f.posted_within:
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(f.posted_within))
        where.append("j.effective_first_seen >= ?"); args.append(cutoff.isoformat(timespec="seconds"))
    if f.pay_listed:
        where.append("j.pay_max_annual IS NOT NULL")
    if f.degree_max == "none":
        where.append("j.degree IS NULL")
    elif f.degree_max in _DEGREE_RANK:
        where.append(f"(j.degree IS NULL OR {_DEGREE_SQL} <= ?)"); args.append(_DEGREE_RANK[f.degree_max])
    if f.clearance == "none":
        where.append("(j.clearance IS NULL OR j.clearance = '')")
    elif f.clearance == "required":
        where.append("(j.clearance IS NOT NULL AND j.clearance <> '')")
    if f.no_citizenship:
        where.append("(j.citizenship IS NULL OR j.citizenship = 0)")
    for b in f.benefits:
        where.append("j.benefits LIKE ?"); args.append(f"%,{b},%")
    if f.travel == "low":
        where.append("(j.travel IS NULL OR j.travel <> 'frequent')")
    if f.language == "english":
        where.append("(j.languages IS NULL OR j.languages = '')")
    elif f.language:
        where.append("j.languages LIKE ?"); args.append(f"%,{f.language.strip().lower()},%")
    if f.eligible_only and f.profile:
        from .profile import eligibility_sql
        clause = eligibility_sql(conn, f.profile, f.yoe_slack)
        if clause:
            where.append(f"({clause[0]})"); args += clause[1]
    if not f.show_ghosts:
        where.append("j.ghost_score < ?"); args.append(f.ghost_hide_at)
    if f.view == "new" and f.since:
        where.append("j.effective_first_seen > ?"); args.append(f.since)
    return where, args


def count_jobs(conn, f: Filters, cap: int = COUNT_CAP) -> Count:
    where, args = _where(conn, f)
    n = conn.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM jobs j WHERE {' AND '.join(where)} LIMIT ?)",
                     args + [cap + 1]).fetchone()[0]
    out = Count(min(n, cap))
    out.capped = n > cap
    return out


def list_jobs(conn, f: Filters, page: int = 1, per_page: int = 50, with_count: bool = True):
    """One row per near-duplicate cluster (the canonical active posting), filtered and paginated,
    with each row's eligibility for f.profile. Counts are capped at COUNT_CAP."""
    where, args = _where(conn, f)
    order = {
        "salary": "j.pay_max_annual IS NULL, j.pay_max_annual DESC",
        "yoe": "j.yoe_min IS NULL, j.yoe_min ASC, j.effective_first_seen DESC",
    }.get(f.sort, "j.effective_first_seen DESC")
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_LIST_COLS} FROM jobs j WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [per_page, max(0, page - 1) * per_page])]
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
    if not row.get("cluster_locations"):
        # A role's places are stored on its main listing; a duplicate or closed listing shows those,
        # else its own.
        lead = conn.execute("SELECT cluster_locations FROM jobs WHERE cluster_id = ? AND is_canonical = 1 "
                            "AND cluster_locations IS NOT NULL LIMIT 1", (row.get("cluster_id"),)).fetchone()
        own = json.loads(row.get("locations") or "[]") or ([row["location"]] if row.get("location") else [])
        row["cluster_locations"] = lead[0] if lead else json.dumps(own)
    from .profile import annotate
    annotate([row], profile or {}, yoe_slack)
    return row


def content(conn, uid: str) -> str:
    """A posting's description; when it has none stored, its role's: the same text if any listing
    has it, else the canonical listing's."""
    r = conn.execute("SELECT html FROM job_content WHERE uid=?", (uid,)).fetchone()
    if r is None:
        r = conn.execute("""SELECT c.html FROM jobs j JOIN jobs k ON k.cluster_id = j.cluster_id
                            JOIN job_content c ON c.uid = k.uid WHERE j.uid = ?
                            ORDER BY k.content_hash = j.content_hash DESC, k.is_canonical DESC LIMIT 1""",
                         (uid,)).fetchone()
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
