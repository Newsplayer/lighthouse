"""The public files: a plain SQLite database anyone can open, the same roles as a CSV, and a manifest.
Written by whitelist; also what a new builder starts from when its cache is lost."""
import csv
import gzip
import io
import lzma
import shutil
import sqlite3
from datetime import datetime, timezone

from lighthouse import config, db, fetch, snapshot
from lighthouse.models import Job
from lighthouse.sanitize import sanitize_html


def job(eid, title="Data Analyst", board="acme", company="Acme", body=None):
    words = " ".join(f"topic{eid}x{k}" for k in range(80))   # distinct text: no accidental clustering
    body = body if body is not None else (f"<p>Build dashboards with SQL. 3+ years of experience. {words}</p>"
                                          "<p>Acme is an equal opportunity employer and values a diverse workforce.</p>")
    return Job("greenhouse", board, str(eid), title, company, f"Austin {eid}, TX", f"https://x/{eid}", sanitize_html(body),
               posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


def write_cfg(tmp_path, name):
    p = tmp_path / f"{name}.toml"
    p.write_text('[[boards]]\nats = "greenhouse"\nslug = "acme"\n\n[[boards]]\nats = "greenhouse"\nslug = "beta"\n')
    return p


class Builder:
    """The GitHub Action's side: a builder DB plus export()."""

    def __init__(self, tmp_path):
        self.tmp, self.path = tmp_path, tmp_path / "builder.db"
        self.cfg = config.load(write_cfg(tmp_path, "builder"))
        self.n = 0

    def ingest(self, board, jobs):
        conn = db.connect(self.path)
        db.sync_config_boards(conn, self.cfg.boards)
        db.upsert_board(conn, "greenhouse", board, jobs)
        db.board_result(conn, "greenhouse", board, count=len(jobs), body_hash=f"h{self.n}")
        fetch.postprocess(conn, self.cfg)
        conn.close()

    def export(self, **kw):
        self.n += 1
        out = self.tmp / f"dist{self.n}"
        self.dist = out
        return snapshot.export(self.path, out, **kw)


def public_db(b, tmp_path) -> sqlite3.Connection:
    """The published jobs.db.xz, unpacked and opened the way anyone would: plain sqlite3, nothing of ours."""
    out = tmp_path / f"public{b.n}.db"
    with lzma.open(b.dist / snapshot.DB_NAME) as f, open(out, "wb") as o:
        shutil.copyfileobj(f, o)
    return sqlite3.connect(out)


def test_public_database_is_plain_whitelisted_and_carries_excerpts_only(tmp_path):
    b = Builder(tmp_path)
    late = "<p>" + "Later paragraphs of the posting stay with the employer. " * 30 + "FINAL-SENTENCE-OF-THE-POSTING</p>"
    b.ingest("acme", [job(1), job(2, "Accountant", body="<p>Close the books every month.</p>" + late)])
    conn = db.connect(b.path)   # things a build machine holds that are not job data
    db.set_meta(conn, "profile", {"resume": "Jane Doe jane@example.com"})
    conn.close()
    m = b.export()
    assert m["db"]["file"] == "jobs.db.xz" and m["db"]["rows"] == 2 and m["db"]["active"] == 2 and m["jobs"] == 2
    assert m["csv"]["rows"] == 2 and len(m["db"]["sha256"]) == 64
    assert sorted(p.name for p in b.dist.iterdir()) == ["jobs.csv.gz", "jobs.db.xz", "manifest.json"]
    c = public_db(b, tmp_path)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"jobs", "excerpts", "events", "extractions", "boards", "companies", "about"}
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert not cols & snapshot.PRIVATE_JOB_COLUMNS and {"uid", "title", "expires_at", "ghost_score", "yoe_min"} <= cols
    raw = b"".join(str(v).encode() for t in tables for row in c.execute(f"SELECT * FROM {t}") for v in row)
    assert b"jane@example.com" not in raw and b"FINAL-SENTENCE-OF-THE-POSTING" not in raw
    # one row per open role, a column per filter, and a short readable excerpt (no function of ours needed)
    row = dict(zip([d[0] for d in c.execute("SELECT * FROM postings").description],
                   c.execute("SELECT * FROM postings WHERE title = 'Accountant'").fetchone()))
    assert row["excerpt"].startswith("Close the books every month.") and len(row["excerpt"]) <= 700
    assert row["has_description"] == 1 and row["url"] == "https://x/2" and row["ats"] == "greenhouse"
    assert c.execute("SELECT years_experience FROM postings WHERE title = 'Data Analyst'").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM events WHERE kind = 'new'").fetchone()[0] == 2
    # the CSV is the same view, without the excerpts
    with gzip.open(b.dist / "jobs.csv.gz", "rt", encoding="utf-8") as f:
        rows = list(csv.DictReader(io.StringIO(f.read())))
    assert [r["title"] for r in rows] == ["Accountant", "Data Analyst"] and "excerpt" not in rows[0]
    assert rows[1]["years_experience"] == "3"

    # a closed role stays in jobs (history) and leaves the postings view; every build replaces the files
    b.ingest("acme", [job(1)])
    m2 = b.export()
    c2 = public_db(b, tmp_path)
    assert m2["jobs"] == 1 and m2["db"]["rows"] == 2 and m2["db"]["active"] == 1
    assert [r[0] for r in c2.execute("SELECT title FROM postings")] == ["Data Analyst"]
    assert c2.execute("SELECT COUNT(*) FROM excerpts").fetchone()[0] == 1


