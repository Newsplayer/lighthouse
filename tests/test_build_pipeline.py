"""The index build under a time limit: boards that push back are left alone (never worked around),
boards not reached go first next run, paged listings need two misses to close a posting, a role's
places live on one row, the published index carries one description per role, and the postings
table / CSV have a column per filter."""
import csv
import gzip
import json
import lzma
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from lighthouse import config, db, fetch, snapshot
from lighthouse.models import Job
from lighthouse.sanitize import sanitize_html
from lighthouse.sources import SOURCES
from lighthouse.sources.base import Blocked, RunGate, json_body, looks_blocked
from lighthouse.sources.workday import WorkdaySource

from test_index import Builder, base_file, reader, unpacked
from test_index import job as index_job

BODY = "<p>Build dashboards with SQL. 3+ years of experience required. " + " ".join(
    f"word{k}" for k in range(80)) + "</p>"


class Resp:
    def __init__(self, status=200, data=None, text=None, ctype="application/json"):
        self.status_code = status
        self._data = data
        self.text = text if text is not None else json.dumps(data)
        self.content = self.text.encode()
        self.headers = {"content-type": ctype}

    def json(self):
        if self._data is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(f"{self.status_code} Server Error")
            err.response = self
            raise err


class WorkdayFake:
    """Workday CXS: wd1 answers with a bot-check page, every other data center with JSON."""

    def __init__(self):
        self.calls = []
        self.counts, self.blocked = {}, {}

    def post(self, url, **kw):
        self.calls.append(url)
        if ".wd1." in url:
            return Resp(200, None, "<html><title>Just a moment...</title></html>", "text/html")
        return Resp(200, {"total": 1, "jobPostings": [{"title": "Data Analyst", "externalPath": "/job/Austin/Data-Analyst_JR1",
                                                       "locationsText": "Austin, TX", "postedOn": "Posted Today"}]})

    def get(self, url, **kw):
        self.calls.append(url)
        return Resp(200, {"jobPostingInfo": {"jobDescription": BODY, "location": "Austin, TX", "startDate": "2026-11-01"}})


def workday_cfg(tmp_path, slugs, extra=""):
    p = tmp_path / "wd.toml"
    p.write_text("[settings]\nfetch_interval_hours = 0\nfetch_workers = 1\n" + extra + "\n"
                 + "".join(f'[[boards]]\nats = "workday"\nslug = "{s}"\n\n' for s in slugs))
    return p


# ---------------------------------------------------------------- pushing back is respected

def test_block_pages_are_recognised():
    with pytest.raises(Blocked, match="text/html"):
        json_body(Resp(200, None, "<html>challenge</html>", "text/html"), "Workday")
    with pytest.raises(Blocked, match="403"):
        json_body(Resp(403, {"x": 1}), "Workday")
    assert json_body(Resp(200, {"ok": 1}), "Workday") == {"ok": 1}
    import requests
    assert looks_blocked(requests.exceptions.RetryError("Max retries exceeded (too many 500 error responses)"))
    assert looks_blocked(Blocked("x"))
    assert not looks_blocked(RuntimeError("HTTP 404"))
    assert WorkdaySource.group("acme.wd5/Careers") == "workday:wd5"
    assert SOURCES["greenhouse"].group("acme") == "greenhouse" and SOURCES["jsonld"].group("x") is None


def test_a_data_center_that_pushes_back_is_left_alone_for_the_run(tmp_path, monkeypatch):
    slugs = [f"t{i}.wd1/Site" for i in range(6)] + ["ok1.wd5/Site", "ok2.wd5/Site"]
    fake = WorkdayFake()
    monkeypatch.setattr(fetch, "default_client", lambda *a, **k: fake)
    results = fetch.run(workday_cfg(tmp_path, slugs), tmp_path / "b.db", use_snapshot=False)
    by = {r["board"]: r for r in results}
    wd1 = [by[s] for s in slugs[:6]]
    assert sum(1 for r in wd1 if "error" in r) == 3                      # three block pages...
    assert sum(1 for r in wd1 if r.get("deferred")) == 3                 # ...then wd1 is left alone
    assert sum(1 for c in fake.calls if ".wd1." in c) == 3               # no request after that
    assert all("Pushed back" in r["error"] for r in wd1 if "error" in r)
    assert by["ok1.wd5/Site"]["new"] == 1 and by["ok2.wd5/Site"]["new"] == 1   # other data centers carry on
    conn = db.connect(tmp_path / "b.db")
    rows = {r["slug"]: r for r in conn.execute("SELECT * FROM boards")}
    assert all(rows[s]["fail_count"] == 0 and rows[s]["enabled"] == 1 for s in slugs)   # never switched off
    deferred = [s for s in slugs[:6] if by[s].get("deferred")]
    assert all(rows[s]["last_fetch"] is None for s in deferred)          # untouched, so first in line next run
    order = [b["slug"] for b in fetch.order_boards([dict(r) for r in rows.values()])]
    assert set(order[:6]) == set(slugs[:6])


