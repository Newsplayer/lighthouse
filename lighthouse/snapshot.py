"""The shared job index: built by one GitHub Action, read by every user's Lighthouse.

One public repo runs a scheduled Action that finds each listed company's job board (discover.py),
fetches every board, de-duplicates and analyses the postings, and publishes the result on its
`job-index` release:

    manifest.json   what's current, with each file's SHA-256
    base.db.xz      a ready-to-use job index (every posting + its search index); rebuilt weekly
    delta.db.xz     only what changed since base.db.xz; rebuilt every run

Readers ([index] repo set) use base.db.xz as it comes: it is unpacked into data/index/ and opened
directly, with nothing re-parsed, re-cleaned or re-indexed. Each delta is applied with a few SQL
statements. Everything personal (profile, marks, Claude verdicts, sponsorship imports) lives in
data/user.db, which attaches the index; a new index never touches it.

    builder:  python -m lighthouse index build --out dist [--published prev/manifest.json]
    reader:   automatic on every refresh; or python -m lighthouse index import | status

The published files are built by whitelist: only job, board, rule-extraction, history and coverage
rows are copied, so nothing personal can be in them even if the build machine had a profile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import lzma
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from . import __version__, db, workers
from . import profile as profile_mod

log = logging.getLogger("lighthouse.index")

FORMAT = 3                          # 3: ready-to-use index (user data apart, search index included)
TAG = "job-index"
BASE, DELTA, MANIFEST = "base.db.xz", "delta.db.xz", "manifest.json"
BUILDER_DB = db.DEFAULT_DB.parent / "builder.db"   # the Action's working database (kept in its cache)
BASE_EVERY_DAYS = 7
MAX_BYTES = 1_900_000_000          # GitHub's per-file release limit is 2 GiB
REDISCOVER_OK_DAYS = 14            # re-check a company whose board works this often
REDISCOVER_BAD_DAYS = 3            # ... and one whose board couldn't be found this often

# Changes on every run for every open posting, or not shown to readers: not a reason to re-send a row.
VOLATILE_JOB_COLUMNS = {"last_seen", "updated_at", "row_sig", "detail_marker"}


class IndexProblem(Exception):
    pass


def _now() -> str:
    # Microseconds: build order must be unambiguous (deltas hold rows changed *after* the base).
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _age_hours(iso: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return float("inf")


def _cols(conn, table: str, schema: str = "main") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ================================================================ builder (the GitHub Action)

def build(out_dir: Path | str, countries=(), config_path=None, db_path: Path | str = BUILDER_DB,
          detail_calls: int | None = None, published: Path | str | None = None, full: bool = False,
          discover_companies: bool = True) -> dict:
    """Find boards for the listed companies, fetch every enabled board into the builder database,
    then write the index files."""
    from . import config as config_mod
    from . import fetch, net
    config_path = Path(config_path or config_mod.DEFAULT_CONFIG)
    conn = db.connect(db_path)
    if db.has_fts(conn):   # the published index gets its own search index; the builder needs none
        conn.execute("DROP TABLE jobs_fts")
        conn.commit()
    p = profile_mod.load(conn)
    p["countries"] = [c.strip().upper() for c in countries if c.strip()]
    profile_mod.save(conn, p)   # countries only: no eligibility is computed on the builder
    cfg = config_mod.load(config_path)
    s = cfg.settings
    client = net.open_network(net.default_client(s.request_timeout_seconds, s.per_host_requests_per_second))
    boards = list(cfg.boards)
    if discover_companies:
        boards += discover_boards(conn, config_path.parent, client, full=full)
    retired = apply_board_list(conn, boards, retry_switched_off=full)
    conn.close()
    overrides = {"max_detail_calls_per_board": detail_calls} if detail_calls else None
    results = fetch.run(config_path, db_path, use_snapshot=False, overrides=overrides)
    failed = sum(1 for r in results or [] if "error" in r)
    log.info("fetched %d boards (%d failed)", len(results or []), failed)
    conn = db.connect(db_path)
    if retired:   # boards switched off in the list: their postings were closed, re-cluster those companies
        fetch.postprocess(conn, cfg, retired)
    _company_openings(conn)
    conn.close()
    return export(db_path, out_dir, countries=p["countries"], published=published, full=full)


def discover_boards(conn, config_dir: Path, client, full: bool = False, workers_n: int = 24) -> list[dict]:
    """Resolve config/companies.csv to boards, re-checking only what's due. Results are kept in the
    builder's `companies` table (and shipped as the coverage report). Returns board-list entries."""
    from . import discover, net
    companies = discover.load_companies(config_dir)
    have = {r["company"]: dict(r) for r in conn.execute("SELECT * FROM companies")}
    failing = {(r[0], r[1]) for r in conn.execute("SELECT ats, slug FROM boards WHERE last_error IS NOT NULL")}
    todo, keep = [], []
    for c in companies:
        old = have.get(c["company"])
        if c["ats"] and c["slug"]:
            keep.append(discover.Result(company=c["company"], group=c["group"], domain=c["domain"],
                                        careers_url=c["careers_url"], ats=c["ats"], slug=c["slug"],
                                        status="pinned", checked_at=_now()))
            continue
        age = _age_hours(old["checked_at"]) / 24 if old else float("inf")
        due = (full or old is None or old.get("careers_url") != c["careers_url"]
               or (old["status"] == "ok" and (age > REDISCOVER_OK_DAYS or (old["ats"], old["slug"]) in failing))
               or (old["status"] != "ok" and age > REDISCOVER_BAD_DAYS))
        if due:
            todo.append(c)
    found = discover.run(todo, discover.discovery_client(), net.BUILDER_AGENT, workers=workers_n) if todo else []
    log.info("discovery: %d companies checked, %s", len(found), discover.summary(found)["status"] if found else {})
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
    from .sources import SOURCES
    return [{"ats": r["ats"], "slug": r["slug"], "company": r["company"], "options": None, "list": "companies",
             "enabled": True}
            for r in conn.execute("SELECT company, ats, slug FROM companies WHERE status IN ('ok', 'pinned')")
            if r["ats"] in SOURCES]


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


