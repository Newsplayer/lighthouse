import re

from lighthouse import db, fetch, config
from lighthouse.sources.greenhouse import GreenhouseSource
from lighthouse.web import create_app


def app_with_jobs(tmp_path, gh, cfg_path):
    path = tmp_path / "w.db"
    conn = db.connect(path)
    db.upsert_board(conn, "greenhouse", "acme", [GreenhouseSource.parse("acme", raw) for raw in gh["jobs"]])
    fetch.postprocess(conn, config.load(cfg_path))
    conn.close()
    app = create_app(path, cfg_path, visa_dir=tmp_path / "visa")
    return app, app.test_client(), path


def test_pages_render(tmp_path, gh, cfg_path):
    app, client, _ = app_with_jobs(tmp_path, gh, cfg_path)
    home = client.get("/").get_data(as_text=True)
    assert "Senior Data Analyst" in home
    # 3 signals (age, evergreen title, not updated) = likely ghost: hidden by default, one click shows it
    assert "Talent Pool" not in home and "1 likely ghosts hidden" in home
    shown = client.get("/?ghosts=1").get_data(as_text=True)
    assert "Talent Pool" in shown and "possible ghost" in shown
    detail = client.get("/job/greenhouse:acme:1001").get_data(as_text=True)
    assert "<strong>analytics</strong>" in detail and "alert(1)" not in detail
    assert client.get("/job/nope").status_code == 404
    for page in ("/sources", "/profile", "/claude", "/sponsorship", "/network", "/status"):
        assert client.get(page).status_code == 200, page


def test_rejects_foreign_host_and_missing_csrf(tmp_path, gh, cfg_path):
    app, client, _ = app_with_jobs(tmp_path, gh, cfg_path)
    assert client.get("/", headers={"Host": "evil.example:5000"}).status_code == 403
    assert client.post("/mark", data={"uid": "greenhouse:acme:1001", "status": "saved"}).status_code == 400


def test_mark_and_profile_flow(tmp_path, gh, cfg_path):
    app, client, path = app_with_jobs(tmp_path, gh, cfg_path)
    tok = app.config["CSRF"]
    r = client.post("/mark", data={"csrf": tok, "uid": "greenhouse:acme:1001", "status": "saved", "next": "/?view=saved"})
    assert r.status_code == 302
    assert "Senior Data Analyst" in client.get("/?view=saved").get_data(as_text=True)
    client.post("/mark", data={"csrf": tok, "uid": "greenhouse:acme:1001", "status": "hidden"})
    assert "Senior Data Analyst" not in client.get("/").get_data(as_text=True)

    client.post("/profile", data={"csrf": tok, "about": "Analytics roles", "years_experience": "1",
                                  "locations": "Denver", "workplaces": ["onsite", "hybrid"]})
    client.post("/profile", data={"csrf": tok, "about": "Analytics roles", "years_experience": "1",
                                  "locations": "Denver", "workplaces": ["onsite", "hybrid"], "countries": ["US", "CA"]})
    listing = client.get("/?yoe_lo=1&yoe_hi=6&sort=yoe").get_data(as_text=True)
    assert "in United States, Canada" in listing and "Years asked" in listing
    page = client.get("/?view=hidden&elig=0").get_data(as_text=True)
    assert "not one of your locations" in page
    prof = client.get("/profile").get_data(as_text=True)
    assert "What Claude would receive" in prof and "cache_control" in prof


def test_add_board_validates(tmp_path, gh, cfg_path, monkeypatch):
    app, client, path = app_with_jobs(tmp_path, gh, cfg_path)
    from lighthouse import boards
    monkeypatch.setattr(boards, "validate", lambda ats, slug: 3)
    monkeypatch.setattr(fetch, "run", lambda **kw: None)
    r = client.post("/sources/add", data={"csrf": app.config["CSRF"], "urls": "https://jobs.lever.co/palantir\nnot a url"},
                    follow_redirects=True)
    text = r.get_data(as_text=True)
    assert "lever:palantir" in text and "3 postings" in text and "Not a URL" in text
    conn = db.connect(path)
    assert ("lever", "palantir") in {(b["ats"], b["slug"]) for b in db.list_boards(conn)}