def test_time_limit_defers_boards_and_description_calls(tmp_path, monkeypatch):
    gate = RunGate(time.monotonic() - 1)                 # time is already up
    board = {"ats": "workday", "slug": "a.wd5/Site"}
    assert fetch._fetch_board(board, {}, WorkdayFake(), 10, gate=gate) == (fetch.DEFERRED, None)
    # a board already running finishes its listing, but starts no more description calls
    fake = WorkdayFake()
    src = WorkdaySource(client=fake, max_detail_calls=10)
    src.gate, src.slug = gate, "a.wd5/Site"
    jobs = src.fetch("a.wd5/Site")
    assert len(jobs) == 1 and jobs[0].partial and not any("/job/" in c for c in fake.calls)


def test_boards_go_stalest_first_then_biggest_and_workday_every_other_run():
    now = datetime.now(timezone.utc)
    ago = lambda h: (now - timedelta(hours=h)).isoformat()
    boards = [{"ats": "greenhouse", "slug": "small", "last_ok": ago(6), "job_count": 5},
              {"ats": "greenhouse", "slug": "big", "last_ok": ago(6), "job_count": 900},
              {"ats": "greenhouse", "slug": "missed", "last_ok": ago(12), "job_count": 1},
              {"ats": "greenhouse", "slug": "new", "last_ok": None, "job_count": None}]
    assert [b["slug"] for b in fetch.order_boards(boards)] == ["new", "missed", "big", "small"]
    s = config.Settings(workday_refresh_hours=11)
    assert fetch.not_due({"ats": "workday", "slug": "x.wd5/S", "last_ok": ago(6)}, s)
    assert not fetch.not_due({"ats": "workday", "slug": "x.wd5/S", "last_ok": ago(12)}, s)
    assert not fetch.not_due({"ats": "workday", "slug": "x.wd5/S", "last_ok": ago(6), "last_error": "Pushed back"}, s)
    assert not fetch.not_due({"ats": "greenhouse", "slug": "x", "last_ok": ago(1)}, s)


# ---------------------------------------------------------------- closures

def posting(eid, city="Austin, TX", board="acme", title="Data Analyst", body=BODY):
    return Job("workday", board, str(eid), title, "Acme", city, f"https://x/{eid}", sanitize_html(body))


def test_paged_listings_close_a_posting_after_two_misses(tmp_path):
    conn = db.connect(tmp_path / "c.db")
    db.add_board(conn, "workday", "acme")
    jobs = [posting(i, title=f"Analyst {i}", body=f"<p>Role {i}</p>") for i in range(5)]
    db.upsert_board(conn, "workday", "acme", jobs, close_after=2)
    seen = lambda: conn.execute("SELECT last_seen FROM jobs WHERE external_id='4'").fetchone()[0]
    before = seen()
    time.sleep(0.01)
    once = db.upsert_board(conn, "workday", "acme", jobs[:4], close_after=2)   # skipped by a shifting page
    assert once["closed"] == 0 and once["missed"] == 1
    assert tuple(conn.execute("SELECT active, missed FROM jobs WHERE external_id='4'").fetchone()) == (1, 1)
    assert seen() == before                                                   # not seen, not refreshed
    back = db.upsert_board(conn, "workday", "acme", jobs, close_after=2)       # it was there all along
    assert back["closed"] == 0 and conn.execute("SELECT missed FROM jobs WHERE external_id='4'").fetchone()[0] == 0
    db.upsert_board(conn, "workday", "acme", jobs[:4], close_after=2)
    gone = db.upsert_board(conn, "workday", "acme", jobs[:4], close_after=2)    # twice in a row: closed
    assert gone["closed"] == 1 and conn.execute("SELECT active FROM jobs WHERE external_id='4'").fetchone()[0] == 0
    # a one-response board (Greenhouse...) is exact: one miss closes
    assert db.upsert_board(conn, "workday", "acme", jobs[:3])["closed"] == 1
    # an identical response after a miss confirms it
    db.upsert_board(conn, "workday", "acme", jobs[:2], close_after=2)
    assert db.board_unchanged(conn, "workday", "acme") == {"acme"}
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 2


# ---------------------------------------------------------------- one role, one row of places

def cluster_builder(tmp_path, cities):
    b = Builder(tmp_path)
    conn = db.connect(b.path)
    db.sync_config_boards(conn, b.cfg.boards)
    jobs = [Job("greenhouse", "acme", str(i), "Data Analyst", "Acme", c, f"https://x/{i}", sanitize_html(BODY))
            for i, c in enumerate(cities)]
    db.upsert_board(conn, "greenhouse", "acme", jobs)
    db.board_result(conn, "greenhouse", "acme", count=len(jobs))
    fetch.postprocess(conn, b.cfg)
    return b, conn, jobs


