"""Readers for large employers' careers systems, against payloads shaped like the real ones
(captured from each system's public endpoints in November 2026)."""
import json

from conftest import FakeClient, FakeResp

from lighthouse import discover
from lighthouse.sources import SOURCES, detect
from lighthouse.sources import eightfold, oracle, phenom
from lighthouse.sources.jsonld import english_only, job_posting


def test_oracle_list_and_detail():
    lst = {"items": [{"TotalJobsCount": 2, "requisitionList": [
        {"Id": "746368", "Title": "Data Analyst", "PostedDate": "2026-11-05", "PrimaryLocation": "Scottsdale, AZ, United States",
         "secondaryLocations": [{"Name": "Boise, ID, United States"}], "WorkplaceType": "Hybrid", "JobFamily": "Analytics"},
        {"Id": "747182", "Title": "Front End Cashier", "PostedDate": "2026-11-04", "PrimaryLocation": "Mesa, AZ"}]}]}
    det = {"items": [{"Id": "746368", "ExternalDescriptionStr": "<p>Build <b>reports</b>.</p><script>x()</script>",
                      "ExternalQualificationsStr": "<ul><li>SQL</li></ul>", "JobSchedule": "Full time",
                      "ExternalPostedStartDate": "2026-11-05T16:40:46+00:00", "Category": "Data"}]}
    c = FakeClient({"recruitingCEJobRequisitionDetails": det, "recruitingCEJobRequisitions?": lst})
    src = SOURCES["oracle"](client=c)
    import re
    src.skip_title = re.compile(r"cashier", re.I)
    [j] = src.fetch("eofd.fa.us6.oraclecloud.com/CX_1001")
    assert j.uid == "oracle:eofd.fa.us6.oraclecloud.com/CX_1001:746368"
    assert j.locations == ["Scottsdale, AZ, United States", "Boise, ID, United States"] and j.workplace == "hybrid"
    assert "<b>reports</b>" in j.content_html and "script" not in j.content_html and "SQL" in j.content_html
    assert j.employment_type == "Full time" and j.url.endswith("/sites/CX_1001/job/746368")
    assert sum("Details" in u for u in c.calls) == 1               # the filtered-out posting got no detail call
    # second run: already have the description -> no detail calls
    c.calls.clear()
    src.fetch("eofd.fa.us6.oraclecloud.com/CX_1001", known={j.uid: ("746368", True)})
    assert not any("Details" in u for u in c.calls)
    page = '<base data-apibaseurl="https://egug.fa.us2.oraclecloud.com:443" data-vanitybaseurl="https://careers.americanexpress.com/" data-sitenumber="CX_1"/>'
    assert oracle.from_page(page) == "egug.fa.us2.oraclecloud.com/CX_1"


SF_FEED = """<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>
<title>Careers at ExxonMobil</title><item><title>Network Automation Engineer (Bangalore, KA, IN, 560066)</title>
<description><![CDATA[&lt;p&gt;Strong &lt;strong&gt;Python&lt;/strong&gt; skills.&lt;/p&gt;&lt;img src=x onerror=alert(1)&gt;]]></description>
<link>https://jobs.exxonmobil.com/job/Bangalore-Network-Automation-Engineer/1414521800/</link><g:id>1414521800</g:id>
<g:location>Bangalore, KA, IN</g:location><g:employer>ExxonMobil</g:employer></item>
<item><title>Process Engineer (Houston, TX, US)</title><description><![CDATA[&lt;p&gt;Refining.&lt;/p&gt;]]></description>
<link>https://jobs.exxonmobil.com/job/Houston-Process-Engineer/22/</link><g:id>22</g:id></item></channel></rss>"""


def test_successfactors_feed():
    c = FakeClient({"/sitemap.xml": FakeResp({}, text=SF_FEED)})
    src = SOURCES["successfactors"](client=c)
    jobs = src.fetch("jobs.exxonmobil.com")
    a, b = jobs
    assert a.title == "Network Automation Engineer" and a.location == "Bangalore, KA, IN" and a.company == "ExxonMobil"
    assert "<strong>Python</strong>" in a.content_html and "onerror" not in a.content_html
    assert b.location == "Houston, TX, US" and b.external_id == "22"
    assert src.peek("jobs.exxonmobil.com") == 2
    src2 = SOURCES["successfactors"](client=c)
    src2.prev_body_hash = src.body_hash      # byte-identical feed next run: nothing to do
    import pytest
    from lighthouse.sources.base import Unchanged
    with pytest.raises(Unchanged):
        src2.fetch("jobs.exxonmobil.com")


