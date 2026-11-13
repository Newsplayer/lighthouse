"""The job database's lifecycle (new, closed, held, deleted), the filter fields read
from descriptions, and the builder filling only the gaps the rules couldn't settle (an open model, and
categories from similar titles)."""
import json
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from lighthouse import classify, config, db, extract, fetch, llm_fill, snapshot
from lighthouse.models import Job
from lighthouse.sanitize import sanitize_html

from test_index import Builder, public_db

FILLER = " ".join(f"word{k}" for k in range(60))


def posting(eid, title, body, board="acme", company="Acme", posted_at=None):
    return Job("greenhouse", board, str(eid), title, company, "Austin, TX", f"https://x/{eid}",
               sanitize_html(f"<p>{body}</p><p>{FILLER} unique{eid}</p>"), posted_at=posted_at)


def cfg_file(tmp_path, extra=""):
    p = tmp_path / "lc.toml"
    p.write_text('[[boards]]\nats = "greenhouse"\nslug = "acme"\n' + extra)
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
    assert m["visa"] == {"value": "no", "evidence": "without sponsorship", "by": "claude"}   # older fills
    assert extract.merge_llm(rules, {"visa": {"value": "no", "evidence": "x", "by": "model"}})["visa"]["by"] == "model"


# ---------------------------------------------------------------- the open-model gap fill on the builder

