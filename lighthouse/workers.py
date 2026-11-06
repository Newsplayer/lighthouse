"""CPU-heavy per-description work, run in a process pool so it uses every core.

Only pure functions live here (no database, no network), so worker processes import
little and can't step on SQLite. The parent streams descriptions in batches, the pool
returns MinHash signatures and rule-based extractions, and the parent writes them.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

RULES_VERSION = "rules-v4"   # bump when extract.rules changes so cached results are refreshed once
PARALLEL_MIN = 300           # below this many descriptions, a pool costs more than it saves
BATCH = 2000


def compute(item: tuple) -> tuple:
    """(content_hash, html, title, location, need_sig, need_rules) -> (content_hash, minhash, rules)."""
    from .dedupe import minhash
    from .extract import rules
    from .sanitize import html_to_text
    chash, html, title, location, need_sig, need_rules = item
    text = html_to_text(html or "")
    return (chash, minhash(text) if need_sig else None,
            rules(text, title or "", location or "") if need_rules else None)


def run(items: list[tuple], workers: int | None = None):
    """Yield results for items, in parallel when it's worth it. Falls back to serial on any pool error."""
    workers = workers or max(1, min(os.cpu_count() or 2, 8))  # parent mostly waits on results
    if len(items) >= PARALLEL_MIN and workers > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                yield from pool.map(compute, items, chunksize=32)
            return
        except Exception:  # e.g. a sandbox that forbids subprocesses
            pass
    for it in items:
        yield compute(it)
