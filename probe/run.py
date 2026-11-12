"""Where the build's own steps spend their time on the last build's database (no network):
post-processing, the build health numbers, the index files and the website."""
import cProfile, io, json, os, pstats, sys, time
sys.path.insert(0, os.getcwd())
from lighthouse import config as config_mod
from lighthouse import db, fetch, site, snapshot

DB = "data/builder.db"
def out(kind, obj): print(kind, json.dumps(obj, ensure_ascii=False, default=str), flush=True)

def profiled(name, fn):
    pr = cProfile.Profile()
    t = time.time()
    try:
        pr.enable(); res = fn(); pr.disable(); err = None
    except Exception as e:
        pr.disable(); res, err = None, f"{type(e).__name__}: {e}"[:300]
    out("PHASE", {"phase": name, "seconds": round(time.time() - t, 1), "error": err})
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(28)
    for line in s.getvalue().splitlines():
        if "/" in line or "{" in line:
            print("  PROF", name[:18], "|", " ".join(line.split())[:170], flush=True)
    return res

out("DB", {"gb": round(os.path.getsize(DB) / 1e9, 2)})
cfg = config_mod.load()
conn = db.connect(DB)
profiled("migrate+postprocess all (first)", lambda: fetch.postprocess(conn, cfg))
profiled("postprocess all (nothing changed)", lambda: fetch.postprocess(conn, cfg))
some = [r[0] for r in conn.execute("SELECT company_key FROM jobs WHERE active = 1 GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 40")]
profiled("postprocess 40 largest", lambda: fetch.postprocess(conn, cfg, some))
profiled("health", lambda: snapshot.health(conn, [], "2026-11-12T00:00:00+00:00", 1.0))
profiled("close_expired", lambda: db.close_expired(conn))
profiled("company openings", lambda: snapshot._company_openings(conn))
conn.close()
m = profiled("export (delta or base)", lambda: snapshot.export(DB, "dist", countries=["US", "CA", "MX"]))
out("EXPORT", {k: (m or {}).get(k) for k in ("jobs", "postings", "changed_this_run", "base", "deltas")})
m2 = profiled("export again (nothing changed)", lambda: snapshot.export(DB, "dist2", countries=["US", "CA", "MX"], published="dist/manifest.json"))
st = profiled("site (as the build does)", lambda: site.generate(DB, "site_out"))
out("SITE", st)
st = profiled("site again", lambda: site.generate(DB, "site_out2"))
out("SITE2", st)
