"""Visa-sponsorship history from free government data, stored locally.

Sources (download the files yourself, then import them; nothing is fetched automatically):
  - DOL OFLC LCA disclosure data (quarterly .xlsx/.csv):
    https://www.dol.gov/agencies/eta/foreign-labor/performance
  - USCIS H-1B Employer Data Hub (annual .csv):
    https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub

Shown as *history*, LCA filings and USCIS approvals separately: a certified LCA does
not prove a petition was filed or approved, and past sponsorship doesn't promise future
sponsorship for a given role.

Scaling: files are streamed row by row and aggregated to one row per employer per
fiscal year (a 600k-row LCA file becomes ~60k rows). Employer names are matched to
job-board companies by normalized name (exact, or the company name followed by more words),
with a per-board `sponsor_name` override in config.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .models import company_key, utcnow

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "visa"


def _h(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _int(v) -> int:
    try:
        return int(float(str(v).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def _rows(path: Path):
    """Yield dicts with normalized header keys from .csv/.tsv/.txt (any encoding) or .xlsx."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise RuntimeError("Reading .xlsx needs openpyxl (pip install openpyxl), "
                               "or save the sheet as CSV first.") from exc
        wb = load_workbook(path, read_only=True, data_only=True)
        it = wb.active.iter_rows(values_only=True)
        header = [_h(str(c or "")) for c in next(it)]
        for row in it:
            yield dict(zip(header, row))
        wb.close()
        return
    raw = path.open("rb")
    head = raw.read(4)
    raw.seek(0)
    enc = "utf-16" if head[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    text = io.TextIOWrapper(raw, encoding=enc, errors="replace", newline="")
    first = text.readline()
    delim = "\t" if first.count("\t") > first.count(",") else ","
    header = [_h(c) for c in next(csv.reader([first], delimiter=delim))]
    for row in csv.reader(text, delimiter=delim):
        yield dict(zip(header, row))
    raw.close()


def _fy_from_date(v) -> int | None:
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v or "").strip()[:10]
        d = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                d = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if d is None:
            return None
    return d.year + (1 if d.month >= 10 else 0)  # US federal fiscal year starts Oct 1


def detect_kind(path: Path) -> str:
    keys = set(next(_rows(path)).keys())
    if "casestatus" in keys and "employername" in keys:
        return "lca"
    if any(k.startswith("employer") and "petitioner" in k for k in keys) or "initialapproval" in keys \
            or "newemploymentapproval" in keys:
        return "uscis"
    raise ValueError(f"{path.name}: not a recognised LCA disclosure or USCIS Data Hub file")


def import_file(conn, path: Path | str, force: bool = False) -> dict:
    path = Path(path)
    if not force and conn.execute("SELECT 1 FROM sponsor_files WHERE file_key=?", (path.name,)).fetchone():
        return {"file": path.name, "skipped": "already imported"}
    kind = detect_kind(path)
    file_fy = re.search(r"FY\s?(\d{4})", path.name, re.I)
    file_fy = int(file_fy.group(1)) if file_fy else None
    agg: dict = defaultdict(lambda: defaultdict(int))
    names: dict = {}
    n = 0
    for row in _rows(path):
        if kind == "lca":
            name = str(row.get("employername") or "").strip()
            if not name:
                continue
            fy = _fy_from_date(row.get("decisiondate") or row.get("receiveddate")) or file_fy
            if not fy:
                continue
            k = (company_key(name), fy)
            a = agg[k]
            a["filings"] += 1
            status = str(row.get("casestatus") or "").strip().lower()
            if status == "certified":
                a["certified"] += 1
                lvl = str(row.get("pwwagelevel") or row.get("wagelevel") or "").strip().upper()
                if lvl in ("I", "II", "III", "IV"):
                    a["wage_l" + str(("I", "II", "III", "IV").index(lvl) + 1)] += 1
        else:
            name = next((str(v).strip() for k, v in row.items() if k.startswith("employer") and v), "")
            fy = _int(next((v for k, v in row.items() if k.startswith("fiscalyear")), 0)) or file_fy
            if not name or not fy:
                continue
            k = (company_key(name), fy)
            a = agg[k]
            for col, v in row.items():
                if not (col.endswith("approval") or col.endswith("denial")):
                    continue
                initial = col.startswith(("initial", "newemployment", "newconcurrent", "changeofemployer"))
                bucket = ("initial_" if initial else "continuing_") + ("approvals" if col.endswith("approval") else "denials")
                a[bucket] += _int(v)
        names[k] = name
        n += 1
    table, cols = (("sponsor_lca", ("filings", "certified", "wage_l1", "wage_l2", "wage_l3", "wage_l4"))
                   if kind == "lca" else
                   ("sponsor_uscis", ("initial_approvals", "initial_denials", "continuing_approvals", "continuing_denials")))
    with conn:
        for (ek, fy), a in agg.items():
            vals = [a.get(c, 0) for c in cols]
            conn.execute(
                f"""INSERT INTO {table}(employer_key, employer_name, fiscal_year, {', '.join(cols)})
                    VALUES (?, ?, ?, {', '.join('?' * len(cols))})
                    ON CONFLICT(employer_key, fiscal_year) DO UPDATE SET
                    {', '.join(f'{c} = {table}.{c} + excluded.{c}' for c in cols)}""",
                (ek, names[(ek, fy)], fy, *vals))
        conn.execute("INSERT OR REPLACE INTO sponsor_files(file_key, kind, rows, imported_at) VALUES (?,?,?,?)",
                     (path.name, kind, n, utcnow()))
    refresh_matches(conn, None)
    return {"file": path.name, "kind": kind, "rows": n, "employers": len({k[0] for k in agg})}


