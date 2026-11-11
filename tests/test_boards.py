from datetime import datetime, timezone

from lighthouse import config, db, fetch
from lighthouse.sources import detect, detect_in_html
from lighthouse.sources.base import BoardNotFound
from lighthouse.sources.workday import WorkdaySource, posted_from_text

from conftest import FakeClient, FakeResp

LIST = {"total": 3, "jobPostings": [
    {"title": "SoC Product Architect, Memory Ecosystem", "externalPath": "/job/US-CA-Santa-Clara/SoC-Product-Architect_JR2019756",
     "locationsText": "US, CA, Santa Clara", "postedOn": "Posted Today", "bulletFields": ["JR2019756"]},
    {"title": "Data Analyst", "externalPath": "/job/US-TX-Austin/Data-Analyst_JR1", "locationsText": "2 Locations",
     "postedOn": "Posted 5 Days Ago"},
    {"title": "Warehouse Associate", "externalPath": "/job/US-TX-Austin/Warehouse-Associate_JR2", "locationsText": "Austin",
     "postedOn": "Posted 30+ Days Ago"}]}
DETAIL = {"jobPostingInfo": {"title": "Data Analyst", "jobDescription": "<p>Analyze <b>things</b>.</p><script>x</script>",
                             "location": "US, TX, Austin", "additionalLocations": ["US, NY, New York"],
                             "startDate": "2026-10-20", "timeType": "Full time", "remoteType": "Hybrid",
                             "externalUrl": "https://acme.wd5.myworkdayjobs.com/Careers/job/US-TX-Austin/Data-Analyst_JR1"},
          "hiringOrganization": {"name": "2100 Acme USA"}}


def wd_client():
    def post(body):
        return FakeResp(LIST if body["offset"] == 0 else {"total": 0, "jobPostings": []})
    c = FakeClient({"/job/": DETAIL, "/jobs": post})
    return c


def test_workday_detect_and_slug():
    assert detect("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-CA/Title_JR1") == \
        ("workday", "nvidia.wd5/NVIDIAExternalCareerSite")
    assert detect("workday:acme.wd1/External") == ("workday", "acme.wd1/External")
    html = '<a href="https://acme.wd12.myworkdayjobs.com/Careers">Jobs</a>'
    assert detect_in_html(html) == ("workday", "acme.wd12/Careers")


def test_workday_posted_text():
    now = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    assert posted_from_text("Posted Today", now).date().isoformat() == "2026-11-03"
    assert posted_from_text("Posted Yesterday", now).date().isoformat() == "2026-11-02"
    assert posted_from_text("Posted 5 Days Ago", now).date().isoformat() == "2026-10-29"
    assert posted_from_text("Posted 30+ Days Ago", now) is None


def test_workday_fetch_lists_pages_and_details_once():
    c = wd_client()
    src = WorkdaySource(client=c, company="Acme", max_detail_calls=10)
    jobs = src.fetch("acme.wd5/Careers")
    assert len(jobs) == 3 and all(j.company == "Acme" for j in jobs)
    j = next(j for j in jobs if j.title == "Data Analyst")
    assert j.uid == "workday:acme.wd5/Careers:Data-Analyst_JR1"
    assert "<b>things</b>" in j.content_html and "script" not in j.content_html
    assert j.locations == ["US, TX, Austin", "US, NY, New York"] and j.workplace == "hybrid"
    assert j.posted_at.date().isoformat() == "2026-10-20" and not j.partial
    post = [x for x in c.calls if isinstance(x, tuple)][0]
    assert post[1] == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs" and post[2]["limit"] == 20
    # Next run: postings already have content -> no detail calls, and stored details survive.
    c.calls.clear()
    known = {x.uid: (x.detail_marker, True) for x in jobs}
    again = src.fetch("acme.wd5/Careers", known=known)
    assert not any(isinstance(x, str) and "/job/" in x for x in c.calls)
    assert all(x.partial for x in again)


