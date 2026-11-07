"""Builder only (the job-index GitHub Action): Claude reads the postings the free rules couldn't settle.

Every new description goes through the rules first (extract.rules). If the posting clearly talks about a
filter field but no rule could read an answer from it (extract.unresolved: it mentions "sponsorship" but
in words no pattern knows, it states years in an odd way, it names a pay band we can't parse...), the
description is queued for Claude. The answer only fills those gaps: a rule answer is never replaced, and
filled fields are marked "by": "claude" so the job page can say so. Postings that simply never mention a
field cost nothing, and each description is read at most once (cached by its hash, kept across reposts).

Cost control (the maintainer's key, so it's strict):
- Runs only when ANTHROPIC_API_KEY is set (a repository secret); otherwise the build is rules-only.
- Message Batches API (half price); newest open, de-duplicated, non-ghost postings first.
- A monthly cap ([index] llm_monthly_cap_usd): each request is estimated before sending, open batches
  reserve their estimate, and the month's spend is carried in the published manifest so a lost Actions
  cache can't reset it.
- A failure here never fails the build: the index is published with the rule answers.

Postings are untrusted input: no tools, a strict JSON schema, values outside the known vocabularies dropped.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import claude_api, extract
from .models import utcnow

log = logging.getLogger("lighthouse.llm_fill")

PURPOSE = "index_fill"
CHUNK = 1000            # requests per batch (keeps each upload around 15 MB)
POLL_SECONDS = 30


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def disabled_reason(cfg) -> str | None:
    if not cfg.index.llm_fill:
        return "turned off ([index] llm_fill = false)"
    if not claude_api.get_key():
        return "no ANTHROPIC_API_KEY (rules only)"
    return None


def candidates(conn, limit: int, ghost_hide_at: int) -> list:
    """Open canonical postings whose rules left a field unsettled, not yet asked or in flight, newest first."""
    return conn.execute(
        """SELECT j.uid, j.content_hash, unz(c.html) AS content_html, j.title, j.company, j.location, e.unresolved
           FROM jobs j
           JOIN extractions e ON e.content_hash = j.content_hash
           JOIN job_content c ON c.uid = j.uid
           WHERE j.active = 1 AND j.is_canonical = 1 AND j.ghost_score < ? AND length(c.html) > 0
             AND e.unresolved IS NOT NULL AND e.unresolved <> ''
             AND NOT EXISTS (SELECT 1 FROM llm_extractions l WHERE l.content_hash = j.content_hash)
             AND NOT EXISTS (SELECT 1 FROM llm_pending p WHERE p.content_hash = j.content_hash)
           GROUP BY j.content_hash ORDER BY MAX(j.effective_first_seen) DESC LIMIT ?""",
        (ghost_hide_at, limit)).fetchall()


def store(conn, content_hash: str, out: dict | None, model: str) -> None:
    """Keep Claude's answer (None: asked, nothing usable). Merged into extractions by apply()."""
    data = claude_api.to_fields(out) if out else {}
    data = {k: v for k, v in data.items() if v.get("value") not in (None, [], "")}
    conn.execute("INSERT OR REPLACE INTO llm_extractions(content_hash, data, model, at) VALUES (?,?,?,?)",
                 (content_hash, json.dumps(data, ensure_ascii=False), model, _stamp()))


def apply(conn) -> int:
    """Merge Claude answers that arrived since an extraction was last written into it, then re-derive
    the filter columns of the affected postings. Returns descriptions updated."""
    rows = conn.execute("SELECT e.content_hash, e.data, l.data AS llm FROM extractions e "
                        "JOIN llm_extractions l ON l.content_hash = e.content_hash WHERE e.at < l.at").fetchall()
    if not rows:
        return 0
    stamp = _stamp()
    with conn:
        for r in rows:
            merged = extract.merge_llm(json.loads(r["data"]), json.loads(r["llm"]))
            conn.execute("UPDATE extractions SET data = ?, at = ? WHERE content_hash = ?",
                         (json.dumps(merged, ensure_ascii=False), stamp, r["content_hash"]))
    hashes = [r["content_hash"] for r in rows]
    keys = set()
    for i in range(0, len(hashes), 500):
        part = hashes[i:i + 500]
        keys |= {k[0] for k in conn.execute(f"SELECT DISTINCT company_key FROM jobs WHERE content_hash IN "
                                            f"({','.join('?' * len(part))})", part)}
    extract.derive(conn, sorted(keys))
    return len(rows)


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def summary(conn) -> dict:
    """For the manifest: this month's spend (carried across a lost builder cache) and progress."""
    spent = claude_api.month_spend(conn)
    asked = conn.execute("SELECT COUNT(*) FROM llm_extractions").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM llm_pending").fetchone()[0]
    return {"month": _month(), "spent_usd": round(spent, 4), "descriptions_read": asked, "pending": pending}


