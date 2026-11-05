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
    "*.myworkdayjobs.com",
    "api.anthropic.com",
    "github.com",                  # the shared job index (release files of the [index] repo)
    "*.githubusercontent.com",     # where GitHub serves those files from
)

USER_AGENT = f"Lighthouse/{__version__} (personal job aggregator; local)"
# The job-index builder (GitHub Action) reads employers' own careers sites, so it may reach any
# https host. It says who it is and where to find the project.
BUILDER_AGENT = f"Lighthouse-index/{__version__} (+https://github.com/Newsplayer/lighthouse)"


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


class ResponseLimit(requests.RequestException):
    """A response took longer than its deadline to arrive in full, or was larger than allowed."""


class Client:
    """Thread-safe enough for our use: one requests.Session per thread.

    `deadline` (seconds) and `max_bytes` bound a whole response, not just each read: a server that
    trickles bytes or never stops can't hold a worker. Per-request overrides: get(..., deadline=,
    max_bytes=). Streamed requests (stream=True) are left to the caller."""

    def __init__(self, timeout: float = 20, per_host_rps: float = 4.0, retries: int = 3,
                 deadline: float | None = None, max_bytes: int | None = None):
        self.timeout = timeout
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.retries = retries
        self.throttle = _HostThrottle(1.0 / per_host_rps if per_host_rps > 0 else 0)
        self.counts: Counter = Counter()
        self.blocked: Counter = Counter()
        self._local = threading.local()
        self._count_lock = threading.Lock()
        self.open = False          # builder only: any https host (see open_network)
        self.agent = USER_AGENT

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            retry = Retry(total=self.retries, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                          allowed_methods=("GET", "POST"), respect_retry_after_header=True)
            s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
            s.headers["User-Agent"] = self.agent
            self._local.s = s
        return s

    def request(self, method: str, url: str, *, allow_once: bool = False, **kw) -> requests.Response:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (allow_once or self.open or host_allowed(host)):
            with self._count_lock:
                self.blocked[host] += 1
            raise BlockedHost(f"{host} is not on Lighthouse's network allowlist")
        self.throttle.wait(host)
        with self._count_lock:
            self.counts[host] += 1
        kw.setdefault("timeout", self.timeout)
        deadline, max_bytes = kw.pop("deadline", self.deadline), kw.pop("max_bytes", self.max_bytes)
        if kw.get("stream") or not (deadline or max_bytes):
            return self._session().request(method, url, **kw)
        start = time.monotonic()
        r = self._session().request(method, url, stream=True, **kw)
        buf = bytearray()
        try:
            for chunk in r.iter_content(1 << 16):
                buf += chunk
                if max_bytes and len(buf) > max_bytes:
                    raise ResponseLimit(f"{host}: response larger than {max_bytes:,} bytes")
                if deadline and time.monotonic() - start > deadline:
                    raise ResponseLimit(f"{host}: response took longer than {deadline:g} s")
        except BaseException:
            r.close()
            raise
        r._content, r._content_consumed = bytes(buf), True
        r.close()
        return r

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


def open_network(client: Client | None = None) -> Client:
    """Builder only (the job-index Action): let the client reach any https host, as the
    BUILDER_AGENT. Readers never call this; their allowlist stays as above."""
    client = client or default_client()
    client.open = True
    client.deadline = client.deadline or 300           # no single response may take more than 5 minutes
    client.max_bytes = client.max_bytes or 300_000_000
    client.agent = BUILDER_AGENT
    client._local = threading.local()   # new sessions pick up the agent
    return client
