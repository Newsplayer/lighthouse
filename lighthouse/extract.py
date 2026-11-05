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

FIELDS = ("salary", "workplace", "yoe_min", "clearance", "citizenship", "degree", "visa", "pipeline",
          "skills", "employment")

_PREFERRED = re.compile(r"\b(prefer|preferred|nice to have|nice-to-have|bonus|a plus|plus if|ideally|desirable)\b", re.I)

_CURS = r"(?:US\$|CA\$|C\$|A\$|\$|£|€|USD|CAD|GBP|EUR|AUD)"
_SALARY = re.compile(
    r"(?P<cur>US\$|CA\$|C\$|A\$|\$|£|€|USD|CAD|GBP|EUR|AUD)\s?(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<ak>[kK])?"
    r"\s*(?:-|–|—|to|and)\s*" + _CURS + r"?\s?(?P<b>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?P<bk>[kK])?")
_CUR = {"$": "USD", "US$": "USD", "USD": "USD", "CA$": "CAD", "C$": "CAD", "CAD": "CAD", "A$": "AUD", "AUD": "AUD",
        "£": "GBP", "GBP": "GBP", "€": "EUR", "EUR": "EUR"}
_HOURLY = re.compile(r"(per hour|an hour|/\s?hr|/\s?hour|hourly)", re.I)
_MONTHLY = re.compile(r"(per month|a month|/\s?mo(nth)?\b|monthly)", re.I)

_NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
             "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "a": 1}
_N = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen)"
# "5+ years of (relevant, hands-on...) experience", "4+ years’ experience", "3-5 yrs of industry experience"
_YOE = re.compile(_N + r"\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?"
                  r"(?:(?:total|full|professional|relevant|combined|overall)\s+)?(?:years?|yrs?)\b['’]?(?:\s+of)?"
                  r"(?:\s+[\w/&,.'’()-]+){0,8}?\s+(?:experience|exp\b|work(?:ing)?\b|industry|background)", re.I)
# "minimum 2-5 years", "at least one year", "minimum of 3 years"
_YOE_MIN = re.compile(r"\b(?:minimum|at least|min\.?)\s+(?:of\s+)?" + _N +
                      r"(?:\s*(?:-|–|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b", re.I)
_HEADING_PREF = re.compile(r"\b(preferred|nice to have|nice-to-have|bonus|plus|desired|ideally)\b", re.I)
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


_AFTER_PREF = re.compile(r"^\W{0,3}(is |are )?(strongly )?(preferred|a plus|nice to have|nice-to-have|desired|a bonus|bonus)\b", re.I)


