"""Oracle Recruiting Cloud (Candidate Experience) — public JSON, no key.

Many large employers (JPMorgan Chase, Kroger, American Express, Dell, Honeywell...) hire through
Oracle. Their careers page, whether on oraclecloud.com or the company's own domain, names the API
host and the site number in its HTML (data-apibaseurl, data-sitenumber).

    slug = "<api host>/<site number>"        e.g. "eofd.fa.us6.oraclecloud.com/CX_1001"

The list endpoint pages 100 at a time with title, location and posted date; the description needs
one detail call per posting, made only for new postings (capped per run, like Workday).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, json_body, workplace

PAGE = 100
LIST = ("https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
        "&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},limit={limit},offset={offset},"
        "sortBy=POSTING_DATES_DESC")
DETAIL = ("https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
          "&finder=ById;Id=%22{id}%22,siteNumber={site}")
_URL = re.compile(r"(?P<host>[\w-]+\.fa(?:\.[\w-]+)*\.oraclecloud\.com)(?::443)?/hcmUI/CandidateExperience/"
                  r"[a-z]{2}(?:-[A-Za-z]{2})?/sites/(?P<site>[\w-]+)", re.I)
_PAGE_API = re.compile(r'data-apibaseurl="https://([\w.-]+?)(?::443)?/?"', re.I)
_PAGE_SITE = re.compile(r'data-sitenumber="([\w-]+)"', re.I)


def from_page(html: str) -> str | None:
    """An Oracle careers page (any domain) names its API host and site number."""
    a, s = _PAGE_API.search(html or ""), _PAGE_SITE.search(html or "")
    return f"{a.group(1).lower()}/{s.group(1)}" if a and s else None


class OracleSource(Source):
    name = "oracle"
    label = "Oracle Recruiting"

    @classmethod
    def detect(cls, url: str) -> str | None:
        m = _URL.search(url or "")
        return f"{m['host'].lower()}/{m['site']}" if m else None

    @staticmethod
    def _split(slug: str) -> tuple[str, str]:
        host, _, site = (slug or "").partition("/")
        if not host or not site:
            raise BoardNotFound(f"Oracle board should look like host/SITE, got {slug!r}")
        return host, site

    def peek(self, slug):
        host, site = self._split(slug)
        r = self.client.get(LIST.format(host=host, site=quote(site), limit=1, offset=0), headers={"Accept": "application/json"})
        r.raise_for_status()
        items = r.json().get("items") or [{}]
        return int(items[0].get("TotalJobsCount") or 0)

    def fetch(self, slug, known=None):
        known = known or {}
        host, site = self._split(slug)
        reqs, offset, total = [], 0, None
        while True:
            r = self.client.get(LIST.format(host=host, site=quote(site), limit=PAGE, offset=offset),
                                headers={"Accept": "application/json"})
            if r.status_code in (400, 404):
                raise BoardNotFound("Oracle: site not found")
            items = (json_body(r, "Oracle").get("items") or [{}])
            page = items[0] if items else {}
            if total is None:
                total = int(page.get("TotalJobsCount") or 0)
            batch = page.get("requisitionList") or []
            reqs += batch
            offset += len(batch)
            if not batch or offset >= total:
                break
        if not reqs:
            raise BoardNotFound("Oracle: no public postings on this site")
        company = self.company_name(slug.split(".")[0])
        jobs = [self.parse_summary(host, site, raw, company) for raw in reqs if raw.get("Id")]
        jobs = [j for j in jobs if self.wanted(j)]
        todo = self.detail_todo([j for j in jobs if not known.get(j.uid, ("", False))[1]])
        with ThreadPoolExecutor(max_workers=4) as pool:
            for job, detail in zip(todo, pool.map(lambda j: self._detail(host, site, j), todo)):
                if detail:
                    self.apply_detail(job, detail)
        return jobs

    def _detail(self, host, site, job):
        if self.out_of_time():
            return None
        try:
            r = self.client.get(DETAIL.format(host=host, site=quote(site), id=quote(job.external_id)),
                                headers={"Accept": "application/json"})
            items = json_body(r, "Oracle").get("items") or []
            return items[0] if items else None
        except Exception as exc:
            self.pushed_back(exc)
            return None

    @staticmethod
    def parse_summary(host: str, site: str, raw: dict, company: str) -> Job:
        main = (raw.get("PrimaryLocation") or "").strip()
        extra = [(x.get("Name") or "").strip() for x in raw.get("secondaryLocations") or [] if isinstance(x, dict)]
        locs = [l for l in [main, *extra] if l]
        return Job(
            source="oracle", source_board=f"{host}/{site}", external_id=str(raw["Id"]),
            title=(raw.get("Title") or "").strip(), company=company, location=main,
            url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{raw['Id']}",
            posted_at=parse_dt(raw.get("PostedDate")), locations=locs,
            workplace=workplace(text=raw.get("WorkplaceType") or raw.get("WorkplaceTypeCode")),
            employment_type=raw.get("JobSchedule") or None,
            departments=[d for d in (raw.get("JobFamily"), raw.get("JobFunction"), raw.get("Department")) if d],
            detail_marker=str(raw["Id"]), partial=True)

    @staticmethod
    def apply_detail(job: Job, d: dict) -> None:
        parts = [d.get("ExternalDescriptionStr"), d.get("ExternalResponsibilitiesStr"),
                 d.get("ExternalQualificationsStr"), d.get("CorporateDescriptionStr")]
        job.content_html = sanitize_html("".join(p for p in parts if p))
        job.posted_at = parse_dt(d.get("ExternalPostedStartDate")) or job.posted_at
        job.employment_type = d.get("JobSchedule") or job.employment_type
        if d.get("Category"):
            job.departments = [d["Category"], *[x for x in job.departments if x != d["Category"]]]
        job.partial = False
