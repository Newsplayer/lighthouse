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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from .models import utcnow

log = logging.getLogger("lighthouse.discover")

COMPANIES_FILE = "companies.csv"   # in the config folder
MAX_PAGES = 9                       # page requests per company (robots.txt and sitemaps not counted)
MAX_BYTES = 3_000_000
PARSE_BYTES = 1_000_000             # links are looked for in the first 1 MB of a page

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


_LOCALE = re.compile(r"/([a-z]{2})[/_-]([a-z]{2})(?=[/._-]|$)", re.I)


def sitemap_rank(url: str) -> int:
    """Careers sites often publish one sitemap per country and language (/pl/pl/, /tw/zh/, /dach/de/),
    in no useful order. Taking the first gave Corning's Polish site and DuPont's Taiwanese one, whose
    jobs are all outside the countries we cover. Prefer the US English one, then sitemaps with no
    country in the path, then other English ones, then the rest."""
    path = urlsplit(url).path.lower()
    pairs = {(a.lower(), b.lower()) for a, b in _LOCALE.findall(path)}
    if pairs & {("us", "en"), ("en", "us")}:
        return 0
    if not pairs and not re.search(r"/(dach|emea|apac|latam)(/|$)", path):
        return 1
    if any("en" in p for p in pairs) or pairs & {("ca", "fr"), ("fr", "ca"), ("mx", "es"), ("es", "mx")}:
        return 2
    return 3


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


_GENERIC_WORDS = {"inc", "corp", "corporation", "company", "companies", "co", "group", "holdings", "holding", "the",
                  "of", "and", "international", "global", "financial", "services", "service", "technologies",
                  "technology", "systems", "brands", "industries", "enterprises", "partners", "resources", "energy",
                  "health", "healthcare", "bank", "capital", "management", "solutions", "america", "american",
                  "united", "national", "us", "usa", "llc", "plc", "ltd", "stores", "foods", "insurance", "trust"}


def board_keys(ats: str, slug: str) -> list[str]:
    """The parts of a board's id that can name its owner: a Workday tenant and site name, an Oracle
    site name, an ATS board slug, a careers host."""
    s = (slug or "").lower()
    keys = []
    if ats == "workday":
        tenant, _, site = s.partition("/")
        keys = [tenant.split(".")[0], site]
    elif ats in ("oracle", "successfactors", "jibe", "phenom", "eightfold", "jsonld"):
        host = urlsplit(s if "://" in s else "https://" + s).hostname or s
        labels = [x for x in host.split(".") if x not in ("www", "careers", "jobs", "career", "job", "com", "net", "org")]
        keys = [labels[0] if labels else host]
        if ats == "oracle" and "/" in s:
            keys.append(s.split("/", 1)[1])                  # e.g. 'BNY-Careers', 'UberCareers'
    else:
        keys = [s]
    out = []
    for k in keys:
        k = re.sub(r"(?:external|careers?|jobs?|site|search|global|cx\d*)", "", re.sub(r"[^a-z0-9]", "", k))
        if len(k) >= 2:
            out.append(k)
    return out


def belongs(company: str, domain: str, ats: str, slug: str) -> bool:
    return any(name_matches(company, domain, k) for k in board_keys(ats, slug))


def name_matches(company: str, domain: str, key: str) -> bool:
    """Does a board id look like it belongs to this company? ('jpmc' / JPMorgan Chase: yes;
    'geico' / Berkshire Hathaway: no). Used to keep a careers page's links to subsidiaries',
    portfolio companies' or partners' boards from being taken as the company's own."""
    k = re.sub(r"[^a-z0-9]", "", (key or "").lower())
    if len(k) < 2:
        return False
    words = re.findall(r"[a-z0-9]+", (company or "").lower().replace("&", " and ").replace("'", ""))
    label = re.sub(r"[^a-z0-9]", "", (domain or "").lower().split(".")[0])
    names = {"".join(words), label} | {w for w in words if len(w) >= 3 and w not in _GENERIC_WORDS}
    for n in names:
        if n == k or (len(n) >= 3 and (n in k or (len(k) >= 3 and k in n))):
            return True
    initials = "".join(w[0] for w in words if w not in ("the", "of", "and"))
    if len(initials) >= 2 and k.startswith(initials):
        return True
    full = "".join(words)
    if 3 <= len(k) <= 6 and full[:1] == k[0]:      # an abbreviation: 'jpmc', 'uhg', 'mpc'
        it = iter(full)
        return all(ch in it for ch in k)
    return False


