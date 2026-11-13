"""Edge cases found in review: a lost index file, search-index growth, ambiguous job links, boards of
other companies, slow responses, and conversion of an old database."""
import io

import pytest
import requests

from conftest import FakeClient, FakeResp
from test_companies import Site
from test_enterprise import _ld

from lighthouse import discover, net
from lighthouse.sources import SOURCES, detect


def fts_entries(conn):
    return conn.execute("SELECT COUNT(*) FROM idx.jobs_fts").fetchone()[0]


def test_job_links_that_differ_only_by_query_stay_apart_and_listings_go_last():
    links = "".join(f"<url><loc>https://jobs.example.com/careers/job?id={i}</loc></url>" for i in (11, 12, 13))
    listings = "".join(f"<url><loc>https://jobs.example.com/jobs/location/city-{c}</loc></url>" for c in "abcdef")
    routes = {"sitemap.xml": FakeResp({}, text=f"<urlset>{listings}{links}</urlset>"), "/robots.txt": FakeResp({}, 404, text="")}
    for i in (11, 12, 13):
        routes[f"job?id={i}"] = FakeResp({}, text=_ld(f"Analyst {i}"))
    for c in "abcdef":
        routes[f"city-{c}"] = FakeResp({}, text="<html>A list of jobs</html>")
    src = SOURCES["jsonld"](client=FakeClient(routes), max_detail_calls=3)
    got = src.fetch("https://jobs.example.com/sitemap.xml")
    assert sorted(j.title for j in got) == ["Analyst 11", "Analyst 12", "Analyst 13"]
    assert len({j.uid for j in got}) == 3
    only_listings = SOURCES["jsonld"](client=FakeClient(routes), max_detail_calls=2)
    routes["sitemap.xml"] = FakeResp({}, text=f"<urlset>{listings}</urlset>")
    assert only_listings.fetch("https://jobs.example.com/sitemap.xml") == []   # over budget: no "not found" yet


def test_careers_page_links_to_other_companies_boards_are_not_taken_as_theirs():
    links = ('<a href="https://boards.greenhouse.io/geico">GEICO</a>'
             '<a href="https://boards.greenhouse.io/duracell">Duracell</a>')
    site = Site({"https://careers.berkshire.com": links, "https://boards.greenhouse.io/geico": "<p>GEICO jobs</p>",
                 "https://boards.greenhouse.io/duracell": "<p>Duracell jobs</p>"})
    r = discover.Discoverer(site, "t").resolve({"company": "Berkshire Hathaway", "domain": "berkshire.com",
                                                "careers_url": "https://careers.berkshire.com"})
    assert r.status == "not_found" and "greenhouse:geico" in r.detail
    own = Site({"https://careers.acme.com": '<a href="https://boards.greenhouse.io/partnerco">Partner</a>'
                                          '<a href="https://boards.greenhouse.io/acmecorp">Our jobs</a>',
                "https://boards.greenhouse.io/partnerco": "<p>jobs</p>", "https://boards.greenhouse.io/acmecorp": "<p>jobs</p>"})
    r = discover.Discoverer(own, "t").resolve({"company": "Acme", "domain": "acme.com",
                                               "careers_url": "https://careers.acme.com"})
    assert (r.status, r.slug) == ("ok", "acmecorp")
    for company, domain, key, ok in [("JPMorgan Chase", "jpmorganchase.com", "jpmc", True), ("3M", "3m.com", "3m", True),
                                     ("UnitedHealth Group", "uhg.com", "uhg", True), ("Lowe's", "lowes.com", "lowes", True),
                                     ("Berkshire Hathaway", "berkshirehathaway.com", "geico", False),
                                     ("Apollo Global Management", "apollo.com", "portfolioco", False)]:
        assert discover.name_matches(company, domain, key) is ok, company
    assert detect("https://www.amazon.jobs/en/jobs/123") == ("amazon", "amazon.jobs")
    assert detect("https://example.com/?ref=amazon.jobs") is None


def trickle(kbytes):
    """A response body of `kbytes` KB, read in chunks like a slow stream."""
    r = requests.Response()
    r.status_code, r.url = 200, "https://slow.example.com/"
    r.raw = io.BytesIO(b"x" * 1000 * kbytes)
    return r


def test_whole_response_is_bounded(monkeypatch):
    c = net.open_network(net.Client(timeout=5, per_host_rps=0, max_bytes=50_000))
    sess = c._session()
    monkeypatch.setattr(sess, "request", lambda *a, **k: trickle(100))
    with pytest.raises(net.ResponseLimit):                 # larger than allowed
        c.get("https://slow.example.com/")
    monkeypatch.setattr(sess, "request", lambda *a, **k: trickle(10))
    assert len(c.get("https://slow.example.com/").content) == 10_000
    clock = iter(range(0, 10**9, 400))                     # each read "takes" 400 s
    monkeypatch.setattr(net.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sess, "request", lambda *a, **k: trickle(10))
    with pytest.raises(net.ResponseLimit):                 # slower than its deadline
        c.get("https://slow.example.com/", deadline=300)


def test_workday_sites_share_one_adaptive_budget_per_data_center(monkeypatch):
    t = net._HostThrottle(0.25)
    assert t.key("acme.wd5.myworkdayjobs.com") == t.key("other.wd5.myworkdayjobs.com") == ("wd5.myworkdayjobs.com", 1 / 3)
    assert t.key("other.wd1.myworkdayjobs.com") == ("wd1.myworkdayjobs.com", 1 / 3)
    assert t.key("wd3.myworkdaysite.com") == ("wd3.myworkdaysite.com", 1 / 3)
    assert t.key("api.lever.co") == ("api.lever.co", 0.25)
    t.slow_down("acme.wd5.myworkdayjobs.com", pause=0)
    t.slow_down("other.wd5.myworkdayjobs.com", pause=0)
    assert t._interval["wd5.myworkdayjobs.com"] == pytest.approx(4 / 3)  # every wd5 site slows down together
    assert "wd1.myworkdayjobs.com" not in t._interval                     # other data centers don't
    for _ in range(50):
        t.ok("acme.wd5.myworkdayjobs.com")
    assert t._interval["wd5.myworkdayjobs.com"] == pytest.approx(4 / 3 * 0.8)   # and eases back

    c = net.open_network(net.Client(timeout=5, per_host_rps=0))
    answers = iter([429, 429, 200])
    pauses = []

    def send(method, url, host, kw):
        r = requests.Response()
        r.status_code, r.headers["Retry-After"] = next(answers), "3"
        r._content, r.raw = b"{}", io.BytesIO()
        return r
    monkeypatch.setattr(c, "_send", send)
    monkeypatch.setattr(c.throttle, "slow_down", lambda host, pause: pauses.append(pause))
    assert c.get("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Ext/jobs").status_code == 200
    assert pauses == [3.0, 3.0]                                        # Retry-After honoured


def test_rate_limited_board_is_tried_again(monkeypatch):
    from lighthouse import fetch

    class Flaky:
        calls = 0

        def __init__(self, **kw):
            self.dropped = set()

        def fetch(self, slug, known=None):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise requests.HTTPError("Max retries exceeded (Caused by ResponseError('too many 429 error responses'))")
            return []
    monkeypatch.setitem(fetch.SOURCES, "flaky", Flaky)
    monkeypatch.setattr(fetch, "BOARD_RETRY_PAUSES", (0, 0))
    jobs, _ = fetch._fetch_board({"ats": "flaky", "slug": "x"}, {}, None, 5)
    assert jobs == [] and Flaky.calls == 2


