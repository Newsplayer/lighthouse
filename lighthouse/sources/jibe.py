"""Jibe career sites (iCIMS's careers front end) — the site's own public JSON API, no key.

Sites like jobs.statefarm.com and pepsicojobs.com serve /api/jobs, with full descriptions.

    slug = "<careers host>"
"""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, workplace

PAGE = 100


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


class JibeSource(Source):
    name = "jibe"
    label = "iCIMS (Jibe)"

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None   # recognised by discovery (the page loads Jibe; /api/jobs answers)

    def peek(self, slug):
        host = slug.strip().lower().strip("/")
        r = self.client.get(f"https://{host}/api/jobs?page=1&limit=1", headers={"Accept": "application/json"})
        r.raise_for_status()
        d = r.json()
        return int(d.get("totalCount") or d.get("count") or len(d.get("jobs") or []))

    def fetch(self, slug, known=None):
        host = slug.strip().lower().strip("/")
        out, page, total = [], 1, None
        while True:
            r = self.client.get(f"https://{host}/api/jobs?page={page}&limit={PAGE}&sortBy=posted_date&descending=true",
                                headers={"Accept": "application/json"})
            if r.status_code in (403, 404):
                raise BoardNotFound("Jibe: no jobs API on this site")
            r.raise_for_status()
            data = r.json()
            if total is None:
                total = int(data.get("totalCount") or data.get("count") or 0)
            batch = [j.get("data") or j for j in data.get("jobs") or []]
            out += batch
            if not batch or len(out) >= total or page >= 200:
                break
            page += 1
        if not out:
            raise BoardNotFound("Jibe: no public postings")
        jobs = []
        for d in out:
            jid = str(_first(d, "req_id", "slug", "id") or "")
            if not jid:
                continue
            title = (d.get("title") or "").strip()
            loc = _first(d, "full_location", "location_name", "short_location") or ", ".join(
                x for x in (d.get("city"), d.get("state"), d.get("country_code") or d.get("country")) if x)
            uid = f"jibe:{host}:{jid}"
            if not self.keep(uid, title, [loc]):
                continue
            body = "".join(x for x in (d.get("description"), d.get("responsibilities"), d.get("qualifications")) if x)
            same, marker = self.unchanged(uid, body)
            cats = [c.get("name") if isinstance(c, dict) else c for c in d.get("categories") or []]
            jobs.append(Job(
                source="jibe", source_board=host, external_id=jid, title=title,
                company=self.company or d.get("hiring_organization") or d.get("client_code") or host,
                location=loc or "", url=_first(d, "apply_url", "url") or f"https://{host}/jobs/{jid}",
                content_html="" if same else sanitize_html(body), departments=[c for c in cats if c],
                posted_at=parse_dt(_first(d, "posted_date", "create_date", "posting_date")),
                updated_at=parse_dt(d.get("update_date")),
                employment_type=_first(d, "employment_type", "type"),
                workplace=workplace(text=_first(d, "location_type", "workplace_type")),
                locations=[loc] if loc else [], detail_marker=marker))
        return jobs