# A cheap substring that every match of the platform's pattern contains: the regex runs only when
# it's present (pages can be megabytes, and dozens of patterns per page add up across 700 sites).
_GATES = {"workday": "myworkday", "oracle": "oraclecloud", "successfactors": "sap", "eightfold": "eightfold",
          "phenom": "ph", "icims": "icims", "taleo": "taleo", "avature": "avature", "brassring": ("brassring", "kenexa"),
          "jobvite": "jobvite", "ultipro": ("ultipro", "ukg"), "adp": "adp.com", "dayforce": "dayforce",
          "cornerstone": "csod", "talentbrew": ("talentbrew", "radancy"), "jibe": "jibe", "paradox": "paradox",
          "greenhouse": "greenhouse", "lever": "lever.co", "ashby": "ashbyhq", "smartrecruiters": "smartrecruiters",
          "workable": "workable", "recruitee": "recruitee"}


def platforms_in(text: str) -> list[str]:
    low = (text or "").lower()
    out = []
    for name, pat in PLATFORMS:
        gate = _GATES.get(name, "")
        if gate and not any(g in low for g in ((gate,) if isinstance(gate, str) else gate)):
            continue
        if name == "successfactors" and "successfactors" not in low and "sapsf" not in low and "career" not in low:
            continue
        if pat.search(text):
            out.append(name)
    return out


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
                r = self.client.get(host + "/robots.txt", timeout=10, allow_redirects=True, deadline=20,
                                    max_bytes=2_000_000)
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
            r = self.client.get(url, allow_redirects=True, deadline=30, max_bytes=MAX_BYTES,
                                headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
        except Exception as exc:
            res.pages.append([url, type(exc).__name__])
            return None
        res.pages.append([url, r.status_code] + ([r.url] if r.url != url else []))
        if r.status_code >= 400 or len(r.content) > MAX_BYTES:
            return None
        return r

    # ------------------------------------------------------------------ systems recognised from a page
    def _special(self, r, platforms: list[str]) -> tuple[str, str] | None:
        from .sources import eightfold, oracle, phenom
        text, host = r.text, (urlsplit(r.url).hostname or "").lower()
        if host.endswith("amazon.jobs"):
            return "amazon", "amazon.jobs"
        o = oracle.from_page(text)
        if o:
            return "oracle", o
        p = phenom.from_page(text)
        if p:
            return self._phenom_board(p)
        e = eightfold.from_page(text)
        if e:
            return "eightfold", e
        low = text[:PARSE_BYTES].lower()
        if "rmkcdn.successfactors" in low or "/platform/js/j2w/" in low:
            return "successfactors", host
        if "jibe" in platforms and ("jibecdn" in low or "/api/jobs" in low or '"jibe' in low):
            return "jibe", host
        return None

    def _phenom_board(self, slug: str) -> tuple[str, str]:
        """A Phenom site often fronts Workday (or another supported system): its Apply links say so."""
        from collections import Counter
        from .sources import detect
        from .sources.phenom import PhenomSource
        try:
            jobs = PhenomSource(client=self.client).list_jobs(slug, size=25, limit=25)
        except Exception:
            return "phenom", slug
        hits = Counter(detect(j.get("applyUrl") or "") for j in jobs if j.get("applyUrl"))
        hits.pop(None, None)
        if hits:
            board, n = hits.most_common(1)[0]
            if n >= max(2, len(jobs) // 2):
                return board
        return "phenom", slug

    def _confirm(self, hit: tuple[str, str], res: Result) -> bool:
        ats, slug = hit
        if ats not in _CONFIRM:
            return True
        from .sources import SOURCES
        try:
            n = SOURCES[ats](client=self.client, max_detail_calls=0).peek(slug)
        except Exception as exc:
            res.detail = f"{ats} board {slug} didn't answer: {type(exc).__name__}"[:300]
            return False
        if not n:
            res.detail = f"{ats} board {slug} has no openings"
            return False
        res.openings = n
        return True

    def _jsonld(self, host: str, res: Result) -> tuple[str, str] | None:
        """Last resort: a sitemap of job pages that carry schema.org JobPosting data."""
        maps = list(dict.fromkeys(self.robots.sitemaps.get(f"https://{host}", []) + [f"https://{host}/sitemap.xml"]))
        if maps and min(map(sitemap_rank, maps)) > 0:   # only other countries listed: the US one is usually there too
            us = [u for u in dict.fromkeys(_LOCALE.sub("/us/en", m, count=1) for m in maps if sitemap_rank(m) >= 2)
                  if u not in maps]
            maps = us[:1] + maps
        maps.sort(key=sitemap_rank)       # a stable sort: the site's own order within each rank
        for sm in maps[:3]:
            if not sm.startswith("https://") or not self.robots.allowed(sm):
                continue
            if self._confirm(("jsonld", sm), res):
                return "jsonld", sm
        return None

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
        others: list[tuple[str, str]] = []       # working boards under another name (subsidiaries, partners...)
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
            special = None if hit else self._special(r, found_platforms)
            cand = hit or special
            if cand and self._confirm(cand, res):
                own = (belongs(res.company, res.domain, *cand)
                       # read off the company's own careers site (Oracle, SuccessFactors, Phenom... front ends)
                       or (special is not None and _site(urlsplit(r.url).hostname or "") in home_sites)
                       # one of the company's own start pages redirected straight to the board
                       or (depth == 0 and r.url != url and detect(r.url) == cand))
                if own:
                    res.ats, res.slug = cand
                    break
                if cand not in others:
                    others.append(cand)
            if depth >= 2:
                continue
            p = _Links()
            try:
                p.feed(r.text[:PARSE_BYTES])
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
        if not res.ats and host:
            hit = self._jsonld(host, res)        # the company's own job pages beat a board under another name
            if hit:
                res.ats, res.slug = hit
        lone = len(others) == 1 and others[0][0] not in _SMALL_BOARDS
        if not res.ats and lone:
            res.ats, res.slug = others[0]
            res.detail = (f"the only board found is named {others[0][0]}:{others[0][1]}, not after the company: "
                          "pin the right one in companies.csv if it isn't theirs")
        elif not res.ats and others:
            res.detail = ("links to boards under other names (" + ", ".join(f"{a}:{s}" for a, s in others[:4])
                          + "), e.g. subsidiaries: pin the right one in companies.csv")
        if res.ats:
            res.status = "ok"
        elif not res.pages or all(not isinstance(p[1], int) or p[1] >= 400 for p in res.pages):
            res.status = "blocked" if any(p[1] == "robots" for p in res.pages) else "error"
            res.detail = "careers page didn't open: " + ", ".join(f"{p[0]} → {p[1]}" for p in res.pages[:3])
        elif others:
            res.status = "not_found"      # several boards under other names: res.detail lists them
        elif found_platforms:
            res.status = "unsupported"
            res.detail = f"uses {found_platforms[0]}"
        else:
            res.status = "not_found"
            res.detail = "no careers system recognised on the careers page"
        return res


def discovery_client(per_host_rps: float = 4.0):
    """Careers sites vary: a short timeout and one retry keep a dead site from costing minutes."""
    from .net import Client, open_network
    return open_network(Client(timeout=10, per_host_rps=per_host_rps, retries=1, deadline=45, max_bytes=60_000_000))


# Systems where a careers page's link often goes to a partner's or portfolio company's board: a
# board there under another name is never taken as the company's own without pinning it.
_SMALL_BOARDS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"}

# Careers systems whose boards are inferred from a page: confirmed with one cheap request first.
_CONFIRM = {"oracle", "successfactors", "phenom", "jibe", "eightfold", "amazon", "jsonld"}


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
