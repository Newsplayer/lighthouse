"""Ghost-job score: count independent signals, each with a plain-language reason.

A single signal never flags a posting (plenty of real roles stay open 60+ days).
  score >= ghost_flag_signals (2): shown with a "possible ghost" warning
  score >= ghost_hide_signals (3): hidden by default; one click shows them

Signals use Lighthouse's own history (dedupe.py), so a reposted role keeps its
original date instead of looking fresh.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone


EVERGREEN_HINTS = ("talent pool", "talent community", "general application", "future opportunities",
                   "open application", "spontaneous application", "general interest", "expression of interest")
PIPELINE_LANGUAGE = re.compile(
    r"(talent (pool|pipeline|community)|future (openings|opportunities|roles|needs)|"
    r"(not|isn.t) (currently|actively) (hiring|recruiting)|no (current|immediate) (opening|vacanc)|"
    r"always (looking|accepting|interested)|evergreen (role|posting|requisition)|"
    r"(keep|hold) your (resume|cv|application) on file|as (positions|roles) become available|"
    r"pipeline (role|requisition|posting))", re.I)


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).days


def signals(row, max_age_days: int, now: datetime | None = None, text: str | None = None,
            pipeline_phrase: str | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    reasons = []
    since = row["effective_first_seen"] or row["first_seen"]
    age = _days_since(since, now)
    if age is not None and age > max_age_days:
        reasons.append(f"open since {since[:10]}")   # a date, not a count: stored reasons don't change daily
    if (row["repost_count"] or 0) > 0:
        n = row["repost_count"]
        reasons.append(f"reposted {n}×" if n > 1 else "reposted")
    t = (row["title"] or "").lower()
    if any(h in t for h in EVERGREEN_HINTS):
        reasons.append("evergreen/pipeline title")
    if pipeline_phrase is None and text is not None:
        m = PIPELINE_LANGUAGE.search(text)
        pipeline_phrase = m.group(0) if m else None
    if pipeline_phrase:
        reasons.append(f"pipeline wording: “{pipeline_phrase}”")
    stale = _days_since(row["updated_at"], now)
    if stale is not None and stale > 2 * max_age_days:
        reasons.append(f"not updated since {row['updated_at'][:10]}")
    return reasons


_SINCE = re.compile(r"^(open|not updated) since (\d{4}-\d{2}-\d{2})$")


def label(reason: str, now: datetime | None = None) -> str:
    """Stored reasons carry dates ("open since 2026-08-26"); people read day counts ("open 71 days")."""
    m = _SINCE.match(reason or "")
    if not m:
        return reason
    days = _days_since(m.group(2), now or datetime.now(timezone.utc))
    if days is None:
        return reason
    return f"open {days} days" if m.group(1) == "open" else f"not updated in {days} days"


def labels(payload, now: datetime | None = None) -> list[str]:
    items = json.loads(payload) if isinstance(payload, str) else (payload or [])
    return [label(r, now) for r in items]


def rescore(conn, company_keys, max_age_days: int) -> None:
    now = datetime.now(timezone.utc)
    for ck in company_keys:
        with conn:
            rows = conn.execute(
                "SELECT j.uid, j.title, j.effective_first_seen, j.first_seen, j.repost_count, j.updated_at, "
                "j.ghost_reasons, j.ghost_score, e.data FROM jobs j "
                "LEFT JOIN extractions e ON e.content_hash = j.content_hash "
                "WHERE j.company_key=? AND j.active=1", (ck,)).fetchall()
            for r in rows:
                phrase = ((json.loads(r["data"]) if r["data"] else {}).get("pipeline") or {}).get("value")
                reasons = signals(r, max_age_days, now, pipeline_phrase=phrase)
                payload = json.dumps(reasons, ensure_ascii=False)
                if payload != r["ghost_reasons"] or len(reasons) != r["ghost_score"]:
                    conn.execute("UPDATE jobs SET ghost_reasons=?, ghost_score=? WHERE uid=?",
                                 (payload, len(reasons), r["uid"]))
