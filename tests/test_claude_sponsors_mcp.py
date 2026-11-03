import json

import pytest

from lighthouse import claude_api, config, db, fetch, mcp_server, profile, sponsors
from lighthouse.models import Job
from conftest import FakeClient, FakeResp

BODY = "<p>" + "Own our SQL models and dashboards. " * 30 + "</p><p>3+ years of experience required.</p>"


def setup_db(tmp_path, cfg_path, n=2):
    conn = db.connect(tmp_path / "c.db")
    jobs = [Job("greenhouse", "acme", str(i), f"Analyst {i}", "Acme", "Austin, TX", f"https://x/{i}",
                BODY.replace("SQL", f"SQL{i}")) for i in range(n)]
    db.upsert_board(conn, "greenhouse", "acme", jobs)
    fetch.postprocess(conn, config.load(cfg_path))
    profile.save(conn, {**profile.DEFAULT, "about": "Analyst roles", "resume": "Pat Lee\npat@x.com\nSQL, 4 years",
                        "years_experience": 4})
    return conn


def fit_reply(body):
    out = {"tier": "Strong", "summary": "Good match.", "requirements": [
        {"requirement": "SQL", "kind": "required", "status": "met", "reason": "Resume lists SQL."}]}
    return FakeResp({"content": [{"type": "text", "text": json.dumps(out)}],
                     "usage": {"input_tokens": 3000, "output_tokens": 300, "cache_read_input_tokens": 1000}})


