"""Pull every enabled board into the local database, then post-process.

Network calls run in a thread pool (fetch_workers) with a shared per-host throttle;
all database writes happen on one thread (SQLite has a single writer). After the
fetch, only companies whose postings changed are re-clustered and re-extracted.
"""

from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import db, dedupe, extract, ghost, profile, sponsors, workers
from .config import DEFAULT_CONFIG
from .net import default_client
from .sources import SOURCES, BoardNotFound
from .sources.base import Unchanged

log = logging.getLogger("lighthouse.fetch")

_lock = threading.Lock()
STATUS: dict = {"running": False, "done": 0, "total": 0, "started": None, "finished": None, "results": []}


def _fetch_board(board: dict, known: dict, client, max_detail: int, skip_title=None, countries=frozenset()):
    cls = SOURCES[board["ats"]]
    src = cls(client=client, max_detail_calls=max_detail, company=board.get("company"))
    src.skip_title, src.countries, src.known = skip_title, countries, known
    src.prev_body_hash = board.get("body_hash")
    try:
        return src.fetch(board["slug"], known=known), src
    except Unchanged:
        return None, src


def run(config_path: Path | str = DEFAULT_CONFIG, db_path: Path | str = db.DEFAULT_DB,
        only: list[tuple[str, str]] | None = None, wait: bool = True,
        use_snapshot: bool = True, overrides: dict | None = None) -> list[dict] | None:
    """Fetch all enabled boards (or just `only`). Returns per-board results, or None if a
    fetch is already running and wait=False.

    With [index] repo set (a reader), nothing is fetched from job boards: the shared index is
    downloaded instead. use_snapshot=False forces a real fetch (what the index builder does)."""
    if not _lock.acquire(blocking=wait):
        return None
    try:
        return _run(config_path, db_path, only, use_snapshot, overrides or {})
    finally:
        STATUS["running"] = False
        STATUS["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _lock.release()


def _run(config_path, db_path, only, use_snapshot=True, overrides=None):
    cfg = config_mod.load(config_path)
    s = cfg.settings
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(s, k):
            setattr(s, k, v)
    conn = db.connect(db_path)
    if not (use_snapshot and cfg.reader):
        db.sync_config_boards(conn, cfg.boards)
    client = default_client(s.request_timeout_seconds, s.per_host_requests_per_second)
    STATUS.update(running=True, done=0, total=0, results=[],
                  started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    results, touched = [], set()
    if use_snapshot and cfg.reader:
        # Reader: job data comes only from the shared index. Nothing is fetched from job boards here.
        if only is not None:
            conn.close()
            return []
        from . import snapshot
        STATUS["phase"] = "index"
        results.append(snapshot.sync(conn, cfg, client, Path(db_path).parent))
        db.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        conn.close()
        return results
    STATUS["phase"] = "boards"
    boards = [dict(b) for b in db.list_boards(conn, enabled_only=True)
              if b["ats"] in SOURCES and (only is None or (b["ats"], b["slug"]) in only)
              ]
    STATUS["total"] = len(boards)
    hourly = re.compile("|".join(config_mod.HOURLY_TITLE_PATTERNS), re.I) if s.skip_hourly_titles else None
    countries = frozenset(profile.load(conn).get("countries") or [])
    with ThreadPoolExecutor(max_workers=max(1, s.fetch_workers)) as pool:
        futures = {pool.submit(_fetch_board, b, db.known_postings(conn, b["ats"], b["slug"]), client,
                               s.max_detail_calls_per_board, hourly, countries): b for b in boards}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                jobs, src = fut.result()
            except Exception as exc:  # one bad board shouldn't stop the run
                log.warning("%s/%s failed: %s", b["ats"], b["slug"], exc)
                off = db.board_result(conn, b["ats"], b["slug"], error=str(exc) or type(exc).__name__,
                                      not_found=isinstance(exc, BoardNotFound), disable_after=s.disable_after_failures)
                results.append({"source": b["ats"], "board": b["slug"], "error": str(exc), "switched_off": off})
            else:
                if jobs is None:  # byte-identical to last run
                    db.board_unchanged(conn, b["ats"], b["slug"])
                    results.append({"source": b["ats"], "board": b["slug"], "unchanged_board": True})
                else:
                    # Postings whose locations only became known from detail calls (Workday) get the country check now.
                    kept = [j for j in jobs if src.wanted(j)]
                    counts = db.upsert_board(conn, b["ats"], b["slug"], kept, keep_alive=src.dropped)
                    counts["filtered_out"] = len(src.dropped)
                    touched |= counts.pop("touched")
                    counts.pop("changed")
                    db.board_result(conn, b["ats"], b["slug"], count=len(kept), body_hash=src.body_hash)
                    log.info("%s/%s: %s", b["ats"], b["slug"], counts)
                    results.append({"source": b["ats"], "board": b["slug"], **counts})
            STATUS["done"] += 1
            STATUS["results"] = results
    postprocess(conn, cfg, touched)
    db.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.execute("PRAGMA optimize")  # keep the query planner's statistics fresh
    conn.close()
    return results


def postprocess(conn, cfg=None, company_keys=None) -> None:
    """Cluster, extract, score and filter. company_keys=None means everything."""
    cfg = cfg or config_mod.load()
    s = cfg.settings
    all_keys = [r[0] for r in conn.execute("SELECT DISTINCT company_key FROM jobs")]
    keys = all_keys if company_keys is None else sorted(set(company_keys))
    precompute(conn, None if company_keys is None else keys)  # signatures + rule extraction, all cores
    for ck in keys:
        dedupe.recompute(conn, [ck], s.repost_gap_days)
        extract.ensure_rules(conn, [ck])   # no-op after precompute; kept as a safety net
        extract.derive(conn, [ck])
    # Age-based signals drift by the day: rescore everything once a day, otherwise only what changed.
    today = datetime.now(timezone.utc).date().isoformat()
    if company_keys is None or db.get_meta(conn, "ghost_full_rescore") != today:
        ghost.rescore(conn, all_keys, s.ghost_age_days)
        db.set_meta(conn, "ghost_full_rescore", today)
    else:
        ghost.rescore(conn, keys, s.ghost_age_days)
    profile.recompute(conn, None, s.yoe_slack_years)
    sponsors.refresh_matches(conn, keys)
    _prune(conn, s.inactive_content_retention_days)
    db.bump_generation(conn)


def _prune(conn, days: int) -> None:
    """Drop stored descriptions of long-closed postings (history and signatures are kept)."""
    if days <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    uids = [r[0] for r in conn.execute(
        "SELECT j.uid FROM jobs j JOIN job_content c ON c.uid = j.uid WHERE j.active=0 AND j.closed_at < ?", (cutoff,))]
    with conn:
        for u in uids:
            conn.execute("DELETE FROM job_content WHERE uid=?", (u,))
            if db.has_fts(conn):
                db.fts_delete(conn, u)


def refresh_derived(conn, cfg=None) -> None:
    """Re-derive filter fields and eligibility after new extractions arrive (no re-clustering)."""
    cfg = cfg or config_mod.load()
    keys = [r[0] for r in conn.execute("SELECT DISTINCT company_key FROM jobs")]
    extract.derive(conn, keys)
    profile.recompute(conn, None, cfg.settings.yoe_slack_years)


def needs_reprocess(conn) -> bool:
    """True after an upgrade: rows without a cluster, filter fields not yet derived, or rule-based
    extractions from an older rules version. Startup then post-processes once before serving."""
    if conn.execute("SELECT 1 FROM jobs WHERE cluster_id IS NULL LIMIT 1").fetchone():
        return True
    if conn.execute("SELECT 1 FROM jobs WHERE active = 1 AND derived_of IS NULL LIMIT 1").fetchone():
        return True
    return conn.execute("SELECT 1 FROM jobs j JOIN job_content c ON c.uid = j.uid "
                        "JOIN extractions e ON e.content_hash = j.content_hash WHERE j.active = 1 "
                        "AND e.method = 'rules' AND COALESCE(e.model, '') <> ? LIMIT 1",
                        (workers.RULES_VERSION,)).fetchone() is not None


def precompute(conn, company_keys=None) -> int:
    """Compute MinHash signatures and rule-based extractions for every description that needs
    them, streamed in batches through a process pool. Returns how many descriptions were processed."""
    import json
    from .models import utcnow
    scope, args = "", []
    if company_keys is not None:
        keys = list(company_keys)
        if not keys:
            return 0
        scope = f" AND j.company_key IN ({','.join('?' * len(keys))})"
        args = keys
    need = {}
    for uid, chash in conn.execute(
            "SELECT j.uid, j.content_hash FROM jobs j JOIN job_content c ON c.uid = j.uid "
            "WHERE (j.minhash_of IS NULL OR j.minhash_of <> j.content_hash)" + scope, args):
        need.setdefault(chash, {"uids": [], "sig": False, "rules": False, "uid": uid})
        need[chash]["uids"].append(uid)
        need[chash]["sig"] = True
    for uid, chash in conn.execute(
            "SELECT j.uid, j.content_hash FROM jobs j JOIN job_content c ON c.uid = j.uid "
            "LEFT JOIN extractions e ON e.content_hash = j.content_hash "
            "WHERE j.active = 1 AND (e.content_hash IS NULL OR (e.method = 'rules' AND COALESCE(e.model, '') <> ?))"
            + scope, [workers.RULES_VERSION, *args]):
        need.setdefault(chash, {"uids": [], "sig": False, "rules": False, "uid": uid})["rules"] = True
    hashes = list(need)
    for i in range(0, len(hashes), workers.BATCH):
        batch = hashes[i:i + workers.BATCH]
        items = []
        for chash in batch:
            n = need[chash]
            r = conn.execute("SELECT j.title, j.location, unz(c.html) FROM jobs j JOIN job_content c ON c.uid = j.uid "
                             "WHERE j.uid = ?", (n["uid"],)).fetchone()
            if r:
                items.append((chash, r[2], r[0], r[1], n["sig"], n["rules"]))
        with conn:
            for chash, sig, data in workers.run(items):
                if need[chash]["sig"]:
                    conn.executemany("UPDATE jobs SET minhash=?, minhash_of=? WHERE uid=?",
                                     [(sig, chash, u) for u in need[chash]["uids"]])
                if data is not None:
                    conn.execute("INSERT OR REPLACE INTO extractions(content_hash, method, data, model, at) "
                                 "SELECT ?, 'rules', ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM extractions "
                                 "WHERE content_hash = ? AND method = 'claude')",
                                 (chash, json.dumps(data, ensure_ascii=False), workers.RULES_VERSION, utcnow(), chash))
    return len(hashes)
