"""Settings from config/sources.toml, with defaults for everything.

Boards: `[[boards]] ats = "lever" slug = "palantir"`. The older `[[greenhouse]] token = ...`
form still works. Boards added from the website or CLI live in the database (boards table),
so the config file never has to be rewritten.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.toml"


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
    fetch_workers: int = 8
    fetch_interval_hours: float = 6  # 0 = no scheduled fetch while the site is running
    max_detail_calls_per_board: int = 400
    inactive_content_retention_days: int = 180
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
class Config:
    settings: Settings
    claude: ClaudeSettings
    boards: list[dict]


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
    claude = _pick(ClaudeSettings, raw.get("claude", {}))
    if "prices" in raw.get("claude", {}):
        claude.prices = {**ClaudeSettings().prices, **raw["claude"]["prices"]}
    return Config(settings=_pick(Settings, raw.get("settings", {})), claude=claude, boards=boards)
