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


class Unchanged(Exception):
    """The board's response is byte-identical to last run: nothing to parse or write."""


class Source:
    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    # Regexes with a named group `slug`, matched against "host/path".
    url_patterns: ClassVar[tuple[str, ...]] = ()
    reserved_slugs: ClassVar[frozenset[str]] = frozenset({"api", "j", "embed", "v0", "v1", "jobs", "careers", "o"})

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

    def unchanged(self, uid: str, *parts) -> tuple[bool, str]:
        """Hash the raw description; if it matches what's stored, skip sanitizing it again."""
        marker = hashlib.blake2b("\x1f".join(p or "" for p in parts).encode(), digest_size=12).hexdigest()
        return self.known.get(uid) == (marker, True), marker

    # -- override these -------------------------------------------------
    def fetch(self, slug: str, known: dict[str, tuple[str, bool]] | None = None) -> list[Job]:
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------
    def company_name(self, slug: str, from_payload: str | None = None) -> str:
        return self.company or from_payload or slug.replace("-", " ").replace("_", " ").title()

    def _get_json(self, url: str, board: bool = False, **kw):
        r = self.client.get(url, **kw)
        if r.status_code == 404:
            raise BoardNotFound(f"{self.label}: board not found")
        r.raise_for_status()
        if board:  # whole-board response: skip everything if it's identical to last time
            self.body_hash = hashlib.blake2b(r.content, digest_size=16).hexdigest()
            if self.prev_body_hash and self.prev_body_hash == self.body_hash:
                raise Unchanged()
        return r.json()

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
