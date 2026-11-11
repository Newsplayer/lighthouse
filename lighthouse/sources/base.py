"""Shared plumbing for ATS adapters.

An adapter only has to say how to list a board and how to turn one raw posting into a Job.
Adding a new ATS = one small subclass + one line in sources/__init__.py.
"""

from __future__ import annotations

import hashlib
import re
from typing import ClassVar

from ..models import Job
from ..net import Client, default_client


class SourceError(Exception):
    pass


class BoardNotFound(SourceError):
    pass


class Blocked(SourceError):
    """The site answered with a bot check, a block page, 403 or a run of rate-limit/edge errors instead
    of data. That is a request to stop: no retries this run, and the site's other boards wait too."""


class Unchanged(Exception):
    """The board's response is byte-identical to last run: nothing to parse or write."""


# Status codes that mean "stop asking" rather than "this board is broken": forbidden, rate limited
# (after the client's own backoff), and CDN-edge errors that bot protection answers with.
BLOCK_STATUS = frozenset({403, 429, 520, 521, 522, 523, 524, 525, 526, 530})


def json_body(r, label: str):
    """r.json(), but a page that isn't JSON (a bot check or block page served with 200) or a blocking
    status raises Blocked, with what came back, instead of a bare JSON error."""
    if r.status_code in BLOCK_STATUS:
        raise Blocked(f"{label}: HTTP {r.status_code}")
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        ctype = (r.headers.get("content-type") or "no content type").split(";")[0]
        raise Blocked(f"{label}: HTTP {r.status_code} {ctype} ({len(r.content)} bytes) instead of JSON") from None


def looks_blocked(exc: Exception) -> bool:
    """A failure that means the site is pushing back (stop for this run) rather than a broken board."""
    if isinstance(exc, Blocked):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in BLOCK_STATUS:
        return True
    text = str(exc)
    return "too many 5" in text or any(f"{code} Server Error" in text for code in BLOCK_STATUS if code >= 500)


class RunGate:
    """Shared by every board in one fetch run: a time limit for starting new work, and the groups of
    boards (one ATS host, or one Workday data center) that pushed back. After `strikes` block-like
    answers from a group, nothing more is sent to it this run; its boards are tried first next run."""

    def __init__(self, deadline: float | None = None, strikes: int = 3):
        import threading
        self.deadline = deadline          # time.monotonic() value; None = no limit
        self.strikes = strikes
        self._hits: dict[str, int] = {}
        self.blocked: dict[str, str] = {}  # group -> first reason
        self._lock = threading.Lock()

    def out_of_time(self) -> bool:
        import time
        return self.deadline is not None and time.monotonic() > self.deadline

    def is_blocked(self, group: str | None) -> bool:
        return bool(group) and group in self.blocked

    def strike(self, group: str | None, why: str) -> bool:
        """Record one block-like answer. True when the group is (now) blocked for this run."""
        if not group:
            return False
        with self._lock:
            self._hits[group] = self._hits.get(group, 0) + 1
            if self._hits[group] >= self.strikes and group not in self.blocked:
                self.blocked[group] = why
            return group in self.blocked