def test_workday_partial_listing_keeps_detail_fields(tmp_path):
    conn = db.connect(tmp_path / "w.db")
    src = WorkdaySource(client=wd_client(), company="Acme")
    first = src.fetch("acme.wd5/Careers")
    db.upsert_board(conn, "workday", "acme.wd5/Careers", first)
    known = db.known_postings(conn, "workday", "acme.wd5/Careers")
    second = WorkdaySource(client=wd_client(), company="Acme").fetch("acme.wd5/Careers", known=known)
    db.upsert_board(conn, "workday", "acme.wd5/Careers", second)
    r = db.get_job(conn, "workday:acme.wd5/Careers:Data-Analyst_JR1")
    assert r["posted_at"].startswith("2026-10-20") and r["location"] == "US, TX, Austin"
    assert "things" in r["content_html"]


def test_workday_not_found():
    c = FakeClient({"/jobs": lambda body: FakeResp({}, 404)})
    try:
        WorkdaySource(client=c).fetch("nope.wd1/Careers")
        assert False
    except BoardNotFound:
        pass


def test_board_lists_load_and_respect_ui_toggles(tmp_path):
    (tmp_path / "boards").mkdir()
    (tmp_path / "boards" / "mine.csv").write_text(
        "# comment\nats,slug,company,enabled\nlever,palantir,Palantir,yes\nworkday,acme.wd5/Careers,Acme,no\nbogus,x,X,yes\n")
    cfgp = tmp_path / "sources.toml"
    cfgp.write_text("")
    cfg = config.load(cfgp)
    assert [(b["ats"], b["slug"], b["enabled"]) for b in cfg.boards] == [("lever", "palantir", True), ("workday", "acme.wd5/Careers", False)]
    conn = db.connect(tmp_path / "b.db")
    db.sync_config_boards(conn, cfg.boards)
    db.set_board_enabled(conn, "workday", "acme.wd5/Careers", True)   # user turns it on
    db.sync_config_boards(conn, cfg.boards)                           # list says no: user wins
    on = {b["slug"] for b in db.list_boards(conn, enabled_only=True)}
    assert on == {"palantir", "acme.wd5/Careers"}
    assert db.board_counts(conn)["total"] == 2


def test_dead_board_switched_off_after_two_not_found(tmp_path, monkeypatch):
    cfgp = tmp_path / "s.toml"
    cfgp.write_text('[settings]\nskip_hourly_titles = true\n[[boards]]\nats="greenhouse"\nslug="gone"\n'
                    '[[boards]]\nats="greenhouse"\nslug="flaky"\n[[boards]]\nats="greenhouse"\nslug="acme"\n')
    from lighthouse.models import Job
    from lighthouse.sources.greenhouse import GreenhouseSource as G

    def fake(self, slug, known=None):
        if slug == "gone":
            raise BoardNotFound("Greenhouse: board not found")
        if slug == "flaky":
            raise TimeoutError("read timed out")
        return [Job("greenhouse", slug, "1", "Data Analyst", "Acme", "Austin", "u", "<p>x</p>"),
                Job("greenhouse", slug, "2", "Warehouse Associate", "Acme", "Austin", "u", "<p>y</p>")]
    monkeypatch.setattr(G, "fetch", fake)
    fetch.run(cfgp, tmp_path / "f.db")
    res = {r["board"]: r for r in fetch.run(cfgp, tmp_path / "f.db")}
    assert res["gone"]["switched_off"] and not res["flaky"]["switched_off"]
    assert res["acme"]["filtered_out"] == 1
    conn = db.connect(tmp_path / "f.db")
    boards = {b["slug"]: b for b in db.list_boards(conn)}
    assert boards["gone"]["enabled"] == 0 and boards["gone"]["last_error"].startswith("Switched off automatically")
    assert boards["flaky"]["enabled"] == 1           # timeouts never switch a board off
    assert db.get_job(conn, "greenhouse:acme:2") is None
    # third run doesn't even try the switched-off board
    assert "gone" not in {r["board"] for r in fetch.run(cfgp, tmp_path / "f.db")}


def test_descriptions_are_compressed(tmp_path):
    from lighthouse.models import Job
    conn = db.connect(tmp_path / "c.db")
    body = "<p>" + "Build reliable systems. " * 200 + "</p>"
    db.upsert_board(conn, "greenhouse", "acme", [Job("greenhouse", "acme", "1", "Eng", "Acme", "Austin", "u", body)])
    raw = conn.execute("SELECT html FROM job_content").fetchone()[0]
    assert isinstance(raw, bytes) and len(raw) < len(body) / 4
    assert db.get_job(conn, "greenhouse:acme:1")["content_html"] == body


