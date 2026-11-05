"""Shared job index: the builder publishes a ready-to-use base + small deltas; readers use the base as it
comes (no re-processing) and apply deltas with SQL. Personal data lives apart, in user.db."""
import json
import lzma
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest

from lighthouse import config, db, fetch, net, profile, snapshot
from lighthouse.models import Job
from lighthouse.sanitize import sanitize_html

from conftest import FakeResp

REPO = "maintainer/lighthouse"


def job(eid, title="Data Analyst", board="acme", company="Acme", body=None):
    words = " ".join(f"topic{eid}x{k}" for k in range(80))   # distinct text: no accidental clustering
    body = body if body is not None else (f"<p>Build dashboards with SQL. 3+ years of experience. {words}</p>"
                                          "<p>Acme is an equal opportunity employer and values a diverse workforce.</p>")
    return Job("greenhouse", board, str(eid), title, company, f"Austin {eid}, TX", f"https://x/{eid}", sanitize_html(body),
               posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


def write_cfg(tmp_path, name, repo=""):
    p = tmp_path / f"{name}.toml"
    p.write_text('[settings]\nfetch_interval_hours = 0\n\n[[boards]]\nats = "greenhouse"\nslug = "acme"\n\n'
                 '[[boards]]\nats = "greenhouse"\nslug = "beta"\n' + (f'\n[index]\nrepo = "{repo}"\n' if repo else ""))
    return p


class Builder:
    """The GitHub Action's side: a builder DB plus export()."""

    def __init__(self, tmp_path):
        self.tmp, self.path = tmp_path, tmp_path / "builder.db"
        self.cfg = config.load(write_cfg(tmp_path, "builder"))
        self.published = None
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
        m = snapshot.export(self.path, out, published=self.published, **kw)
        self.published = out / snapshot.MANIFEST
        self.dist = out
        return m


def base_file(b):
    found = list(b.dist.glob("base-*.db.xz"))
    return found[0] if found else b.dist / "base-missing.db.xz"


def unpacked(path, tmp_path):
    out = tmp_path / (path.name + ".db")
    with lzma.open(path) as f, open(out, "wb") as o:
        shutil.copyfileobj(f, o)
    return out


def reader(tmp_path):
    cfg = config.load(write_cfg(tmp_path, "reader", REPO))
    return db.connect(db.app_db(cfg, tmp_path / "data")), cfg


def test_export_is_whitelisted_and_compact(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1), job(2, "Accountant")])
    conn = db.connect(b.path)   # personal data a careless build machine might hold
    uid = conn.execute("SELECT uid FROM jobs LIMIT 1").fetchone()[0]
    db.set_mark(conn, uid, "applied")
    p = profile.load(conn)
    p.update(resume="Jane Doe jane@example.com", years_experience=4)
    profile.save(conn, p)
    conn.execute("INSERT INTO api_usage(at, purpose, model, input_tokens, output_tokens, cost_usd) VALUES ('x','fit','m',1,1,.1)")
    conn.commit()
    conn.close()
    m = b.export()
    assert m["base"]["rows"] == 2 and m["delta"] is None and (base_file(b)).exists()
    path = unpacked(base_file(b), tmp_path)
    assert b"jane@example.com" not in path.read_bytes()
    c = sqlite3.connect(path)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"marks", "fit", "api_usage", "claude_batches", "meta", "sponsor_match", "claude_extractions"}
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    assert not cols & {"minhash", "minhash_of", "elig", "elig_reasons"}
    assert c.execute("SELECT COUNT(*) FROM jobs WHERE mark IS NOT NULL").fetchone()[0] == 0
    assert all(isinstance(r[0], bytes) for r in c.execute("SELECT html FROM job_content"))   # compressed per row...
    assert c.execute("SELECT COUNT(*) FROM zdict").fetchone()[0] == 1                       # ...against one dictionary
    # ready to use: search index and list indexes are already in the file
    assert "jobs_fts" in tables and c.execute("SELECT COUNT(*) FROM jobs WHERE fts_rowid IS NOT NULL").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM jobs_fts WHERE jobs_fts MATCH '\"dashboards\"*'").fetchone()[0] == 2
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='idx_canon_eff'").fetchone()
    assert c.execute("PRAGMA application_id").fetchone()[0] == db.INDEX_APP_ID


