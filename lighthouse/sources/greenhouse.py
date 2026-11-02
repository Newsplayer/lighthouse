"""Greenhouse public Job Board API — free, no API key.
Docs: https://developers.greenhouse.io/job-board.html
"""

from __future__ import annotations

import html
from datetime import datetime

import requests

from ..models import Job
from ..sanitize import sanitize_html

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseSource:
    name = "greenhouse"

    def __init__(self, timeout: float = 20, session: requests.Session | None = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "Lighthouse/0.1 (personal job aggregator)")

    def fetch(self, token: str) -> list[Job]:
        resp = self.session.get(API.format(token=token), timeout=self.timeout)
        resp.raise_for_status()
        return [self.parse(token, raw) for raw in resp.json().get("jobs", [])]

    @staticmethod
    def parse(token: str, raw: dict) -> Job:
        # Greenhouse returns `content` HTML-escaped (&lt;p&gt;...), so unescape before sanitizing.
        body = sanitize_html(html.unescape(raw.get("content") or ""))
        return Job(
            source="greenhouse",
            source_board=token,
            external_id=str(raw["id"]),
            title=(raw.get("title") or "").strip(),
            company=raw.get("company_name") or token.title(),
            location=((raw.get("location") or {}).get("name") or "").strip(),
            url=raw.get("absolute_url") or "",
            content_html=body,
            departments=[d["name"] for d in raw.get("departments") or [] if d.get("name")],
            posted_at=_dt(raw.get("first_published")),
            updated_at=_dt(raw.get("updated_at")),
        )


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