def seed_builder(index_file: Path | str, db_path: Path | str = BUILDER_DB) -> int:
    """Start a builder database from a published base (when the Action's cache has expired), so the
    next build only fetches what changed instead of everything. Returns postings seeded."""
    index_file = Path(index_file)
    tmp = None
    if index_file.suffix == ".xz":
        fd, tmp = tempfile.mkstemp(suffix=".db", dir=Path(db_path).parent)
        with os.fdopen(fd, "wb") as fout, lzma.open(index_file, "rb") as fin:
            shutil.copyfileobj(fin, fout, 1 << 20)
        index_file = Path(tmp)
    try:
        _check_index_file(index_file, "seed")
        conn = db.connect(db_path)
        conn.execute("ATTACH DATABASE ? AS s", (str(index_file),))
        db.load_zdicts(conn, "s")
        cols = [c for c in _cols(conn, "jobs", "s") if c in set(_cols(conn, "jobs")) and c not in ("mark", "fts_rowid")]
        bcols = [c for c in _cols(conn, "boards", "s") if c in set(_cols(conn, "boards"))]
        ccols = [c for c in _cols(conn, "companies", "s") if c in set(_cols(conn, "companies"))]
        with conn:
            conn.execute(f"INSERT OR REPLACE INTO jobs ({', '.join(cols)}) SELECT {', '.join(cols)} FROM s.jobs")
            conn.execute("INSERT OR REPLACE INTO job_content (uid, html) SELECT uid, zpack(unz(html)) FROM s.job_content")
            conn.execute("INSERT OR REPLACE INTO extractions SELECT content_hash, method, data, model, at FROM s.extractions")
            conn.execute("INSERT INTO events (uid, kind, at) SELECT uid, kind, at FROM s.events ORDER BY id")
            conn.execute(f"INSERT OR REPLACE INTO boards ({', '.join(bcols)}) SELECT {', '.join(bcols)} FROM s.boards")
            conn.execute(f"INSERT OR REPLACE INTO companies ({', '.join(ccols)}) SELECT {', '.join(ccols)} FROM s.companies")
            conn.execute("UPDATE jobs SET minhash = NULL, minhash_of = NULL")   # recomputed by the next build
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.execute("DETACH DATABASE s")
        conn.close()
        return n
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def _job_columns(conn, schema: str = "main") -> list[tuple[str, str]]:
    """(name, type) of the job columns a published index carries."""
    return [(r[1], r[2]) for r in conn.execute(f"PRAGMA {schema}.table_info(jobs)")
            if r[1] not in db.INDEX_EXCLUDED_COLUMNS]


