"""The build (what the Job index Action runs) and the public files: jobs.db.xz, jobs.csv.gz and
manifest.json on the `job-index` release. README.md describes them for their users.

The public database is written by whitelist (write_public_db), and a test checks it. Anything added
to it must be job data: never descriptions in full, and nothing about the people using the site.
Other people's queries read its tables (postings first), so a renamed or dropped column breaks
them; say so in the PR and update the README.

The same file is what a new builder starts from when the Actions cache is lost (seed_builder), so a
column the builder needs back has to be in it.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import lzma
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, db, dedupe, extract
from .models import age_hours, utcnow

log = logging.getLogger("lighthouse.index")

MANIFEST = "manifest.json"
DB_NAME = "jobs.db.xz"             # the public database
CSV_NAME = "jobs.csv.gz"           # every open role, one column per filter
BUILDER_DB = db.DEFAULT_DB         # the Action's working database (kept in its cache)
REDISCOVER_OK_DAYS = 14            # re-check a company whose board works this often
REDISCOVER_BAD_DAYS = 3            # ... and one whose board couldn't be found this often
FETCH_DETAIL_MINUTES = 120         # boards started later than this skip description calls this run
# The whole build fits well inside an hour, so it can run every hour. Counted from the start of the
# build: looking up companies stops starting new ones after DISCOVER_MINUTES, no board starts after
# FETCH_MINUTES, every request stops 10 minutes later (fetch.STOP_AFTER_LIMIT_MINUTES), and
# clustering, the rules and the files take the rest. What isn't reached goes first next run.
DISCOVER_MINUTES = 10              # a full re-check (--full) gets three times this: 700 companies take about 30
FETCH_MINUTES = 35
FETCH_WORKERS = 48                 # boards in parallel: most wait on their host's rate limit, not on CPU
WORKDAY_REFRESH_HOURS = 11         # each Workday board twice a day (see Settings.workday_refresh_hours)
PAGED_REFRESH_HOURS = 5            # other many-request listings: hourly builds mustn't mean hourly requests to them


def _now() -> str:
    # Microseconds: build order must be unambiguous (deltas hold rows changed *after* the base).
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _cols(conn, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ================================================================ the build

def build(out_dir: Path | str, countries=(), config_path=None, db_path: Path | str = BUILDER_DB,
          detail_calls: int | None = None, full: bool = False, discover_companies: bool = True,
          llm: bool = True, llm_minutes: float | None = None, fetch_minutes: float | None = None) -> dict:
    """Find boards for the listed companies, fetch every enabled board into the builder database, let a
    free open model on this machine fill the fields the rules couldn't settle (when one is set up: see
    gap_fill.server_from_env) and guess the categories of titles they couldn't place, then write the
    public files. full: look every company up again and retry boards that were switched off."""
    from . import config as config_mod
    from . import fetch, net
    config_path = Path(config_path or config_mod.DEFAULT_CONFIG)
    started, t0 = utcnow(), time.monotonic()   # whole seconds, as events are stamped
    countries = [c.strip().upper() for c in countries if c.strip()]
    conn = db.connect(db_path)
    cfg = config_mod.load(config_path)
    s = cfg.settings
    client = net.default_client(s.request_timeout_seconds, s.per_host_requests_per_second)
    boards = list(cfg.boards)
    if discover_companies:
        boards += discover_boards(conn, config_path.parent, client, full=full, listed=cfg.boards,
                                  minutes=DISCOVER_MINUTES * (3 if full else 1))
    boards = merge_same_boards(conn, boards)
    retired = apply_board_list(conn, boards, retry_switched_off=full)
    conn.close()
    # Nothing new is started after fetch_minutes, so even a first build of the whole list (hundreds of
    # thousands of postings) finishes and publishes; later runs pick up where it stopped.
    fetch_minutes = FETCH_MINUTES if fetch_minutes is None else fetch_minutes
    if fetch_minutes:   # what discovery took comes out of it (at least 5 minutes of fetching)
        fetch_minutes = max(5.0, fetch_minutes - (time.monotonic() - t0) / 60)
    overrides = {"detail_minutes": FETCH_DETAIL_MINUTES, "countries": tuple(countries),
                 "fetch_minutes": fetch_minutes,
                 "fetch_workers": max(s.fetch_workers, FETCH_WORKERS),
                 "workday_refresh_hours": s.workday_refresh_hours or WORKDAY_REFRESH_HOURS,
                 "paged_refresh_hours": s.paged_refresh_hours or PAGED_REFRESH_HOURS,
                 **({"max_detail_calls_per_board": detail_calls} if detail_calls else {})}
    results = fetch.run(config_path, db_path, overrides=overrides)
    log.info("fetched %d boards (%d failed)", len(results), sum(1 for r in results if "error" in r))
    conn = db.connect(db_path)
    gone, keys = db.purge_out_of_scope(conn, countries)
    if gone:
        log.info("deleted %d postings outside %s (stored before the builder covered only those)", gone,
                 ", ".join(countries))
    if retired or keys:   # postings closed (boards switched off in the list) or deleted: re-cluster those companies
        fetch.postprocess(conn, cfg, set(retired) | keys)
    if llm:   # optional steps: the rule answers are published whatever happens here
        from . import gap_fill
        gap_fill.fill(conn, cfg, llm_minutes)
    _company_openings(conn)
    db.set_meta(conn, "build_health", health(conn, results, started, (time.monotonic() - t0) / 60,
                                             s.ghost_hide_signals))
    if db.compact(conn):
        log.info("compacted the database")
    conn.close()
    return export(db_path, out_dir, countries=countries)


_SIDE_BOARD = re.compile(r"campus|intern|early|student|graduate|redeploy|restricted|contractor|alumni|referral", re.I)


def listed_board(company: str, by_company: dict) -> dict | None:
    """The board a board list already has for this company (same name once "Inc", "Holdings"... are
    dropped): one that's on if there is one, and its main site rather than a campus or internal one."""
    from .models import company_key
    found = by_company.get(company_key(company)) or []
    found = sorted(found, key=lambda b: (not b.get("enabled", True), bool(_SIDE_BOARD.search(b["slug"]))))
    return found[0] if found else None


