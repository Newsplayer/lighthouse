"""SmartRecruiters public Posting API — free, no key.

The list endpoint is paginated (100 per page) and has no description, so each posting
needs one detail call. To stay fast on big employers (some list 5,000+ roles) we only
call the detail endpoint for postings that are new or whose releasedDate changed, and at
most `max_detail_calls` per run; the rest are listed title-only and filled in next run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, workplace

LIST = "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}"
DETAIL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}"
SECTIONS = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


class SmartRecruitersSource(Source):
    name = "smartrecruiters"
    label = "SmartRecruiters"
    url_patterns = (
        r"(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[\w-]+)",
        r"api\.smartrecruiters\.com/v1/companies/(?P<slug>[\w-]+)",
    )

    def fetch(self, slug, known=None):
        known = known or {}
        items, offset = [], 0
        while True:
            page = self._get_json(LIST.format(slug=slug, offset=offset))
            content = page.get("content") or []
            items += content
            offset += len(content)
            if not content or offset >= int(page.get("totalFound") or 0):
                break
        if not items:
            raise BoardNotFound("SmartRecruiters: no public postings for this company id "
                                "(ids are case-sensitive, e.g. BoschGroup)")
        jobs = [self.parse_summary(slug, raw) for raw in items]
        todo = [j for j in jobs if known.get(j.uid, ("", False)) != (j.detail_marker, True)]
        todo = todo[: self.max_detail_calls]
        with ThreadPoolExecutor(max_workers=4) as pool:  # per-host throttle still applies
            for job, detail in zip(todo, pool.map(self._detail, todo)):
                if detail:
                    self.apply_detail(job, detail)
        return jobs

    def _detail(self, job: Job) -> dict | None:
        try:
            return self._get_json(DETAIL.format(slug=job.source_board, id=job.external_id))
        except Exception:
            return None  # leave it title-only; retried next run

    def parse_summary(self, slug: str, raw: dict) -> Job:
        loc = raw.get("location") or {}
        full = loc.get("fullLocation") or ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
        return Job(
            source="smartrecruiters",
            source_board=slug,
            external_id=str(raw["id"]),
            title=(raw.get("name") or "").strip(),
            company=self.company or (raw.get("company") or {}).get("name") or slug,
            location=full,
            url=f"https://jobs.smartrecruiters.com/{slug}/{raw['id']}",
            content_html="",
            departments=[d for d in ((raw.get("department") or {}).get("label"),
                                     (raw.get("function") or {}).get("label")) if d],
            posted_at=parse_dt(raw.get("releasedDate")),
            workplace=workplace(remote=loc.get("remote"), hybrid=loc.get("hybrid")) or (
                "onsite" if loc.get("remote") is False and loc.get("hybrid") is False else None),
            employment_type=(raw.get("typeOfEmployment") or {}).get("label"),
            locations=[full] if full else [],
            detail_marker=str(raw.get("releasedDate") or ""),
        )

    @staticmethod
    def apply_detail(job: Job, detail: dict) -> None:
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        parts = []
        for key in SECTIONS:
            sec = sections.get(key) or {}
            if sec.get("text"):
                parts.append(f"<h3>{sec.get('title') or ''}</h3>{sec['text']}")
        job.content_html = sanitize_html("".join(parts))
        job.url = detail.get("postingUrl") or detail.get("applyUrl") or job.url
