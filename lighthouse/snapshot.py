"""The shared job index: built by one GitHub Action, read by every user's Lighthouse.

One public repo runs a scheduled Action that fetches every board, de-duplicates and analyses the
postings, and publishes the result on its `job-index` release:

    manifest.json   what's current, with each file's SHA-256
    base.db.xz      every posting; rebuilt weekly (or when the changes outgrow half of it)
    delta.db.xz     only the postings that changed since base.db.xz; rebuilt every run

Readers ([index] repo set) download the base once a week and the small delta after that. They
never fetch boards and never change job data; they only add their own layer on top (eligibility,
fit verdicts, marks, Claude extractions), which never leaves their computer.

    builder:  python -m lighthouse index build --out dist [--published prev/manifest.json]
    reader:   automatic on every refresh; or python -m lighthouse index import | status

The published files are built by whitelist: only job, board, rule-extraction and history rows are
copied, so nothing personal can be in them even if the build machine had a profile.
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
from .sanitize import ensure_safe

log = logging.getLogger("lighthouse.index")

FORMAT = 2
TAG = "job-index"
BASE, DELTA, MANIFEST = "base.db.xz", "delta.db.xz", "manifest.json"
BUILDER_DB = db.DEFAULT_DB.parent / "builder.db"   # the Action's working database (kept in its cache)
BASE_EVERY_DAYS = 7
MAX_BYTES = 1_900_000_000          # GitHub's per-file release limit is 2 GiB

# Never copied: personal to whoever owns the database.
PERSONAL_JOB_COLUMNS = {"elig", "elig_reasons", "elig_version", "fts_rowid", "mark"}
# Builder-only: big and useless to readers (near-duplicate signatures; readers don't cluster).
BUILDER_JOB_COLUMNS = {"minhash", "minhash_of"}
# Changes on every run for every open posting; not a reason to re-send a row.
VOLATILE_JOB_COLUMNS = {"last_seen"}


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
          detail_calls: int | None = None, published: Path | str | None = None, full: bool = False) -> dict:
    """Fetch every enabled board into the builder database, then write the index files."""
    from . import config as config_mod
    from . import fetch
    conn = db.connect(db_path)
    if db.has_fts(conn):   # readers build their own search index; skip the work here
        conn.execute("DROP TABLE jobs_fts")
        conn.commit()
    p = profile_mod.load(conn)
    p["countries"] = [c.strip().upper() for c in countries if c.strip()]
    profile_mod.save(conn, p)   # countries only: no eligibility is computed on the builder
    cfg = config_mod.load(config_path or config_mod.DEFAULT_CONFIG)
    retired = apply_board_list(conn, cfg, retry_switched_off=full)
    conn.close()
    overrides = {"max_detail_calls_per_board": detail_calls} if detail_calls else None
    results = fetch.run(config_path or config_mod.DEFAULT_CONFIG, db_path, use_snapshot=False, overrides=overrides)
    failed = sum(1 for r in results or [] if "error" in r)
    log.info("fetched %d boards (%d failed)", len(results or []), failed)
    if retired:   # boards switched off in the list: their postings were closed, re-cluster those companies
        conn = db.connect(db_path)
        fetch.postprocess(conn, cfg, retired)
        conn.close()
    return export(db_path, out_dir, countries=p["countries"], published=published, full=full)


def apply_board_list(conn, cfg, retry_switched_off: bool = False) -> set[str]:
    """On the builder, the committed board lists are the truth (there's no Sources page): each run
    applies their on/off column, switches off boards no longer listed, and closes the postings of
    boards that are off so readers see them disappear. Boards switched off automatically (gone)
    stay off until a full build. Returns the company keys whose postings were closed."""
    db.sync_config_boards(conn, cfg.boards)
    want = {(b["ats"], b["slug"]): bool(b.get("enabled", True)) for b in cfg.boards}
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


def _shipped_job_columns(conn) -> list[str]:
    return [c for c in _cols(conn, "jobs") if c not in PERSONAL_JOB_COLUMNS | BUILDER_JOB_COLUMNS]


def _update_signatures(conn, now: str) -> int:
    """Remember, per posting, a hash of everything a reader receives and when it last changed."""
    conn.execute("CREATE TABLE IF NOT EXISTS export_sig (uid TEXT PRIMARY KEY, sig TEXT NOT NULL, "
                 "changed_at TEXT NOT NULL)")
    cols = [c for c in _shipped_job_columns(conn) if c not in VOLATILE_JOB_COLUMNS]
    old = dict(conn.execute("SELECT uid, sig FROM export_sig").fetchall())
    sel = ", ".join(f"j.{c}" for c in cols)
    changed = []
    for row in conn.execute(f"SELECT {sel}, e.model, e.method FROM jobs j "
                            "LEFT JOIN extractions e ON e.content_hash = j.content_hash"):
        sig = hashlib.blake2b(repr(tuple(row)).encode(), digest_size=12).hexdigest()
        uid = row[cols.index("uid")]
        if old.get(uid) != sig:
            changed.append((uid, sig, now))
    with conn:
        conn.executemany("INSERT INTO export_sig(uid, sig, changed_at) VALUES (?,?,?) ON CONFLICT(uid) "
                         "DO UPDATE SET sig=excluded.sig, changed_at=excluded.changed_at", changed)
    return len(changed)


def _write(src_path: Path, out_path: Path, since: str | None) -> dict:
    """Write one index file: every posting (since=None) or those changed after `since`."""
    plain = out_path.with_name(out_path.name.removesuffix(".xz"))
    for p in (plain, out_path, Path(str(plain) + "-wal"), Path(str(plain) + "-shm")):
        p.unlink(missing_ok=True)
    tmp = db.connect(plain)           # same schema as everyone's database
    tmp.execute("DROP TABLE IF EXISTS jobs_fts")
    tmp.commit()
    tmp.execute("PRAGMA journal_mode=DELETE")
    tmp.close()
    c = sqlite3.connect(plain)
    c.create_function("unz", 1, db.unpack, deterministic=True)
    c.execute("ATTACH DATABASE ? AS src", (str(src_path),))
    with c:
        if since is None:
            c.execute("CREATE TEMP TABLE pick AS SELECT uid FROM src.jobs")
        else:
            c.execute("CREATE TEMP TABLE pick AS SELECT uid FROM src.export_sig WHERE changed_at > ?", (since,))
        cols = ", ".join(_shipped_job_columns(c))
        c.execute(f"INSERT INTO main.jobs ({cols}) SELECT {cols} FROM src.jobs WHERE uid IN (SELECT uid FROM pick)")
        # Plain text: compressing the whole file at once is ~3x smaller than per-description zlib,
        # because postings from one employer share most of their boilerplate.
        c.execute("INSERT INTO main.job_content (uid, html) SELECT uid, unz(html) FROM src.job_content "
                  "WHERE uid IN (SELECT uid FROM pick)")
        c.execute("INSERT INTO main.extractions (content_hash, method, data, model, at) "
                  "SELECT content_hash, method, data, model, at FROM src.extractions "
                  "WHERE method = 'rules' AND content_hash IN (SELECT content_hash FROM main.jobs)")
        c.execute("INSERT INTO main.events (uid, kind, at) SELECT uid, kind, at FROM src.events "
                  "WHERE uid IN (SELECT uid FROM pick) ORDER BY id")
        bcols = ", ".join(x for x in _cols(c, "boards") if x in set(_cols(c, "boards", "src")))
        c.execute(f"INSERT INTO main.boards ({bcols}) SELECT {bcols} FROM src.boards")
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
    reason = ("asked for" if full else "first build" if not since
              else "published index differs from this builder's" if not pub or (pub.get("base") or {}).get("built_at") != since
              or pub.get("format") != FORMAT or pub.get("schema") != db.SCHEMA_VERSION
              else "weekly rebuild" if _age_hours(since) > BASE_EVERY_DAYS * 24
              else "changes outgrew half the base" if pending > total / 2 else None)
    conn.close()
    if reason:
        log.info("writing a new base: %s", reason)
        base = {**_write(Path(db_path), out / BASE, None), "built_at": now}
        delta = None
        conn = db.connect(db_path)
        db.set_meta(conn, "index_base", {"built_at": now, "schema": db.SCHEMA_VERSION})
        conn.close()
    else:
        base = pub["base"]
        delta = {**_write(Path(db_path), out / DELTA, since), "built_at": now, "base_built_at": since}
    manifest = {"format": FORMAT, "schema": db.SCHEMA_VERSION, "rules_version": workers.RULES_VERSION,
                "lighthouse": __version__, "built_at": now, "countries": list(countries),
                "jobs": _active_total(db_path),
                "boards": _board_total(db_path), "changed_this_run": changed,
                "switched_off": _switched_off(db_path), "base": base, "delta": delta}
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


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
    if m.get("format") == FORMAT and m.get("schema") == db.SCHEMA_VERSION:
        return None
    if (m.get("schema") or 0) > db.SCHEMA_VERSION or (m.get("format") or 0) > FORMAT:
        where = f" from github.com/{repo}" if repo else ""
        return f"the job index needs a newer Lighthouse ({m.get('lighthouse')}). Download the latest version{where}."
    return "the job index was built by an older Lighthouse; it updates itself after its next build"


def download(client, repo: str, info: dict, folder: Path) -> Path:
    """Stream one index file, check size and SHA-256, unpack. Returns the path of the database."""
    if int(info.get("bytes") or 0) > MAX_BYTES:
        raise IndexProblem("index file is unexpectedly large")
    folder.mkdir(parents=True, exist_ok=True)
    fd, xz_path = tempfile.mkstemp(prefix="index-", suffix=".xz", dir=folder)
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
        out = Path(xz_path[:-3] + ".db")
        with lzma.open(xz_path, "rb") as fin, open(out, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        return out
    finally:
        Path(xz_path).unlink(missing_ok=True)


def merge(conn: sqlite3.Connection, path: Path | str, full: bool, built_at: str) -> dict:
    """Bring an index file into your database.

    full=True (a base file): it is the whole truth, so boards and open postings not in it are
    removed/closed. full=False (a delta): only the rows it contains are updated.
    Your marks, verdicts, eligibility and Claude extractions are never touched. Descriptions and
    links are re-checked, since the file comes from someone else's machine."""
    from . import extract
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS snap", (str(path),))
    try:
        if conn.execute("PRAGMA snap.user_version").fetchone()[0] != db.SCHEMA_VERSION:
            raise IndexProblem("index file schema differs from this database")
        snap_cols = set(_cols(conn, "jobs", "snap"))
        cols = [c for c in _cols(conn, "jobs") if c in snap_cols and c not in PERSONAL_JOB_COLUMNS]
        safe_url = "CASE WHEN url LIKE 'https://%' OR url LIKE 'http://%' THEN url ELSE '' END"
        select = ", ".join(safe_url if c == "url" else c for c in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "uid")
        bcols = [c for c in _cols(conn, "boards") if c in set(_cols(conn, "boards", "snap"))]
        for t in ("inc", "chg"):
            conn.execute(f"DROP TABLE IF EXISTS temp.{t}")
        with conn:
            conn.execute(f"""INSERT INTO main.boards ({', '.join(bcols)}) SELECT {', '.join(bcols)} FROM snap.boards
                             WHERE true ON CONFLICT(ats, slug) DO UPDATE SET
                             {', '.join(f'{c} = excluded.{c}' for c in bcols if c not in ('ats', 'slug'))}""")
            if full:
                conn.execute("""DELETE FROM main.boards WHERE NOT EXISTS
                                (SELECT 1 FROM snap.boards s WHERE s.ats = boards.ats AND s.slug = boards.slug)""")
            conn.execute("CREATE TEMP TABLE inc AS SELECT uid, content_hash, row_sig, company_key FROM snap.jobs")
            conn.execute("CREATE UNIQUE INDEX temp.inc_pk ON inc(uid)")
            # New rows or rows whose text/fields changed: these need their description and search entry.
            conn.execute("""CREATE TEMP TABLE chg AS SELECT i.uid FROM temp.inc i LEFT JOIN main.jobs m ON m.uid = i.uid
                            WHERE m.uid IS NULL OR m.content_hash IS NOT i.content_hash OR m.row_sig IS NOT i.row_sig
                               OR m.fts_rowid IS NULL OR NOT EXISTS (SELECT 1 FROM main.job_content c WHERE c.uid = i.uid)""")
            conn.execute("CREATE UNIQUE INDEX temp.chg_pk ON chg(uid)")
            new = conn.execute("SELECT COUNT(*) FROM temp.inc WHERE uid NOT IN (SELECT uid FROM main.jobs)").fetchone()[0]
            conn.execute(f"""INSERT INTO main.jobs ({', '.join(cols)}) SELECT {select} FROM snap.jobs
                             WHERE true ON CONFLICT(uid) DO UPDATE SET {updates}""")
            closed = 0
            if full:
                closed = conn.execute("""UPDATE main.jobs SET active = 0, closed_at = COALESCE(closed_at, ?)
                                         WHERE active = 1 AND uid NOT IN (SELECT uid FROM temp.inc)""",
                                      (built_at,)).rowcount
            conn.execute("""INSERT INTO main.extractions (content_hash, method, data, model, at)
                            SELECT content_hash, method, data, model, at FROM snap.extractions WHERE method = 'rules'
                            ON CONFLICT(content_hash) DO UPDATE SET method = excluded.method, data = excluded.data,
                               model = excluded.model, at = excluded.at
                            WHERE extractions.method = 'rules'""")   # a Claude extraction you paid for wins
            conn.execute("DELETE FROM main.events WHERE uid IN (SELECT uid FROM temp.inc)")
            conn.execute("INSERT INTO main.events (uid, kind, at) SELECT uid, kind, at FROM snap.events ORDER BY id")
            conn.execute("UPDATE main.jobs SET last_seen = ? WHERE active = 1", (built_at,))
            cur = conn.execute("SELECT uid, html FROM snap.job_content WHERE uid IN (SELECT uid FROM temp.chg)")
            while batch := cur.fetchmany(2000):
                conn.executemany("INSERT INTO main.job_content (uid, html) VALUES (?, ?) "
                                 "ON CONFLICT(uid) DO UPDATE SET html = excluded.html",
                                 [(u, db.pack(ensure_safe(db.unpack(h)))) for u, h in batch])
        if db.has_fts(conn):
            with conn:
                rows = conn.execute("""SELECT j.uid, j.title, j.company, j.location, unz(c.html) AS html
                                       FROM main.jobs j LEFT JOIN main.job_content c ON c.uid = j.uid
                                       WHERE j.uid IN (SELECT uid FROM temp.chg)""").fetchall()
                for r in rows:
                    db._fts_put(conn, r["uid"], r["title"], r["company"], r["location"], r["html"])
        companies = [r[0] for r in conn.execute("SELECT DISTINCT company_key FROM temp.inc")]
        yours = [r[0] for r in conn.execute(
            """SELECT DISTINCT j.company_key FROM main.jobs j JOIN main.extractions e ON e.content_hash = j.content_hash
               WHERE e.method <> 'rules' AND j.uid IN (SELECT uid FROM temp.inc)""")]
        changed = conn.execute("SELECT COUNT(*) FROM temp.chg").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM temp.inc").fetchone()[0]
        for t in ("inc", "chg"):
            conn.execute(f"DROP TABLE temp.{t}")
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE snap")
    if yours:
        extract.derive(conn, yours)   # re-apply your own Claude extractions on top of the index's
    return {"postings": total, "new": new, "changed": changed, "closed": closed, "companies": companies}


def _personal_refresh(conn, cfg, companies) -> None:
    from . import sponsors
    db.sync_marks(conn)
    profile_mod.recompute(conn, None, cfg.settings.yoe_slack_years)
    sponsors.refresh_matches(conn, companies)
    conn.commit()
    db.bump_generation(conn)


def import_file(conn, cfg, path: Path | str, full: bool = True, built_at: str | None = None) -> dict:
    """Merge a local index file (.db or .db.xz). For testing a build before publishing it."""
    path = Path(path)
    tmp = None
    if path.suffix == ".xz":
        fd, tmp = tempfile.mkstemp(suffix=".db", dir=Path(db.DEFAULT_DB).parent)
        with os.fdopen(fd, "wb") as fout, lzma.open(path, "rb") as fin:
            shutil.copyfileobj(fin, fout, 1 << 20)
        path = Path(tmp)
    try:
        stats = merge(conn, path, full, built_at or _now())
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    _personal_refresh(conn, cfg, stats["companies"])
    return {k: v for k, v in stats.items() if k != "companies"}


def sync(conn, cfg, client, folder: Path | None = None) -> dict:
    """Bring this copy up to date with the published index. Never raises: if anything goes wrong,
    the jobs you already have stay, and the Sources page says why."""
    repo = cfg.index.repo
    folder = folder or Path(db.DEFAULT_DB).parent
    info = db.get_meta(conn, "index") or {}
    did = []
    try:
        m = latest_manifest(client, repo)
        why = compatible(m, repo)
        if why:
            raise IndexProblem(why)
        base, delta = m["base"], m.get("delta")
        companies: set = set()
        if info.get("base_built_at") != base["built_at"]:
            path = download(client, repo, base, folder)
            try:
                stats = merge(conn, path, True, base["built_at"])
            finally:
                Path(path).unlink(missing_ok=True)
            companies |= set(stats.pop("companies"))
            info.update(base_built_at=base["built_at"], built_at=base["built_at"])
            db.set_meta(conn, "index", info)   # a failed delta below won't redo the base
            did.append({"file": "base", **stats})
        if delta and delta.get("base_built_at") == base["built_at"] and delta["built_at"] > (info.get("built_at") or ""):
            path = download(client, repo, delta, folder)
            try:
                stats = merge(conn, path, False, delta["built_at"])
            finally:
                Path(path).unlink(missing_ok=True)
            companies |= set(stats.pop("companies"))
            info["built_at"] = delta["built_at"]
            did.append({"file": "delta", **stats})
        if did:
            _personal_refresh(conn, cfg, sorted(companies))
        info.update(error=None, jobs=m.get("jobs"), boards=m.get("boards"), countries=m.get("countries"),
                    lighthouse=m.get("lighthouse"))
    except Exception as exc:
        log.warning("job index not updated: %s", exc)
        info["error"] = str(exc)
    info["checked_at"] = _now()
    db.set_meta(conn, "index", info)
    return {"source": "index", "board": repo, "updated": did, "error": info.get("error")}


def status(conn, cfg) -> dict:
    info = db.get_meta(conn, "index") or {}
    return {"repo": cfg.index.repo or None, "built_at": info.get("built_at"),
            "base_built_at": info.get("base_built_at"), "checked_at": info.get("checked_at"),
            "jobs": info.get("jobs"), "boards": info.get("boards"), "countries": info.get("countries") or [],
            "error": info.get("error"),
            "stale": bool(info.get("built_at")) and _age_hours(info.get("built_at")) > cfg.index.stale_after_hours}


def suggest_url(repo: str) -> str:
    """Where users suggest a new board: a pre-filled GitHub issue on the index repo."""
    from urllib.parse import quote
    return (f"https://github.com/{repo}/issues/new?title={quote('Board suggestion: ')}"
            f"&body={quote('Company:' + chr(10) + 'Careers page or job-board link:' + chr(10))}")