def _spans(text: str, low: str, keys, before: int, after: int, whole_line: bool = False) -> list[tuple[int, int]]:
    """Merged windows around keyword hits. Regexes then run only inside them
    (re.finditer(text, pos, endpos) keeps absolute positions), so a 5,000-word posting
    costs a few short scans instead of one full scan per pattern."""
    if len(low) != len(text):  # rare Unicode case-folding length change: scan everything
        return [(0, len(text))]
    spans = []
    for k in keys:
        i = low.find(k)
        while i != -1:
            if whole_line:
                a = text.rfind("\n", 0, i) + 1
                b = text.find("\n", i)
                spans.append((a, len(text) if b == -1 else b))
            else:
                spans.append((max(0, i - before), min(len(text), i + len(k) + after)))
            i = low.find(k, i + 1)
    spans.sort()
    merged: list[list[int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(m) for m in merged]


def _iter(pat, text: str, spans):
    for a, b in spans:
        yield from pat.finditer(text, a, b)


def _first(pat, text: str, spans):
    return next(_iter(pat, text, spans), None)


_PIPELINE_KEYS = ("talent", "future", "currently", "actively", "no current", "no immediate", "always", "evergreen",
                  "on file", "become available", "pipeline")


def _is_preferred(text: str, start: int, end: int) -> bool:
    """Preferred when the clause says so before the number ("Preferred: 5+ years", "Bonus if 3 years"),
    right after it ("2+ years of experience preferred"), or the item sits under a
    'Preferred / Nice to have / Bonus' heading. "…experience, ideally in fintech" is still required:
    'ideally' there qualifies the domain, not the years."""
    a = max(text.rfind(d, 0, start) for d in ".;\n") + 1
    nl = text.find("\n", end)
    after = text[end: min(end + 30, nl if nl != -1 else len(text))]
    if _PREFERRED.search(text[a:start]) or _AFTER_PREF.search(after):
        return True
    for line in reversed(text[:a].splitlines()[-25:]):  # nearest heading above
        line = line.strip()
        if line and not line.startswith("- ") and len(line) <= 70:
            return bool(_HEADING_PREF.search(line))
    return False


# Title-level fallback when a posting states no number (clearly labelled "from title" in the UI,
# never used to rule anyone out).
_TITLE_LEVELS = [
    (0, re.compile(r"\b(intern|internship|co-?op|new grad|new graduate|entry[- ]level|junior|jr\.?|early career|"
                   r"graduate (program|scheme|engineer|analyst)|apprentice)\b", re.I)),
    (10, re.compile(r"\b(principal|distinguished|fellow|director|head of|vice president|vp)\b", re.I)),
    (8, re.compile(r"\bstaff\+?\s+(software|engineer|research|scientist|data|ml|machine|product|designer|security|"
                   r"infrastructure|backend|frontend|full[- ]stack|platform|applied|site|systems)", re.I)),
    (5, re.compile(r"\b(senior|sr\.?)\b", re.I)),
]


def yoe_from_title(title: str) -> int | None:
    for years, pat in _TITLE_LEVELS:
        if pat.search(title or ""):
            return years
    return None


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

    best, pref = None, None   # required wins; a preferred-only number is kept, labelled
    if "year" in low or "yr" in low:
        yspans = _spans(text, low, ("year", "yr"), 60, 170)
        for pat in (_YOE, _YOE_MIN):
            for mm in _iter(pat, text, yspans):
                n = _NUMWORDS.get(mm.group(1).lower()) or (int(mm.group(1)) if mm.group(1).isdigit() else 0)
                if not 0 < n <= 20:
                    continue
                hit = (n, _line(text, mm.start(), mm.end()))
                if _is_preferred(text, mm.start(), mm.end()):
                    if pref is None or n > pref[0]:
                        pref = hit
                elif best is None or n > best[0]:
                    best = hit
    if best:
        out["yoe_min"] = _f(best[0], best[1])
    elif pref:
        out["yoe_min"] = {"value": pref[0], "evidence": pref[1], "preferred": True}

    cspans = _spans(text, low, ("clearance", "secret", "public trust", "ts/sci", "ts /"), 60, 60)
    for level, pat in (_CLEARANCE if cspans else ()):
        mm = _first(pat, text, cspans)
        if mm:
            out["clearance"] = _f(level, _line(text, mm.start(), mm.end()))
            break

    mm = _first(_CITIZEN, text, _spans(text, low, ("citizen",), 70, 40))
    if mm:
        out["citizenship"] = _f(True, _line(text, mm.start(), mm.end()))

    # "We sponsor, but can't for every role" is a caveat, not a refusal.
    vspans = _spans(text, low, ("sponsor", "visa support"), 110, 70)
    no = next((m for m in _iter(_VISA_NO, text, vspans)
               if not _CAVEAT.search(_clause(text, m.start(), m.end()) + text[m.end(): m.end() + 60])), None)
    yes = _first(_VISA_YES, text, vspans)
    if no:
        out["visa"] = _f("no", _line(text, no.start(), no.end()))
    elif yes:
        out["visa"] = _f("yes", _line(text, yes.start(), yes.end()))

    dspans = _spans(text, low, ("degree",), 0, 0, whole_line=True)  # the check below needs "degree" on the line
    for level, pat in (_DEGREES if dspans else ()):  # lowest required level wins
        for mm in _iter(pat, text, dspans):
            line = _line(text, mm.start(), mm.end())
            clause = _clause(text, mm.start(), mm.end())
            if "degree" in line.lower() and not _PREFERRED.search(clause):
                out["degree"] = _f(level, line)
                break
        if out["degree"]["value"]:
            break

    from .ghost import PIPELINE_LANGUAGE
    mm = _first(PIPELINE_LANGUAGE, text, _spans(text, low, _PIPELINE_KEYS, 50, 70))
    if mm:
        out["pipeline"] = _f(mm.group(0), _line(text, mm.start(), mm.end()))

    from . import classify
    found = classify.skills(text)
    if found:
        out["skills"] = _f(found)
    emp = classify.employment_from_text(text)
    if emp:
        out["employment"] = _f(emp)
    return out


def ensure_rules(conn, company_keys) -> int:
    """Run the free extractor on any description hash that has no extraction yet."""
    n = 0
    for ck in company_keys:
        rows = conn.execute(
            """SELECT j.content_hash, unz(c.html) AS content_html, j.title, j.location FROM jobs j
               JOIN job_content c ON c.uid = j.uid
               LEFT JOIN extractions e ON e.content_hash = j.content_hash
               WHERE j.company_key=? AND j.active=1 AND e.content_hash IS NULL AND length(c.html) > 0
               GROUP BY j.content_hash""", (ck,)).fetchall()
        with conn:
            for r in rows:
                data = rules(html_to_text(r["content_html"]), r["title"], r["location"] or "")
                from .workers import RULES_VERSION
                conn.execute("INSERT OR IGNORE INTO extractions(content_hash, method, data, model, at) "
                             "VALUES (?, 'rules', ?, ?, ?)",
                             (r["content_hash"], json.dumps(data, ensure_ascii=False), RULES_VERSION, utcnow()))
                n += 1
    return n


_ANNUAL = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}


