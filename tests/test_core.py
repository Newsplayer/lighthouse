import json
import sqlite3
from datetime import datetime, timedelta, timezone

from lighthouse import config, db, dedupe, extract, fetch, profile
from lighthouse.ghost import ghost_reasons
from lighthouse.models import Job, company_key
from lighthouse.sanitize import html_to_text, sanitize_html
from lighthouse.sources.greenhouse import GreenhouseSource

BODY = "<p>" + " ".join(f"We build reliable payment systems for word{i} customers." for i in range(60)) + "</p>"


def job(eid, title="Backend Engineer", loc="Austin, TX", body=BODY, posted=None, board="acme", company="Acme"):
    return Job("greenhouse", board, str(eid), title, company, loc, f"https://x/{eid}", body,
               posted_at=posted or datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_sanitizer_strips_dangerous_markup():
    out = sanitize_html('<p onclick="x()">hi</p><script>bad()</script><a href="javascript:x">j</a><img src=x onerror=y>')
    assert "script" not in out and "bad()" not in out
    assert "onclick" not in out and "javascript" not in out and "<img" not in out
    assert out.startswith("<p>hi</p>")
    assert html_to_text("<p>a</p><ul><li>b</li></ul>") == "a\n\n- b"


def test_company_key():
    assert company_key("Stripe, Inc.") == company_key("stripe") == "stripe"
    assert company_key("Bosch Group") == "bosch"


def test_minhash_similarity():
    a = dedupe.minhash(html_to_text(BODY))
    b = dedupe.minhash(html_to_text(BODY.replace("word5 ", "other ")))
    c = dedupe.minhash(" ".join(f"unrelated text number {i}" for i in range(200)))
    assert dedupe.similarity(a, b) > 0.8 and dedupe.similarity(a, c) < 0.2


def test_ghost_needs_two_signals():
    old = datetime.now(timezone.utc) - timedelta(days=400)
    assert ghost_reasons("Senior Data Analyst", datetime.now(timezone.utc), 60) == []
    assert len(ghost_reasons("Senior Data Analyst", old, 60)) == 1
    assert len(ghost_reasons("General Application - Talent Pool", old, 60)) == 2


def test_cluster_multilocation_and_repost(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    cfg = config.load(tmp_path / "none.toml")
    t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
    # Same role in two cities + a different role with different text.
    other = "<p>" + " ".join(f"Design marketing campaigns for region {i}." for i in range(60)) + "</p>"
    db.upsert_board(conn, "greenhouse", "acme", [job(1, posted=t0), job(2, loc="Denver, CO", posted=t0),
                                                  job(3, title="Marketing Lead", body=other, posted=t0)])
    fetch.postprocess(conn, cfg)
    rows, total = db.list_jobs(conn, db.Filters(show_ghosts=True))
    assert total == 2
    backend = next(r for r in rows if r["title"] == "Backend Engineer")
    assert backend["cluster_count"] == 2 and json.loads(backend["cluster_locations"]) == ["Austin, TX", "Denver, CO"]
    assert backend["repost_count"] == 0  # a second city is expansion, not a repost

    # The Austin posting is pulled and reposted later with a new id and a slightly edited description.
    db.upsert_board(conn, "greenhouse", "acme", [job(2, loc="Denver, CO", posted=t0), job(3, title="Marketing Lead", body=other, posted=t0)])
    db.upsert_board(conn, "greenhouse", "acme", [job(2, loc="Denver, CO", posted=t0), job(3, title="Marketing Lead", body=other, posted=t0),
                                                  job(9, body=BODY.replace("word7 ", "folks "), posted=t0 + timedelta(days=90))])
    fetch.postprocess(conn, cfg)
    r9 = db.get_job(conn, "greenhouse:acme:9")
    assert r9["cluster_id"] == "greenhouse:acme:1"          # joined the original cluster
    assert r9["effective_first_seen"].startswith("2026-03-01")  # inherits the original date
    assert r9["repost_count"] == 1
    kinds = [e["kind"] for e in db.events_for(conn, ["greenhouse:acme:1"])]
    assert kinds == ["new", "closed"]


def test_dedupe_across_boards_and_closing(tmp_path, gh):
    conn = db.connect(tmp_path / "t.db")
    a = [GreenhouseSource.parse("acme", raw) for raw in gh["jobs"]]
    b = [GreenhouseSource.parse("acme-careers", raw) for raw in gh["jobs"]]
    db.upsert_board(conn, "greenhouse", "acme", a)
    db.upsert_board(conn, "greenhouse", "acme-careers", b)
    fetch.postprocess(conn, config.load(tmp_path / "none.toml"))
    rows, total = db.list_jobs(conn, db.Filters(show_ghosts=True))
    assert total == 2 and all(r["cluster_count"] == 2 for r in rows)
    counts = db.upsert_board(conn, "greenhouse", "acme", a[:1])
    assert (counts["new"], counts["updated"], counts["unchanged"], counts["closed"]) == (0, 0, 1, 1)
    assert db.get_job(conn, "greenhouse:acme:1002")["active"] == 0


def test_empty_detail_never_overwrites_content(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_board(conn, "greenhouse", "acme", [job(1)])
    db.upsert_board(conn, "greenhouse", "acme", [job(1, body="")])
    assert "payment systems" in db.get_job(conn, "greenhouse:acme:1")["content_html"]


def test_extraction_and_derived_filters(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = ("<p>Salary: $120,000 - $150,000 per year.</p><ul><li>5+ years of experience in data engineering</li>"
            "<li>Bachelor's degree required</li></ul><p>We are unable to sponsor visas. This role is remote.</p>")
    db.upsert_board(conn, "greenhouse", "acme", [job(1, body=body)])
    fetch.postprocess(conn, config.load(tmp_path / "none.toml"))
    r = db.get_job(conn, "greenhouse:acme:1")
    assert (r["pay_min_annual"], r["pay_max_annual"], r["yoe_min"], r["visa"], r["degree"], r["workplace_eff"]) == \
        (120000, 150000, 5, "no", "bachelors", "remote")
    data, method = extract.get(conn, r["content_hash"])
    assert method == "rules" and "unable to sponsor" in data["visa"]["evidence"]
    assert db.list_jobs(conn, db.Filters(min_salary=140000))[1] == 1
    assert db.list_jobs(conn, db.Filters(min_salary=160000))[1] == 0
    assert db.list_jobs(conn, db.Filters(max_yoe=3))[1] == 0
    assert db.list_jobs(conn, db.Filters(visa="not_no"))[1] == 0


def test_placeholder_salary_is_unknown():
    assert extract.rules("Annual Salary:\n$1—$2 USD")["salary"]["value"] is None
    assert extract.rules("Pay: $45 - $60 per hour")["salary"]["value"]["interval"] == "hour"


def test_eligibility(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    body = "<p>5+ years of experience required. We are unable to sponsor visas.</p>"
    db.upsert_board(conn, "greenhouse", "acme", [job(1, body=body), job(2, title="Data Analyst", loc="Austin, TX",
                                                                       body="<p>" + "Analyze data daily. " * 40 + "</p>")])
    cfg = config.load(tmp_path / "none.toml")
    fetch.postprocess(conn, cfg)
    profile.save(conn, {**profile.DEFAULT, "years_experience": 2, "needs_sponsorship": True, "locations": ["Austin"]})
    profile.recompute(conn, None, 2, force=True)
    r = db.get_job(conn, "greenhouse:acme:1")
    reasons = json.loads(r["elig_reasons"])
    assert r["elig"] == 0 and any("sponsor" in x for x in reasons) and any("5+ years" in x for x in reasons)
    assert db.get_job(conn, "greenhouse:acme:2")["elig"] == 1
    assert db.list_jobs(conn, db.Filters(eligible_only=True))[1] == 1


def test_redaction():
    text = "Jane Q Doe\njane@doe.com · (512) 555-0134 · linkedin.com/in/jane · 12 Oak Street, Austin\nAnalyst, 5 years"
    out = profile.redact(text)
    for leaked in ("Jane", "jane@", "555-0134", "linkedin", "Oak Street"):
        assert leaked not in out
    assert "Analyst, 5 years" in out


def test_marks_hide_whole_cluster(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_board(conn, "greenhouse", "acme", [job(1), job(2, loc="Denver, CO")])
    fetch.postprocess(conn, config.load(tmp_path / "none.toml"))
    db.set_mark(conn, "greenhouse:acme:2", "hidden")  # hide via the non-canonical listing
    assert db.list_jobs(conn, db.Filters())[1] == 0
    assert db.list_jobs(conn, db.Filters(view="hidden"))[1] == 1
    db.set_mark(conn, "greenhouse:acme:2", "applied")
    assert db.list_jobs(conn, db.Filters(view="applied"))[1] == 1 and db.list_jobs(conn, db.Filters())[1] == 1


def test_fts_search(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.upsert_board(conn, "greenhouse", "acme", [job(1), job(2, title="Designer", body="<p>" + "Figma prototypes. " * 30 + "</p>")])
    fetch.postprocess(conn, config.load(tmp_path / "none.toml"))
    assert db.has_fts(conn)
    assert [r["title"] for r in db.list_jobs(conn, db.Filters(q="figma"))[0]] == ["Designer"]
    assert db.list_jobs(conn, db.Filters(q="payment"))[1] == 1


def test_migrates_v01_database(tmp_path, gh):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(db.V1)
    raw.execute("INSERT INTO jobs VALUES ('greenhouse:acme:1','acme|x|y','greenhouse','acme','1','Analyst','Acme',"
                "'Austin, TX','https://x','<p>hi there friend of mine</p>','[]','2026-10-20T09:00:00-05:00',NULL,"
                "'2026-11-02T17:00:00+00:00','2026-11-02T17:00:00+00:00',1,'[\"open 5 days\"]')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    r = db.get_job(conn, "greenhouse:acme:1")
    assert r["posted_at"] == "2026-10-20T14:00:00+00:00" and r["company_key"] == "acme"
    assert fetch.needs_reprocess(conn)
    fetch.postprocess(conn, config.load(tmp_path / "none.toml"))
    assert not fetch.needs_reprocess(conn) and db.list_jobs(conn, db.Filters())[1] == 1


def test_fetch_run_parallel_with_errors(tmp_path, monkeypatch, gh):
    cfgp = tmp_path / "s.toml"
    cfgp.write_text('[[boards]]\nats="greenhouse"\nslug="acme"\n[[boards]]\nats="greenhouse"\nslug="broken"\n[[greenhouse]]\ntoken="legacy"\n')
    from lighthouse.sources.greenhouse import GreenhouseSource as G

    def fake_fetch(self, slug, known=None):
        if slug == "broken":
            raise RuntimeError("boom")
        return [G.parse(slug, raw) for raw in gh["jobs"]]
    monkeypatch.setattr(G, "fetch", fake_fetch)
    results = fetch.run(cfgp, tmp_path / "f.db")
    by = {r["board"]: r for r in results}
    assert by["broken"]["error"] == "boom" and by["acme"]["new"] == 2 and by["legacy"]["new"] == 2
    conn = db.connect(tmp_path / "f.db")
    boards = {b["slug"]: b for b in db.list_boards(conn)}
    assert boards["broken"]["last_error"] == "boom" and boards["acme"]["job_count"] == 2
