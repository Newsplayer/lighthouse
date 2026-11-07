"""Lever public postings API — free, no key. https://github.com/lever/postings-api"""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source, interval, num, workplace

API = "https://api.lever.co/v0/postings/{slug}?mode=json"


class LeverSource(Source):
    name = "lever"
    label = "Lever"
    single_response = True
    shared_host = True
    url_patterns = (
        r"jobs\.lever\.co/(?P<slug>[\w.-]+)",
        r"api\.lever\.co/v0/postings/(?P<slug>[\w.-]+)",
    )

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(API.format(slug=slug), board=True)
        if isinstance(data, dict):  # {"ok": false, "error": "Document not found"}
            from .base import BoardNotFound
            raise BoardNotFound(data.get("error") or "Lever: board not found")
        company, out = self.company_name(slug), []
        for raw in data:
            uid = f"lever:{slug}:{raw['id']}"
            cats = raw.get("categories") or {}
            if not self.keep(uid, raw.get("text"), cats.get("allLocations") or [cats.get("location")]):
                continue
            parts = self._parts(raw)
            same, marker = self.unchanged(uid, *parts)
            job = self.parse(slug, raw, company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def _parts(raw: dict) -> list[str]:
        parts = [raw.get("description") or ""]
        for lst in raw.get("lists") or []:
            parts.append(f"<h3>{lst.get('text') or ''}</h3><ul>{lst.get('content') or ''}</ul>")
        parts.append(raw.get("additional") or "")
        return parts

    @staticmethod
    def parse(slug: str, raw: dict, company: str, skip_body: bool = False) -> Job:
        cats = raw.get("categories") or {}
        parts = LeverSource._parts(raw)
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
            content_html="" if skip_body else sanitize_html("".join(parts)),
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