def discover_boards(conn, config_dir: Path, client, full: bool = False, workers_n: int = 24,
                    listed: list[dict] | None = None, minutes: float | None = None) -> list[dict]:
    """Resolve config/companies.csv to boards, re-checking only what's due. Results are kept in the
    builder's `companies` table (and shipped as the coverage report). Returns board-list entries.

    A company that a board list (config/boards/*.csv: boards checked by hand) already has needs no
    looking up: its careers page may hide the system behind scripts or robots.txt (Stripe, SpaceX,
    Cloudflare were reported "not found" while their boards were being read all along)."""
    from . import discover, net
    from .models import company_key
    from .sources import SOURCES
    companies = discover.load_companies(config_dir)
    by_company: dict[str, list] = {}
    for b in listed or []:
        if b.get("company") and b["ats"] in SOURCES:
            by_company.setdefault(company_key(b["company"]), []).append(b)
    have = {r["company"]: dict(r) for r in conn.execute("SELECT * FROM companies")}
    failing = {(r[0], r[1]) for r in conn.execute("SELECT ats, slug FROM boards WHERE last_error IS NOT NULL")}
    empty = {(r[0], r[1]) for r in conn.execute("SELECT ats, slug FROM boards WHERE enabled = 1 AND job_count = 0 "
                                                "AND last_ok IS NOT NULL")}   # read fine, but lists nothing for us
    todo, keep = [], []
    for c in companies:
        old = have.get(c["company"])
        known = None if c["ats"] and c["slug"] else listed_board(c["company"], by_company)
        if (c["ats"] and c["slug"]) or known:
            keep.append(discover.Result(company=c["company"], group=c["group"], domain=c["domain"],
                                        careers_url=c["careers_url"], ats=c["ats"] or known["ats"],
                                        slug=c["slug"] or known["slug"], status="pinned", checked_at=_now()))
            continue
        age = age_hours(old["checked_at"]) / 24 if old else float("inf")
        due = (full or old is None or old.get("careers_url") != c["careers_url"]
               or (old["status"] == "ok" and (age > REDISCOVER_OK_DAYS or (old["ats"], old["slug"]) in failing
                                              or ((old["ats"], old["slug"]) in empty and age > REDISCOVER_BAD_DAYS)))
               or (old["status"] != "ok" and age > REDISCOVER_BAD_DAYS))
        if due:
            todo.append(c)
    stop = time.monotonic() + minutes * 60 if minutes else None
    found = discover.run(todo, discover.discovery_client(), net.BUILDER_AGENT, workers=workers_n,
                         stop_at=stop) if todo else []
    log.info("discovery: %d companies checked, %s%s", len(found), discover.summary(found)["status"] if found else {},
             f"; {len(todo) - len(found)} left for the next run (time limit)" if len(found) < len(todo) else "")
    with conn:
        for r in keep + found:
            conn.execute(
                """INSERT INTO companies(company, grp, domain, careers_url, final_url, ats, slug, status, detail,
                       platforms, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(company) DO UPDATE SET grp=excluded.grp, domain=excluded.domain,
                       careers_url=excluded.careers_url, final_url=excluded.final_url, ats=excluded.ats,
                       slug=excluded.slug, status=excluded.status, detail=excluded.detail,
                       platforms=excluded.platforms, checked_at=excluded.checked_at""",
                (r.company, r.group, r.domain, r.careers_url, r.final_url, r.ats or None, r.slug or None, r.status,
                 r.detail or None, json.dumps(r.platforms), r.checked_at))
        names = {c["company"] for c in companies}
        for (name,) in conn.execute("SELECT company FROM companies").fetchall():
            if name not in names:
                conn.execute("DELETE FROM companies WHERE company = ?", (name,))
    return [{"ats": r["ats"], "slug": r["slug"], "company": r["company"], "options": None, "list": "companies",
             "enabled": True}
            for r in conn.execute("SELECT company, ats, slug FROM companies WHERE status IN ('ok', 'pinned')")
            if r["ats"] in SOURCES]


