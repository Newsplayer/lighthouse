"""Near-duplicate clustering, repost history, and the ghost score built on them (stdlib only).

Two postings at the same company are the same role when
  - company + title + location match exactly, or
  - their titles mostly agree AND their descriptions are near-identical
    (MinHash estimate of word-5-gram Jaccard >= SIM_THRESHOLD).

Scaling: each description is hashed once into a 64-value one-permutation MinHash
signature (one pass over its shingles; stored,
keyed by content hash, so it's never recomputed for unchanged text). Candidates come
from LSH banding (8 bands x 8 rows), so work per company grows ~linearly with postings
rather than with every pair. Only companies touched by a fetch are recomputed.

Per cluster we store: the earliest date anyone (ATS or Lighthouse) saw it, how many times
it was reposted (a near-identical posting appearing >= repost_gap_days after the previous
one, or the same posting coming back after it closed), and one canonical active row that
the job list shows.
"""

from __future__ import annotations

import json
import re
import zlib
from array import array
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .models import norm, strings
from .sanitize import html_to_text

NUM_PERM = 64
BANDS, ROWS = 8, 8
SHINGLE = 5
SIM_THRESHOLD = 0.80
TITLE_THRESHOLD = 0.6
_M64 = (1 << 64) - 1
_EMPTY = _M64
_WORD = re.compile(r"[a-z0-9]+")


def shingles(text: str) -> set[int]:
    words = _WORD.findall(text.lower())
    if len(words) < SHINGLE:
        return {zlib.crc32(" ".join(words).encode())} if words else set()
    return {zlib.crc32(" ".join(words[i:i + SHINGLE]).encode()) for i in range(len(words) - SHINGLE + 1)}


def _mix(x: int) -> int:
    """splitmix64 finalizer: spreads 32-bit shingle hashes over 64 bits."""
    z = (x + 0x9E3779B97F4A7C15) & _M64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
    return z ^ (z >> 31)


def minhash(text: str) -> bytes | None:
    """One-permutation MinHash with rotation densification (Shrivastava & Li, 2014):
    one hash per shingle, binned into NUM_PERM buckets, min kept per bucket."""
    sh = shingles(text)
    if len(sh) < 3:
        return None
    bins = [_EMPTY] * NUM_PERM
    for x in sh:
        z = _mix(x)
        b, v = z % NUM_PERM, z // NUM_PERM
        if v < bins[b]:
            bins[b] = v
    filled = [i for i, v in enumerate(bins) if v != _EMPTY]
    for i in range(NUM_PERM):
        if bins[i] == _EMPTY:
            j = next((k for k in filled if k > i), filled[0])
            bins[i] = (bins[j] + ((j - i) % NUM_PERM) * 0x5BD1E995) & _M64
    return array("Q", bins).tobytes()


def similarity(sig_a: bytes, sig_b: bytes) -> float:
    a, b = array("Q"), array("Q")
    a.frombytes(sig_a)
    b.frombytes(sig_b)
    return sum(1 for x, y in zip(a, b) if x == y) / NUM_PERM


def _bands(sig: bytes) -> list[bytes]:
    step = ROWS * 8
    return [bytes([i]) + sig[i * step:(i + 1) * step] for i in range(BANDS)]


def title_similarity(a: str, b: str) -> float:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


SEEDED = "seeded"     # jobs.minhash_of of a posting that came from the public database and has no text yet


class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _start(r) -> str:
    vals = [v for v in (r["posted_at"], r["first_seen"]) if v]
    return min(vals) if vals else ""


def refresh_signatures(conn, company_key: str) -> None:
    rows = conn.execute(
        "SELECT j.uid, unz(c.html) AS content_html, j.content_hash FROM jobs j JOIN job_content c ON c.uid = j.uid "
        "WHERE j.company_key=? AND length(c.html) > 0 "
        "AND (j.minhash_of IS NULL OR j.minhash_of <> j.content_hash)", (company_key,)).fetchall()
    for r in rows:
        conn.execute("UPDATE jobs SET minhash=?, minhash_of=? WHERE uid=?",
                     (minhash(html_to_text(r["content_html"])), r["content_hash"], r["uid"]))


