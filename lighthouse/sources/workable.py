"""Workable public widget API — free, no key."""

from __future__ import annotations

from ..models import Job, parse_dt
from ..sanitize import sanitize_html
from .base import Source, workplace

API = "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"


def _loc(l: dict) -> str:
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
        data = self._get_json(API.format(slug=slug), board=True)
        company, out = self.company_name(slug, data.get("name")), []
        for raw in data.get("jobs", []):
            uid = f"workable:{slug}:{raw.get('shortcode') or raw.get('id')}"
            if not self.keep(uid, raw.get("title"), [_loc(l) for l in raw.get("locations") or []] or [_loc(raw)]):
                continue
            same, marker = self.unchanged(uid, raw.get("description"))
            job = self.parse(slug, raw, company, skip_body=same)
            job.detail_marker = marker
            out.append(job)
        return out

    @staticmethod
    def parse(slug: str, raw: dict, company: str, skip_body: bool = False) -> Job:
        locs = [_loc(l) for l in raw.get("locations") or [] if not l.get("hidden")]
        main = locs[0] if locs else _loc(raw)
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
