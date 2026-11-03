"""Shared plumbing for ATS adapters.

An adapter only has to say how to list a board and how to turn one raw posting into a Job.
Adding a new ATS = one small subclass + one line in sources/__init__.py.
"""

from __future__ import annotations

import re
from typing import ClassVar

from ..models import Job
from ..net import Client, default_client


class SourceError(Exception):
    pass


class BoardNotFound(SourceError):
    pass


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

    # -- override these -------------------------------------------------
    def fetch(self, slug: str, known: dict[str, tuple[str, bool]] | None = None) -> list[Job]:
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------
    def company_name(self, slug: str, from_payload: str | None = None) -> str:
        return self.company or from_payload or slug.replace("-", " ").replace("_", " ").title()

    def _get_json(self, url: str, **kw):
        r = self.client.get(url, **kw)
        if r.status_code == 404:
            raise BoardNotFound(f"{self.label}: board not found")
        r.raise_for_status()
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
