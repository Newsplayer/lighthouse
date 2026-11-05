"""TEMPORARY: capture sample responses from careers systems (removed before merging)."""
import json, re, sys, time
sys.path.insert(0, ".")
from lighthouse.net import Client, open_network

c = open_network(Client(timeout=15, per_host_rps=2, retries=1))
HDR = {"Accept": "application/json, text/html;q=0.9, */*;q=0.8"}


def show(label, r, n=2500, find=()):
    body = r.text if r is not None else ""
    out = {"status": getattr(r, "status_code", None), "url": getattr(r, "url", None),
           "ctype": (r.headers.get("content-type") if r is not None else None), "len": len(body)}
    snippets = []
    for pat in find:
        for m in list(re.finditer(pat, body, re.I))[:3]:
            snippets.append(body[max(0, m.start() - 200): m.end() + 300])
    out["snippets"] = snippets
    out["head"] = body[:n]
    print("PROBE " + label + " " + json.dumps(out), flush=True)


def get(label, url, n=2500, find=(), **kw):
    try:
        r = c.get(url, headers=HDR, **kw)
    except Exception as e:
        print("PROBE " + label + " " + json.dumps({"error": repr(e)[:300], "url": url}), flush=True)
        return None
    show(label, r, n, find)
    return r


def post(label, url, body, n=2500, **kw):
    try:
        r = c.post(url, json=body, headers={"Content-Type": "application/json", **HDR}, **kw)
    except Exception as e:
        print("PROBE " + label + " " + json.dumps({"error": repr(e)[:300], "url": url}), flush=True)
        return None
    show(label, r, n)
    return r


ORA = ("/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations,"
       "flexFieldsFacet.values&finder=findReqs;siteNumber={site},facetsList=LOCATIONS,limit=3,offset=0,sortBy=POSTING_DATES_DESC")
# Oracle Recruiting Cloud: custom domain and oraclecloud host
for label, host, site in (("oracle_amex", "careers.americanexpress.com", "CX_1"),
                          ("oracle_albertsons", "eofd.fa.us6.oraclecloud.com", "CX_1001"),
                          ("oracle_kroger", "www.krogerfamilycareers.com", "CX_2001")):
    r = get(label, "https://" + host + ORA.format(site=site), n=4000)
    try:
        req = r.json()["items"][0]["requisitionList"][0]
        get(label + "_detail", f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
            f"&finder=ById;Id=%22{req['Id']}%22,siteNumber={site}", n=5000)
    except Exception as e:
        print("PROBE " + label + "_detail_skip " + json.dumps({"error": repr(e)[:200]}))
get("albertsons_page", "https://eofd.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001", n=500, find=(r"siteNumber", r"CX_\d+"))

# Eightfold
r = get("eightfold_ms", "https://apply.careers.microsoft.com/api/apply/v2/jobs?domain=microsoft.com&start=0&num=2", n=5000)
try:
    pid = r.json()["positions"][0]["id"]
    get("eightfold_ms_detail", f"https://apply.careers.microsoft.com/api/apply/v2/jobs/{pid}?domain=microsoft.com", n=5000)
except Exception as e:
    print("PROBE eightfold_ms_detail_skip " + json.dumps({"error": repr(e)[:200]}))
get("eightfold_hp_page", "https://jobs.hp.com/", n=300, find=(r"eightfold", r"[\"']domain[\"']\s*:"))
get("eightfold_chevron_page", "https://careers.chevron.com/", n=300, find=(r"eightfold", r"[\"']domain[\"']\s*:"))
get("eightfold_starbucks_page", "https://careers.starbucks.com/", n=300, find=(r"eightfold", r"[\"']domain[\"']\s*:"))

# Phenom
r = get("phenom_thermo_search", "https://jobs.thermofisher.com/global/en/search-results", n=600,
        find=(r"phApp\.ddo\s*=", r"\"refNum\"\s*:", r"eagerLoadRefineSearch", r"\"totalHits\""))