def import_folder(conn, folder: Path = DATA_DIR) -> list[dict]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in (".csv", ".tsv", ".txt", ".xlsx"):
            try:
                out.append(import_file(conn, p))
            except Exception as exc:
                out.append({"file": p.name, "error": str(exc)})
    return out


def _board_overrides(conn) -> dict[str, str]:
    """company_key -> sponsor_name from board options."""
    out = {}
    for b in conn.execute("SELECT ats, slug, options FROM boards WHERE options IS NOT NULL"):
        name = (json.loads(b["options"]) or {}).get("sponsor_name")
        if name:
            r = conn.execute("SELECT company_key FROM jobs WHERE source=? AND source_board=? LIMIT 1",
                             (b["ats"], b["slug"])).fetchone()
            if r:
                out[r[0]] = company_key(name)
    return out


def _employer_keys(conn, ck: str) -> list[str]:
    hits = {r[0] for t in ("sponsor_lca", "sponsor_uscis") for r in conn.execute(
        f"SELECT DISTINCT employer_key FROM {t} WHERE employer_key = ? OR "
        f"(length(?) >= 4 AND employer_key >= ? AND employer_key < ?)", (ck, ck, ck + " ", ck + "!"))}
    return sorted(hits)


def refresh_matches(conn, company_keys=None) -> None:
    if not conn.execute("SELECT 1 FROM sponsor_files LIMIT 1").fetchone():
        return
    if company_keys is None:
        company_keys = [r[0] for r in conn.execute("SELECT DISTINCT company_key FROM jobs WHERE company_key IS NOT NULL")]
    overrides = _board_overrides(conn)
    lca_max = conn.execute("SELECT MAX(fiscal_year) FROM sponsor_lca").fetchone()[0] or 0
    us_max = conn.execute("SELECT MAX(fiscal_year) FROM sponsor_uscis").fetchone()[0] or 0
    with conn:
        for ck in company_keys:
            keys = _employer_keys(conn, overrides.get(ck, ck))
            if not keys:
                conn.execute("DELETE FROM sponsor_match WHERE company_key=?", (ck,))
                continue
            q = ",".join("?" * len(keys))
            lca = conn.execute(f"SELECT COALESCE(SUM(certified),0) FROM sponsor_lca WHERE employer_key IN ({q}) "
                               f"AND fiscal_year > ?", (*keys, lca_max - 3)).fetchone()[0]
            us = conn.execute(f"SELECT COALESCE(SUM(initial_approvals + continuing_approvals),0) FROM sponsor_uscis "
                              f"WHERE employer_key IN ({q}) AND fiscal_year > ?", (*keys, us_max - 3)).fetchone()[0]
            names = sorted({r[0] for t in ("sponsor_lca", "sponsor_uscis") for r in conn.execute(
                f"SELECT DISTINCT employer_name FROM {t} WHERE employer_key IN ({q})", keys)})[:10]
            conn.execute("INSERT OR REPLACE INTO sponsor_match(company_key, lca_recent, uscis_recent, names) "
                         "VALUES (?,?,?,?)", (ck, lca, us, json.dumps(names)))


def history(conn, ck: str) -> dict:
    """Per-fiscal-year LCA and USCIS history for one company (for the job page)."""
    m = conn.execute("SELECT * FROM sponsor_match WHERE company_key=?", (ck,)).fetchone()
    if not m:
        return {}
    keys = _employer_keys(conn, _board_overrides(conn).get(ck, ck))
    q = ",".join("?" * len(keys))
    lca = conn.execute(f"SELECT fiscal_year, SUM(filings) f, SUM(certified) c, SUM(wage_l1) l1, SUM(wage_l2) l2, "
                       f"SUM(wage_l3) l3, SUM(wage_l4) l4 FROM sponsor_lca WHERE employer_key IN ({q}) "
                       f"GROUP BY fiscal_year ORDER BY fiscal_year DESC LIMIT 5", keys).fetchall()
    us = conn.execute(f"SELECT fiscal_year, SUM(initial_approvals) ia, SUM(initial_denials) idn, "
                      f"SUM(continuing_approvals) ca, SUM(continuing_denials) cd FROM sponsor_uscis "
                      f"WHERE employer_key IN ({q}) GROUP BY fiscal_year ORDER BY fiscal_year DESC LIMIT 5", keys).fetchall()
    return {"names": json.loads(m["names"] or "[]"), "lca": lca, "uscis": us,
            "lca_recent": m["lca_recent"], "uscis_recent": m["uscis_recent"]}


def files(conn) -> list:
    return conn.execute("SELECT * FROM sponsor_files ORDER BY imported_at DESC").fetchall()