CASELESS_SLUGS = {"workday"}   # the site name in a Workday address is the same site in any letter case


def merge_same_boards(conn, boards: list[dict]) -> list[dict]:
    """One board listed twice under spellings that differ only in letter case (a board list says
    "bah.wd1/bah_jobs", the company's careers page "bah.wd1/BAH_Jobs") is one board: every posting was
    being read, stored and de-duplicated twice. The first spelling listed wins; later entries (and the
    companies table, so coverage still finds its board) take it."""
    first: dict[tuple[str, str], str] = {}
    out = []
    for b in boards:
        if b["ats"] in CASELESS_SLUGS:
            slug = first.setdefault((b["ats"], b["slug"].lower()), b["slug"])
            if slug != b["slug"]:
                with conn:
                    conn.execute("UPDATE companies SET slug = ? WHERE ats = ? AND slug = ?", (slug, b["ats"], b["slug"]))
                b = {**b, "slug": slug}
        out.append(b)
    return out


def _company_openings(conn) -> None:
    """Coverage report: openings per company = postings its board listed in this run."""
    with conn:
        conn.execute("""UPDATE companies SET openings = (SELECT b.job_count FROM boards b
                        WHERE b.ats = companies.ats AND b.slug = companies.slug AND b.enabled = 1),
                        status = CASE WHEN status IN ('ok', 'pinned') AND EXISTS (SELECT 1 FROM boards b
                            WHERE b.ats = companies.ats AND b.slug = companies.slug AND b.last_error IS NOT NULL
                            AND b.last_ok IS NULL) THEN 'error' ELSE status END,
                        detail = CASE WHEN status IN ('ok', 'pinned') THEN (SELECT b.last_error FROM boards b
                            WHERE b.ats = companies.ats AND b.slug = companies.slug) ELSE detail END""")


def apply_board_list(conn, cfg_or_boards, retry_switched_off: bool = False) -> set[str]:
    """On the builder, the committed lists are the truth (there's no Sources page): each run applies
    their on/off column, switches off boards no longer listed, and closes the postings of boards that
    are off so readers see them disappear. A board listed twice (a board list and the companies list)
    is on if either says so. Boards switched off automatically (gone) stay off until a full build.
    Returns the company keys whose postings were closed."""
    boards = cfg_or_boards.boards if hasattr(cfg_or_boards, "boards") else list(cfg_or_boards)
    db.sync_config_boards(conn, boards)
    want: dict = {}
    for b in boards:
        k = (b["ats"], b["slug"])
        want[k] = want.get(k, False) or bool(b.get("enabled", True))
    now = _now()
    with conn:
        if retry_switched_off:
            conn.execute("UPDATE boards SET fail_count = 0, last_error = NULL "
                         "WHERE last_error LIKE 'Switched off automatically%'")
        for (ats, slug), on in want.items():
            conn.execute("UPDATE boards SET enabled = ? WHERE ats = ? AND slug = ? AND NOT "
                         "(enabled = 0 AND COALESCE(last_error, '') LIKE 'Switched off automatically%')",
                         (int(on), ats, slug))
        for ats, slug in conn.execute("SELECT ats, slug FROM boards").fetchall():
            if (ats, slug) not in want:
                conn.execute("UPDATE boards SET enabled = 0 WHERE ats = ? AND slug = ?", (ats, slug))
        off = """active = 1 AND EXISTS (SELECT 1 FROM boards b WHERE b.ats = jobs.source
                 AND b.slug = jobs.source_board AND b.enabled = 0)"""
        keys = {r[0] for r in conn.execute(f"SELECT DISTINCT company_key FROM jobs WHERE {off}")}
        conn.execute(f"UPDATE jobs SET active = 0, closed_at = COALESCE(closed_at, ?) WHERE {off}", (now,))
    return keys