def test_base_then_cumulative_deltas(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(i) for i in range(1, 6)])
    m1 = b.export()
    assert m1["base"]["rows"] == 5
    b.ingest("acme", [job(i) for i in range(1, 5)] + [job(6, "Controller")])     # 5 closed, 6 new
    m2 = b.export()
    assert not (base_file(b)).exists(), "base must not be re-uploaded"
    assert m2["base"] == m1["base"] and m2["delta"]["base_built_at"] == m1["base"]["built_at"]
    assert m2["delta"]["rows"] == 2
    m3 = b.export()   # nothing changed: the delta still carries every change since the base
    assert m3["delta"]["rows"] == 2
    b.published = tmp_path / "missing.json"   # published index doesn't match the builder: start over
    assert b.export()["delta"] is None and (base_file(b)).exists()
    assert b.export(full=True)["delta"] is None


def test_reader_merges_base_and_delta_keeping_personal_data(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1), job(2, "Accountant"), job(3, "Data Engineer")])
    m1 = b.export()
    base = base_file(b)
    conn, cfg = reader(tmp_path)
    assert snapshot.import_file(conn, cfg, base, full=True)["postings"] == 3
    assert db.list_jobs(conn, db.Filters(q="dashboards"))[1] == 3       # search index came with the file
    uid = conn.execute("SELECT uid FROM jobs WHERE title='Data Analyst'").fetchone()[0]
    db.set_mark(conn, uid, "saved")
    ch = conn.execute("SELECT content_hash FROM jobs WHERE uid=?", (uid,)).fetchone()[0]
    conn.execute("INSERT INTO claude_extractions(content_hash, data, model, at) VALUES (?, ?, 'm', 'x')",
                 (ch, json.dumps({"yoe_min": {"value": 9}})))
    conn.commit()
    from lighthouse import extract
    extract.derive_claude(conn)   # what the app does after a Claude extraction
    assert conn.execute("SELECT name FROM pragma_database_list WHERE name='idx'").fetchone()   # two files
    b.ingest("acme", [job(1), job(2, "Accountant"), job(4, "Controller")])
    m2 = b.export()
    stats = snapshot.import_file(conn, cfg, next(b.dist.glob('delta-*.db.xz')), full=False)
    assert stats["postings"] == 2 and stats["new"] == 1
    assert conn.execute("SELECT active FROM jobs WHERE title='Data Engineer'").fetchone()[0] == 0
    assert conn.execute("SELECT mark FROM jobs WHERE uid=?", (uid,)).fetchone()[0] == db.MARK_RANK["saved"]
    assert conn.execute("SELECT COUNT(*) FROM claude_extractions WHERE content_hash=?", (ch,)).fetchone()[0] == 1
    assert conn.execute("SELECT yoe_min FROM jobs WHERE uid=?", (uid,)).fetchone()[0] == 9   # your Claude result wins
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 3
    assert db.list_jobs(conn, db.Filters(q="Controller"))[1] == 1       # delta rows are searchable
    assert db.list_jobs(conn, db.Filters(q="Data Engineer"))[1] == 0    # closed ones drop out
    snapshot.import_file(conn, cfg, base, full=True)   # a new base replaces the index: your layer is re-applied
    assert conn.execute("SELECT yoe_min FROM jobs WHERE uid=?", (uid,)).fetchone()[0] == 9
    assert conn.execute("SELECT mark FROM jobs WHERE uid=?", (uid,)).fetchone()[0] == db.MARK_RANK["saved"]
    data = tmp_path / "data"
    assert len(list((data / "index").glob("index-2*.db"))) == 1   # the replaced index file is gone


