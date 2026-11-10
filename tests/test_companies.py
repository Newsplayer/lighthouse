"""Big-company coverage: careers-page discovery, richer filters, query-time eligibility, reader files."""
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from lighthouse import classify, config, db, discover, ghost, profile, snapshot
from lighthouse.models import Job
from lighthouse.sanitize import sanitize_html


class R:
    def __init__(self, url, text="", status=200, ctype="text/html"):
        self.url, self.text, self.status_code = url, text, status
        self.content, self.headers = text.encode(), {"content-type": ctype}


class Site:
    """A fake web: url -> response; robots.txt per host."""

    def __init__(self, pages, robots=None):
        self.pages, self.robots, self.calls = pages, robots or {}, []

    def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            host = url.split("/")[2]
            return R(url, self.robots.get(host, ""), 200 if host in self.robots else 404, "text/plain")
        hit = self.pages.get(url)
        if isinstance(hit, R):
            return hit
        return R(url, hit, 200) if hit is not None else R(url, "", 404)


def test_discovery_follows_search_page_to_the_apply_link():
    site = Site({
        "https://careers.acme.com": R("https://careers.acme.com/us/en",
                                      '<a href="/us/en/search-results">Search all jobs</a><script src="https://cdn.phenompeople.com/a.js"></script>'),
        "https://careers.acme.com/us/en/search-results": '<a href="/us/en/job/R1/Analyst">Analyst</a><a href="/about">About</a>',
        "https://careers.acme.com/us/en/job/R1/Analyst": '<a href="https://acme.wd5.myworkdayjobs.com/en-US/Ext/job/X_R1">Apply</a>',
    })
    r = discover.Discoverer(site, "test").resolve({"company": "Acme", "domain": "acme.com",
                                                   "careers_url": "https://careers.acme.com"})
    assert (r.status, r.ats, r.slug) == ("ok", "workday", "acme.wd5/Ext")
    assert "phenom" in r.platforms and "https://careers.acme.com/about" not in site.calls


def test_discovery_reports_unsupported_systems_and_respects_robots():
    site = Site({"https://careers.big.com": '<a href="https://big.taleo.net/careersection/2/jobsearch.ftl">Jobs</a>'})
    r = discover.Discoverer(site, "test").resolve({"company": "Big", "domain": "big.com",
                                                   "careers_url": "https://careers.big.com"})
    assert r.status == "unsupported" and r.detail == "uses taleo"
    blocked = Site({"https://jobs.shy.com/": "<a href='https://jobs.lever.co/shy'>x</a>"},
                   robots={"jobs.shy.com": "User-agent: *\nDisallow: /"})
    r = discover.Discoverer(blocked, "test").resolve({"company": "Shy", "domain": "", "careers_url": "https://jobs.shy.com/"})
    assert r.status == "blocked" and "https://jobs.shy.com/" not in blocked.calls
    pinned = discover.Discoverer(Site({}), "t").resolve({"company": "P", "ats": "lever", "slug": "p", "careers_url": ""})
    assert pinned.status == "pinned"


def test_discovered_boards_join_the_build_and_are_rechecked_only_when_due(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "companies.csv").write_text(
        "# test\ncompany,group,domain,careers_url,ats,slug,enabled\n"
        "Acme,fortune500,acme.com,https://jobs.lever.co/acme,,,yes\n"
        "Pinned Co,tech,p.com,,greenhouse,pinned,yes\n"
        "Gone Inc,tech,gone.com,https://careers.gone.com,,,yes\n"
        "Skip Co,tech,s.com,https://careers.s.com,,,no\n", encoding="utf-8")
    calls = []
    real = discover.run
    monkeypatch.setattr(discover, "run", lambda todo, *a, **k: calls.append([c["company"] for c in todo]) or real(todo, Site({}), "t"))
    conn = db.connect(tmp_path / "b.db")
    boards = snapshot.discover_boards(conn, cfg_dir, client=None)
    assert sorted((b["ats"], b["slug"]) for b in boards) == [("greenhouse", "pinned"), ("lever", "acme")]
    assert calls == [["Acme", "Gone Inc"]]
    st = dict(conn.execute("SELECT company, status FROM companies").fetchall())
    assert st == {"Acme": "ok", "Pinned Co": "pinned", "Gone Inc": "error"}
    snapshot.discover_boards(conn, cfg_dir, client=None)   # nothing due yet
    assert calls[-1] == ["Acme", "Gone Inc"] and len(calls) == 1
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    conn.execute("UPDATE companies SET checked_at = ?", (old,))
    conn.commit()
    snapshot.discover_boards(conn, cfg_dir, client=None)   # failures are retried after 3 days, good ones after 14
    assert calls[-1] == ["Gone Inc"]
    # the companies list switches a board on even when another list has it off
    closed = snapshot.apply_board_list(conn, [{"ats": "lever", "slug": "acme", "enabled": False, "company": "Acme"}] + boards)
    assert conn.execute("SELECT enabled, list FROM boards WHERE slug='acme'").fetchone()[0] == 1 and closed == set()


