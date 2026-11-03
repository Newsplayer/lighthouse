"""Pull every enabled board into the local database, then post-process.

Network calls run in a thread pool (fetch_workers) with a shared per-host throttle;
all database writes happen on one thread (SQLite has a single writer). After the
fetch, only companies whose postings changed are re-clustered and re-extracted.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import db, dedupe, extract, ghost, profile, sponsors
from .config import DEFAULT_CONFIG
from .net import default_client
from .sources import SOURCES

log = logging.getLogger("lighthouse.fetch")

_lock = threading.Lock()
STATUS: dict = {"running": False, "done": 0, "total": 0, "started": None, "finished": None, "results": []}


def _fetch_board(board: dict, known: dict, client, max_detail: int):
    cls = SOURCES[board["ats"]]
    src = cls(client=client, max_detail_calls=max_detail, company=board.get("company"))
    return src.fetch(board["slug"], known=known)


def run(config_path: Path | str = DEFAULT_CONFIG, db_path: Path | str = db.DEFAULT_DB,
        only: list[tuple[str, str]] | None = None, wait: bool = True) -> list[dict] | None:
    """Fetch all enabled boards (or just `only`). Returns per-board results, or None if a
    fetch is already running and wait=False."""
    if not _lock.acquire(blocking=wait):
        return None
    try:
        return _run(config_path, db_path, only)
    finally:
        STATUS["running"] = False
        STATUS["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _lock.release()


def _run(config_path, db_path, only):
    cfg = config_mod.load(config_path)
    s = cfg.settings
    conn = db.connect(db_path)
    db.sync_config_boards(conn, cfg.boards)
    boards = [dict(b) for b in db.list_boards(conn, enabled_only=True)
              if b["ats"] in SOURCES and (only is None or (b["ats"], b["slug"]) in only)]
    client = default_client(s.request_timeout_seconds, s.per_host_requests_per_second)
    STATUS.update(running=True, done=0, total=len(boards), results=[],
                  started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    results, touched = [], set()
    with ThreadPoolExecutor(max_workers=max(1, s.fetch_workers)) as pool:
        futures = {pool.submit(_fetch_board, b, db.known_postings(conn, b["ats"], b["slug"]), client,
                               s.max_detail_calls_per_board): b for b in boards}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                jobs = fut.result()
            except Exception as exc:  # one bad board shouldn't stop the run
                log.warning("%s/%s failed: %s", b["ats"], b["slug"], exc)
                db.board_result(conn, b["ats"], b["slug"], error=str(exc))
                results.append({"source": b["ats"], "board": b["slug"], "error": str(exc)})
            else:
                counts = db.upsert_board(conn, b["ats"], b["slug"], jobs)
                touched |= counts.pop("touched")
                counts.pop("changed")
                db.board_result(conn, b["ats"], b["slug"], count=len(jobs))
                log.info("%s/%s: %s", b["ats"], b["slug"], counts)
                results.append({"source": b["ats"], "board": b["slug"], **counts})
            STATUS["done"] += 1
            STATUS["results"] = results
    postprocess(conn, cfg, touched)
    db.set_meta(conn, "last_fetch", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.close()
    return results


def postprocess(conn, cfg=None, company_keys=None) -> None:
    """Cluster, extract, score and filter. company_keys=None means everything."""
    cfg = cfg or config_mod.load()
    s = cfg.settings
    all_keys = [r[0] for r in conn.execute("SELECT DISTINCT company_key FROM jobs")]
    keys = all_keys if company_keys is None else sorted(set(company_keys))
    for ck in keys:  # per company, so the cached HTML->text conversion is reused across steps
        dedupe.recompute(conn, [ck], s.repost_gap_days)
        extract.ensure_rules(conn, [ck])
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
    """True right after the v0.1 -> v0.2 migration (rows have no cluster yet)."""
    return conn.execute("SELECT 1 FROM jobs WHERE cluster_id IS NULL LIMIT 1").fetchone() is not None
