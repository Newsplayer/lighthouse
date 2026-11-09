"""Years-asked filter, country scope, and the 'skip unchanged work' paths."""
import json
from datetime import datetime, timezone

from lighthouse import config, db, fetch, geo, profile, workers
from lighthouse.extract import rules, yoe_from_title
from lighthouse.models import Job
from lighthouse.sources.base import Unchanged
from lighthouse.sources.greenhouse import GreenhouseSource

from conftest import FakeClient, FakeResp


def job(eid, title="Data Analyst", loc="Austin, TX", body=None, company="Acme"):
    body = body if body is not None else "<p>" + f"Build dashboards for team {eid}. " * 30 + "</p>"
    return Job("greenhouse", "acme", str(eid), title, company, loc, f"https://x/{eid}", body,
               posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


def cfg(tmp_path):
    return config.load(tmp_path / "none.toml")


def test_yoe_detection_variants():
    for text, want in [("- 5+ years' experience maintaining systems", 5), ("- 4+ years’ experience as an attorney", 4),
                       ("- 5+ years of commercial contracting, drafting or partnership negotiation experience", 5),
                       ("- At least one year of previous industry experience", 1),
                       ("- manager (minimum 2-5 years) with a track record", 2),
                       ("- 4+ years of experience with Linux (RHEL or equivalent preferred)", 4),
                       ("- 2+ years of experience preferred", 2),
                       ("- 3+ years of backend experience, ideally in fintech", 3),
                       ("- 10+ total years of engineering experience with a minimal 4 years of management experience", 10),
                       ("Requirements\n- 3+ years of SQL experience\nNice to have\n- 9+ years of finance experience", 3)]:
        assert rules(text)["yoe_min"]["value"] == want, text
    assert rules("- 2+ years of experience preferred")["yoe_min"].get("preferred") is True
    assert "preferred" not in rules("- 3+ years of backend experience, ideally in fintech")["yoe_min"]
    assert yoe_from_title("Senior Data Analyst") == 5 and yoe_from_title("Staff Accountant") is None
    assert yoe_from_title("Software Engineering Intern") == 0 and yoe_from_title("Data Analyst") is None


def test_years_asked_filter_and_title_fallback(tmp_path):
    conn = db.connect(tmp_path / "y.db")
    db.upsert_board(conn, "greenhouse", "acme", [
        job(1, body="<p>Requires 2+ years of experience in SQL. " + "Lots of detail here. " * 20 + "</p>"),
        job(2, title="Analytics Lead", body="<p>Requires 7+ years of experience leading teams. " + "More text follows. " * 20 + "</p>"),
        job(3, title="Senior Data Analyst", body="<p>" + "No number stated anywhere in this one. " * 20 + "</p>"),
        job(4, title="Data Analyst II", body="<p>" + "Nothing about years in this posting at all. " * 20 + "</p>")])
    fetch.postprocess(conn, cfg(tmp_path))
    titles = lambda f: sorted(r["title"] + f":{r['yoe_min']}" for r in db.list_jobs(conn, f)[0])
    assert db.get_job(conn, "greenhouse:acme:3")["yoe_source"] == "title"
    assert titles(db.Filters(yoe_lo=3)) == ["Analytics Lead:7", "Data Analyst II:None", "Senior Data Analyst:5"]
    assert titles(db.Filters(yoe_lo=3, yoe_unknown=False)) == ["Analytics Lead:7", "Senior Data Analyst:5"]
    assert titles(db.Filters(yoe_hi=4, yoe_unknown=False)) == ["Data Analyst:2"]
    assert [r["yoe_min"] for r in db.list_jobs(conn, db.Filters(sort="yoe"))[0]] == [2, 5, 7, None]
    # a title-based estimate never rules anyone out
    p = {**profile.DEFAULT, "years_experience": 1}
    assert db.get_job(conn, "greenhouse:acme:3", p)["elig"] == 1
    assert db.get_job(conn, "greenhouse:acme:2", p)["elig"] == 0


def test_geo_classifier():
    cases = {"San Francisco, CA": {"US"}, "US, CA, Santa Clara": {"US"}, "Remote - US": {"US"}, "London, UK": {"GB"},
             "Toronto, Ontario, Canada": {"CA"}, "Vancouver, BC": {"CA"}, "Bengaluru, India": {"IN"}, "Remote": set(),
             "New York City, NY; Dublin": {"US", "IE"}, "2 Locations": set(), "USA - Remote": {"US"}, "SF": {"US"}}
    for loc, want in cases.items():
        assert set(geo.countries(loc)) == want, loc
    assert geo.in_scope(["Remote"], {"US"}) and not geo.in_scope(["London, UK"], {"US"})
    assert geo.in_scope(["London, UK", "Austin, TX"], {"US"})


def test_country_scope_filters_list_and_ingest_without_closing(tmp_path, monkeypatch):
    cfgp = tmp_path / "s.toml"
    cfgp.write_text('[settings]\nskip_hourly_titles = false\n[[boards]]\nats="greenhouse"\nslug="acme"\n')
    path = tmp_path / "g.db"
    postings = [job(1, loc="Austin, TX"), job(2, title="Analyst", loc="London, UK"), job(3, title="Engineer", loc="Remote")]

    def fake(self, slug, known=None):
        return [j for j in postings if self.wanted(j)]
    monkeypatch.setattr(GreenhouseSource, "fetch", fake)
    fetch.run(cfgp, path)
    conn = db.connect(path)
    assert db.list_jobs(conn, db.Filters(countries=("US",)))[1] == 2          # London hidden
    profile.save(conn, {**profile.DEFAULT, "countries": ["US"]})
    res = fetch.run(cfgp, path)[0]
    assert res["filtered_out"] == 1 and res["closed"] == 0                       # skipped, not "closed"
    assert db.get_job(conn, "greenhouse:acme:2")["active"] == 1


def test_unchanged_board_and_descriptions_are_skipped(tmp_path, gh):
    conn = db.connect(tmp_path / "u.db")
    client = FakeClient({"boards-api.greenhouse.io": gh})
    src = GreenhouseSource(client=client)
    first = src.fetch("acme")
    assert all(j.content_html for j in first)
    db.upsert_board(conn, "greenhouse", "acme", first)
    db.board_result(conn, "greenhouse", "acme", count=2, body_hash=src.body_hash)
    # same bytes again -> Unchanged before any parsing
    again = GreenhouseSource(client=client)
    again.prev_body_hash = src.body_hash
    try:
        again.fetch("acme")
        assert False
    except Unchanged:
        pass
    # payload changed (new job) but existing descriptions didn't -> they aren't re-sanitized or rewritten
    gh2 = json.loads(json.dumps(gh))
    gh2["jobs"].append({**gh["jobs"][0], "id": 1003, "title": "New Role"})
    third = GreenhouseSource(client=FakeClient({"boards-api.greenhouse.io": gh2}))
    jobs = third.fetch("acme", known=db.known_postings(conn, "greenhouse", "acme"))
    assert {j.external_id: bool(j.content_html) for j in jobs} == {"1001": False, "1002": False, "1003": True}
    counts = db.upsert_board(conn, "greenhouse", "acme", jobs)
    assert (counts["new"], counts["unchanged"]) == (1, 2)
    assert "<strong>analytics</strong>" in db.get_job(conn, "greenhouse:acme:1001")["content_html"]


def test_precompute_parallel_matches_serial(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "PARALLEL_MIN", 2)
    a, b = db.connect(tmp_path / "a.db"), db.connect(tmp_path / "b.db")
    jobs = [job(i, body=f"<p>Needs {i % 9 + 1}+ years of experience. " + f"Topic {i} details. " * 25 + "</p>")
            for i in range(12)]
    for c in (a, b):
        db.upsert_board(c, "greenhouse", "acme", jobs)
    assert fetch.precompute(a) == 12
    monkeypatch.setattr(workers, "PARALLEL_MIN", 10**9)
    assert fetch.precompute(b) == 12
    q = "SELECT j.uid, j.minhash, e.data FROM jobs j JOIN extractions e ON e.content_hash = j.content_hash ORDER BY j.uid"
    assert [tuple(r) for r in a.execute(q)] == [tuple(r) for r in b.execute(q)]
    assert fetch.precompute(a) == 0   # nothing left to do


def test_marks_column_follows_clusters(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    db.upsert_board(conn, "greenhouse", "acme", [job(1), job(2, loc="Denver, CO", body=job(1).content_html)])
    fetch.postprocess(conn, cfg(tmp_path))
    db.set_mark(conn, "greenhouse:acme:2", "saved")
    assert {r["mark"] for r in conn.execute("SELECT mark FROM jobs")} == {2}
    assert db.list_jobs(conn, db.Filters(view="saved"))[1] == 1


def test_counts_are_capped(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "c.db")
    db.upsert_board(conn, "greenhouse", "acme", [job(i, title=f"Role {i}") for i in range(30)])
    fetch.postprocess(conn, cfg(tmp_path))
    n = db.count_jobs(conn, db.Filters(), cap=10)
    assert n == 10 and n.capped
    assert not db.count_jobs(conn, db.Filters(), cap=100).capped


def test_geo_north_america_shapes():
    """Real location strings from the large-employer build, for a US/Canada/Mexico scope."""
    cases = {"Tampa Florida United States": {"US"}, "Erie PA, 16502, 232 West 25th St": {"US"},
             "Mississauga Ontario Canada": {"CA"}, "CA-QC-LONGUEUIL-J01 ~ 1000 Blvd Marie-Vi": {"CA"},
             "Querétaro, Mexico": {"MX"}, "Tijuana, Baja California": {"MX"}, "Monterrey, NL": {"MX"},
             "Albuquerque, New Mexico": {"US"}, "St. John's, NL": {"CA"}, "Ontario, CA": {"US"},
             "Wilmington, DE": {"US"}, "DE - Berlin": {"DE"}, "GB - Luton - Cap. Green 300": {"GB"},
             "IT - Roma - Via Faustiniana": {"IT"}, "Toulouse": {"FR"}, "Vélizy-Villacoublay": {"FR"},
             "Schenectady": {"US"}, "IN - TDC 1 (IN110)": set()}
    for loc, want in cases.items():
        assert set(geo.countries(loc)) == want, loc
    # A bare name a North American city also has is never placed abroad (it would hide a local job).
    assert geo.countries("Birmingham") == frozenset() and geo.countries("Stafford") == frozenset()
    assert geo.in_scope(["Birmingham"], {"US", "CA", "MX"}) and not geo.in_scope(["Toulouse"], {"US", "CA", "MX"})
