"""Workday career sites (the CXS JSON API behind every *.myworkdayjobs.com site) — no key.

Most large employers hire through Workday. A board is identified by its tenant, data
center and site, all visible in the careers URL:

    https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
            └tenant┘└dc┘                  └────── site ──────┘
    slug = "nvidia.wd5/NVIDIAExternalCareerSite"

The list endpoint pages 20 at a time and returns only title, location text and a relative
date ("Posted 3 Days Ago"), so each posting needs one detail call for its description,
exact start date and full location list. As with SmartRecruiters, detail calls go only to
new postings, capped per run (max_detail_calls_per_board); the rest are listed title-only
and filled in on later runs. Some very large sites report at most ~2,000 results per listing.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, json_body, workplace

PAGE = 20  # the API rejects larger pages
_URL = re.compile(r"(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[\w-]+)", re.I)
# The same boards are also served as wdN.myworkdaysite.com/recruiting/<tenant>/<site>
_SITE_URL = re.compile(r"(?P<dc>wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[\w-]+)/(?P<site>[\w-]+)", re.I)
_SLUG = re.compile(r"^(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)/(?P<site>[\w-]+)$", re.I)
_RESERVED_SITES = {"wday", "job", "jobs", "login", "en-us"}


def parse_slug(slug: str) -> tuple[str, str, str]:
    m = _SLUG.match(slug.strip())
    if not m:
        raise BoardNotFound(f"Workday board should look like tenant.wdN/Site, got {slug!r}")
    return m["tenant"].lower(), m["dc"].lower(), m["site"]


def posted_from_text(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago'. '30+ Days Ago' is too vague: None."""
    t = (text or "").lower()
    now = now or datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "today" in t:
        return day
    if "yesterday" in t:
        return day - timedelta(days=1)
    m = re.search(r"(\d+)\s*days?\s*ago", t)
    if m and "+" not in t:
        return day - timedelta(days=int(m.group(1)))
    return None


class WorkdaySource(Source):
    name = "workday"
    label = "Workday"

    @classmethod
    def group(cls, slug: str) -> str | None:
        # Each data center (wd1, wd5, ...) answers for every company on it, and its bot protection
        # pushes back for all of them at once.
        m = _SLUG.match(slug.strip())
        return f"workday:{m['dc'].lower()}" if m else "workday"

    @classmethod
    def detect(cls, url: str) -> str | None:
        m = _URL.search(url or "") or _SITE_URL.search(url or "")
        if not m or m["site"].lower() in _RESERVED_SITES:
            return None
        return f"{m['tenant'].lower()}.{m['dc'].lower()}/{m['site']}"

    def _base(self, slug: str) -> tuple[str, str, str]:
        tenant, dc, site = parse_slug(slug)
        host = f"https://{tenant}.{dc}.myworkdayjobs.com"
        return host, f"{host}/wday/cxs/{tenant}/{site}", site

    def fetch(self, slug, known=None):
        known = known or {}
        host, api, site = self._base(slug)
        items, offset, total = [], 0, None
        while True:
            r = self.client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": PAGE, "offset": offset,
                                                      "searchText": ""},
                                 headers={"Accept": "application/json"})
            if r.status_code in (404, 422):
                raise BoardNotFound("Workday: site not found (check tenant, wdN and site name)")
            page = json_body(r, "Workday")
            if total is None:
                total = int(page.get("total") or 0)  # only the first page carries the total
            batch = page.get("jobPostings") or []
            items += batch
            offset += len(batch)
            if not batch or offset >= total:
                break
        if not items:
            raise BoardNotFound("Workday: no public postings on this site")
        company = self.company_name(slug.split(".")[0])
        jobs = [self.parse_summary(slug, host, site, raw, company) for raw in items if raw.get("externalPath")]
        jobs = [j for j in jobs if self.wanted(j)]
        todo = [j for j in jobs if not known.get(j.uid, ("", False))[1]][: self.max_detail_calls]
        with ThreadPoolExecutor(max_workers=4) as pool:  # per-host throttle still applies
            for job, detail in zip(todo, pool.map(lambda j: self._detail(api, j), todo)):
                if detail:
                    self.apply_detail(job, detail)
        return jobs

    def _detail(self, api: str, job: Job) -> dict | None:
        if self.out_of_time():
            return None
        try:
            r = self.client.get(f"{api}{job.detail_marker}", headers={"Accept": "application/json"})
            return json_body(r, "Workday")
        except Exception as exc:
            self.pushed_back(exc)
            return None  # stays title-only; retried next run

    @staticmethod
    def parse_summary(slug: str, host: str, site: str, raw: dict, company: str) -> Job:
        path = raw["externalPath"]  # "/job/US-CA-Santa-Clara/Title_JR123"
        loc = (raw.get("locationsText") or "").strip()
        multi = bool(re.match(r"^\d+\s+locations?$", loc, re.I))
        return Job(
            source="workday",
            source_board=slug,
            external_id=path.rsplit("/", 1)[-1],
            title=(raw.get("title") or "").strip(),
            company=company,
            location="" if multi else loc,
            url=f"{host}/{site}{path}",
            content_html="",
            posted_at=posted_from_text(raw.get("postedOn")),
            locations=[] if multi else ([loc] if loc else []),
            detail_marker=path,   # constant per posting: details are fetched once
            partial=True,
        )

    @staticmethod
    def apply_detail(job: Job, detail: dict) -> None:
        info = detail.get("jobPostingInfo") or {}
        job.content_html = sanitize_html(info.get("jobDescription") or "")
        main = (info.get("location") or job.location or "").strip()
        extra = [l for l in info.get("additionalLocations") or [] if isinstance(l, str)]
        job.location = main
        job.locations = [l for l in [main, *extra] if l]
        job.posted_at = parse_dt(info.get("startDate")) or job.posted_at
        job.employment_type = info.get("timeType") or None
        job.workplace = workplace(text=info.get("remoteType")) or job.workplace
        job.url = info.get("externalUrl") or job.url
        job.partial = False