class FakeModel:
    """Stands in for llama-server (local_llm.Server): answers each question with answer(user_text, schema),
    and records what it was asked."""
    name, parallel = "fake-model", 2

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def chat(self, messages, schema, max_tokens=200):
        assert messages[0]["role"] == "system" and "untrusted" in messages[0]["content"]
        assert schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])
        self.asked.append((messages[1]["content"], sorted(schema["properties"])))
        return self.answer(messages[1]["content"], schema), {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def model_says(text, schema):
    out = {k: None for k in schema["properties"]}
    out.update({k: "" for k in schema["properties"] if k.endswith("_quote")})
    if "visa_sponsorship" in out and "without the need for employer support" in text:
        out.update(visa_sponsorship="no", visa_quote="without the need for employer support")
    if "yoe_min" in out and "five plus years" in text:
        out.update(yoe_min=5, yoe_quote="five plus years")
    return out


@pytest.fixture
def builder_db(tmp_path):
    cfg, _ = cfg_file(tmp_path)
    conn = db.connect(tmp_path / "b.db")
    db.sync_config_boards(conn, cfg.boards)
    db.upsert_board(conn, "greenhouse", "acme", [
        posting(1, "Data Analyst", "Candidates must be authorized to work in the U.S. now and in the future "
                                   "without the need for employer support. 3+ years of experience."),
        posting(2, "Data Engineer", "Build pipelines. 2+ years of experience."),
        posting(3, "Wrangler of Llamas", "Keep the herd happy."),
        posting(4, "Field Analyst", "Applicants must be authorized to work in the U.S. Experience: five plus "
                                    "years in the field."),
    ])
    fetch.postprocess(conn, cfg)
    return conn, cfg


def test_gap_fill_asks_only_unsettled_fields_and_keeps_rule_answers(builder_db):
    conn, cfg = builder_db
    model = FakeModel(model_says)
    stats = llm_fill.run(conn, cfg, minutes=1, server=model)
    asked = {text.split("\n")[0]: fields for text, fields in model.asked}
    # 2 had nothing unsettled; 3 only lacks a category (that's categorize.py's job, not the model's)
    assert set(asked) == {"Title: Data Analyst", "Title: Field Analyst"}
    assert asked["Title: Data Analyst"] == ["visa_quote", "visa_sponsorship"]           # yoe was settled
    assert "yoe_min" in asked["Title: Field Analyst"] and "salary_min" not in asked["Title: Field Analyst"]
    assert all("word59" not in text for text, _ in model.asked)         # only the sentences around the field
    assert stats["read"] == 2 and stats["merged"] == 2 and stats["fields_filled"] == 2
    row = lambda eid: conn.execute("SELECT * FROM jobs WHERE external_id=?", (eid,)).fetchone()
    assert row("1")["visa"] == "no" and row("1")["yoe_min"] == 3                         # gap filled; rule kept
    assert row("4")["yoe_min"] == 5 and row("4")["yoe_source"] == "stated"
    data, method = extract.get(conn, row("1")["content_hash"])
    assert method == "rules" and data["visa"] == {"value": "no", "evidence": "without the need for employer support",
                                                  "by": "model"} and "by" not in data["yoe_min"]
    assert db.list_jobs(conn, db.Filters(visa="not_no"))[1] == 3
    again = llm_fill.run(conn, cfg, minutes=1, server=model)                              # read once per description
    assert again["read"] == 0 and len(model.asked) == 2
    conn.execute("UPDATE extractions SET model = 'old'")                                  # new rules keep the fills
    conn.commit()
    fetch.precompute(conn)
    assert extract.get(conn, row("1")["content_hash"])[0]["visa"]["value"] == "no"
    assert llm_fill.summary(conn) == {"model": "fake-model", "descriptions_read": 2, "with_answers": 2, "waiting": 0}


def test_gap_fill_keeps_only_answers_quoted_from_the_text(builder_db):
    conn, cfg = builder_db

    def makes_things_up(text, schema):
        out = {k: ("" if k.endswith("_quote") else None) for k in schema["properties"]}
        if "visa_sponsorship" in out:
            out.update(visa_sponsorship="yes", visa_quote="we sponsor H-1B visas")         # not in the posting
        if "yoe_min" in out:
            out.update(yoe_min=7, yoe_quote="five plus years")                            # quote real, number not
        return out
    stats = llm_fill.run(conn, cfg, minutes=1, server=FakeModel(makes_things_up))
    assert stats["read"] == 2 and stats["fields_filled"] == 0 and stats["dropped_ungrounded"] == 3
    assert {r["visa"] for r in conn.execute("SELECT visa FROM jobs")} == {None}
    # read, nothing usable: not read again, and the extraction says so
    data = extract.get(conn, conn.execute("SELECT content_hash FROM jobs WHERE external_id='1'").fetchone()[0])[0]
    assert "llm_read" in data and llm_fill.candidates(conn, 100, 99) == []


def test_answer_checks():
    shown = "Title: Analyst\nLocation: Remote - US\n\nExcerpts:\nThe base salary range is $85K – $110,000 per year. " \
            "Requires 3+ years’ experience. Fluent Spanish required."
    ans = {"salary_min": 85000, "salary_max": 110000, "salary_interval": "year", "salary_quote": "$85K – $110,000",
           "yoe_min": 3, "yoe_quote": "3+ years' experience", "workplace": "remote", "workplace_quote": "Remote - US",
           "languages": ["Spanish", "klingon"], "languages_quote": "fluent spanish required",
           "travel": "sometimes", "travel_quote": "x"}
    got, dropped = llm_fill.to_fields(ans, ["salary", "yoe_min", "workplace", "languages", "travel"], shown)
    assert got["salary"]["value"] == {"min": 85000, "max": 110000, "currency": None, "interval": "year"}
    assert got["yoe_min"]["value"] == 3 and got["workplace"]["value"] == "remote"   # quotes may come from the location
    assert got["languages"]["value"] == ["spanish"] and "travel" not in got and dropped == []
    for quote, level in (("Approximately 20% travel.", "occasional"), ("up to 25% of the time", "occasional"),
                         ("Travel Required: 00% - 25%", "occasional"), ("Travel: 50%", "frequent"), ("0% travel", "none")):
        got = llm_fill.to_fields({"travel": "frequent", "travel_quote": quote}, ["travel"], f"Excerpts: {quote}")[0]
        assert got["travel"]["value"] == level, quote                              # the quoted share decides
    for quote, value, kept in (("Bachelor's degree preferred", "bachelors", False),
                               ("Bachelor's Degree or equivalent work experience", "bachelors", False),
                               ("Bachelor of Science degree in Chemistry required", "bachelors", True)):
        got = llm_fill.to_fields({"degree_required": value, "degree_quote": quote}, ["degree"], f"Excerpts: {quote}")[0]
        assert ("degree" in got) == kept, quote                                    # preferred isn't required
    for quote, value, kept in (("DoD Clearance: Secret", "secret", True), ("SC level security clearance", "secret", False),
                               ("Active Secret clearance and the ability to obtain TS/SCI", "secret", True),
                               ("Active TS/SCI with poly", "ts", False), ("Must hold a security clearance", "unspecified", True)):
        got = llm_fill.to_fields({"clearance": value, "clearance_quote": quote}, ["clearance"], f"Excerpts: {quote}")[0]
        assert ("clearance" in got) == kept, quote                                 # the level is named in the quote
    bad = {**ans, "salary_max": 120000}
    assert "salary" in llm_fill.to_fields(bad, ["salary"], shown)[1]               # 120,000 isn't in the quote
    assert llm_fill.snippet("x " * 400 + "We will not sponsor a visa for this role. " + "y " * 400, ["visa"]).count("sponsor") == 1
    assert llm_fill.fields_of("visa,job_function,yoe_min") == ["visa", "yoe_min"]


def test_gap_fill_without_a_model_is_rules_only(builder_db, monkeypatch):
    conn, cfg = builder_db
    for k in ("LIGHTHOUSE_LLM_URL", "LIGHTHOUSE_LLM_SERVER", "LIGHTHOUSE_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert llm_fill.server_from_env(cfg) is None
    assert "no open model" in llm_fill.run(conn, cfg)["skipped"]
    monkeypatch.setenv("LIGHTHOUSE_LLM_URL", "https://api.example.com")
    with pytest.raises(ValueError):                                      # only a model on this machine
        llm_fill.server_from_env(cfg)


def test_gap_fills_ship_in_the_public_database_and_survive_a_lost_cache(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Must be authorized to work in the U.S. without the need for "
                                                 "employer support.")])
    conn = db.connect(b.path)
    llm_fill.run(conn, b.cfg, minutes=1, server=FakeModel(model_says))
    conn.close()
    m = b.export()
    assert m["llm"]["descriptions_read"] == 1 and m["llm"]["model"] == "fake-model"
    pub = public_db(b, tmp_path)
    assert pub.execute("SELECT visa_sponsorship, filled_by_ai FROM postings").fetchone() == ("no", "visa")
    ch = pub.execute("SELECT content_hash FROM jobs").fetchone()[0]
    assert json.loads(pub.execute("SELECT data FROM extractions").fetchone()[0])["visa"]["by"] == "model"
    # Actions cache lost: a new builder seeded from the published database knows it was already read
    seeded = tmp_path / "seeded.db"
    snapshot.seed_builder(b.dist / snapshot.DB_NAME, seeded)
    s = db.connect(seeded)
    assert s.execute("SELECT model FROM llm_extractions WHERE content_hash=?", (ch,)).fetchone()[0] == "seeded"
    assert llm_fill.candidates(s, 100, 99) == []