get("phenom_thermo_sitemap", "https://jobs.thermofisher.com/sitemap.xml", n=2000)
post("phenom_thermo_widgets", "https://jobs.thermofisher.com/widgets",
     {"lang": "en_global", "deviceType": "desktop", "country": "global", "pageName": "search-results", "ddoKey": "refineSearch",
      "sortBy": "", "subsearch": "", "from": 0, "jobs": True, "counts": True, "all_fields": ["category", "country"],
      "size": 2, "clearAll": False, "jdsource": "facets", "isSliderEnable": False, "pageId": "page20",
      "siteType": "external", "keywords": "", "global": True, "selected_fields": {}, "locationData": {}}, n=4000)

# SuccessFactors RMK
get("sf_exxon_home", "https://jobs.exxonmobil.com/", n=300, find=(r"successfactors", r"rmk", r"/search/\?"))
get("sf_exxon_search", "https://jobs.exxonmobil.com/search/?q=&startrow=0", n=300, find=(r"class=\"jobTitle", r"data-row", r"totalResults|of \d+ results|Results \d"))
get("sf_exxon_sitemap", "https://jobs.exxonmobil.com/sitemap.xml", n=2000)
get("sf_exxon_rss", "https://jobs.exxonmobil.com/services/rss/job/?locale=en_US&keywords=()", n=3000)
get("sf_deere_sitemap", "https://jobs.deere.com/sitemap.xml", n=1500)

# iCIMS
get("icims_statefarm_home", "https://jobs.statefarm.com/main/", n=300, find=(r"icims\.com[^\"']*", r"sitemap"))
get("icims_statefarm_sitemap", "https://jobs.statefarm.com/sitemap.xml", n=2000)
get("icims_pepsi_sitemap", "https://www.pepsicojobs.com/sitemap.xml", n=2000)

# Taleo
get("taleo_costco_page", "https://www.costco.com/f/-/careers", n=300, find=(r"taleo\.net[^\"']*",))
get("taleo_textron_page", "https://careers.textron.com", n=300, find=(r"taleo\.net[^\"']*", r"sitemap"))

# Avature / TalentBrew / generic sitemaps
get("avature_walmart_page", "https://careers.walmart.com/us/en/home", n=300, find=(r"avature[^\"']*", r"/jobs/\d"))
get("avature_walmart_robots", "https://careers.walmart.com/robots.txt", n=1500)
get("talentbrew_ford_sitemap", "https://www.careers.ford.com/sitemap.xml", n=1500)
get("talentbrew_ford_robots", "https://www.careers.ford.com/robots.txt", n=1500)
get("generic_wellsfargo_sitemap", "https://www.wellsfargojobs.com/sitemap.xml", n=1500)
get("generic_caterpillar_sitemap", "https://careers.caterpillar.com/sitemap.xml", n=1500)
get("generic_southern_sitemap", "https://southerncompany.jobs/sitemaps/index.xml", n=1500)

# custom: Amazon, Google, Apple, Goldman, Netflix
get("amazon_search", "https://www.amazon.jobs/en/search.json?result_limit=2&offset=0&sort=recent", n=4000)
get("google_robots", "https://www.google.com/about/careers/applications/robots.txt", n=1500)
get("google_jobs_page", "https://www.google.com/about/careers/applications/jobs/results", n=400, find=(r"AF_initDataCallback", r"ld\+json"))
get("apple_sitemap", "https://jobs.apple.com/sitemap/sitemap-jobs-en-us.xml", n=1500)
get("apple_robots", "https://jobs.apple.com/robots.txt", n=1500)
get("netflix_page", "https://jobs.netflix.com/", n=300, find=(r"eightfold", r"explore\.jobs\.netflix\.net[^\"']*"))
get("goldman_robots", "https://higher.gs.com/robots.txt", n=1500)