def seed_builder(db_file: Path | str, db_path: Path | str = BUILDER_DB) -> int:
    """Start a builder database from the published jobs.db.xz (when the Action's cache is lost), so it
    keeps every posting's history. The public file carries no descriptions: each posting takes its text
    back the first time its board lists it. Returns postings seeded."""
    from . import site
    db_file = Path(db_file)
    tmp = None
    if db_file.suffix == ".xz":
        fd, tmp = tempfile.mkstemp(suffix=".db", dir=Path(db_path).parent)
        with os.fdopen(fd, "wb") as fout, lzma.open(db_file, "rb") as fin:
            shutil.copyfileobj(fin, fout, 1 << 20)
        db_file = Path(tmp)
    try:
        conn = db.connect(db_path)
        conn.executescript(site._STATE_TABLES)
        conn.execute("ATTACH DATABASE ? AS s", (str(db_file),))
        with conn:
            for table in ("jobs", "boards", "companies"):
                cols = ", ".join(c for c in _cols(conn, table, "s") if c in set(_cols(conn, table)))
                conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM s.{table}")
            # model 'seeded': the next build re-runs the rules (which also records what they left unsettled)
            conn.execute("INSERT OR REPLACE INTO extractions (content_hash, method, data, model, at) "
                         "SELECT content_hash, method, data, 'seeded', at FROM s.extractions")
            # The gap fill's answers are in the published extractions ("by": "model"): keep them as already
            # read, so a lost cache never reads the same descriptions twice. Category guesses
            # ("by": "titles") aren't kept: they're redone each build in seconds.
            now = _now()
            for chash, data in conn.execute("SELECT content_hash, data FROM s.extractions WHERE data LIKE "
                                            "'%\"by\":%' OR data LIKE '%_read\"%'").fetchall():
                try:
                    d = json.loads(data)
                    filled = {k: v for k, v in d.items() if isinstance(v, dict) and v.get("by") in ("model", "claude")}
                except (ValueError, AttributeError):
                    continue
                if filled or any(m in d for m in extract.READ_MARKERS):   # read before, even if it found nothing
                    conn.execute("INSERT OR IGNORE INTO llm_extractions (content_hash, data, model, at) "
                                 "VALUES (?, ?, 'seeded', ?)", (chash, json.dumps(filled, ensure_ascii=False), now))
            conn.execute("INSERT INTO events (uid, kind, at) SELECT uid, kind, at FROM s.events ORDER BY rowid")
            # the website shows the published excerpts until the descriptions are read again
            if conn.execute("SELECT 1 FROM s.sqlite_master WHERE name = 'excerpts'").fetchone():   # files before 2026-11-19
                conn.execute("INSERT OR REPLACE INTO site_excerpts (content_hash, text) SELECT content_hash, text FROM s.excerpts")
            else:
                conn.execute("INSERT OR REPLACE INTO site_excerpts (content_hash, text) SELECT j.content_hash, p.excerpt "
                             "FROM s.postings p JOIN s.jobs j ON j.uid = p.id WHERE p.excerpt IS NOT NULL")
            # One-response boards are read in full once (not skipped as unchanged), so their postings
            # get their descriptions back.
            conn.execute("UPDATE boards SET body_hash = NULL")
            conn.execute("UPDATE jobs SET minhash_of = ?", (dedupe.SEEDED,))   # roles stay as published meanwhile
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.execute("DETACH DATABASE s")
        conn.close()
        return n
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


# ================================================================ the public files

# Builder bookkeeping the public database leaves out (a builder seeded from it works them out again).
PRIVATE_JOB_COLUMNS = db.RETIRED_JOB_COLUMNS | {"minhash", "minhash_of", "missed", "row_sig", "derived_of", "detail_marker"}