def interval_of(per: str | None) -> str:
    """How the posting states pay: 'hour' or 'year' (week/month/day shown as yearly)."""
    return "hour" if per == "hour" else "year"


def _annual(v, per):
    return v * _ANNUAL.get(per or "year", 1) if v is not None else None


_DERIVE_SELECT = """SELECT j.uid, j.title, j.workplace, j.salary_min, j.salary_max, j.salary_currency,
       j.salary_interval, j.content_hash, j.derived_of, j.cluster_locations, j.location, j.departments,
       j.employment_type, COALESCE(ce.data, e.data) AS data, CASE WHEN ce.data IS NOT NULL THEN 'claude' ELSE e.method END AS method
FROM jobs j LEFT JOIN extractions e ON e.content_hash = j.content_hash
LEFT JOIN claude_extractions ce ON ce.content_hash = j.content_hash"""


def derive(conn, company_keys) -> None:
    """Combine ATS structure + extraction (+ title level, + location countries) into the indexed
    filter columns on jobs. Pure lookups and string checks: no description parsing here."""
    for ck in company_keys:
        _derive_rows(conn, conn.execute(_DERIVE_SELECT + " WHERE j.company_key=? AND j.active=1", (ck,)).fetchall())


def derive_claude(conn) -> int:
    """Reader: re-apply your own Claude extractions on top of the job index (only those rows differ)."""
    rows = conn.execute(_DERIVE_SELECT + " WHERE j.active=1 AND ce.data IS NOT NULL").fetchall()
    _derive_rows(conn, rows)
    return len(rows)


def _derive_rows(conn, rows) -> None:
    from . import classify, geo
    with conn:
        for r in rows:
            key = (f"{classify.DERIVE_VERSION}:{r['content_hash']}:{r['method']}:{r['workplace']}:{r['salary_min']}:"
                   f"{r['salary_max']}:{r['salary_interval']}:{r['title']}:{r['cluster_locations']}:"
                   f"{r['departments']}:{r['employment_type']}")
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
                smin = smax = cur = per = None
            cit = val("citizenship")
            yoe = val("yoe_min")
            ysrc = "preferred" if (d.get("yoe_min") or {}).get("preferred") else "stated"
            if yoe is None:
                yoe = yoe_from_title(r["title"])
                ysrc = "title" if yoe is not None else None
            locs = json.loads(r["cluster_locations"] or "[]") or ([r["location"]] if r["location"] else [])
            stated = yoe if ysrc == "stated" else None
            conn.execute(
                """UPDATE jobs SET workplace_eff=?, pay_min_annual=?, pay_max_annual=?, pay_currency=?,
                       pay_interval=?, yoe_min=?, yoe_source=?, visa=?, clearance=?, citizenship=?, degree=?,
                       countries=?, job_function=?, seniority=?, emp_type=?, skills=?, derived_of=? WHERE uid=?""",
                (r["workplace"] or val("workplace"), smin, smax, cur, interval_of(per) if smax else None, yoe, ysrc,
                 val("visa"), val("clearance"), None if cit is None else int(bool(cit)), val("degree"),
                 geo.encode(geo.countries_of(locs)),
                 classify.job_function(r["title"], r["departments"] or ""),
                 classify.seniority(r["title"], stated, ysrc),
                 classify.employment_type(r["employment_type"], r["title"], val("employment")),
                 classify.encode_skills(val("skills") or []), key, r["uid"]))


def get(conn, content_hash: str) -> tuple[dict, str | None]:
    """Your Claude extraction if you have one, else the free rule-based one."""
    r = conn.execute("SELECT data, 'claude' AS method FROM claude_extractions WHERE content_hash=?",
                     (content_hash,)).fetchone()
    r = r or conn.execute("SELECT data, method FROM extractions WHERE content_hash=?", (content_hash,)).fetchone()
    return (json.loads(r["data"]), r["method"]) if r else ({}, None)