def test_a_description_the_model_found_nothing_in_is_not_read_again(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Must be authorized to work in the U.S. without the need for "
                                                 "employer support.")])
    conn = db.connect(b.path)
    llm_fill.run(conn, b.cfg, minutes=1, server=FakeModel(lambda text, schema: {}))   # read, nothing to add
    ch = conn.execute("SELECT content_hash FROM jobs").fetchone()[0]
    assert "llm_read" in json.loads(conn.execute("SELECT data FROM extractions").fetchone()[0])
    conn.close()
    b.export()
    seeded = tmp_path / "seeded.db"
    snapshot.seed_builder(b.dist / snapshot.DB_NAME, seeded)  # the Actions cache is gone
    s = db.connect(seeded)
    assert s.execute("SELECT data FROM llm_extractions WHERE content_hash=?", (ch,)).fetchone()[0] == "{}"
    assert llm_fill.candidates(s, 100, 99) == []              # nothing to read again
    assert public_db(b, tmp_path).execute("SELECT filled_by_ai FROM postings").fetchone()[0] is None   # no fills shown


# ---------------------------------------------------------------- job categories from similar titles

def _bag_of_words(texts):
    """A stand-in for the embedding model: one dimension per word."""
    np = pytest.importorskip("numpy")
    out = np.zeros((len(texts), 4096), dtype=np.float32)
    for i, t in enumerate(texts):
        for w in t.lower().split():
            out[i, zlib.crc32(w.encode()) % 4096] += 1   # not hash(): it differs per run, and words collided
    return out


