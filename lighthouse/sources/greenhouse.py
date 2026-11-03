"""Greenhouse public Job Board API — free, no key. https://developers.greenhouse.io/job-board.html"""

from __future__ import annotations

import html

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


class GreenhouseSource(Source):
    name = "greenhouse"
    label = "Greenhouse"
    url_patterns = (
        r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?(?P<slug>[\w-]+)",
        r"(?:job-)?boards\.greenhouse\.io/embed/job_board\?for=(?P<slug>[\w-]+)",
    )

    def fetch(self, slug, known=None):
        data = self._get_json(API.format(slug=slug))
        return [self.parse(slug, raw, self.company) for raw in data.get("jobs", [])]

    @staticmethod
    def parse(slug: str, raw: dict, company: str | None = None) -> Job:
        # Greenhouse returns `content` HTML-escaped (&lt;p&gt;...), so unescape before sanitizing.
        body = sanitize_html(html.unescape(raw.get("content") or ""))
        return Job(
            source="greenhouse",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("title") or "").strip(),
            company=company or raw.get("company_name") or slug.title(),
            location=((raw.get("location") or {}).get("name") or "").strip(),
            url=raw.get("absolute_url") or "",
            content_html=body,
            departments=[d["name"] for d in raw.get("departments") or [] if d.get("name")],
            posted_at=parse_dt(raw.get("first_published")),
            updated_at=parse_dt(raw.get("updated_at")),
        )
