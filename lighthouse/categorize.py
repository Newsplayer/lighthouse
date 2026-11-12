"""Builder only: the job category for roles whose title the rules can't place (classify.job_function).

A role the rules couldn't place takes the category of the roles it most resembles among those they did
place. Resemblance is by meaning, not exact words: each title (with its department) becomes a small vector
from a static embedding model (model2vec's potion models: numpy only, thousands of titles a second on a
CPU, no key). The role takes the category most of its nearest placed roles share, and only when they
clearly agree; otherwise it stays unknown, like any field the rules can't read.

The answer goes into the role's extraction as {"value", "evidence": "like: <titles>", "by": "titles"},
and the filter column is derived from it. It's recomputed each build for roles still without a category,
so a rules update that rewrites extractions loses nothing for long.
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import classify, extract
from .models import strings

log = logging.getLogger("lighthouse.categorize")

BY = "titles"
K = 15                 # nearest placed roles that vote
MIN_SHARE = 0.6        # share of the (similarity-weighted) vote the winning category needs
MIN_SIM = 0.5          # and how close its closest example has to be
BATCH = 512
MODEL = os.environ.get("LIGHTHOUSE_EMBED_MODEL") or "minishlab/potion-base-8M"


def text_of(title: str, departments: str | None) -> str:
    return " ".join(f"{title or ''} {' '.join(strings(departments)[:2])}".split())


def load_embedder(model: str = MODEL):
    """model2vec's encoder (a local folder, e.g. the build's llm/embed, or a Hugging Face name), or None
    when model2vec isn't installed or the folder is missing."""
    try:
        from model2vec import StaticModel
    except ImportError:
        return None
    if os.path.sep in model and not os.path.isdir(model):
        return None
    m = StaticModel.from_pretrained(model)
    return lambda texts: m.encode(list(texts), use_multiprocessing=False)


def _unit(np, x):
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, 1e-9)


def guess(labeled: list[tuple[str, str]], unknown: list[str], embed, k: int = K, min_share: float = MIN_SHARE,
          min_sim: float = MIN_SIM) -> list[tuple[str | None, float, list[str]]]:
    """For each unknown text: (category or None, share of the vote, the closest placed titles)."""
    import numpy as np
    if not unknown:
        return []
    votes: dict[str, Counter] = defaultdict(Counter)
    for text, cat in labeled:
        if text:
            votes[text][cat] += 1
    texts = list(votes)
    if not texts:
        return [(None, 0.0, []) for _ in unknown]
    cats = [votes[t].most_common(1)[0][0] for t in texts]
    names = sorted(set(cats))
    cat_ix = np.array([names.index(c) for c in cats])
    base = _unit(np, embed(texts))
    out = []
    k = min(k, len(texts))
    for i in range(0, len(unknown), BATCH):
        q = _unit(np, embed(unknown[i:i + BATCH]))
        sims = q @ base.T
        top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        for row, idx in zip(sims, top):
            s = row[idx]
            weights = np.zeros(len(names), dtype=np.float32)
            np.add.at(weights, cat_ix[idx], np.maximum(s, 0))
            best = int(weights.argmax())
            share = float(weights[best] / max(weights.sum(), 1e-9))
            closest = idx[np.argsort(-s)][:3]
            ok = share >= min_share and float(s.max()) >= min_sim
            out.append((names[best] if ok else None, round(share, 3), [texts[j] for j in closest]))
    return out


def evaluate(labeled: list[tuple[str, str]], embed, n: int = 2000, seed: int = 7) -> dict:
    """Hold out n placed roles, guess them from the rest: how often a guess is made and is right. (Placed
    titles are the easier ones, so this is an upper bound on what unplaced roles get.)"""
    import random
    rows = list({t: c for t, c in labeled if t}.items())
    random.Random(seed).shuffle(rows)
    test, train = rows[:n], rows[n:]
    got = guess(train, [t for t, _ in test], embed)
    made = [(g, c) for (g, _, _), (_, c) in zip(got, test) if g]
    right = sum(1 for g, c in made if g == c)
    return {"held_out": len(test), "guessed": len(made), "right": right,
            "precision": round(right / len(made), 3) if made else None,
            "coverage": round(len(made) / len(test), 3) if test else None}


def run(conn, embed=None) -> dict:
    """Guess the category of open roles that have none, write it into their extractions, re-derive."""
    stats = {"unplaced": 0, "guessed": 0}
    rows = conn.execute("""SELECT j.uid, j.title, j.departments, j.content_hash, j.company_key FROM jobs j
                           JOIN extractions e ON e.content_hash = j.content_hash
                           WHERE j.active = 1 AND j.is_canonical = 1 AND j.job_function IS NULL""").fetchall()
    stats["unplaced"] = len(rows)
    if not rows:
        return stats
    if embed is None:
        embed = load_embedder()
        if embed is None:
            stats["skipped"] = f"no embedding model ({MODEL}) or model2vec isn't installed (rules only)"
            return stats
    labeled = []
    for r in conn.execute("SELECT title, departments FROM jobs WHERE active = 1 AND is_canonical = 1 "
                          "AND job_function IS NOT NULL"):
        cat = classify.job_function(r["title"], r["departments"] or "")   # the rules' own answer, not a guess
        if cat:
            labeled.append((text_of(r["title"], r["departments"]), cat))
    stats["examples"] = len(labeled)
    by_text: dict[str, list] = defaultdict(list)
    for r in rows:
        by_text[text_of(r["title"], r["departments"])].append(r)
    texts = [t for t in by_text if t]
    got = guess(labeled, texts, embed)
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    keys = set()
    with conn:
        for text, (cat, share, like) in zip(texts, got):
            if not cat:
                continue
            for r in by_text[text]:
                e = conn.execute("SELECT data FROM extractions WHERE content_hash = ?", (r["content_hash"],)).fetchone()
                data = json.loads(e["data"]) if e and e["data"] else {}
                if (data.get("job_function") or {}).get("value"):
                    continue
                data["job_function"] = {"value": cat, "evidence": "like: " + "; ".join(like)[:200], "by": BY}
                conn.execute("UPDATE extractions SET data = ?, at = ? WHERE content_hash = ?",
                             (json.dumps(data, ensure_ascii=False), stamp, r["content_hash"]))
                keys.add(r["company_key"])
                stats["guessed"] += 1
    extract.derive(conn, sorted(keys))
    log.info("categories: %s", stats)
    return stats