# One row per open role (duplicate listings collapsed into the role), one column per filter, in plain
# words: what most people want, and what jobs.csv.gz holds. A table, not a view, so it's quick to query.
# The comments travel with it inside the file: any SQLite tool shows what each column means.
POSTINGS_TABLE = """CREATE TABLE postings (
    id TEXT PRIMARY KEY,            -- "<ats>:<board>:<the employer's posting id>" (jobs.uid)
    title TEXT,
    company TEXT,
    location TEXT,                  -- as the employer wrote it
    all_locations TEXT,             -- every place this role is listed, separated by " | "
    countries TEXT,                 -- ISO codes, ", "-separated; NULL when no country could be read
    workplace TEXT,                 -- remote | hybrid | onsite
    employment_type TEXT,           -- full_time | part_time | contract | temporary | internship
    seniority TEXT,                 -- intern | entry | mid | senior | lead | manager | director | executive
    job_function TEXT,              -- the role's category, e.g. Data & Analytics
    years_experience INTEGER,       -- years of experience asked
    years_experience_source TEXT,   -- stated (required) | preferred | title (estimated from the title: a guess)
    degree TEXT,                    -- bachelors | masters | phd: the lowest the posting requires
    visa_sponsorship TEXT,          -- yes | no: what the posting says about sponsoring a visa
    clearance TEXT,                 -- public_trust | secret | ts | ts_sci | unspecified (required, level not given)
    us_citizens_only INTEGER,       -- 1 when the posting requires US citizenship
    pay_min_per_year INTEGER,       -- in pay_currency, not converted; hourly pay x 2080
    pay_max_per_year INTEGER,
    pay_currency TEXT,              -- ISO code: USD, CAD, MXN...
    pay_stated_per TEXT,            -- how the posting gave pay: year | hour | month...
    skills TEXT,                    -- ", "-separated
    benefits TEXT,                  -- ", "-separated
    travel TEXT,                    -- none | occasional | frequent
    languages_required TEXT,        -- ", "-separated
    departments TEXT,               -- as the employer gave them, ", "-separated
    posted_on TEXT,                 -- YYYY-MM-DD from the careers system; a repost resets it
    first_seen_on TEXT,             -- YYYY-MM-DD, the earliest the role was seen anywhere: use this for "new"
    times_listed INTEGER,           -- listings collapsed into this row (one role posted in six cities: 6)
    reposts INTEGER,                -- times it was taken down and posted again
    ghost_score INTEGER,            -- 0-4 signs of a ghost job; the website hides 3 and up
    ghost_signals TEXT,             -- those signs, "; "-separated
    has_description INTEGER,        -- 1 when the description was read (most columns come from it)
    filled_by_ai TEXT,              -- columns no rule could read, filled by a small open model from the posting's own words
    url TEXT,                       -- the employer's page: the full posting, and where to apply
    ats TEXT,                       -- the careers system: workday, greenhouse, jsonld (a careers site)...
    board TEXT,                     -- that system's board (boards.slug)
    excerpt TEXT                    -- about 600 characters of the description
)"""

# Fills postings from the public jobs and extractions (and the builder's excerpts, attached as src).
# clean() trims and collapses whitespace and turns "" into NULL; a role with no title is left out. Rows go
# by company and title: newest first made jobs.csv.gz 37% bigger (a company's rows side by side compress).
POSTINGS_FILL = """INSERT INTO postings SELECT
    j.uid, clean(j.title), clean(j.company), clean(j.location),
    (SELECT group_concat(clean(value), ' | ') FROM json_each(COALESCE(j.cluster_locations, json_array(j.location)))),
    NULLIF(replace(trim(j.countries, ','), ',', ', '), ''),
    j.workplace_eff, j.emp_type, j.seniority, j.job_function,
    j.yoe_min, j.yoe_source, j.degree, j.visa, j.clearance, j.citizenship,
    CAST(j.pay_min_annual AS INTEGER), CAST(j.pay_max_annual AS INTEGER), j.pay_currency, j.pay_interval,
    NULLIF(replace(trim(j.skills, ','), ',', ', '), ''),
    NULLIF(replace(trim(j.benefits, ','), ',', ', '), ''),
    j.travel,
    NULLIF(replace(trim(j.languages, ','), ',', ', '), ''),
    (SELECT group_concat(clean(value), ', ') FROM json_each(COALESCE(j.departments, '[]'))),
    CASE WHEN substr(j.posted_at, 1, 10) <= date('now') THEN substr(j.posted_at, 1, 10) END,
    substr(COALESCE(j.effective_first_seen, j.first_seen), 1, 10),
    j.cluster_count, j.repost_count, j.ghost_score,
    (SELECT group_concat(value, '; ') FROM json_each(COALESCE(j.ghost_reasons, '[]'))),
    x.text IS NOT NULL,
    (SELECT group_concat(f.key, ', ') FROM extractions e, json_each(e.data) f
      WHERE e.content_hash = j.content_hash AND json_extract(f.value, '$.by') IS NOT NULL),
    j.url, j.source, j.source_board, x.text
FROM jobs j LEFT JOIN src.site_excerpts x ON x.content_hash = j.content_hash AND x.text <> ''
WHERE j.active = 1 AND j.is_canonical = 1 AND clean(j.title) IS NOT NULL
ORDER BY 3 COLLATE NOCASE, 2 COLLATE NOCASE"""


