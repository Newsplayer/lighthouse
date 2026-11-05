"""Amazon (amazon.jobs) — the site's own public search JSON, no key.

The search returns the 10,000 most recent openings with descriptions and qualifications.
    slug = "amazon.jobs"
"""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source

PAGE = 100
MAX = 10000


class AmazonSource(Source):
    name = "amazon"
    label = "Amazon Jobs"

    @classmethod
    def detect(cls, url: str) -> str | None:
        return "amazon.jobs" if "amazon.jobs" in (url or "").lower() else None

    def peek(self, slug):
        r = self.client.get("https://www.amazon.jobs/en/search.json?result_limit=1&offset=0", headers={"Accept": "application/json"})
        r.raise_for_status()
        return int(r.json().get("hits") or 0)

    def fetch(self, slug, known=None):
        out, offset = [], 0
        while offset < MAX:
            r = self.client.get(f"https://www.amazon.jobs/en/search.json?result_limit={PAGE}&offset={offset}&sort=recent",
                                headers={"Accept": "application/json"})
            r.raise_for_status()
            batch = r.json().get("jobs") or []
            if not batch:
                break
            out += batch
            offset += len(batch)
        if not out:
            raise BoardNotFound("Amazon: no postings returned")
        jobs = []
        for d in out:
            jid = str(d.get("id_icims") or d.get("id") or "")
            if not jid:
                continue
            title = (d.get("title") or "").strip()
            loc = d.get("normalized_location") or d.get("location") or ", ".join(
                x for x in (d.get("city"), d.get("state"), d.get("country_code")) if x)
            uid = f"amazon:amazon.jobs:{jid}"
            if not self.keep(uid, title, [loc]):
                continue
            parts = [d.get("description")]
            for label, key in (("Basic qualifications", "basic_qualifications"),
                               ("Preferred qualifications", "preferred_qualifications")):
                if d.get(key):
                    parts.append(f"<h3>{label}</h3><p>{d[key]}</p>")
            body = "".join(p for p in parts if p)
            same, marker = self.unchanged(uid, body)
            jobs.append(Job(source="amazon", source_board="amazon.jobs", external_id=jid, title=title,
                            company=self.company or d.get("company_name") or "Amazon", location=loc or "",
                            url="https://www.amazon.jobs" + (d.get("job_path") or f"/en/jobs/{jid}"),
                            content_html="" if same else sanitize_html(body),
                            departments=[x for x in (d.get("job_category"), d.get("business_category")) if x],
                            posted_at=parse_dt(d.get("posted_date")), updated_at=parse_dt(d.get("updated_time")),
                            employment_type=d.get("job_schedule_type") or None, locations=[loc] if loc else [],
                            detail_marker=marker))
        return jobs