def test_phenom_reader_and_workday_front():
    jobs = [{"jobId": "R-1", "jobSeqNo": "TFSCR1", "title": "Data Analyst", "multi_location": ["Waltham, MA, US"],
             "postedDate": "2026-09-04T00:00:00.000+0000", "type": "Full time", "category": "Operations",
             "applyUrl": "https://thermofisher.wd5.myworkdayjobs.com/ThermoFisherCareers/job/Waltham/Data-Analyst_R-1/apply"},
            {"jobId": "R-2", "jobSeqNo": "TFSCR2", "title": "Chemist", "location": "Tokyo, Japan",
             "applyUrl": "https://thermofisher.wd5.myworkdayjobs.com/ThermoFisherCareers/job/Tokyo/Chemist_R-2/apply"}]

    def widgets(body):
        if body.get("ddoKey") == "jobDetail":
            return FakeResp({"jobDetail": {"data": {"job": {"description": "<p>Analyze <em>data</em>.</p>"}}}})
        return FakeResp({"refineSearch": {"totalHits": 2, "data": {"jobs": jobs[body["from"]: body["from"] + body["size"]]}}})
    c = FakeClient({"/widgets": widgets})
    out = SOURCES["phenom"](client=c).fetch("jobs.thermofisher.com/global/en")
    assert [j.title for j in out] == ["Data Analyst", "Chemist"] and "<em>data</em>" in out[0].content_html
    assert out[0].url == "https://jobs.thermofisher.com/global/en/job/R-1" and out[0].locations == ["Waltham, MA, US"]
    page = ('var phApp = phApp || {"widgetApiEndpoint":"https://jobs.thermofisher.com/widgets","country":"global",'
            '"deviceType":"desktop","locale":"en_global","refNum":"TFSCGLOBAL"};')
    assert phenom.from_page(page) == "jobs.thermofisher.com/global/en"
    d = discover.Discoverer(c, "t")
    assert d._phenom_board("jobs.thermofisher.com/global/en") == ("workday", "thermofisher.wd5/ThermoFisherCareers")


def test_jibe_eightfold_amazon():
    jibe = {"totalCount": 1, "jobs": [{"data": {"req_id": "46671", "title": "Data Analyst - P&C", "description": "<p>Query data.</p>",
                                                "full_location": "Bloomington, IL", "posted_date": "2026-10-30T00:00:00+0000",
                                                "categories": [{"name": "Analytics"}]}}]}
    [j] = SOURCES["jibe"](client=FakeClient({"/api/jobs": jibe})).fetch("jobs.statefarm.com")
    assert (j.external_id, j.location, j.departments) == ("46671", "Bloomington, IL", ["Analytics"])

    lst = {"count": 1, "positions": [{"id": 790298014263, "name": "AI Engineer 6", "locations": ["Los Gatos, CA"],
                                      "department": "Data & Insights", "t_create": 1721692800, "t_update": 1779148800}]}
    det = {"id": 790298014263, "job_description": "<p>Build <b>ads</b> systems.</p>"}
    c = FakeClient({"/api/apply/v2/jobs/790298014263": det, "/api/apply/v2/jobs?": lst})
    [e] = SOURCES["eightfold"](client=c).fetch("explore.jobs.netflix.net|netflix.com")
    assert e.title == "AI Engineer 6" and "<b>ads</b>" in e.content_html and e.posted_at.year == 2024
    assert eightfold.from_page('<a href="https://explore.jobs.netflix.net/careers?domain=netflix.com">x</a>') == \
        "explore.jobs.netflix.net|netflix.com"

    pages = {0: [{"id_icims": "3100001", "title": "Data Engineer", "normalized_location": "Seattle, WA, USA",
                  "description": "Build pipelines.", "basic_qualifications": "- 3+ years of SQL", "posted_date": "October 1, 2026",
                  "job_path": "/en/jobs/3100001/data-engineer"}]}
    c = FakeClient({"search.json": lambda *_: None})
    c.get = lambda url, **kw: FakeResp({"jobs": pages.get(int(url.split("offset=")[1].split("&")[0]), [])})
    [a] = SOURCES["amazon"](client=c).fetch("amazon.jobs")
    assert a.url == "https://www.amazon.jobs/en/jobs/3100001/data-engineer" and "3+ years of SQL" in a.content_html
    assert a.posted_at.month == 10 and detect("https://www.amazon.jobs/en/") == ("amazon", "amazon.jobs")


