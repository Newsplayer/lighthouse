"""The website (lighthouse/site.py + site/): generated files, update files, and the page's logic
agreeing with the local app's (Node runs site/logic.js; a browser runs the page)."""
import functools
import http.server
import json
import random
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_index import Builder, job

from lighthouse import classify, db, places, profile, site

NODE = shutil.which("node")
LOGIC = (Path(__file__).resolve().parent.parent / "site" / "logic.js").as_uri()
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
needs_node = pytest.mark.skipif(not NODE, reason="Node.js isn't installed")


def node(script: str, payload) -> object:
    """Run an ES module with `logic` imported and `input` parsed from stdin; it prints JSON."""
    code = f"import * as logic from {json.dumps(LOGIC)};\n" \
           "const input = JSON.parse(require_stdin());\n" + script
    code = code.replace("require_stdin()", "(await new Promise((ok) => { let s = ''; process.stdin.on('data', (d) => s += d); "
                                           "process.stdin.on('end', () => ok(s)); }))")
    out = subprocess.run([NODE, "--input-type=module", "-e", code], input=json.dumps(payload), capture_output=True,
                         encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


FUNCTIONS = ["Software Engineering", "Data & Analytics", "Finance & Accounting", ""]
# Location strings as careers systems write them, and the country each is in.
PLACES = {"Austin, TX": "US", "New York, NY": "US", "Remote - US": "US", "Toronto, ON": "CA", "Monterrey, NL": "MX",
          "London, UK": "GB", "Denver, CO": "US", "US, TX, Round Rock": "US", "NY-New York": "US",
          "Mississauga, Ontario, Canada": "CA", "Greater Seattle Area": "US", "Ciudad de México": "MX",
          "Remote (Canada)": "CA", "Texas": "US"}


def varied_builder(tmp_path, n=80, seed=7) -> Builder:
    """A builder database whose jobs cover every filter field, set directly so each value is known."""
    b = Builder(tmp_path)
    b.ingest("acme", [job(i, title=f"{['Data', 'Software', 'Finance'][i % 3]} Engineer {i}") for i in range(1, n + 1)])
    vary(b, seed)
    return b


def vary(b, seed=7):
    """Set every filter field of every job from a generator seeded by its uid (re-running it after an
    ingest, which re-derives fields, gives existing jobs the same values again)."""
    conn = db.connect(b.path)
    for (uid,) in conn.execute("SELECT uid FROM jobs").fetchall():
        rng = random.Random(f"{seed}:{uid}")
        places = rng.sample(sorted(PLACES), rng.choice([1, 1, 2, 3]))
        codes = sorted({PLACES[p] for p in places})
        pay = rng.choice([None, 60000, 95000, 120000, 180000])
        yoe = rng.choice([None, 0, 2, 3, 5, 8])
        conn.execute("""UPDATE jobs SET visa = ?, citizenship = ?, clearance = ?, workplace_eff = ?, yoe_min = ?,
                        yoe_source = ?, pay_min_annual = ?, pay_max_annual = ?, pay_currency = ?, pay_interval = ?,
                        degree = ?, job_function = ?, seniority = ?, emp_type = ?, skills = ?, benefits = ?, travel = ?,
                        languages = ?, countries = ?, cluster_locations = ?, location = ?, effective_first_seen = ?,
                        ghost_score = ?, is_canonical = 1 WHERE uid = ?""",
                     (rng.choice([None, "yes", "no"]), rng.choice([None, 0, 1]),
                      rng.choice([None, None, "public_trust", "secret", "ts", "ts_sci"]),
                      rng.choice([None, "remote", "hybrid", "onsite"]), yoe,
                      None if yoe is None else rng.choice(["stated", "stated", "title"]),
                      pay and pay - 20000, pay, pay and "USD", pay and rng.choice(["year", "year", "hour"]),
                      rng.choice([None, "bachelors", "masters", "phd"]), rng.choice(FUNCTIONS) or None,
                      rng.choice([None, "entry", "mid", "senior"]), rng.choice([None, "full_time", "contract"]),
                      classify.encode_skills(rng.sample(["python", "sql", "excel", "aws"], rng.randint(0, 2))),
                      classify.encode_list(rng.sample(classify.BENEFITS, rng.randint(0, 3))),
                      rng.choice([None, "none", "occasional", "frequent"]),
                      classify.encode_list(rng.sample(["spanish", "french"], rng.randint(0, 1))),
                      "," + ",".join(codes) + ",", json.dumps(places), places[0],
                      (NOW - timedelta(days=rng.randint(0, 40), hours=rng.randint(0, 20))).isoformat(),
                      rng.choice([0, 0, 0, 1, 2, 3]), uid))
    conn.commit()
    conn.close()


def gen(b, out, now=NOW, **kw):
    return site.generate(b.path, out, countries=(), now=now, **kw)


def read(out, name):
    return json.loads((out / "data" / name).read_text())


# ------------------------------------------------------------------ the generated files

def test_site_files_and_nothing_personal(tmp_path):
    b = varied_builder(tmp_path)
    conn = db.connect(b.path)
    db.set_meta(conn, "profile", {"resume": "MY-PRIVATE-RESUME-TEXT", "about": "secret wish"})
    conn.commit()
    conn.close()
    out = tmp_path / "site"
    stats = gen(b, out)
    latest = read(out, "latest.json")
    assert stats["new_base"] == "first site" and latest["delta"] is None and latest["jobs"] == 80
    base = read(out, latest["base"]["file"])
    assert base["n"] == 80 and set(base["cols"]) == set(site.COLUMNS) and len(read(out, latest["base"]["keys"])) == 80
    for f in ("index.html", "app.js", "logic.js", "app.css", ".nojekyll"):
        assert (out / f).exists()
    details = {}
    for p in (out / "data" / latest["detail"]).glob("*.json"):
        details.update(json.loads(p.read_text()))
    assert len(details) == 80
    for d in details.values():
        assert all(u.startswith(("https://", "http://")) for _, u in d["u"])
        assert "equal opportunity" not in d["x"].lower()            # boilerplate skipped
    everything = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    assert b"MY-PRIVATE-RESUME-TEXT" not in everything and b"secret wish" not in everything


def test_links_never_cross_postings_without_a_cluster(tmp_path):
    b = varied_builder(tmp_path, n=6)
    conn = db.connect(b.path)
    conn.execute("UPDATE jobs SET cluster_id = NULL")
    conn.commit()
    conn.close()
    out = tmp_path / "site"
    gen(b, out)
    latest = read(out, "latest.json")
    details = json.loads(next((out / "data" / latest["detail"]).glob("*.json")).read_text())
    assert all(len(d["u"]) == 1 for d in details.values())


def test_updates_are_small_files_with_stable_ids(tmp_path):
    b = varied_builder(tmp_path, n=40)
    out = tmp_path / "site"
    gen(b, out)
    base_file = read(out, "latest.json")["base"]["file"]
    conn = db.connect(b.path)
    id_of = dict(conn.execute("SELECT uid, id FROM site_ids"))
    closed, edited = "greenhouse:acme:1", "greenhouse:acme:2"
    conn.execute("UPDATE jobs SET active = 0 WHERE uid = ?", (closed,))
    conn.execute("UPDATE jobs SET title = 'Renamed role' WHERE uid = ?", (edited,))
    conn.commit()
    conn.close()
    b.ingest("beta", [job(900, title="Brand new role", board="beta", company="Beta")])
    vary(b)
    conn = db.connect(b.path)
    conn.execute("UPDATE jobs SET title = 'Brand new role' WHERE uid = 'greenhouse:beta:900'")
    conn.execute("UPDATE jobs SET title = 'Renamed role' WHERE uid = ?", (edited,))
    conn.commit()
    conn.close()
    gen(b, out, now=NOW + timedelta(hours=1))
    latest = read(out, "latest.json")
    assert latest["base"]["file"] == base_file and (out / "data" / base_file).exists()   # base kept for the week
    d1 = read(out, latest["delta"]["file"])
    assert id_of[closed] in d1["gone"] and id_of[edited] in d1["ids"]
    new_id = [i for i in d1["ids"] if i >= 40]
    assert new_id == [40] and "Brand new role" in d1["cols"]["t"]
    b.ingest("beta", [job(900, title="Brand new role", board="beta", company="Beta"),
                      job(901, title="Another new role", board="beta", company="Beta")])
    vary(b)
    conn = db.connect(b.path)
    conn.execute("UPDATE jobs SET title = 'Renamed role' WHERE uid = ?", (edited,))
    conn.execute("UPDATE jobs SET active = 0 WHERE uid = ?", (closed,))
    conn.commit()
    conn.close()
    gen(b, out, now=NOW + timedelta(hours=2))
    d2 = read(out, read(out, "latest.json")["delta"]["file"])
    assert {40, 41} <= set(d2["ids"]) and id_of[edited] in d2["ids"]     # cumulative since the base, same ids
    assert gen(b, out, now=NOW + timedelta(days=8))["new_base"] == "weekly"
    assert read(out, "latest.json")["delta"] is None


def test_excerpt_starts_at_the_role_and_skips_boilerplate():
    html = ("<h2>About us</h2><p>Acme is a leading provider of things since 1902 and our mission is to delight.</p>"
            "<h2>What you'll do</h2><ul><li>Build data pipelines in Python.</li><li>Own the warehouse.</li></ul>"
            "<p>Acme is an equal opportunity employer. All qualified applicants will receive consideration.</p>")
    text = site.excerpt(html)
    assert text.startswith("Build data pipelines in Python.") and "Own the warehouse" in text
    assert "equal opportunity" not in text and "since 1902" not in text
    assert len(site.excerpt("<p>" + "word " * 400 + "</p>")) <= site.EXCERPT_CHARS + 1


# ------------------------------------------------------------------ the page's logic agrees with the local app

def _store_script():
    return """
import { readFileSync } from "node:fs";
const dir = input.dir;
const latest = JSON.parse(readFileSync(`${dir}/data/latest.json`));
const base = JSON.parse(readFileSync(`${dir}/data/${latest.base.file}`));
const delta = latest.delta ? JSON.parse(readFileSync(`${dir}/data/${latest.delta.file}`)) : null;
const store = logic.buildStore(base, delta, latest.vocab);
"""


@needs_node
def test_page_filters_match_the_local_app(tmp_path):
    b = varied_builder(tmp_path)
    out = tmp_path / "site"
    gen(b, out)
    conn = db.connect(b.path)
    uid_of = {i: u for u, i in conn.execute("SELECT uid, id FROM site_ids")}
    prof = {**profile.DEFAULT, "years_experience": 3, "needs_sponsorship": True,   # where: the Where filter, not the profile
            "min_salary": 100000, "degree": "bachelors"}
    cases = [  # (local app Filters; a list = any of them, the page's filters)
        ({}, {}),
        ({"function": "Software Engineering"}, {"fns": ["Software Engineering"]}),
        ([{"function": "Software Engineering"}, {"function": "Data & Analytics"}],
         {"fns": ["Software Engineering", "Data & Analytics"]}),
        ({"seniority": "senior", "emp_type": "full_time"}, {"levels": ["senior"], "emps": ["full_time"]}),
        ([{"seniority": "entry"}, {"seniority": "mid"}], {"levels": ["entry", "mid"]}),
        ([{"emp_type": "full_time"}, {"emp_type": "contract"}], {"emps": ["full_time", "contract"]}),
        ({"workplace": "remote", "min_salary": 100000}, {"workplaces": ["remote"], "minSalary": 100000}),
        ([{"workplace": "remote"}, {"workplace": "hybrid"}], {"workplaces": ["remote", "hybrid"]}),
        ({"company": "Acme"}, {"companies": ["Acme"]}),
        ({"yoe_lo": 2, "yoe_hi": 5}, {"yoeLo": 2, "yoeHi": 5}),
        ({"yoe_hi": 3, "yoe_unknown": False}, {"yoeHi": 3, "yoeUnknown": False}),
        ({"pay_listed": True}, {"payListed": True}),
        ({"visa": "yes"}, {"visa": "yes"}),
        ({"clearance": "required"}, {"clearance": "required"}),
        ({"benefits": ("equity", "health")}, {"benefits": ["equity", "health"]}),
        ({"travel": "low", "language": "english"}, {"travelLow": True, "language": "english"}),
        ({"language": "spanish"}, {"language": "spanish"}),
        ({"skills": ("python",), "posted_within": 7}, {"skills": ["python"], "postedWithin": 7}),
        ({"show_ghosts": True}, {"showGhosts": True}),
        ({"eligible_only": True, "profile": prof}, {"eligibleOnly": True}),
    ]
    expected = []
    for py, _ in cases:
        uids = set()
        for one in py if isinstance(py, list) else [py]:
            rows, _ = db.list_jobs(conn, db.Filters(**{"eligible_only": False, **one}), per_page=1000, with_count=False)
            uids |= {r["uid"] for r in rows}
        expected.append(sorted(uids))
    conn.close()
    got = node(_store_script() + """
const out = input.cases.map((f) => logic.filterIds(store, {...logic.freshFilters(), eligibleOnly: false, ...f},
  {now: logic.nowHours(), profile: input.profile}));
console.log(JSON.stringify(out));
""", {"dir": str(out), "cases": [js for _, js in cases], "profile": prof})
    for (py, _), want, ids in zip(cases, expected, got):
        assert sorted(uid_of[i] for i in ids) == want, py
    assert all(expected[1:9]) and expected[0] != expected[1]          # the cases select something, not everything


def _where_expected(r, where: set, remote_too: bool, metro_country: dict) -> bool:
    """The page's place filter, in Python: any of the role's places is in a chosen place, or (remote
    roles, when asked) the role is open in a chosen place's country."""
    locs = site._places(r)[:site.MAX_PLACES]
    if places.keys_of(locs) & where:
        return True
    want = {metro_country[k[1:]] if k.startswith("@") else k[:2] for k in where}
    have = {p.country for loc in locs for p in places.parse(loc)}
    return bool(remote_too and r["workplace_eff"] == "remote" and have & want)


@needs_node
def test_page_place_filter_matches_python(tmp_path):
    b = varied_builder(tmp_path, n=120, seed=5)
    out = tmp_path / "site"
    gen(b, out)
    conn = db.connect(b.path)
    rows = {i: conn.execute("SELECT * FROM jobs WHERE uid = ?", (u,)).fetchone()
            for u, i in conn.execute("SELECT uid, id FROM site_ids")}
    conn.close()
    metro_country = {slug: cc for slug, (_, cc, _, _) in places.metros().items()}
    cases = [["US-TX-Austin"], ["@austin"], ["US-TX"], ["US"], ["CA"], ["MX-CMX-Mexico City"], ["@toronto"],
             ["US-NY-New York", "CA-ON"], ["@seattle"], ["MX"], ["US-CO-Denver", "@cdmx"]]
    want = [[sorted(i for i, r in rows.items() if _where_expected(r, set(w), rt, metro_country)) for w in cases]
            for rt in (True, False)]
    got = node(_store_script() + """
const res = [true, false].map((rt) => input.cases.map((w) => logic.filterIds(store,
  {...logic.freshFilters(), eligibleOnly: false, showGhosts: true, where: w, remoteToo: rt}, {now: logic.nowHours()})));
const labels = Object.fromEntries(input.cases.flat().map((k) => [k, logic.keyLabel(store, k)]));
const counts = logic.placeCounts(store, logic.filterIds(store, {...logic.freshFilters(), eligibleOnly: false, showGhosts: true}, {now: logic.nowHours()}));
const sugg = Object.fromEntries(["aus", "round", "texas", "new york", "gta", "cdmx", "seattle area"].map((q) =>
  [q, logic.suggestPlaces(store, q, counts).map((s) => s.label)]));
console.log(JSON.stringify({res, labels, sugg}));
""", {"dir": str(out), "cases": cases})
    for rt in (0, 1):
        for w, exp, ids in zip(cases, want[rt], got["res"][rt]):
            assert sorted(ids) == exp, (w, rt)
    assert all(want[0]) and want[0] != want[1]                     # each choice finds roles; remote ones join in
    assert got["labels"]["US-TX-Austin"] == "Austin, TX" and got["labels"]["@austin"] == "Austin area"
    assert got["labels"]["US-TX"] == "Texas" and got["labels"]["MX-CMX-Mexico City"] == "Mexico City"
    s = got["sugg"]
    assert "Austin, TX" in s["aus"] and "Austin area" in s["aus"] and s["round"] == ["Round Rock, TX"]
    assert s["texas"][0] == "Texas" and s["new york"][0] == "New York, NY"
    assert s["gta"] == ["Toronto area (GTA)"] and s["cdmx"][0] == "Mexico City" and s["seattle area"] == ["Seattle area"]


def test_places_normalize_the_ways_careers_sites_write_a_city():
    same = {
        "US|NY|New York|nyc": ["New York, NY", "NY-New York", "New York City", "New York, New York, United States",
                                    "US, NY, New York", "New York", "NYC", "Brooklyn, NY", "New York New York United States"],
        "US|CA|Santa Clara|sf-bay": ["US, CA, Santa Clara", "Santa Clara, California", "Santa Clara, CA 95054"],
        "US|TX|Dallas|dfw": ["TX-Dallas", "Dallas, TX, United States", "Dallas Texas", "03209 - UT Southwstrn-Dallas D"],
        "CA|QC|Longueuil|montreal": ["CA-QC-Longueuil", "Longueuil, Quebec, Canada",
                                     "CA-QC-LONGUEUIL-J01 ~ 1000 Blvd Marie-Victorin ~ J01 BLDG"],
        "MX|NLE|Monterrey|monterrey": ["Monterrey, NL", "Monterrey, N.L., México", "MX-NLE-Monterrey"],
        "MX|CMX|Mexico City|cdmx": ["Ciudad de México", "CDMX", "Mexico City, Mexico"],
        "CA|ON|Barrie|": ["Barrie, CA"],                         # "CA" can be Canada
        "US|CA|Ontario|": ["Ontario, CA"],                       # ... or California
        "US|AZ|Peoria|phoenix": ["Store 03812 Peoria AZ"],
        "US|WI|Madison|madison": ["Madison, WI", "Store 02521 Madison WI"],     # a common word, but WI says which
        "US|DC|Washington|dc": ["Washington, DC", "Washington DC", "DC - Washington (20006)", "Washington, D.C."],
        "US|WA||seattle": ["Greater Seattle Area"],
        "US|CA||sf-bay": ["Bay Area", "Silicon Valley"],
        "US|TX||": ["Texas", "TX, Working at Home - Texas"],
        "US|||": ["United States", "Remote - US", "USA - Remote", "Washington"],   # the state or the capital
    }
    for want, texts in same.items():
        for t in texts:
            assert [p.entry() for p in places.parse(t)] == [want], t
    assert [p.entry() for p in places.parse("Houston, TX, Milford, UT")] == ["US|TX|Houston|houston", "US|UT|Milford|"]
    assert [p.entry() for p in places.parse("Remote (United States | Canada)")] == ["US|||", "CA|||"]
    for abroad, cc in (("London", "GB"), ("Bangalore, KA, IN, 560066", "IN"), ("Campo Largo, PR, Brazil", "BR"),
                       ("Marktoberdorf, DE", "DE")):
        assert [p.entry() for p in places.parse(abroad)] == [f"{cc}|||"], abroad
    for unknown in ("Remote", "", "Richmond", "Cambridge", "Penn State University Park"):
        assert places.parse(unknown) == (), unknown            # several cities share it: nothing guessed
    assert places.parse("London, ON")[0].city == "London" and places.parse("Paris, TX")[0].region == "TX"
    assert places.parse("Austin, TX")[0].keys() == {"US", "US-TX", "@austin", "US-TX-Austin"}


@needs_node
def test_page_levels_put_internships_together(tmp_path):
    b = varied_builder(tmp_path, n=30)
    conn = db.connect(b.path)
    conn.execute("UPDATE jobs SET emp_type = 'internship', seniority = NULL WHERE CAST(external_id AS INT) % 4 = 0")
    conn.execute("UPDATE jobs SET seniority = 'intern', emp_type = 'full_time' WHERE CAST(external_id AS INT) % 4 = 1")
    conn.commit()
    conn.close()
    out = tmp_path / "site"
    gen(b, out)
    conn = db.connect(b.path)
    rows = {i: conn.execute("SELECT * FROM jobs WHERE uid = ?", (u,)).fetchone()
            for u, i in conn.execute("SELECT uid, id FROM site_ids")}
    conn.close()
    got = node(_store_script() + """
console.log(JSON.stringify(input.levels.map((l) => logic.filterIds(store,
  {...logic.freshFilters(), eligibleOnly: false, showGhosts: true, levels: l}, {now: logic.nowHours()}))));
""", {"dir": str(out), "levels": [["intern"], ["entry", "mid"]]})
    interns = sorted(i for i, r in rows.items() if site.level_of(r) == "intern")
    assert sorted(got[0]) == interns and len(interns) >= 14
    assert sorted(got[1]) == sorted(i for i, r in rows.items() if site.level_of(r) in ("entry", "mid"))


@needs_node
def test_page_unanswered_questions_never_hide_a_role(tmp_path):
    b = varied_builder(tmp_path, n=120, seed=11)
    out = tmp_path / "site"
    gen(b, out)
    conn = db.connect(b.path)
    rows = {i: conn.execute("SELECT * FROM jobs WHERE uid = ?", (u,)).fetchone()
            for u, i in conn.execute("SELECT uid, id FROM site_ids")}
    conn.close()
    answers = [{"sponsorship": True}, {"citizen": False}, {"citizen": True, "clearance": "secret"},
               {"degree": "bachelors"}, {"sponsorship": False, "citizen": True, "degree": "none", "clearance": "none"}]
    got = node(_store_script() + """
const ids = Object.keys(input.ids).map(Number).sort((a, b) => a - b);
console.log(JSON.stringify({none: logic.profileFrom(logic.NO_ANSWERS),
  res: input.answers.map((a) => { const p = logic.profileFrom({...logic.NO_ANSWERS, ...a});
    return ids.map((i) => logic.eligibility(store, i, p)); })}));
""", {"dir": str(out), "answers": answers, "ids": {str(i): 1 for i in rows}})
    assert got["none"] is None
    for a, res in zip(answers, got["res"]):
        # what the answers mean for lighthouse/profile.py: unanswered = never a reason
        p = {**profile.DEFAULT, "needs_sponsorship": a.get("sponsorship") is True, "us_citizen": a.get("citizen") is not False,
             "degree": a.get("degree") or "phd", "clearance": a.get("clearance") or "none", "years_experience": None,
             "workplaces": ["remote", "hybrid", "onsite"], "min_salary": None}
        assert res == [profile.check(rows[i], p) for i in sorted(rows)], a
        reasons = " ".join(" ".join(r) for r in res)
        assert ("citizenship" in reasons) == (a.get("citizen") is False) and ("degree" in reasons) == ("degree" in a)
        assert ("sponsor" in reasons) == (a.get("sponsorship") is True)


@needs_node
def test_page_filters_survive_the_address_bar():
    got = node("""
const f = {...logic.freshFilters(), q: "data engineer", where: ["US-TX-Austin", "@sf-bay"], levels: ["entry", "mid"],
  workplaces: ["remote"], companies: ["Johnson & Johnson", "AT&T, Inc."], yoeHi: 3, remoteToo: false, minSalary: 100000};
const back = logic.filtersFromHash("#" + logic.filtersToHash(f));
const old = logic.filtersFromHash("#sen=senior&emp=contract&fn=Sales&company=Acme&countries=CA,MX&skills=python,sql&workplace=remote");
console.log(JSON.stringify({same: JSON.stringify(back) === JSON.stringify(f), old, empty: logic.filtersToHash(logic.freshFilters()),
  bad: logic.filtersFromHash("#q=%E0%A4%A&levels=senior")}));
""", None)
    assert got["same"] and got["empty"] == ""
    old = got["old"]
    assert old["levels"] == ["senior"] and old["emps"] == ["contract"] and old["fns"] == ["Sales"]
    assert old["companies"] == ["Acme"] and old["where"] == ["CA", "MX"] and old["skills"] == ["python", "sql"]
    assert old["workplaces"] == ["remote"] and got["bad"]["levels"] == ["senior"] and got["bad"]["q"] == ""


@needs_node
def test_page_eligibility_matches_profile_check(tmp_path):
    b = varied_builder(tmp_path, n=120, seed=11)
    out = tmp_path / "site"
    gen(b, out)
    rng = random.Random(3)
    profiles = [{**profile.DEFAULT, "years_experience": rng.choice([None, 0, 2, 5, 10]),
                 "degree": rng.choice(["none", "bachelors", "masters", "phd"]),
                 "needs_sponsorship": rng.random() < .4, "us_citizen": rng.random() < .5,
                 "clearance": rng.choice(["none", "public_trust", "secret", "ts", "ts_sci"]),
                 "locations": [],                   # the page asks where with the Where filter
                 "workplaces": rng.sample(["remote", "hybrid", "onsite"], rng.randint(1, 3)),
                 "min_salary": rng.choice([None, 90000, 150000])} for _ in range(40)]
    conn = db.connect(b.path)
    rows = {i: conn.execute("SELECT * FROM jobs WHERE uid = ?", (u,)).fetchone()
            for u, i in conn.execute("SELECT uid, id FROM site_ids")}
    conn.close()
    want = [[profile.check(rows[i], p) for i in sorted(rows)] for p in profiles]
    got = node(_store_script() + """
const ids = Object.keys(input.ids).map(Number).sort((a, b) => a - b);
console.log(JSON.stringify(input.profiles.map((p) => ids.map((i) => logic.eligibility(store, i, p)))));
""", {"dir": str(out), "profiles": profiles, "ids": {str(i): 1 for i in rows}})
    assert got == want


@needs_node
def test_page_search_words_match_the_index():
    texts = ["Senior C++ / C# Engineer", "Ingeniero de Datos – Querétaro", "Node.js, R and SQL", "Data-Analyst (Remote)"]
    got = node("console.log(JSON.stringify(input.map((t) => { const w = logic.tokens(t).sort(); return [w, w.map(logic.shardName)]; })));",
               texts)
    for t, (tokens, shards) in zip(texts, got):
        assert tokens == sorted(site.words(t)), t
        assert shards == [site.shard_name(w) for w in sorted(site.words(t))]


@needs_node
def test_page_search_finds_by_prefix(tmp_path):
    b = varied_builder(tmp_path, n=30)
    conn = db.connect(b.path)
    conn.execute("UPDATE jobs SET title = 'Quokka Wrangler ' || external_id WHERE CAST(external_id AS INT) % 5 = 0")
    conn.execute("UPDATE jobs SET title = 'Quokka Statistician' WHERE external_id = '7'")
    conn.commit()
    conn.close()
    out = tmp_path / "site"
    gen(b, out)
    conn = db.connect(b.path)
    uid_of = {i: u for u, i in conn.execute("SELECT uid, id FROM site_ids")}
    titles = dict(conn.execute("SELECT uid, title FROM jobs"))
    conn.close()
    got = node(_store_script() + """
const getShard = async (name) => { try { return JSON.parse(readFileSync(`${dir}/data/${latest.words}/${name}.json`)); } catch { return null; } };
const res = [];
for (const q of input.queries) { const ids = await logic.searchIds(q, getShard); res.push([...ids].sort((a, b) => a - b)); }
console.log(JSON.stringify(res));
""", {"dir": str(out), "queries": ["quok", "quokka wrang", "QUÓKKA statist", "nothing-like-this"]})
    found = [sorted(titles[uid_of[i]] for i in ids) for ids in got]
    assert found[0] == sorted(t for t in titles.values() if t.startswith("Quokka"))
    assert found[1] == sorted(t for t in titles.values() if t.startswith("Quokka Wrangler"))
    assert found[2] == ["Quokka Statistician"] and found[3] == []


# ------------------------------------------------------------------ the page in a browser

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(http.server.SimpleHTTPRequestHandler):
    slow = None              # a path prefix whose files take 1.5 s, as on a slow connection

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.slow and self.path.startswith(self.slow):
            time.sleep(1.5)
        super().do_GET()


@pytest.fixture
def site_server(tmp_path):
    """A builder, its site in tmp_path/site, a local server for that folder, and the server's handler
    class (set .slow to delay some files). Regenerating the site replaces the folder, as a Pages
    deploy replaces the site."""
    b = varied_builder(tmp_path, n=60)
    out = tmp_path / "site"
    gen(b, out, now=datetime.now(timezone.utc))
    handler = type("Handler", (_Handler,), {})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), functools.partial(handler, directory=str(out)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/", b, out, handler
    srv.shutdown()


@pytest.fixture
def served_site(site_server):
    return site_server[0]


def _browser_page(p, sync):
    try:
        browser = p.chromium.launch()
    except Exception as e:  # no browser installed here
        pytest.skip(f"no Chromium: {e}")
    # bypass_csp: Playwright's own polling would be blocked by the page's strict policy (as intended)
    ctx = browser.new_context(viewport={"width": 1200, "height": 900}, bypass_csp=True)
    return browser, ctx.new_page()


def test_page_has_a_strict_policy():
    html = (Path(__file__).resolve().parent.parent / "site" / "index.html").read_text(encoding="utf-8")
    assert "default-src 'self'" in html and "connect-src 'self'" in html and 'name="robots" content="noindex"' in html
    js = (Path(__file__).resolve().parent.parent / "site" / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js     # posting text is only ever text


def test_page_works_in_a_browser(served_site):
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        browser, page = _browser_page(p, sync)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(served_site)
        page.wait_for_selector("#results li.job")
        assert "roles" in page.inner_text("#summary")
        shown = lambda: int(page.inner_text("#summary").split(" roles")[0].replace(",", ""))  # noqa: E731
        total = shown()
        page.fill("#f-q", "software")
        page.wait_for_timeout(600)
        found = shown()
        assert 0 < found < total
        page.fill("#f-q", "")
        page.wait_for_timeout(600)
        assert shown() == total
        # a pill: its panel shows a count per choice, and the pill says what's picked
        page.click("details[data-facet=experience] summary")
        page.wait_for_selector("details[data-facet=experience] .pop", state="visible")
        senior = page.locator("#opts-levels .opt", has_text="Senior").first
        page.wait_for_function("document.querySelector('#opts-levels .opt .n').textContent !== ''")
        n_senior = int(senior.locator(".n").inner_text().replace(",", ""))
        senior.locator("input").check()
        page.wait_for_timeout(200)
        assert shown() == n_senior and "Senior" in page.inner_text("details[data-facet=experience] summary")
        page.keyboard.press("Escape")
        assert not page.locator("details[data-facet=experience]").get_attribute("open")
        page.click("#reset")
        page.wait_for_timeout(200)
        assert shown() == total
        # a place: typed, picked from the suggestions with the keyboard, shown as a chip
        page.fill("#f-where", "texas")
        page.wait_for_selector("#where-list .sg")
        assert page.locator("#where-list .sg-name").first.inner_text() == "Texas"
        n_texas = int(page.locator("#where-list .sg .n").first.inner_text().replace(",", ""))
        page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        assert page.locator("#where-chips .chip").count() == 1 and shown() == n_texas
        page.locator("#where-chips .chip button").click()
        page.wait_for_timeout(200)
        assert shown() == total
        # "Can you apply?": an answer hides roles and says how many
        page.click("#open-about")
        page.select_option("select[data-answer=sponsorship]", "yes")
        page.wait_for_timeout(200)
        page.click("#more-done")
        assert "hidden" in page.inner_text("#elig-note") and shown() < total
        page.locator("#results li.job .title").first.click()
        page.wait_for_selector("#job .apply a")
        assert page.locator("#job .apply a").first.get_attribute("href").startswith("https://x/")
        page.locator("#job .close button").click()
        page.locator("#results li.job .mark").first.click()
        page.wait_for_timeout(300)
        assert page.inner_text("#tab-saved .count") == "1"
        page.reload()
        page.wait_for_selector("#results li.job")
        assert page.inner_text("#tab-saved .count") == "1"          # kept in the browser
        assert page.locator("select[data-answer=sponsorship]").input_value() == "yes"   # answers kept too
        page.set_viewport_size({"width": 380, "height": 800})
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth") <= 380
        page.click("details[data-facet=companies] summary")                # its panel is a sheet on a phone
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth") <= 380
        assert not errors, errors
        browser.close()


def test_page_keeps_working_when_the_site_is_redeployed_under_it(site_server):
    """A deploy removes the previous build's search and job files. A page left open must catch up
    instead of showing no matches or a job without apply links."""
    sync = pytest.importorskip("playwright.sync_api")
    url, b, out, _ = site_server
    start = datetime.now(timezone.utc)
    count = lambda page: int(page.inner_text("#summary").split(" roles")[0].replace(",", ""))
    with sync.sync_playwright() as p:
        browser, page = _browser_page(p, sync)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#results li.job")
        total = count(page)

        gen(b, out, now=start + timedelta(hours=1))                 # a build within the week
        page.fill("#f-q", "software")
        page.wait_for_function("document.querySelector('#notice') && !document.querySelector('#notice').hidden")
        page.wait_for_timeout(300)
        assert 0 < count(page) < total and "Updated just now" in page.inner_text("#notice")

        gen(b, out, now=start + timedelta(hours=2))
        page.locator("#results li.job .title").first.click()
        page.wait_for_selector("#job .apply a")                    # same ids: the same job, with its links
        page.locator("#job .close button").click()

        page.reload()                                              # job files already read stay usable,
        page.wait_for_selector("#results li.job")                  # so start without any
        assert gen(b, out, now=start + timedelta(days=8))["new_base"] == "weekly"   # jobs renumbered
        page.locator("#results li.job .title").first.click()
        page.wait_for_selector("#job p:has-text('just updated')")
        page.locator("#job .close button").click()
        page.locator("#results li.job .title").first.click()
        page.wait_for_selector("#job .apply a")
        page.locator("#job .close button").click()

        page.reload()
        page.wait_for_selector("#results li.job")
        gen(b, out, now=start + timedelta(days=8, hours=1))
        page.locator("#results li.job .mark").first.click()
        page.wait_for_function("document.querySelector('#tab-saved .count').textContent === '1'")
        assert not errors, errors
        browser.close()


@needs_node
def test_first_page_preview_is_the_pages_own_first_page(tmp_path):
    """top-<build>.json holds exactly the first page the page computes from the whole list (default
    view: newest first, likely ghosts left out), shown the same way, after a new base and a delta."""
    b = varied_builder(tmp_path, n=140)
    out = tmp_path / "site"
    script = _store_script() + """
const top = JSON.parse(readFileSync(`${dir}/data/${latest.top}`));
const ctx = { now: logic.nowHours(), profile: null, markOf: () => undefined, since: undefined };
const ids = logic.sortIds(store, logic.filterIds(store, logic.freshFilters(), ctx, null), "newest").slice(0, 50);
const mini = logic.buildStore(top, null, latest.vocab);
const shown = (s, i) => JSON.stringify([s.cols.t[i], s.dicts.company[s.cols.c[i]], logic.placeLabels(s, i),
  logic.payText(s, i), s.dicts.function[s.cols.f[i]], s.cols.s[i], s.cols.e[i], s.cols.w[i], s.cols.h[i], s.cols.gh[i]]);
console.log(JSON.stringify({ ids, top: top.ids, same: top.ids.every((id, j) => shown(store, id) === shown(mini, j)) }));
"""
    gen(b, out)
    r = node(script, {"dir": str(out)})
    assert len(r["ids"]) == 50 and r["top"] == r["ids"] and r["same"]

    b.ingest("beta", [job(900 + k, title=f"Fresh role {k}", board="beta", company="Beta") for k in range(5)])
    vary(b)
    conn = db.connect(b.path)
    conn.execute("UPDATE jobs SET effective_first_seen = ? WHERE source_board = 'beta'", ((NOW + timedelta(hours=1)).isoformat(),))
    conn.commit()
    conn.close()
    gen(b, out, now=NOW + timedelta(hours=2))
    assert read(out, "latest.json")["delta"]                     # new roles arrive in the change file
    r = node(script, {"dir": str(out)})
    assert r["top"] == r["ids"] and r["same"] and set(r["ids"]) & set(range(140, 145))


def test_first_visit_shows_the_newest_roles_while_the_list_downloads(site_server):
    sync = pytest.importorskip("playwright.sync_api")
    url, _, _, handler = site_server
    handler.slow = "/data/jobs-"                                  # the job list takes a while to arrive
    titles = "[...document.querySelectorAll('#results li.job .title')].slice(0, 50).map((t) => t.textContent)"
    loaded = "!document.querySelector('#summary').innerText.includes('loading all')"
    with sync.sync_playwright() as p:
        browser, page = _browser_page(p, sync)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#results li.job")
        assert "loading all" in page.inner_text("#summary")      # shown before the list is in
        preview = page.evaluate(titles)
        page.wait_for_function(loaded)
        assert page.evaluate(titles) == preview and " roles" in page.inner_text("#summary")

        page = browser.new_context(viewport={"width": 1200, "height": 900}, bypass_csp=True).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url)
        page.wait_for_selector("#results li.job")
        page.evaluate("window.stillHere = true")
        page.fill("#f-q", "software")                             # typed while the list downloads
        page.press("#f-q", "Enter")
        page.wait_for_function(loaded)
        page.wait_for_function("location.hash.includes('q=software')")
        assert page.evaluate("window.stillHere === true")        # Enter didn't reload the page
        assert page.input_value("#f-q") == "software"
        assert not errors, errors
        browser.close()


def test_sources_lists_every_company_with_a_plain_reason(site_server):
    """The list used to stop after 400 companies (around "M") and showed status codes."""
    sync = pytest.importorskip("playwright.sync_api")
    url, b, out, _ = site_server
    conn = db.connect(b.path)
    kinds = [("ok", None, "workday"), ("unsupported", "uses successfactors", None), ("blocked", "careers page didn't open: https://x → robots", None),
             ("not_found", "no careers system recognised on the careers page", None), ("error", "careers page didn't open: https://x → 403", None)]
    for k in range(450):
        status, detail, ats = kinds[k % 5]
        name = f"{chr(65 + k * 26 // 450)}-company {k:03d}"
        conn.execute("INSERT INTO companies(company, ats, slug, status, detail, openings, checked_at) VALUES (?,?,?,?,?,?,?)",
                     (name, ats, "x.wd1/y" if ats else None, status, detail, 7 if ats else None, "2026-11-10"))
    db.set_meta(conn, "build_health", {
        "postings": {"open": 5000, "new": 1234, "closed": 56}, "boards": {"read": 700, "unchanged": 100, "failed": 3, "waiting": 0},
        "with_description_pct": 81.5, "ghost_pct": 4.2, "systems": {"workday": {"postings": 4000, "with_description_pct": 77.0}},
        "failing": [{"board": "greenhouse:x", "error": "https://secret"}]})
    conn.commit()
    conn.close()
    gen(b, out, now=datetime.now(timezone.utc) + timedelta(hours=1))
    src = read(out, read(out, "latest.json")["sources"])
    assert len(src["companies"]) == 450 and "detail" not in src["companies"][1]
    assert "https://" not in json.dumps(src["companies"])              # reasons, never addresses or errors
    with sync.sync_playwright() as p:
        browser, page = _browser_page(p, sync)
        page.goto(url)
        page.wait_for_selector("#results li.job")
        page.click("#open-sources")
        page.wait_for_selector("#sources tbody tr")
        rows = page.locator("#sources tbody tr")
        assert rows.count() == 450 and rows.last.inner_text().startswith("Z-company 449")
        text = page.inner_text("#sources")
        assert "90 of 450 companies" in text and "Covered · Workday" in text
        assert "Last update: 1,234 new postings, 56 closed. Descriptions read for 81.5% of postings." in text
        page.click("#sources details.health summary")
        assert "800 job boards answered, 3 didn't." in page.inner_text("#sources details.health")
        assert page.inner_text("#sources details.health li") == "workday: 4,000 open postings, descriptions read for 77%"
        assert "secret" not in json.dumps(src["health"])                   # board errors stay out of it
        assert "Uses SAP SuccessFactors, which Lighthouse can't read yet" in text
        for code in ("unsupported", "not_found", "blocked"):
            assert code not in text
        page.select_option("#sources select", "no")
        assert rows.count() == 360 and "360 shown" in page.inner_text("#sources")
        page.fill("#sources input[type=search]", "z-company")
        assert 0 < rows.count() < 20
        browser.close()