def test_bundled_top_companies_list():
    cfg = config.load()
    listed = [b for b in cfg.boards if b.get("list") == "top-companies"]
    on = [b for b in listed if b["enabled"]]
    assert len(listed) > 800 and 450 <= len(on) <= 550
    assert len({(b["ats"], b["slug"].lower()) for b in listed}) == len(listed)  # no duplicates
    assert {"workday", "greenhouse", "ashby", "lever", "smartrecruiters"} <= {b["ats"] for b in on}


def test_hourly_title_filter_is_narrow():
    import re
    h = re.compile("|".join(config.HOURLY_TITLE_PATTERNS), re.I)
    skip = ["Warehouse Associate", "Delivery Driver", "CDL Truck Driver", "Production Operator - 2nd Shift", "Cashier",
            "Retail Sales Associate", "Store Manager", "Line Cook", "Server", "Banquet Server", "Material Handler"]
    keep = ["SQL Server DBA", "Data Warehouse Engineer", "Program Manager, Warehouse Systems", "Linux Kernel Driver Engineer",
            "Chief Information Security Officer", "Server Engineer", "Software Engineer, Delivery Platform", "Sales Associate",
            "Executive Chef", "Records Custodian Analyst", "Production Engineer", "Assembly Line Process Engineer"]
    assert [t for t in skip if not h.search(t)] == []
    assert [t for t in keep if h.search(t)] == []


# ---------------------------------------------------------------- Workday: big sites and the country filter
US, CA, IN = "bc33aa31", "a30a87ed", "c4f78be1"
FACETS = [{"facetParameter": "jobFamilyGroup", "descriptor": "Job Category",
           "values": [{"descriptor": "Engineering", "id": "jf1", "count": 9}, {"descriptor": "Canada Sales", "id": "jf2", "count": 3}]},
          {"facetParameter": "locationMainGroup", "values": [
              {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                  {"descriptor": "United States of America", "id": US, "count": 45},
                  {"descriptor": "India", "id": IN, "count": 60}, {"descriptor": "Canada", "id": CA, "count": 5}]},
              {"facetParameter": "locations", "descriptor": "Locations",
               "values": [{"descriptor": "Austin", "id": "l1", "count": 5}, {"descriptor": "Toronto", "id": "l2", "count": 5}]}]}]


def workday_site(postings, filters, cap=2000):
    """A Workday site like the real ones: a listing serves at most `cap` results (past that, its first page
    again), reports min(matching, cap) as the total, and counts the matching postings per filter value.
    postings: dicts of filter -> value id (None: not set)."""
    from collections import Counter
    names = {US: "United States of America", CA: "Canada", IN: "India"}
    def post(body):
        applied, off = body["appliedFacets"], body["offset"]
        match = [(k, p) for k, p in enumerate(postings) if all(p.get(f) in ids for f, ids in applied.items())]
        off = 0 if off >= cap else off
        batch = [{"title": f"Analyst {k}", "externalPath": f"/job/X/Analyst_{k}",
                  "locationsText": "Austin, TX" if p.get("locationCountry") in (US, None) else "Elsewhere"}
                 for k, p in match[off:off + 20]]
        facets = [{"facetParameter": f, "values": [{"descriptor": names.get(i, i), "id": i, "count": n}
                                                   for i, n in Counter(p[f] for _, p in match if p.get(f)).items()]}
                  for f in filters]
        return FakeResp({"total": min(len(match), cap), "jobPostings": batch, "facets": facets})
    return FakeClient({"/job/": DETAIL, "/jobs": post})


def wd_fetch(client, countries=None):
    src = WorkdaySource(client=client, company="Acme", max_detail_calls=0)
    if countries:
        src.countries = frozenset(countries)
    jobs = src.fetch("acme.wd5/Careers")
    return jobs, [x[2] for x in client.calls if isinstance(x, tuple)]


