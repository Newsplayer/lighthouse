"""A posting's own closing date, which descriptions are read first, how a build reports its health,
and the check of listed boards against their companies' names."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from conftest import FakeClient, FakeResp

from lighthouse import db, discover, site, snapshot
from lighthouse.models import Job, parse_expiry
from lighthouse.sources import SOURCES

from test_enterprise import _ld
from test_index import Builder, job

NOW = datetime.now(timezone.utc)
PAST, FUTURE = NOW - timedelta(days=2), NOW + timedelta(days=30)


def _state(conn, eid):
    return tuple(conn.execute("SELECT active, expires_at IS NOT NULL FROM jobs WHERE external_id = ?", (str(eid),)).fetchone())


def _events(conn, eid):
    return [r[0] for r in conn.execute("SELECT kind FROM events WHERE uid LIKE ? ORDER BY id", (f"%:{eid}",))]


# ---------------------------------------------------------------- closing dates

def test_a_posting_past_its_closing_date_is_closed(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    db.add_board(conn, "greenhouse", "acme")
    jobs = [replace(job(1), expires_at=FUTURE), job(2), job(3)]
    db.upsert_board(conn, "greenhouse", "acme", jobs)
    assert _state(conn, 1) == (1, 1) and _state(conn, 2) == (1, 0)

    # the board still lists it, but its date has passed: closed once, and never reopened by the listing
    lapsed = [replace(job(1), expires_at=PAST), job(2), job(3)]
    counts = db.upsert_board(conn, "greenhouse", "acme", lapsed)
    assert counts["closed"] == 1 and counts["touched"] == {"acme"} and _state(conn, 1)[0] == 0
    again = db.upsert_board(conn, "greenhouse", "acme", lapsed)
    assert again["closed"] == 0 and _events(conn, 1) == ["new", "closed"]

    # a page that isn't re-read (kept as it is) closes when its stored date passes
    db.upsert_board(conn, "greenhouse", "acme", [job(2), replace(job(3), expires_at=FUTURE)], keep_alive={"greenhouse:acme:1"})
    assert db.close_expired(conn) == set()
    conn.execute("UPDATE jobs SET expires_at = ? WHERE external_id = '3'", (PAST.isoformat(timespec="seconds"),))
    assert db.close_expired(conn) == {"acme"} and _state(conn, 3)[0] == 0 and _events(conn, 3) == ["new", "closed"]
    assert db.close_expired(conn) == set()                                   # once

    # the employer extends the date: open again; a posting that arrives already lapsed is never stored
    back = db.upsert_board(conn, "greenhouse", "acme", [replace(job(1), expires_at=FUTURE), job(2),
                                                         replace(job(4), expires_at=PAST)],
                           keep_alive={"greenhouse:acme:3"})
    assert _state(conn, 1) == (1, 1) and _events(conn, 1)[-1] == "reopened" and back["new"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE external_id = '4'").fetchone()[0] == 0

    # a summary-only listing (no detail call this run) keeps the stored date
    db.upsert_board(conn, "greenhouse", "acme", [replace(job(1, title="Data Analyst II"), partial=True), job(2)],
                    keep_alive={"greenhouse:acme:3"})
    assert _state(conn, 1) == (1, 1)


def test_closing_date_formats():
    assert parse_expiry("2026-12-01") == datetime(2026, 12, 2, tzinfo=timezone.utc)      # a bare date: that whole day
    assert parse_expiry("2026-12-01T17:00:00Z") == datetime(2026, 12, 1, 17, tzinfo=timezone.utc)
    assert parse_expiry("") is None and parse_expiry("soon") is None and parse_expiry(None) is None


def test_jsonld_reader_reads_valid_through_and_a_lapsed_page_closes(tmp_path):
    def routes(dates):
        urls = "".join(f"<url><loc>https://www.example.com/jobs/r-{i}/analyst/</loc><lastmod>{d}</lastmod></url>"
                       for i, d in dates.items())
        r = {"sitemap.xml": FakeResp({}, text=f"<urlset>{urls}</urlset>"), "/robots.txt": FakeResp({}, 404, text="")}
        for i, d in dates.items():
            r[f"/jobs/r-{i}/"] = FakeResp({}, text=_ld(f"Analyst {i}", {"validThrough": d} if i == 1 else None))
        return r
    slug = "https://www.example.com/sitemap.xml"
    conn = db.connect(tmp_path / "b.db")
    db.add_board(conn, "jsonld", slug)
    first = SOURCES["jsonld"](client=FakeClient(routes({1: FUTURE.date().isoformat(), 2: "2026-10-02"}))).fetch(slug)
    assert [j.expires_at is not None for j in first] == [True, False]
    db.upsert_board(conn, "jsonld", slug, first)
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1").fetchone()[0] == 2
    # the page changed (new lastmod) and now says it closed two days ago
    src = SOURCES["jsonld"](client=FakeClient(routes({1: PAST.date().isoformat(), 2: "2026-10-02"})))
    second = src.fetch(slug, known=db.known_postings(conn, "jsonld", slug))
    counts = db.upsert_board(conn, "jsonld", slug, second, keep_alive=src.dropped)
    assert counts["closed"] == 1
    assert [r[0] for r in conn.execute("SELECT title FROM jobs WHERE active = 1")] == ["Analyst 2"]


# ---------------------------------------------------------------- which descriptions are read first

def test_description_calls_go_to_new_postings_first_then_known_categories_newest_first():
    src = SOURCES["workday"](client=FakeClient({}), max_detail_calls=3)
    day = lambda d: datetime(2026, 11, d, tzinfo=timezone.utc)
    mk = lambda eid, title, d: Job("workday", "acme.wd1/x", eid, title, "Acme", "Austin, TX", "", posted_at=d and day(d))
    waiting = mk("old", "Software Engineer", 9)                 # seen on an earlier run, still without its text
    jobs = [waiting, mk("odd", "Wrangler of Llamas", 8), mk("a", "Data Analyst", 3), mk("b", "Accountant", 7),
            mk("undated", "Nurse", None)]
    src.known = {waiting.uid: ("", False)}
    assert [j.external_id for j in src.detail_todo(jobs)] == ["b", "a", "undated"]
    src.max_detail_calls = 10
    assert [j.external_id for j in src.detail_todo(jobs)] == ["b", "a", "undated", "odd", "old"]


# ---------------------------------------------------------------- build health

def test_build_health_reaches_the_manifest_the_summary_and_the_site(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1), job(2), job(3)])
    conn = db.connect(b.path)
    started = conn.execute("SELECT MIN(at) FROM events").fetchone()[0]
    db.upsert_board(conn, "greenhouse", "acme", [job(1), job(2)])            # one closes
    conn.execute("DELETE FROM job_content WHERE uid LIKE '%:2'")            # one still waits for its description
    db.board_result(conn, "greenhouse", "beta", error="HTTP 500 | <html>")
    conn.execute("UPDATE boards SET last_ok = '2026-11-01T00:00:00+00:00' WHERE slug = 'beta'")
    results = [{"source": "greenhouse", "board": "acme", "new": 3, "closed": 1, "held": 0},
               {"source": "greenhouse", "board": "beta", "error": "HTTP 500"},
               {"source": "greenhouse", "board": "gamma", "deferred": True},
               {"source": "greenhouse", "board": "delta", "unchanged_board": True}]
    h = snapshot.health(conn, results, started, 61.26)
    assert h["build_minutes"] == 61.3 and h["boards"] == {"read": 1, "unchanged": 1, "failed": 1, "waiting": 1, "switched_off": 0}
    assert h["postings"] == {"open": 2, "new": 3, "closed": 1, "reopened": 0, "held": 0}
    assert h["with_description_pct"] == 50.0 and h["systems"] == {"greenhouse": {"postings": 2, "with_description_pct": 50.0}}
    assert [(f["board"], f["since"][:10]) for f in h["failing"]] == [("greenhouse:beta", "2026-11-01")]
    db.set_meta(conn, "build_health", h)
    conn.commit()
    conn.close()

    m = b.export()
    assert m["health"] == json.loads(json.dumps(h))
    text = snapshot.summary_markdown(m)
    assert "3 new, 1 closed" in text and "| greenhouse | 2 | 50.0% |" in text and "1 job boards need a look" in text
    issue = snapshot.boards_markdown(m)
    assert "| `greenhouse:beta` |  | 2026-11-01 | HTTP 500 / <html> |" in issue  # a table cell never breaks the table
    m["health"]["failing"][0]["since"] = m["built_at"]                        # one bad run is not worth an issue
    assert snapshot.boards_markdown(m) == "" and snapshot.boards_markdown({}) == ""

    out = tmp_path / "site"
    site.generate(b.path, out, countries=())
    latest = json.loads((out / "data" / "latest.json").read_text())
    src = json.loads((out / "data" / latest["sources"]).read_text())
    assert src["health"]["postings"]["closed"] == 1 and "failing" not in src["health"]
    assert "health" not in latest                                             # nothing added to the file every visit fetches


# ---------------------------------------------------------------- listed boards against company names

def test_report_of_boards_that_dont_look_like_their_company():
    boards = [{"ats": "greenhouse", "slug": "stripe", "company": "Stripe"},
              {"ats": "greenhouse", "slug": "figureai", "company": "FightCamp"},
              {"ats": "workday", "slug": "jpmc.wd5/External", "company": "JPMorgan Chase"},
              {"ats": "lever", "slug": "noname", "company": None}]
    companies = [{"company": "Meta", "domain": "meta.com", "ats": "workday", "slug": "fb.wd1/Careers"},
                 {"company": "Alphabet", "domain": "abc.xyz", "ats": "greenhouse", "slug": "abc"},
                 {"company": "Unpinned", "domain": "unpinned.com", "ats": "", "slug": ""}]
    odd = discover.mismatched(boards, companies)
    assert [(b["company"], b["slug"]) for b in odd] == [("FightCamp", "figureai"), ("Meta", "fb.wd1/Careers")]
