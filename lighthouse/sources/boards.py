"""Job boards that answer in one request, through each system's public, keyless API: Greenhouse, Lever,
Ashby, Workable and Recruitee. One request returns every open posting with its description, so these
boards are read every build, and an unchanged board is skipped by the hash of its response.
"""

from __future__ import annotations

import html

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source, interval, num, workplace


# ================================================================ Greenhouse: https://developers.greenhouse.io/job-board.html

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


class GreenhouseSource(Source):
    name = "greenhouse"
    label = "Greenhouse"
    single_response = True
    shared_host = True
    url_patterns = (
        r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?(?P<slug>[\w-]+)",
        r"(?:job-)?boards\.greenhouse\.io/embed/job_board\?for=(?P<slug>[\w-]+)",
    )

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(GREENHOUSE_API.format(slug=slug), board=True)
        out = []
        for raw in data.get("jobs", []):
            uid = f"greenhouse:{slug}:{raw['id']}"
            if not self.keep(uid, raw.get("title"), [(raw.get("location") or {}).get("name")]):
                continue
            same, marker = self.unchanged(uid, raw.get("content"))
            job = self.parse(slug, raw, self.company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str | None = None, skip_body: bool = False) -> Job:
        # Greenhouse returns `content` HTML-escaped (&lt;p&gt;...), so unescape before sanitizing.
        body = "" if skip_body else sanitize_html(html.unescape(raw.get("content") or ""))
        return Job(
            source="greenhouse",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("title") or "").strip(),
            company=company or raw.get("company_name") or slug.title(),
            location=((raw.get("location") or {}).get("name") or "").strip(),
            url=raw.get("absolute_url") or "",
            content_html=body,
            departments=[d["name"] for d in raw.get("departments") or [] if d.get("name")],
            posted_at=parse_dt(raw.get("first_published")),
            updated_at=parse_dt(raw.get("updated_at")),
        )


# ================================================================ Lever: https://github.com/lever/postings-api

LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"


class LeverSource(Source):
    name = "lever"
    label = "Lever"
    single_response = True
    shared_host = True
    url_patterns = (
        r"jobs\.lever\.co/(?P<slug>[\w.-]+)",
        r"api\.lever\.co/v0/postings/(?P<slug>[\w.-]+)",
    )

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(LEVER_API.format(slug=slug), board=True)
        if isinstance(data, dict):  # {"ok": false, "error": "Document not found"}
            from .base import BoardNotFound
            raise BoardNotFound(data.get("error") or "Lever: board not found")
        company, out = self.company_name(slug), []
        for raw in data:
            uid = f"lever:{slug}:{raw['id']}"
            cats = raw.get("categories") or {}
            if not self.keep(uid, raw.get("text"), cats.get("allLocations") or [cats.get("location")]):
                continue
            parts = self._parts(raw)
            same, marker = self.unchanged(uid, *parts)
            job = self.parse(slug, raw, company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def _parts(raw: dict) -> list[str]:
        parts = [raw.get("description") or ""]
        for lst in raw.get("lists") or []:
            parts.append(f"<h3>{lst.get('text') or ''}</h3><ul>{lst.get('content') or ''}</ul>")
        parts.append(raw.get("additional") or "")
        return parts

    @staticmethod
    def parse(slug: str, raw: dict, company: str, skip_body: bool = False) -> Job:
        cats = raw.get("categories") or {}
        parts = LeverSource._parts(raw)
        sal = raw.get("salaryRange") or {}
        all_locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        return Job(
            source="lever",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("text") or "").strip(),
            company=company,
            location=(cats.get("location") or "").strip(),
            url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
            content_html="" if skip_body else sanitize_html("".join(parts)),
            departments=[d for d in (cats.get("department"), cats.get("team")) if d],
            posted_at=parse_dt(raw.get("createdAt")),
            workplace=workplace(text=raw.get("workplaceType")),
            employment_type=cats.get("commitment"),
            salary_min=num(sal.get("min")),
            salary_max=num(sal.get("max")),
            salary_currency=sal.get("currency"),
            salary_interval=interval(sal.get("interval")),
            locations=[l for l in all_locs if l],
        )


# ================================================================ Ashby (includes pay): https://developers.ashbyhq.com/docs/public-job-posting-api

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


class AshbySource(Source):
    name = "ashby"
    label = "Ashby"
    single_response = True
    shared_host = True
    url_patterns = (
        r"jobs\.ashbyhq\.com/(?P<slug>[\w.%-]+)",
        r"api\.ashbyhq\.com/posting-api/job-board/(?P<slug>[\w.%-]+)",
    )

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(ASHBY_API.format(slug=slug), board=True)
        company, out = self.company_name(slug), []
        for raw in data.get("jobs", []):
            if not raw.get("isListed", True):
                continue
            uid = f"ashby:{slug}:{raw['id']}"
            locs = [raw.get("location")] + [s.get("location") for s in raw.get("secondaryLocations") or []]
            if not self.keep(uid, raw.get("title"), locs):
                continue
            same, marker = self.unchanged(uid, raw.get("descriptionHtml"))
            job = self.parse(slug, raw, company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str, skip_body: bool = False) -> Job:
        comp = raw.get("compensation") or {}
        salary = None
        for tier in comp.get("compensationTiers") or []:
            for c in tier.get("components") or []:
                if c.get("compensationType") == "Salary" and (c.get("minValue") or c.get("maxValue")):
                    salary = c
                    break
            if salary:
                break
        salary = salary or {}
        locs = [raw.get("location") or ""] + [s.get("location") or "" for s in raw.get("secondaryLocations") or []]
        wp = workplace(text=raw.get("workplaceType")) or ("remote" if raw.get("isRemote") else None)
        return Job(
            source="ashby",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("title") or "").strip(),
            company=company,
            location=(raw.get("location") or "").strip(),
            url=raw.get("jobUrl") or raw.get("applyUrl") or "",
            content_html="" if skip_body else sanitize_html(raw.get("descriptionHtml") or ""),
            departments=[d for d in (raw.get("department"), raw.get("team")) if d],
            posted_at=parse_dt(raw.get("publishedAt")),
            workplace=wp,
            employment_type=raw.get("employmentType"),
            salary_min=num(salary.get("minValue")),
            salary_max=num(salary.get("maxValue")),
            salary_currency=salary.get("currencyCode"),
            salary_interval=interval(salary.get("interval")),
            locations=[l.strip() for l in locs if l.strip()],
        )


