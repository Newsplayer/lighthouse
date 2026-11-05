"""Find each company's job board from its careers page. Runs only in the job-index Action.

config/companies.csv lists large employers by name, domain and careers page. For each one this
module works out which careers system serves its openings and returns a board (ats + slug) that a
reader in sources/ can fetch:

  1. The careers URL itself names a supported system (a Workday, Lever... link): done, no request.
  2. Open the careers page (following redirects) and look for a supported system in the final URL
     and in the page: links, iframes, scripts, embedded config.
  3. Follow up to a few "search jobs" / "view openings" links on the same site (or to a known
     careers system), then one job page, and look again: big employers often front Workday,
     Oracle or SuccessFactors with their own search page, and the Apply link gives it away.
  4. Still nothing: check the site's sitemap for job pages that carry schema.org JobPosting data
     (what Google for Jobs reads). That is a board too ("jsonld").

Every page fetch respects robots.txt and identifies Lighthouse with a link to the repo. Pages are
read for links and structured data only; nothing else is scraped. A company whose careers system
can't be read is recorded with the system's name, so the coverage report says why it's missing.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from .models import company_key, utcnow

log = logging.getLogger("lighthouse.discover")

COMPANIES_FILE = "companies.csv"   # in the config folder
MAX_PAGES = 9                       # page requests per company (robots.txt and sitemaps not counted)
MAX_BYTES = 3_000_000

# Fingerprints of careers systems, for the coverage report (supported or not). Order matters a
# little: the first match in a page is reported first.
PLATFORMS: list[tuple[str, re.Pattern]] = [(n, re.compile(p, re.I)) for n, p in (
    ("workday", r"[\w-]+\.wd\d+\.myworkday(?:jobs|site)\.com"),
    ("oracle", r"[\w.-]+\.oraclecloud\.com/hcmUI/CandidateExperience|recruitingCEJobRequisitions"),
    ("successfactors", r"successfactors\.(?:com|eu)|rmkcdn\.successfactors|sapsf\.(?:com|eu)|career\d*\.sap"),
    ("eightfold", r"[\w-]+\.eightfold\.ai|/api/apply/v2/jobs|eightfold-careers"),
    ("phenom", r"phenompeople\.com|cdn\.phenompeople|phApp\b|\"phenom"),
    ("icims", r"[\w-]+\.icims\.com"),
    ("taleo", r"[\w-]+\.taleo\.net"),
    ("avature", r"[\w-]+\.avature\.net|avatureCDN|avature\.net"),
    ("brassring", r"brassring\.com|kenexa\.com"),
    ("jobvite", r"jobs\.jobvite\.com|\bjobvite\b"),
    ("ultipro", r"recruiting\d*\.ultipro\.com|ukg\.net/careers"),
    ("adp", r"workforcenow\.adp\.com|myjobs\.adp\.com|recruiting\.adp\.com"),
    ("dayforce", r"dayforcehcm\.com"),
    ("cornerstone", r"[\w-]+\.csod\.com"),
    ("talentbrew", r"tbcdn\.talentbrew\.com|talentbrew|radancy"),
    ("jibe", r"jibecdn\.com|\"jibe|jibe-"),
    ("paradox", r"paradox\.ai"),
    ("greenhouse", r"(?:job-)?boards(?:-api)?\.greenhouse\.io"),
    ("lever", r"jobs\.lever\.co"),
    ("ashby", r"jobs\.ashbyhq\.com"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com"),
    ("workable", r"apply\.workable\.com"),
    ("recruitee", r"[\w-]+\.recruitee\.com"),
)]
_JOBPOSTING = re.compile(r"\"@type\"\s*:\s*\[?\s*\"JobPosting\"", re.I)
_JOBISH_TEXT = re.compile(
    r"\b(search (all )?(jobs|roles|openings|positions|careers)|view (all )?(jobs|openings|positions|roles|opportunities)|"
    r"(see|browse|explore|find) (all |our |open )?(jobs|roles|openings|positions|opportunities|careers)|"
    r"open (positions|roles|jobs)|job (search|openings|opportunities|listings)|current (openings|opportunities|vacancies)|"
    r"career opportunities|apply now|join (us|our team)|find (a|your) (job|role|career)|all jobs|jobs)\b", re.I)
_JOBISH_HREF = re.compile(r"(/jobs?\b|/careers?\b|/search|/openings|/positions|/opportunities|/vacancies|job[-_]?search)", re.I)
_JOB_PAGE_HREF = re.compile(r"/(?:job|jobs|position|positions|requisition|req|opening|vacancy|careers?/job)s?/[^/?#]{3,}", re.I)


@dataclass
class Result:
    company: str
    group: str = ""
    domain: str = ""
    careers_url: str = ""
    final_url: str = ""
    ats: str = ""
    slug: str = ""
    status: str = ""            # ok | unsupported | not_found | blocked | error | pinned
    detail: str = ""
    platforms: list = field(default_factory=list)   # careers systems seen, best guess first
    jsonld: bool = False         # a page carried schema.org JobPosting data
    sitemaps: list = field(default_factory=list)
    pages: list = field(default_factory=list)       # [url, http status] fetched, in order
    openings: int | None = None
    checked_at: str = ""


class _Links(HTMLParser):
    """Collect links (href + text), iframe/script sources and the page title."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.srcs: list[str] = []
        self.title = ""
        self._a: list | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._a = [a["href"], a.get("aria-label") or a.get("title") or ""]
        elif tag in ("iframe", "script", "frame") and a.get("src"):
            self.srcs.append(a["src"])
        elif tag == "form" and a.get("action"):
            self.srcs.append(a["action"])
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data):
        if self._a is not None:
            self._a[1] += " " + data
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], " ".join(self._a[1].split())[:120]))
            self._a = None
        elif tag == "title":
            self._in_title = False


