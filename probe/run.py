"""TEMPORARY: capture sample responses from careers systems (removed before merging)."""
import json, re, sys
sys.path.insert(0, ".")
from lighthouse.net import Client, open_network

c = open_network(Client(timeout=20, per_host_rps=2, retries=1))
HDR = {"Accept": "application/json, text/html;q=0.9, */*;q=0.8"}


def emit(label, d):
    print("PROBE " + label + " " + json.dumps(d), flush=True)


def get(label, url, n=2500, find=(), **kw):
    try:
        r = c.get(url, headers=HDR, **kw)
    except Exception as e:
        emit(label, {"error": repr(e)[:300], "url": url}); return None
    body = r.text
    snips = [body[max(0, m.start() - 150): m.end() + 400] for pat in find for m in list(re.finditer(pat, body, re.I))[:2]]
    emit(label, {"status": r.status_code, "url": r.url, "len": len(body), "ctype": r.headers.get("content-type"),
                 "head": body[:n], "snippets": snips})
    return r


def post(label, url, body, n=2500, headers=None):
    try:
        r = c.post(url, json=body, headers={"Content-Type": "application/json", **HDR, **(headers or {})})
    except Exception as e:
        emit(label, {"error": repr(e)[:300], "url": url}); return None
    emit(label, {"status": r.status_code, "url": r.url, "len": len(r.text), "head": r.text[:n]})
    return r


def ldjson(label, url):
    r = get(label, url, n=0, find=(r'<script[^>]+application/ld\+json[^>]*>',))
    if r is not None:
        blocks = re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', r.text, re.S | re.I)
        emit(label + "_ld", {"count": len(blocks), "blocks": [b[:3000] for b in blocks[:3]]})


# SuccessFactors: one full feed item
r = get("sf_exxon_feed", "https://jobs.exxonmobil.com/sitemap.xml", n=0)
if r is not None:
    items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
    emit("sf_exxon_item", {"items": len(items), "first": items[0][:5000] if items else None,
                           "tags": sorted(set(re.findall(r"<([a-zA-Z:]+)[ >]", items[0]))) if items else []})
r = get("sf_deere_feed", "https://jobs.deere.com/sitemap.xml", n=0)
if r is not None:
    items = re.findall(r"<item>(.*?)</item>", r.text, re.S)
    emit("sf_deere_item", {"items": len(items), "first": items[0][:1500] if items else None})
    sm = re.findall(r"<loc>(.*?)</loc>", r.text)[:5]
    emit("sf_deere_locs", {"locs": sm})

# Phenom: job detail through the widgets API, and a job page
post("phenom_thermo_detail", "https://jobs.thermofisher.com/widgets",
     {"lang": "en_global", "deviceType": "desktop", "country": "global", "pageName": "job", "ddoKey": "jobDetail",
      "jobSeqNo": "TFSCGLOBALR01369236EXTERNALENGLOBAL"}, n=3000)
post("phenom_ups_search", "https://www.jobs-ups.com/widgets",
     {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results", "ddoKey": "refineSearch",
      "from": 0, "size": 2, "jobs": True, "counts": True, "siteType": "external", "keywords": "", "global": True,
      "selected_fields": {}, "all_fields": ["category"], "refNum": "UPSUUS"}, n=2500)
get("phenom_ups_page", "https://www.jobs-ups.com/us/en/search-results", n=0, find=(r'"refNum":"[^"]+"', r'"applyUrl":"[^"]+"'))
get("phenom_tjx_page", "https://jobs.tjx.com/global/en/search-results", n=0, find=(r'"refNum":"[^"]+"', r'"applyUrl":"[^"]+"'))
get("phenom_autonation_page", "https://careers.autonation.com/us/en/search-results", n=0, find=(r'"refNum":"[^"]+"', r'"applyUrl":"[^"]+"'))

# Eightfold tenants
for label, host, dom in (("ef_chevron", "chevron.eightfold.ai", "chevron.com"), ("ef_starbucks", "starbucks.eightfold.ai", "starbucks.com"),
                         ("ef_netflix", "explore.jobs.netflix.net", "netflix.com"), ("ef_hp", "apply.hp.com", "hp.com"),
                         ("ef_amex", "aexp.eightfold.ai", "aexp.com")):
    r = get(label, f"https://{host}/api/apply/v2/jobs?domain={dom}&start=0&num=2", n=3000)
    try:
        pid = r.json()["positions"][0]["id"]
        get(label + "_detail", f"https://{host}/api/apply/v2/jobs/{pid}?domain={dom}", n=3000)
    except Exception as e:
        emit(label + "_detail_skip", {"error": repr(e)[:200]})
get("ef_netflix_page", "https://explore.jobs.netflix.net/careers", n=0, find=(r'"domain"\s*:\s*"[^"]+"', r"domain=[\w.]+"))
get("ef_chevron_page", "https://chevron.eightfold.ai/careers", n=0, find=(r'"domain"\s*:\s*"[^"]+"', r"domain=[\w.]+"))

# Jibe (iCIMS front end)
get("jibe_statefarm", "https://jobs.statefarm.com/api/jobs?page=1&limit=2", n=3500)
get("jibe_pepsico", "https://www.pepsicojobs.com/api/jobs?page=1&limit=2", n=1500)
get("jibe_statefarm_sitemap1", "https://jobs.statefarm.com/sitemap1.xml", n=800)

# Oracle on vanity domains: the careers page names the API host and site
get("oracle_amex_page", "https://careers.americanexpress.com/en/sites/CX_1", n=0, find=(r'data-apibaseurl="[^"]*"', r'data-sitenumber="[^"]*"', r'data-vanitybaseurl="[^"]*"'))
get("oracle_kroger_page", "https://www.krogerfamilycareers.com/en/sites/CX_2001", n=0, find=(r'data-apibaseurl="[^"]*"', r'data-sitenumber="[^"]*"'))
get("oracle_jpm_page", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001", n=0, find=(r'data-apibaseurl="[^"]*"', r'data-sitenumber="[^"]*"'))
get("oracle_page_lim", "https://eofd.fa.us6.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_1001,limit=200,offset=0,sortBy=POSTING_DATES_DESC", n=300)

# Generic sitemap + JSON-LD
for label, url in (("ford_jobs_sm", "https://www.careers.ford.com/en/sitemap.xml"), ("southern_jobs_sm", "https://southerncompany.jobs/sitemaps/jobs_1.xml"),
                   ("wf_sm", "https://www.wellsfargojobs.com/sitemap.xml"), ("walmart_sm", "https://careers.walmart.com/sitemap.xml"),
                   ("apple_sm", "https://jobs.apple.com/sitemap/sitemap-jobs-en-us.xml")):
    r = get(label, url, n=0)
    if r is not None:
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text)
        jobs = [u for u in locs if re.search(r"/(job|jobs|details|position)s?/[^/]+", u)]
        emit(label + "_summary", {"locs": len(locs), "jobish": len(jobs), "sample": jobs[:3], "other": [u for u in locs if u not in jobs][:3]})
        if jobs:
            ldjson(label + "_page", jobs[len(jobs) // 2])

# Amazon paging
get("amazon_deep", "https://www.amazon.jobs/en/search.json?result_limit=100&offset=9900&sort=recent", n=300)
get("amazon_us", "https://www.amazon.jobs/en/search.json?result_limit=1&offset=0&sort=recent&country%5B%5D=USA", n=200)
