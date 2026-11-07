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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_index import Builder, job

from lighthouse import classify, db, profile, site

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
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


FUNCTIONS = ["Software Engineering", "Data & Analytics", "Finance & Accounting", ""]
PLACES = ["Austin, TX", "New York, NY", "Remote - US", "Toronto, ON", "Monterrey, NL", "London, UK", "Denver, CO"]


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
        places = rng.sample(PLACES, rng.choice([1, 1, 2, 3]))
        codes = sorted({"Austin": "US", "New York": "US", "Remote": "US", "Toronto": "CA", "Monterrey": "MX",
                        "London": "GB", "Denver": "US"}[p.split(",")[0].split(" -")[0]] for p in places)
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
    prof = {**profile.DEFAULT, "years_experience": 3, "needs_sponsorship": True, "locations": ["Austin", "Remote"],
            "min_salary": 100000, "degree": "bachelors"}
    cases = [  # (local app Filters, the page's filters)
        ({}, {}),
        ({"function": "Software Engineering"}, {"fn": "Software Engineering"}),
        ({"seniority": "senior", "emp_type": "full_time"}, {"sen": "senior", "emp": "full_time"}),
        ({"workplace": "remote", "min_salary": 100000}, {"workplace": "remote", "minSalary": 100000}),
        ({"yoe_lo": 2, "yoe_hi": 5}, {"yoeLo": 2, "yoeHi": 5}),
        ({"yoe_hi": 3, "yoe_unknown": False}, {"yoeHi": 3, "yoeUnknown": False}),
        ({"visa": "not_no", "pay_listed": True}, {"visa": "not_no", "payListed": True}),
        ({"visa": "yes"}, {"visa": "yes"}),
        ({"degree_max": "none"}, {"degreeMax": "none"}),
        ({"degree_max": "bachelors", "clearance": "none"}, {"degreeMax": "bachelors", "clearance": "none"}),
        ({"clearance": "required", "no_citizenship": True}, {"clearance": "required", "noCitizenship": True}),
        ({"benefits": ("equity", "health")}, {"benefits": ["equity", "health"]}),
        ({"travel": "low", "language": "english"}, {"travelLow": True, "language": "english"}),
        ({"language": "spanish"}, {"language": "spanish"}),
        ({"skills": ("python",), "posted_within": 7}, {"skills": ["python"], "postedWithin": 7}),
        ({"countries": ("CA", "MX")}, {"countries": ["CA", "MX"]}),
        ({"location": "new york"}, {"location": "new york"}),
        ({"show_ghosts": True}, {"showGhosts": True}),
        ({"eligible_only": True, "profile": prof}, {"eligibleOnly": True}),
    ]
    expected = []
    for py, _ in cases:
        rows, _ = db.list_jobs(conn, db.Filters(**{"eligible_only": False, **py}), per_page=1000, with_count=False)
        expected.append(sorted(r["uid"] for r in rows))
    conn.close()
    got = node(_store_script() + """
const out = input.cases.map((f) => logic.filterIds(store, {...logic.DEFAULT_FILTERS, eligibleOnly: false, ...f},
  {now: logic.nowHours(), profile: input.profile}));
console.log(JSON.stringify(out));
""", {"dir": str(out), "cases": [js for _, js in cases], "profile": prof})
    for (py, _), want, ids in zip(cases, expected, got):
        assert sorted(uid_of[i] for i in ids) == want, py


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
                 "locations": rng.sample(["austin", "Toronto", "remote", "denver", "mexico"], rng.randint(0, 2)),
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


@pytest.fixture
def served_site(tmp_path):
    b = varied_builder(tmp_path, n=60)
    out = tmp_path / "site"
    gen(b, out, now=datetime.now(timezone.utc))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


def test_page_has_a_strict_policy():
    html = (Path(__file__).resolve().parent.parent / "site" / "index.html").read_text()
    assert "default-src 'self'" in html and "connect-src 'self'" in html and 'name="robots" content="noindex"' in html
    js = (Path(__file__).resolve().parent.parent / "site" / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js     # posting text is only ever text


def test_page_works_in_a_browser(served_site):
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser installed here
            pytest.skip(f"no Chromium: {e}")
        # bypass_csp: Playwright's own polling would be blocked by the page's strict policy (as intended)
        ctx = browser.new_context(viewport={"width": 1200, "height": 900}, bypass_csp=True)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(served_site)
        page.wait_for_selector("#results li.job")
        assert "roles" in page.inner_text("#summary")
        total = int(page.inner_text("#summary").split(" roles")[0].replace(",", ""))
        page.fill("#f-q", "software")
        page.wait_for_timeout(600)
        found = int(page.inner_text("#summary").split(" roles")[0].replace(",", ""))
        assert 0 < found < total
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
        page.set_viewport_size({"width": 380, "height": 800})
        page.wait_for_timeout(200)
        assert page.evaluate("document.documentElement.scrollWidth") <= 380
        assert not errors, errors
        browser.close()
