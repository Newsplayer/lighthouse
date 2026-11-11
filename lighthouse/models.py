from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class Job:
    """One posting, normalized across sources."""

    source: str              # e.g. "greenhouse"
    source_board: str        # e.g. "stripe" (board token / company slug)
    external_id: str         # the source's own id for the posting
    title: str
    company: str
    location: str
    url: str                 # link to apply / original posting
    content_html: str = ""   # sanitized copy of the posting body ("" = not fetched yet)
    departments: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    # Structured fields some ATSs return directly (None = the source didn't say).
    workplace: str | None = None          # "remote" | "hybrid" | "onsite"
    employment_type: str | None = None    # free text from the source, e.g. "Full-time"
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_interval: str | None = None    # "year" | "month" | "hour" ...
    locations: list[str] = field(default_factory=list)   # all locations, if the ATS lists several
    detail_marker: str = ""   # changes when the source's copy changes (used to skip detail calls)
    partial: bool = False     # summary-only listing (detail not fetched this run): keep stored date/locations
    expires_at: datetime | None = None    # the posting's own closing date; past it, the posting is closed

    @property
    def uid(self) -> str:
        """Stable per-source key: same posting re-fetched => same uid."""
        return f"{self.source}:{self.source_board}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Exact cross-source key: same company + title + location => same fingerprint."""
        return "|".join(norm(x) for x in (company_key(self.company), self.title, self.location))

    @property
    def content_hash(self) -> str:
        return content_hash(self.content_html)


_WS = re.compile(r"[^a-z0-9]+")
_CORP_SUFFIX = re.compile(
    r"\b(the|inc|incorporated|llc|l l c|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|sa|"
    r"s a|bv|b v|nv|pbc|lp|llp|holdings?|group)\b")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return _WS.sub(" ", text.lower()).strip()


def company_key(name: str) -> str:
    """'Stripe, Inc.' and 'stripe' => 'stripe'. Used for dedupe and sponsor matching."""
    key = _CORP_SUFFIX.sub(" ", norm(name))
    return " ".join(key.split()) or norm(name)


def content_hash(html: str) -> str:
    return hashlib.blake2b((html or "").encode(), digest_size=16).hexdigest()


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    dt = to_utc(dt)
    return dt.isoformat(timespec="seconds") if dt else None


def parse_dt(value) -> datetime | None:
    """ISO strings, 'YYYY-MM-DD', 'YYYY-MM-DD HH:MM:SS UTC', or epoch millis."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        s = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        try:
            return to_utc(datetime.fromisoformat(s))
        except ValueError:
            for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%m/%d/%Y"):   # "October 1, 2026"
                try:
                    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            return None
    except (ValueError, OverflowError, OSError):
        return None


def parse_expiry(value) -> datetime | None:
    """A posting's closing date (schema.org validThrough). A bare date means the end of that day."""
    dt = parse_dt(value)
    if dt and isinstance(value, str) and len(value.strip()) <= 10:
        dt += timedelta(days=1)
    return dt


def age_hours(iso_time: str | None) -> float:
    """Hours since an ISO time (never, or unreadable: infinity)."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_time)).total_seconds() / 3600
    except (TypeError, ValueError):
        return float("inf")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
