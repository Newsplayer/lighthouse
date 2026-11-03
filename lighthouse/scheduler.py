"""Background work while the website is running: scheduled fetches and collecting
finished Claude batches. One daemon thread; never overlaps a manual refresh."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import claude_api, config, db, fetch

log = logging.getLogger("lighthouse.scheduler")
TICK_SECONDS = 300


def _due(last: str | None, hours: float) -> bool:
    if hours <= 0:
        return False
    if not last:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(hours=hours)
    except ValueError:
        return True


def tick(db_path=db.DEFAULT_DB, config_path=config.DEFAULT_CONFIG) -> None:
    cfg = config.load(config_path)
    conn = db.connect(db_path)
    try:
        if _due(db.get_meta(conn, "last_fetch"), cfg.settings.fetch_interval_hours):
            conn.close()
            fetch.run(config_path, db_path, wait=False)
            conn = db.connect(db_path)
        pending = conn.execute("SELECT 1 FROM claude_batches WHERE status <> 'ended' LIMIT 1").fetchone()
        if pending and claude_api.get_key():
            if claude_api.poll_batches(conn, cfg):
                fetch.refresh_derived(conn, cfg)
    except Exception as exc:
        log.warning("scheduled work failed: %s", exc)
    finally:
        conn.close()


def start(db_path=db.DEFAULT_DB, config_path=config.DEFAULT_CONFIG) -> threading.Thread:
    stop = threading.Event()

    def loop():
        while not stop.wait(5 if not getattr(loop, "ran", False) else TICK_SECONDS):
            loop.ran = True
            tick(db_path, config_path)

    t = threading.Thread(target=loop, name="lighthouse-scheduler", daemon=True)
    t.stop = stop
    t.start()
    return t