def recompute(conn, company_keys, repost_gap_days: int = 7) -> None:
    """Re-cluster the given companies (iterable of company_key) and store cluster fields."""
    gap = timedelta(days=repost_gap_days)
    for ck in company_keys:
        with conn:
            refresh_signatures(conn, ck)
            rows = conn.execute(
                "SELECT uid, fingerprint, title, location, locations, minhash, minhash_of, posted_at, first_seen, active, "
                "closed_at, reopened_count, cluster_id, is_canonical, cluster_count, cluster_locations, "
                "effective_first_seen, repost_count FROM jobs WHERE company_key=?", (ck,)).fetchall()
            if not rows:
                continue
            uf = _UF()
            by_fp = defaultdict(list)
            # Postings with the same title words and the same text signature always match each other, and
            # match (or don't match) any other posting together. So they're joined at once and one of them
            # stands for all: a chain's 18,000 store postings of a dozen roles are compared as a dozen,
            # not pair by pair (that was 400 million checks a build).
            groups: dict[tuple, str] = {}
            info: dict[str, tuple] = {}
            buckets = defaultdict(list)
            for r in rows:
                uid = r["uid"]
                uf.find(uid)
                by_fp[r["fingerprint"]].append(uid)
                if r["minhash"]:
                    words = frozenset(norm(r["title"]).split())
                    if not words:
                        continue                      # no title words: never similar to anything by title
                    lead = groups.setdefault((words, r["minhash"]), uid)
                    if lead != uid:
                        uf.union(lead, uid)
                        continue
                    info[uid] = (words, r["minhash"])
                    for band in _bands(r["minhash"]):
                        buckets[band].append(uid)
            for members in by_fp.values():
                for u in members[1:]:
                    uf.union(members[0], u)
            # A builder seeded from the public database has no descriptions yet, so nothing to compare:
            # a role stays as it was published until the text of every one of its listings is read again.
            published = defaultdict(list)
            for r in rows:
                if r["cluster_id"]:
                    published[r["cluster_id"]].append(r)
            for members in published.values():
                if any(m["minhash_of"] == SEEDED for m in members):
                    for m in members[1:]:
                        uf.union(members[0]["uid"], m["uid"])
            checked = set()
            for members in buckets.values():
                if len(members) < 2:
                    continue
                for i, a in enumerate(members):
                    wa, ma = info[a]
                    for b in members[i + 1:]:
                        key = (a, b) if a < b else (b, a)
                        if key in checked:
                            continue
                        checked.add(key)
                        if uf.find(a) == uf.find(b):
                            continue
                        wb, mb = info[b]
                        if len(wa & wb) / len(wa | wb) >= TITLE_THRESHOLD and similarity(ma, mb) >= SIM_THRESHOLD:
                            uf.union(a, b)

            clusters = defaultdict(list)
            for r in rows:
                clusters[uf.find(r["uid"])].append(r)
            for members in clusters.values():
                members.sort(key=lambda r: (_start(r), r["uid"]))
                cid = members[0]["uid"]
                eff = _start(members[0])
                reposts = _count_reposts(members, gap)
                active = [r for r in members if r["active"]]
                canonical = active[0]["uid"] if active else None
                locs = []
                for r in active:
                    for l in strings(r["locations"]) or ([r["location"]] if r["location"] else []):
                        if l and l not in locs:
                            locs.append(l)
                locs_json = json.dumps(locs)
                for r in members:
                    # The role's count and places live on the listing that represents it (every list and
                    # filter reads canonical rows only). The other listings keep just their own location
                    # (cluster_locations NULL), so one opening or closing rewrites one row, not the whole
                    # cluster: a 2,000-store role used to store 2,000 copies of a 2,000-place list.
                    lead = r["uid"] == canonical
                    new = (cid, int(lead), len(active) if lead else 1, locs_json if lead else None, eff, reposts)
                    old = (r["cluster_id"], r["is_canonical"], r["cluster_count"], r["cluster_locations"],
                           r["effective_first_seen"], r["repost_count"])
                    if new != old:
                        conn.execute(
                            "UPDATE jobs SET cluster_id=?, is_canonical=?, cluster_count=?, cluster_locations=?, "
                            "effective_first_seen=?, repost_count=? WHERE uid=?", (*new, r["uid"]))


def _count_reposts(members, gap: timedelta) -> int:
    """A member is a repost when it appears >= gap after an earlier member in the same
    location, or after an earlier member had already closed. Opening the same role in a
    new city is expansion, not a repost. A posting that closed and came back counts too."""
    n = sum(r["reopened_count"] or 0 for r in members)
    seen_at: dict[str, datetime] = {}
    closed: list[datetime] = []
    for r in members:  # sorted by start
        start = _parse(_start(r))
        if start is None:
            continue
        loc = norm(r["location"] or "")
        if (loc in seen_at and start - seen_at[loc] >= gap) or any(c <= start for c in closed):
            n += 1
        seen_at[loc] = start
        if r["closed_at"]:
            c = _parse(r["closed_at"])
            if c:
                closed.append(c)
    return n


def _parse(s: str):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


# ================================================================ ghost score
# Count independent signals, each with a plain-language reason. A single signal never flags a posting
# (plenty of real roles stay open 60+ days): ghost_flag_signals (2) shows a "possible ghost" warning,
# ghost_hide_signals (3) hides it by default. Signals use the history above, so a reposted role keeps
# its original date instead of looking fresh.

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
