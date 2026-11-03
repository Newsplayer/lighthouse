import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


class FakeResp:
    def __init__(self, data, status=200, text=None, url=""):
        self._data, self.status_code, self.url = data, status, url
        self.text = text if text is not None else json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Maps URL substrings to canned responses; records calls."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []
        from collections import Counter
        self.counts, self.blocked = Counter(), Counter()

    def get(self, url, **kw):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                return val if isinstance(val, FakeResp) else FakeResp(val, url=url)
        return FakeResp({"error": "not found"}, 404, url=url)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("json")))
        for key, val in self.routes.items():
            if key in url:
                return val(kw.get("json")) if callable(val) else FakeResp(val, url=url)
        return FakeResp({}, 404)


@pytest.fixture
def ats():
    return json.loads((FIX / "ats_samples.json").read_text(encoding="utf-8"))


@pytest.fixture
def gh():
    return json.loads((FIX / "greenhouse_sample.json").read_text())


@pytest.fixture
def cfg_path(tmp_path):
    p = tmp_path / "sources.toml"
    p.write_text('[settings]\nghost_age_days = 60\nfetch_interval_hours = 0\n\n'
                 '[claude]\nmonthly_cap_usd = 1.0\nbatch_threshold = 3\n\n'
                 '[[boards]]\nats = "greenhouse"\nslug = "acme"\n')
    return p
