from lighthouse.sources import SOURCES, detect, detect_in_html
from lighthouse.sources.ashby import AshbySource
from lighthouse.sources.base import BoardNotFound
from lighthouse.sources.greenhouse import GreenhouseSource
from lighthouse.sources.lever import LeverSource
from lighthouse.sources.recruitee import RecruiteeSource
from lighthouse.sources.smartrecruiters import SmartRecruitersSource
from lighthouse.sources.workable import WorkableSource

from conftest import FakeClient, FakeResp


def test_registry_has_six_ats():
    assert set(SOURCES) == {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"}


def test_parse_greenhouse(gh):
    job = GreenhouseSource.parse("acme", gh["jobs"][0])
    assert job.uid == "greenhouse:acme:1001"
    assert job.title == "Senior Data Analyst" and job.location == "Austin, TX"
    assert job.departments == ["Analytics"] and job.url.endswith("/1001")
    assert "<strong>analytics</strong>" in job.content_html and "script" not in job.content_html
    assert job.posted_at.utcoffset().total_seconds() == 0  # normalized to UTC


def test_lever(ats):
    src = LeverSource(client=FakeClient({"api.lever.co": ats["lever"]}), company="Acme")
    [j] = src.fetch("acme")
    assert j.uid == "lever:acme:6ed76ce8-aaaa" and j.company == "Acme"
    assert j.workplace == "hybrid" and j.salary_min == 90000 and j.salary_interval == "year"
    assert j.locations == ["Austin, TX", "Remote - US"]
    assert "3+ years" in j.content_html and "sponsor" in j.content_html


def test_lever_not_found():
    src = LeverSource(client=FakeClient({"api.lever.co": {"ok": False, "error": "Document not found"}}))
    try:
        src.fetch("nope")
        assert False
    except BoardNotFound:
        pass


def test_ashby_compensation_and_unlisted(ats):
    jobs = AshbySource(client=FakeClient({"api.ashbyhq.com": ats["ashby"]})).fetch("acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Security Engineer, Cloud"
    assert (j.salary_min, j.salary_max, j.salary_currency, j.salary_interval) == (211400, 290600, "USD", "year")
    assert j.workplace == "hybrid" and j.locations == ["New York, NY (HQ)", "Remote (US)"]


def test_workable(ats):
    [j] = WorkableSource(client=FakeClient({"apply.workable.com": ats["workable"]})).fetch("huggingface")
    assert j.company == "Hugging Face" and j.external_id == "81B46579FE"
    assert j.workplace == "remote" and j.location == "Paris, Île-de-France, France"


def test_smartrecruiters_detail_only_when_new_or_changed(ats):
    client = FakeClient({"/postings?": ats["smartrecruiters_list"], "/postings/744000159462147": ats["smartrecruiters_detail"]})
    src = SmartRecruitersSource(client=client)
    [j] = src.fetch("Acme")
    assert "Audit quality" in j.content_html and j.url.endswith("quality-auditor")
    assert j.workplace == "onsite" and j.company == "Acme Group"
    detail_calls = sum(1 for c in client.calls if "/postings/7440" in c)
    assert detail_calls == 1
    # Second run: same releasedDate and we already have content -> no detail call.
    client.calls.clear()
    [j2] = src.fetch("Acme", known={j.uid: (j.detail_marker, True)})
    assert j2.content_html == "" and not any("/postings/7440" in c for c in client.calls)


def test_smartrecruiters_unknown_company_is_not_found():
    src = SmartRecruitersSource(client=FakeClient({"/postings?": {"totalFound": 0, "content": []}}))
    try:
        src.fetch("Nope")
        assert False
    except BoardNotFound:
        pass


def test_recruitee(ats):
    [j] = RecruiteeSource(client=FakeClient({"recruitee.com": ats["recruitee"]})).fetch("bunq")
    assert j.company == "bunq" and j.workplace == "hybrid"
    assert "Requirements" in j.content_html and j.posted_at.year == 2026


def test_detect_urls():
    cases = {
        "https://job-boards.greenhouse.io/anthropic": ("greenhouse", "anthropic"),
        "https://boards.greenhouse.io/embed/job_board?for=stripe": ("greenhouse", "stripe"),
        "jobs.lever.co/palantir/6ed76ce8": ("lever", "palantir"),
        "https://jobs.ashbyhq.com/ramp": ("ashby", "ramp"),
        "https://apply.workable.com/huggingface/": ("workable", "huggingface"),
        "https://jobs.smartrecruiters.com/BoschGroup/744000159462147-x": ("smartrecruiters", "BoschGroup"),
        "https://bunq.recruitee.com/o/aml-manager": ("recruitee", "bunq"),
        "lever:palantir": ("lever", "palantir"),
    }
    for url, want in cases.items():
        assert detect(url) == want, url
    assert detect("https://apply.workable.com/j/81B46579FE") is None  # a job link, not a board
    assert detect("https://example.com/careers") is None


def test_detect_embedded_board():
    html = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmeco"></script>'
    assert detect_in_html(html) == ("greenhouse", "acmeco")
    assert detect_in_html('<a href="https://jobs.ashbyhq.com/foo">Jobs</a>') == ("ashby", "foo")
