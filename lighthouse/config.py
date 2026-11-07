"""Settings from config/sources.toml, with defaults for everything.

Boards come from three places, all merged into the database `boards` table:
  - `[[boards]] ats = "lever" slug = "palantir"` in sources.toml (older `[[greenhouse]] token = ...` works too)
  - board lists: every CSV in config/boards/ (columns ats, slug, company, enabled) — e.g. the
    bundled top-companies.csv with ~900 checked employer boards
  - the Sources page / `python -m lighthouse add`
A board's on/off state is set once from its list; after that the Sources page owns it.
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
    ghost_flag_signals: int = 2      # flag at this many independent signals
    ghost_hide_signals: int = 3      # hidden by default at this many (one-click to show)
    repost_gap_days: int = 7         # a near-identical posting this much later counts as a repost
    # fetching
    request_timeout_seconds: float = 20
    per_host_requests_per_second: float = 4
    fetch_workers: int = 16          # boards in parallel (the per-host limit still applies to each ATS)
    fetch_interval_hours: float = 6  # 0 = no scheduled fetch while the site is running
    max_detail_calls_per_board: int = 150   # SmartRecruiters/Workday description calls per board per run
    detail_minutes: float = 0        # boards started after this many minutes make no description calls this
                                     # run (0 = no limit); keeps a first build of a big list inside the time limit
    disable_after_failures: int = 2   # a board that reports "not found" this many runs in a row is switched off
    inactive_content_retention_days: int = 180
    # A posting no longer listed is closed at once; it's deleted (and readers drop it) this many days
    # after it closed, once no other listing of the same role is open (repost history needs it until then).
    closed_retention_days: int = 45
    # A board that suddenly lists less than half of its open postings (20+) is more likely a glitch than
    # mass closure: those postings are closed only if the next run agrees.
    hold_mass_closures: bool = True
    skip_hourly_titles: bool = True
    # eligibility
    yoe_slack_years: int = 2


@dataclass
class ClaudeSettings:
    extract_model: str = "claude-haiku-5-5"
    fit_model: str = "claude-sonnet-5-5"
    monthly_cap_usd: float = 5.0
    batch_threshold: int = 20        # this many or more requests -> Message Batches API (50% off)
    # USD per million tokens (input, output), standard rate. Batch = half.
    prices: dict = field(default_factory=lambda: {
        "claude-haiku-5-5": [0.10, 0.50],
        "claude-sonnet-5-5": [2.0, 10.0],
        "claude-opus-5-5": [4.0, 20.0],
    })


@dataclass
class IndexSettings:
    """The shared job index. When repo is set, this copy of Lighthouse is a reader: it downloads the
    index that repo's GitHub Action publishes and never fetches boards or changes job data itself."""
    repo: str = ""                   # "owner/name" of the public repo that publishes the index
    check_every_hours: float = 1     # how often to look for a newer index while the app is open
    stale_after_hours: float = 30    # warn when the newest index is older than this
    # Builder only (`index build`, i.e. the GitHub Action): postings whose filter fields the rules
    # couldn't settle are read by Claude. Runs only when ANTHROPIC_API_KEY is set; readers ignore these.
    llm_fill: bool = True
    llm_model: str = "claude-haiku-5-5"
    llm_monthly_cap_usd: float = 10.0  # hard stop for the month (estimated before sending; batches reserve)
    llm_max_per_run: int = 5000        # newest first; the rest wait for the next run
    llm_wait_minutes: float = 20       # wait this long for the batch so results ship in the same build


@dataclass
class Config:
    settings: Settings
    claude: ClaudeSettings
    boards: list[dict]
    index: IndexSettings = field(default_factory=IndexSettings)

    @property
    def reader(self) -> bool:
        """True when job data comes from the shared index (users' normal setup)."""
        return bool(self.index.repo.strip())


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
    from .sources import SOURCES
    for ats in SOURCES:  # legacy: [[greenhouse]] token = "x"
        for b in raw.get(ats, []):
            slug = b.get("token") or b.get("slug")
            if slug:
                boards.append({"ats": ats, "slug": str(slug), "company": b.get("company"), "options": None})
    boards += load_board_lists(path.parent / BOARD_LISTS_DIR)
    claude = _pick(ClaudeSettings, raw.get("claude", {}))
    if "prices" in raw.get("claude", {}):
        claude.prices = {**ClaudeSettings().prices, **raw["claude"]["prices"]}
    return Config(settings=_pick(Settings, raw.get("settings", {})), claude=claude, boards=boards,
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
