"""Pull every enabled board into the builder database, then post-process.

Network calls run in a thread pool (fetch_workers) with a shared per-host throttle;
all database writes happen on one thread (SQLite has a single writer). After the
fetch, only companies whose postings changed are re-clustered and re-extracted.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_mod
from . import db, dedupe, extract, ghost, workers
from .config import DEFAULT_CONFIG
from .models import age_hours, utcnow
from .net import default_client
from .sources import SOURCES, BoardNotFound
from .sources.base import RunGate, Unchanged, looks_blocked

log = logging.getLogger("lighthouse.fetch")

BOARD_RETRY_PAUSES = (60, 180)   # a rate-limited board is tried again after these pauses (seconds)


def _rate_limited(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) == 429 or bool(re.search(r"\b429\b", str(exc)))


DEFERRED = "deferred"            # _fetch_board: not started (time is up, or its group pushed back)


def board_group(board: dict) -> str:
    """Boards that share one rate limit or bot protection (a Workday data center, one API host); a
    board on its own site is its own group."""
    group = getattr(SOURCES[board["ats"]], "group", None)
    return (group(board["slug"]) if group else None) or f"{board['ats']}:{board['slug']}"


def _fetch_board(board: dict, known: dict, client, max_detail: int, skip_title=None, countries=frozenset(),
                 detail_stop: float | None = None, gate: RunGate | None = None):
    cls = SOURCES[board["ats"]]
    group = board_group(board)
    for pause in (*BOARD_RETRY_PAUSES, None):
        if gate and (gate.out_of_time() or gate.is_blocked(group)):
            return DEFERRED, None
        details = 0 if detail_stop is not None and time.monotonic() > detail_stop else max_detail
        src = cls(client=client, max_detail_calls=details, company=board.get("company"))
        src.skip_title, src.countries, src.known = skip_title, countries, known
        src.prev_body_hash = board.get("body_hash")
        src.gate, src.slug = gate, board["slug"]
        try:
            return src.fetch(board["slug"], known=known), src
        except Unchanged:
            return None, src
        except Exception as exc:
            # A bot check, 403 or a run of edge errors counts against the board's group. After a few,
            # the group is left alone for the rest of the run. Nothing is retried around a block.
            if gate and looks_blocked(exc):
                gate.strike(group, f"{board['ats']}/{board['slug']}: {exc}"[:300])
            if pause is None or not _rate_limited(exc) or (gate and gate.is_blocked(group)):
                raise
            log.info("%s/%s rate limited; trying again in %d s", board["ats"], board["slug"], pause)
            time.sleep(pause)


def order_boards(boards: list[dict], every_hours: float = 6) -> list[dict]:
    """Stalest first: boards not reached (or pushed back) last run lead, so under a time limit every
    board is refreshed in turn. Among equally stale boards the biggest go first, so long listings
    start early instead of being what the run waits for at the end."""
    step = max(every_hours, 1)

    def tier(b):   # how many runs ago it was last fetched OK (never: first of all)
        age = age_hours(b.get("last_ok"))
        return 99 if age == float("inf") else min(int(age // step), 98)
    return sorted(boards, key=lambda b: (-tier(b), -(b.get("job_count") or 0)))


def not_due(board: dict, s) -> bool:
    """A board that answers in one request (it has a response hash: Greenhouse, Lever, Ashby...) is
    read every run; an unchanged one costs that request and nothing else. A listing that takes many
    requests is read every paged_refresh_hours, and Workday every workday_refresh_hours (one request
    per 20 postings, and every company on a data center shares that data center's limit)."""
    hours = (s.workday_refresh_hours if board["ats"] == "workday"
             else 0 if board.get("body_hash") else s.paged_refresh_hours)
    return hours > 0 and not board.get("last_error") and age_hours(board.get("last_ok")) < hours


def run(config_path: Path | str = DEFAULT_CONFIG, db_path: Path | str = db.DEFAULT_DB,
        only: list[tuple[str, str]] | None = None, overrides: dict | None = None) -> list[dict]:
    """Fetch all enabled boards (or just `only`), then post-process what changed. Returns per-board results."""
    cfg = config_mod.load(config_path)
    s = cfg.settings
    for k, v in (overrides or {}).items():
        if v is not None and hasattr(s, k):
            setattr(s, k, v)
    conn = db.connect(db_path)
    db.sync_config_boards(conn, cfg.boards)
    client = default_client(s.request_timeout_seconds, s.per_host_requests_per_second)
    results, touched = [], set()
    boards = [dict(b) for b in db.list_boards(conn, enabled_only=True)
              if b["ats"] in SOURCES and (only is None or (b["ats"], b["slug"]) in only)]
    if only is None:
        boards = [b for b in boards if not not_due(b, s)]
    boards = order_boards(boards)
    hourly = re.compile("|".join(config_mod.HOURLY_TITLE_PATTERNS), re.I) if s.skip_hourly_titles else None
    countries = frozenset(s.countries)
    started = time.monotonic()
    detail_stop = started + s.detail_minutes * 60 if s.detail_minutes else None
    gate = RunGate(started + s.fetch_minutes * 60 if s.fetch_minutes else None)
    deferred = []
    with ThreadPoolExecutor(max_workers=max(1, s.fetch_workers)) as pool:
        futures = {pool.submit(_fetch_board, b, db.known_postings(conn, b["ats"], b["slug"]), client,
                               s.max_detail_calls_per_board, hourly, countries, detail_stop, gate): b for b in boards}
        for fut in as_completed(futures):
            b = futures[fut]
            try:
                jobs, src = fut.result()
            except Exception as exc:  # one bad board shouldn't stop the run
                why = str(exc) or type(exc).__name__
                if looks_blocked(exc):
                    why = f"Pushed back (bot check, block or rate limit); tried again next run. {why}"
                log.warning("%s/%s failed: %s", b["ats"], b["slug"], why)
                off = db.board_result(conn, b["ats"], b["slug"], error=why,
                                      not_found=isinstance(exc, BoardNotFound), disable_after=s.disable_after_failures)
                results.append({"source": b["ats"], "board": b["slug"], "error": why, "switched_off": off})
            else:
                if jobs == DEFERRED:   # not started: first in line next run; its postings stay as they are
                    deferred.append(b)
                    results.append({"source": b["ats"], "board": b["slug"], "deferred": True})
                elif jobs is None:  # byte-identical to last run
                    touched |= db.board_unchanged(conn, b["ats"], b["slug"]) or set()
                    results.append({"source": b["ats"], "board": b["slug"], "unchanged_board": True})
                else:
                    # Postings whose locations only became known from detail calls (Workday) get the country check now.
                    kept = [j for j in jobs if src.wanted(j)]
                    counts = db.upsert_board(conn, b["ats"], b["slug"], kept, keep_alive=src.dropped,
                                             hold_mass_closures=s.hold_mass_closures,
                                             close_after=getattr(src, "close_after_misses", 1))
                    counts["filtered_out"] = len(src.dropped)
                    touched |= counts.pop("touched")
                    counts.pop("changed")
                    db.board_result(conn, b["ats"], b["slug"], count=len(kept), body_hash=src.body_hash)
                    log.info("%s/%s: %s", b["ats"], b["slug"], counts)
                    results.append({"source": b["ats"], "board": b["slug"], **counts})
    for group, why in gate.blocked.items():
        n = sum(1 for b in deferred if board_group(b) == group)
        log.warning("%s pushed back (%s); %d more of its boards wait for the next run", group, why, n)
    late = sum(1 for b in deferred if not gate.is_blocked(board_group(b)))
    if late:
        log.info("time limit (%g min) reached: %d boards not started, first in line next run", s.fetch_minutes, late)
    log.info("fetched for %.1f min", (time.monotonic() - started) / 60)
    touched |= db.close_expired(conn)   # pages not re-read this run whose closing date has passed
    # After an upgrade (new rules, filter fields or schema), every company is reprocessed once, not just the
    # ones whose boards changed this run, so the next build carries the new fields everywhere.
    postprocess(conn, cfg, None if db.get_meta(conn, "processed_versions") != processed_versions() else touched)
    conn.execute("PRAGMA optimize")  # keep the query planner's statistics fresh
    conn.close()
    return results


def processed_versions() -> dict:
    """What a full postprocess brings every row up to (stored in meta after one)."""
    from .classify import DERIVE_VERSION
    return {"rules": workers.RULES_VERSION, "derive": DERIVE_VERSION, "schema": db.SCHEMA_VERSION}


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
    _prune(conn, s.inactive_content_retention_days)
    n = db.purge_closed(conn, s.closed_retention_days)
    if n:
        log.info("deleted %d postings that closed more than %d days ago", n, s.closed_retention_days)
    if company_keys is None:
        db.set_meta(conn, "processed_versions", processed_versions())


def _prune(conn, days: int) -> None:
    """Drop stored descriptions of long-closed postings (history and signatures are kept)."""
    if days <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    uids = [r[0] for r in conn.execute(
        "SELECT j.uid FROM jobs j JOIN job_content c ON c.uid = j.uid WHERE j.active=0 AND j.closed_at < ?", (cutoff,))]
    with conn:
        conn.executemany("DELETE FROM job_content WHERE uid=?", [(u,) for u in uids])


def precompute(conn, company_keys=None) -> int:
    """Compute MinHash signatures and rule-based extractions for every description that needs
    them, streamed in batches through a process pool. Returns how many descriptions were processed."""
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
            r = conn.execute("SELECT j.title, j.location, unz(c.html), j.departments, "
                             "j.salary_min IS NOT NULL OR j.salary_max IS NOT NULL, j.workplace "
                             "FROM jobs j JOIN job_content c ON c.uid = j.uid WHERE j.uid = ?", (n["uid"],)).fetchone()
            if r:
                items.append((chash, r[2], r[0], r[1], n["sig"], n["rules"],
                              {"departments": r[3] or "", "salary": bool(r[4]), "workplace": r[5]}))
        llm = extract.llm_for(conn, [h for h in batch if need[h]["rules"]])   # gap fills survive new rules
        with conn:
            for chash, sig, data, todo in workers.run(items):
                if need[chash]["sig"]:
                    conn.executemany("UPDATE jobs SET minhash=?, minhash_of=? WHERE uid=?",
                                     [(sig, chash, u) for u in need[chash]["uids"]])
                if data is not None:
                    conn.execute("INSERT OR REPLACE INTO extractions(content_hash, method, data, model, at, unresolved) "
                                 "VALUES (?, 'rules', ?, ?, ?, ?)",
                                 (chash, json.dumps(extract.merge_llm(data, llm.get(chash)), ensure_ascii=False),
                                  workers.RULES_VERSION, utcnow(), ",".join(todo or []) or None))
    return len(hashes)