def test_tampered_index_is_harmless_when_shown(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1)])
    b.export()
    path = unpacked(base_file(b), tmp_path)
    c = sqlite3.connect(path)
    c.execute("UPDATE job_content SET html = ?", (db.pack('<p>Hi</p><script>steal()</script><img src=x onerror=alert(1)>'),))
    c.execute("UPDATE jobs SET url = 'javascript:alert(1)'")
    c.commit()
    c.close()
    conn, cfg = reader(tmp_path)
    snapshot.import_file(conn, cfg, path)
    uid = conn.execute("SELECT uid FROM jobs").fetchone()[0]
    conn.close()
    from lighthouse.web import create_app
    app = create_app(db_path=tmp_path / "data" / "user.db", config_path=write_cfg(tmp_path, "reader", REPO))
    client = app.test_client()
    page = client.get(f"/job/{uid}").get_data(as_text=True)
    assert "<p>Hi</p>" in page and "steal()" not in page and "onerror" not in page and "javascript:" not in page
    assert "javascript:" not in client.get("/").get_data(as_text=True)


def test_new_base_removes_dropped_boards(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1)])
    b.ingest("beta", [job(7, board="beta", company="Beta")])
    b.export()
    conn, cfg = reader(tmp_path)
    snapshot.import_file(conn, cfg, base_file(b))
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 2
    bc = db.connect(b.path)   # maintainer removes the beta board
    bc.execute("DELETE FROM boards WHERE slug='beta'")
    bc.execute("DELETE FROM jobs WHERE source_board='beta'")
    bc.commit()
    bc.close()
    b.export(full=True)
    snapshot.import_file(conn, cfg, base_file(b), full=True)
    assert [r[0] for r in conn.execute("SELECT slug FROM boards")] == ["acme"]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source_board='beta'").fetchone()[0] == 0


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


class GitHubFake:
    """github.com release downloads, redirected to the file host like the real thing."""

    def __init__(self, dist_dirs, file_host="release-assets.githubusercontent.com"):
        self.files, self.file_host, self.calls = {}, file_host, []
        for d in dist_dirs:
            self.publish(d)

    def publish(self, d):
        for p in d.iterdir():
            self.files[p.name] = p.read_bytes()

    def get(self, url, headers=None, allow_redirects=True, stream=False, **kw):
        assert allow_redirects is False
        host = url.split("/")[2]
        if not net.host_allowed(host):
            raise net.BlockedHost(host)
        self.calls.append(url)
        name = url.rsplit("/", 1)[1]
        if host == "github.com":
            assert f"/{REPO}/releases/download/{snapshot.TAG}/" in url
            if name not in self.files:
                return FakeResp({}, 404)
            r = FakeResp({}, 302)
            r.headers = {"Location": f"https://{self.file_host}/x/{name}"}
            return r
        data = self.files[name]
        r = FakeResp(json.loads(data) if name.endswith(".json") else {})
        r.iter_content = lambda n: [data[i:i + n] for i in range(0, len(data), n)]
        return r


def kinds(gh):
    return [n.split("-")[0] for n in downloads(gh)]


def downloads(gh):
    return [u.rsplit("/", 1)[1] for u in gh.calls if "githubusercontent" in u and not u.endswith(".json")]


def test_sync_downloads_base_once_then_only_deltas(tmp_path):
    b = Builder(tmp_path)
    ten = [job(i) for i in range(1, 11)]
    b.ingest("acme", ten)
    b.export()
    first = b.dist
    b.ingest("acme", ten + [job(11, "Controller")])
    b.export()
    gh = GitHubFake([first, b.dist])
    conn, cfg = reader(tmp_path)
    data = tmp_path / "data"
    r = snapshot.sync(conn, cfg, gh)
    assert r["error"] is None and kinds(gh) == ["base", "delta"]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 11
    gh.calls.clear()
    snapshot.sync(conn, cfg, gh)       # nothing new: only the manifest is read
    assert downloads(gh) == []
    b.ingest("acme", ten[1:] + [job(11, "Controller"), job(12, "Auditor")])   # 1 closed, 12 new
    b.export()
    gh.publish(b.dist)
    gh.calls.clear()
    snapshot.sync(conn, cfg, gh)
    assert kinds(gh) == ["delta"]
    active = {r[0] for r in conn.execute("SELECT external_id FROM jobs WHERE active=1")}
    assert active == {str(i) for i in range(2, 13)}
    assert snapshot.status(conn, cfg)["built_at"] == json.loads(gh.files["manifest.json"])["delta"]["built_at"]
    assert not list((data / "index").glob("download-*"))   # temp files cleaned up
    assert db.list_jobs(conn, db.Filters(q="Auditor"))[1] == 1