def test_classifiers():
    assert classify.job_function("Senior Machine Learning Engineer") == "Machine Learning & AI"
    assert classify.job_function("Software Engineer II, Payments") == "Software Engineering"
    assert classify.job_function("Data Engineer") == "Data & Analytics"
    assert classify.job_function("Product Designer") == "Design & UX"
    assert classify.job_function("Registered Nurse - ICU") == "Healthcare & Clinical"
    assert classify.job_function("Account Executive, Mid-Market") == "Sales"
    assert classify.job_function("Senior Accountant") == "Finance & Accounting"
    assert classify.job_function("Zzz") is None
    assert classify.seniority("Software Engineering Intern") == "intern"
    assert classify.seniority("Staff Software Engineer") == "lead"
    assert classify.seniority("Account Manager") == "mid" and classify.seniority("Engineering Manager") == "manager"
    assert classify.seniority("Vice President, Finance") == "executive"
    assert classify.seniority("Data Analyst", 6, "stated") == "senior" and classify.seniority("Data Analyst") is None
    # A title that reads entry level but asks for 3+ years isn't entry level (reported: "entry-level", 4+ years)
    assert classify.seniority("Entry Level Analyst", 4, "stated") == "mid"
    assert classify.seniority("Associate", 5, "stated") == "senior"
    assert classify.seniority("Analyst I", 2, "stated") == "entry"          # 0-2 years is still entry level
    assert classify.seniority("Associate", 5, "title") == "entry"           # only stated years count
    assert classify.seniority("Summer Intern", 4, "stated") == "intern"


def test_workplace_from_the_title_only_when_it_is_a_tag():
    from lighthouse import extract
    wp = lambda title, loc="": extract.rules("We build things.", title, loc)["workplace"]["value"]
    assert wp("Senior Engineer (Remote)") == "remote" and wp("Software Engineer - Remote") == "remote"
    assert wp("Remote - Customer Success Manager") == "remote" and wp("Data Engineer | Hybrid") == "hybrid"
    assert wp("Analyst", "Remote - US") == "remote" and wp("Analyst", "Hybrid - Chicago, IL") == "hybrid"
    assert wp("Remote Sensing Scientist") is None and wp("Remote Monitoring Technician") is None
    assert wp("Hybrid Cloud Architect") is None
    assert classify.employment_type("FULL_TIME") == "full_time"
    assert classify.employment_type(None, "Summer Intern 2026") == "internship"
    assert classify.employment_type(None, "Regular Analyst") is None
    text = ("You know Python, SQL and AWS; experience with Spring Boot, React.js and Kubernetes (k8s). "
            "Go, Rust or C/C++ a plus. Excel in a fast-paced spring season.")
    found = classify.skills(text)
    for s in ("python", "sql", "aws", "spring", "react", "kubernetes", "go", "rust", "c++", "c"):
        assert s in found, s
    assert "excel" not in found          # the verb, not the spreadsheet
    assert classify.skills("Spring 2026 internship. Go to our site.") == []


def _random_row(rng):
    locs = rng.choice([None, "[]", '["Austin, TX"]', '["Remote - US"]', '["New York, NY", "Austin, TX"]', '["London"]'])
    return {"visa": rng.choice([None, "no", "yes"]), "citizenship": rng.choice([None, 0, 1]),
            "clearance": rng.choice([None, "", "secret", "ts", "ts_sci", "public_trust", "unspecified"]),
            "workplace_eff": rng.choice([None, "", "remote", "hybrid", "onsite"]), "cluster_locations": locs,
            "location": rng.choice([None, "", "Austin, TX", "Remote"]), "yoe_min": rng.choice([None, 0, 2, 5, 9]),
            "yoe_source": rng.choice([None, "stated", "title", "preferred"]),
            "pay_max_annual": rng.choice([None, 0, 50000, 150000]), "degree": rng.choice([None, "bachelors", "masters", "phd"])}


def test_query_eligibility_matches_the_reason_checker():
    """The SQL clause and profile.check() must agree on every row: one filters the list, the other
    explains it."""
    rng = random.Random(7)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = list(_random_row(rng))
    conn.execute(f"CREATE TABLE jobs (id INTEGER PRIMARY KEY, {', '.join(cols)})")
    rows = [_random_row(rng) for _ in range(400)]
    conn.executemany(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [tuple(r[c] for c in cols) for r in rows])
    for _ in range(60):
        p = {**profile.DEFAULT, "needs_sponsorship": rng.random() < .5, "us_citizen": rng.random() < .5,
             "clearance": rng.choice(["none", "secret", "ts"]), "years_experience": rng.choice([None, 1, 4]),
             "degree": rng.choice(["none", "bachelors", "phd"]), "min_salary": rng.choice([None, 100000]),
             "workplaces": rng.choice([["remote", "hybrid", "onsite"], ["remote"], ["hybrid", "onsite"]]),
             "locations": rng.choice([[], ["Austin"], ["remote"], ["new york", "remote"]]), "about": "x"}
        clause, args = profile.eligibility_sql(conn, p, 2)
        ok = {r[0] for r in conn.execute(f"SELECT id FROM jobs j WHERE {clause}", args)}
        for i, row in enumerate(conn.execute("SELECT * FROM jobs ORDER BY id"), start=1):
            assert (i in ok) == (not profile.check(row, p, 2)), (p, dict(row))


