"""The job database's lifecycle (new, closed, held, deleted -> readers delete too), the filter fields read
from descriptions, and Claude filling only the gaps the rules couldn't settle (builder only)."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from lighthouse import claude_api, classify, config, db, extract, fetch, llm_fill, snapshot
from lighthouse.models import Job
from lighthouse.sanitize import sanitize_html

from conftest import FakeResp
from test_index import Builder, base_file, reader, unpacked

FILLER = " ".join(f"word{k}" for k in range(60))


def posting(eid, title, body, board="acme", company="Acme", posted_at=None):
    return Job("greenhouse", board, str(eid), title, company, "Austin, TX", f"https://x/{eid}",
               sanitize_html(f"<p>{body}</p><p>{FILLER} unique{eid}</p>"), posted_at=posted_at)


def cfg_file(tmp_path, extra=""):
    p = tmp_path / "lc.toml"
    p.write_text('[settings]\nfetch_interval_hours = 0\n\n[[boards]]\nats = "greenhouse"\nslug = "acme"\n' + extra)
    return config.load(p), p


# ---------------------------------------------------------------- rules: the new fields

def test_benefits_travel_languages_rules():
    b = classify.benefits("We offer medical, dental and vision insurance, a 401(k) with match, and RSUs. "
                          "Unlimited PTO. Annual bonus eligible. Paid parental leave. Tuition reimbursement.")
    assert b == ["health", "retirement", "equity", "bonus", "unlimited_pto", "parental_leave", "learning"]
    assert "equity" not in classify.benefits("We are committed to diversity, equity and inclusion. Pay equity matters.")
    assert classify.benefits("Relocation assistance is not available for this role.") == []
    assert classify.benefits("Relocation assistance is available.") == ["relocation"]
    assert classify.benefits("This role is not eligible for an annual bonus.") == []
    # benefits named as the work, not offered as perks
    assert classify.benefits("Experience with equity compensation matters. Experience managing pension plans. "
                             "Knowledge of the Health Insurance Portability and Accountability Act. "
                             "Incentive Compensation Management (ICM) tools.") == []
    assert classify.benefits("Additional benefits for this role may include: equity, company bonus or sales "
                             "commissions/bonuses; 401(k) plan; medical, dental, and vision benefits.") == \
        ["health", "retirement", "equity", "bonus"]
    assert classify.benefits("Optional equity donation matching.") == []

    assert classify.travel("Travel up to 25% of the time.")[0] == "occasional"
    assert classify.travel("This role requires 50-75% travel to client sites.")[0] == "frequent"
    assert classify.travel("No travel required.")[0] == "none"
    assert classify.travel("Extensive international travel.")[0] == "frequent"
    assert classify.travel("We reimburse travel to the office.") is None

    assert classify.languages("Fluency in German and French is required.")[0] == ["german", "french"]
    assert classify.languages("Spanish language skills are a plus.") is None
    assert classify.languages("You will support Spanish-speaking customers.") is None
    assert classify.languages("Native Mandarin Chinese speaker.")[0] == ["mandarin"]
    assert classify.languages("Our German office is in Berlin.") is None

    r = extract.rules("Benefits: health insurance and stock options.\nTravel: up to 10%.\n"
                      "Business-level Japanese required.")
    assert r["benefits"]["value"] == ["health", "equity"] and r["travel"]["value"] == "occasional"
    assert r["languages"]["value"] == ["japanese"] and "Japanese" in r["languages"]["evidence"]


def test_unresolved_only_where_the_posting_talks_about_it():
    text = "Applicants must be authorized to work in the U.S. Experience: five plus years in the field."
    data = extract.rules(text)
    todo = extract.unresolved(text, data, "Data Analyst")
    assert "visa" in todo and "yoe_min" in todo and "salary" not in todo and "job_function" not in todo
    settled = "We cannot sponsor visas. 3+ years of experience required."
    assert extract.unresolved(settled, extract.rules(settled), "Data Analyst") == []
    plain = "Build dashboards with the team."
    assert extract.unresolved(plain, extract.rules(plain), "Data Analyst") == []
    assert extract.unresolved(plain, extract.rules(plain), "Wrangler of Llamas") == ["job_function"]
    # the ATS already gave pay: not a gap
    pay = "Base salary range: 120,000 to 150,000 depending on level."
    assert "salary" in extract.unresolved(pay, extract.rules(pay), "Data Analyst")
    assert "salary" not in extract.unresolved(pay, extract.rules(pay), "Data Analyst", ats={"salary": True})


def test_merge_never_overrides_a_rule_answer():
    rules = {"yoe_min": {"value": 3, "evidence": "3+ years"}, "visa": {"value": None, "evidence": None}}
    llm = {"yoe_min": {"value": 5, "evidence": "5 years"}, "visa": {"value": "no", "evidence": "without sponsorship"}}
    m = extract.merge_llm(rules, llm)
    assert m["yoe_min"] == {"value": 3, "evidence": "3+ years"}
    assert m["visa"] == {"value": "no", "evidence": "without sponsorship", "by": "claude"}


# ---------------------------------------------------------------- Claude gap fill on the builder

class BatchAPI:
    """The Message Batches endpoints: records what was sent, answers each request with answer(params)."""

    def __init__(self, answer):
        self.answer, self.sent, self.batches = answer, [], {}
        from collections import Counter
        self.counts, self.blocked = Counter(), Counter()

    def post(self, url, json=None, **kw):
        assert url.endswith("/messages/batches")
        bid = f"msgbatch_{len(self.batches) + 1}"
        self.batches[bid] = json["requests"]
        self.sent += json["requests"]
        return FakeResp({"id": bid, "processing_status": "in_progress"})

    def get(self, url, **kw):
        bid = url.split("/messages/batches/")[1].split("/")[0]
        if url.endswith("/results"):
            lines = [{"custom_id": r["custom_id"], "result": {"type": "succeeded", "message": {
                "content": [{"type": "text", "text": json_dumps(self.answer(r["params"]))}],
                "usage": {"input_tokens": 2500, "output_tokens": 200}}}} for r in self.batches[bid]]
            return FakeResp(None, text="\n".join(json_dumps(x) for x in lines))
        return FakeResp({"id": bid, "processing_status": "ended",
                         "results_url": f"https://api.anthropic.com/v1/messages/batches/{bid}/results"})


def json_dumps(x):
    return json.dumps(x)


def claude_says(params):
    text = params["messages"][0]["content"]
    out = {k: None for k in claude_api.EXTRACT_SCHEMA["required"]}
    out.update(benefits=[], languages=[], evidence={k: "" for k in claude_api._EVIDENCE_KEYS})
    if "without the need for employer support" in text:
        out.update(visa_sponsorship="no", yoe_min=5)   # 5 contradicts the rules' 3: must not win
        out["evidence"]["visa"] = "without the need for employer support"
    if "Llamas" in text:
        out.update(job_function="Operations & Supply Chain", seniority="mid")
    return out


@pytest.fixture
def builder_db(tmp_path, monkeypatch):
    cfg, _ = cfg_file(tmp_path)
    conn = db.connect(tmp_path / "b.db")
    db.sync_config_boards(conn, cfg.boards)
    db.upsert_board(conn, "greenhouse", "acme", [
        posting(1, "Data Analyst", "Candidates must be authorized to work in the U.S. now and in the future "
                                   "without the need for employer support. 3+ years of experience."),
        posting(2, "Data Engineer", "Build pipelines. 2+ years of experience."),
        posting(3, "Wrangler of Llamas", "Keep the herd happy."),
    ])
    fetch.postprocess(conn, cfg)
    monkeypatch.setattr(llm_fill, "POLL_SECONDS", 0)
    return conn, cfg


def test_gap_fill_sends_only_unsettled_postings_and_keeps_rule_answers(builder_db, monkeypatch):
    conn, cfg = builder_db
    api = BatchAPI(claude_says)
    monkeypatch.setattr(claude_api, "default_client", lambda: api)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stats = llm_fill.run(conn, cfg, wait_minutes=1)
    hashes = {r[0]: r[1] for r in conn.execute("SELECT external_id, content_hash FROM jobs")}
    assert {r["custom_id"] for r in api.sent} == {hashes["1"], hashes["3"]}       # 2 had nothing unsettled
    sent = api.sent[0]["params"]
    assert sent["model"] == "claude-haiku-5-5" and sent["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in sent and stats["sent"] == 2 and stats["merged"] == 2 and stats["pending"] == 0
    row = lambda eid: conn.execute("SELECT * FROM jobs WHERE external_id=?", (eid,)).fetchone()
    assert row("1")["visa"] == "no" and row("1")["yoe_min"] == 3                    # gap filled; rule answer kept
    assert row("3")["job_function"] == "Operations & Supply Chain" and row("3")["seniority"] == "mid"
    data, method = extract.get(conn, hashes["1"])
    assert method == "rules" and data["visa"]["by"] == "claude" and "by" not in data["yoe_min"]
    assert db.list_jobs(conn, db.Filters(visa="not_no"))[1] == 2
    assert claude_api.month_spend(conn) > 0 and claude_api.reserved(conn) == 0
    again = llm_fill.run(conn, cfg, wait_minutes=0)                                # read once per description
    assert again["sent"] == 0 and len(api.sent) == 2
    # new rules (e.g. after a RULES_VERSION bump) keep Claude's fills
    conn.execute("UPDATE extractions SET model = 'old'")
    conn.commit()
    fetch.precompute(conn)
    assert extract.get(conn, hashes["1"])[0]["visa"]["value"] == "no"


def test_gap_fill_respects_the_cap_and_needs_a_key(builder_db, monkeypatch):
    conn, cfg = builder_db
    api = BatchAPI(claude_says)
    monkeypatch.setattr(claude_api, "default_client", lambda: api)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(claude_api, "get_key", lambda: None)
    assert "no ANTHROPIC_API_KEY" in llm_fill.run(conn, cfg)["skipped"] and api.sent == []
    monkeypatch.setattr(claude_api, "get_key", lambda: "sk-test")
    capped = llm_fill.run(conn, cfg, cap_usd=0.0000001, wait_minutes=0)
    assert capped["sent"] == 0 and capped["over_budget"] == 2 and api.sent == []
    one = claude_api.estimate(cfg, "claude-haiku-5-5", claude_api.extract_params(cfg, llm_fill.candidates(conn, 1, 3)[0],
                                                                                  "claude-haiku-5-5"), True)
    assert llm_fill.run(conn, cfg, cap_usd=one * 1.5, wait_minutes=0)["sent"] == 1


def test_gap_fills_ship_in_the_index_and_survive_a_lost_cache(tmp_path, monkeypatch):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Must be authorized to work in the U.S. without the need for "
                                                 "employer support.")])
    conn = db.connect(b.path)
    monkeypatch.setattr(llm_fill, "POLL_SECONDS", 0)
    api = BatchAPI(claude_says)
    monkeypatch.setattr(claude_api, "default_client", lambda: api)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm_fill.run(conn, b.cfg, wait_minutes=1)
    conn.close()
    m = b.export()
    assert m["llm"]["descriptions_read"] == 1 and m["llm"]["spent_usd"] > 0
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, base_file(b), full=True)
    assert db.list_jobs(r, db.Filters(visa="not_no"))[1] == 0
    ch = r.execute("SELECT content_hash FROM jobs").fetchone()[0]
    assert extract.get(r, ch)[0]["visa"]["by"] == "claude"
    # Actions cache expired: a new builder seeded from the published base knows it was already asked
    seeded = tmp_path / "seeded.db"
    snapshot.seed_builder(base_file(b), seeded)
    s = db.connect(seeded)
    assert s.execute("SELECT model FROM llm_extractions WHERE content_hash=?", (ch,)).fetchone()[0] == "seeded"
    llm_fill.carry_spend(s, m)
    assert claude_api.month_spend(s) == pytest.approx(m["llm"]["spent_usd"], abs=1e-4)


def test_a_description_claude_found_nothing_in_is_not_paid_for_again(tmp_path, monkeypatch):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Must be authorized to work in the U.S. without the need for "
                                                 "employer support.")])
    conn = db.connect(b.path)
    monkeypatch.setattr(llm_fill, "POLL_SECONDS", 0)
    api = BatchAPI(lambda params: {})                         # Claude read it and had nothing to add
    monkeypatch.setattr(claude_api, "default_client", lambda: api)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm_fill.run(conn, b.cfg, wait_minutes=1)
    ch = conn.execute("SELECT content_hash FROM jobs").fetchone()[0]
    assert "claude_read" in json.loads(conn.execute("SELECT data FROM extractions").fetchone()[0])
    conn.close()
    b.export(full=True)
    seeded = tmp_path / "seeded.db"
    snapshot.seed_builder(base_file(b), seeded)               # the Actions cache is gone
    s = db.connect(seeded)
    assert s.execute("SELECT data FROM llm_extractions WHERE content_hash=?", (ch,)).fetchone()[0] == "{}"
    assert llm_fill.candidates(s, 100, 99) == []              # nothing to send again
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, base_file(b), full=True)
    data = extract.get(r, ch)[0]
    assert not [v for v in data.values() if isinstance(v, dict) and v.get("by")]   # the job page shows no fills


# ---------------------------------------------------------------- lifecycle

def test_builder_reprocesses_everything_once_after_an_upgrade(tmp_path, monkeypatch):
    """New rules or filter fields must reach companies whose boards didn't change, but only once."""
    _, cfgp = cfg_file(tmp_path, '\n[[boards]]\nats = "greenhouse"\nslug = "beta"\n')
    path = tmp_path / "b.db"
    boards = {"acme": [posting(1, "Data Analyst", "Stock options offered.")],
              "beta": [posting(2, "Accountant", "Ledgers.", board="beta", company="Beta")]}
    stub = type("S", (), {"wanted": lambda s, j: True, "dropped": set(), "body_hash": "x"})()
    monkeypatch.setattr(fetch, "_fetch_board", lambda board, *a, **k: (boards[board["slug"]], stub))
    fetch.run(cfgp, path, use_snapshot=False)
    conn = db.connect(path)
    assert db.get_meta(conn, "processed_versions") == fetch.processed_versions()
    # as an older version left it: acme's row derived without the new fields
    conn.execute("UPDATE jobs SET benefits = NULL, derived_of = 'old' WHERE source_board = 'acme'")
    db.set_meta(conn, "processed_versions", {"rules": "rules-v4", "derive": "d1", "schema": 5})
    conn.close()
    monkeypatch.setattr(fetch, "_fetch_board", lambda board, *a, **k: (None, stub))   # nothing changed anywhere
    monkeypatch.setattr(db, "board_unchanged", lambda *a, **k: None)
    fetch.run(cfgp, path, use_snapshot=False)
    conn = db.connect(path)
    assert classify.decode_list(conn.execute("SELECT benefits FROM jobs WHERE source_board='acme'").fetchone()[0]) == ["equity"]
    calls = []
    real = fetch.postprocess
    monkeypatch.setattr(fetch, "postprocess", lambda c, cfg, keys=None: (calls.append(keys), real(c, cfg, keys)))
    fetch.run(cfgp, path, use_snapshot=False)
    assert calls == [set()]          # up to date: only what changed (nothing) is reprocessed