def _site(host: str) -> str:
    """'careers.jpmorgan.com' -> 'jpmorgan.com' (last two labels; good enough for same-site checks)."""
    parts = (host or "").lower().split(".")
    return ".".join(parts[-3:]) if len(parts) > 2 and len(parts[-1]) == 2 and len(parts[-2]) <= 3 else ".".join(parts[-2:])


def platforms_in(text: str) -> list[str]:
    return [name for name, pat in PLATFORMS if pat.search(text or "")]


class Robots:
    """robots.txt per host, fetched once through the shared client (so the per-host limit applies)."""

    def __init__(self, client, agent: str):
        self.client, self.agent = client, agent
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.sitemaps: dict[str, list[str]] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            have = host in self._cache
        if not have:
            rp = None
            try:
                r = self.client.get(host + "/robots.txt", timeout=10, allow_redirects=True)
                if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
                    self.sitemaps[host] = rp.site_maps() or []
                elif r.status_code in (401, 403):
                    rp = RobotFileParser()
                    rp.disallow_all = True
            except Exception:
                rp = None   # unreachable robots.txt: treated as no rules
            with self._lock:
                self._cache[host] = rp
        rp = self._cache.get(host)
        return True if rp is None else rp.can_fetch(self.agent, url)


def load_companies(config_dir: Path | str) -> list[dict]:
    path = Path(config_dir) / COMPANIES_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    out = []
    for row in csv.DictReader(lines):
        name = (row.get("company") or "").strip()
        if not name or (row.get("enabled") or "yes").strip().lower() in ("no", "false", "0", "off"):
            continue
        out.append({"company": name, "group": (row.get("group") or "").strip(),
                    "domain": (row.get("domain") or "").strip().lower().removeprefix("www."),
                    "careers_url": (row.get("careers_url") or "").strip(),
                    "ats": (row.get("ats") or "").strip().lower(), "slug": (row.get("slug") or "").strip()})
    return out


