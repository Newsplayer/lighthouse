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
    single_response = True
    shared_host = True
    url_patterns = (
        r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?(?P<slug>[\w-]+)",
        r"(?:job-)?boards\.greenhouse\.io/embed/job_board\?for=(?P<slug>[\w-]+)",
    )

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(API.format(slug=slug), board=True)
        out = []
        for raw in data.get("jobs", []):
            uid = f"greenhouse:{slug}:{raw['id']}"
            if not self.keep(uid, raw.get("title"), [(raw.get("location") or {}).get("name")]):
                continue
            same, marker = self.unchanged(uid, raw.get("content"))
            job = self.parse(slug, raw, self.company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str | None = None, skip_body: bool = False) -> Job:
        # Greenhouse returns `content` HTML-escaped (&lt;p&gt;...), so unescape before sanitizing.
        body = "" if skip_body else sanitize_html(html.unescape(raw.get("content") or ""))
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
