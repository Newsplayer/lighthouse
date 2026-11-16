"""Any careers site that publishes a sitemap of job pages carrying schema.org JobPosting data.

That structured data is what Google for Jobs reads, so most large employers' own careers sites
(TalentBrew/Radancy sites, DirectEmployers ".jobs" sites, SAP SuccessFactors career sites, iCIMS
portals, many custom ones) carry it. This reader takes the job links from the sitemap and reads each
page's JobPosting: title, description, dates, locations, pay. Nothing else on the page is used. A page
whose validThrough date has passed is a closed posting, even while the sitemap still lists it.

The data comes as a JSON-LD block or, on SuccessFactors career sites, as schema.org microdata (itemprop
attributes in the page's markup). iCIMS portals carry it in the frame version of each job page.

A page read once and found outside the builder's countries (or without job data) is remembered with
its sitemap date, so it isn't read again until it changes: big sites list mostly roles elsewhere.

    slug = "<sitemap URL>"       e.g. "https://www.careers.ford.com/en/sitemap.xml"

Each job page is one request, so (like Workday) only new or changed pages are read, at most
max_detail_calls per run; the rest are picked up on later runs. Pages already read and unchanged
are kept as they are. robots.txt is respected for every page.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from ..models import Job, parse_dt, parse_expiry
from ..sanitize import sanitize_html
from .base import BLOCK_STATUS, Blocked, BoardNotFound, Source, interval, num

JOB_URL = re.compile(r"/(?:job|jobs|job_details|details|position|positions|vacancy|vacancies|opening|careers/job)s?/[^/?#]+|/job/?$",
                     re.I)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_LASTMOD = re.compile(r"<url>\s*<loc>\s*(.*?)\s*</loc>.*?(?:<lastmod>\s*(.*?)\s*</lastmod>)?.*?</url>", re.I | re.S)
# Some sites escape the "+" in the script type (ld&#x2B;json): Caterpillar, Cognizant.
_LD = re.compile(r"<script[^>]+application/ld(?:\+|&#x2B;|&#43;)json[^>]*>(.*?)</script>", re.I | re.S)
_LOCALE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$", re.I)
MAX_SITEMAPS = 20
BOARD_SECONDS = 20 * 60          # a slow site can't hold the build: the rest waits for the next run
# Listing pages that share the job-URL shape (/jobs/location/austin, /jobs/search?...): read last.
_LISTING = re.compile(r"/(?:search|location|locations|category|categories|department|departments|team|teams|"
                      r"city|cities|state|states|country|countries|keyword|keywords|tag|tags|saved|alerts?)(?:/|$)", re.I)


def page_id(url: str) -> str:
    """A stable id for a job page: its path, plus the query when the path alone isn't unique
    (/careers/job?id=123)."""
    parts = urlsplit(url)
    ext = parts.path.strip("/") + (f"?{parts.query}" if parts.query else "")
    return ext if 0 < len(ext) <= 200 else hashlib.sha1(url.encode()).hexdigest()


def _priority(url: str) -> int:
    """Job pages first: a URL with an id-like segment (digits) beats one that may be a listing."""
    path = urlsplit(url).path
    if _LISTING.search(path):
        return 2
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return 0 if re.search(r"\d", last) or re.search(r"\d", urlsplit(url).query) else 1


def job_posting(page: str) -> dict | None:
    """The page's schema.org JobPosting: its first JSON-LD one, else one read from microdata."""
    for block in _LD.findall(page or ""):
        try:
            data = json.loads(html.unescape(block.strip()) if block.lstrip().startswith("&") else block.strip(), strict=False)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return d
            stack += d.get("@graph") or []
    return _microdata(page or "")


_ITEMPROP = re.compile(r"<(\w+)\b([^>]*?)\bitemprop=[\"'](\w+)[\"']([^>]*)>", re.I)
_CONTENT = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)
_PLACE_PARTS = ("addressLocality", "addressRegion", "addressCountry", "streetAddress")


def _inner(page: str, tag: str, start: int) -> str:
    """The markup inside the element whose start tag ends at `start`, nested same-name tags included."""
    depth, pat = 1, re.compile(rf"<(/?){tag}\b[^>]*>", re.I)
    for m in pat.finditer(page, start):
        depth += -1 if m.group(1) else 1
        if not depth:
            return page[start:m.start()]
    return page[start:start + 200_000]