def _clean(v):
    """Whitespace trimmed and collapsed (newlines and tabs too); "" becomes NULL."""
    return (" ".join(v.split()) or None) if isinstance(v, str) else v


def _compact_extraction(data: str | None) -> str | None:
    """Rule extractions without their empty fields: most postings mention only a few, so ~4x smaller."""
    try:
        d = json.loads(data)
    except (TypeError, ValueError):
        return data
    if not isinstance(d, dict):
        return data
    keep = {k: v for k, v in d.items() if not (isinstance(v, dict) and v.get("value") in (None, [], "")
                                               and not v.get("evidence"))}
    return json.dumps(keep, separators=(",", ":"), ensure_ascii=False)


def _xz(plain: Path, out_path: Path) -> None:
    """Compress with the xz tool on every core when it's installed (GitHub's runners have it), else
    with Python's lzma. Same .xz format either way."""
    import subprocess
    exe = shutil.which("xz")
    if exe:
        try:
            with open(out_path, "wb") as fout:
                subprocess.run([exe, "-6", "-T0", "-c", str(plain)], stdout=fout, check=True)
            return
        except (OSError, subprocess.CalledProcessError) as exc:
            log.warning("xz tool failed (%s); compressing in Python", exc)
    with open(plain, "rb") as fin, lzma.open(out_path, "wb", preset=6) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)


def write_public_db(src_path: Path, out: Path, about: dict) -> tuple[dict, dict | None]:
    """Write jobs.db.xz and jobs.csv.gz into `out` from the builder database. Returns what was written
    for each ({file, bytes, ...}; None for the CSV if it couldn't be written)."""
    plain = out / DB_NAME.removesuffix(".xz")
    for f in (plain, out / DB_NAME, out / CSV_NAME):
        f.unlink(missing_ok=True)
    c = sqlite3.connect(plain)
    c.execute("PRAGMA journal_mode=OFF")
    c.execute("PRAGMA synchronous=OFF")
    c.create_function("compact", 1, _compact_extraction, deterministic=True)
    c.create_function("clean", 1, _clean, deterministic=True)
    c.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    cols = ", ".join(n for n in _cols(c, "jobs", "src") if n not in PRIVATE_JOB_COLUMNS)
    c.executescript(f"""
        CREATE TABLE jobs AS SELECT {cols} FROM src.jobs ORDER BY uid;
        CREATE UNIQUE INDEX jobs_uid ON jobs(uid);
        CREATE INDEX jobs_cluster ON jobs(cluster_id);
        CREATE INDEX jobs_hash ON jobs(content_hash);
        CREATE TABLE events AS SELECT uid, kind, at FROM src.events ORDER BY id;
        CREATE INDEX events_uid ON events(uid);
        CREATE TABLE extractions AS SELECT content_hash, method, compact(data) AS data, model, at
            FROM src.extractions WHERE content_hash IN (SELECT content_hash FROM jobs);
        CREATE UNIQUE INDEX extractions_hash ON extractions(content_hash);
        CREATE TABLE boards AS SELECT * FROM src.boards;
        CREATE TABLE companies AS SELECT * FROM src.companies;
        CREATE TABLE about (key TEXT PRIMARY KEY, value TEXT);
        {POSTINGS_TABLE};
        {POSTINGS_FILL};
    """)
    c.executemany("INSERT INTO about (key, value) VALUES (?, ?)", [(k, json.dumps(v)) for k, v in about.items()])
    c.commit()
    c.execute("DETACH DATABASE src")
    rows, active = c.execute("SELECT COUNT(*), COALESCE(SUM(active), 0) FROM jobs").fetchone()
    csv_info = None
    try:        # a convenience for people: never stops the database from publishing
        cur = c.execute("SELECT * FROM postings ORDER BY rowid")   # by company and title, as written
        keep = [i for i, d in enumerate(cur.description) if d[0] != "excerpt"]
        n = 0
        with gzip.open(out / CSV_NAME, "wt", encoding="utf-8", newline="", compresslevel=6) as f:
            w = csv.writer(f)
            w.writerow([cur.description[i][0] for i in keep])
            for row in cur:
                w.writerow([row[i] for i in keep])
                n += 1
        csv_info = {"file": CSV_NAME, "rows": n, "bytes": (out / CSV_NAME).stat().st_size}
    except Exception as exc:
        log.warning("%s not written: %s", CSV_NAME, exc)
    c.close()
    _xz(plain, out / DB_NAME)
    plain.unlink()
    return ({"file": DB_NAME, "sha256": _sha256(out / DB_NAME), "bytes": (out / DB_NAME).stat().st_size,
             "rows": rows, "active": active}, csv_info)


