"""Structured fields from a posting: salary, workplace, years of experience, clearance,
citizenship, degree, visa-sponsorship language, pipeline wording.

Two tiers, both cached by description hash so identical text is never processed twice:
  rules  — free regexes, run on every new description during fetch
  claude — optional, more accurate (claude_api.py); replaces the rules result when present

Every field is {"value": ..., "evidence": "<the sentence it came from>"}. When nothing
matches the value is None ("unknown"), never a guess.

ATS-provided structure (Ashby/Lever/Recruitee salary, workplace flags) always wins over text.
"""

from __future__ import annotations

import json
import re

from .models import utcnow
from .sanitize import html_to_text

FIELDS = ("salary", "workplace", "yoe_min", "clearance", "citizenship", "degree", "visa", "pipeline")

_PREFERRED = re.compile(r"\b(prefer|preferred|nice to have|nice-to-have|bonus|a plus|plus if|ideally|desirable)\b", re.I)

_CURS = r"(?:US\$|CA\$|C\$|A\$|\$|£|€|USD|CAD|GBP|EUR|AUD)"
_SALARY = re.compile(
    r"(?P<cur>US\$|CA\$|C\$|A\$|\$|£|€|USD|CAD|GBP|EUR|AUD)\s?(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<ak>[kK])?"
    r"\s*(?:-|–|—|to|and)\s*" + _CURS + r"?\s?(?P<b>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<bk>[kK])?")
_CUR = {"$": "USD", "US$": "USD", "USD": "USD", "CA$": "CAD", "C$": "CAD", "CAD": "CAD", "A$": "AUD", "AUD": "AUD",
        "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR"}
_HOURLY = re.compile(r"(per hour|an hour|/\s?hr|/\s?hour|hourly)", re.I)
_MONTHLY = re.compile(r"(per month|a month|/\s?mo(nth)?\b|monthly)", re.I)

_YOE = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b(?:\s+of)?(?:\s+[\w/&,-]+){0,5}?\s+"
                  r"(?:experience|exp\b|work(?:ing)?\b|industry)", re.I)
_CLEARANCE = [
    ("ts_sci", re.compile(r"\bTS\s*/\s*SCI\b|top secret\s*/\s*sci", re.I)),
    ("ts", re.compile(r"\btop secret\b", re.I)),
    ("secret", re.compile(r"\bsecret (security )?clearance\b|\bactive secret\b", re.I)),
    ("public_trust", re.compile(r"\bpublic trust\b", re.I)),
    ("unspecified", re.compile(r"\b(security clearance|active clearance|obtain (and maintain )?a clearance|clearance (is )?required)\b", re.I)),
]
_CITIZEN = re.compile(
    r"(must be a (U\.?S\.?|United States) citizen|(U\.?S\.?|United States) citizenship (is )?(required|mandatory)|"
    r"requires? (U\.?S\.?|United States) citizenship|(U\.?S\.?|United States) citizens? only|"
    r"only (U\.?S\.?|United States) citizens)", re.I)
_VISA_NO = re.compile(
    r"((not|unable to|cannot|can.t|won.t|will not|do not|does not|don.t|doesn.t|are not able to|is not able to|"
    r"aren.t able to|isn.t able to|not in a position to)\s+"
    r"(?:\w+\s+){0,3}?(sponsor|provide (?:\w+\s+){0,2}sponsorship|offer (?:\w+\s+){0,2}sponsorship)|"
    r"without (the need for |requiring )?(current or future |any )?(employer |visa |immigration )?sponsorship|"
    r"no (visa |immigration )?sponsorship|sponsorship (is )?not (available|provided|offered|possible))", re.I)
_VISA_YES = re.compile(
    r"((visa|immigration|h-?1b) sponsorship (is |may be )?(available|provided|offered)|"
    r"(will|can|able to|happy to|we|we do|we can|we will|we're able to|we are able to) sponsor (visas?|h-?1b|work (visas?|permits?))|"
    r"sponsorship (is )?available|offer(s|ing)? (visa |immigration )?sponsorship|relocation and visa support)", re.I)