def _date(value: str) -> str:
    """SuccessFactors writes 'Fri Oct 30 02:00:00 UTC 2026'."""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(value.replace(" UTC ", " "), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return value


def _microdata(page: str) -> dict | None:
    """A JobPosting written as schema.org microdata, shaped like the JSON-LD one. A posting that names no
    place is left out: without one it can't be kept to the builder's countries (Wipro's pages)."""
    if "schema.org/JobPosting" not in page:
        return None
    out: dict = {"@type": "JobPosting"}
    places: list[dict] = []
    for m in _ITEMPROP.finditer(page):
        tag, prop = m.group(1).lower(), m.group(3)
        content = _CONTENT.search(m.group(2) + m.group(4))
        if prop in ("address", "jobLocation"):
            if not places or any(places[-1].values()):
                places.append({})
        elif prop in _PLACE_PARTS and content:
            if not places:
                places.append({})
            places[-1][prop] = html.unescape(content.group(1)).strip()
        elif prop in ("datePosted", "validThrough") and content:
            out.setdefault(prop, _date(content.group(1).strip()))
        elif prop in ("hiringOrganization", "employmentType") and content:
            out.setdefault(prop, html.unescape(content.group(1)).strip())
        elif prop == "title" and "title" not in out:
            out["title"] = re.sub(r"<[^>]+>", "", content.group(1) if content else _inner(page, tag, m.end()))
        elif prop == "description" and tag != "meta":
            text = _inner(page, tag, m.end())
            if len(text) > len(out.get("description", "")):     # Wipro: a short blurb first, then the posting
                out["description"] = text
    addrs = []
    for p in places:
        if p.get("addressLocality") or p.get("addressRegion") or p.get("addressCountry"):
            addrs.append({k: p[k] for k in _PLACE_PARTS[:3] if p.get(k)})
        elif p.get("streetAddress"):
            addrs.append({"addressLocality": p["streetAddress"]})     # 'Dublin 2, IE', 'Mexico City, MX, 6600'
    if not out.get("title") or not addrs:
        return None
    out["title"] = html.unescape(out["title"]).strip()
    out["jobLocation"] = [{"@type": "Place", "address": a} for a in addrs]
    return out


def _text(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("value") or "")
    return str(v or "")


def _places(jp: dict) -> list[str]:
    out = []
    locs = jp.get("jobLocation") or []
    for loc in locs if isinstance(locs, list) else [locs]:
        if not isinstance(loc, dict):
            continue
        addrs = loc.get("address") or {}
        for a in addrs if isinstance(addrs, list) else [addrs]:
            if isinstance(a, dict):
                parts = [_text(a.get(k)).strip() for k in ("addressLocality", "addressRegion", "addressCountry")]
                s = ", ".join(p for p in parts if p)
                if s and s not in out:
                    out.append(s)
            elif isinstance(a, str) and a.strip():
                out.append(a.strip())
        if not addrs and loc.get("name"):
            out.append(str(loc["name"]))
    return out


def english_only(urls: list[str]) -> list[str]:
    """Sites list each job once per language (/en/..., /fr/..., /de-de/...): keep the English copy."""
    def first_seg(u):
        segs = [s for s in urlsplit(u).path.split("/") if s]
        return segs[0].lower() if segs else ""
    has_en = any(_LOCALE.match(first_seg(u)) and first_seg(u).startswith("en") for u in urls)
    if not has_en:
        return urls
    return [u for u in urls if not (_LOCALE.match(first_seg(u)) and not first_seg(u).startswith("en"))]


class JsonLdSource(Source):
    name = "jsonld"
    label = "Careers site (structured data)"
    single_response = True

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None   # chosen by discovery when a site's job pages carry JobPosting data

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._robots = None
        self.pushed_back = ""    # a page answered with a bot check or block: no more pages this run

    def _allowed(self, url: str) -> bool:
        if self._robots is None:
            from ..discover import Robots
            from ..net import BUILDER_AGENT
            self._robots = Robots(self.client, BUILDER_AGENT)
        return self._robots.allowed(url)

    def job_urls(self, sitemap: str) -> list[tuple[str, str]]:
        """(url, lastmod) of job pages, following a sitemap index one level down."""
        todo, seen, out = [sitemap], set(), {}
        while todo and len(seen) < MAX_SITEMAPS:
            sm = todo.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            r = self.client.get(sm, headers={"Accept": "application/xml, text/xml"}, deadline=120, max_bytes=150_000_000)
            if r.status_code == 404:
                raise BoardNotFound("sitemap not found")
            r.raise_for_status()
            body = r.text
            if "<sitemapindex" in body[:2000]:
                kids = _LOC.findall(body)
                jobby = [k for k in kids if re.search(r"job|position|posting|career|req", k, re.I)]
                todo += (jobby or kids)[:MAX_SITEMAPS]
                continue
            for m in _LASTMOD.finditer(body):
                u = html.unescape(m.group(1).strip())
                if JOB_URL.search(urlsplit(u).path):
                    out[u] = m.group(2) or ""
        urls = english_only(list(out))
        if len(urls) > 20 and len({out[u] for u in urls}) == 1:
            out = dict.fromkeys(out, "")    # every page stamped with the same date (the day the sitemap was made)
        return [(u, out[u]) for u in urls]

    def peek(self, slug):
        """Job pages in the sitemap, if one of them really carries JobPosting data."""
        pairs = self.job_urls(slug)
        for url, _ in pairs[len(pairs) // 2: len(pairs) // 2 + 2]:
            page = self._page(url)
            if page and job_posting(page):
                return len(pairs)
        return 0

    def fetch(self, slug, known=None):
        known = known or {}
        pairs = self.job_urls(slug)
        if not pairs:
            raise BoardNotFound("no job pages in this sitemap")
        board_host = urlsplit(slug).hostname or slug
        jobs, todo = [], []
        pairs = sorted(pairs, key=lambda p: _priority(p[0]))
        for url, lastmod in pairs:
            ext = page_id(url)
            uid = f"jsonld:{slug}:{ext}"
            marker = lastmod or url
            if known.get(uid) == (marker, True) or (known.get(uid, ("", False))[1] and not lastmod):
                self.dropped.add(uid)     # already read and unchanged: keep it as it is
                continue
            todo.append((uid, ext, url, marker))
        todo.sort(key=lambda t: t[0] in known)   # pages never read before first (the sort keeps the order within each)
        fetchable = todo[: self.max_detail_calls]
        for uid, *_ in todo[self.max_detail_calls:]:
            if uid in known:
                self.dropped.add(uid)     # changed, but over this run's budget: next run
        stop_at = time.monotonic() + BOARD_SECONDS

        def read(t):
            return self._page(t[2]) if time.monotonic() < stop_at and not self.out_of_time() and not self.pushed_back \
                else False

        with ThreadPoolExecutor(max_workers=4) as pool:
            for (uid, ext, url, marker), page in zip(fetchable, pool.map(read, fetchable)):
                jp = job_posting(page) if page else None
                if not jp:
                    if isinstance(page, str):
                        self.skipped[uid] = marker   # read, but no job data on it (a listing page): not again
                    if uid in known:
                        self.dropped.add(uid)   # not read this time (or no data today): keep what we have
                    continue
                j = self.from_posting(slug, board_host, ext, url, marker, jp)
                if j and self.wanted(j):
                    jobs.append(j)
                else:
                    self.skipped[uid] = marker       # elsewhere, or an hourly title: not read again until it changes
        if not jobs and not self.dropped and len(todo) <= len(fetchable):
            if self.pushed_back:
                raise Blocked(f"careers site: job pages answered {self.pushed_back}")
            raise BoardNotFound("job pages carry no JobPosting data")
        return jobs

    def _page(self, url: str) -> str | None:
        try:
            if not self._allowed(url):
                return None
            if (urlsplit(url).hostname or "").endswith(".icims.com") and "in_iframe=" not in url:
                url += ("&" if "?" in url else "?") + "in_iframe=1"     # the full page is a frame around this one
            r = self.client.get(url, headers={"Accept": "text/html"}, deadline=30, max_bytes=5_000_000)
            if r.status_code in BLOCK_STATUS or r.status_code == 202:   # 202: an AWS WAF bot check (WTW, Etsy)
                self.pushed_back = f"HTTP {r.status_code}"
            return r.text if r.status_code == 200 else None
        except Exception:
            return None

    def from_posting(self, slug, host, ext, url, marker, jp: dict) -> Job | None:
        title = html.unescape(_text(jp.get("title")).strip())
        if not title:
            return None
        desc = jp.get("description") or ""
        if "&lt;" in desc[:500]:
            desc = html.unescape(desc)
        places = _places(jp)
        remote = str(jp.get("jobLocationType") or "").upper() == "TELECOMMUTE"
        sal = jp.get("baseSalary") if isinstance(jp.get("baseSalary"), dict) else {}
        val = sal.get("value") if isinstance(sal.get("value"), dict) else {}
        emp = jp.get("employmentType")
        org = jp.get("hiringOrganization")
        return Job(source="jsonld", source_board=slug, external_id=ext, title=title,
                   company=self.company or _text(org) or host, location=places[0] if places else ("Remote" if remote else ""),
                   url=_text(jp.get("url")) or url, content_html=sanitize_html(desc),
                   posted_at=parse_dt(jp.get("datePosted")), locations=places,
                   workplace="remote" if remote else None,
                   employment_type=", ".join(emp) if isinstance(emp, list) else (emp or None),
                   salary_min=num(val.get("minValue") or val.get("value")), salary_max=num(val.get("maxValue") or val.get("value")),
                   salary_currency=sal.get("currency") or None, salary_interval=interval(val.get("unitText")),
                   departments=[d for d in ([jp.get("industry")] if isinstance(jp.get("industry"), str) else
                                            jp.get("industry") or []) if isinstance(d, str)],
                   detail_marker=marker, expires_at=parse_expiry(jp.get("validThrough")))