def carry_spend(conn, published: dict | None) -> None:
    """A builder seeded from the published index starts with no usage records: take the month's spend
    from the published manifest, so the cap still holds."""
    prev = (published or {}).get("llm") or {}
    if prev.get("month") != _month():
        return
    missing = float(prev.get("spent_usd") or 0) - claude_api.month_spend(conn)
    if missing > 0.0001:
        conn.execute("INSERT INTO api_usage(at, purpose, model, input_tokens, output_tokens, cost_usd, batch) "
                     "VALUES (?, ?, 'carried over', 0, 0, ?, 1)", (utcnow(), PURPOSE, missing))
        conn.commit()


def _poll(conn, cfg, stats: dict) -> bool:
    try:
        stats["batches_finished"] += claude_api.poll_batches(conn, cfg)
        return True
    except Exception as exc:   # network trouble: the batch is still there next run
        stats["error"] = f"poll: {exc}"
        log.warning("gap fill: couldn't collect batches: %s", exc)
        return False


def run(conn, cfg, cap_usd: float | None = None, wait_minutes: float | None = None,
        max_rows: int | None = None) -> dict:
    """Collect finished batches, send what's new (within budget), optionally wait for it, merge."""
    stats = {"sent": 0, "merged": 0, "batches_finished": 0, "estimate_usd": 0.0}
    why = disabled_reason(cfg)
    if why:
        stats["skipped"] = why
        return stats
    ix = cfg.index
    cap = ix.llm_monthly_cap_usd if cap_usd is None else cap_usd
    wait = ix.llm_wait_minutes if wait_minutes is None else wait_minutes
    _poll(conn, cfg, stats)
    stats["merged"] += apply(conn)

    if ix.llm_model not in cfg.claude.prices:
        stats["skipped"] = f"no price for {ix.llm_model} under [claude.prices], so spend can't be capped"
        return stats
    budget = cap - claude_api.month_spend(conn) - claude_api.reserved(conn)
    rows = candidates(conn, max_rows or ix.llm_max_per_run, cfg.settings.ghost_hide_signals)
    picked, total = [], 0.0
    for r in rows:
        p = claude_api.extract_params(cfg, r, model=ix.llm_model)
        est = claude_api.estimate(cfg, p["model"], p, batch=True)
        if total + est > budget:
            stats["over_budget"] = len(rows) - len(picked)
            break
        picked.append((r, p, est))
        total += est
    for i in range(0, len(picked), CHUNK):
        part = picked[i:i + CHUNK]
        try:
            res = claude_api._post("/messages/batches", {"requests": [
                {"custom_id": r["content_hash"], "params": p} for r, p, _ in part]})
        except Exception as exc:
            stats["error"] = f"submit: {exc}"
            log.warning("gap fill: couldn't submit a batch: %s", exc)
            break
        est = sum(e for _, _, e in part)
        now = utcnow()
        with conn:
            conn.execute("INSERT INTO claude_batches(id, purpose, model, profile_hash, status, request_count, "
                         "reserved_usd, submitted_at) VALUES (?,?,?,?,?,?,?,?)",
                         (res["id"], PURPOSE, ix.llm_model, None, res.get("processing_status", "in_progress"),
                          len(part), est, now))
            conn.executemany("INSERT OR REPLACE INTO llm_pending(content_hash, batch_id, at) VALUES (?,?,?)",
                             [(r["content_hash"], res["id"], now) for r, _, _ in part])
        stats["sent"] += len(part)
        stats["estimate_usd"] += est
    stats["estimate_usd"] = round(stats["estimate_usd"], 4)

    deadline = time.monotonic() + max(0.0, wait) * 60
    failures = 0
    while failures < 3 and time.monotonic() < deadline and conn.execute(
            "SELECT 1 FROM claude_batches WHERE purpose = ? AND status <> 'ended' LIMIT 1", (PURPOSE,)).fetchone():
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        failures = 0 if _poll(conn, cfg, stats) else failures + 1
    stats["merged"] += apply(conn)
    stats["pending"] = conn.execute("SELECT COUNT(*) FROM llm_pending").fetchone()[0]
    log.info("gap fill: %s", stats)
    return stats
