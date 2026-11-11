"""Phenom career sites — the site's own public search API, no key.

Phenom powers many large employers' careers sites (careers.<company>.com/us/en/...). Discovery
first checks where a Phenom site's Apply links go: when they lead to Workday (or another supported
system) that board is used directly. Otherwise this reader uses the site's widgets API, the same
one its search page calls.

    slug = "<host>/<country>/<lang>"      e.g. "jobs.example.com/us/en"
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, workplace

PAGE = 100
_SLUG = re.compile(r"^(?P<host>[\w.-]+)/(?P<country>[\w-]+)/(?P<lang>[a-z]{2})$", re.I)
_PAGE = re.compile(r'"widgetApiEndpoint"\s*:\s*"https://(?P<host>[\w.-]+)/widgets".{0,400}?"country"\s*:\s*"(?P<country>[\w-]+)"'
                   r'.{0,200}?"locale"\s*:\s*"(?P<lang>[a-z]{2})_', re.I | re.S)


def from_page(html: str) -> str | None:
    m = _PAGE.search(html or "")
    return f"{m['host'].lower()}/{m['country'].lower()}/{m['lang'].lower()}" if m else None


class PhenomSource(Source):
    name = "phenom"
    label = "Phenom"

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None   # recognised by discovery from the page (phApp config)

    def _parts(self, slug):
        m = _SLUG.match(slug or "")
        if not m:
            raise BoardNotFound(f"Phenom board should look like host/country/lang, got {slug!r}")
        return m["host"].lower(), m["country"].lower(), m["lang"].lower()

    def _post(self, host, body):
        r = self.client.post(f"https://{host}/widgets", json=body,
                             headers={"Accept": "application/json", "Content-Type": "application/json"})
        if r.status_code in (403, 404):
            raise BoardNotFound("Phenom: search API not available on this site")
        r.raise_for_status()
        return r.json()

    def list_jobs(self, slug, size=PAGE, limit=None) -> list[dict]:
        host, country, lang = self._parts(slug)
        base = {"lang": f"{lang}_{country}", "deviceType": "desktop", "country": country, "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "Most recent", "subsearch": "", "jobs": True, "counts": False,
                "all_fields": [], "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "siteType": "external", "keywords": "", "global": True, "selected_fields": {}, "locationData": {}}
        out, start, total = [], 0, None
        while True:
            data = (self._post(host, {**base, "from": start, "size": size}).get("refineSearch") or {})
            if total is None:
                total = int(data.get("totalHits") or 0)
            batch = (data.get("data") or {}).get("jobs") or []
            out += batch
            start += len(batch)
            if not batch or start >= total or (limit and start >= limit):
                break
        return out

    def peek(self, slug):
        host, country, lang = self._parts(slug)
        data = self._post(host, {"lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
                                 "pageName": "search-results", "ddoKey": "refineSearch", "from": 0, "size": 1, "jobs": True,
                                 "siteType": "external", "keywords": "", "global": True, "selected_fields": {}})
        return int((data.get("refineSearch") or {}).get("totalHits") or 0)

    def fetch(self, slug, known=None):
        known = known or {}
        host, country, lang = self._parts(slug)
        raws = self.list_jobs(slug)
        if not raws:
            raise BoardNotFound("Phenom: no public postings")
        company = self.company_name(host.split(".")[1] if host.count(".") >= 2 else host)
        jobs = [self.parse_summary(slug, host, country, lang, r, company) for r in raws if r.get("jobSeqNo") or r.get("jobId")]
        jobs = [j for j in jobs if self.wanted(j)]
        todo = self.detail_todo([j for j in jobs if not known.get(j.uid, ("", False))[1]])
        with ThreadPoolExecutor(max_workers=4) as pool:
            for job, d in zip(todo, pool.map(lambda j: self._detail(host, country, lang, j), todo)):
                if d:
                    job.content_html = sanitize_html(d.get("description") or "")
                    job.partial = False
        return jobs

    def _detail(self, host, country, lang, job):
        if self.out_of_time():
            return None
        try:
            data = self._post(host, {"lang": f"{lang}_{country}", "deviceType": "desktop", "country": country,
                                     "pageName": "job", "ddoKey": "jobDetail", "jobSeqNo": job.detail_marker})
            return ((data.get("jobDetail") or {}).get("data") or {}).get("job")
        except Exception as exc:
            self.pushed_back(exc)
            return None

    @staticmethod
    def parse_summary(slug, host, country, lang, r: dict, company: str) -> Job:
        locs = [l for l in r.get("multi_location") or [] if isinstance(l, str)] or ([r["location"]] if r.get("location") else [])
        jid = str(r.get("jobId") or r.get("reqId"))
        return Job(source="phenom", source_board=slug, external_id=jid, title=(r.get("title") or "").strip(),
                   company=r.get("companyName") or company, location=locs[0] if locs else "",
                   url=f"https://{host}/{country}/{lang}/job/{jid}", posted_at=parse_dt(r.get("postedDate")),
                   locations=locs, employment_type=r.get("type") or None,
                   workplace=workplace(text=r.get("remote") or r.get("workplaceType")),
                   departments=[c for c in r.get("multi_category") or [r.get("category")] if c],
                   detail_marker=r.get("jobSeqNo") or jid, partial=True)
