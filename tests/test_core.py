import json
from pathlib import Path

from lighthouse import db
from lighthouse.ghost import ghost_reasons
from lighthouse.sanitize import sanitize_html
from lighthouse.sources.greenhouse import GreenhouseSource
from lighthouse.web import create_app

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_sample.json").read_text())


def parsed(board="acme"):
    return [GreenhouseSource.parse(board, raw) for raw in FIXTURE["jobs"]]


def test_parse_greenhouse():
    job = parsed()[0]
    assert job.uid == "greenhouse:acme:1001"
    assert job.title == "Senior Data Analyst"
    assert job.location == "Austin, TX"
    assert job.departments == ["Analytics"]
    assert job.url.endswith("/1001")
    assert "<strong>analytics</strong>" in job.content_html


def test_sanitizer_strips_dangerous_markup():
    out = sanitize_html('<p onclick="x()">hi</p><script>bad()</script><a href="javascript:x">j</a><img src=x onerror=y>')
    assert "script" not in out and "bad()" not in out
    assert "onclick" not in out and "javascript" not in out and "<img" not in out
    assert out.startswith("<p>hi</p>")


def test_ghost_flags():
    jobs = parsed()
    assert ghost_reasons(jobs[0].title, jobs[0].posted_at, 10_000) == []
    reasons = ghost_reasons(jobs[1].title, jobs[1].posted_at, 60)
    assert any("days" in r for r in reasons) and "evergreen/pipeline title" in reasons


def test_dedupe_and_closing(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    a = parsed("acme")
    b = parsed("acme-careers")  # same company/title/location on a second board
    db.upsert_board(conn, "greenhouse", "acme", a, {})
    db.upsert_board(conn, "greenhouse", "acme-careers", b, {})
    rows = db.list_jobs(conn)
    assert len(rows) == 2                      # 4 postings -> 2 unique roles
    assert all(r["dup_count"] == 2 for r in rows)

    # Re-fetch where one posting disappeared -> marked closed, not deleted.
    counts = db.upsert_board(conn, "greenhouse", "acme", a[:1], {})
    assert counts == {"new": 0, "updated": 1, "closed": 1}
    assert db.get_job(conn, "greenhouse:acme:1002")["active"] == 0


def test_web_pages_render(tmp_path):
    path = tmp_path / "w.db"
    conn = db.connect(path)
    db.upsert_board(conn, "greenhouse", "acme", parsed(), {"greenhouse:acme:1002": ["open 600 days"]})
    conn.close()

    client = create_app(path).test_client()
    home = client.get("/").get_data(as_text=True)
    assert "Senior Data Analyst" in home and "open 600 days" in home
    assert "Senior Data Analyst" in client.get("/?hide_ghosts=1").get_data(as_text=True)
    assert "Talent Pool" not in client.get("/?hide_ghosts=1").get_data(as_text=True)

    detail = client.get("/job/greenhouse:acme:1001").get_data(as_text=True)
    assert "<strong>analytics</strong>" in detail and "alert(1)" not in detail
    assert client.get("/job/nope").status_code == 404