def test_workday_reads_a_listing_bigger_than_the_2000_cap_by_splitting_it():
    from lighthouse.sources.workday import split_facet
    # 5,000 postings. 400 have no job category; every posting has a state (four of them, 1,250 each).
    site = [{"cat": None if k % 25 < 2 else f"c{k % 3}", "state": f"s{k % 4}", "time": "full" if k % 5 < 3 else "part"}
            for k in range(5000)]
    c = workday_site(site, ["cat", "state", "time"])
    jobs, posts = wd_fetch(c)
    assert len(jobs) == 5000 and len({j.external_id for j in jobs}) == 5000       # all of it, each once
    assert {tuple(p["appliedFacets"]) for p in posts[1:]} == {("state",)}         # the filter that covers it, under the cap
    assert len(posts) == 1 + 4 * 63                                               # no page past a listing's end
    # Paging alone would have stopped at 2,000:
    assert len(wd_fetch(workday_site(site, []))[0]) == 2000

    # A part that is still too big is split again by another filter.
    site = [{"cat": "c0" if k % 5 < 3 else f"c{k % 5}", "time": "full" if k % 2 else "part"} for k in range(5000)]
    c = workday_site(site, ["cat", "time"])
    jobs, posts = wd_fetch(c)
    assert len(jobs) == 5000
    assert {tuple(p["appliedFacets"]) for p in posts[1:]} == {("time",), ("time", "cat")}
    assert len(posts) == 1 + 2 + 2 * (75 + 25 + 25)

    # Which filter: one whose values add up to the listing, and are each under the cap if possible.
    f = lambda **kw: [{"facetParameter": k, "values": [{"id": f"{k}{i}", "count": n} for i, n in enumerate(v)]}  # noqa: E731
                      for k, v in kw.items()]
    assert split_facet(f(cat=[900, 800], state=[1500, 1500, 1500]), {})[0] == "state"      # cat misses most postings
    assert split_facet(f(time=[3000, 1500], state=[1500, 1500, 1500]), {})[0] == "state"   # no part over the cap
    assert split_facet(f(time=[3000, 1500], state=[1500, 1500, 1500]), {"state": ["state0"]})[0] == "time"
    assert split_facet(f(time=[1000, 500], state=[900, 800]), {}, expected=1500)[0] == "time"
    assert split_facet(f(city=[40] * 55, only=[4500]), {}) is None       # a cut-short list, a single value: no split
    assert [v["id"] for v in split_facet(f(time=[3000, 0, 1500]), {})[1]] == ["time0", "time2"]

    c = workday_site([{} for _ in range(45)], [])                        # a small site: three pages, nothing more
    jobs, posts = wd_fetch(c)
    assert len(jobs) == 45 and len(posts) == 3


def test_workday_asks_only_for_the_countries_the_builder_covers():
    from lighthouse.sources.workday import country_facet
    assert country_facet(FACETS, {"US", "CA", "MX"}) == {"locationCountry": [US, CA]}
    assert country_facet(FACETS, {"MX"}) == {}                 # a country filter, and Mexico isn't in it
    assert country_facet(FACETS[:1], {"US"}) is None           # "Canada Sales" is a job category, not a country
    many = [{"facetParameter": "Location_Country", "values": [{"descriptor": "United States of America", "id": US}] +
             [{"descriptor": f"Country {k}", "id": f"c{k}"} for k in range(54)]}]
    assert country_facet(many, {"US", "MX"}) is None           # a long list may be cut short: Mexico unknown
    assert country_facet(many, {"US"}) == {"Location_Country": [US]}

    # 9,000 postings, 3,000 of them in the US and Canada: only those are asked for, split by state.
    site = [{"locationCountry": (US, IN, CA, IN, IN, IN, IN, IN, US)[k % 9], "state": f"s{k % 4}"} for k in range(9000)]
    c = workday_site(site, ["locationCountry", "state"])
    jobs, posts = wd_fetch(c, {"US", "CA", "MX"})
    assert len(jobs) == 3000 and all(int(j.external_id.split("_")[1]) % 9 in (0, 2, 8) for j in jobs)
    assert all(set(p["appliedFacets"]["locationCountry"]) == {US, CA} for p in posts[1:])
    assert len(posts) == 1 + 1 + 4 * 38                        # first page, the narrowed one, four states of 750
    c = workday_site(site[:900], ["locationCountry", "state"])            # 300 in scope: one short listing
    jobs, posts = wd_fetch(c, {"US", "CA", "MX"})
    assert len(jobs) == 300 and len(posts) == 1 + 15
    assert len(wd_fetch(workday_site(site, ["locationCountry", "state"]))[0]) == 9000    # no country scope: everything