def export(db_path: Path | str, out_dir: Path | str, countries=()) -> dict:
    """Write the public files into out_dir: jobs.db.xz, jobs.csv.gz and manifest.json (returned)."""
    from . import gap_fill, site
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = _now()
    conn = db.connect(db_path)
    site.excerpts(conn)          # an excerpt for every open role, kept in the builder database
    # A role listed in 40 places is one role, 40 postings. "jobs" counts roles: the number every list adds up to.
    roles, postings = conn.execute("SELECT COALESCE(SUM(is_canonical), 0), COUNT(*) FROM jobs WHERE active = 1").fetchone()
    manifest = {"lighthouse": __version__, "rules_version": extract.RULES_VERSION, "built_at": now,
                "countries": list(countries), "jobs": roles, "postings": postings,
                "boards": conn.execute("SELECT COUNT(*) FROM boards WHERE enabled = 1 AND last_ok IS NOT NULL").fetchone()[0],
                # boards that stopped existing, for the maintainer to fix or drop
                "switched_off": [f"{a}:{b}" for a, b in conn.execute(
                    "SELECT ats, slug FROM boards WHERE last_error LIKE 'Switched off automatically%' ORDER BY ats, slug")],
                "coverage": _coverage(conn), "llm": gap_fill.summary(conn), "fill": _fill_rates(conn),
                "health": db.get_meta(conn, "build_health")}
    conn.close()
    manifest["db"], manifest["csv"] = write_public_db(Path(db_path), out, {
        "built_at": now, "lighthouse": __version__, "countries": list(countries),
        "about": "Open job postings read from employers' own careers sites by Lighthouse. Start with the "
                 "postings table: one row per open role (ORDER BY first_seen_on DESC for the newest); its columns are described in its definition. "
                 "jobs keeps every listing, closed ones too, for history. Descriptions are excerpts; url is the "
                 "employer's page."})
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


FILL_COLUMNS = ("yoe_min", "degree", "visa", "workplace_eff", "pay_max_annual", "seniority", "job_function",
                "emp_type", "clearance", "citizenship", "travel", "benefits", "languages", "skills")


def _fill_rates(conn) -> dict:
    row = conn.execute("SELECT COUNT(*), SUM(EXISTS (SELECT 1 FROM job_content c WHERE c.uid = j.uid)), "
                       + ", ".join(f"SUM({c} IS NOT NULL AND {c} <> '')" for c in FILL_COLUMNS)
                       + " FROM jobs j WHERE j.active = 1 AND j.is_canonical = 1").fetchone()
    total = row[0] or 0
    return {"roles": total, "with_description_pct": _pct(row[1], total),
            **{c: _pct(v, total) for c, v in zip(FILL_COLUMNS, row[2:])}}


def _pct(part, whole) -> float:
    return round(100 * (part or 0) / whole, 1) if whole else 0.0