def test_categories_from_similar_titles(tmp_path):
    pytest.importorskip("numpy")
    from lighthouse import categorize
    labeled = [(f"Software Engineer {k}", "Software Engineering") for k in range(20)] + \
              [(f"Registered Nurse unit {k}", "Healthcare & Clinical") for k in range(20)]
    got = categorize.guess(labeled, ["Staff Engineer Platform Software", "Nurse Unit Lead", "Llama Wrangler"],
                           _bag_of_words, min_sim=0.2)
    assert [g[0] for g in got] == ["Software Engineering", "Healthcare & Clinical", None]
    ev = categorize.evaluate(labeled, _bag_of_words, n=10)
    assert ev["held_out"] == 10 and ev["precision"] == 1.0

    cfg, _ = cfg_file(tmp_path)
    conn = db.connect(tmp_path / "c.db")
    db.sync_config_boards(conn, cfg.boards)
    db.upsert_board(conn, "greenhouse", "acme", [posting(k, f"Senior Software Engineer Code Platform {k}", "Build it.")
                                                 for k in range(1, 11)] +
                    [posting(50, "Senior Code Platform Wizard", "Build it well.")])     # no rule places this title
    fetch.postprocess(conn, cfg)
    assert conn.execute("SELECT job_function FROM jobs WHERE external_id='50'").fetchone()[0] is None
    stats = categorize.run(conn, embed=_bag_of_words)
    assert stats["unplaced"] == 1 and stats["guessed"] == 1
    row = conn.execute("SELECT job_function, content_hash FROM jobs WHERE external_id='50'").fetchone()
    assert row["job_function"] == "Software Engineering"
    f = extract.get(conn, row["content_hash"])[0]["job_function"]
    assert f["by"] == "titles" and f["evidence"].startswith("like: Senior Software Engineer Code Platform")
    assert categorize.run(conn, embed=_bag_of_words)["unplaced"] == 0            # done once


# ---------------------------------------------------------------- lifecycle

def test_builder_reprocesses_everything_once_after_an_upgrade(tmp_path, monkeypatch):
    """New rules or filter fields must reach companies whose boards didn't change, but only once."""
    _, cfgp = cfg_file(tmp_path, '\n[[boards]]\nats = "greenhouse"\nslug = "beta"\n')
    path = tmp_path / "b.db"
    boards = {"acme": [posting(1, "Data Analyst", "Stock options offered.")],
              "beta": [posting(2, "Accountant", "Ledgers.", board="beta", company="Beta")]}
    stub = type("S", (), {"wanted": lambda s, j: True, "dropped": set(), "body_hash": "x"})()
    monkeypatch.setattr(fetch, "_fetch_board", lambda board, *a, **k: (boards[board["slug"]], stub))
    fetch.run(cfgp, path)
    conn = db.connect(path)
    assert db.get_meta(conn, "processed_versions") == fetch.processed_versions()
    # as an older version left it: acme's row derived without the new fields
    conn.execute("UPDATE jobs SET benefits = NULL, derived_of = 'old' WHERE source_board = 'acme'")
    db.set_meta(conn, "processed_versions", {"rules": "rules-v4", "derive": "d1", "schema": 5})
    conn.close()
    monkeypatch.setattr(fetch, "_fetch_board", lambda board, *a, **k: (None, stub))   # nothing changed anywhere
    monkeypatch.setattr(db, "board_unchanged", lambda *a, **k: None)
    fetch.run(cfgp, path)
    conn = db.connect(path)
    assert classify.decode_list(conn.execute("SELECT benefits FROM jobs WHERE source_board='acme'").fetchone()[0]) == ["equity"]
    calls = []
    real = fetch.postprocess
    monkeypatch.setattr(fetch, "postprocess", lambda c, cfg, keys=None: (calls.append(keys), real(c, cfg, keys)))
    fetch.run(cfgp, path)
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