_CAVEAT = re.compile(r"\b(every|all roles|each role|always|every candidate)\b", re.I)
_DEGREES = [
    ("bachelors", re.compile(r"\b(bachelor'?s|bachelors|B\.?S\.?c?\b|B\.?A\.?\b|BS/BA|BA/BS|undergraduate degree|4-year degree|four-year degree)", re.I)),
    ("masters", re.compile(r"\b(master'?s|masters|M\.?S\.?c?\b|MBA\b)", re.I)),
    ("phd", re.compile(r"\b(Ph\.?D|doctorate|doctoral)", re.I)),
]
_REMOTE = re.compile(r"\b(fully remote|100% remote|remote-first|remote first|work from anywhere|this (role|position) is remote|remote (role|position))\b", re.I)
_HYBRID = re.compile(r"\b(hybrid|\d days? (a|per) week in (the )?office|in[- ]office \d days)\b", re.I)
_ONSITE = re.compile(r"\b(on-?site (role|position|5 days)|fully in[- ]office|in[- ]person (role|position)|this role is on-?site)\b", re.I)


def _line(text: str, start: int, end: int) -> str:
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    line = text[a: b if b != -1 else len(text)].strip()
    return line if len(line) <= 240 else text[max(a, start - 100): end + 100].strip()


def _clause(text: str, start: int, end: int) -> str:
    """The sentence/clause around a match, so 'BS required; MS preferred' splits correctly."""
    delims = ".;,\n"
    a = max(text.rfind(d, 0, start) for d in delims) + 1
    bs = [i for i in (text.find(d, end) for d in delims) if i != -1]
    return text[a: min(bs) if bs else len(text)]


def _f(value=None, evidence=None):
    return {"value": value, "evidence": evidence}


def _money(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def rules(text: str, title: str = "", location: str = "") -> dict:
    out = {k: _f() for k in FIELDS}
    low = text.lower()  # cheap keyword gates keep the expensive regexes off most postings

    m = _SALARY.search(text) if any(c in text for c in "$£€") or "usd" in low or "eur" in low else None
    if m:
        a, b = _money(m["a"], m["ak"]), _money(m["b"], m["bk"] or m["ak"])
        if b >= a > 0 and b >= 7:  # employers sometimes post placeholders like "$1—$2"; that's unknown, not pay
            ctx = text[m.start(): m.end() + 40]
            per = "hour" if _HOURLY.search(ctx) or b < 500 else "month" if _MONTHLY.search(ctx) else "year"
            out["salary"] = _f({"min": a, "max": b, "currency": _CUR.get(m["cur"].upper(), m["cur"]), "interval": per},
                               _line(text, m.start(), m.end()))

    loc_title = f"{title} {location}".lower()
    if "remote" in loc_title:
        out["workplace"] = _f("remote", location or title)
    elif "hybrid" in loc_title:
        out["workplace"] = _f("hybrid", location or title)
    else:
        for value, pat in (("remote", _REMOTE), ("hybrid", _HYBRID), ("onsite", _ONSITE)):
            mm = pat.search(text)
            if mm:
                out["workplace"] = _f(value, _line(text, mm.start(), mm.end()))
                break

    best = None
    for mm in (_YOE.finditer(text) if ("year" in low or "yr" in low) else ()):
        line = _line(text, mm.start(), mm.end())
        n = int(mm.group(1))
        if 0 < n <= 20 and not _PREFERRED.search(_clause(text, mm.start(), mm.end())) and (best is None or n > best[0]):
            best = (n, line)
    if best:
        out["yoe_min"] = _f(best[0], best[1])

    for level, pat in (_CLEARANCE if ("clearance" in low or "secret" in low or "public trust" in low) else ()):
        mm = pat.search(text)
        if mm:
            out["clearance"] = _f(level, _line(text, mm.start(), mm.end()))
            break

    mm = _CITIZEN.search(text) if "citizen" in low else None
    if mm:
        out["citizenship"] = _f(True, _line(text, mm.start(), mm.end()))

    # "We sponsor, but can't for every role" is a caveat, not a refusal.
    has_sp = "sponsor" in low
    no = next((m for m in _VISA_NO.finditer(text)
               if not _CAVEAT.search(_clause(text, m.start(), m.end()) + text[m.end(): m.end() + 60])), None) if has_sp else None
    yes = _VISA_YES.search(text) if has_sp or "visa support" in low else None
    if no:
        out["visa"] = _f("no", _line(text, no.start(), no.end()))
    elif yes:
        out["visa"] = _f("yes", _line(text, yes.start(), yes.end()))

    for level, pat in (_DEGREES if "degree" in low else ()):  # lowest required level wins
        for mm in pat.finditer(text):
            line = _line(text, mm.start(), mm.end())
            clause = _clause(text, mm.start(), mm.end())
            if "degree" in line.lower() and not _PREFERRED.search(clause):
                out["degree"] = _f(level, line)
                break
        if out["degree"]["value"]:
            break

    from .ghost import PIPELINE_LANGUAGE
    mm = PIPELINE_LANGUAGE.search(text)
    if mm:
        out["pipeline"] = _f(mm.group(0), _line(text, mm.start(), mm.end()))
    return out


def ensure_rules(conn, company_keys) -> int:
    """Run the free extractor on any description hash that has no extraction yet."""
    n = 0
    for ck in company_keys:
        rows = conn.execute(
            """SELECT j.content_hash, c.html AS content_html, j.title, j.location FROM jobs j
               JOIN job_content c ON c.uid = j.uid
               LEFT JOIN extractions e ON e.content_hash = j.content_hash
               WHERE j.company_key=? AND j.active=1 AND e.content_hash IS NULL AND c.html <> ''
               GROUP BY j.content_hash""", (ck,)).fetchall()
        with conn:
            for r in rows:
                data = rules(html_to_text(r["content_html"]), r["title"], r["location"] or "")
                conn.execute("INSERT OR IGNORE INTO extractions(content_hash, method, data, at) VALUES (?, 'rules', ?, ?)",
                             (r["content_hash"], json.dumps(data, ensure_ascii=False), utcnow()))
                n += 1
    return n


_ANNUAL = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}


