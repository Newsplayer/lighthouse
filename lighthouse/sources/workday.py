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
and filled in on later runs.

Two things keep big sites complete and cheap (checked against real sites, 2026-11-10):
- Many sites serve at most 2,000 results per listing, whatever they have (Walmart: about 20,000; past
  2,000 they repeat earlier pages). The site's own filters ("facets": state, job category, full or
  part time...) each give a smaller listing, so a capped listing is read as the sum of the listings
  of one filter's values, split again where one of those is still capped.
- Most sites offer a country filter. When the builder covers only some countries, the listing is
  asked for those countries only: Accenture's is 1,195 postings instead of every country's.
"""

from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .. import geo
from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, json_body, workplace

PAGE = 20  # the API rejects larger pages
CAP = 2000          # a listing reporting this many (or more) is cut short by the site
MAX_LISTED = 40000  # stop a runaway listing (the largest real site seen is about 20,000)
MAX_SPLITS = 3      # filters applied on top of each other to get under the cap
MAX_VALUES = 120    # a filter with more values than this costs too many requests to split by
LIST_WORKERS = 4    # listing pages read at once
_COUNTRY_NAMES = {name: code for code, names in geo.COUNTRIES.items() for name in names}
_URL = re.compile(r"(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[\w-]+)", re.I)
# The same boards are also served as wdN.myworkdaysite.com/recruiting/<tenant>/<site>
_SITE_URL = re.compile(r"(?P<dc>wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/(?P<tenant>[\w-]+)/(?P<site>[\w-]+)", re.I)
_SLUG = re.compile(r"^(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)/(?P<site>[\w-]+)$", re.I)
_RESERVED_SITES = {"wday", "job", "jobs", "login", "en-us"}


def _leaves(facets, param=None):
    """(facet parameter, value) for every selectable value; some facets nest groups of values."""
    for f in facets or []:
        if not isinstance(f, dict):
            continue
        p = f.get("facetParameter") or param
        if f.get("values"):
            yield from _leaves(f["values"], p)
        elif f.get("id") and p:
            yield p, f


def country_facet(facets, wanted) -> dict | None:
    """The filter that narrows a listing to the wanted countries: {facet parameter: [value ids]}.
    None: this site has no country filter we can trust, so read everything. {}: it has one, and none
    of the wanted countries are in it (no postings there).

    A country filter is a facet whose values are nearly all country names ("United States of America",
    "Canada"...), whatever the site calls it (locationCountry, Location_Country, locationHierarchy1,
    Country_and_Jurisdiction have all been seen). A long list may be cut short by the site, so a
    wanted country missing from one of 50+ values means "can't tell", not "none"."""
    by_param: dict[str, list] = {}
    for param, v in _leaves(facets):
        by_param.setdefault(param, []).append(v)
    best = None
    for param, values in by_param.items():
        codes = [_COUNTRY_NAMES.get(geo._norm(v.get("descriptor") or "")) for v in values]
        named = sum(1 for c in codes if c)
        if named < 0.6 * len(values) and not (named and "country" in param.lower()):
            continue
        ids = [v["id"] for v, c in zip(values, codes) if c in wanted]
        found = {c for c in codes if c in wanted}
        if len(values) >= 50 and found != set(wanted):
            return None
        if best is None or len(ids) > len(best[1]):
            best = (param, ids)
    if best is None:
        return None
    return {best[0]: best[1]} if best[1] else {}


def split_facet(facets, applied: dict, expected: int | None = None) -> tuple[str, list, bool] | None:
    """The filter to split a capped listing by: (facet parameter, its values, whether they cover the
    listing), or None when there is nothing to split by.

    Checked on real sites (2026-11-10): some filters list every value (states, locations), others stop
    at 55 values, and some apply to only part of the postings (Advance Auto's "hiring company": 3,806
    of 16,260). So a filter is judged by its counts:
    - they should add up to the listing (`expected`, or the total most filters agree on), since a
      posting with no value for the filter is in none of its parts;
    - as few postings as possible should sit in values that are over the cap themselves (those need
      another split);
    - then fewer values, which is fewer requests."""
    by_param: dict[str, list] = {}
    for param, v in _leaves(facets):
        if param not in applied and param != "distance" and isinstance(v.get("count"), int):
            by_param.setdefault(param, []).append(v)
    if not expected:
        sums = Counter(sum(v["count"] for v in vals) for vals in by_param.values())
        expected = max(sums, key=lambda t: (sums[t], t)) if sums else 0
    best = None
    for param, vals in by_param.items():
        if not 2 <= len(vals) <= MAX_VALUES:
            continue
        counts = [v["count"] for v in vals]
        missing = max(0, expected - sum(counts))
        key = (0 if missing <= 0.01 * expected else 1 if missing <= 0.1 * expected else 2,
               sum(c for c in counts if c >= CAP), len(vals))
        if best is None or key < best[0]:
            best = (key, param, vals)
    if best is None:
        return None
    return best[1], [v for v in best[2] if v["count"] > 0], best[0][0] == 0


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

    def _page(self, api: str, facets: dict, offset: int) -> dict:
        r = self.client.post(f"{api}/jobs", json={"appliedFacets": facets, "limit": PAGE, "offset": offset,
                                                  "searchText": ""},
                             headers={"Accept": "application/json"})
        if r.status_code in (404, 422):
            raise BoardNotFound("Workday: site not found (check tenant, wdN and site name)")
        return json_body(r, "Workday")

    def _pages(self, api: str, facets: dict, page: dict, out: dict) -> None:
        """Read one listing (its first page is in hand) into out, four pages at a time (the per-host rate
        limit still applies), until its total (or the cap: past it, sites repeat earlier pages) is reached
        or a page comes back empty. A posting seen before (in another part of a split listing, or moved
        between pages while we read) is kept once."""
        total = int(page.get("total") or 0)          # only the first page carries the total

        def take(batch) -> int:
            before = len(out)
            for raw in batch:
                if raw.get("externalPath"):
                    out.setdefault(raw["externalPath"], raw)
            return len(out) - before

        got = len(page.get("jobPostings") or [])
        take(page.get("jobPostings") or [])
        offset = PAGE
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            while got and len(out) < MAX_LISTED and offset < min(total, CAP):
                left = (min(total, CAP) - offset + PAGE - 1) // PAGE
                offsets = [offset + k * PAGE for k in range(min(LIST_WORKERS, left))]
                for batch in pool.map(lambda o: self._page(api, facets, o).get("jobPostings") or [], offsets):
                    got = len(batch)
                    take(batch)
                    offset += PAGE
                    if not got:
                        break

    def _collect(self, api: str, facets: dict, out: dict, page: dict | None = None,
                 expected: int | None = None, depth: int = 0) -> None:
        """Every posting matching `facets`, into out. A listing at the cap is split by one more filter
        (split_facet) and each part collected the same way. If that filter doesn't cover the whole
        listing, the listing's own first 2,000 are read as well, for the postings the filter misses."""
        page = page if page is not None else self._page(api, facets, 0)
        if int(page.get("total") or 0) >= CAP and depth < MAX_SPLITS and len(out) < MAX_LISTED:
            split = split_facet(page.get("facets"), facets, expected)
            if split:
                param, values, covers = split
                for v in values:
                    self._collect(api, {**facets, param: [v["id"]]}, out, None, v["count"], depth + 1)
                if covers:
                    return
        self._pages(api, facets, page, out)

    def fetch(self, slug, known=None):
        known = known or {}
        host, api, site = self._base(slug)
        first = self._page(api, {}, 0)
        if not first.get("jobPostings"):
            raise BoardNotFound("Workday: no public postings on this site")
        narrow = country_facet(first.get("facets"), self.countries) if self.countries else None
        if narrow == {}:           # the site has a country filter, and none of our countries are in it
            return []
        found: dict[str, dict] = {}
        if narrow:
            ids = {i for v in narrow.values() for i in v}
            expected = sum(v.get("count") or 0 for _, v in _leaves(first.get("facets")) if v["id"] in ids) or None
            self._collect(api, narrow, found, None, expected)
        if not found:              # no country filter (or it matched nothing though it listed our countries)
            self._collect(api, {}, found, first)
        items = list(found.values())
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
