"""Recruitee public careers-site API — free, no key."""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source, interval, num, workplace

API = "https://{slug}.recruitee.com/api/offers/"


class RecruiteeSource(Source):
    name = "recruitee"
    label = "Recruitee"
    url_patterns = (r"(?P<slug>[\w-]+)\.recruitee\.com",)
    reserved_slugs = Source.reserved_slugs | {"www", "app", "support", "blog"}

    def fetch(self, slug, known=None):
        self.known = known or self.known
        data = self._get_json(API.format(slug=slug), board=True)
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
