"""Add boards from whatever the user has: an ATS URL, a company careers page, or 'ats:slug'.

Slug discovery is the real bottleneck for coverage, so this accepts many lines at once
(paste a list, or `python -m lighthouse add urls.txt`).
"""

from __future__ import annotations

import re

from . import db
from .net import BlockedHost, default_client
from .sources import SOURCES, BoardNotFound, detect, detect_in_html


class AddError(Exception):
    pass


def resolve(text: str) -> tuple[str, str]:
    """Work out (ats, slug). For a careers page on the company's own domain, the page is
    fetched once (at your request) and scanned for an embedded ATS board."""
    text = text.strip()
    hit = detect(text)
    if hit:
        return hit
    if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+", text):
        url = text if text.startswith("http") else f"https://{text}"
        try:
            r = default_client().get(url, allow_once=True, timeout=15)
            hit = detect_in_html(r.text) or detect(r.url)
        except (BlockedHost, Exception) as exc:
            raise AddError(f"Couldn't open {url}: {exc}") from exc
        if hit:
            return hit
        raise AddError(f"No supported job board found on {url}. Supported: {', '.join(SOURCES)}.")
    raise AddError(f"Not a URL or 'ats:slug': {text!r}")


def validate(ats: str, slug: str) -> int:
    src = SOURCES[ats](max_detail_calls=0)
    try:
        return len(src.fetch(slug))
    except BoardNotFound as exc:
        raise AddError(f"{ats}:{slug} — {exc}") from exc
    except Exception as exc:
        raise AddError(f"{ats}:{slug} — {exc}") from exc


def add_many(conn, lines: list[str], origin: str = "ui", check: bool = True) -> list[dict]:
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            ats, slug = resolve(line)
            count = validate(ats, slug) if check else None
            db.add_board(conn, ats, slug, origin=origin)
            out.append({"input": line, "ats": ats, "slug": slug, "count": count})
        except AddError as exc:
            out.append({"input": line, "error": str(exc)})
    return out
