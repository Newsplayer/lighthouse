"""The website: static files for GitHub Pages, written by the builder after each build.

Users download nothing and install nothing. The page (site/index.html + app.js) loads these files
and does all searching and filtering in the browser. Nothing is ever sent anywhere: the page talks
only to its own GitHub Pages origin, and a user's profile and saved jobs live in their browser.

    data/latest.json               what's current (fetched with a cache-busting query, ~1 KB)
    data/jobs-<base>.json          one entry per role (de-duplicated, in scope), as columns; weekly.
                                   Its places are normalized ("US|TX|Austin|austin": country, state or
                                   province, city, metro area); a place that can't be read stays as
                                   the employer wrote it ("~Plant 7")
    data/delta-<build>.json        entries added or changed since that base, and ids gone; every build
    data/keys-<base>.json          a short stable key per base entry (to keep saved jobs across bases)
    data/w-<build>/<xx>.json       keyword index over title, company, places, skills, function,
                                   departments, sharded by the first two letters of each word
    data/d-<build>/<n>.json        job details, 128 per file: excerpt, every apply link, places,
                                   departments, ghost reasons
    data/sources-<build>.json      the companies list's coverage, and the boards
    data/top-<build>.json          the default view's first page, as a tiny job list: shown on a first
                                   visit while the whole list downloads

Ids are stable for a base week: base entries are 0..n-1, new roles get the next ids, and the
builder remembers both (table site_ids; the weekly files themselves in site_files; state in meta
site_state), so successive deltas agree and the base travels with the database in the Actions cache. A new base comes
weekly, when the delta outgrows a quarter of the base, or when the builder database is new.

Only job data is written, column by column, from a whitelist (nothing personal exists in the
builder database, and nothing here would copy it if it did). Descriptions are not published:
only a ~600-character excerpt that skips boilerplate; "Read the full posting" links to the employer.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import re
import shutil
import unicodedata
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import classify, db, geo, places
from .models import strings
from .sanitize import clean_url, html_to_text

FORMAT = 2                         # 2: normalized places (dict loc, column L); internships are a level
ASSETS = Path(__file__).resolve().parent.parent / "site"
# The page: every file in site/ (.github/workflows/website.yml publishes the same files on its own).
ASSET_FILES = tuple(sorted(p.name for p in ASSETS.iterdir() if p.is_file())) if ASSETS.is_dir() else ()
DETAIL_PER_FILE = 128
PREVIEW = 50                       # roles in top-<build>.json: the page's first page of results
GHOST_HIDE_AT = 3                  # the page hides roles with this ghost level by default (logic.js)
MAX_PLACES = 25                    # places kept per role in the list (all of them in its details)
EXCERPT_CHARS = 600
BASE_EVERY_DAYS = 7
DELTA_MAX_SHARE = 0.25             # a delta bigger than this share of the base: write a new base
EPOCH = datetime(2020, 1, 1, tzinfo=timezone.utc)
COUNTRY_BITS = {"US": 1, "CA": 2, "MX": 4}   # any other country: 8; unknown: 0

# Fixed vocabularies, so an index is the same number in every file of a base.
WORKPLACES = ["", "remote", "hybrid", "onsite"]
VISA = {None: 0, "yes": 1, "no": 2}
CLEARANCES = ["", "public_trust", "secret", "ts", "ts_sci", "unspecified"]
DEGREES = ["", "bachelors", "masters", "phd"]
SENIORITIES = [""] + classify.SENIORITY
EMP_TYPES = [""] + classify.EMP_TYPES
TRAVELS = [""] + classify.TRAVEL
INTERVALS = ["", "year", "hour"]

# Columns of the job list, in order. The page reads them by name from the file.
COLUMNS = ("t", "c", "L", "cc", "h", "f", "s", "e", "w", "p0", "p1", "pi", "cur", "y", "ys",
           "v", "z", "k", "g", "gh", "n", "sk", "b", "tr", "lg")
DICTS = ("company", "function", "currency", "skill", "language", "loc")


# ------------------------------------------------------------------ helpers

def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


_WORD = re.compile(r"[a-z0-9+#]+")


def words(text: str) -> set[str]:
    """Search words: lowercase, accents removed, split on anything but letters, digits, + and #.
    The page splits a query the same way (app logic: tokens())."""
    return {w for w in _WORD.findall(_fold(text)) if len(w) > 1 or w in ("c", "r")}


def shard_name(word: str) -> str:
    """File for a word: its first two characters, with anything but a-z0-9 as '_'."""
    head = (word + "_")[:2]
    return "".join(ch if ch.isascii() and ch.isalnum() else "_" for ch in head)


def key_of(uid: str) -> str:
    """A short key that stays the same for a posting across bases (for saved/applied/hidden)."""
    return hashlib.sha1(uid.encode()).hexdigest()[:12]


def _hours(iso: str | None) -> int:
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return 0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((t - EPOCH).total_seconds() // 3600))


def _varints(ids: list[int]) -> str:
    out, prev = bytearray(), -1
    for x in ids:
        d = x - prev - 1          # ids are sorted and distinct, so gaps start at 0
        prev = x
        while d >= 128:
            out.append((d & 127) | 128)
            d >>= 7
        out.append(d)
    return base64.b64encode(bytes(out)).decode()


_BOILERPLATE = re.compile(
    r"\b(equal (employment )?opportunity|eeo\b|affirmative action|without regard to|reasonable accommodation|"
    r"e-verify|pay transparency|privacy (notice|policy)|background check|drug[- ]free|protected veteran|"
    r"applicants? with disabilit|know your rights|recruitment fraud|we (are|re) an equal|"
    r"all qualified applicants|benefits (include|package)|our benefits|perks|about (us|the company)\b|"
    r"who we are\b|our (mission|story|values)\b|follow us|learn more about)", re.I)
_ROLE_HEAD = re.compile(
    r"^(about the (role|job|position|team|opportunity)|the role|role (overview|summary|description)|"
    r"(job|position) (summary|description|overview|purpose)|overview|summary|what you.ll (do|be doing)|"
    r"responsibilities|key responsibilities|your (role|responsibilities|impact)|in this role|the opportunity|"
    r"what you.ll bring|requirements|qualifications|minimum qualifications|basic qualifications|"
    r"what we.re looking for|who you are|you have)\b", re.I)


def excerpt(html: str, limit: int = EXCERPT_CHARS) -> str:
    """About `limit` characters that describe the role: starting at its responsibilities or summary
    when the posting has such a heading, skipping company boilerplate, ending on a sentence."""
    text = html_to_text(html or "")
    paras = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    if not paras:
        return ""
    start = next((i for i, p in enumerate(paras) if len(p) < 80 and _ROLE_HEAD.match(p.lstrip("-• "))), None)
    picked = paras[start + 1:] if start is not None else paras
    keep = [p for p in picked if not _BOILERPLATE.search(p)] or picked
    out = ""
    for p in keep:
        p = p.lstrip("-• ").strip()
        if len(p) < 3:
            continue
        nxt = (out + (" · " if out and len(p) < 120 and p[-1:] not in ".!?:" else " ") + p).strip()
        if len(nxt) > limit:
            if not out:                      # one long paragraph: cut it at a sentence or word
                cut = p[:limit]
                end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
                out = cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…"
            else:
                out += "…" if not out.endswith((".", "!", "?", "…")) else ""
            break
        out = nxt
    return out


# ------------------------------------------------------------------ state kept in the builder database

_STATE_TABLES = """
CREATE TABLE IF NOT EXISTS site_ids (uid TEXT PRIMARY KEY, id INTEGER NOT NULL, sig TEXT, in_base INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS site_excerpts (content_hash TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS site_files (name TEXT PRIMARY KEY, data BLOB NOT NULL);
"""


def _rows(conn, countries) -> list[dict]:
    """Open roles, one per de-duplicated cluster, in scope."""
    rows = [dict(r) for r in conn.execute(
        """SELECT uid, title, company, location, cluster_locations, cluster_id, cluster_count, effective_first_seen,
                  job_function, seniority, emp_type, workplace_eff, pay_min_annual, pay_max_annual, pay_currency,
                  pay_interval, yoe_min, yoe_source, visa, citizenship, clearance, degree, ghost_score, skills,
                  benefits, travel, languages, countries, content_hash, url, source, departments, ghost_reasons
           FROM jobs WHERE active = 1 AND is_canonical = 1""")]
    if countries:
        want = set(countries)
        rows = [r for r in rows if not (r["countries"] or "").strip(",")
                or want & set((r["countries"] or "").strip(",").split(","))]
    return rows


def _places(r: dict) -> list[str]:
    if "places" not in r:      # read three times per role (list, search words, details): parsed once
        r["places"] = strings(r["cluster_locations"]) or ([r["location"]] if r["location"] else [])
    return r["places"]


def _card(r, dicts: dict[str, dict]) -> list:
    """One role's list entry, by COLUMNS. Strings that repeat are numbers into a dictionary."""
    def ix(name, value):
        d = dicts[name]
        if value not in d:
            d[value] = len(d)
        return d[value]
    cc = 0
    for code in (r["countries"] or "").strip(",").split(","):
        if code:
            cc |= COUNTRY_BITS.get(code, 8)
    benefits = classify.decode_skills(r["benefits"])
    return [
        r["title"] or "",
        ix("company", r["company"] or ""),
        [ix("loc", e) for e in _loc_entries(r)],
        cc,
        _hours(r["effective_first_seen"]),
        ix("function", r["job_function"] or ""),
        SENIORITIES.index(level_of(r)) if level_of(r) in SENIORITIES else 0,
        EMP_TYPES.index(r["emp_type"]) if r["emp_type"] in EMP_TYPES else 0,
        WORKPLACES.index(r["workplace_eff"]) if r["workplace_eff"] in WORKPLACES else 0,
        round(r["pay_min_annual"] or 0),
        round(r["pay_max_annual"] or 0),
        INTERVALS.index(r["pay_interval"]) if r["pay_interval"] in INTERVALS else 0,
        ix("currency", r["pay_currency"] or ""),
        -1 if r["yoe_min"] is None else int(r["yoe_min"]),
        1 if r["yoe_source"] == "stated" else 0,           # as profile.check: only stated years rule anyone out
        VISA.get(r["visa"], 0),
        1 if r["citizenship"] else 0,
        CLEARANCES.index(r["clearance"]) if r["clearance"] in CLEARANCES else (5 if r["clearance"] else 0),
        DEGREES.index(r["degree"]) if r["degree"] in DEGREES else 0,
        int(r["ghost_score"] or 0),
        int(r["cluster_count"] or 1),
        [ix("skill", s) for s in classify.decode_skills(r["skills"])],
        sum(1 << i for i, b in enumerate(classify.BENEFITS) if b in benefits),
        TRAVELS.index(r["travel"]) if r["travel"] in TRAVELS else 0,
        [ix("language", lang) for lang in classify.decode_skills(r["languages"])],
    ]


def place_names() -> dict:
    """Names for the normalized places (in the weekly base: they change only with places.VERSION)."""
    return {"countries": {**dict(geo.CHOICES), **places.COUNTRY_NAMES},
            "regions": {f"{cc}-{code}": name for cc, t in places.REGION_NAMES.items() for code, name in t.items()},
            "metros": {slug: [label, cc, *aliases] for slug, (label, cc, _, aliases) in places.metros().items()},
            "nicknames": places.nicknames()}


def level_of(r) -> str | None:
    """The page's Experience level: the seniority, with every internship (by title or by the ATS's
    employment type) an internship, so the page needs no separate "Internship" job type."""
    return "intern" if r["emp_type"] == "internship" else r["seniority"]


def _loc_entries(r) -> list[str]:
    """A role's places for the list: normalized ("US|TX|Austin|austin"), or the employer's words
    ("~Plant 7") where they can't be read, in order, without repeats."""
    out: list[str] = []
    for p in _places(r)[:MAX_PLACES]:
        for e in [x.entry() for x in places.parse(p)] or ["~" + " ".join(p.split())]:
            if e not in out:
                out.append(e)
    return out


def _columns(cards: list[list]) -> dict:
    return {name: [c[i] for c in cards] for i, name in enumerate(COLUMNS)}


def _dict_list(d: dict) -> list:
    out = [None] * len(d)
    for value, i in d.items():
        out[i] = value
    return out


def _write_json(path: Path, obj) -> int:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    path.write_bytes(data)
    return len(data)


# ------------------------------------------------------------------ the generator

def _sig(card: list) -> str:
    return hashlib.blake2b(json.dumps(card).encode(), digest_size=8).hexdigest()


def generate(db_path: Path | str, out_dir: Path | str, countries=("US", "CA", "MX"),
             now: datetime | None = None, new_base: bool = False) -> dict:
    """Write the whole site into out_dir (replaced). Returns what it wrote, for the build log."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    data = out / "data"
    data.mkdir(parents=True)
    for f in ASSET_FILES:
        shutil.copy(ASSETS / f, out / f)
    (out / ".nojekyll").write_text("")

    conn = db.connect(db_path)
    try:
        conn.executescript(_STATE_TABLES)
        state = db.get_meta(conn, "site_state") or {}
        rows = _rows(conn, countries)
        by_uid = {r["uid"]: r for r in rows}

        age_days = ((now - datetime.fromisoformat(state["built_at"])).total_seconds() / 86400
                    if state.get("built_at") else None)
        reason = ("asked for" if new_base else "first site" if not state
                  else "format changed" if state.get("format") != FORMAT or state.get("places") != places.VERSION
                  else "weekly" if age_days is None or age_days >= BASE_EVERY_DAYS
                  else "base file missing" if not _has_base_file(conn, state["stamp"]) else None)
        delta = None
        if not reason:
            # Against the base: dictionaries only grow during its week, so every index stays valid.
            dicts = {name: {v: i for i, v in enumerate(state["dicts"].get(name, []))} for name in DICTS}
            known = {u: (i, sig, b) for u, i, sig, b in conn.execute("SELECT uid, id, sig, in_base FROM site_ids")}
            cards = {u: _card(r, dicts) for u, r in by_uid.items()}
            sigs = {u: _sig(c) for u, c in cards.items()}
            # Cumulative since the base: every open role not in it, every base role changed since,
            # and the base roles gone. A page applies only the latest delta, onto the base.
            delta_uids = [u for u in by_uid if u not in known or not known[u][2] or known[u][1] != sigs[u]]
            gone = sorted(i for u, (i, _, b) in known.items() if b and u not in by_uid)
            if len(delta_uids) + len(gone) > DELTA_MAX_SHARE * max(state["count"], 1):
                reason = "changes outgrew a quarter of the base"
        if reason:
            dicts = {name: {} for name in DICTS}
            ordered = sorted(rows, key=lambda r: (-_hours(r["effective_first_seen"]), r["uid"]))
            cards = {r["uid"]: _card(r, dicts) for r in ordered}
            with conn:
                conn.execute("DELETE FROM site_ids")
                conn.executemany("INSERT INTO site_ids (uid, id, sig, in_base) VALUES (?, ?, ?, 1)",
                                 [(r["uid"], i, _sig(cards[r["uid"]])) for i, r in enumerate(ordered)])
            state = {"format": FORMAT, "places": places.VERSION, "stamp": stamp, "built_at": now.isoformat(),
                     "count": len(ordered), "next_id": len(ordered), "dicts": {k: _dict_list(v) for k, v in dicts.items()}}
            state["base_lens"] = {k: len(v) for k, v in state["dicts"].items()}
            _store_base_file(conn, f"jobs-{stamp}.json", clear=True, obj={
                "format": FORMAT, "base": stamp, "n": len(ordered), "dicts": state["dicts"],
                "cols": _columns([cards[r["uid"]] for r in ordered]), "places": place_names()})
            _store_base_file(conn, f"keys-{stamp}.json", [key_of(r["uid"]) for r in ordered])
        else:
            new_ids = []
            for u in delta_uids:
                if u not in known:
                    known[u] = (state["next_id"], sigs[u], 0)
                    new_ids.append((u, state["next_id"], sigs[u]))
                    state["next_id"] += 1
            with conn:
                conn.executemany("INSERT INTO site_ids (uid, id, sig, in_base) VALUES (?, ?, ?, 0)", new_ids)
            state["dicts"] = {k: _dict_list(v) for k, v in dicts.items()}
            lens = state["base_lens"]
            delta = {"file": f"delta-{stamp}.json", "rows": len(delta_uids), "gone": len(gone)}
            delta["bytes"] = _write_json(data / delta["file"], {
                "format": FORMAT, "base": state["stamp"], "built": stamp,
                "ids": [known[u][0] for u in delta_uids], "keys": [key_of(u) for u in delta_uids], "gone": gone,
                "dicts": {k: v[lens.get(k, 0):] for k, v in state["dicts"].items()},   # added since the base
                "cols": _columns([cards[u] for u in delta_uids])})
        base_stamp = state["stamp"]
        base_bytes = _restore_base_file(conn, data, f"jobs-{base_stamp}.json")
        _restore_base_file(conn, data, f"keys-{base_stamp}.json")

        id_of = {u: i for u, i in conn.execute("SELECT uid, id FROM site_ids") if u in by_uid}

        # Keyword index
        postings: dict[str, list[int]] = {}
        for u, r in by_uid.items():
            text = " ".join([r["title"] or "", r["company"] or "", " ".join(_places(r)), r["job_function"] or "",
                             " ".join(classify.decode_skills(r["skills"])), " ".join(_departments(r))])
            for w in words(text):
                postings.setdefault(w, []).append(id_of[u])
        wdir = data / f"w-{stamp}"
        wdir.mkdir()
        shards: dict[str, dict] = {}
        for w in sorted(postings):
            sh = shards.setdefault(shard_name(w), {"w": [], "p": []})
            sh["w"].append(w)
            sh["p"].append(_varints(sorted(postings[w])))
        word_bytes = sum(_write_json(wdir / f"{name}.json", sh) for name, sh in shards.items())

        # Details, 128 roles per file, by id
        excerpts = _excerpts(conn, [r["content_hash"] for r in rows if r["content_hash"]])
        links = _apply_links(conn, {r["cluster_id"] or r["uid"] for r in rows})
        ddir = data / f"d-{stamp}"
        ddir.mkdir()
        files: dict[int, dict] = {}
        for u, r in by_uid.items():
            i = id_of[u]
            own = clean_url(r["url"])
            files.setdefault(i // DETAIL_PER_FILE, {})[str(i)] = {
                "k": key_of(u), "x": excerpts.get(r["content_hash"], ""),
                "u": links.get(r["cluster_id"] or u) or ([[r["source"], own]] if own else []),
                "L": _places(r), "dep": _departments(r), "gr": strings(r["ghost_reasons"])}
        detail_bytes = sum(_write_json(ddir / f"{n}.json", f) for n, f in files.items())

        sources_file = f"sources-{stamp}.json"
        _write_json(data / sources_file, _sources(conn))

        # What a first visit sees first: the default view's first page, as a tiny job list of its own,
        # so it shows while the whole list downloads (logic.js computes the same page from the list).
        h_col, gh_col = COLUMNS.index("h"), COLUMNS.index("gh")
        top = heapq.nsmallest(PREVIEW, (u for u in by_uid if cards[u][gh_col] < GHOST_HIDE_AT),
                              key=lambda u: (-cards[u][h_col], id_of[u]))
        top_dicts: dict[str, dict] = {name: {} for name in DICTS}
        top_cards = [_card(by_uid[u], top_dicts) for u in top]
        top_file = f"top-{stamp}.json"
        top_bytes = _write_json(data / top_file, {
            "format": FORMAT, "n": len(top), "ids": [id_of[u] for u in top],
            "dicts": {k: _dict_list(v) for k, v in top_dicts.items()}, "cols": _columns(top_cards),
            "places": place_names()})

        latest = {
            "format": FORMAT, "built_at": now.isoformat(timespec="seconds"), "stamp": stamp,
            "base": {"stamp": base_stamp, "file": f"jobs-{base_stamp}.json", "keys": f"keys-{base_stamp}.json",
                     "count": state["count"]},
            "delta": delta, "words": f"w-{stamp}", "detail": f"d-{stamp}", "per_detail": DETAIL_PER_FILE,
            "sources": sources_file, "top": top_file, "jobs": len(rows), "companies": len({r["company"] for r in rows}),
            "countries": list(countries or []),
            "vocab": {"workplace": WORKPLACES, "seniority": SENIORITIES, "emp": EMP_TYPES, "clearance": CLEARANCES,
                      "degree": DEGREES, "travel": TRAVELS, "interval": INTERVALS, "benefits": classify.BENEFITS,
                      "visa": ["", "yes", "no"]},
            "labels": {"seniority": classify.SENIORITY_LABEL, "emp": classify.EMP_LABEL,
                       "benefits": classify.BENEFIT_LABEL, "travel": classify.TRAVEL_LABEL,
                       "functions": classify.FUNCTION_NAMES, "languages": classify.LANGUAGES},
        }
        _write_json(data / "latest.json", latest)
        db.set_meta(conn, "site_state", state)
        conn.commit()
    finally:
        conn.close()
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return {"new_base": reason, "jobs": len(rows), "base_bytes": base_bytes, "delta": delta,
            "words_bytes": word_bytes, "words_files": len(shards), "detail_bytes": detail_bytes,
            "detail_files": len(files), "top_bytes": top_bytes, "site_bytes": total}


def _departments(r: dict) -> list[str]:
    if "deps" not in r:
        r["deps"] = strings(r["departments"])[:5]
    return r["deps"]


def _excerpts(conn, hashes: list[str]) -> dict[str, str]:
    """Excerpts by description hash, cached in the builder database (most don't change)."""
    have = {h: t for h, t in conn.execute("SELECT content_hash, text FROM site_excerpts")}
    todo = [h for h in set(hashes) if h not in have]
    new = []
    for h in todo:
        row = conn.execute("SELECT c.html FROM jobs j JOIN job_content c ON c.uid = j.uid "
                           "WHERE j.content_hash = ? LIMIT 1", (h,)).fetchone()
        text = excerpt(db.unpack(row[0])) if row else ""
        have[h] = text
        new.append((h, text))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO site_excerpts (content_hash, text) VALUES (?, ?)", new)
        live = set(hashes)
        stale = [(h,) for h in have if h not in live]
        conn.executemany("DELETE FROM site_excerpts WHERE content_hash = ?", stale)
    return have


def _apply_links(conn, clusters: set[str]) -> dict[str, list]:
    """Every open listing of each role, tracking parameters removed, one per address."""
    out: dict[str, list] = {}
    seen: dict[str, set] = {}
    # a posting not (yet) in a cluster is its own: never group postings by a missing cluster id
    for cid, url, company, source in conn.execute(
            "SELECT COALESCE(cluster_id, uid), url, company, source FROM jobs WHERE active = 1 "
            "ORDER BY is_canonical DESC, first_seen"):
        if cid not in clusters:
            continue
        u = clean_url(url)
        if not u or u in seen.setdefault(cid, set()):
            continue
        seen[cid].add(u)
        lst = out.setdefault(cid, [])
        if len(lst) < 8:
            lst.append([source, u])
    return out


_SYSTEM_NAMES = {"successfactors": "SAP SuccessFactors", "eightfold": "Eightfold", "icims": "iCIMS", "paradox": "Paradox",
                 "avature": "Avature", "taleo": "Oracle Taleo", "adp": "ADP", "brassring": "BrassRing",
                 "talentbrew": "TalentBrew", "workday": "Workday", "smartrecruiters": "SmartRecruiters",
                 "cornerstone": "Cornerstone", "greenhouse": "Greenhouse", "jobvite": "Jobvite", "oracle": "Oracle",
                 "jibe": "iCIMS", "jsonld": "its own site", "ashby": "Ashby", "lever": "Lever", "amazon": "its own site",
                 "phenom": "Phenom", "workable": "Workable", "recruitee": "Recruitee"}


def _why(status: str, detail: str | None) -> str:
    """Why a listed company isn't covered, in a few plain words (never a URL or an error dump)."""
    d = (detail or "").lower()
    if status == "unsupported":
        name = _SYSTEM_NAMES.get(d.replace("uses ", "").strip(), "")
        return f"Uses {name}, which Lighthouse can't read yet" if name else "Its careers system can't be read yet"
    if status == "blocked":
        return "Its site asks automated readers to stay away"
    if status == "not_found":
        return "Has several job sites; the right one isn't chosen yet" if "under other names" in d \
            else "No readable job list found on its careers page"
    if status == "error":
        return "Its job site has moved or closed" if "switched off" in d else "Its careers page didn't open"
    return ""


def _sources(conn) -> dict:
    companies = []
    if db.has_table(conn, "companies"):
        for r in conn.execute("SELECT company, grp, ats, status, detail, openings, checked_at FROM companies "
                              "ORDER BY company COLLATE NOCASE"):
            c = dict(r)
            covered = c["status"] in ("ok", "pinned")
            c["why"] = "" if covered else _why(c["status"], c.pop("detail"))
            c.pop("detail", None)
            c["system"] = _SYSTEM_NAMES.get(c["ats"] or "", c["ats"] or "") if covered else ""
            companies.append(c)
    boards = [dict(r) for r in conn.execute(
        "SELECT ats, slug, company, job_count, last_ok, last_error FROM boards WHERE enabled = 1 ORDER BY company")]
    for b in boards:                       # a sentence for people, never a stack trace
        b["last_error"] = (b["last_error"] or "")[:160]
    health = dict(db.get_meta(conn, "build_health") or {})
    health.pop("failing", None)            # the boards above carry their own last answer
    return {"companies": companies, "boards": boards, "health": health}


# The base file is written once a week but must be in every deploy (each replaces the whole site).
# The builder database keeps it (compressed), so it travels with the database in the Actions cache.

def _store_base_file(conn, name: str, obj, clear: bool = False) -> None:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    with conn:
        if clear:                                   # a new base: last week's files go
            conn.execute("DELETE FROM site_files")
        conn.execute("INSERT OR REPLACE INTO site_files (name, data) VALUES (?, ?)", (name, zlib.compress(data, 6)))


def _has_base_file(conn, stamp: str) -> bool:
    return conn.execute("SELECT COUNT(*) FROM site_files WHERE name IN (?, ?)",
                        (f"jobs-{stamp}.json", f"keys-{stamp}.json")).fetchone()[0] == 2


def _restore_base_file(conn, data_dir: Path, name: str) -> int:
    row = conn.execute("SELECT data FROM site_files WHERE name = ?", (name,)).fetchone()
    raw = zlib.decompress(row[0])
    (data_dir / name).write_bytes(raw)
    return len(raw)