def test_fit_sync_records_spend_and_redacts(tmp_path, cfg_path, monkeypatch):
    conn = setup_db(tmp_path, cfg_path, n=2)
    cfg = config.load(cfg_path)
    fake = FakeClient({"/v1/messages": fit_reply})
    monkeypatch.setattr(claude_api, "default_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    prof = profile.load(conn)
    rows = claude_api.fit_candidates(conn, profile.fit_hash(prof), None, 10, 3)
    assert len(rows) == 2
    res = claude_api.run(conn, cfg, "fit", rows)
    assert res == {"sent": 2, "done": 2}
    sent = fake.calls[0][2]
    assert sent["output_config"]["format"]["type"] == "json_schema" and "tools" not in sent
    assert "pat@x.com" not in json.dumps(sent) and "Pat Lee" not in json.dumps(sent)
    assert claude_api.month_spend(conn) > 0
    assert db.list_jobs(conn, db.Filters(tier="Strong", profile_hash=profile.fit_hash(prof)))[1] == 2
    assert claude_api.fit_candidates(conn, profile.fit_hash(prof), None, 10, 3) == []  # cached


def test_spend_cap_blocks(tmp_path, cfg_path, monkeypatch):
    conn = setup_db(tmp_path, cfg_path, n=1)
    cfg = config.load(cfg_path)
    conn.execute("INSERT INTO api_usage(at, purpose, model, input_tokens, output_tokens, cost_usd) "
                 "VALUES (datetime('now'), 'fit', 'claude-sonnet-5-5', 1, 1, 0.999)")
    conn.commit()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rows = claude_api.fit_candidates(conn, profile.fit_hash(profile.load(conn)), None, 10, 3)
    with pytest.raises(claude_api.SpendCapReached):
        claude_api.run(conn, cfg, "fit", rows)


def test_extraction_batch_roundtrip(tmp_path, cfg_path, monkeypatch):
    conn = setup_db(tmp_path, cfg_path, n=3)  # batch_threshold = 3 in the test config
    cfg = config.load(cfg_path)
    rows = claude_api.extraction_candidates(conn, None, 10)
    out = {"salary_min": 90000, "salary_max": 110000, "salary_currency": "USD", "salary_interval": "year",
           "workplace": "hybrid", "yoe_min": 3, "clearance": None, "citizenship_required": None,
           "degree_required": None, "visa_sponsorship": None,
           "evidence": {"salary": "$90k-$110k", "workplace": "hybrid", "yoe_min": "3+ years", "clearance": "",
                        "citizenship": "", "degree": "", "visa": ""}}
    results = "\n".join(json.dumps({"custom_id": r["content_hash"], "result": {"type": "succeeded", "message": {
        "content": [{"type": "text", "text": json.dumps(out)}], "usage": {"input_tokens": 900, "output_tokens": 120}}}})
        for r in rows)
    fake = FakeClient({"/messages/batches/msgbatch_1": {"id": "msgbatch_1", "processing_status": "ended",
                                                        "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results"},
                       "/results": FakeResp(None, text=results),
                       "/messages/batches": {"id": "msgbatch_1", "processing_status": "in_progress"}})
    fake.routes = {"/results": fake.routes["/results"], **fake.routes}
    monkeypatch.setattr(claude_api, "default_client", lambda: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    res = claude_api.run(conn, cfg, "extract", rows)
    assert res["batch"] == "msgbatch_1" and claude_api.reserved(conn) > 0
    assert claude_api.poll_batches(conn, cfg) == 1
    fetch.refresh_derived(conn, cfg)
    r = db.get_job(conn, rows[0]["uid"])
    assert r["pay_max_annual"] == 110000 and r["workplace_eff"] == "hybrid"
    assert claude_api.reserved(conn) == 0 and claude_api.month_spend(conn) > 0


def test_sponsor_import_and_match(tmp_path, cfg_path):
    conn = setup_db(tmp_path, cfg_path, n=1)
    lca = tmp_path / "LCA_Disclosure_Data_FY2025_Q4.csv"
    lca.write_text("CASE_NUMBER,CASE_STATUS,DECISION_DATE,EMPLOYER_NAME,PW_WAGE_LEVEL\n"
                   "I-1,Certified,2025-03-01,ACME INC,II\nI-2,Certified,2025-04-01,Acme Inc.,III\n"
                   "I-3,Denied,2025-04-01,ACME INC,\nI-4,Certified,2025-04-01,Other Co,I\n")
    uscis = tmp_path / "Employer_Information.csv"
    uscis.write_text("Fiscal Year,Employer (Petitioner) Name,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial\n"
                     "2025,ACME LLC,4,1,\"1,200\",0\n", encoding="utf-16")
    assert sponsors.import_file(conn, lca)["kind"] == "lca"
    assert sponsors.import_file(conn, uscis)["kind"] == "uscis"
    assert sponsors.import_file(conn, lca)["skipped"]  # idempotent
    h = sponsors.history(conn, "acme")
    assert h["lca"][0]["c"] == 2 and h["lca"][0]["f"] == 3 and h["uscis"][0]["ca"] == 1200
    assert db.list_jobs(conn, db.Filters(visa="sponsors"))[1] == 1


def test_mcp_protocol(tmp_path, cfg_path):
    conn = setup_db(tmp_path, cfg_path, n=2)
    cfg = config.load(cfg_path)
    init = mcp_server.handle(conn, cfg, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "lighthouse"
    assert mcp_server.handle(conn, cfg, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp_server.handle(conn, cfg, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"search_jobs", "get_posting", "get_profile", "save_verdict", "set_mark"}
    call = lambda name, args: json.loads(mcp_server.handle(conn, cfg, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                         "params": {"name": name, "arguments": args}})["result"]["content"][0]["text"])
    found = call("search_jobs", {"query": "analyst"})
    assert found["total"] == 2
    uid = found["jobs"][0]["uid"]
    assert "untrusted_posting_text" in call("get_posting", {"uid": uid})
    assert "pat@x.com" not in json.dumps(call("get_profile", {}))
    call("save_verdict", {"uid": uid, "tier": "Possible", "summary": "ok", "requirements": []})
    assert call("search_jobs", {"query": "analyst", "unjudged_only": True})["total"] == 2
    assert len(call("search_jobs", {"query": "analyst", "unjudged_only": True})["jobs"]) == 1
