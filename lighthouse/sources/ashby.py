"""Ashby public job board API — free, no key, includes compensation.
https://developers.ashbyhq.com/docs/public-job-posting-api
"""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source, interval, num, workplace

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


class AshbySource(Source):
    name = "ashby"
    label = "Ashby"
    url_patterns = (
        r"jobs\.ashbyhq\.com/(?P<slug>[\w.%-]+)",
        r"api\.ashbyhq\.com/posting-api/job-board/(?P<slug>[\w.%-]+)",
    )

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(API.format(slug=slug), board=True)
        company, out = self.company_name(slug), []
        for raw in data.get("jobs", []):
            if not raw.get("isListed", True):
                continue
            uid = f"ashby:{slug}:{raw['id']}"
            locs = [raw.get("location")] + [s.get("location") for s in raw.get("secondaryLocations") or []]
            if not self.keep(uid, raw.get("title"), locs):
                continue
            same, marker = self.unchanged(uid, raw.get("descriptionHtml"))
            job = self.parse(slug, raw, company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str, skip_body: bool = False) -> Job:
        comp = raw.get("compensation") or {}
        salary = None
        for tier in comp.get("compensationTiers") or []:
            for c in tier.get("components") or []:
                if c.get("compensationType") == "Salary" and (c.get("minValue") or c.get("maxValue")):
                    salary = c
                    break
            if salary:
                break
        salary = salary or {}
        locs = [raw.get("location") or ""] + [s.get("location") or "" for s in raw.get("secondaryLocations") or []]
        wp = workplace(text=raw.get("workplaceType")) or ("remote" if raw.get("isRemote") else None)
        return Job(
            source="ashby",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("title") or "").strip(),
            company=company,
            location=(raw.get("location") or "").strip(),
            url=raw.get("jobUrl") or raw.get("applyUrl") or "",
            content_html="" if skip_body else sanitize_html(raw.get("descriptionHtml") or ""),
            departments=[d for d in (raw.get("department"), raw.get("team")) if d],
            posted_at=parse_dt(raw.get("publishedAt")),
            workplace=wp,
            employment_type=raw.get("employmentType"),
            salary_min=num(salary.get("minValue")),
            salary_max=num(salary.get("maxValue")),
            salary_currency=salary.get("currencyCode"),
            salary_interval=interval(salary.get("interval")),
            locations=[l.strip() for l in locs if l.strip()],
        )
