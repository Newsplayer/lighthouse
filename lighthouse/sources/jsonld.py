"""Any careers site that publishes a sitemap of job pages carrying schema.org JobPosting data.

That structured data is what Google for Jobs reads, so most large employers' own careers sites
(TalentBrew/Radancy sites, DirectEmployers ".jobs" sites, many custom ones) carry it. This reader
takes the job links from the sitemap and reads each page's JobPosting block: title, description,
dates, locations, pay. Nothing else on the page is used.

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

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, interval, num

JOB_URL = re.compile(r"/(?:job|jobs|details|position|positions|vacancy|vacancies|opening|careers/job)s?/[^/?#]+|/job/?$", re.I)
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
_LASTMOD = re.compile(r"<url>\s*<loc>\s*(.*?)\s*</loc>.*?(?:<lastmod>\s*(.*?)\s*</lastmod>)?.*?</url>", re.I | re.S)
_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.I | re.S)
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
    """The first schema.org JobPosting object in a page's JSON-LD blocks."""
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
    return None


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

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None   # chosen by discovery when a site's job pages carry JobPosting data

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._robots = None

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
        fetchable = todo[: self.max_detail_calls]
        for uid, *_ in todo[self.max_detail_calls:]:
            if uid in known:
                self.dropped.add(uid)     # changed, but over this run's budget: next run
        stop_at = time.monotonic() + BOARD_SECONDS

        def read(t):
            return self._page(t[2]) if time.monotonic() < stop_at else False

        with ThreadPoolExecutor(max_workers=4) as pool:
            for (uid, ext, url, marker), page in zip(fetchable, pool.map(read, fetchable)):
                jp = job_posting(page) if page else None
                if not jp:
                    if uid in known:
                        self.dropped.add(uid)   # not read this time (or no data today): keep what we have
                    continue
                j = self.from_posting(slug, board_host, ext, url, marker, jp)
                if j and self.wanted(j):
                    jobs.append(j)
        if not jobs and not self.dropped and len(todo) <= len(fetchable):
            raise BoardNotFound("job pages carry no JobPosting data")
        return jobs

    def _page(self, url: str) -> str | None:
        try:
            if not self._allowed(url):
                return None
            r = self.client.get(url, headers={"Accept": "text/html"}, deadline=30, max_bytes=5_000_000)
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
                   detail_marker=marker)