def job(eid, title="Data Analyst", body=None, emp=None, departments=None):
    body = body or f"<p>Python and SQL daily. 3+ years of experience. {' '.join(f'w{eid}x{k}' for k in range(60))}</p>"
    return Job("greenhouse", "acme", str(eid), title, "Acme", "Austin, TX", f"https://x/{eid}?utm_source=li&gh_src=1",
               sanitize_html(body), posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc), employment_type=emp,
               departments=departments or [])


def test_new_filters_end_to_end(tmp_path):
    from lighthouse import fetch
    conn = db.connect(tmp_path / "f.db")
    db.upsert_board(conn, "greenhouse", "acme", [
        job(1, "Senior Software Engineer", emp="Full-time"),
        job(2, "Data Analyst Intern", body="<p>Excel and Tableau. Pay: $25 - $30 per hour. " + "x " * 80 + "</p>"),
        job(3, "Account Executive", body="<p>Salesforce CRM. Contract role. " + "y " * 80 + "</p>", emp="Contract")])
    fetch.postprocess(conn, config.load(tmp_path / "none.toml"))
    rows = {r["title"]: r for r in db.list_jobs(conn, db.Filters())[0]}
    assert rows["Senior Software Engineer"]["job_function"] == "Software Engineering"
    assert rows["Senior Software Engineer"]["seniority"] == "senior"
    assert rows["Data Analyst Intern"]["emp_type"] == "internship" and rows["Account Executive"]["emp_type"] == "contract"
    assert rows["Data Analyst Intern"]["pay_interval"] == "hour" and rows["Data Analyst Intern"]["pay_max_annual"] == 30 * 2080
    names = lambda **kw: sorted(r["title"] for r in db.list_jobs(conn, db.Filters(**kw))[0])
    assert names(function="Sales") == ["Account Executive"]
    assert names(seniority="intern") == ["Data Analyst Intern"]
    assert names(skills=("python", "sql")) == ["Senior Software Engineer"]
    assert names(skills=("tableau",)) == ["Data Analyst Intern"]
    assert names(emp_type="contract") == ["Account Executive"]


def test_ghost_reasons_store_dates_not_counts():
    row = {"effective_first_seen": "2026-08-01T00:00:00+00:00", "first_seen": None, "repost_count": 0,
           "title": "Analyst", "updated_at": "2026-01-01T00:00:00+00:00"}
    now = datetime(2026, 11, 5, tzinfo=timezone.utc)
    stored = ghost.signals(row, 60, now, text="")
    assert stored == ["open since 2026-08-01", "not updated since 2026-01-01"]
    assert stored == ghost.signals(row, 60, now + timedelta(days=1), text="")   # unchanged the next day
    assert ghost.labels(json.dumps(stored), now) == ["open 96 days", "not updated in 308 days"]


def test_existing_reader_database_is_converted(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    old = db.connect(data / "lighthouse.db")      # a 0.5 reader: jobs and personal data in one file
    old.execute("INSERT INTO marks(uid, status, at) VALUES ('greenhouse:acme:1', 'applied', 'x')")
    old.execute("INSERT INTO claude_extractions(content_hash, data, model, at) VALUES ('h', '{}', 'm', 'x')")
    db.set_meta(old, "profile", {"about": "data jobs"})
    db.set_meta(old, "generation", 9)
    old.close()
    cfg = config.load(tmp_path / "none.toml")
    cfg.index.repo = "o/r"
    path = db.app_db(cfg, data)
    assert path.name == "user.db"
    conn = db.connect(path)
    assert conn.execute("SELECT status FROM marks").fetchone()[0] == "applied"
    assert profile.load(conn)["about"] == "data jobs" and db.get_meta(conn, "generation") is None
    assert conn.execute("SELECT COUNT(*) FROM claude_extractions").fetchone()[0] == 1
    assert (data / "lighthouse.db").exists()    # kept until a real index is installed...
    snapshot.cleanup(conn)
    assert (data / "lighthouse.db").exists()
    conn.close()


def test_an_age_is_not_years_of_experience():
    from lighthouse import extract
    yoe = lambda t: extract.rules(t)["yoe_min"]["value"]
    assert yoe("- Must be at least 18\n\n- 1 year of retail or consumer service experience") == 1
    assert yoe("Must be 18+ years of age. 2 years of experience in sanitation.") == 2
    assert yoe("Must be at least 18 years old.") is None
    assert yoe("Applicants must be 21 years of age or older with 3 years driving experience") == 3
    assert yoe("Minimum of 18 years of experience in sales") == 18
    assert yoe("5+ years of relevant experience") == 5 and yoe("at least 3 years in a similar role") == 3
