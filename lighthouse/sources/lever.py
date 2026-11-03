"""Lever public postings API — free, no key. https://github.com/lever/postings-api"""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source, interval, num, workplace

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


class LeverSource(Source):
    name = "lever"
    label = "Lever"
    url_patterns = (
        r"jobs\.lever\.co/(?P<slug>[\w.-]+)",
        r"api\.lever\.co/v0/postings/(?P<slug>[\w.-]+)",
    )

    def fetch(self, slug, known=None):
        data = self._get_json(API.format(slug=slug))
        if isinstance(data, dict):  # {"ok": false, "error": "Document not found"}
            from .base import BoardNotFound
            raise BoardNotFound(data.get("error") or "Lever: board not found")
        return [self.parse(slug, raw, self.company_name(slug)) for raw in data]

    @staticmethod
    def parse(slug: str, raw: dict, company: str) -> Job:
        cats = raw.get("categories") or {}
        parts = [raw.get("description") or ""]
        for lst in raw.get("lists") or []:
            parts.append(f"<h3>{lst.get('text') or ''}</h3><ul>{lst.get('content') or ''}</ul>")
        parts.append(raw.get("additional") or "")
        sal = raw.get("salaryRange") or {}
        all_locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        return Job(
            source="lever",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("text") or "").strip(),
            company=company,
            location=(cats.get("location") or "").strip(),
            url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
            content_html=sanitize_html("".join(parts)),
            departments=[d for d in (cats.get("department"), cats.get("team")) if d],
            posted_at=parse_dt(raw.get("createdAt")),
            workplace=workplace(text=raw.get("workplaceType")),
            employment_type=cats.get("commitment"),
            salary_min=num(sal.get("min")),
            salary_max=num(sal.get("max")),
            salary_currency=sal.get("currency"),
            salary_interval=interval(sal.get("interval")),
            locations=[l for l in all_locs if l],
        )