class Discoverer:
    def __init__(self, client, agent: str, max_pages: int = MAX_PAGES):
        self.client = client
        self.robots = Robots(client, agent)
        self.max_pages = max_pages

    # ------------------------------------------------------------------ fetching
    def _get(self, url: str, res: Result):
        if len(res.pages) >= self.max_pages:
            return None
        if not self.robots.allowed(url):
            res.pages.append([url, "robots"])
            return None
        try:
            r = self.client.get(url, timeout=20, allow_redirects=True,
                                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        except Exception as exc:
            res.pages.append([url, type(exc).__name__])
            return None
        res.pages.append([url, r.status_code] + ([r.url] if r.url != url else []))
        if r.status_code >= 400 or len(r.content) > MAX_BYTES:
            return None
        return r

    # ------------------------------------------------------------------ one company
    def resolve(self, company: dict) -> Result:
        from .sources import detect, detect_in_html
        res = Result(company=company["company"], group=company.get("group", ""), domain=company.get("domain", ""),
                     careers_url=company.get("careers_url", ""), checked_at=utcnow())
        if company.get("ats") and company.get("slug"):
            res.ats, res.slug, res.status = company["ats"], company["slug"], "pinned"
            return res
        hit = detect(res.careers_url) if res.careers_url else None
        if hit:
            res.ats, res.slug, res.status, res.final_url = hit[0], hit[1], "ok", res.careers_url
            return res
        dom = res.domain
        starts = [u for u in (res.careers_url, f"https://careers.{dom}" if dom else "", f"https://jobs.{dom}" if dom else "",
                              f"https://www.{dom}/careers" if dom else "", f"https://www.{dom}" if dom else "") if u]
        seen_urls: set[str] = set()
        queue: list[tuple[int, int, str]] = []   # (priority, depth, url)
        found_platforms: list[str] = []
        home_sites = {_site(urlsplit(u).hostname or "") for u in starts}

        def note(text: str):
            for p in platforms_in(text):
                if p not in found_platforms:
                    found_platforms.append(p)

        def look(r) -> tuple[str, str] | None:
            text = r.text
            note(r.url)
            note(text[:MAX_BYTES])
            if _JOBPOSTING.search(text):
                res.jsonld = True
            return detect(r.url) or detect_in_html(text)

        start_i = 0
        while (queue or start_i < len(starts)) and len(res.pages) < self.max_pages:
            if queue:
                queue.sort()
                _, depth, url = queue.pop(0)
            else:
                url, depth = starts[start_i], 0
                start_i += 1
                if res.final_url:   # a start page already worked; others are fallbacks only
                    break
            key = url.split("#")[0].rstrip("/")
            if key in seen_urls:
                continue
            seen_urls.add(key)
            r = self._get(url, res)
            if r is None:
                continue
            res.final_url = res.final_url or r.url
            home_sites.add(_site(urlsplit(r.url).hostname or ""))
            hit = look(r)
            if hit:
                res.ats, res.slug = hit
                break
            if depth >= 2:
                continue
            p = _Links()
            try:
                p.feed(r.text[:MAX_BYTES])
            except Exception:
                pass
            for src in p.srcs:
                note(src)
            for href, text in p.links:
                href = unescape(href.strip())
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                absu = urljoin(r.url, href)
                host = (urlsplit(absu).hostname or "").lower()
                if not absu.startswith("https://"):
                    continue
                hit = detect(absu)
                if hit:   # a direct link to a supported board
                    queue.append((0, depth + 1, absu))
                    continue
                known = bool(platforms_in(host))
                if not known and _site(host) not in home_sites:
                    continue
                if _JOB_PAGE_HREF.search(absu) and depth >= 1:
                    queue.append((2, depth + 1, absu))    # one job page: its Apply link names the system
                elif _JOBISH_TEXT.search(text) or _JOBISH_HREF.search(urlsplit(absu).path) or known:
                    queue.append((1 if known or _JOBISH_TEXT.search(text) else 3, depth + 1, absu))
        res.platforms = found_platforms
        host = urlsplit(res.final_url).netloc
        res.sitemaps = self.robots.sitemaps.get(f"https://{host}", [])[:5] if host else []
        if res.ats:
            res.status = "ok"
        elif not res.pages or all(not isinstance(p[1], int) or p[1] >= 400 for p in res.pages):
            res.status = "blocked" if any(p[1] == "robots" for p in res.pages) else "error"
            res.detail = "careers page didn't open: " + ", ".join(f"{p[0]} → {p[1]}" for p in res.pages[:3])
        elif found_platforms:
            res.status = "unsupported"
            res.detail = f"uses {found_platforms[0]}"
        else:
            res.status = "not_found"
            res.detail = "no careers system recognised on the careers page"
        return res


def run(companies: list[dict], client, agent: str, workers: int = 16, only: str | None = None,
        on_result=None) -> list[Result]:
    d = Discoverer(client, agent)
    todo = [c for c in companies if not only or only.lower() in c["company"].lower()]
    out: list[Result] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(d.resolve, c): c for c in todo}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                r = fut.result()
            except Exception as exc:   # one bad site never stops the run
                r = Result(company=c["company"], group=c.get("group", ""), careers_url=c.get("careers_url", ""),
                           status="error", detail=f"{type(exc).__name__}: {exc}"[:300], checked_at=utcnow())
            out.append(r)
            if on_result:
                on_result(r)
    out.sort(key=lambda r: r.company.lower())
    return out


def summary(results: list[Result]) -> dict:
    from collections import Counter
    return {"companies": len(results), "status": dict(Counter(r.status for r in results)),
            "resolved_ats": dict(Counter(r.ats for r in results if r.ats)),
            "unsupported": dict(Counter((r.platforms or ["?"])[0] for r in results if r.status == "unsupported")),
            "jsonld_pages": sum(1 for r in results if r.jsonld and not r.ats)}


def to_json(r: Result) -> str:
    return json.dumps(asdict(r), ensure_ascii=False)