def health(conn, results, since: str, minutes: float, ghost_at: int = 3) -> dict:
    """How the build that just ran went, for the manifest, the Actions summary and the website's Sources
    view: what the boards answered, what opened and closed, and how much of the index is analysed."""
    results = results or []
    events = dict(conn.execute("SELECT kind, COUNT(*) FROM events WHERE at >= ? GROUP BY kind", (since,)))
    systems, total, described = {}, 0, 0
    for ats, n, with_text in conn.execute(
            "SELECT source, COUNT(*), SUM(EXISTS (SELECT 1 FROM job_content c WHERE c.uid = j.uid)) "
            "FROM jobs j WHERE active = 1 GROUP BY source ORDER BY COUNT(*) DESC"):
        systems[ats] = {"postings": n, "with_description_pct": _pct(with_text, n)}
        total, described = total + n, described + (with_text or 0)
    roles, ghosts = conn.execute("SELECT COUNT(*), SUM(ghost_score >= ?) FROM jobs WHERE active = 1 "
                                 "AND is_canonical = 1", (ghost_at,)).fetchone()
    cov = _coverage(conn)
    failing = [{"board": f"{a}:{b}", "company": c or "", "since": ok, "error": (e or "")[:200]} for a, b, c, ok, e in conn.execute(
        "SELECT ats, slug, company, COALESCE(last_ok, added_at), last_error FROM boards WHERE last_error IS NOT NULL "
        "AND (enabled = 1 OR last_error LIKE 'Switched off automatically%') ORDER BY ats, slug")]
    return {
        "build_minutes": round(minutes, 1),
        "boards": {"read": sum(1 for r in results if "new" in r), "unchanged": sum(1 for r in results if r.get("unchanged_board")),
                   "failed": sum(1 for r in results if "error" in r), "waiting": sum(1 for r in results if r.get("deferred")),
                   "switched_off": sum(1 for f in failing if f["error"].startswith("Switched off automatically"))},
        "postings": {"open": total, "new": events.get("new", 0), "closed": events.get("closed", 0),
                     "reopened": events.get("reopened", 0), "held": sum(r.get("held") or 0 for r in results)},
        "coverage_pct": _pct(cov.get("ok", 0) + cov.get("pinned", 0), sum(cov.values())),
        "with_description_pct": _pct(described, total),
        "ghost_pct": _pct(ghosts, roles),
        "systems": systems,
        "failing": failing,
    }


def summary_markdown(m: dict) -> str:
    """The build's summary for the Actions run page (and the log)."""
    h = m.get("health") or {}
    b, p = h.get("boards") or {}, h.get("postings") or {}
    size = lambda f: f"{f['file']} ({f['rows']:,} rows, {f['bytes'] / 1e6:.1f} MB)" if f else "not written"
    out = ["## Job index build", "",
           f"**{m['jobs']:,} open roles** ({m.get('postings') or 0:,} postings) from {m['boards']:,} boards.", "",
           "| | |", "|---|---|",
           f"| Build | {h.get('build_minutes', '?')} min |",
           f"| Postings | {p.get('new', 0):,} new, {p.get('closed', 0):,} closed, {p.get('reopened', 0):,} reopened, "
           f"{p.get('held', 0):,} closures held |",
           f"| Boards | {b.get('read', 0)} read, {b.get('unchanged', 0)} unchanged, {b.get('failed', 0)} failed, "
           f"{b.get('waiting', 0)} waiting for the next run, {b.get('switched_off', 0)} switched off |",
           f"| Companies list covered | {h.get('coverage_pct', '?')}% ({m.get('coverage')}) |",
           f"| Descriptions read | {h.get('with_description_pct', '?')}% of open postings |",
           f"| Likely ghost jobs | {h.get('ghost_pct', '?')}% of open roles |",
           f"| Published | {size(m.get('db'))}, {size(m.get('csv'))} |",
           f"| Open-model gap fill so far | {m.get('llm')} |", "",
           "<details><summary>Filters filled, % of open roles</summary>", "", f"`{m.get('fill')}`", "", "</details>", ""]
    if h.get("systems"):
        out += ["| Careers system | Open postings | Descriptions read |", "|---|---:|---:|"]
        out += [f"| {a} | {s['postings']:,} | {s['with_description_pct']}% |" for a, s in h["systems"].items()]
        out.append("")
    return "\n".join(out) + boards_markdown(m)


BROKEN_AFTER_DAYS = 2    # a board failing this long is listed for the maintainer (one bad run is noise)


def boards_markdown(m: dict) -> str:
    """Boards that haven't answered for two days or more, or were switched off: the text of the one
    GitHub issue the workflow keeps up to date ("" when there are none)."""
    rows = [f for f in (m.get("health") or {}).get("failing") or []
            if f["error"].startswith("Switched off automatically")
            or age_hours(f["since"]) - age_hours(m.get("built_at")) >= BROKEN_AFTER_DAYS * 24]
    if not rows:
        return ""
    cell = lambda s: str(s).replace("|", "/").replace("\n", " ")
    return "\n".join([f"### {len(rows)} job boards need a look", "",
                      f"They haven't answered for {BROKEN_AFTER_DAYS} days or more, or were switched off. Fix the "
                      "board's id in `config/` (see MAINTAINING.md), or remove it.", "",
                      "| Board | Company | Last worked | What it answers now |", "|---|---|---|---|",
                      *[f"| `{cell(f['board'])}` | {cell(f['company'])} | {cell((f['since'] or 'never')[:10])} | "
                        f"{cell(f['error'])} |" for f in rows], ""])


def _coverage(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) FROM companies GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}