# ================================================================ Workable (its public widget API)

WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def _workable_loc(l: dict) -> str:
    return ", ".join(x for x in (l.get("city"), l.get("region") or l.get("state"), l.get("country")) if x)


class WorkableSource(Source):
    name = "workable"
    label = "Workable"
    single_response = True
    shared_host = True
    url_patterns = (
        r"apply\.workable\.com/api/v1/widget/accounts/(?P<slug>[\w-]+)",
        r"apply\.workable\.com/(?P<slug>[\w-]+)",
        r"(?P<slug>[\w-]+)\.workable\.com",
    )
    reserved_slugs = Source.reserved_slugs | {"apply", "www"}

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(WORKABLE_API.format(slug=slug), board=True)
        company, out = self.company_name(slug, data.get("name")), []
        for raw in data.get("jobs", []):
            uid = f"workable:{slug}:{raw.get('shortcode') or raw.get('id')}"
            if not self.keep(uid, raw.get("title"), [_workable_loc(l) for l in raw.get("locations") or []] or [_workable_loc(raw)]):
                continue
            same, marker = self.unchanged(uid, raw.get("description"))
            job = self.parse(slug, raw, company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str, skip_body: bool = False) -> Job:
        locs = [_workable_loc(l) for l in raw.get("locations") or [] if not l.get("hidden")]
        main = locs[0] if locs else _workable_loc(raw)
        return Job(
            source="workable",
            source_board=slug,
            external_id=str(raw.get("shortcode") or raw.get("id")),
            title=(raw.get("title") or "").strip(),
            company=company,
            location=main,
            url=raw.get("url") or raw.get("shortlink") or "",
            content_html="" if skip_body else sanitize_html(raw.get("description") or ""),
            departments=[d for d in (raw.get("department"), raw.get("function")) if d],
            posted_at=parse_dt(raw.get("published_on") or raw.get("created_at")),
            workplace=workplace(remote=bool(raw.get("telecommuting")) or None),
            employment_type=raw.get("employment_type") or None,
            locations=[l for l in locs if l] or ([main] if main else []),
        )


# ================================================================ Recruitee (its public careers-site API)

RECRUITEE_API = "https://{slug}.recruitee.com/api/offers/"


class RecruiteeSource(Source):
    name = "recruitee"
    label = "Recruitee"
    single_response = True
    shared_host = True
    url_patterns = (r"(?P<slug>[\w-]+)\.recruitee\.com",)
    reserved_slugs = Source.reserved_slugs | {"www", "app", "support", "blog"}

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(RECRUITEE_API.format(slug=slug), board=True)
        out = []
        for raw in data.get("offers", []):
            if raw.get("status", "published") != "published":
                continue
            uid = f"recruitee:{slug}:{raw['id']}"
            if not self.keep(uid, raw.get("title"), [raw.get("location")]):
                continue
            same, marker = self.unchanged(uid, raw.get("description"), raw.get("requirements"))
            job = self.parse(slug, raw, self.company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str | None = None, skip_body: bool = False) -> Job:
        sal = raw.get("salary") or {}
        locs = [", ".join(x for x in (l.get("city") or l.get("name"), l.get("state"), l.get("country")) if x)
                for l in raw.get("locations") or []]
        if raw.get("remote"):
            wp = "remote"
        elif raw.get("hybrid"):
            wp = "hybrid"
        elif raw.get("on_site"):
            wp = "onsite"
        else:
            wp = None
        body = (raw.get("description") or "") + (
            f"<h3>Requirements</h3>{raw['requirements']}" if raw.get("requirements") else "")
        return Job(
            source="recruitee",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("title") or "").strip(),
            company=company or raw.get("company_name") or slug.title(),
            location=(raw.get("location") or "").strip(),
            url=raw.get("careers_url") or "",
            content_html="" if skip_body else sanitize_html(body),
            departments=[raw["department"]] if raw.get("department") else [],
            posted_at=parse_dt(raw.get("published_at") or raw.get("created_at")),
            updated_at=parse_dt(raw.get("updated_at")),
            workplace=workplace(text=wp),
            employment_type=raw.get("employment_type_code"),
            salary_min=num(sal.get("min")),
            salary_max=num(sal.get("max")),
            salary_currency=sal.get("currency"),
            salary_interval=interval(sal.get("period")),
            locations=[l for l in locs if l],
        )