def _update_signatures(conn, now: str) -> int:
    """Remember, per posting, a hash of everything a reader receives (and separately of its search
    text and its description), and when each last changed. Deltas carry only what changed."""
    conn.execute("CREATE TABLE IF NOT EXISTS export_sig (uid TEXT PRIMARY KEY, sig TEXT NOT NULL, "
                 "changed_at TEXT NOT NULL)")
    have = set(_cols(conn, "export_sig"))
    for col in ("fts_sig", "fts_changed_at", "content_sig", "content_changed_at"):
        if col not in have:
            conn.execute(f"ALTER TABLE export_sig ADD COLUMN {col} TEXT")
    cols = [n for n, _ in _job_columns(conn) if n not in VOLATILE_JOB_COLUMNS | {"mark", "fts_rowid"}]
    old = {r[0]: r[1:] for r in conn.execute("SELECT uid, sig, fts_sig, content_sig FROM export_sig")}
    sel = ", ".join(f"j.{c}" for c in cols)
    changed, n = [], 0
    h = lambda v: hashlib.blake2b(repr(v).encode(), digest_size=12).hexdigest()
    for row in conn.execute(f"SELECT {sel}, e.model, (SELECT 1 FROM job_content c WHERE c.uid = j.uid) "
                            "FROM jobs j LEFT JOIN extractions e ON e.content_hash = j.content_hash"):
        rec = dict(zip(cols, row))
        uid = rec["uid"]
        sig = h(tuple(row))
        fts = h((rec["title"], rec["company"], rec["location"], rec["content_hash"], rec["active"]))
        content = h((rec["content_hash"], row[-1]))
        prev = old.get(uid, (None, None, None))
        if prev != (sig, fts, content):
            n += prev[0] != sig
            changed.append((uid, sig, now, fts, now, content, now, prev[1] != fts, prev[2] != content))
    with conn:
        conn.executemany(
            """INSERT INTO export_sig(uid, sig, changed_at, fts_sig, fts_changed_at, content_sig, content_changed_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET
                   changed_at = CASE WHEN export_sig.sig IS excluded.sig THEN export_sig.changed_at ELSE excluded.changed_at END,
                   sig = excluded.sig,
                   fts_changed_at = CASE WHEN ?8 THEN excluded.fts_changed_at ELSE export_sig.fts_changed_at END,
                   fts_sig = excluded.fts_sig,
                   content_changed_at = CASE WHEN ?9 THEN excluded.content_changed_at ELSE export_sig.content_changed_at END,
                   content_sig = excluded.content_sig""", changed)
    return n


def _plain_db(out_path: Path) -> Path:
    plain = out_path.with_name(out_path.name.removesuffix(".xz"))
    for p in (plain, out_path, Path(str(plain) + "-wal"), Path(str(plain) + "-shm"), Path(str(plain) + "-journal")):
        p.unlink(missing_ok=True)
    return plain


def _finish(c, plain: Path, out_path: Path) -> dict:
    rows = c.execute("SELECT COUNT(*) FROM main.jobs").fetchone()[0]
    active = c.execute("SELECT COUNT(*) FROM main.jobs WHERE active = 1").fetchone()[0]
    c.execute("DETACH DATABASE src")
    c.execute("VACUUM")
    c.close()
    with open(plain, "rb") as fin, lzma.open(out_path, "wb", preset=6) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    plain.unlink()
    return {"file": out_path.name, "sha256": _sha256(out_path), "bytes": out_path.stat().st_size,
            "rows": rows, "active": active}


def _open_writer(src_path: Path, plain: Path, zdict: bytes):
    from .sanitize import html_to_text
    c = sqlite3.connect(plain)
    c.execute("PRAGMA journal_mode=OFF")
    c.execute("PRAGMA synchronous=OFF")
    c.create_function("unz", 1, db.unpack, deterministic=True)
    c.create_function("zd", 1, lambda h: db.pack_with(db.unpack(h), zdict), deterministic=True)
    c.create_function("fts_text", 1, lambda h: html_to_text(db.unpack(h))[:db.FTS_BODY_CHARS], deterministic=True)
    c.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    return c


