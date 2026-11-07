"""Build lighthouse/data/cities.tsv.gz: city name -> country, from GeoNames (CC BY 4.0) cities of
5,000+ people. A name is kept only when one country clearly owns it (its largest city of that
name has 3x the people of any other country's), so "Cambridge" (GB/US) is left out. A name that
any US, Canadian or Mexican city also has is never given another country ("Birmingham" stays
unknown rather than GB): a wrongly foreign tag would hide a real local job; an unknown is kept."""
NA = {"US", "CA", "MX"}
import gzip, json, sys, unicodedata
from collections import defaultdict
src, out = sys.argv[1], sys.argv[2]
STOP = {"remote", "hybrid", "virtual", "home", "office", "field", "global", "central", "north", "south", "east",
        "west", "union", "college", "university", "commerce", "liberty", "independence", "mobile", "orange",
        "reading", "nice", "sale", "split", "deal", "march", "bath", "hope", "victoria", "georgia", "jersey",
        "washington", "york", "lincoln", "jackson", "florence", "manchester", "salem", "aurora", "wellington",
        "multiple", "various", "anywhere", "worldwide", "flexible", "headquarters", "hq", "onsite", "on site",
        "distribution", "plant", "warehouse", "corporate", "campus", "downtown", "midtown", "uptown", "industrial",
        "san", "santa", "saint", "mount", "fort", "port", "lake", "river", "valley", "beach", "springs"}

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("-", " ").replace("'", "").split())

best = defaultdict(dict)                      # name -> {cc: max population}
for c in json.load(open(src)).values():
    for n in {c["name"], norm(c["name"])}:
        n = n.lower().strip()
        if len(n) < 4 or n in STOP:
            continue
        p = c["population"] or 0
        if p > best[n].get(c["countrycode"], -1):
            best[n][c["countrycode"]] = p
rows = []
for n, by in best.items():
    top = sorted(by.items(), key=lambda kv: -kv[1])
    if top[0][0] not in NA and any(cc in NA for cc in by):
        continue
    if len(top) == 1 or top[0][1] >= 3 * max(top[1][1], 1):
        rows.append(f"{n}\t{top[0][0]}")
rows.sort()
with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
    f.write("# city name -> ISO country. Data: GeoNames (geonames.org), CC BY 4.0. Built by scripts/make_cities.py\n")
    f.write("\n".join(rows) + "\n")
print(len(rows), "names")