def test_builder_applies_board_list_and_closes_switched_off_boards(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1)])
    b.ingest("beta", [job(7, board="beta", company="Beta")])
    conn = db.connect(b.path)
    lst = config.load(write_cfg(tmp_path, "builder"))
    lst.boards = [x for x in lst.boards if x["slug"] == "acme"]       # maintainer dropped beta from the list
    closed = snapshot.apply_board_list(conn, lst)
    assert closed == {"beta"}
    assert conn.execute("SELECT enabled FROM boards WHERE slug='beta'").fetchone()[0] == 0
    assert conn.execute("SELECT active FROM jobs WHERE source_board='beta'").fetchone()[0] == 0
    conn.execute("UPDATE boards SET enabled=0, last_error='Switched off automatically: not found' WHERE slug='acme'")
    conn.commit()
    snapshot.apply_board_list(conn, lst)                                # a board that's gone stays off...
    assert conn.execute("SELECT enabled FROM boards WHERE slug='acme'").fetchone()[0] == 0
    snapshot.apply_board_list(conn, lst, retry_switched_off=True)      # ...until a full build retries it
    assert conn.execute("SELECT enabled FROM boards WHERE slug='acme'").fetchone()[0] == 1


def test_fetch_reads_every_listed_board(tmp_path, monkeypatch):
    fetched = []

    def fake(board, known, client, *a, **k):
        fetched.append(board["slug"])
        return [job(9, board=board["slug"])], type("S", (), {"wanted": lambda s, j: True, "dropped": set(), "body_hash": "x"})()
    monkeypatch.setattr(fetch, "_fetch_board", fake)
    fetch.run(write_cfg(tmp_path, "builder"), tmp_path / "b.db")
    assert sorted(fetched) == ["acme", "beta"]


def test_builder_can_restart_from_the_published_database(tmp_path):
    b = Builder(tmp_path)
    same = "<p>Run the regional warehouse network and its carriers. " + " ".join(f"duty{k}" for k in range(80)) + "</p>"
    twins = [Job("greenhouse", "acme", str(i), "Logistics Lead", "Acme", city, f"https://x/{i}", sanitize_html(same))
             for i, city in ((5, "Reno, NV"), (6, "Tulsa, OK"))]          # one role, listed in two cities
    b.ingest("acme", [job(1), job(2, "Accountant"), *twins])
    b.ingest("acme", [job(1), *twins])                           # 2 closes: history worth keeping
    assert b.export()["jobs"] == 2
    fresh = tmp_path / "fresh.db"
    assert snapshot.seed_builder(b.dist / snapshot.DB_NAME, fresh) == 4
    conn = db.connect(fresh)
    # with no text to compare, the two listings stay one role until both descriptions are read again
    fetch.postprocess(conn, b.cfg)
    roles = "SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_canonical = 1"
    assert conn.execute(roles).fetchone()[0] == 2
    assert [r[0] for r in conn.execute("SELECT kind FROM events WHERE uid LIKE '%:2' ORDER BY id")] == ["new", "closed"]
    first_seen = conn.execute("SELECT first_seen FROM jobs WHERE external_id = '1'").fetchone()[0]
    # no descriptions travel in the public file; the published excerpt stands in until the board is read again
    assert db.content(conn, "greenhouse:acme:1") == ""
    assert conn.execute("SELECT text FROM site_excerpts").fetchone()[0].startswith("Build dashboards")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE minhash IS NULL").fetchone()[0] == 4
    counts = db.upsert_board(conn, "greenhouse", "acme", [job(1), job(3, "Controller"), twins[0]])   # one twin's text is back
    assert counts["new"] == 1 and db.content(conn, "greenhouse:acme:1").startswith("<p>Build dashboards")
    assert conn.execute("SELECT first_seen FROM jobs WHERE external_id = '1'").fetchone()[0] == first_seen
    fetch.postprocess(conn, b.cfg)
    assert conn.execute(roles).fetchone()[0] == 3                # still one Logistics Lead (plus 1 and 3)
    db.upsert_board(conn, "greenhouse", "acme", [job(1), job(3, "Controller"), *twins])              # both are back
    fetch.postprocess(conn, b.cfg)
    assert conn.execute(roles).fetchone()[0] == 3                # now by their text, and still one role
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE minhash_of = 'seeded'").fetchone()[0] == 1   # only the closed one
    conn.close()
    m = snapshot.export(fresh, tmp_path / "again")
    assert m["db"]["rows"] == 5 and m["jobs"] == 3