def _annual(v, per):
    return v * _ANNUAL.get(per or "year", 1) if v is not None else None


def derive(conn, company_keys) -> None:
    """Combine ATS structure + extraction into the indexed filter columns on jobs."""
    for ck in company_keys:
        rows = conn.execute(
            """SELECT j.uid, j.workplace, j.salary_min, j.salary_max, j.salary_currency, j.salary_interval,
                      j.content_hash, j.derived_of, e.data, e.method
               FROM jobs j LEFT JOIN extractions e ON e.content_hash = j.content_hash
               WHERE j.company_key=? AND j.active=1""", (ck,)).fetchall()
        with conn:
            for r in rows:
                key = f"{r['content_hash']}:{r['method']}:{r['workplace']}:{r['salary_min']}:{r['salary_max']}:{r['salary_interval']}"
                if key == r["derived_of"]:
                    continue
                d = json.loads(r["data"]) if r["data"] else {}
                val = lambda k: (d.get(k) or {}).get("value")
                sal = val("salary") or {}
                if r["salary_min"] is not None or r["salary_max"] is not None:
                    smin, smax, cur, per = r["salary_min"], r["salary_max"], r["salary_currency"], r["salary_interval"]
                else:
                    smin, smax, cur, per = sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("interval")
                smin, smax = _annual(smin, per), _annual(smax, per)
                if smax is None:
                    smax = smin
                if smax is not None and smax < 10000:  # implausible as annual pay: treat as unknown
                    smin = smax = cur = None
                cit = val("citizenship")
                conn.execute(
                    """UPDATE jobs SET workplace_eff=?, pay_min_annual=?, pay_max_annual=?, pay_currency=?,
                           yoe_min=?, visa=?, clearance=?, citizenship=?, degree=?, derived_of=? WHERE uid=?""",
                    (r["workplace"] or val("workplace"), smin, smax, cur, val("yoe_min"), val("visa"),
                     val("clearance"), None if cit is None else int(bool(cit)), val("degree"), key, r["uid"]))


def get(conn, content_hash: str) -> tuple[dict, str | None]:
    r = conn.execute("SELECT data, method FROM extractions WHERE content_hash=?", (content_hash,)).fetchone()
    return (json.loads(r["data"]), r["method"]) if r else ({}, None)