@pytest.mark.parametrize("problem", ["checksum", "evil_redirect", "newer_schema", "not_published"])
def test_sync_failure_keeps_existing_jobs(tmp_path, problem):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1)])
    b.export()
    conn, cfg = reader(tmp_path)
    snapshot.import_file(conn, cfg, base_file(b))
    gh = GitHubFake([b.dist], file_host="evil.example.com" if problem == "evil_redirect" else
                    "release-assets.githubusercontent.com")
    m = json.loads(gh.files["manifest.json"])
    m["base"]["built_at"] = "2099-01-01T00:00:00+00:00"    # pretend it's a new base
    if problem == "checksum":
        m["base"]["sha256"] = "0" * 64
    if problem == "newer_schema":
        m["index_schema"] += 1
    gh.files["manifest.json"] = json.dumps(m).encode()
    if problem == "not_published":
        gh.files.clear()
    r = snapshot.sync(conn, cfg, gh)
    assert r["error"]
    if problem == "newer_schema":
        assert "newer Lighthouse" in r["error"]
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 1


def test_reader_never_fetches_boards(tmp_path, monkeypatch):
    cfg_path = write_cfg(tmp_path, "reader", REPO)
    synced = []
    monkeypatch.setattr(snapshot, "sync", lambda conn, cfg, client, folder=None: synced.append(1) or {"error": None})
    monkeypatch.setattr(fetch, "_fetch_board", lambda *a, **k: pytest.fail("a reader must not fetch boards"))
    fetch.run(cfg_path, tmp_path / "r.db")
    assert synced == [1]
    assert fetch.run(cfg_path, tmp_path / "r.db", only=[("greenhouse", "acme")]) == []
    conn = db.connect(tmp_path / "r.db")
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0   # local board lists ignored


def test_builder_mode_fetches(tmp_path, monkeypatch):
    cfg_path = write_cfg(tmp_path, "reader", REPO)
    fetched = []

    def fake(board, known, client, *a, **k):
        fetched.append(board["slug"])
        return [job(9, board=board["slug"])], type("S", (), {"wanted": lambda s, j: True, "dropped": set(), "body_hash": "x"})()
    monkeypatch.setattr(fetch, "_fetch_board", fake)
    fetch.run(cfg_path, tmp_path / "b.db", use_snapshot=False)
    assert sorted(fetched) == ["acme", "beta"]


def test_sources_page_is_read_only_for_readers(tmp_path):
    from lighthouse.web import create_app
    app = create_app(db_path=tmp_path / "w.db", config_path=write_cfg(tmp_path, "reader", REPO))
    client = app.test_client()
    html = client.get("/sources").get_data(as_text=True)
    assert "Suggest a board" in html and f"github.com/{REPO}/issues/new" in html and "Add boards" not in html
    assert "Check for new jobs" in html
    assert client.post("/sources/add", data={"csrf": app.config["CSRF"], "urls": "https://jobs.lever.co/x"}).status_code == 403


def test_builder_can_restart_from_the_published_base(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [job(1), job(2, "Accountant")])
    b.export()
    fresh = tmp_path / "fresh.db"
    assert snapshot.seed_builder(base_file(b), fresh) == 2
    conn = db.connect(fresh)
    assert db.content(conn, "greenhouse:acme:1").startswith("<p>Build dashboards")   # stored compressed again
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE minhash IS NULL").fetchone()[0] == 2
    db.upsert_board(conn, "greenhouse", "acme", [job(1), job(2, "Accountant"), job(3, "Controller")])
    fetch.postprocess(conn, b.cfg)
    conn.close()
    m = snapshot.export(fresh, tmp_path / "again", full=True)
    assert m["base"]["rows"] == 3