def _zdict(src_path: Path, new: bool) -> bytes:
    """The description dictionary: trained afresh for each base, reused by its deltas."""
    conn = db.connect(src_path)
    try:
        saved = db.get_meta(conn, "index_zdict")
        if saved and not new:
            d = bytes.fromhex(saved)
        else:
            sample = [db.unpack(r[0]) for r in conn.execute(
                "SELECT html FROM job_content WHERE uid IN (SELECT uid FROM jobs WHERE active = 1) "
                "ORDER BY random() LIMIT 30000")]
            d = db.train_zdict(sample) if sample else b""
            db.set_meta(conn, "index_zdict", d.hex())
        db.register_zdict(d)
        return d
    finally:
        conn.close()


def _copy_shared(c, uid_filter: str, args=()) -> None:
    """Tables both files carry: extractions, events, boards, companies (+ the description dictionary)."""
    c.execute("INSERT INTO main.extractions (content_hash, method, data, model, at) "
              "SELECT content_hash, method, data, model, at FROM src.extractions "
              "WHERE content_hash IN (SELECT content_hash FROM main.jobs)")
    c.execute(f"INSERT INTO main.events (uid, kind, at) SELECT uid, kind, at FROM src.events "
              f"WHERE uid IN ({uid_filter}) ORDER BY id", args)
    bcols = ", ".join(x for x in _cols(c, "boards") if x in set(_cols(c, "boards", "src")))
    c.execute(f"INSERT INTO main.boards ({bcols}) SELECT {bcols} FROM src.boards")
    ccols = ", ".join(x for x in _cols(c, "companies") if x in set(_cols(c, "companies", "src")))
    c.execute(f"INSERT INTO main.companies ({ccols}) SELECT {ccols} FROM src.companies")


def _put_zdict(c, zdict: bytes) -> None:
    if zdict:
        c.execute("INSERT INTO main.zdict (data) VALUES (?)", (zdict,))


def _write_base(src_path: Path, out_path: Path, meta: dict) -> dict:
    """The whole index, ready to open: every posting, each description compressed against a shared
    dictionary (readable one row at a time, ~3x smaller on disk), and a contentless search index
    over open postings."""
    plain = _plain_db(out_path)
    zdict = _zdict(src_path, new=True)
    c = _open_writer(src_path, plain, zdict)
    cols = _job_columns(c, "src")
    db.create_index_tables(c, cols, fts=True, indexes=False)   # indexes after the bulk insert: faster
    names = [n for n, _ in cols]
    sel = ", ".join("NULL" if n in ("mark", "fts_rowid") else n for n in names)
    with c:
        c.execute(f"INSERT INTO main.jobs ({', '.join(names)}) SELECT {sel} FROM src.jobs ORDER BY uid")
        c.execute("INSERT INTO main.job_content (uid, html) SELECT uid, zd(html) FROM src.job_content "
                  "WHERE uid IN (SELECT uid FROM main.jobs)")
        _copy_shared(c, "SELECT uid FROM main.jobs")
        _put_zdict(c, zdict)
        c.execute("""INSERT INTO main.jobs_fts (rowid, title, company, location, body)
                     SELECT j.rowid, j.title, j.company, COALESCE(j.location, ''), fts_text(c.html)
                     FROM main.jobs j LEFT JOIN main.job_content c ON c.uid = j.uid WHERE j.active = 1""")
        c.execute("UPDATE main.jobs SET fts_rowid = rowid WHERE active = 1")
        c.executemany("INSERT INTO main.index_meta(key, value) VALUES (?, ?)",
                      [(k, json.dumps(v)) for k, v in meta.items()])
    c.executescript(db.INDEX_INDEXES)
    c.execute("INSERT INTO main.jobs_fts(jobs_fts) VALUES ('optimize')")
    c.commit()
    return _finish(c, plain, out_path)


