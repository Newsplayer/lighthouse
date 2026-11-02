"""Pull every configured board into the local database."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from . import db
from .ghost import ghost_reasons
from .sources import SOURCES

log = logging.getLogger("lighthouse.fetch")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.toml"


def load_config(path: Path | str = DEFAULT_CONFIG) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def run(config_path: Path | str = DEFAULT_CONFIG, db_path: Path | str = db.DEFAULT_DB) -> list[dict]:
    cfg = load_config(config_path)
    settings = cfg.get("settings", {})
    max_age = int(settings.get("ghost_age_days", 60))
    timeout = float(settings.get("request_timeout_seconds", 20))

    conn = db.connect(db_path)
    results = []
    for source_name, source_cls in SOURCES.items():
        source = source_cls(timeout=timeout)
        for entry in cfg.get(source_name, []):
            board = entry["token"]
            try:
                jobs = source.fetch(board)
            except Exception as exc:  # one bad board shouldn't stop the run
                log.warning("%s/%s failed: %s", source_name, board, exc)
                results.append({"source": source_name, "board": board, "error": str(exc)})
                continue
            ghosts = {j.uid: ghost_reasons(j.title, j.posted_at, max_age) for j in jobs}
            counts = db.upsert_board(conn, source_name, board, jobs, ghosts)
            log.info("%s/%s: %s", source_name, board, counts)
            results.append({"source": source_name, "board": board, **counts})
    conn.close()
    return results