def test_a_roles_places_live_on_its_canonical_row(tmp_path):
    cities = ["Austin, TX", "Denver, CO", "Boston, MA", "Seattle, WA"]
    b, conn, jobs = cluster_builder(tmp_path, cities)
    rows = conn.execute("SELECT is_canonical, cluster_count, cluster_locations FROM jobs ORDER BY is_canonical DESC").fetchall()
    assert rows[0][0] == 1 and rows[0][1] == 4 and sorted(json.loads(rows[0][2])) == sorted(cities)
    assert all(r[0] == 0 and r[1] == 1 and r[2] is None for r in rows[1:])
    b.export()
    # one listing closes: the canonical row (count, places) and that listing change, not every listing
    db.upsert_board(conn, "greenhouse", "acme", jobs[:3])
    fetch.postprocess(conn, b.cfg)
    conn.close()
    assert b.export()["changed_this_run"] == 2


def test_index_ships_one_description_per_role_and_search_still_finds_every_city(tmp_path):
    b, conn, jobs = cluster_builder(tmp_path, ["Austin, TX", "Denver, CO", "Boston, MA"])
    conn.close()
    m = b.export()
    path = unpacked(base_file(b), tmp_path)
    c = sqlite3.connect(path)
    assert c.execute("SELECT COUNT(*) FROM job_content").fetchone()[0] == 1     # was 3
    assert c.execute("SELECT COUNT(*) FROM jobs_fts").fetchone()[0] == 1
    c.close()
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, base_file(b), full=True)
    rows, total = db.list_jobs(r, db.Filters(q="Denver"))
    assert total == 1                                                            # a member's city finds the role
    member = r.execute("SELECT uid FROM idx.jobs WHERE is_canonical = 0 LIMIT 1").fetchone()[0]
    assert "dashboards" in db.content(r, member)                                 # its page shows the role's text
    assert m["fill"]["roles"] == 1 and m["fill"]["with_description_pct"] == 100.0
    assert m["jobs"] == 1 and m["postings"] == 3                                   # shown as "open roles"


def test_a_duplicate_listings_page_shows_its_roles_places(tmp_path):
    b, conn, jobs = cluster_builder(tmp_path, ["Austin, TX", "Denver, CO"])
    conn.close()
    b.export()
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, base_file(b), full=True)
    member = r.execute("SELECT uid FROM idx.jobs WHERE is_canonical = 0").fetchone()[0]
    assert sorted(json.loads(db.get_job(r, member)["cluster_locations"])) == ["Austin, TX", "Denver, CO"]


def test_a_delta_moves_search_to_the_listing_that_now_leads_the_role(tmp_path):
    def listing(eid, city, day):
        return Job("greenhouse", "acme", str(eid), "Data Analyst", "Acme", city, f"https://x/{eid}",
                   sanitize_html(BODY), posted_at=datetime(2026, 10, day, tzinfo=timezone.utc))
    b = Builder(tmp_path)
    conn = db.connect(b.path)
    db.sync_config_boards(conn, b.cfg.boards)
    first = [listing(1, "Chicago, IL", 5), listing(2, "Denver, CO", 6)]
    first += [index_job(i, f"Role {i}") for i in range(10, 16)]   # other roles: the change stays a delta
    db.upsert_board(conn, "greenhouse", "acme", first)
    fetch.postprocess(conn, b.cfg)
    b.export()
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, base_file(b), full=True)
    # an earlier listing of the same role turns up: it leads the role now, the old lead stays open
    db.upsert_board(conn, "greenhouse", "acme", first + [listing(3, "Boston, MA", 1)])
    fetch.postprocess(conn, b.cfg)
    conn.close()
    b.export()
    snapshot.import_file(r, rcfg, next(b.dist.glob("delta-*.db.xz")), full=False)
    lead = r.execute("SELECT uid FROM idx.jobs WHERE is_canonical = 1 AND title = 'Data Analyst'").fetchone()[0]
    assert lead == "greenhouse:acme:3"
    assert r.execute("SELECT COUNT(*) FROM idx.jobs WHERE fts_rowid IS NOT NULL AND title = 'Data Analyst'").fetchone()[0] == 1
    for city in ("Chicago", "Denver", "Boston"):
        assert db.list_jobs(r, db.Filters(q=city))[1] == 1, city
    assert "dashboards" in db.content(r, lead)


