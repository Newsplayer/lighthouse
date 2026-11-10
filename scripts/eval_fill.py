"""How well the builder's free gap fill and category guesses work, on a real builder database.

    python scripts/eval_fill.py --db data/builder.db [--server llm/bin/llama-server --model llm/model.gguf]
                                [--embed llm/embed] [--per-field 20] [--speed-minutes 4]

Model (llm_fill.py), on the same questions the build asks:
  KNOWN    postings whose rules DID read a field: ask the model that field, from the same kind of
           excerpt, and compare its (grounded) answer with the rule's. Right, wrong, or no answer.
  ABSENT   postings that never mention a field: ask anyway; any kept answer is made up.
  SPEED    the build's own loop on waiting postings for a few minutes: per hour, answers kept, dropped.
  SAMPLE   a few answers on waiting postings, to read.
Categories (categorize.py):
  CATEGORY held-out placed titles guessed from the rest (precision, coverage), and time for all unplaced.
  GUESS    a few guesses for unplaced titles, to read.
One JSON line per result. Run it in Actions with the network probe and probe/run-site (MAINTAINING.md).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lighthouse import categorize, config, db, extract, llm_fill, local_llm  # noqa: E402
from lighthouse.sanitize import html_to_text  # noqa: E402

FIELDS = ("salary", "yoe_min", "visa", "citizenship", "clearance", "degree", "workplace", "travel")


def out(kind, obj):
    print(kind, json.dumps(obj, ensure_ascii=False), flush=True)


def same(field, mine, rule) -> bool:
    if field == "salary":
        mine, rule = mine or {}, rule or {}
        close = lambda a, b: a is not None and b is not None and abs(a - b) <= 0.01 * max(abs(b), 1)
        return close(mine.get("min"), rule.get("min")) and close(mine.get("max"), rule.get("max"))
    return mine == rule


def sample_rows(conn, n_per_field: int, seed: int = 7):
    uids = [r[0] for r in conn.execute("""SELECT j.uid FROM jobs j JOIN job_content c ON c.uid = j.uid
                                          WHERE j.active = 1 AND j.is_canonical = 1 AND j.content_hash IS NOT NULL""")]
    random.Random(seed).shuffle(uids)
    rows = [conn.execute("""SELECT j.uid, j.title, j.company, j.location, e.data, unz(c.html) AS content_html
                            FROM jobs j JOIN extractions e ON e.content_hash = j.content_hash
                            JOIN job_content c ON c.uid = j.uid WHERE j.uid = ?""", (u,)).fetchone() for u in uids[:6000]]
    rows = [r for r in rows if r and r["content_html"]]
    known, absent = {f: [] for f in FIELDS}, {f: [] for f in FIELDS}
    for r in rows:
        data = json.loads(r["data"] or "{}")
        text = html_to_text(r["content_html"] or "")
        for f in FIELDS:
            v = (data.get(f) or {}).get("value")
            by = (data.get(f) or {}).get("by")
            gate = extract._GATES[f]
            if v not in (None, [], "") and not by and len(known[f]) < n_per_field and gate[1].search(text):
                known[f].append((r, v))
            elif v in (None, [], "") and len(absent[f]) < max(3, n_per_field // 4) and not gate[1].search(text) \
                    and not any(k in text.lower() for k in gate[0] if len(k) > 3):
                absent[f].append(r)
    return known, absent


def ask(server, r, fields, shown_override=None):
    messages, schema, shown = llm_fill.question(r, fields)
    if shown_override is not None:
        shown = shown_override
        messages[1]["content"] = shown
    ans, _ = server.chat(messages, schema, max_tokens=40 + 60 * len(fields))
    got, dropped = llm_fill.to_fields(ans or {}, fields, shown)
    return ans, got, dropped


def eval_model(conn, cfg, server, per_field: int, speed_minutes: float):
    known, absent = sample_rows(conn, per_field)
    totals = {"right": 0, "wrong": 0, "none": 0}
    for f in FIELDS:
        res = {"right": 0, "wrong": 0, "none": 0, "dropped": 0}
        wrong = []
        for r, rule in known[f]:
            ans, got, dropped = ask(server, r, [f])
            res["dropped"] += len(dropped)
            if f not in got:
                res["none"] += 1
            elif same(f, got[f]["value"], rule):
                res["right"] += 1
            else:
                res["wrong"] += 1
                wrong.append({"title": r["title"], "rule": rule, "model": got[f]["value"], "quote": got[f]["evidence"]})
        made_up = 0
        for r in absent[f]:
            text = html_to_text(r["content_html"] or "")
            shown = f"Title: {r['title']}\nCompany: {r['company']}\nLocation: {r['location'] or ''}\n\nExcerpts:\n{' '.join(text[:700].split())}"
            ans, got, _ = ask(server, r, [f], shown_override=shown)
            made_up += f in got
        for k in totals:
            totals[k] += res[k]
        out("KNOWN", {"model": server.name, "field": f, "asked": len(known[f]), **res,
                      "absent_asked": len(absent[f]), "absent_made_up": made_up, "wrong_examples": wrong[:3]})
    answered = totals["right"] + totals["wrong"]
    out("KNOWN_TOTAL", {"model": server.name, **totals,
                        "precision": round(totals["right"] / answered, 3) if answered else None,
                        "recall": round(totals["right"] / max(1, sum(totals.values())), 3)})

    # The build's own loop on waiting postings (this database is a throwaway copy: the probe never saves it).
    t = time.time()
    stats = llm_fill.run(conn, cfg, minutes=speed_minutes, server=server)
    shown = conn.execute("""SELECT j.title, l.data FROM llm_extractions l JOIN jobs j ON j.content_hash = l.content_hash
                            WHERE l.model = ? ORDER BY l.at DESC LIMIT 12""", (server.name,)).fetchall()
    for s in shown:
        out("SAMPLE", {"model": server.name, "title": s["title"], "kept": json.loads(s["data"])})
    out("SPEED", {"model": server.name, "minutes": round((time.time() - t) / 60, 1), **stats})


def eval_categories(conn, embed_path: str):
    embed = categorize.load_embedder(embed_path)
    if embed is None:
        return out("CATEGORY", {"error": f"no embedding model at {embed_path}"})
    labeled = []
    for r in conn.execute("SELECT title, departments FROM jobs WHERE active = 1 AND is_canonical = 1"):
        cat = categorize.classify.job_function(r["title"], r["departments"] or "")
        if cat:
            labeled.append((categorize.text_of(r["title"], r["departments"]), cat))
    t = time.time()
    ev = categorize.evaluate(labeled, embed, n=3000)
    out("CATEGORY", {"model": embed_path, "examples": len(labeled), **ev, "seconds": round(time.time() - t, 1)})
    unplaced = [categorize.text_of(r["title"], r["departments"]) for r in conn.execute(
        "SELECT title, departments FROM jobs WHERE active = 1 AND is_canonical = 1 AND job_function IS NULL")]
    t = time.time()
    got = categorize.guess(labeled, sorted(set(unplaced)), embed)
    placed = sum(1 for g in got if g[0])
    out("CATEGORY_ALL", {"unplaced_roles": len(unplaced), "unique_titles": len(got), "placed_titles": placed,
                         "seconds": round(time.time() - t, 1)})
    for text, g in random.Random(3).sample(list(zip(sorted(set(unplaced)), got)), min(25, len(got))):
        out("GUESS", {"title": text, "category": g[0], "share": g[1], "like": g[2]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/builder.db")
    ap.add_argument("--server")
    ap.add_argument("--model")
    ap.add_argument("--url", help="an OpenAI-compatible server already running on 127.0.0.1 (instead of --server)")
    ap.add_argument("--embed")
    ap.add_argument("--per-field", type=int, default=20)
    ap.add_argument("--speed-minutes", type=float, default=4)
    ap.add_argument("--parallel", type=int, default=2)
    a = ap.parse_args()
    conn = db.connect(a.db)
    cfg = config.load()
    if a.embed:
        eval_categories(conn, a.embed)
    if a.url or (a.server and a.model):
        t = time.time()
        server = local_llm.Remote(a.url, "local", a.parallel) if a.url else \
            local_llm.Server(a.server, a.model, parallel=a.parallel)
        with server:
            out("SERVER", {"model": server.name, "start_seconds": round(time.time() - t, 1)})
            eval_model(conn, cfg, server, a.per_field, a.speed_minutes)


if __name__ == "__main__":
    main()
