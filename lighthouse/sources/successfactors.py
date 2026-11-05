"""SAP SuccessFactors career sites ("RMK") — one public feed per site, no key.

Every SuccessFactors career site (jobs.exxonmobil.com, jobs.deere.com, ...) publishes /sitemap.xml
as a Google-Base RSS feed of all its openings, each with the full description. One request reads
the whole board.

    slug = "<careers host>"      e.g. "jobs.exxonmobil.com"
"""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET

from ..models import Job
from ..sanitize import sanitize_html
from .base import BoardNotFound, Source, Unchanged

G = "{http://base.google.com/ns/1.0}"
_TRAILING_PLACE = re.compile(r"\s*\(([^()]*,[^()]*)\)\s*$")


class SuccessFactorsSource(Source):
    name = "successfactors"
    label = "SAP SuccessFactors"

    @classmethod
    def detect(cls, url: str) -> str | None:
        return None   # career sites live on company domains; discovery recognises them by their page

    def peek(self, slug):
        host = slug.strip().lower().strip("/")
        r = self.client.get(f"https://{host}/sitemap.xml", headers={"Accept": "application/rss+xml, text/xml"})
        r.raise_for_status()
        return r.text.count("<item>") if "<rss" in r.text[:500] else 0

    def fetch(self, slug, known=None):
        host = slug.strip().lower().removeprefix("https://").strip("/")
        r = self.client.get(f"https://{host}/sitemap.xml", headers={"Accept": "application/rss+xml, text/xml"})
        if r.status_code == 404:
            raise BoardNotFound("SuccessFactors: no job feed on this site")
        r.raise_for_status()
        self.body_hash = hashlib.blake2b(r.content, digest_size=16).hexdigest()
        if self.prev_body_hash and self.prev_body_hash == self.body_hash:
            raise Unchanged()
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as exc:
            raise BoardNotFound(f"SuccessFactors: feed isn't XML ({exc})") from exc
        items = root.findall("./channel/item")
        if not items:
            raise BoardNotFound("SuccessFactors: feed has no openings")
        company = self.company or (root.findtext("./channel/title") or "").replace("Careers at ", "").strip() or host
        jobs = []
        for it in items:
            j = self.parse_item(host, it, company)
            if j and self.wanted(j):
                jobs.append(j)
        return jobs

    def parse_item(self, host: str, it, company: str) -> Job | None:
        link = (it.findtext("link") or "").strip()
        jid = (it.findtext(f"{G}id") or it.findtext("guid") or "").strip() or link.rstrip("/").rsplit("/", 1)[-1]
        if not jid:
            return None
        title = html.unescape(it.findtext("title") or "").strip()
        place = (it.findtext(f"{G}location") or "").strip()
        m = _TRAILING_PLACE.search(title)
        if m:
            place = place or m.group(1)
            title = title[: m.start()].strip()
        desc = html.unescape(it.findtext("description") or "")   # escaped HTML inside CDATA
        same, marker = self.unchanged(f"successfactors:{host}:{jid}", desc)
        return Job(source="successfactors", source_board=host, external_id=jid, title=title, company=company,
                   location=place, url=link or f"https://{host}/job/{jid}/",
                   content_html="" if same else sanitize_html(desc), locations=[place] if place else [],
                   detail_marker=marker)