def test_long_closed_postings_are_deleted(tmp_path):
    b = Builder(tmp_path)
    b.ingest("acme", [posting(1, "Data Analyst", "Dashboards."), posting(2, "Accountant", "Ledgers.")])
    b.ingest("acme", [posting(1, "Data Analyst", "Dashboards.")])         # 2 is no longer listed: closed
    conn = db.connect(b.path)
    assert conn.execute("SELECT active FROM jobs WHERE external_id='2'").fetchone()[0] == 0
    assert db.purge_closed(conn, 45) == 0                                  # closed today: kept for now
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec="seconds")
    conn.execute("UPDATE jobs SET closed_at=? WHERE external_id='2'", (old,))
    conn.commit()
    assert db.purge_closed(conn, 45) == 1
    for table, key in (("jobs", "external_id = '2'"), ("job_content", "uid LIKE '%:2'"), ("events", "uid LIKE '%:2'")):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {key}").fetchone()[0] == 0, table
    conn.close()
    assert b.export()["db"]["rows"] == 1                                   # gone from the public database too


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
    conn.execute("UPDATE jobs SET active=0, closed_at=? WHERE external_id='2'", (old,))
    conn.commit()
    assert db.purge_closed(conn, 45) == 2       # the role is gone: both go


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


FAKE_LLAMA_SERVER = '''#!/usr/bin/env python3
import http.server, json, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
assert "--host" in sys.argv and sys.argv[sys.argv.index("--host") + 1] == "127.0.0.1"
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write("request\\n" * 50)            # chatty, like llama-server: must not stall it
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status": "ok"}')
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["temperature"] == 0 and body["response_format"]["type"] == "json_schema"
        props = body["response_format"]["json_schema"]["schema"]["properties"]
        ans = {k: ("" if k.endswith("_quote") else None) for k in props}
        b = json.dumps({"choices": [{"message": {"content": json.dumps(ans)}}], "usage": {}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def test_local_server_starts_answers_and_stops(tmp_path):
    from lighthouse import local_llm
    exe = tmp_path / "llama-server"
    exe.write_text(FAKE_LLAMA_SERVER)
    exe.chmod(0o755)
    model = tmp_path / "tiny-model-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    with local_llm.Server(exe, model, parallel=2, start_seconds=30) as server:
        assert server.name == "tiny-model-Q4_K_M" and server.url.startswith("http://127.0.0.1:")
        for _ in range(300):                       # well past a pipe's worth of log lines
            ans, _ = server.chat([{"role": "user", "content": "x"}], llm_fill.schema_for(["visa"]))
        assert ans == {"visa_sponsorship": None, "visa_quote": ""}
        proc = server.proc
    assert proc.poll() is not None                 # stopped with the block
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\necho 'cannot load model' >&2\nexit 3\n")
    broken.chmod(0o755)
    with pytest.raises(RuntimeError, match="cannot load model"):
        local_llm.Server(broken, model, start_seconds=10).__enter__()
