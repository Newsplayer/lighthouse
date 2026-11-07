"""Prototype for ROADMAP P1-1: write nothing, just measure what the website's static files would weigh.

Usage: python scripts/site_prototype.py data/builder.db

Measured 2026-11-07 on the real 124k-posting builder database: 68,365 jobs in scope after
de-duplication; job list 1.4 MB gzip; keyword index 0.9 MB in 831 files (largest 26 KB);
details 41 MB on the site, ~25 KB gzip per 128-job file. The real generator (lighthouse/site.py)
should start from this."""
import gzip, html as H, json, re, sqlite3, sys, time, collections
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from lighthouse import db, geo
src = sys.argv[1]
t0 = time.time()
c = db.connect(src)
NA = {"US", "CA", "MX"}
rows = c.execute("""SELECT uid, title, company, location, locations, cluster_id, cluster_count, effective_first_seen,
  job_function, seniority, emp_type, workplace_eff, pay_min_annual, pay_max_annual, pay_interval, yoe_min, visa,
  clearance, citizenship, degree, ghost_score, skills, url, ghost_reasons, content_hash
  FROM jobs WHERE active=1 AND is_canonical=1""").fetchall()
keep = []
for r in rows:
    try: L = json.loads(r["locations"] or "[]") or [r["location"]]
    except ValueError: L = [r["location"]]
    if geo.in_scope([x for x in L if x], NA):
        keep.append(r)
print("canonical", len(rows), "in scope", len(keep))
companies = sorted({r["company"] for r in keep}); cidx = {n: i for i, n in enumerate(companies)}
day0 = 20000
def day(s):
    try: return int(time.mktime(time.strptime(s[:10], "%Y-%m-%d")) // 86400) - day0
    except Exception: return 0
cards = {"title": [], "company": [], "loc": [], "day": [], "fn": [], "sen": [], "emp": [], "wp": [], "pay": [], "yoe": [], "flags": [], "ghost": [], "n": []}
for r in keep:
    cards["title"].append(r["title"]); cards["company"].append(cidx[r["company"]]); cards["loc"].append((r["location"] or "")[:40])
    cards["day"].append(day(r["effective_first_seen"] or "")); cards["fn"].append(r["job_function"] or ""); cards["sen"].append(r["seniority"] or "")
    cards["emp"].append(r["emp_type"] or ""); cards["wp"].append(r["workplace_eff"] or ""); cards["pay"].append([int((r["pay_min_annual"] or 0)/1000), int((r["pay_max_annual"] or 0)/1000)])
    cards["yoe"].append(r["yoe_min"] if r["yoe_min"] is not None else -1); cards["flags"].append([r["visa"], r["clearance"], r["citizenship"], r["degree"]]); cards["ghost"].append(r["ghost_score"] or 0); cards["n"].append(r["cluster_count"])
blob = json.dumps({"companies": companies, **cards}, separators=(",", ":")).encode()
print(f"cards: {len(blob)/1e6:.1f} MB raw, {len(gzip.compress(blob, 9))/1e6:.1f} MB gzip")
# keyword index over title + company + skills + location
tok = re.compile(r"[a-z0-9+#]+")
post = collections.defaultdict(list)
for i, r in enumerate(keep):
    words = set(tok.findall(f"{r['title']} {r['company']} {(r['skills'] or '').replace(',', ' ')} {r['location'] or ''}".lower()))
    for w in words:
        if len(w) > 1: post[w].append(i)
def varints(ids):
    out, prev = bytearray(), 0
    for x in ids:
        d = x - prev; prev = x
        while d >= 128: out.append((d & 127) | 128); d >>= 7
        out.append(d)
    return bytes(out)
shards = collections.defaultdict(dict)
for w, ids in post.items():
    shards[w[:2]][w] = varints(ids)
sizes = []
for k, m in shards.items():
    b = json.dumps({w: v.hex() for w, v in m.items()}).encode()   # hex for the estimate; binary in reality
    raw = sum(len(v) + len(w) + 2 for w, v in m.items())
    sizes.append(len(gzip.compress(b"".join(w.encode() + b"\0" + v for w, v in m.items()), 6)))
sizes.sort()
print(f"terms: {len(post)} words in {len(shards)} shards, total {sum(sizes)/1e6:.1f} MB gzip, median shard {sizes[len(sizes)//2]/1e3:.0f} KB, largest {sizes[-1]/1e3:.0f} KB")
# detail shards: excerpt + links + skills + ghost reasons, 128 jobs per shard
c.create_function("unz", 1, db.unpack)
members = collections.defaultdict(list)
for cid, url in c.execute("SELECT cluster_id, url FROM jobs WHERE active=1"):
    members[cid].append(url)
def excerpt(h):
    t = " ".join(H.unescape(re.sub(r"<[^>]+>", " ", h or "")).split())
    return t[:600]
det_sizes, raw_total = [], 0
for s in range(0, len(keep), 128):
    chunk = []
    for r in keep[s:s+128]:
        h = c.execute("SELECT unz(html) FROM job_content WHERE uid=?", (r["uid"],)).fetchone()
        chunk.append({"u": sorted(set(members.get(r["cluster_id"], [r["url"]])))[:6], "x": excerpt(h[0] if h else ""),
                      "s": r["skills"], "g": r["ghost_reasons"], "l": r["locations"]})
    b = json.dumps(chunk, separators=(",", ":")).encode(); raw_total += len(b)
    det_sizes.append(len(gzip.compress(b, 6)))
print(f"details: {len(det_sizes)} shards, {raw_total/1e6:.0f} MB raw on the site, {sum(det_sizes)/1e6:.0f} MB gzip, one shard ~{det_sizes[0]/1e3:.0f} KB gzip")
print("seconds", round(time.time() - t0))
