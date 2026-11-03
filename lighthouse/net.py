"""Outbound HTTP. Every request Lighthouse makes goes through here.

- Host allowlist: only ATS APIs and api.anthropic.com. Anything else raises
  BlockedHost, unless the user explicitly asked for that one URL (careers-page probe).
- Per-host politeness: a minimum gap between requests to the same host, shared by
  all worker threads, so fetching many boards in parallel never hammers one API.
- Retries with backoff on 429/5xx.
- A per-host counter feeds the Network page.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from collections import Counter
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

ALLOWED_HOSTS = (
    "boards-api.greenhouse.io",
    "api.lever.co",
    "api.ashbyhq.com",
    "apply.workable.com",
    "api.smartrecruiters.com",
    "*.recruitee.com",
    "api.anthropic.com",
)

USER_AGENT = f"Lighthouse/{__version__} (personal job aggregator; local)"


class BlockedHost(Exception):
    pass


def host_allowed(host: str) -> bool:
    host = (host or "").lower()
    return any(fnmatch.fnmatch(host, pat) for pat in ALLOWED_HOSTS)


class _HostThrottle:
    """Minimum interval between request starts per host, across threads."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next.get(host, 0.0))
            self._next[host] = start + self.min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class Client:
    """Thread-safe enough for our use: one requests.Session per thread."""

    def __init__(self, timeout: float = 20, per_host_rps: float = 4.0):
        self.timeout = timeout
        self.throttle = _HostThrottle(1.0 / per_host_rps if per_host_rps > 0 else 0)
        self.counts: Counter = Counter()
        self.blocked: Counter = Counter()
        self._local = threading.local()
        self._count_lock = threading.Lock()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=("GET", "POST"), respect_retry_after_header=True)
            s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
            s.headers["User-Agent"] = USER_AGENT
            self._local.s = s
        return s

    def request(self, method: str, url: str, *, allow_once: bool = False, **kw) -> requests.Response:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (allow_once or host_allowed(host)):
            with self._count_lock:
                self.blocked[host] += 1
            raise BlockedHost(f"{host} is not on Lighthouse's network allowlist")
        self.throttle.wait(host)
        with self._count_lock:
            self.counts[host] += 1
        kw.setdefault("timeout", self.timeout)
        return self._session().request(method, url, **kw)

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)


_default: Client | None = None
_default_lock = threading.Lock()


def default_client(timeout: float = 20, per_host_rps: float = 4.0) -> Client:
    """Process-wide client so the throttle and counters are shared."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Client(timeout=timeout, per_host_rps=per_host_rps)
        return _default
