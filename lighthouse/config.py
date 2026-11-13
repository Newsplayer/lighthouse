"""Settings from config/sources.toml, with defaults for everything.

Boards come from three places, all merged into the builder's `boards` table:
  - `[[boards]] ats = "lever" slug = "palantir"` in sources.toml
  - board lists: every CSV in config/boards/ (columns ats, slug, company, enabled)
  - the companies list (config/companies.csv), whose careers pages discover.py resolves to boards
"""

from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.toml"
BOARD_LISTS_DIR = "boards"  # relative to the config file's folder

# Titles that are almost always hourly / frontline work. Postings matching these are skipped at
# ingest when skip_hourly_titles is on, which keeps big employers' Workday sites focused on
# professional roles (and the database small). Set skip_hourly_titles = false to keep everything.
HOURLY_TITLE_PATTERNS = (
    r"\bwarehouse (associate|worker|operator|clerk|team member|picker|packer|loader)\b", r"\bcashier\b", r"\bcdl\b",
    r"\bforklift\b", r"\b(delivery|truck|route|bus|van|shuttle|class a|class b) driver\b", r"^driver\b", r"\bcourier\b",
    r"\bstore (associate|team member|manager|clerk)\b", r"\bcrew member\b", r"\bbarista\b", r"\bcustodial\b",
    r"^custodian\b", r"\bjanitor", r"\bhousekeep", r"\b(line|prep) cook\b", r"^cook\b", r"\bdishwasher\b",
    r"\border (picker|selector)\b", r"^(picker|packer)\b", r"\bmaterial handler\b", r"\bassembler\b",
    r"\bmachine operator\b", r"\bproduction (associate|operator|worker)\b", r"\bretail (sales )?associate\b",
    r"\bstocker\b", r"^servers?\s*($|[-–(,/])", r"\b(banquet|restaurant|food|cocktail) server\b", r"\bsecurity guard\b",
    r"\bmerchandiser\b", r"\bcaregiver\b", r"\bgeneral labor", r"^mover\b", r"\bdock worker\b",
)


@dataclass
class Settings:
    # ghosts
    ghost_age_days: int = 60
    ghost_hide_signals: int = 3      # the website hides roles with this many signals by default
    repost_gap_days: int = 7         # a near-identical posting this much later counts as a repost
    # fetching
    request_timeout_seconds: float = 20
    per_host_requests_per_second: float = 4
    fetch_workers: int = 16          # boards in parallel (the per-host limit still applies to each ATS)
    max_detail_calls_per_board: int = 150   # SmartRecruiters/Workday description calls per board per run
    detail_minutes: float = 0        # boards started after this many minutes make no description calls this
                                     # run (0 = no limit); keeps a first build of a big list inside the time limit
    fetch_minutes: float = 0         # no board or description call is started after this many minutes (0 = no
                                     # limit). Boards not reached are fetched first next run; nothing is lost.
    workday_refresh_hours: float = 0  # a Workday board fetched OK more recently than this is skipped this run
                                     # (0 = every run). Every Workday site shares one rate limit per data center.
    paged_refresh_hours: float = 0   # the same for every other listing that takes many requests (0 = every run)
    disable_after_failures: int = 2   # a board that reports "not found" this many runs in a row is switched off
    inactive_content_retention_days: int = 180
    # A posting no longer listed is closed at once; it's deleted this many days
    # after it closed, once no other listing of the same role is open (repost history needs it until then).
    closed_retention_days: int = 45
    # A board that suddenly lists less than half of its open postings (20+) is more likely a glitch than
    # mass closure: those postings are closed only if the next run agrees.
    hold_mass_closures: bool = True
    skip_hourly_titles: bool = True
    countries: tuple = ()            # keep only postings located here, e.g. ("US", "CA"); empty = everywhere


@dataclass
class IndexSettings:
    """The gap fill: postings whose filter fields the rules couldn't settle are read by a free open
    model on the build machine (llm_fill.py)."""
    llm_fill: bool = True
    llm_minutes: float = 60            # reading time per build, newest postings first; the rest wait
    llm_parallel: int = 2              # questions the model works on at once
    llm_max_per_run: int = 20000


@dataclass
class Config:
    settings: Settings
    boards: list[dict]
    index: IndexSettings = field(default_factory=IndexSettings)


def _pick(cls, data: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in names})


def load(path: Path | str = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    raw = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    boards = []
    for b in raw.get("boards", []):
        if b.get("ats") and b.get("slug"):
            opts = {k: v for k, v in b.items() if k not in ("ats", "slug", "company")}
            boards.append({"ats": b["ats"].lower(), "slug": str(b["slug"]),
                           "company": b.get("company"), "options": opts or None})
    boards += load_board_lists(path.parent / BOARD_LISTS_DIR)
    return Config(settings=_pick(Settings, raw.get("settings", {})), boards=boards,
                  index=_pick(IndexSettings, raw.get("index") or {}))


def load_board_lists(folder: Path) -> list[dict]:
    """Read every CSV board list in config/boards/. Lines starting with # are comments."""
    out = []
    if not folder.is_dir():
        return out
    from .sources import SOURCES
    for path in sorted(folder.glob("*.csv")):
        with open(path, encoding="utf-8", newline="") as f:
            lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            ats, slug = (row.get("ats") or "").strip().lower(), (row.get("slug") or "").strip()
            if ats in SOURCES and slug:
                out.append({"ats": ats, "slug": slug, "company": (row.get("company") or "").strip() or None,
                            "options": None, "list": path.stem,
                            "enabled": (row.get("enabled") or "yes").strip().lower() not in ("no", "false", "0", "off")})
    return out
