"""Time writing the website with this branch's code on the last build's database (twice: the first run
also fills the place cache)."""
import json, os, sys, time
sys.path.insert(0, os.getcwd())
from lighthouse import site
for n in (1, 2, 3):
    t = time.time()
    st = site.generate("data/builder.db", f"site_out{n}")
    print("SITE", json.dumps({"run": n, "seconds": round(time.time() - t, 1), "jobs": st["jobs"], "site_bytes": st["site_bytes"],
                              "detail_bytes": st["detail_bytes"], "new_base": st["new_base"]}), flush=True)