def _write_delta(src_path: Path, out_path: Path, since: str, meta: dict) -> dict:
    """Postings changed after the base: full job rows; descriptions only where they changed; the
    search text of rows whose title/company/location/description changed or that reopened."""
    plain = _plain_db(out_path)
    zdict = _zdict(src_path, new=False)
    c = _open_writer(src_path, plain, zdict)
    cols = _job_columns(c, "src")
    db.create_index_tables(c, cols, fts=False, indexes=False)
    c.execute("CREATE TABLE fts_rows (uid TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, body TEXT)")
    names = [n for n, _ in cols]
    sel = ", ".join("NULL" if n in ("mark", "fts_rowid") else n for n in names)
    with c:
        c.execute("CREATE TEMP TABLE pick AS SELECT uid, fts_changed_at > ? AS fts, content_changed_at > ? AS content "
                  "FROM src.export_sig WHERE changed_at > ? OR fts_changed_at > ? OR content_changed_at > ?",
                  (since,) * 5)
        c.execute("CREATE UNIQUE INDEX temp.pick_uid ON pick(uid)")
        c.execute(f"INSERT INTO main.jobs ({', '.join(names)}) SELECT {sel} FROM src.jobs "
                  "WHERE uid IN (SELECT uid FROM pick)")
        c.execute("INSERT INTO main.job_content (uid, html) SELECT uid, zd(html) FROM src.job_content "
                  "WHERE uid IN (SELECT uid FROM pick WHERE content)")
        _copy_shared(c, "SELECT uid FROM pick")
        _put_zdict(c, zdict)
        c.execute("""INSERT INTO main.fts_rows (uid, title, company, location, body)
                     SELECT j.uid, j.title, j.company, COALESCE(j.location, ''), fts_text(c.html)
                     FROM src.jobs j LEFT JOIN src.job_content c ON c.uid = j.uid
                     WHERE j.active = 1 AND j.uid IN (SELECT uid FROM pick WHERE fts)""")
        c.executemany("INSERT INTO main.index_meta(key, value) VALUES (?, ?)",
                      [(k, json.dumps(v)) for k, v in meta.items()])
    return _finish(c, plain, out_path)


