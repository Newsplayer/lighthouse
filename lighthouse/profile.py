"""The eligibility rules, in Python: the reference the website's "Can you apply?" answers are tested
against (site/logic.js is the copy that runs in the browser; tests/test_site.py compares the two).

Eligibility is rules, not judgment: a posting is ruled out only when it clearly states something the
person can't meet (no sponsorship, citizenship, clearance, location, years of experience, salary floor,
degree). Unknown values never disqualify.
"""

from __future__ import annotations

import json

CLEARANCE_ORDER = {"none": 0, "public_trust": 1, "secret": 2, "ts": 3, "ts_sci": 4}
DEGREE_ORDER = {"none": 0, "bachelors": 1, "masters": 2, "phd": 3}
DEGREE_LABEL = {"bachelors": "a bachelor's degree", "masters": "a master's degree", "phd": "a PhD"}

DEFAULT = {
    "years_experience": None,
    "degree": "none",
    "needs_sponsorship": False,
    "us_citizen": False,
    "clearance": "none",
    "locations": [],             # e.g. ["Austin", "New York", "Remote"]
    "workplaces": ["remote", "hybrid", "onsite"],
    "min_salary": None,          # annual, posting currency
}


def is_set(p: dict) -> bool:
    return bool(p.get("years_experience") is not None or p.get("locations") or p.get("needs_sponsorship"))


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
    """The same rules as check(), as one WHERE clause over jobs (None = no profile, so everything
    counts as eligible). Locations use a small SQL function for exact matching."""
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