def _ld(title, extra=None):
    d = {"@context": "http://schema.org", "@type": "JobPosting", "title": title, "datePosted": "2026-10-20",
         "description": "<p>Do the work.</p>", "employmentType": "FULL_TIME",
         "hiringOrganization": {"@type": "Organization", "name": "Wells Fargo"},
         "jobLocation": [{"@type": "Place", "address": [{"addressLocality": "Charlotte", "addressRegion": "NC",
                                                          "addressCountry": "United States"}]}],
         "baseSalary": {"currency": "USD", "value": {"minValue": 90000, "maxValue": 120000, "unitText": "YEAR"}}, **(extra or {})}
    return f'<html><script type="application/ld+json">{json.dumps(d)}</script></html>'


def test_jsonld_sitemap_reader_reads_only_new_or_changed_pages():
    index = '<sitemapindex><sitemap><loc>https://www.example.com/sitemap-jobs.xml</loc></sitemap></sitemapindex>'
    urls = "".join(f"<url><loc>https://www.example.com/{lang}/jobs/r-{i}/analyst/</loc><lastmod>2026-10-0{i}</lastmod></url>"
                   for i in (1, 2, 3) for lang in ("en", "fr"))
    urlset = f'<urlset><url><loc>https://www.example.com/en/</loc></url>{urls}</urlset>'
    routes = {"sitemap.xml": FakeResp({}, text=index), "sitemap-jobs.xml": FakeResp({}, text=urlset),
              "/robots.txt": FakeResp({}, 404, text="")}
    for i in (1, 2, 3):
        routes[f"/en/jobs/r-{i}/"] = FakeResp({}, text=_ld(f"Analyst {i}"))
    c = FakeClient(routes)
    src = SOURCES["jsonld"](client=c, max_detail_calls=2)
    first = src.fetch("https://www.example.com/sitemap.xml")
    assert [j.title for j in first] == ["Analyst 1", "Analyst 2"]            # budget of 2 pages this run
    j = first[0]
    assert j.company == "Wells Fargo" and j.location == "Charlotte, NC, United States"
    assert (j.salary_min, j.salary_max, j.salary_interval) == (90000, 120000, "year")
    assert not any("/fr/" in u for u in c.calls)                             # English copy only
    known = {x.uid: (x.detail_marker, True) for x in first}
    c.calls.clear()
    src = SOURCES["jsonld"](client=c, max_detail_calls=2)
    second = src.fetch("https://www.example.com/sitemap.xml", known=known)
    assert [x.title for x in second] == ["Analyst 3"]                         # only the page not read yet
    assert src.dropped == set(known)                                          # the others are kept as they are
    assert english_only(["https://a.com/en/jobs/1", "https://a.com/de-de/jobs/1", "https://a.com/jobs/2"]) == \
        ["https://a.com/en/jobs/1", "https://a.com/jobs/2"]
    assert job_posting('<script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":"X"}]}</script>')["title"] == "X"


def test_discovery_finds_oracle_on_a_vanity_domain_and_checks_it():
    page = ('<html><base data-apibaseurl="https://egug.fa.us2.oraclecloud.com:443" data-sitenumber="CX_1"/>'
            '<script src="/hcmUI/CandExpStatic/js/main.js"></script></html>')
    site = FakeClient({"careers.americanexpress.com": FakeResp({}, text=page, url="https://careers.americanexpress.com/en/sites/CX_1"),
                       "recruitingCEJobRequisitions": {"items": [{"TotalJobsCount": 1234}]}})
    site.get_orig = site.get

    def get(url, **kw):
        if url.endswith("/robots.txt"):
            return FakeResp({}, 404, text="")
        r = site.get_orig(url, **kw)
        r.headers = {"content-type": "text/html"}
        return r
    site.get = get
    r = discover.Discoverer(site, "t").resolve({"company": "American Express", "domain": "americanexpress.com",
                                                "careers_url": "https://careers.americanexpress.com/en/sites/CX_1"})
    assert (r.status, r.ats, r.slug, r.openings) == ("ok", "oracle", "egug.fa.us2.oraclecloud.com/CX_1", 1234)
