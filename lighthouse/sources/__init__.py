"""Job sources. Each adapter turns one board's public feed into a list of Job objects.

To add an ATS: subclass sources.base.Source (name, label, url_patterns, fetch) and
register it in SOURCES below. Nothing else needs to change.
"""

from __future__ import annotations

import re

from .amazon import AmazonSource
from .ashby import AshbySource
from .base import BoardNotFound, Source, SourceError
from .eightfold import EightfoldSource
from .jibe import JibeSource
from .jsonld import JsonLdSource
from .oracle import OracleSource
from .phenom import PhenomSource
from .successfactors import SuccessFactorsSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .recruitee import RecruiteeSource
from .smartrecruiters import SmartRecruitersSource
from .workable import WorkableSource
from .workday import WorkdaySource

SOURCES: dict[str, type[Source]] = {cls.name: cls for cls in (
    GreenhouseSource, LeverSource, AshbySource, WorkableSource, SmartRecruitersSource, RecruiteeSource,
    WorkdaySource, OracleSource, SuccessFactorsSource, PhenomSource, JibeSource, EightfoldSource, AmazonSource,
    JsonLdSource,
)}

# Embedded-board patterns we look for when a careers page is on the company's own domain.
_EMBED_HINTS = (
    ("greenhouse", r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)?([\w-]+)"),
    ("lever", r"jobs\.lever\.co/([\w.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)"),
    ("workable", r"apply\.workable\.com/([\w-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"),
    ("recruitee", r"([\w-]+)\.recruitee\.com"),
    ("workday", r"((?:https?://)?(?:[\w-]+\.wd\d+\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?[\w-]+|"
                r"wd\d+\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/[\w-]+/[\w-]+))"),
    ("oracle", r"((?:https?://)?[\w-]+\.fa(?:\.[\w-]+)*\.oraclecloud\.com/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Za-z]{2})?/sites/[\w-]+)"),
)


def detect(text: str) -> tuple[str, str] | None:
    """'https://jobs.lever.co/palantir' -> ('lever', 'palantir'). Also accepts 'lever:palantir'."""
    text = (text or "").strip()
    m = re.fullmatch(r"([a-z]+)\s*[:/]\s*([\w.%/-]+)", text, flags=re.I)
    if m and m.group(1).lower() in SOURCES and "." not in m.group(1):
        return m.group(1).lower(), m.group(2)
    for name, cls in SOURCES.items():
        slug = cls.detect(text)
        if slug:
            return name, slug
    return None


def detect_in_html(html: str) -> tuple[str, str] | None:
    """Find an embedded ATS board in a careers page's HTML."""
    for name, pat in _EMBED_HINTS:
        for m in re.finditer(pat, html or "", flags=re.I):
            slug = m.group(1)
            if name in ("workday", "oracle"):
                slug = SOURCES[name].detect(slug)
                if slug:
                    return name, slug
                continue
            if slug.lower() not in SOURCES[name].reserved_slugs and slug.lower() not in ("www", "embed"):
                return name, slug
    return None


__all__ = ["SOURCES", "Source", "SourceError", "BoardNotFound", "detect", "detect_in_html"]
