"""Eightfold careers sites — the site's own public JSON API, no key.

    slug = "<host>|<domain>"       e.g. "explore.jobs.netflix.net|netflix.com"

Some tenants (the newer "PCSX" sites) refuse this API; those are reported as not readable.
"""

from __future__ import annotations

import re

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, json_body

PAGE = 100
_LINK = re.compile(r"https://([\w-]+\.eightfold\.ai|explore\.jobs\.netflix\.net)/careers[^\"'\s<>]*?domain=([\w.-]+)", re.I)


def from_page(html: str) -> str | None:
    m = _LINK.search(html or "")
    return f"{m.group(1).lower()}|{m.group(2).lower()}" if m else None


class EightfoldSource(Source):
    name = "eightfold"
    label = "Eightfold"

    @classmethod
    def detect(cls, url: str) -> str | None:
        return from_page(url)

    @staticmethod
    def _split(slug):
        host, _, domain = (slug or "").partition("|")
        if not host or not domain:
            raise BoardNotFound(f"Eightfold board should look like host|domain, got {slug!r}")
        return host, domain

    def peek(self, slug):
        host, domain = self._split(slug)
        r = self.client.get(f"https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=1", headers={"Accept": "application/json"})
        r.raise_for_status()
        return int(r.json().get("count") or 0)

    def fetch(self, slug, known=None):
        known = known or {}
        host, domain = self._split(slug)
        raws, start, total = [], 0, None
        while True:
            r = self.client.get(f"https://{host}/api/apply/v2/jobs?domain={domain}&start={start}&num={PAGE}&sort_by=new",
                                headers={"Accept": "application/json"})
            if r.status_code in (401, 403, 404):
                raise BoardNotFound("Eightfold: this site's jobs API isn't public")
            r.raise_for_status()
            data = r.json()
            if total is None:
                total = int(data.get("count") or 0)
            batch = data.get("positions") or []
            raws += batch
            start += len(batch)
            if not batch or start >= total:
                break
        if not raws:
            raise BoardNotFound("Eightfold: no public postings")
        jobs = []
        for p in raws:
            locs = [l for l in p.get("locations") or [] if l] or ([p["location"]] if p.get("location") else [])
            j = Job(source="eightfold", source_board=slug, external_id=str(p["id"]),
                    title=(p.get("name") or p.get("posting_name") or "").strip(),
                    company=self.company or domain.split(".")[0].title(), location=locs[0] if locs else "",
                    url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                    posted_at=parse_dt(p.get("t_create")), locations=locs,
                    departments=[d for d in (p.get("department"), p.get("business_unit")) if d],
                    detail_marker=str(p.get("t_update") or p["id"]), partial=True)
            if self.wanted(j):
                jobs.append(j)
        for job, d in self.details([j for j in jobs if known.get(j.uid, ("", False)) != (j.detail_marker, True)], lambda j: self._detail(host, domain, j)):
            if d and d.get("job_description"):
                job.content_html = sanitize_html(d["job_description"])
                job.partial = False
        return jobs

    def _detail(self, host, domain, job):
        if self.out_of_time():
            return None
        try:
            r = self.client.get(f"https://{host}/api/apply/v2/jobs/{job.external_id}?domain={domain}",
                                headers={"Accept": "application/json"})
            return json_body(r, "Eightfold")
        except Exception as exc:
            self.pushed_back(exc)
            return None
