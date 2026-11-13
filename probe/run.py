"""Where writing the public database spends its time on the real database, how xz presets compare,
and a builder seeded from the public file (roles must stay as published)."""
import json, os, shutil, subprocess, sys, time
sys.path.insert(0, os.getcwd())
from lighthouse import config as config_mod
from lighthouse import db, fetch, site, snapshot

DB = "data/builder.db"
def out(kind, obj): print(kind, json.dumps(obj, ensure_ascii=False, default=str), flush=True)
def timed(name, fn):
    t = time.time()
    try:
        res, err = fn(), None
    except Exception as e:
        import traceback; traceback.print_exc()
        res, err = None, f"{type(e).__name__}: {e}"[:300]
    out("PHASE", {"phase": name, "seconds": round(time.time() - t, 1), "error": err})
    return res

conn = db.connect(DB)
roles_before = conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_canonical = 1").fetchone()[0]
conn.close()

real_xz = snapshot._xz
def xz_probe(plain, out_path):
    out("PLAIN", {"mb": round(os.path.getsize(plain) / 1e6, 1)})
    for preset in ("-1", "-2", "-3", "-6"):
        t = time.time()
        with open(f"/tmp/x{preset}.xz", "wb") as f:
            subprocess.run(["xz", preset, "-T0", "-c", str(plain)], stdout=f, check=True)
        out("XZ", {"preset": preset, "seconds": round(time.time() - t, 1), "mb": round(os.path.getsize(f"/tmp/x{preset}.xz") / 1e6, 1)})
    t = time.time()
    subprocess.run(["zstd", "-q", "-10", "-T0", "-f", str(plain), "-o", "/tmp/x.zst"], check=False)
    if os.path.exists("/tmp/x.zst"):
        out("ZSTD", {"level": 10, "seconds": round(time.time() - t, 1), "mb": round(os.path.getsize("/tmp/x.zst") / 1e6, 1)})
    import sqlite3
    c = sqlite3.connect(plain)
    out("TABLE_MB", {r[0]: round(r[1] / 1e6, 1) for r in c.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 12")} if True else None)
    c.close()
    t = time.time(); real_xz(plain, out_path); out("PHASE", {"phase": "  xz -6 (as built)", "seconds": round(time.time() - t, 1)})
snapshot._xz = xz_probe
real_write = snapshot.write_public_db
def write_probe(src, outdir, about):
    t = time.time(); r = real_write(src, outdir, about); out("PHASE", {"phase": "  write_public_db in all (incl. the xz trials above)", "seconds": round(time.time() - t, 1)}); return r
snapshot.write_public_db = write_probe
m = timed("export", lambda: snapshot.export(DB, "dist", countries=["US", "CA", "MX"]))
snapshot._xz = real_xz; snapshot.write_public_db = real_write
out("MANIFEST", {k: m.get(k) for k in ("jobs", "postings", "db", "csv")} if m else None)

n = timed("seed a new builder from jobs.db.xz", lambda: snapshot.seed_builder("dist/jobs.db.xz", "data/seeded.db"))
s = db.connect("data/seeded.db")
cfg = config_mod.load()
timed("seeded: postprocess everything", lambda: fetch.postprocess(s, cfg))
out("SEEDED", {"postings": n, "roles_before": roles_before,
               "roles_after": s.execute("SELECT COUNT(*) FROM jobs WHERE active = 1 AND is_canonical = 1").fetchone()[0],
               "excerpts": s.execute("SELECT COUNT(*) FROM site_excerpts").fetchone()[0]})
s.close()
st = timed("seeded: site", lambda: site.generate("data/seeded.db", "site_seeded"))
out("SITE_SEEDED", st)