def test_a_sudden_mass_disappearance_waits_for_confirmation(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    db.add_board(conn, "greenhouse", "acme")
    many = [posting(i, f"Analyst {i}", "Work.") for i in range(30)]
    db.upsert_board(conn, "greenhouse", "acme", many)
    glitch = db.upsert_board(conn, "greenhouse", "acme", many[:3])        # API hiccup: 3 of 30 listed
    assert glitch["held"] == 27 and glitch["closed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 30
    back = db.upsert_board(conn, "greenhouse", "acme", many)               # next run: all back, nothing lost
    assert back["closed"] == 0 and back["held"] == 0
    db.upsert_board(conn, "greenhouse", "acme", many[:3])                  # held again...
    real = db.upsert_board(conn, "greenhouse", "acme", many[:3])           # ...and confirmed: they really closed
    assert real["closed"] == 27 and conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0] == 3
    small = db.upsert_board(conn, "greenhouse", "acme", [])                # below 20 open: no hold
    assert small["closed"] == 3


def test_long_closed_postings_are_deleted_and_readers_follow(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Dashboards."), posting(2, "Accountant", "Ledgers.")])
    b.export()
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, base_file(b), full=True)
    b.ingest("acme", [posting(1, "Data Analyst", "Dashboards.")])         # 2 is no longer listed: closed
    b.export()
    snapshot.import_file(r, rcfg, next(b.dist.glob("delta-*.db.xz")), full=False)
    assert r.execute("SELECT active FROM jobs WHERE external_id='2'").fetchone()[0] == 0
    conn = db.connect(b.path)
    assert db.purge_closed(conn, 45) == 0                                  # closed today: kept for now
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET closed_at=? WHERE external_id='2'", (old,))
    conn.commit()
    assert db.purge_closed(conn, 45) == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE external_id='2'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 1
    conn.close()
    m = b.export()
    assert m["deleted_this_run"] == 1 and m["deltas"][-1]["rows"] == 0
    stats = snapshot.import_file(r, rcfg, sorted(b.dist.glob("delta-*.db.xz"))[-1], full=False)
    assert stats["deleted"] == 1
    assert r.execute("SELECT COUNT(*) FROM jobs WHERE external_id='2'").fetchone()[0] == 0
    assert r.execute("SELECT COUNT(*) FROM job_content WHERE uid LIKE '%:2'").fetchone()[0] == 0
    assert db.list_jobs(r, db.Filters())[1] == 1


def test_closed_rows_of_an_open_role_are_kept_for_repost_history(tmp_path):
    conn = db.connect(tmp_path / "h.db")
    body = "Same role. " + FILLER
    db.upsert_board(conn, "greenhouse", "acme", [Job("greenhouse", "acme", "1", "Data Analyst", "Acme", "Austin, TX",
                                                     "https://x/1", sanitize_html(f"<p>{body}</p>"))])
    db.upsert_board(conn, "greenhouse", "acme", [Job("greenhouse", "acme", "2", "Data Analyst", "Acme", "Austin, TX",
                                                     "https://x/2", sanitize_html(f"<p>{body}</p>"))])
    fetch.postprocess(conn, config.load(cfg_file(tmp_path)[1]))
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET closed_at=? WHERE external_id='1'", (old,))
    conn.commit()
    assert db.purge_closed(conn, 45) == 0       # its repost (2) is still open
    db.set_mark(conn, "greenhouse:acme:1", "applied")
    conn.execute("UPDATE jobs SET active=0, closed_at=? WHERE external_id='2'", (old,))
    conn.commit()
    assert db.purge_closed(conn, 45) == 1       # role gone; the posting you applied to stays
    assert conn.execute("SELECT uid FROM jobs").fetchall()[0][0] == "greenhouse:acme:1"


# ---------------------------------------------------------------- filters

def test_new_filters(tmp_path):
    cfg, _ = cfg_file(tmp_path)
    conn = db.connect(tmp_path / "f.db")
    rich = ("Salary: $120,000 - $150,000 per year. We offer a 401(k) match, stock options and unlimited PTO. "
            "Travel up to 50%. Fluency in German is required. Bachelor's degree required. "
            "Active Secret clearance required. Must be a U.S. citizen.")
    old = datetime.now(timezone.utc) - timedelta(days=40)
    db.upsert_board(conn, "greenhouse", "acme", [posting(1, "Data Analyst", rich),
                                                 posting(2, "Data Engineer", "Build pipelines.", posted_at=old)])
    fetch.postprocess(conn, cfg)
    titles = lambda **kw: sorted(r["title"] for r in db.list_jobs(conn, db.Filters(**kw))[0])
    A, E = "Data Analyst", "Data Engineer"
    assert titles(pay_listed=True) == [A]
    assert titles(benefits=("equity", "unlimited_pto")) == [A] and titles(benefits=("parental_leave",)) == []
    assert titles(travel="low") == [E]
    assert titles(language="german") == [A] and titles(language="english") == [E]
    assert titles(degree_max="none") == [E] and titles(degree_max="bachelors") == [A, E]
    assert titles(clearance="none") == [E] and titles(clearance="required") == [A]
    assert titles(no_citizenship=True) == [E]
    assert titles(posted_within=30) == [A] and titles(posted_within=60) == [A, E]
    row = conn.execute("SELECT benefits, travel, languages FROM jobs WHERE external_id='1'").fetchone()
    assert classify.decode_list(row[0]) == ["retirement", "equity", "unlimited_pto"]
    assert row[1] == "frequent" and classify.decode_list(row[2]) == ["german"]


def test_an_older_index_gains_the_new_columns(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Dashboards.")])
    b.export()
    path = unpacked(base_file(b), tmp_path)
    c = sqlite3.connect(path)
    c.execute("DROP VIEW postings")                     # what a 0.6 builder published: no view,
    for col in ("benefits", "travel", "languages"):    # none of these columns
        c.execute(f"ALTER TABLE jobs DROP COLUMN {col}")
    c.commit()
    c.close()
    r, rcfg = reader(tmp_path)
    snapshot.import_file(r, rcfg, path, full=True)
    assert db.list_jobs(r, db.Filters(benefits=("equity",), travel="low", language="english"))[1] == 0
    assert db.list_jobs(r, db.Filters(travel="low"))[1] == 1


def test_filter_bar_and_job_page(tmp_path, cfg_path):
    from lighthouse.web import create_app
    cfg, _ = cfg_file(tmp_path)
    path = tmp_path / "w.db"
    conn = db.connect(path)
    db.upsert_board(conn, "greenhouse", "acme", [posting(1, "Data Analyst", "Stock options and unlimited PTO. "
                                                                            "Fluency in German is required.")])
    fetch.postprocess(conn, cfg)
    ch = conn.execute("SELECT content_hash FROM jobs").fetchone()[0]
    conn.execute("INSERT INTO llm_extractions(content_hash, data, model, at) VALUES (?, ?, 'm', '9999')",
                 (ch, json.dumps({"visa": {"value": "yes", "evidence": "we sponsor"}})))
    conn.commit()
    llm_fill.apply(conn)
    conn.close()
    client = create_app(path, cfg_path, visa_dir=tmp_path / "visa").test_client()
    page = client.get("/?benefit=equity&benefit=unlimited_pto&posted=7&language=german&visa=yes").get_data(as_text=True)
    assert "Data Analyst" in page and "More filters (on)" in page and "German required" in page
    assert "?benefit=equity&amp;benefit=unlimited_pto&amp;posted=7" in page   # tab links keep every benefit
    assert "Data Analyst" not in client.get("/?language=english").get_data(as_text=True)
    detail = client.get("/job/greenhouse:acme:1").get_data(as_text=True)
    assert "read by Claude" in detail and "Equity / stock" in detail and "German" in detail
