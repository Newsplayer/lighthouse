"""TEMPORARY: summarise a test build (removed before merging)."""
import json, os, sqlite3, sys, collections
db, manifest = sys.argv[1], sys.argv[2]
m = json.load(open(manifest))
print("MANIFEST " + json.dumps({k: m[k] for k in ("jobs", "boards", "coverage", "changed_this_run")} | {"base": m["base"], "switched_off": len(m["switched_off"])}))
c = sqlite3.connect(db)
for r in c.execute("SELECT company, grp, careers_url, final_url, ats, slug, status, detail, platforms, openings FROM companies ORDER BY company"):
    print("COVER " + json.dumps(dict(zip(("company", "group", "careers_url", "final_url", "ats", "slug", "status", "detail", "platforms", "openings"), r))))
print("BYATS " + json.dumps(c.execute("SELECT b.ats, COUNT(*), SUM(b.job_count), SUM(b.last_error IS NOT NULL) FROM boards b WHERE b.enabled=1 GROUP BY b.ats").fetchall()))
print("ACTIVE " + json.dumps(c.execute("SELECT source, COUNT(*), SUM(EXISTS(SELECT 1 FROM job_content x WHERE x.uid=j.uid)) FROM jobs j WHERE active=1 GROUP BY source").fetchall()))
print("ERRORS " + json.dumps(c.execute("SELECT ats, slug, substr(last_error,1,160) FROM boards WHERE enabled=1 AND last_error IS NOT NULL LIMIT 120").fetchall()))
print("SIZES " + json.dumps({f: os.path.getsize(os.path.join("dist", f)) for f in os.listdir("dist")} | {"builder_db": os.path.getsize(db)}))
