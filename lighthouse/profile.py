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


def _posted_places(cluster_locations, location) -> str:
    places = json.loads(cluster_locations or "[]") or [location or ""]
    return " | ".join(places).lower()


def eligibility_sql(conn, p: dict, yoe_slack: int = 2, alias: str = "j") -> tuple[str, list] | None:
    """The same rules as check(), as one WHERE clause over the job index (None = no profile, so
    everything counts as eligible). Nothing is stored per job: changing your profile or getting a
    new index never needs a recompute. Locations use a small SQL function for exact matching."""
    if not is_set(p):
        return None
    j = alias
    conds, args = [], []
    if p.get("needs_sponsorship"):
        conds.append(f"{j}.visa IS NOT 'no'")
    us = bool(p.get("us_citizen"))
    if not us:
        conds.append(f"({j}.citizenship IS NULL OR {j}.citizenship = 0)")
    held = CLEARANCE_ORDER.get(p.get("clearance") or "none", 0)
    rank = (f"CASE {j}.clearance WHEN 'none' THEN 0 WHEN 'public_trust' THEN 1 WHEN 'secret' THEN 2 "
            f"WHEN 'ts' THEN 3 WHEN 'ts_sci' THEN 4 ELSE 2 END")
    if not us and held == 0:
        conds.append(f"({j}.clearance IS NULL OR {j}.clearance = '')")
    elif held > 0:
        conds.append(f"({j}.clearance IS NULL OR {j}.clearance = '' OR {rank} <= ?)")
        args.append(held)
    wants = sorted(set(p.get("workplaces") or []))
    if wants:
        conds.append(f"({j}.workplace_eff IS NULL OR {j}.workplace_eff = '' OR {j}.workplace_eff IN "
                     f"({','.join('?' * len(wants))}))")
        args += wants
    locs = [l.strip().lower() for l in p.get("locations") or [] if l.strip()]
    if locs:
        want_remote = "remote" in locs
        others = [l for l in locs if l != "remote"]

        def loc_ok(cluster_locations, location) -> int:
            posted = _posted_places(cluster_locations, location)
            return int(not posted or (want_remote and "remote" in posted) or any(l in posted for l in others))
        conn.create_function("lh_loc_ok", 2, loc_ok, deterministic=True)
        conds.append(f"({j}.workplace_eff IS 'remote' OR lh_loc_ok({j}.cluster_locations, {j}.location))")
    have = p.get("years_experience")
    if have is not None:
        conds.append(f"({j}.yoe_source IS NOT 'stated' OR {j}.yoe_min IS NULL OR {j}.yoe_min <= ?)")
        args.append(have + yoe_slack)
    floor = p.get("min_salary")
    if floor:
        conds.append(f"({j}.pay_max_annual IS NULL OR {j}.pay_max_annual = 0 OR {j}.pay_max_annual >= ?)")
        args.append(floor)
    mine = DEGREE_ORDER.get(p.get("degree") or "none", 0)
    if mine < 3:
        conds.append(f"(CASE {j}.degree WHEN 'bachelors' THEN 1 WHEN 'masters' THEN 2 WHEN 'phd' THEN 3 "
                     f"ELSE 0 END <= ?)")
        args.append(mine)
    return (" AND ".join(conds) or "1=1"), args


def annotate(rows: list[dict], p: dict, yoe_slack: int = 2) -> None:
    """Add elig (1/0, None without a profile) and elig_reasons (JSON) to rows about to be shown."""
    on = is_set(p)
    for r in rows:
        reasons = check(r, p, yoe_slack) if on else []
        r["elig"] = (0 if reasons else 1) if on else None
        r["elig_reasons"] = json.dumps(reasons)


def recompute(*_a, **_k) -> int:
    """Kept for old callers: eligibility is now evaluated at query time (eligibility_sql)."""
    return 0


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
