from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime


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
    content_html: str = ""   # sanitized copy of the posting body
    departments: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def uid(self) -> str:
        """Stable per-source key: same posting re-fetched => same uid."""
        return f"{self.source}:{self.source_board}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Cross-source key: the same role listed on two boards => same fingerprint."""
        return "|".join(_norm(x) for x in (self.company, self.title, self.location))


_WS = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return _WS.sub(" ", text.lower()).strip()