class Source:
    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    # Regexes with a named group `slug`, matched against "host/path".
    url_patterns: ClassVar[tuple[str, ...]] = ()
    reserved_slugs: ClassVar[frozenset[str]] = frozenset({"api", "j", "embed", "v0", "v1", "jobs", "careers", "o"})
    # A posting the board stops listing is closed after this many successful listings in a row without
    # it. Paged listings (most ATSs) can skip a posting when the order shifts between pages; boards that
    # answer in one response (single_response = True) are exact, so one miss is enough there.
    single_response: ClassVar[bool] = False
    # Every company's board is on one API host (Greenhouse, Lever...): pushing back there applies to all of them.
    shared_host: ClassVar[bool] = False

    def __init__(self, client: Client | None = None, max_detail_calls: int = 400,
                 company: str | None = None):
        self.client = client or default_client()
        self.max_detail_calls = max_detail_calls
        self.company = company  # display name override (some APIs don't return one)
        self.skip_title = None  # compiled regex: postings to drop before any per-posting detail call
        self.countries: frozenset = frozenset()  # keep only postings located here (empty = everywhere)
        self.known: dict = {}                    # uid -> (detail_marker, has_content) from the database
        self.prev_body_hash: str | None = None   # last run's response hash (single-request boards)
        self.body_hash: str | None = None
        self.dropped: set[str] = set()           # uids skipped by title/country filters this run
        self.gate: RunGate | None = None         # the run's time limit and blocked groups (fetch.py)
        self.slug: str = ""

    @property
    def close_after_misses(self) -> int:
        return 1 if self.single_response else 2

    @classmethod
    def group(cls, slug: str) -> str | None:
        """Boards that share one rate limit or bot protection (one API host). None = the board's own
        site, which pushing back affects only itself."""
        return cls.name if cls.shared_host else None

    def out_of_time(self) -> bool:
        """Stop starting per-posting description calls: the run's time is up, or this board's group
        pushed back. Postings left title-only are filled in on a later run."""
        g = self.gate
        return bool(g) and (g.out_of_time() or g.is_blocked(self.group(self.slug)))

    def pushed_back(self, exc: Exception) -> None:
        """A description call got a block-like answer: count it against the group."""
        if self.gate and looks_blocked(exc):
            self.gate.strike(self.group(self.slug) or f"{self.name}:{self.slug}", str(exc)[:200])

    def keep(self, uid: str, title: str, locations) -> bool:
        """Title and country filters, checked on raw fields before any parsing or detail call."""
        from ..geo import in_scope
        if (self.skip_title and self.skip_title.search(title or "")) or \
                (self.countries and not in_scope([l for l in locations if l], self.countries)):
            self.dropped.add(uid)
            return False
        return True

    def wanted(self, job: Job) -> bool:
        return self.keep(job.uid, job.title, job.locations or [job.location])

    def detail_todo(self, jobs: list[Job]) -> list[Job]:
        """Which of these postings (all still without a description) get their description call this
        run: postings never seen before first, then titles with a known job category, newest first.
        The rest wait for a later run."""
        from ..classify import job_function

        def key(j: Job):
            return (j.uid in self.known, job_function(j.title) is None, -(j.posted_at.timestamp() if j.posted_at else 0))
        return sorted(jobs, key=key)[: self.max_detail_calls]

    def unchanged(self, uid: str, *parts) -> tuple[bool, str]:
        """Hash the raw description; if it matches what's stored, skip sanitizing it again."""
        marker = hashlib.blake2b("\x1f".join(p or "" for p in parts).encode(), digest_size=12).hexdigest()
        return self.known.get(uid) == (marker, True), marker

    # -- override these -------------------------------------------------
    def fetch(self, slug: str, known: dict[str, tuple[str, bool]] | None = None) -> list[Job]:
        raise NotImplementedError

    def peek(self, slug: str) -> int | None:
        """How many openings the board has, from one cheap request (None = can't tell cheaply).
        Discovery uses it to confirm a board it inferred from a careers page."""
        return None

    # -- helpers ----------------------------------------------------------
    def company_name(self, slug: str, from_payload: str | None = None) -> str:
        return self.company or from_payload or slug.replace("-", " ").replace("_", " ").title()

    def _get_json(self, url: str, board: bool = False, **kw):
        r = self.client.get(url, **kw)
        if r.status_code == 404:
            raise BoardNotFound(f"{self.label}: board not found")
        if r.status_code in BLOCK_STATUS:
            raise Blocked(f"{self.label}: HTTP {r.status_code}")
        r.raise_for_status()
        if board:  # whole-board response: skip everything if it's identical to last time
            self.body_hash = hashlib.blake2b(r.content, digest_size=16).hexdigest()
            if self.prev_body_hash and self.prev_body_hash == self.body_hash:
                raise Unchanged()
        return json_body(r, self.label)

    @classmethod
    def detect(cls, url: str) -> str | None:
        s = re.sub(r"^[a-z]+://", "", url.strip(), flags=re.I)
        for pat in cls.url_patterns:
            m = re.match(pat, s, flags=re.I)
            if m and m.group("slug").lower() not in cls.reserved_slugs:
                return m.group("slug")
        return None


def interval(value: str | None) -> str | None:
    v = (value or "").lower()
    for key in ("year", "month", "week", "day", "hour"):
        if key in v:
            return key
    if v in ("annual", "annually", "yearly", "1 year"):
        return "year"
    return None


def workplace(remote=None, hybrid=None, text: str | None = None) -> str | None:
    t = (text or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    if t in ("remote", "fullyremote"):
        return "remote"
    if t == "hybrid":
        return "hybrid"
    if t in ("onsite", "inoffice", "office"):
        return "onsite"
    if remote is True:
        return "remote"
    if hybrid is True:
        return "hybrid"
    return None


def num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
