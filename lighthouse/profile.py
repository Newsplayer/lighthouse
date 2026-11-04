"""Your profile (stored locally in the database) and the deterministic eligibility filter.

Eligibility is rules, not judgment: it only removes a posting when the posting clearly
states something you can't meet (no sponsorship, citizenship, clearance, location,
years of experience, salary floor, degree). Unknown values never disqualify.
No API calls; it runs on every posting and gates which ones Claude is asked about.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import db

CLEARANCE_ORDER = {"none": 0, "public_trust": 1, "secret": 2, "ts": 3, "ts_sci": 4}
DEGREE_ORDER = {"none": 0, "bachelors": 1, "masters": 2, "phd": 3}
DEGREE_LABEL = {"bachelors": "a bachelor's degree", "masters": "a master's degree", "phd": "a PhD"}

DEFAULT = {
    "about": "",                 # plain-language: what you want next
    "resume": "",                # pasted resume text (stays local; redacted before any API call)
    "years_experience": None,
    "degree": "none",
    "needs_sponsorship": False,
    "us_citizen": False,
    "clearance": "none",
    "locations": [],             # e.g. ["Austin", "New York", "Remote"]
    "workplaces": ["remote", "hybrid", "onsite"],
    "min_salary": None,          # annual, posting currency
    "countries": [],             # only keep/process jobs located here, e.g. ["US"]; empty = everywhere
}

ELIG_KEYS = ("years_experience", "degree", "needs_sponsorship", "us_citizen", "clearance",
             "locations", "workplaces", "min_salary")
FIT_KEYS = ("about", "resume", "years_experience", "degree")


def load(conn) -> dict:
    return {**DEFAULT, **(db.get_meta(conn, "profile", {}) or {})}


def save(conn, p: dict) -> None:
    db.set_meta(conn, "profile", {k: p.get(k, DEFAULT[k]) for k in DEFAULT})


def is_set(p: dict) -> bool:
    return bool(p.get("about") or p.get("resume") or p.get("years_experience") is not None
                or p.get("locations") or p.get("needs_sponsorship"))


def _hash(p: dict, keys) -> str:
    return hashlib.blake2b(json.dumps({k: p.get(k) for k in keys}, sort_keys=True).encode(),
                           digest_size=8).hexdigest()


def elig_version(p: dict, slack: int) -> str:
    return _hash({**p, "_slack": slack}, ELIG_KEYS + ("_slack",))


def fit_hash(p: dict) -> str:
    return _hash(p, FIT_KEYS)


def check(row, p: dict, yoe_slack: int = 2) -> list[str]:
    """Reasons this posting is out of reach. Empty list = eligible."""
    reasons = []
    if row["visa"] == "no" and p.get("needs_sponsorship"):
        reasons.append("Says it can't sponsor visas; you need sponsorship")
    if row["citizenship"] and not p.get("us_citizen"):
        reasons.append("Requires U.S. citizenship")
    clr = row["clearance"]
    if clr:
        held = CLEARANCE_ORDER.get(p.get("clearance") or "none", 0)
        need = CLEARANCE_ORDER.get(clr, 2)  # "unspecified" ~ secret
        if not p.get("us_citizen") and held == 0:
            reasons.append("Requires a security clearance (generally U.S. citizens only)")
        elif held < need and held > 0:
            reasons.append(f"Asks for {clr.replace('_', '/').upper()} clearance")
    wp = row["workplace_eff"]
    wants = set(p.get("workplaces") or [])
    if wp and wants and wp not in wants:
        reasons.append(f"{wp.capitalize()} role; you want {', '.join(sorted(wants))}")
    locs = [l.strip().lower() for l in p.get("locations") or [] if l.strip()]
    if locs and wp != "remote":
        places = json.loads(row["cluster_locations"] or "[]") or [row["location"] or ""]
        posted = " | ".join(places).lower()
        remote_ok = "remote" in locs and "remote" in posted
        if posted and not remote_ok and not any(l in posted for l in locs if l != "remote"):
            more = f" (+{len(places) - 1} more)" if len(places) > 1 else ""
            reasons.append(f"Located in {places[0]}{more}; not one of your locations")
    yoe, have = row["yoe_min"], p.get("years_experience")
    stated = (row["yoe_source"] if "yoe_source" in row.keys() else "stated") == "stated"
    if stated and yoe is not None and have is not None and yoe > have + yoe_slack:
        reasons.append(f"Asks for {yoe}+ years of experience; you have {have}")
    floor = p.get("min_salary")
    if floor and row["pay_max_annual"] and row["pay_max_annual"] < floor:
        reasons.append(f"Pays up to {row['pay_max_annual']:,.0f}; your minimum is {floor:,.0f}")
    deg = row["degree"]
    if deg and DEGREE_ORDER.get(deg, 0) > DEGREE_ORDER.get(p.get("degree") or "none", 0):
        reasons.append(f"Asks for {DEGREE_LABEL.get(deg, deg)}")
    return reasons


def recompute(conn, company_keys=None, yoe_slack: int = 2, force: bool = False) -> int:
    p = load(conn)
    if not is_set(p):
        conn.execute("UPDATE jobs SET elig=NULL, elig_reasons=NULL, elig_version=NULL WHERE elig IS NOT NULL")
        conn.commit()
        return 0
    version = elig_version(p, yoe_slack)
    sql = ("SELECT uid, visa, citizenship, clearance, workplace_eff, cluster_locations, location, yoe_min, yoe_source, "
           "pay_max_annual, degree, derived_of, elig_version FROM jobs WHERE active=1")
    args: list = []
    if company_keys is not None:
        keys = list(company_keys)
        if not keys:
            return 0
        sql += f" AND company_key IN ({','.join('?' * len(keys))})"
        args = keys
    n = 0
    with conn:
        for r in conn.execute(sql, args).fetchall():
            stamp = f"{version}:{r['derived_of']}:{r['cluster_locations']}"
            if not force and r["elig_version"] == stamp:
                continue
            reasons = check(r, p, yoe_slack)
            conn.execute("UPDATE jobs SET elig=?, elig_reasons=?, elig_version=? WHERE uid=?",
                         (0 if reasons else 1, json.dumps(reasons), stamp, r["uid"]))
            n += 1
    return n


# ------------------------------------------------------------ privacy: what Claude may see

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]?\d{3,4}")
_URL = re.compile(r"(https?://\S+|www\.\S+|\b(?:linkedin|github)\.com/\S*)", re.I)
_ADDRESS = re.compile(r"\b\d{1,6}\s+(?:[A-Z][\w.]*\s){1,4}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|"
                      r"Ct|Court|Way|Pl|Place|Ter|Terrace|Pkwy)\b\.?(?:,?\s*(?:Apt|Unit|Suite|#)\s*\w+)?", re.I)
_NAME_LINE = re.compile(r"^[A-Z][a-zA-Z'’-]+(?:\s+[A-Z][a-zA-Z'’.-]*){1,3}$")


def redact(text: str) -> str:
    """Strip contact details and the name line from a resume before it leaves the machine."""
    lines = (text or "").strip().splitlines()
    if lines and _NAME_LINE.match(lines[0].strip()):
        lines[0] = "[name removed]"
    out = "\n".join(lines)
    out = _EMAIL.sub("[email removed]", out)
    out = _URL.sub("[link removed]", out)
    out = _ADDRESS.sub("[address removed]", out)
    out = _PHONE.sub(lambda m: "[phone removed]" if sum(c.isdigit() for c in m.group(0)) >= 9 else m.group(0), out)
    return out


def for_claude(p: dict) -> dict:
    """The only profile data that's ever sent: derived fields + redacted resume."""
    return {
        "looking_for": p.get("about") or "",
        "years_experience": p.get("years_experience"),
        "highest_degree": p.get("degree"),
        "resume": redact(p.get("resume") or ""),
    }