def test_a_builder_restarted_from_the_index_gets_every_description_back(tmp_path):
    b, conn, jobs = cluster_builder(tmp_path, ["Austin, TX", "Denver, CO", "Boston, MA"])
    db.board_result(conn, "greenhouse", "acme", count=3, body_hash="same")
    conn.close()
    b.export()
    fresh = tmp_path / "fresh.db"
    snapshot.seed_builder(base_file(b), fresh)
    conn = db.connect(fresh)
    assert conn.execute("SELECT COUNT(*) FROM job_content").fetchone()[0] == 1          # one per role
    assert conn.execute("SELECT body_hash FROM boards WHERE slug = 'acme'").fetchone()[0] is None   # re-read once
    out = db.upsert_board(conn, "greenhouse", "acme", jobs)                               # the same listing
    assert conn.execute("SELECT COUNT(*) FROM job_content").fetchone()[0] == 3
    assert out["touched"] == {"acme"}
    db.upsert_board(conn, "greenhouse", "acme", jobs)
    assert conn.execute("SELECT COUNT(*) FROM job_content").fetchone()[0] == 3


def test_postings_table_and_csv_have_a_column_per_filter(tmp_path):
    b = Builder(tmp_path)
    conn = db.connect(b.path)
    db.sync_config_boards(conn, b.cfg.boards)
    body = ("<p>Senior Data Engineer. 5+ years of experience. Bachelor's degree required. We cannot sponsor visas. "
            "Hybrid in Austin. Salary $150,000 - $180,000 per year. Skills: Python, SQL, Spark. "
            "Benefits: medical insurance, 401(k), stock options. Travel up to 10%.</p>")
    db.upsert_board(conn, "greenhouse", "acme", [
        Job("greenhouse", "acme", "1", "Senior Data Engineer", "Acme", "Austin, TX", "https://x/1", sanitize_html(body)),
        Job("greenhouse", "acme", "2", "Barista", "Acme", "Austin, TX", "https://x/2", sanitize_html("<p>Coffee.</p>"))])
    db.board_result(conn, "greenhouse", "acme", count=2)
    fetch.postprocess(conn, b.cfg)
    conn.close()
    m = b.export()
    path = unpacked(base_file(b), tmp_path)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM postings WHERE title = 'Senior Data Engineer'").fetchone()
    assert row["years_experience"] == 5 and row["degree"] == "bachelors" and row["visa_sponsorship"] == "no"
    assert row["pay_min_per_year"] == 150000 and row["pay_max_per_year"] == 180000 and row["pay_currency"] == "USD"
    assert row["countries"] == "US" and row["all_locations"] == "Austin, TX" and row["seniority"] == "senior"
    assert "python" in row["skills"] and "health" in row["benefits"] and row["travel"] == "occasional"
    assert row["has_description"] == 1 and row["url"] == "https://x/1"
    c.close()
    # the CSV next to the index files: same columns, one row per open role
    assert m["csv"]["rows"] == 2
    with gzip.open(b.dist / snapshot.CSV_NAME, "rt", encoding="utf-8", newline="") as f:
        table = list(csv.DictReader(f))
    assert len(table) == 2 and {"years_experience", "visa_sponsorship", "pay_max_per_year", "skills"} <= set(table[0])
    eng = next(t for t in table if t["title"] == "Senior Data Engineer")
    assert eng["years_experience"] == "5" and eng["visa_sponsorship"] == "no"
    # and from any index file, read-only
    assert snapshot.write_csv(path, tmp_path / "again.csv.gz")["rows"] == 2


def test_compression_without_the_xz_tool(tmp_path, monkeypatch):
    plain = tmp_path / "x.db"
    plain.write_bytes(b"hello index " * 1000)
    monkeypatch.setattr(snapshot.shutil, "which", lambda name: None)
    snapshot._xz(plain, tmp_path / "x.db.xz")
    assert lzma.decompress((tmp_path / "x.db.xz").read_bytes()) == plain.read_bytes()


def test_builder_settings_fit_the_hour(tmp_path, monkeypatch):
    seen = {}

    def fake_run(config_path, db_path, use_snapshot=True, overrides=None, **kw):
        seen.update(overrides or {})
        return []
    monkeypatch.setattr(fetch, "run", fake_run)
    monkeypatch.setattr(snapshot, "discover_boards", lambda *a, **k: [])
    cfgp = tmp_path / "s.toml"
    cfgp.write_text("[settings]\nfetch_interval_hours = 0\n")
    snapshot.build(tmp_path / "dist", config_path=cfgp, db_path=tmp_path / "b.db", llm=False)
    assert seen["fetch_minutes"] == snapshot.FETCH_MINUTES and seen["fetch_workers"] >= 48
    assert seen["workday_refresh_hours"] == snapshot.WORKDAY_REFRESH_HOURS
    snapshot.build(tmp_path / "dist", config_path=cfgp, db_path=tmp_path / "b.db", llm=False, fetch_minutes=5)
    assert seen["fetch_minutes"] == 5