def export(db_path: Path | str, out_dir: Path | str, countries=(), published=None, full: bool = False) -> dict:
    """Write dist/manifest.json plus a new base.db.xz or delta.db.xz. Only files that changed are
    written, so the workflow uploads only those."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name in (BASE, DELTA, MANIFEST):
        (out / name).unlink(missing_ok=True)
    now = _now()
    conn = db.connect(db_path)
    changed = _update_signatures(conn, now)
    state = db.get_meta(conn, "index_base") or {}
    pub = json.loads(Path(published).read_text(encoding="utf-8")) if published and Path(published).exists() else None
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    since = state.get("built_at")
    pending = conn.execute("SELECT COUNT(*) FROM export_sig WHERE changed_at > ?", (since or "",)).fetchone()[0]
    coverage = _coverage(conn)
    conn.close()
    reason = ("asked for" if full else "first build" if not since
              else "published index differs from this builder's" if not pub or (pub.get("base") or {}).get("built_at") != since
              or pub.get("format") != FORMAT or pub.get("index_schema") != db.INDEX_SCHEMA
              or pub.get("rules_version") != workers.RULES_VERSION
              else "weekly rebuild" if _age_hours(since) > BASE_EVERY_DAYS * 24
              else "changes outgrew half the base" if pending > total / 2 else None)
    meta = {"built_at": now, "rules_version": workers.RULES_VERSION, "lighthouse": __version__,
            "countries": list(countries)}
    if reason:
        log.info("writing a new base: %s", reason)
        base = {**_write_base(Path(db_path), out / BASE, {**meta, "base_built_at": now}), "built_at": now}
        delta = None
        conn = db.connect(db_path)
        db.set_meta(conn, "index_base", {"built_at": now, "schema": db.SCHEMA_VERSION})
        conn.close()
    else:
        base = pub["base"]
        delta = {**_write_delta(Path(db_path), out / DELTA, since, {**meta, "base_built_at": since}),
                 "built_at": now, "base_built_at": since}
    manifest = {"format": FORMAT, "index_schema": db.INDEX_SCHEMA, "schema": db.SCHEMA_VERSION,
                "rules_version": workers.RULES_VERSION, "lighthouse": __version__, "built_at": now,
                "countries": list(countries), "jobs": _active_total(db_path), "boards": _board_total(db_path),
                "changed_this_run": changed, "switched_off": _switched_off(db_path), "coverage": coverage,
                "base": base, "delta": delta}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _coverage(conn) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) FROM companies GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}


def _active_total(db_path) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1").fetchone()[0]
    finally:
        conn.close()


def _switched_off(db_path) -> list[str]:
    """Boards that stopped existing (shown in the build log so the maintainer can fix or drop them)."""
    conn = db.connect(db_path)
    try:
        return [f"{a}:{b}" for a, b in conn.execute(
            "SELECT ats, slug FROM boards WHERE last_error LIKE 'Switched off automatically%' ORDER BY ats, slug")]
    finally:
        conn.close()


def _board_total(db_path) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM boards WHERE enabled = 1 AND last_ok IS NOT NULL").fetchone()[0]
    finally:
        conn.close()


# ================================================================ reader (everyone else)

def _url(repo: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{TAG}/{name}"


def _check_repo(repo: str) -> str:
    repo = (repo or "").strip().strip("/")
    if repo.count("/") != 1 or any(ch in repo for ch in " ?#"):
        raise IndexProblem(f"[index] repo should look like 'owner/name', not '{repo}'")
    return repo


def _get(client, url: str, stream: bool = False):
    """GET following redirects by hand, so every hop is checked against the network allowlist."""
    for _ in range(6):
        r = client.get(url, allow_redirects=False, stream=stream, headers={"Accept": "application/octet-stream"})
        if r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            continue
        if r.status_code == 404:
            raise IndexProblem("no job index published there yet (or the repo is private)")
        r.raise_for_status()
        return r
    raise IndexProblem("too many redirects")


def latest_manifest(client, repo: str) -> dict:
    return _get(client, _url(_check_repo(repo), MANIFEST)).json()


def compatible(m: dict, repo: str = "") -> str | None:
    """None if this copy can read the index, else what to do about it."""
    if m.get("format") == FORMAT and m.get("index_schema") == db.INDEX_SCHEMA:
        return None
    if (m.get("index_schema") or 0) > db.INDEX_SCHEMA or (m.get("format") or 0) > FORMAT:
        where = f" from github.com/{repo}" if repo else ""
        return f"the job index needs a newer Lighthouse ({m.get('lighthouse')}). Download the latest version{where}."
    return "the job index was built by an older Lighthouse; it updates itself after its next build"


def _data_dir(conn) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent


def download(client, repo: str, info: dict, folder: Path, name: str | None = None) -> Path:
    """Stream one index file, check size and SHA-256, unpack into `folder`. Returns the database path."""
    if int(info.get("bytes") or 0) > MAX_BYTES:
        raise IndexProblem("index file is unexpectedly large")
    folder.mkdir(parents=True, exist_ok=True)
    fd, xz_path = tempfile.mkstemp(prefix="download-", suffix=".xz", dir=folder)
    h, n = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as f:
            r = _get(client, _url(_check_repo(repo), info["file"]), stream=True)
            for chunk in r.iter_content(1 << 20):
                n += len(chunk)
                if n > MAX_BYTES:
                    raise IndexProblem("index download is larger than its manifest says")
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != info.get("sha256"):
            raise IndexProblem("index download is incomplete or corrupt (checksum mismatch); will retry")
        out = folder / (name or Path(xz_path).name.replace(".xz", ".db"))
        part = Path(str(out) + ".part")
        with lzma.open(xz_path, "rb") as fin, open(part, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        part.replace(out)
        return out
    finally:
        Path(xz_path).unlink(missing_ok=True)


def _check_index_file(path: Path, kind: str) -> None:
    c = sqlite3.connect(path)
    try:
        app, ver = c.execute("PRAGMA application_id").fetchone()[0], c.execute("PRAGMA user_version").fetchone()[0]
        if app != db.INDEX_APP_ID or ver != db.INDEX_SCHEMA:
            raise IndexProblem(f"{kind} file isn't a job index this Lighthouse can read")
        if kind == "base":
            c.execute("PRAGMA journal_mode=WAL")   # readers keep reading while a delta is applied
    except sqlite3.DatabaseError as exc:
        raise IndexProblem(f"{kind} file is damaged: {exc}") from exc
    finally:
        c.close()


def install_base(conn, path: Path) -> None:
    """Make `path` (an unpacked base in data/index/) the current index, for this connection and every
    connection opened after. The previous index file is deleted once nothing has it open."""
    _check_index_file(path, "base")
    folder = _data_dir(conn)
    old = db.index_path(conn, folder)
    conn.commit()
    if db.is_reader(conn):
        conn.execute("DETACH DATABASE idx")
    db.set_meta(conn, "index_file", str(Path(path).resolve().relative_to(folder.resolve())).replace("\\", "/"))
    conn.execute("ATTACH DATABASE ? AS idx", (str(path),))
    db._tune(conn, "idx")
    cleanup(conn, keep=Path(path))
    if old and old.name != "index-empty.db" and old.resolve() != Path(path).resolve():
        _unlink_db(old)


def _unlink_db(path: Path) -> None:
    for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            p.unlink(missing_ok=True)
        except OSError:   # still open somewhere (Windows): removed by a later cleanup
            pass


def cleanup(conn, keep: Path | None = None) -> None:
    """Remove index files that are no longer current, and the pre-0.6 single-file database once a
    real index has been installed (it held a full copy of the jobs: often 1 GB+)."""
    folder = _data_dir(conn)
    cur = (keep or db.index_path(conn, folder) or Path()).resolve()
    for p in (folder / "index").glob("index-*.db"):
        if p.resolve() != cur and p.name != "index-empty.db":
            _unlink_db(p)
    for p in (folder / "index").glob("download-*"):
        _unlink_db(p)
    legacy = db.get_meta(conn, "legacy_db")
    if legacy and cur.name != "index-empty.db":
        _unlink_db(folder / legacy)
        if not (folder / legacy).exists():
            db.set_meta(conn, "legacy_db", None)


def apply_delta(conn, path: Path) -> dict:
    """Bring a delta into the current index with a handful of SQL statements."""
    _check_index_file(path, "delta")
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS d", (str(path),))
    try:
        db.load_zdicts(conn, "d")
        cols = [c for c in _cols(conn, "jobs", "d") if c in set(_cols(conn, "jobs", "idx")) and c not in ("mark", "fts_rowid")]
        safe_url = "CASE WHEN url LIKE 'https://%' OR url LIKE 'http://%' THEN url ELSE '' END"
        select = ", ".join(safe_url if c == "url" else c for c in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "uid")
        bcols = ", ".join(c for c in _cols(conn, "boards", "d") if c in set(_cols(conn, "boards", "idx")))
        ccols = ", ".join(c for c in _cols(conn, "companies", "d") if c in set(_cols(conn, "companies", "idx")))
        with conn:
            new = conn.execute("SELECT COUNT(*) FROM d.jobs WHERE uid NOT IN (SELECT uid FROM idx.jobs)").fetchone()[0]
            conn.execute(f"INSERT INTO idx.jobs ({', '.join(cols)}) SELECT {select} FROM d.jobs WHERE true "
                         f"ON CONFLICT(uid) DO UPDATE SET {updates}")
            conn.execute("INSERT INTO idx.job_content (uid, html) SELECT uid, html FROM d.job_content WHERE true "
                         "ON CONFLICT(uid) DO UPDATE SET html = excluded.html")
            conn.execute("INSERT INTO idx.extractions (content_hash, method, data, model, at) "
                         "SELECT content_hash, method, data, model, at FROM d.extractions WHERE true "
                         "ON CONFLICT(content_hash) DO UPDATE SET method = excluded.method, data = excluded.data, "
                         "model = excluded.model, at = excluded.at")
            conn.execute("DELETE FROM idx.events WHERE uid IN (SELECT uid FROM d.jobs)")
            conn.execute("INSERT INTO idx.events (uid, kind, at) SELECT uid, kind, at FROM d.events ORDER BY id")
            conn.execute("DELETE FROM idx.boards")
            conn.execute(f"INSERT INTO idx.boards ({bcols}) SELECT {bcols} FROM d.boards")
            conn.execute("DELETE FROM idx.companies")
            conn.execute(f"INSERT INTO idx.companies ({ccols}) SELECT {ccols} FROM d.companies")
            conn.execute("INSERT OR REPLACE INTO idx.index_meta (key, value) SELECT key, value FROM d.index_meta")
            conn.execute("INSERT INTO idx.zdict (data) SELECT data FROM d.zdict WHERE data NOT IN (SELECT data FROM idx.zdict)")
            # Search text: new rows get fresh search-index rowids (the old entries simply stop matching any job).
            start = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM idx.jobs_fts").fetchone()[0]
            conn.execute("CREATE TEMP TABLE fts_new AS SELECT ? + row_number() OVER (ORDER BY uid) AS rid, uid, title, "
                         "company, location, body FROM d.fts_rows", (start,))
            conn.execute("INSERT INTO idx.jobs_fts (rowid, title, company, location, body) "
                         "SELECT rid, title, company, location, body FROM temp.fts_new")
            conn.execute("UPDATE idx.jobs SET fts_rowid = (SELECT rid FROM temp.fts_new f WHERE f.uid = jobs.uid) "
                         "WHERE uid IN (SELECT uid FROM temp.fts_new)")
            conn.execute("DROP TABLE temp.fts_new")
            changed = conn.execute("SELECT COUNT(*) FROM d.jobs").fetchone()[0]
    finally:
        conn.execute("DETACH DATABASE d")
    return {"postings": changed, "new": new}


def _personal_refresh(conn) -> None:
    """Put your own layer back on top of a new index: marks, your Claude extractions, sponsor matches."""
    from . import extract, sponsors
    db.sync_marks(conn)
    extract.derive_claude(conn)
    if conn.execute("SELECT 1 FROM sponsor_files LIMIT 1").fetchone():
        sponsors.refresh_matches(conn, None)
    conn.commit()
    db.bump_generation(conn)


def import_file(conn, cfg, path: Path | str, full: bool = True, built_at: str | None = None) -> dict:
    """Use a local index file (.db or .db.xz): a base replaces the index, a delta is applied to it.
    For testing a build before publishing it."""
    path = Path(path)
    folder = _data_dir(conn) / "index"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (built_at or _now()).replace(":", "").replace("+", "")[:22]
    target = folder / (f"index-{stamp}.db" if full else f"download-{stamp}.db")
    if path.suffix == ".xz":
        with lzma.open(path, "rb") as fin, open(target, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    else:
        shutil.copyfile(path, target)
    if full:
        install_base(conn, target)
        stats = {"postings": conn.execute("SELECT COUNT(*) FROM idx.jobs").fetchone()[0]}
    else:
        try:
            stats = apply_delta(conn, target)
        finally:
            _unlink_db(target)
    _personal_refresh(conn)
    return stats


def sync(conn, cfg, client, folder: Path | None = None) -> dict:
    """Bring this copy up to date with the published index. Never raises: if anything goes wrong,
    the jobs you already have stay, and the Sources page says why."""
    repo = cfg.index.repo
    folder = Path(folder or _data_dir(conn))
    info = db.get_meta(conn, "index") or {}
    did = []
    try:
        if not db.is_user_db(conn):
            raise IndexProblem("this database isn't set up as a reader (run the app once to convert it)")
        m = latest_manifest(client, repo)
        why = compatible(m, repo)
        if why:
            raise IndexProblem(why)
        base, delta = m["base"], m.get("delta")
        if info.get("base_built_at") != base["built_at"] or not db.has_table(conn, "jobs_fts"):
            stamp = base["built_at"].replace(":", "").replace("+", "")[:22]
            path = download(client, repo, base, folder / "index", name=f"index-{stamp}.db")
            install_base(conn, path)
            info.update(base_built_at=base["built_at"], built_at=base["built_at"])
            db.set_meta(conn, "index", info)   # a failed delta below won't redo the base
            did.append({"file": "base", "postings": base.get("rows")})
        if delta and delta.get("base_built_at") == base["built_at"] and delta["built_at"] > (info.get("built_at") or ""):
            path = download(client, repo, delta, folder / "index")
            try:
                stats = apply_delta(conn, path)
            finally:
                _unlink_db(path)
            info["built_at"] = delta["built_at"]
            did.append({"file": "delta", **stats})
        if did:
            _personal_refresh(conn)
        info.update(error=None, jobs=m.get("jobs"), boards=m.get("boards"), countries=m.get("countries"),
                    lighthouse=m.get("lighthouse"), coverage=m.get("coverage"))
    except Exception as exc:
        log.warning("job index not updated: %s", exc)
        info["error"] = str(exc)
    info["checked_at"] = _now()
    db.set_meta(conn, "index", info)
    try:
        cleanup(conn)
    except OSError:
        pass
    return {"source": "index", "board": repo, "updated": did, "error": info.get("error")}


def status(conn, cfg) -> dict:
    info = db.get_meta(conn, "index") or {}
    return {"repo": cfg.index.repo or None, "built_at": info.get("built_at"),
            "base_built_at": info.get("base_built_at"), "checked_at": info.get("checked_at"),
            "jobs": info.get("jobs"), "boards": info.get("boards"), "countries": info.get("countries") or [],
            "coverage": info.get("coverage") or {}, "error": info.get("error"),
            "stale": bool(info.get("built_at")) and _age_hours(info.get("built_at")) > cfg.index.stale_after_hours}


def suggest_url(repo: str) -> str:
    """Where users suggest a new board: a pre-filled GitHub issue on the index repo."""
    from urllib.parse import quote
    return (f"https://github.com/{repo}/issues/new?title={quote('Board suggestion: ')}"
            f"&body={quote('Company:' + chr(10) + 'Careers page or job-board link:' + chr(10))}")
